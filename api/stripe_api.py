"""
Stripe Payment API - Credit card payments for subscription upgrades.
Works alongside existing Zelle payment system.

Stripe provides INSTANT activation - no admin approval needed.
"""
import os
import stripe
import traceback
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g
from flask_restful import Api, Resource

from api.jwt_authorize import token_required
from model.subscription import Subscription, PaymentHistory
from model.user import User
from __init__ import db

# Create blueprint
stripe_api = Blueprint('stripe_api', __name__, url_prefix='/api/stripe')
api = Api(stripe_api)


# =============================================================================
# WEBHOOK ROUTE - Defined directly on blueprint to ensure raw body access
# This MUST be defined BEFORE Flask-RESTful consumes the request body
# =============================================================================
@stripe_api.route('/webhook', methods=['POST'])
def stripe_webhook():
    """
    Handle Stripe webhooks for subscription events.
    
    This is defined as a direct Flask route (not Flask-RESTful) to ensure
    we get access to the raw request body for signature verification.
    
    CRITICAL: This automatically activates subscriptions when payment succeeds.
    No admin approval needed for Stripe payments!
    """
    print("[Stripe Webhook] Received webhook request")
    
    # Get raw payload - MUST use get_data() before any JSON parsing
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    print(f"[Stripe Webhook] Signature header present: {bool(sig_header)}")
    print(f"[Stripe Webhook] Payload length: {len(payload) if payload else 0}")
    
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    
    # Verify webhook signature (important for security!)
    try:
        if webhook_secret:
            print("[Stripe Webhook] Verifying signature with webhook secret")
            event = stripe.Webhook.construct_event(
                payload, sig_header, webhook_secret
            )
        else:
            # For development without webhook secret (NOT recommended for production)
            import json
            print("[Stripe Webhook] WARNING: No webhook secret configured, skipping signature verification")
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key
            )
    except ValueError as e:
        print(f"[Stripe Webhook] Invalid payload: {str(e)}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        print(f"[Stripe Webhook] Invalid signature: {str(e)}")
        return jsonify({'error': 'Invalid signature'}), 400
    except Exception as e:
        print(f"[Stripe Webhook] Error constructing event: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'error': 'Error processing webhook'}), 400
    
    # Handle the event
    event_type = event['type']
    data = event['data']['object']
    
    print(f"[Stripe Webhook] Processing event: {event_type}")
    
    try:
        if event_type == 'checkout.session.completed':
            _handle_checkout_completed(data)
        elif event_type == 'customer.subscription.updated':
            _handle_subscription_updated(data)
        elif event_type == 'customer.subscription.deleted':
            _handle_subscription_deleted(data)
        elif event_type == 'invoice.paid':
            _handle_invoice_paid(data)
        elif event_type == 'invoice.payment_failed':
            _handle_payment_failed(data)
        else:
            print(f"[Stripe Webhook] Unhandled event type: {event_type}")
            
    except Exception as e:
        print(f"[Stripe Webhook] Error handling {event_type}: {str(e)}")
        print(traceback.format_exc())
        # Return 200 anyway to acknowledge receipt
        # Stripe will retry on non-2xx responses, but we don't want retries for processing errors
    
    print(f"[Stripe Webhook] Successfully processed event: {event_type}")
    return jsonify({'received': True}), 200


# =============================================================================
# Webhook Handler Functions (module-level for use by direct route)
# =============================================================================

def _handle_checkout_completed(session):
    """
    Handle successful checkout - ACTIVATE subscription immediately.
    """
    user_id = session.get('metadata', {}).get('user_id')
    tier = session.get('metadata', {}).get('tier')
    billing_interval = session.get('metadata', {}).get('billing_interval', 'monthly')
    stripe_subscription_id = session.get('subscription')
    customer_id = session.get('customer')
    
    if not user_id or not tier:
        print("[Stripe Webhook] Missing user_id or tier in session metadata")
        return
    
    user_id = int(user_id)
    
    # Get or create subscription
    subscription = Subscription.query.filter_by(_user_id=user_id).first()
    
    if not subscription:
        subscription = Subscription(user_id=user_id)
        db.session.add(subscription)
    
    # Calculate expiration
    if billing_interval == 'yearly':
        expires_at = datetime.utcnow() + timedelta(days=365)
    else:
        expires_at = datetime.utcnow() + timedelta(days=30)
    
    # Activate subscription
    subscription.tier = tier
    subscription.status = 'active'
    subscription.billing_interval = billing_interval
    subscription.expires_at = expires_at
    subscription.stripe_customer_id = customer_id
    subscription.stripe_subscription_id = stripe_subscription_id
    
    db.session.commit()
    
    # Record payment
    amount = session.get('amount_total', 0)
    _record_payment(
        user_id=user_id,
        subscription_id=subscription.id,
        amount=amount,
        description=f"Stripe payment - {tier.capitalize()} ({billing_interval})",
        stripe_payment_intent_id=session.get('payment_intent'),
        stripe_invoice_id=session.get('invoice')
    )
    
    print(f"[Stripe Webhook] Activated {tier} subscription for user {user_id}")


def _handle_subscription_updated(stripe_subscription):
    """
    Handle subscription updates (plan changes, etc.)
    """
    customer_id = stripe_subscription.get('customer')
    
    # Find subscription by Stripe customer ID
    subscription = Subscription.query.filter_by(_stripe_customer_id=customer_id).first()
    
    if not subscription:
        print(f"[Stripe Webhook] No subscription found for customer {customer_id}")
        return
    
    # Update Stripe subscription ID
    subscription.stripe_subscription_id = stripe_subscription.get('id')
    
    # Check if subscription is still active
    status = stripe_subscription.get('status')
    if status == 'active':
        subscription.status = 'active'
    elif status in ['past_due', 'unpaid']:
        subscription.status = 'pending'
    elif status in ['canceled', 'incomplete_expired']:
        subscription.status = 'cancelled'
    
    # Update expiration from current period end
    current_period_end = stripe_subscription.get('current_period_end')
    if current_period_end:
        subscription.expires_at = datetime.fromtimestamp(current_period_end)
    
    db.session.commit()
    print(f"[Stripe Webhook] Updated subscription for user {subscription.user_id}")


def _handle_subscription_deleted(stripe_subscription):
    """
    Handle subscription cancellation.
    """
    customer_id = stripe_subscription.get('customer')
    
    subscription = Subscription.query.filter_by(_stripe_customer_id=customer_id).first()
    
    if subscription:
        subscription.status = 'cancelled'
        subscription.tier = 'free'
        subscription.stripe_subscription_id = None
        db.session.commit()
        print(f"[Stripe Webhook] Cancelled subscription for user {subscription.user_id}")


def _handle_invoice_paid(invoice):
    """
    Handle successful recurring payment.
    """
    customer_id = invoice.get('customer')
    subscription = Subscription.query.filter_by(_stripe_customer_id=customer_id).first()
    
    if not subscription:
        return
    
    # Record the payment
    _record_payment(
        user_id=subscription.user_id,
        subscription_id=subscription.id,
        amount=invoice.get('amount_paid', 0),
        description=f"Stripe recurring payment - {subscription.tier.capitalize()}",
        stripe_payment_intent_id=invoice.get('payment_intent'),
        stripe_invoice_id=invoice.get('id')
    )
    
    # Extend subscription
    billing_reason = invoice.get('billing_reason')
    if billing_reason == 'subscription_cycle':
        # This is a renewal, extend the subscription
        if subscription.billing_interval == 'yearly':
            subscription.expires_at = datetime.utcnow() + timedelta(days=365)
        else:
            subscription.expires_at = datetime.utcnow() + timedelta(days=30)
        subscription.status = 'active'
        db.session.commit()
    
    print(f"[Stripe Webhook] Recorded payment for user {subscription.user_id}")


def _handle_payment_failed(invoice):
    """
    Handle failed payment - notify but don't immediately downgrade.
    """
    customer_id = invoice.get('customer')
    subscription = Subscription.query.filter_by(_stripe_customer_id=customer_id).first()
    
    if subscription:
        # Record failed payment attempt
        _record_payment(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            amount=invoice.get('amount_due', 0),
            description="Stripe payment failed",
            status='rejected',
            stripe_invoice_id=invoice.get('id')
        )
        print(f"[Stripe Webhook] Payment failed for user {subscription.user_id}")


def _record_payment(user_id, subscription_id, amount, description, 
                    status='paid', stripe_payment_intent_id=None, stripe_invoice_id=None):
    """
    Record a payment in the payment history.
    """
    payment = PaymentHistory(
        user_id=user_id,
        amount=amount,
        status=status,
        description=description,
        payment_method='stripe'
    )
    payment.subscription_id = subscription_id
    payment.stripe_payment_intent_id = stripe_payment_intent_id
    payment.stripe_invoice_id = stripe_invoice_id
    payment.create()
    return payment

# Initialize Stripe with your secret key from environment
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# Validate Stripe is configured on startup
if not stripe.api_key:
    print("⚠️  WARNING: STRIPE_SECRET_KEY not set in environment!")

# Price IDs from your Stripe Dashboard (loaded from environment)
STRIPE_PRICES = {
    'plus_monthly': os.environ.get('STRIPE_PRICE_PLUS_MONTHLY'),
    'plus_yearly': os.environ.get('STRIPE_PRICE_PLUS_YEARLY'),
    'pro_monthly': os.environ.get('STRIPE_PRICE_PRO_MONTHLY'),
    'pro_yearly': os.environ.get('STRIPE_PRICE_PRO_YEARLY'),
}

# Webhook secret from Stripe Dashboard
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# Frontend URLs - deployed and local
FRONTEND_URL_DEPLOYED = os.environ.get('FRONTEND_URL', 'https://ahaanv19.github.io/SD_Auto_Frontend')
FRONTEND_URL_LOCAL = os.environ.get('FRONTEND_URL_LOCAL', 'http://localhost:4887')


def get_frontend_url():
    """
    Dynamically determine frontend URL based on request origin.
    If request comes from localhost, redirect back to localhost.
    Otherwise, redirect to deployed frontend.
    """
    origin = request.headers.get('Origin', '')
    referer = request.headers.get('Referer', '')
    
    # Check if request is from localhost
    if 'localhost' in origin or '127.0.0.1' in origin or 'localhost' in referer or '127.0.0.1' in referer:
        return FRONTEND_URL_LOCAL
    
    return FRONTEND_URL_DEPLOYED


class CreateCheckoutSession(Resource):
    """
    Create a Stripe Checkout session for subscription upgrade.
    Frontend redirects user to Stripe's hosted checkout page.
    """
    @token_required()
    def post(self):
        """
        POST /api/stripe/checkout
        
        Body: {
            "tier": "plus" or "pro",  (or "plan" for frontend compatibility)
            "billing_interval": "monthly" or "yearly"  (or "billing" for frontend compatibility)
        }
        
        Returns: { "checkout_url": "https://checkout.stripe.com/..." }
        """
        try:
            current_user = g.current_user
            data = request.get_json() or {}
            
            # Accept both "tier" and "plan" for frontend compatibility
            tier = data.get('tier') or data.get('plan')
            # Accept both "billing_interval" and "billing" for frontend compatibility
            billing_interval = data.get('billing_interval') or data.get('billing', 'monthly')
            
            # Validate tier
            if tier not in ['plus', 'pro']:
                return {'message': 'Invalid tier. Must be "plus" or "pro"'}, 400
            
            # Validate billing interval
            if billing_interval not in ['monthly', 'yearly']:
                return {'message': 'Invalid billing interval. Must be "monthly" or "yearly"'}, 400
            
            # Get price ID
            price_key = f"{tier}_{billing_interval}"
            price_id = STRIPE_PRICES.get(price_key)
            
            if not price_id:
                return {'message': f'Price not configured for {tier} {billing_interval}'}, 500
            
            # Get or create Stripe customer
            subscription = Subscription.query.filter_by(_user_id=current_user.id).first()
            
            if subscription and subscription.stripe_customer_id:
                customer_id = subscription.stripe_customer_id
            else:
                # Get user email - validate it's a real email, not placeholder
                user_email = None
                if hasattr(current_user, '_email') and current_user._email and current_user._email != '?' and '@' in current_user._email:
                    user_email = current_user._email
                
                # Create new Stripe customer
                customer = stripe.Customer.create(
                    email=user_email,
                    name=current_user._name if hasattr(current_user, '_name') else current_user._uid,
                    metadata={
                        'user_id': str(current_user.id),
                        'uid': current_user._uid
                    }
                )
                customer_id = customer.id
                
                # Save customer ID to subscription
                if subscription:
                    subscription.stripe_customer_id = customer_id
                    subscription.update()
                else:
                    # Create subscription record if it doesn't exist
                    subscription = Subscription(user_id=current_user.id, tier='free', status='active')
                    subscription.stripe_customer_id = customer_id
                    subscription.create()
            
            # Get URLs from frontend request, or use auto-detected fallback
            frontend_url = get_frontend_url()
            success_url = data.get('success_url') or f"{frontend_url}/subscription/success?session_id={{CHECKOUT_SESSION_ID}}"
            cancel_url = data.get('cancel_url') or f"{frontend_url}/subscription/cancel"
            
            # Create checkout session
            checkout_session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    'user_id': str(current_user.id),
                    'tier': tier,
                    'billing_interval': billing_interval
                }
            )
            
            return {
                'checkout_url': checkout_session.url,
                'session_id': checkout_session.id
            }, 200
            
        except stripe.error.StripeError as e:
            return {'message': f'Stripe error: {str(e)}'}, 500
        except Exception as e:
            return {'message': f'Error creating checkout session: {str(e)}'}, 500


class VerifySession(Resource):
    """
    Verify a completed checkout session.
    Called by frontend after successful payment to confirm subscription is active.
    Also activates subscription as fallback if webhook didn't fire (e.g., localhost testing).
    """
    @token_required()
    def get(self):
        """
        GET /api/stripe/verify?session_id=cs_xxx
        
        Returns current subscription status after payment.
        Also activates subscription if not already activated by webhook.
        """
        try:
            current_user = g.current_user
            session_id = request.args.get('session_id')
            
            if not session_id:
                return {'message': 'session_id is required'}, 400
            
            # Retrieve the session from Stripe
            session = stripe.checkout.Session.retrieve(session_id)
            
            # Verify this session belongs to the current user
            if session.metadata.get('user_id') != str(current_user.id):
                return {'message': 'Session does not belong to current user'}, 403
            
            # Check payment status
            if session.payment_status != 'paid':
                return {
                    'message': 'Payment not completed',
                    'payment_status': session.payment_status
                }, 400
            
            # Get or create subscription
            subscription = Subscription.query.filter_by(_user_id=current_user.id).first()
            
            # FALLBACK: If subscription not activated yet (webhook didn't fire), activate it now
            tier = session.metadata.get('tier')
            billing_interval = session.metadata.get('billing_interval', 'monthly')
            
            if subscription and tier and subscription.tier != tier:
                # Subscription exists but tier not updated - webhook didn't fire
                print(f"[Stripe Verify] Activating subscription for user {current_user.id} (webhook fallback)")
                
                # Calculate expiration
                if billing_interval == 'yearly':
                    expires_at = datetime.utcnow() + timedelta(days=365)
                else:
                    expires_at = datetime.utcnow() + timedelta(days=30)
                
                subscription.tier = tier
                subscription.status = 'active'
                subscription.billing_interval = billing_interval
                subscription.expires_at = expires_at
                subscription.stripe_customer_id = session.get('customer')
                subscription.stripe_subscription_id = session.get('subscription')
                db.session.commit()
                
                # Record payment if not already recorded
                existing_payment = PaymentHistory.query.filter_by(
                    _user_id=current_user.id,
                    _stripe_payment_intent_id=session.get('payment_intent')
                ).first()
                
                if not existing_payment:
                    payment = PaymentHistory(
                        user_id=current_user.id,
                        amount=session.get('amount_total', 0),
                        status='paid',
                        description=f"Stripe payment - {tier.capitalize()} ({billing_interval})",
                        payment_method='stripe'
                    )
                    payment.subscription_id = subscription.id
                    payment.stripe_payment_intent_id = session.get('payment_intent')
                    payment.stripe_invoice_id = session.get('invoice')
                    payment.create()
            
            elif not subscription and tier:
                # No subscription exists - create one
                print(f"[Stripe Verify] Creating subscription for user {current_user.id} (webhook fallback)")
                
                if billing_interval == 'yearly':
                    expires_at = datetime.utcnow() + timedelta(days=365)
                else:
                    expires_at = datetime.utcnow() + timedelta(days=30)
                
                subscription = Subscription(user_id=current_user.id, tier=tier, status='active', billing_interval=billing_interval)
                subscription.expires_at = expires_at
                subscription.stripe_customer_id = session.get('customer')
                subscription.stripe_subscription_id = session.get('subscription')
                subscription.create()
                
                # Record payment
                payment = PaymentHistory(
                    user_id=current_user.id,
                    amount=session.get('amount_total', 0),
                    status='paid',
                    description=f"Stripe payment - {tier.capitalize()} ({billing_interval})",
                    payment_method='stripe'
                )
                payment.subscription_id = subscription.id
                payment.stripe_payment_intent_id = session.get('payment_intent')
                payment.stripe_invoice_id = session.get('invoice')
                payment.create()
            
            return {
                'message': 'Payment verified and subscription activated',
                'subscription': subscription.read() if subscription else None
            }, 200
            
        except stripe.error.StripeError as e:
            return {'message': f'Stripe error: {str(e)}'}, 500
        except Exception as e:
            return {'message': f'Error verifying session: {str(e)}'}, 500


class CancelStripeSubscription(Resource):
    """
    Cancel a Stripe subscription.
    Can cancel immediately or at end of billing period.
    """
    @token_required()
    def post(self):
        """
        POST /api/stripe/cancel
        
        Body: {
            "immediate": false  // true = cancel now, false = cancel at period end
        }
        """
        try:
            current_user = g.current_user
            data = request.get_json() or {}
            immediate = data.get('immediate', False)
            
            subscription = Subscription.query.filter_by(_user_id=current_user.id).first()
            
            if not subscription:
                return {'message': 'No subscription found'}, 404
            
            if not subscription.stripe_subscription_id:
                return {'message': 'No Stripe subscription to cancel. Use Zelle cancellation for Zelle subscriptions.'}, 400
            
            if immediate:
                # Cancel immediately
                stripe.Subscription.delete(subscription.stripe_subscription_id)
                subscription.status = 'cancelled'
                subscription.tier = 'free'
                subscription.stripe_subscription_id = None
            else:
                # Cancel at end of billing period
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    cancel_at_period_end=True
                )
                subscription.status = 'active'  # Still active until period ends
            
            db.session.commit()
            
            return {
                'message': 'Subscription cancelled' + (' immediately' if immediate else ' at end of billing period'),
                'subscription': subscription.read()
            }, 200
            
        except stripe.error.StripeError as e:
            return {'message': f'Stripe error: {str(e)}'}, 500
        except Exception as e:
            return {'message': f'Error cancelling subscription: {str(e)}'}, 500


class CreateBillingPortal(Resource):
    """
    Create a Stripe Billing Portal session.
    Allows users to manage their payment methods, view invoices, etc.
    """
    @token_required()
    def post(self):
        """
        POST /api/stripe/billing-portal
        
        Returns: { "portal_url": "https://billing.stripe.com/..." }
        """
        try:
            current_user = g.current_user
            
            subscription = Subscription.query.filter_by(_user_id=current_user.id).first()
            
            if not subscription or not subscription.stripe_customer_id:
                return {'message': 'No Stripe customer found. Please make a payment first.'}, 404
            
            frontend_url = get_frontend_url()
            portal_session = stripe.billing_portal.Session.create(
                customer=subscription.stripe_customer_id,
                return_url=f"{frontend_url}/subscription"
            )
            
            return {
                'portal_url': portal_session.url
            }, 200
            
        except stripe.error.StripeError as e:
            return {'message': f'Stripe error: {str(e)}'}, 500
        except Exception as e:
            return {'message': f'Error creating billing portal: {str(e)}'}, 500


class GetStripeConfig(Resource):
    """
    Get Stripe publishable key for frontend.
    """
    def get(self):
        """
        GET /api/stripe/config
        
        Returns the publishable key for Stripe.js initialization.
        """
        publishable_key = os.environ.get('STRIPE_PUBLISHABLE_KEY')
        
        if not publishable_key:
            return {'message': 'Stripe publishable key not configured'}, 500
        
        return {
            'publishable_key': publishable_key,
            'prices': {
                'plus_monthly': STRIPE_PRICES['plus_monthly'],
                'plus_yearly': STRIPE_PRICES['plus_yearly'],
                'pro_monthly': STRIPE_PRICES['pro_monthly'],
                'pro_yearly': STRIPE_PRICES['pro_yearly'],
            }
        }, 200


# Register resources
# NOTE: /webhook is handled by a direct Flask route defined at the top of this file
# This is necessary to ensure we get access to the raw request body for signature verification
api.add_resource(CreateCheckoutSession, '/checkout', '/create-checkout-session')
api.add_resource(VerifySession, '/verify', '/verify-session')
api.add_resource(CancelStripeSubscription, '/cancel')
api.add_resource(CreateBillingPortal, '/billing-portal')
api.add_resource(GetStripeConfig, '/config')
