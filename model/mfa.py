"""
TOTP multi-factor auth — additive and opt-in.

Stored in its own table so the existing ``users`` table schema is untouched
(no migration, nothing breaks). A user has MFA only if they explicitly enroll;
until then login behaves exactly as before.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from __init__ import app, db


class UserMFA(db.Model):
    __tablename__ = 'user_mfa'

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    _totp_secret = db.Column(db.String(64), nullable=True)
    _enabled = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=datetime.utcnow)
    _confirmed_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, user_id, totp_secret=None):
        self._user_id = user_id
        self._totp_secret = totp_secret
        self._enabled = False

    @property
    def enabled(self):
        return bool(self._enabled)

    @property
    def secret(self):
        return self._totp_secret

    def _commit(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def set_secret(self, secret):
        """Store a new (unconfirmed) secret; MFA stays disabled until verified."""
        self._totp_secret = secret
        self._enabled = False
        return self._commit()

    def enable(self):
        self._enabled = True
        self._confirmed_at = datetime.utcnow()
        return self._commit()

    def disable(self):
        self._enabled = False
        self._totp_secret = None
        return self._commit()

    @staticmethod
    def for_user(user_id):
        return UserMFA.query.filter_by(_user_id=user_id).first()


def initUserMFA():
    """Create the table. Safe to call repeatedly; mirrors other model init funcs."""
    with app.app_context():
        db.create_all()
