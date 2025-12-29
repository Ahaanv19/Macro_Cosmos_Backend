"""
Subscription models for Zelle-based payment system.
Integrates with EXISTING User model and database.

Subscription Tiers:
- free: Default tier for all users
- plus: $4.99/month or $47.88/year
- pro: $9.99/month or $95.88/year

Admin users automatically have full access to all features.
"""
from datetime import datetime
from __init__ import db


class Subscription(db.Model):
    """
    Subscription model - links to existing User model.
    Tracks subscription tier and status.
    """
    __tablename__ = 'subscriptions'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Foreign key to existing users table
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    
    # Subscription info
    _tier = db.Column('tier', db.String(20), default='free')  # 'free', 'plus', 'pro'
    _status = db.Column('status', db.String(20), default='active')  # 'active', 'pending', 'cancelled', 'expired'
    _billing_interval = db.Column('billing_interval', db.String(20))  # 'monthly', 'yearly'
    
    # Timestamps
    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)
    _updated_at = db.Column('updated_at', db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    _expires_at = db.Column('expires_at', db.DateTime)  # When subscription expires
    
    # Stripe fields
    _stripe_customer_id = db.Column('stripe_customer_id', db.String(100))
    _stripe_subscription_id = db.Column('stripe_subscription_id', db.String(100))
    
    def __init__(self, user_id, tier='free', status='active', billing_interval=None):
        """
        Constructor for Subscription.
        
        Args:
            user_id (int): The ID of the user (foreign key to users table)
            tier (str): Subscription tier - 'free', 'plus', or 'pro'
            status (str): Status - 'active', 'pending', 'cancelled', 'expired'
            billing_interval (str): 'monthly' or 'yearly'
        """
        self._user_id = user_id
        self._tier = tier
        self._status = status
        self._billing_interval = billing_interval
    
    # Properties
    @property
    def user_id(self):
        return self._user_id
    
    @property
    def tier(self):
        return self._tier
    
    @tier.setter
    def tier(self, value):
        if value in ['free', 'plus', 'pro']:
            self._tier = value
    
    @property
    def status(self):
        return self._status
    
    @status.setter
    def status(self, value):
        if value in ['active', 'pending', 'cancelled', 'expired']:
            self._status = value
    
    @property
    def billing_interval(self):
        return self._billing_interval
    
    @billing_interval.setter
    def billing_interval(self, value):
        if value in ['monthly', 'yearly', None]:
            self._billing_interval = value
    
    @property
    def expires_at(self):
        return self._expires_at
    
    @expires_at.setter
    def expires_at(self, value):
        self._expires_at = value
    
    @property
    def stripe_customer_id(self):
        return self._stripe_customer_id
    
    @stripe_customer_id.setter
    def stripe_customer_id(self, value):
        self._stripe_customer_id = value
    
    @property
    def stripe_subscription_id(self):
        return self._stripe_subscription_id
    
    @stripe_subscription_id.setter
    def stripe_subscription_id(self, value):
        self._stripe_subscription_id = value
    
    @property
    def created_at(self):
        return self._created_at
    
    @property
    def updated_at(self):
        return self._updated_at
    
    def is_active(self):
        """Check if subscription is currently active and not expired."""
        if self._status != 'active':
            return False
        if self._expires_at and self._expires_at < datetime.utcnow():
            return False
        return True
    
    def read(self):
        """Return subscription data as dictionary."""
        return {
            'id': self.id,
            'user_id': self._user_id,
            'tier': self._tier,
            'status': self._status,
            'billing_interval': self._billing_interval,
            'expires_at': self._expires_at.isoformat() if self._expires_at else None,
            'created_at': self._created_at.isoformat() if self._created_at else None,
            'updated_at': self._updated_at.isoformat() if self._updated_at else None,
            'stripe_customer_id': self._stripe_customer_id,
            'stripe_subscription_id': self._stripe_subscription_id
        }
    
    def create(self):
        """Add subscription to database."""
        db.session.add(self)
        db.session.commit()
        return self
    
    def update(self):
        """Commit changes to database."""
        self._updated_at = datetime.utcnow()
        db.session.commit()
        return self
    
    def delete(self):
        """Remove subscription from database."""
        db.session.delete(self)
        db.session.commit()


class SubscriptionRequest(db.Model):
    """
    Tracks pending subscription requests awaiting admin approval.
    Users submit these after sending Zelle payment.
    """
    __tablename__ = 'subscription_requests'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Foreign key to existing users table
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Request details
    _plan = db.Column('plan', db.String(20), nullable=False)  # 'plus', 'pro'
    _billing_interval = db.Column('billing_interval', db.String(20), default='monthly')  # 'monthly', 'yearly'
    _amount = db.Column('amount', db.Float, nullable=False)  # Payment amount
    
    # Zelle payment info (for admin verification)
    _zelle_name = db.Column('zelle_name', db.String(100))  # Name on Zelle account
    _email = db.Column('email', db.String(100))  # User's email for contact
    
    # Status: 'pending', 'approved', 'rejected'
    _status = db.Column('status', db.String(20), default='pending')
    _rejection_reason = db.Column('rejection_reason', db.String(500))
    
    # Admin who processed the request
    _processed_by = db.Column('processed_by', db.Integer, db.ForeignKey('users.id'))
    _processed_at = db.Column('processed_at', db.DateTime)
    
    # Timestamps
    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)
    
    def __init__(self, user_id, plan, billing_interval, amount, zelle_name=None, email=None):
        """
        Constructor for SubscriptionRequest.
        
        Args:
            user_id (int): The ID of the user making the request
            plan (str): The plan being requested - 'plus' or 'pro'
            billing_interval (str): 'monthly' or 'yearly'
            amount (float): The payment amount
            zelle_name (str): Name on the Zelle account
            email (str): User's email for contact
        """
        self._user_id = user_id
        self._plan = plan
        self._billing_interval = billing_interval
        self._amount = amount
        self._zelle_name = zelle_name
        self._email = email
        self._status = 'pending'
    
    # Properties
    @property
    def user_id(self):
        return self._user_id
    
    @property
    def plan(self):
        return self._plan
    
    @property
    def billing_interval(self):
        return self._billing_interval
    
    @property
    def amount(self):
        return self._amount
    
    @property
    def zelle_name(self):
        return self._zelle_name
    
    @property
    def email(self):
        return self._email
    
    @property
    def status(self):
        return self._status
    
    @status.setter
    def status(self, value):
        if value in ['pending', 'approved', 'rejected']:
            self._status = value
    
    @property
    def rejection_reason(self):
        return self._rejection_reason
    
    @rejection_reason.setter
    def rejection_reason(self, value):
        self._rejection_reason = value
    
    @property
    def processed_by(self):
        return self._processed_by
    
    @processed_by.setter
    def processed_by(self, value):
        self._processed_by = value
    
    @property
    def processed_at(self):
        return self._processed_at
    
    @processed_at.setter
    def processed_at(self, value):
        self._processed_at = value
    
    @property
    def created_at(self):
        return self._created_at
    
    def read(self):
        """Return request data as dictionary."""
        from model.user import User
        user = User.query.get(self._user_id)
        processor = User.query.get(self._processed_by) if self._processed_by else None
        
        return {
            'id': self.id,
            'user_id': self._user_id,
            'username': user.uid if user else 'Unknown',
            'name': user.name if user else 'Unknown',
            'email': self._email,
            'plan': self._plan,
            'billing_interval': self._billing_interval,
            'amount': self._amount,
            'zelle_name': self._zelle_name,
            'status': self._status,
            'rejection_reason': self._rejection_reason,
            'processed_by': processor.uid if processor else None,
            'processed_at': self._processed_at.strftime('%Y-%m-%d %H:%M') if self._processed_at else None,
            'created_at': self._created_at.strftime('%Y-%m-%d %H:%M') if self._created_at else None
        }
    
    def create(self):
        """Add request to database."""
        db.session.add(self)
        db.session.commit()
        return self
    
    def update(self):
        """Commit changes to database."""
        db.session.commit()
        return self


class PaymentHistory(db.Model):
    """
    Payment history - tracks all subscription payments.
    Provides audit trail for both users and admins.
    """
    __tablename__ = 'payment_history'
    
    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False)
    _subscription_id = db.Column('subscription_id', db.Integer, db.ForeignKey('subscriptions.id'))
    _request_id = db.Column('request_id', db.Integer, db.ForeignKey('subscription_requests.id'))
    
    _amount = db.Column('amount', db.Integer)  # Amount in cents
    _status = db.Column('status', db.String(20))  # 'paid', 'pending', 'rejected'
    _description = db.Column('description', db.String(200))
    _payment_method = db.Column('payment_method', db.String(50), default='zelle')
    
    # Stripe fields
    _stripe_payment_intent_id = db.Column('stripe_payment_intent_id', db.String(100))
    _stripe_invoice_id = db.Column('stripe_invoice_id', db.String(100))
    
    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)
    
    def __init__(self, user_id, amount, status, description, payment_method='zelle'):
        """
        Constructor for PaymentHistory.
        
        Args:
            user_id (int): The ID of the user
            amount (int): Amount in cents
            status (str): 'paid', 'pending', 'rejected'
            description (str): Description of the payment
            payment_method (str): Payment method (default: 'zelle')
        """
        self._user_id = user_id
        self._amount = amount
        self._status = status
        self._description = description
        self._payment_method = payment_method
    
    # Properties
    @property
    def user_id(self):
        return self._user_id
    
    @property
    def subscription_id(self):
        return self._subscription_id
    
    @subscription_id.setter
    def subscription_id(self, value):
        self._subscription_id = value
    
    @property
    def request_id(self):
        return self._request_id
    
    @request_id.setter
    def request_id(self, value):
        self._request_id = value
    
    @property
    def amount(self):
        return self._amount
    
    @property
    def status(self):
        return self._status
    
    @status.setter
    def status(self, value):
        if value in ['paid', 'pending', 'rejected']:
            self._status = value
    
    @property
    def description(self):
        return self._description
    
    @property
    def payment_method(self):
        return self._payment_method
    
    @property
    def stripe_payment_intent_id(self):
        return self._stripe_payment_intent_id
    
    @stripe_payment_intent_id.setter
    def stripe_payment_intent_id(self, value):
        self._stripe_payment_intent_id = value
    
    @property
    def stripe_invoice_id(self):
        return self._stripe_invoice_id
    
    @stripe_invoice_id.setter
    def stripe_invoice_id(self, value):
        self._stripe_invoice_id = value
    
    @property
    def created_at(self):
        return self._created_at
    
    def read(self):
        """Return payment history data as dictionary."""
        return {
            'id': self.id,
            'user_id': self._user_id,
            'subscription_id': self._subscription_id,
            'request_id': self._request_id,
            'date': self._created_at.strftime('%Y-%m-%d') if self._created_at else None,
            'amount': self._amount,
            'amount_dollars': self._amount / 100 if self._amount else 0,
            'status': self._status,
            'description': self._description,
            'payment_method': self._payment_method,
            'stripe_payment_intent_id': self._stripe_payment_intent_id,
            'stripe_invoice_id': self._stripe_invoice_id
        }
    
    def create(self):
        """Add payment to database."""
        db.session.add(self)
        db.session.commit()
        return self
    
    def update(self):
        """Commit changes to database."""
        db.session.commit()
        return self


class RouteUsage(db.Model):
    """
    Tracks daily route usage per user for rate limiting.
    
    Tier Limits:
    - free: 4 routes per day
    - plus: 50 routes per day
    - pro: unlimited
    - admin: unlimited
    """
    __tablename__ = 'route_usage'
    
    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False)
    _date = db.Column('date', db.Date, nullable=False)  # Date only, no time
    _count = db.Column('count', db.Integer, default=0)
    
    # Unique constraint: one record per user per day
    __table_args__ = (
        db.UniqueConstraint('user_id', 'date', name='unique_user_date'),
    )
    
    # Route limits by tier
    ROUTE_LIMITS = {
        'free': 4,
        'plus': 50,
        'pro': -1,  # unlimited
        'admin': -1  # unlimited
    }
    
    def __init__(self, user_id, date=None, count=0):
        """
        Constructor for RouteUsage.
        
        Args:
            user_id (int): The ID of the user
            date (date): The date for this usage record (defaults to today)
            count (int): Initial count (defaults to 0)
        """
        from datetime import date as date_type
        self._user_id = user_id
        self._date = date if date else date_type.today()
        self._count = count
    
    @property
    def user_id(self):
        return self._user_id
    
    @property
    def date(self):
        return self._date
    
    @property
    def count(self):
        return self._count
    
    @count.setter
    def count(self, value):
        self._count = value
    
    def increment(self):
        """Increment the usage count by 1."""
        self._count += 1
        db.session.commit()
        return self._count
    
    def read(self):
        """Return usage data as dictionary."""
        return {
            'id': self.id,
            'user_id': self._user_id,
            'date': self._date.isoformat() if self._date else None,
            'count': self._count
        }
    
    def create(self):
        """Add usage record to database."""
        db.session.add(self)
        db.session.commit()
        return self
    
    def update(self):
        """Commit changes to database."""
        db.session.commit()
        return self
    
    @classmethod
    def get_today_usage(cls, user_id):
        """
        Get today's usage record for a user, creating one if it doesn't exist.
        
        Args:
            user_id (int): The user's ID
            
        Returns:
            RouteUsage: The usage record for today
        """
        from datetime import date
        today = date.today()
        
        usage = cls.query.filter_by(_user_id=user_id, _date=today).first()
        
        if not usage:
            usage = cls(user_id=user_id, date=today, count=0)
            usage.create()
        
        return usage
    
    @classmethod
    def get_limit_for_tier(cls, tier):
        """
        Get the route limit for a subscription tier.
        
        Args:
            tier (str): The subscription tier
            
        Returns:
            int: The daily limit (-1 for unlimited)
        """
        return cls.ROUTE_LIMITS.get(tier, cls.ROUTE_LIMITS['free'])
    
    @classmethod
    def check_can_use_route(cls, user_id, tier):
        """
        Check if a user can use another route today.
        
        Args:
            user_id (int): The user's ID
            tier (str): The user's subscription tier
            
        Returns:
            tuple: (can_use: bool, usage_info: dict)
        """
        limit = cls.get_limit_for_tier(tier)
        usage = cls.get_today_usage(user_id)
        
        # Unlimited
        if limit == -1:
            return True, {
                'used': usage.count,
                'limit': -1,
                'remaining': -1,
                'unlimited': True
            }
        
        remaining = max(0, limit - usage.count)
        can_use = usage.count < limit
        
        return can_use, {
            'used': usage.count,
            'limit': limit,
            'remaining': remaining,
            'unlimited': False
        }


def initSubscriptions():
    """
    Initialize subscription tables.
    Call this after db.create_all() in your main.py.
    """
    db.create_all()
    print("Subscription tables initialized")

