"""
Subscription API endpoints for Zelle-based payment system.

This module provides REST API endpoints for:
- User subscription management (view status, request upgrades, cancel)
- Admin subscription management (approve/reject requests, view all users)

Payment Flow:
1. User selects plan and clicks "Upgrade"
2. User sees Zelle payment instructions (phone: 858-205-9428, email: ahaanvk@gmail.com)
3. User sends payment via Zelle
4. User confirms payment on website (POST /api/subscription/request)
5. Request goes to "pending" status
6. Admin reviews pending requests (GET /api/admin/subscriptions/pending)
7. Admin approves (POST /api/admin/subscriptions/approve) or rejects
8. User gets access to premium features

Zelle Payment Info:
- Phone: 858-205-9428
- Email: ahaanvk@gmail.com
- Users must include their username in the memo
"""
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g
from flask_restful import Api, Resource
from functools import wraps

from __init__ import db
from model.subscription import Subscription, SubscriptionRequest, PaymentHistory, RouteUsage
from model.user import User
from api.jwt_authorize import token_required

# Create Blueprint
subscription_api = Blueprint('subscription_api', __name__, url_prefix='/api')
api = Api(subscription_api)

# Pricing configuration
PRICING = {
    'plus': {
        'monthly': 4.99,
        'yearly': 47.88  # 20% discount from $59.88
    },
    'pro': {
        'monthly': 9.99,
        'yearly': 95.88  # 20% discount from $119.88
    }
}

# Zelle payment info
ZELLE_INFO = {
    'phone': '858-205-9428',
    'email': 'ahaanvk@gmail.com'
}

# Feature access configuration
# Each feature specifies which tiers can access it and any limits
FEATURE_ACCESS = {
    'daily_routine': {
        'name': 'Daily Routine',
        'description': 'Save and manage daily commute routines',
        'allowed_tiers': ['plus', 'pro', 'admin'],
        'limits': {
            'free': 0,
            'plus': -1,  # unlimited
            'pro': -1,
            'admin': -1
        }
    },
    'favorite_locations': {
        'name': 'Favorite Locations',
        'description': 'Save frequently visited locations',
        'allowed_tiers': ['plus', 'pro', 'admin'],
        'limits': {
            'free': 0,
            'plus': 10,  # max 10 locations
            'pro': -1,   # unlimited
            'admin': -1
        }
    },
    'incident_report': {
        'name': 'Incident Report',
        'description': 'Report and track traffic incidents',
        'allowed_tiers': ['plus', 'pro', 'admin'],
        'limits': {
            'free': 0,
            'plus': -1,
            'pro': -1,
            'admin': -1
        }
    },
    'route_calculation': {
        'name': 'Route Calculation',
        'description': 'Calculate optimal routes',
        'allowed_tiers': ['free', 'plus', 'pro', 'admin'],
        'limits': {
            'free': 4,    # 4 per day
            'plus': 50,   # 50 per day
            'pro': -1,    # unlimited
            'admin': -1
        }
    }
}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_user_tier(user):
    """
    Get subscription tier for a user.
    Admins automatically get 'admin' tier with full access.
    
    Args:
        user: User object from database
        
    Returns:
        str: 'admin', 'pro', 'plus', or 'free'
    """
    # Admin users get full access
    if hasattr(user, 'role') and user.role == 'Admin':
        return 'admin'
    
    # Check subscription
    subscription = Subscription.query.filter_by(_user_id=user.id).first()
    
    if not subscription:
        return 'free'
    
    # Check if subscription is active and not expired
    if subscription.status != 'active':
        return 'free'
    
    if subscription.expires_at and subscription.expires_at < datetime.utcnow():
        return 'free'
    
    return subscription.tier or 'free'


def require_tier(min_tier):
    """
    Decorator to require minimum subscription tier for an endpoint.
    
    Tier hierarchy: free < plus < pro < admin
    
    Usage:
        @token_required()
        @require_tier('plus')
        def my_endpoint():
            # Only Plus, Pro, or Admin users can access
            pass
    
    Args:
        min_tier (str): Minimum required tier - 'free', 'plus', 'pro'
        
    Returns:
        403 if user doesn't have sufficient tier
    """
    tier_levels = {'free': 0, 'plus': 1, 'pro': 2, 'admin': 3}
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_tier = get_user_tier(g.current_user)
            user_level = tier_levels.get(user_tier, 0)
            required_level = tier_levels.get(min_tier, 0)
            
            if user_level < required_level:
                return jsonify({
                    'error': 'Subscription required',
                    'message': f'This feature requires {min_tier} tier or higher',
                    'required_tier': min_tier,
                    'current_tier': user_tier,
                    'upgrade_url': '/subscription'
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_admin():
    """
    Decorator to require admin role for an endpoint.
    
    Usage:
        @token_required()
        @require_admin()
        def admin_endpoint():
            # Only Admin users can access
            pass
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g.current_user, 'role') or g.current_user.role != 'Admin':
                return jsonify({'error': 'Admin access required'}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def requires_feature(feature_name):
    """
    Decorator to require access to a specific premium feature.
    
    Usage:
        @token_required()
        @requires_feature('daily_routine')
        def my_routine_endpoint():
            # Only Plus, Pro, or Admin users can access
            pass
    
    Args:
        feature_name (str): The feature key from FEATURE_ACCESS config
        
    Returns:
        403 if user doesn't have access to the feature
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = g.current_user
            tier = get_user_tier(user)
            
            # Check if feature exists
            if feature_name not in FEATURE_ACCESS:
                return jsonify({'error': f'Unknown feature: {feature_name}'}), 500
            
            feature = FEATURE_ACCESS[feature_name]
            allowed_tiers = feature.get('allowed_tiers', [])
            
            # Check if user's tier is allowed
            if tier not in allowed_tiers:
                return jsonify({
                    'error': 'Premium feature',
                    'message': f'{feature["name"]} requires a premium subscription',
                    'feature': feature_name,
                    'feature_name': feature['name'],
                    'current_tier': tier,
                    'required_tier': 'plus',
                    'upgrade_url': '/subscription'
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def check_feature_access(user, feature_name):
    """
    Utility function to check if a user has access to a feature.
    
    Args:
        user: User object
        feature_name (str): The feature key from FEATURE_ACCESS config
        
    Returns:
        dict: { allowed: bool, tier: str, feature: dict, message: str }
    """
    tier = get_user_tier(user)
    
    if feature_name not in FEATURE_ACCESS:
        return {
            'allowed': False,
            'tier': tier,
            'feature': None,
            'message': f'Unknown feature: {feature_name}'
        }
    
    feature = FEATURE_ACCESS[feature_name]
    allowed_tiers = feature.get('allowed_tiers', [])
    limit = feature.get('limits', {}).get(tier, 0)
    
    allowed = tier in allowed_tiers
    
    if allowed:
        message = f'You have access to {feature["name"]}'
        if limit > 0:
            message += f' (limit: {limit})'
        elif limit == -1:
            message += ' (unlimited)'
    else:
        message = f'{feature["name"]} requires Plus or Pro subscription'
    
    return {
        'allowed': allowed,
        'tier': tier,
        'feature': {
            'name': feature['name'],
            'description': feature['description'],
            'limit': limit
        },
        'message': message
    }


def get_feature_limit(user, feature_name):
    """
    Get the limit for a specific feature for a user.
    
    Args:
        user: User object
        feature_name (str): The feature key
        
    Returns:
        int: The limit (-1 for unlimited, 0 for no access)
    """
    tier = get_user_tier(user)
    
    if feature_name not in FEATURE_ACCESS:
        return 0
    
    return FEATURE_ACCESS[feature_name].get('limits', {}).get(tier, 0)


def require_route_available(auto_increment=True):
    """
    Decorator to check if user has available routes for today.
    Automatically increments usage count if auto_increment=True.
    
    Usage:
        @token_required()
        @require_route_available()
        def calculate_route():
            # Only users with remaining routes can access
            pass
        
        @token_required()
        @require_route_available(auto_increment=False)
        def preview_route():
            # Check limit but don't increment
            pass
    
    Returns:
        429 if daily route limit exceeded
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = g.current_user
            tier = get_user_tier(user)
            
            can_use, usage_info = RouteUsage.check_can_use_route(user.id, tier)
            
            if not can_use:
                return jsonify({
                    'error': 'Daily route limit reached',
                    'message': 'You have used all your routes for today.',
                    'limit': usage_info['limit'],
                    'used': usage_info['used'],
                    'tier': tier,
                    'upgrade_message': 'Upgrade to Plus for 50 routes/day or Pro for unlimited routes.',
                    'upgrade_url': '/subscription'
                }), 429
            
            # Auto-increment usage if enabled
            if auto_increment:
                usage = RouteUsage.get_today_usage(user.id)
                usage.increment()
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def check_route_limit(user):
    """
    Utility function to check if a user can use another route.
    Use this for manual checking in endpoints.
    
    Args:
        user: The user object
        
    Returns:
        tuple: (can_use: bool, usage_info: dict, tier: str)
    
    Example:
        can_use, usage_info, tier = check_route_limit(g.current_user)
        if not can_use:
            return {'error': 'Limit reached', **usage_info}, 429
    """
    tier = get_user_tier(user)
    can_use, usage_info = RouteUsage.check_can_use_route(user.id, tier)
    return can_use, usage_info, tier


def increment_route_usage(user):
    """
    Utility function to increment route usage for a user.
    
    Args:
        user: The user object
        
    Returns:
        int: The new usage count
    """
    usage = RouteUsage.get_today_usage(user.id)
    return usage.increment()


# =============================================================================
# USER ENDPOINTS
# =============================================================================

class SubscriptionStatus(Resource):
    """
    Get current user's subscription status.
    
    GET /api/subscription
    
    Returns subscription tier, status, and any pending requests.
    Admin users always see 'admin' tier.
    """
    
    @token_required()
    def get(self):
        user = g.current_user
        
        # Admin check - admins have full access
        if hasattr(user, 'role') and user.role == 'Admin':
            return {
                'tier': 'admin',
                'status': 'active',
                'message': 'Admin users have full access to all features',
                'is_admin': True
            }
        
        # Check for pending request
        pending_request = SubscriptionRequest.query.filter_by(
            _user_id=user.id,
            _status='pending'
        ).first()
        
        if pending_request:
            return {
                'tier': 'free',
                'status': 'pending',
                'requested_tier': pending_request.plan,
                'pending_amount': pending_request.amount,
                'request_date': pending_request.created_at.strftime('%Y-%m-%d') if pending_request.created_at else None,
                'message': 'Your payment is pending verification. This usually takes 24-48 hours.'
            }
        
        # Check for recent rejection
        recent_rejection = SubscriptionRequest.query.filter_by(
            _user_id=user.id,
            _status='rejected'
        ).order_by(SubscriptionRequest._created_at.desc()).first()
        
        # Get subscription
        subscription = Subscription.query.filter_by(_user_id=user.id).first()
        
        if not subscription or subscription.tier == 'free':
            result = {
                'tier': 'free',
                'status': 'active'
            }
            if recent_rejection and recent_rejection.created_at:
                # Only show rejection if within last 7 days
                if (datetime.utcnow() - recent_rejection.created_at).days < 7:
                    result['last_rejection'] = {
                        'reason': recent_rejection.rejection_reason,
                        'date': recent_rejection.created_at.strftime('%Y-%m-%d')
                    }
            return result
        
        # Check expiration
        if subscription.expires_at and subscription.expires_at < datetime.utcnow():
            return {
                'tier': 'free',
                'status': 'expired',
                'expired_tier': subscription.tier,
                'expired_at': subscription.expires_at.isoformat(),
                'message': 'Your subscription has expired. Please renew to continue enjoying premium features.'
            }
        
        return {
            **subscription.read(),
            'days_remaining': (subscription.expires_at - datetime.utcnow()).days if subscription.expires_at else None
        }


class SubscriptionPlans(Resource):
    """
    Get available subscription plans and pricing.
    
    GET /api/subscription/plans
    
    Returns all available plans with pricing info.
    """
    
    def get(self):
        return {
            'plans': {
                'free': {
                    'name': 'Free',
                    'price_monthly': 0,
                    'price_yearly': 0,
                    'features': [
                        'Basic route planning',
                        'View traffic data',
                        'Basic carpool matching'
                    ]
                },
                'plus': {
                    'name': 'Plus',
                    'price_monthly': PRICING['plus']['monthly'],
                    'price_yearly': PRICING['plus']['yearly'],
                    'yearly_savings': round(PRICING['plus']['monthly'] * 12 - PRICING['plus']['yearly'], 2),
                    'features': [
                        'Everything in Free',
                        'Save up to 10 favorite locations',
                        'Daily commute routines',
                        'Priority carpool matching',
                        'Ad-free experience'
                    ]
                },
                'pro': {
                    'name': 'Pro',
                    'price_monthly': PRICING['pro']['monthly'],
                    'price_yearly': PRICING['pro']['yearly'],
                    'yearly_savings': round(PRICING['pro']['monthly'] * 12 - PRICING['pro']['yearly'], 2),
                    'features': [
                        'Everything in Plus',
                        'Unlimited saved locations',
                        'AI traffic predictions',
                        'Advanced analytics',
                        'Priority support',
                        'Early access to new features'
                    ]
                }
            },
            'zelle_info': ZELLE_INFO
        }


class SubscriptionRequestEndpoint(Resource):
    """
    Submit a subscription request after Zelle payment.
    
    POST /api/subscription/request
    
    Body:
    {
        "plan": "plus" | "pro",
        "billing_interval": "monthly" | "yearly",
        "amount": 4.99,
        "zelle_name": "John Doe",  // Optional: name on Zelle account
        "email": "john@example.com"  // Optional: contact email
    }
    """
    
    @token_required()
    def post(self):
        user = g.current_user
        data = request.get_json()
        
        if not data:
            return {'error': 'Request body is required'}, 400
        
        plan = data.get('plan')
        billing_interval = data.get('billing_interval', 'monthly')
        zelle_name = data.get('zelle_name')
        email = data.get('email')
        amount = data.get('amount')
        
        # Validation
        if plan not in ['plus', 'pro']:
            return {'error': 'Invalid plan. Must be "plus" or "pro"'}, 400
        
        if billing_interval not in ['monthly', 'yearly']:
            return {'error': 'Invalid billing interval. Must be "monthly" or "yearly"'}, 400
        
        # Validate amount matches expected price
        expected_amount = PRICING[plan][billing_interval]
        if amount is None:
            amount = expected_amount
        elif abs(float(amount) - expected_amount) > 0.01:
            return {
                'error': 'Amount mismatch',
                'message': f'Expected ${expected_amount} for {plan} {billing_interval} plan',
                'expected': expected_amount
            }, 400
        
        # Check if user already has active subscription
        existing_sub = Subscription.query.filter_by(_user_id=user.id).first()
        if existing_sub and existing_sub.tier in ['plus', 'pro'] and existing_sub.status == 'active':
            if existing_sub.expires_at and existing_sub.expires_at > datetime.utcnow():
                return {
                    'error': 'Active subscription exists',
                    'message': 'You already have an active subscription. Please wait for it to expire or cancel first.',
                    'current_tier': existing_sub.tier,
                    'expires_at': existing_sub.expires_at.isoformat()
                }, 400
        
        # Check for existing pending request
        existing_request = SubscriptionRequest.query.filter_by(
            _user_id=user.id,
            _status='pending'
        ).first()
        
        if existing_request:
            return {
                'error': 'Pending request exists',
                'message': 'You already have a pending subscription request. Please wait for it to be processed.',
                'request_id': existing_request.id,
                'request_date': existing_request.created_at.strftime('%Y-%m-%d %H:%M') if existing_request.created_at else None
            }, 400
        
        # Create subscription request
        sub_request = SubscriptionRequest(
            user_id=user.id,
            plan=plan,
            billing_interval=billing_interval,
            amount=amount,
            zelle_name=zelle_name,
            email=email or user.email
        )
        sub_request.create()
        
        # Create payment history entry
        payment = PaymentHistory(
            user_id=user.id,
            amount=int(amount * 100),  # Convert to cents
            status='pending',
            description=f'{plan.capitalize()} Plan - {billing_interval.capitalize()}'
        )
        payment.request_id = sub_request.id
        payment.create()
        
        return {
            'success': True,
            'message': 'Request submitted successfully. Your payment will be verified within 24-48 hours.',
            'request_id': sub_request.id,
            'plan': plan,
            'amount': amount,
            'billing_interval': billing_interval
        }, 201


class CancelPendingRequest(Resource):
    """
    Cancel a pending subscription request.
    
    DELETE /api/subscription/request
    """
    
    @token_required()
    def delete(self):
        user = g.current_user
        
        pending_request = SubscriptionRequest.query.filter_by(
            _user_id=user.id,
            _status='pending'
        ).first()
        
        if not pending_request:
            return {'error': 'No pending request found'}, 404
        
        # Update request status
        pending_request.status = 'rejected'
        pending_request.rejection_reason = 'Cancelled by user'
        pending_request.update()
        
        # Update payment history
        payment = PaymentHistory.query.filter_by(_request_id=pending_request.id).first()
        if payment:
            payment.status = 'rejected'
            payment.update()
        
        return {
            'success': True,
            'message': 'Pending request cancelled'
        }


class CancelSubscription(Resource):
    """
    Cancel user's active subscription.
    
    POST /api/subscription/cancel
    
    Subscription remains active until expiration date.
    """
    
    @token_required()
    def post(self):
        user = g.current_user
        subscription = Subscription.query.filter_by(_user_id=user.id).first()
        
        if not subscription or subscription.tier == 'free':
            return {'error': 'No active subscription to cancel'}, 404
        
        if subscription.status == 'cancelled':
            return {'error': 'Subscription is already cancelled'}, 400
        
        subscription.status = 'cancelled'
        subscription.update()
        
        return {
            'success': True,
            'message': 'Subscription cancelled. You will retain access until your current billing period ends.',
            'access_until': subscription.expires_at.isoformat() if subscription.expires_at else None
        }


class PaymentHistoryEndpoint(Resource):
    """
    Get payment history for current user.
    
    GET /api/subscription/history
    """
    
    @token_required()
    def get(self):
        user = g.current_user
        
        payments = PaymentHistory.query.filter_by(_user_id=user.id).order_by(
            PaymentHistory._created_at.desc()
        ).limit(20).all()
        
        return {
            'payments': [p.read() for p in payments],
            'count': len(payments)
        }


class RouteUsageStatus(Resource):
    """
    Get current user's daily route usage status.
    
    GET /api/subscription/route-usage
    
    Returns:
        {
            "used": 3,
            "limit": 4,
            "remaining": 1,
            "unlimited": false,
            "tier": "free",
            "date": "2025-12-27"
        }
    
    Limits by tier:
        - free: 4 routes/day
        - plus: 50 routes/day
        - pro: unlimited (-1)
        - admin: unlimited (-1)
    """
    
    @token_required()
    def get(self):
        user = g.current_user
        tier = get_user_tier(user)
        
        can_use, usage_info = RouteUsage.check_can_use_route(user.id, tier)
        
        from datetime import date
        return {
            **usage_info,
            'tier': tier,
            'date': date.today().isoformat()
        }


class RouteUsageIncrement(Resource):
    """
    Increment route usage for current user.
    
    POST /api/subscription/route-usage/increment
    
    Success Response:
        {
            "success": true,
            "used": 4,
            "limit": 50,
            "remaining": 46
        }
    
    Limit Exceeded Response (429):
        {
            "error": "Daily route limit reached",
            "limit": 4,
            "used": 4,
            "tier": "free",
            "upgrade_message": "Upgrade to Plus for 50 routes/day or Pro for unlimited routes."
        }
    """
    
    @token_required()
    def post(self):
        user = g.current_user
        tier = get_user_tier(user)
        
        # Check if user can use another route
        can_use, usage_info = RouteUsage.check_can_use_route(user.id, tier)
        
        if not can_use:
            return {
                'error': 'Daily route limit reached',
                'limit': usage_info['limit'],
                'used': usage_info['used'],
                'tier': tier,
                'upgrade_message': 'Upgrade to Plus for 50 routes/day or Pro for unlimited routes.'
            }, 429
        
        # Increment usage
        usage = RouteUsage.get_today_usage(user.id)
        new_count = usage.increment()
        
        # Recalculate remaining
        limit = RouteUsage.get_limit_for_tier(tier)
        if limit == -1:
            remaining = -1
        else:
            remaining = max(0, limit - new_count)
        
        return {
            'success': True,
            'used': new_count,
            'limit': limit,
            'remaining': remaining,
            'unlimited': limit == -1
        }


class CheckFeatureAccess(Resource):
    """
    Check if current user has access to a specific premium feature.
    
    GET /api/subscription/check-feature/<feature_name>
    
    Features:
        - daily_routine: Save and manage daily commute routines
        - favorite_locations: Save frequently visited locations  
        - incident_report: Report and track traffic incidents
        - route_calculation: Calculate optimal routes
    
    Returns:
        {
            "allowed": true/false,
            "tier": "free" | "plus" | "pro" | "admin",
            "feature": {
                "name": "Daily Routine",
                "description": "Save and manage daily commute routines",
                "limit": -1  // -1 = unlimited, 0 = no access, >0 = limited
            },
            "message": "You have access to Daily Routine"
        }
    
    Feature Access Matrix:
        | Feature           | Free | Plus    | Pro | Admin |
        |-------------------|------|---------|-----|-------|
        | Route Calculation | 4/day| 50/day  | ∞   | ∞     |
        | Daily Routine     | ❌   | ✅      | ✅  | ✅    |
        | Favorite Locations| ❌   | ✅ (10) | ✅  | ✅    |
        | Incident Report   | ❌   | ✅      | ✅  | ✅    |
    """
    
    @token_required()
    def get(self, feature_name):
        user = g.current_user
        result = check_feature_access(user, feature_name)
        return result


class ListAllFeatures(Resource):
    """
    List all available features and user's access status.
    
    GET /api/subscription/features
    
    Returns all features with user's current access status.
    """
    
    @token_required()
    def get(self):
        user = g.current_user
        tier = get_user_tier(user)
        
        features = {}
        for feature_key, feature_config in FEATURE_ACCESS.items():
            allowed_tiers = feature_config.get('allowed_tiers', [])
            limit = feature_config.get('limits', {}).get(tier, 0)
            
            features[feature_key] = {
                'name': feature_config['name'],
                'description': feature_config['description'],
                'allowed': tier in allowed_tiers,
                'limit': limit,
                'unlimited': limit == -1
            }
        
        return {
            'tier': tier,
            'features': features
        }


# =============================================================================
# ADMIN ENDPOINTS
# =============================================================================

class AdminPendingRequests(Resource):
    """
    Get all pending subscription requests (Admin only).
    
    GET /api/admin/subscriptions/pending
    """
    
    @token_required()
    @require_admin()
    def get(self):
        requests = SubscriptionRequest.query.filter_by(_status='pending').order_by(
            SubscriptionRequest._created_at.desc()
        ).all()
        
        return {
            'requests': [r.read() for r in requests],
            'count': len(requests)
        }


class AdminAllRequests(Resource):
    """
    Get all subscription requests with optional filtering (Admin only).
    
    GET /api/admin/subscriptions/requests?status=pending&limit=50
    """
    
    @token_required()
    @require_admin()
    def get(self):
        status = request.args.get('status')  # 'pending', 'approved', 'rejected', or None for all
        limit = request.args.get('limit', 50, type=int)
        
        query = SubscriptionRequest.query
        
        if status:
            query = query.filter_by(_status=status)
        
        requests = query.order_by(
            SubscriptionRequest._created_at.desc()
        ).limit(limit).all()
        
        return {
            'requests': [r.read() for r in requests],
            'count': len(requests),
            'filter': status
        }


class AdminActiveSubscriptions(Resource):
    """
    Get all active paid subscriptions (Admin only).
    
    GET /api/admin/subscriptions/active
    """
    
    @token_required()
    @require_admin()
    def get(self):
        subscriptions = Subscription.query.filter(
            Subscription._tier.in_(['plus', 'pro']),
            Subscription._status == 'active'
        ).all()
        
        result = []
        for sub in subscriptions:
            user = User.query.get(sub.user_id)
            result.append({
                **sub.read(),
                'username': user.uid if user else 'Unknown',
                'name': user.name if user else 'Unknown',
                'email': user.email if user else None,
                'days_remaining': (sub.expires_at - datetime.utcnow()).days if sub.expires_at else None
            })
        
        return {
            'subscriptions': result,
            'count': len(result)
        }


class AdminAllUsers(Resource):
    """
    Get all users with subscription info (Admin only).
    
    GET /api/admin/users?limit=100
    """
    
    @token_required()
    @require_admin()
    def get(self):
        limit = request.args.get('limit', 100, type=int)
        users = User.query.limit(limit).all()
        
        result = []
        for user in users:
            subscription = Subscription.query.filter_by(_user_id=user.id).first()
            pending_request = SubscriptionRequest.query.filter_by(
                _user_id=user.id,
                _status='pending'
            ).first()
            
            result.append({
                'id': user.id,
                'username': user.uid,
                'name': user.name,
                'email': user.email,
                'role': user.role,
                'tier': subscription.tier if subscription else 'free',
                'subscription_status': subscription.status if subscription else 'none',
                'expires_at': subscription.expires_at.isoformat() if subscription and subscription.expires_at else None,
                'has_pending_request': pending_request is not None
            })
        
        return {
            'users': result,
            'count': len(result)
        }


class AdminApproveRequest(Resource):
    """
    Approve a subscription request (Admin only).
    
    POST /api/admin/subscriptions/approve
    
    Body:
    {
        "request_id": 123
    }
    """
    
    @token_required()
    @require_admin()
    def post(self):
        data = request.get_json()
        request_id = data.get('request_id')
        
        if not request_id:
            return {'error': 'request_id is required'}, 400
        
        sub_request = SubscriptionRequest.query.get(request_id)
        
        if not sub_request:
            return {'error': 'Request not found'}, 404
        
        if sub_request.status != 'pending':
            return {'error': f'Request is not pending (current status: {sub_request.status})'}, 400
        
        # Update request status
        sub_request.status = 'approved'
        sub_request.processed_by = g.current_user.id
        sub_request.processed_at = datetime.utcnow()
        sub_request.update()
        
        # Create or update subscription
        subscription = Subscription.query.filter_by(_user_id=sub_request.user_id).first()
        
        if not subscription:
            subscription = Subscription(
                user_id=sub_request.user_id,
                tier=sub_request.plan,
                status='active',
                billing_interval=sub_request.billing_interval
            )
            db.session.add(subscription)
        else:
            subscription.tier = sub_request.plan
            subscription.status = 'active'
            subscription.billing_interval = sub_request.billing_interval
        
        # Set expiration date
        if sub_request.billing_interval == 'yearly':
            subscription.expires_at = datetime.utcnow() + timedelta(days=365)
        else:
            subscription.expires_at = datetime.utcnow() + timedelta(days=30)
        
        db.session.commit()
        
        # Update payment history
        payment = PaymentHistory.query.filter_by(_request_id=request_id).first()
        if payment:
            payment.status = 'paid'
            payment.subscription_id = subscription.id
            db.session.commit()
        
        # Get user info for response
        user = User.query.get(sub_request.user_id)
        
        return {
            'success': True,
            'message': f'Approved {sub_request.plan} subscription for {user.uid if user else "user"}',
            'subscription': subscription.read()
        }


class AdminRejectRequest(Resource):
    """
    Reject a subscription request (Admin only).
    
    POST /api/admin/subscriptions/reject
    
    Body:
    {
        "request_id": 123,
        "reason": "Payment not found"  // Optional
    }
    """
    
    @token_required()
    @require_admin()
    def post(self):
        data = request.get_json()
        request_id = data.get('request_id')
        reason = data.get('reason', 'Payment could not be verified')
        
        if not request_id:
            return {'error': 'request_id is required'}, 400
        
        sub_request = SubscriptionRequest.query.get(request_id)
        
        if not sub_request:
            return {'error': 'Request not found'}, 404
        
        if sub_request.status != 'pending':
            return {'error': f'Request is not pending (current status: {sub_request.status})'}, 400
        
        # Update request status
        sub_request.status = 'rejected'
        sub_request.rejection_reason = reason
        sub_request.processed_by = g.current_user.id
        sub_request.processed_at = datetime.utcnow()
        sub_request.update()
        
        # Update payment history
        payment = PaymentHistory.query.filter_by(_request_id=request_id).first()
        if payment:
            payment.status = 'rejected'
            db.session.commit()
        
        # Get user info for response
        user = User.query.get(sub_request.user_id)
        
        return {
            'success': True,
            'message': f'Rejected subscription request from {user.uid if user else "user"}',
            'reason': reason
        }


class AdminSetSubscription(Resource):
    """
    Manually set a user's subscription (Admin only).
    Useful for granting free trials or fixing issues.
    
    POST /api/admin/subscriptions/set
    
    Body:
    {
        "user_id": 123,
        "tier": "plus" | "pro" | "free",
        "billing_interval": "monthly" | "yearly",  // Optional
        "days": 30  // Optional: number of days until expiration
    }
    """
    
    @token_required()
    @require_admin()
    def post(self):
        data = request.get_json()
        user_id = data.get('user_id')
        tier = data.get('tier', 'free')
        billing_interval = data.get('billing_interval', 'monthly')
        days = data.get('days', 30 if billing_interval == 'monthly' else 365)
        
        if not user_id:
            return {'error': 'user_id is required'}, 400
        
        if tier not in ['free', 'plus', 'pro']:
            return {'error': 'Invalid tier. Must be "free", "plus", or "pro"'}, 400
        
        # Verify user exists
        user = User.query.get(user_id)
        if not user:
            return {'error': 'User not found'}, 404
        
        # Get or create subscription
        subscription = Subscription.query.filter_by(_user_id=user_id).first()
        
        if not subscription:
            subscription = Subscription(
                user_id=user_id,
                tier=tier,
                status='active',
                billing_interval=billing_interval if tier != 'free' else None
            )
            db.session.add(subscription)
        else:
            subscription.tier = tier
            subscription.status = 'active'
            subscription.billing_interval = billing_interval if tier != 'free' else None
        
        # Set expiration for paid tiers
        if tier != 'free':
            subscription.expires_at = datetime.utcnow() + timedelta(days=days)
        else:
            subscription.expires_at = None
        
        db.session.commit()
        
        # Create payment history entry for admin action
        payment = PaymentHistory(
            user_id=user_id,
            amount=0,
            status='paid',
            description=f'Admin granted {tier} tier for {days} days'
        )
        payment.subscription_id = subscription.id
        payment.create()
        
        return {
            'success': True,
            'message': f'Subscription updated to {tier} for {user.uid}',
            'subscription': subscription.read()
        }


class AdminApproveRequestById(Resource):
    """
    Approve a subscription request by ID in URL (Admin only).
    
    PUT /api/admin/subscriptions/<id>/approve
    """
    
    @token_required()
    @require_admin()
    def put(self, id):
        sub_request = SubscriptionRequest.query.get(id)
        
        if not sub_request:
            return {'error': 'Request not found'}, 404
        
        if sub_request.status != 'pending':
            return {'error': f'Request is not pending (current status: {sub_request.status})'}, 400
        
        # Update request status
        sub_request.status = 'approved'
        sub_request.processed_by = g.current_user.id
        sub_request.processed_at = datetime.utcnow()
        sub_request.update()
        
        # Create or update subscription
        subscription = Subscription.query.filter_by(_user_id=sub_request.user_id).first()
        
        if not subscription:
            subscription = Subscription(
                user_id=sub_request.user_id,
                tier=sub_request.plan,
                status='active',
                billing_interval=sub_request.billing_interval
            )
            db.session.add(subscription)
        else:
            subscription.tier = sub_request.plan
            subscription.status = 'active'
            subscription.billing_interval = sub_request.billing_interval
        
        # Set expiration date
        if sub_request.billing_interval == 'yearly':
            subscription.expires_at = datetime.utcnow() + timedelta(days=365)
        else:
            subscription.expires_at = datetime.utcnow() + timedelta(days=30)
        
        db.session.commit()
        
        # Update payment history
        payment = PaymentHistory.query.filter_by(_request_id=id).first()
        if payment:
            payment.status = 'paid'
            payment.subscription_id = subscription.id
            db.session.commit()
        
        # Get user info for response
        user = User.query.get(sub_request.user_id)
        
        return {
            'success': True,
            'message': f'Approved {sub_request.plan} subscription for {user.uid if user else "user"}',
            'subscription': subscription.read()
        }


class AdminRejectRequestById(Resource):
    """
    Reject a subscription request by ID in URL (Admin only).
    
    PUT /api/admin/subscriptions/<id>/reject
    
    Body (optional):
    {
        "reason": "Payment not found"
    }
    """
    
    @token_required()
    @require_admin()
    def put(self, id):
        data = request.get_json() or {}
        reason = data.get('reason', 'Payment could not be verified')
        
        sub_request = SubscriptionRequest.query.get(id)
        
        if not sub_request:
            return {'error': 'Request not found'}, 404
        
        if sub_request.status != 'pending':
            return {'error': f'Request is not pending (current status: {sub_request.status})'}, 400
        
        # Update request status
        sub_request.status = 'rejected'
        sub_request.rejection_reason = reason
        sub_request.processed_by = g.current_user.id
        sub_request.processed_at = datetime.utcnow()
        sub_request.update()
        
        # Update payment history
        payment = PaymentHistory.query.filter_by(_request_id=id).first()
        if payment:
            payment.status = 'rejected'
            db.session.commit()
        
        # Get user info for response
        user = User.query.get(sub_request.user_id)
        
        return {
            'success': True,
            'message': f'Rejected subscription request from {user.uid if user else "user"}',
            'reason': reason
        }


class AdminSetUserTierById(Resource):
    """
    Manually set a user's subscription tier by user ID in URL (Admin only).
    
    PUT /api/admin/users/<id>/set-tier
    
    Body:
    {
        "tier": "free" | "plus" | "pro",
        "billing_interval": "monthly" | "yearly"  // Optional, defaults to monthly
    }
    """
    
    @token_required()
    @require_admin()
    def put(self, id):
        data = request.get_json() or {}
        tier = data.get('tier', 'free')
        billing_interval = data.get('billing_interval', 'monthly')
        days = 30 if billing_interval == 'monthly' else 365
        
        if tier not in ['free', 'plus', 'pro']:
            return {'error': 'Invalid tier. Must be "free", "plus", or "pro"'}, 400
        
        # Verify user exists
        user = User.query.get(id)
        if not user:
            return {'error': 'User not found'}, 404
        
        # Get or create subscription
        subscription = Subscription.query.filter_by(_user_id=id).first()
        
        if not subscription:
            subscription = Subscription(
                user_id=id,
                tier=tier,
                status='active',
                billing_interval=billing_interval if tier != 'free' else None
            )
            db.session.add(subscription)
        else:
            subscription.tier = tier
            subscription.status = 'active'
            subscription.billing_interval = billing_interval if tier != 'free' else None
        
        # Set expiration for paid tiers
        if tier != 'free':
            subscription.expires_at = datetime.utcnow() + timedelta(days=days)
        else:
            subscription.expires_at = None
        
        db.session.commit()
        
        # Create payment history entry for admin action
        payment = PaymentHistory(
            user_id=id,
            amount=0,
            status='paid',
            description=f'Admin set tier to {tier} for {days} days'
        )
        payment.subscription_id = subscription.id
        payment.create()
        
        return {
            'success': True,
            'message': f'Subscription updated to {tier} for {user.uid}',
            'subscription': subscription.read(),
            'user': {
                'id': user.id,
                'username': user.uid,
                'name': user.name,
                'email': user.email
            }
        }


class AdminSubscriptionStats(Resource):
    """
    Get subscription statistics (Admin only).
    
    GET /api/admin/subscriptions/stats
    """
    
    @token_required()
    @require_admin()
    def get(self):
        # Count by tier
        free_count = Subscription.query.filter_by(_tier='free').count()
        plus_count = Subscription.query.filter(
            Subscription._tier == 'plus',
            Subscription._status == 'active'
        ).count()
        pro_count = Subscription.query.filter(
            Subscription._tier == 'pro',
            Subscription._status == 'active'
        ).count()
        
        # Users without subscription record (default free)
        total_users = User.query.count()
        users_with_sub = Subscription.query.count()
        implicit_free = total_users - users_with_sub
        
        # Pending requests
        pending_count = SubscriptionRequest.query.filter_by(_status='pending').count()
        
        # Revenue calculation (from approved payments)
        total_revenue = db.session.query(
            db.func.sum(PaymentHistory._amount)
        ).filter(
            PaymentHistory._status == 'paid'
        ).scalar() or 0
        
        return {
            'tiers': {
                'free': free_count + implicit_free,
                'plus': plus_count,
                'pro': pro_count
            },
            'total_users': total_users,
            'pending_requests': pending_count,
            'total_revenue_cents': total_revenue,
            'total_revenue_dollars': total_revenue / 100
        }


# =============================================================================
# REGISTER ROUTES
# =============================================================================

# User endpoints
api.add_resource(SubscriptionStatus, '/subscription')
api.add_resource(SubscriptionPlans, '/subscription/plans')
api.add_resource(SubscriptionRequestEndpoint, '/subscription/request')
api.add_resource(CancelPendingRequest, '/subscription/request')  # DELETE method
api.add_resource(CancelSubscription, '/subscription/cancel')
api.add_resource(PaymentHistoryEndpoint, '/subscription/history')
api.add_resource(RouteUsageStatus, '/subscription/route-usage')
api.add_resource(RouteUsageIncrement, '/subscription/route-usage/increment')
api.add_resource(CheckFeatureAccess, '/subscription/check-feature/<string:feature_name>')
api.add_resource(ListAllFeatures, '/subscription/features')

# Admin endpoints
api.add_resource(AdminPendingRequests, '/admin/subscriptions/pending')
api.add_resource(AdminAllRequests, '/admin/subscriptions/requests')
api.add_resource(AdminActiveSubscriptions, '/admin/subscriptions/active')
api.add_resource(AdminAllUsers, '/admin/users')
api.add_resource(AdminApproveRequest, '/admin/subscriptions/approve')
api.add_resource(AdminRejectRequest, '/admin/subscriptions/reject')
api.add_resource(AdminSetSubscription, '/admin/subscriptions/set')
api.add_resource(AdminSubscriptionStats, '/admin/subscriptions/stats')

# Admin endpoints with ID in URL (RESTful style)
api.add_resource(AdminApproveRequestById, '/admin/subscriptions/<int:id>/approve')
api.add_resource(AdminRejectRequestById, '/admin/subscriptions/<int:id>/reject')
api.add_resource(AdminSetUserTierById, '/admin/users/<int:id>/set-tier')
