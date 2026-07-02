"""
WebAuthn passkey credentials — additive, opt-in passwordless / biometric login
(Face ID / Touch ID / Windows Hello / security keys).

Stored in its own table so the existing ``users`` table is untouched (no
migration, nothing breaks). Credential id and public key are kept as base64url
strings to keep the schema simple and portable across SQLite/MySQL.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from __init__ import app, db


class UserPasskey(db.Model):
    __tablename__ = 'user_passkeys'

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column(db.Integer, nullable=False, index=True)
    _credential_id = db.Column(db.String(512), nullable=False, unique=True, index=True)
    _public_key = db.Column(db.Text, nullable=False)
    _sign_count = db.Column(db.Integer, default=0, nullable=False)
    _transports = db.Column(db.String(255), nullable=True)
    _name = db.Column(db.String(120), nullable=True)
    _created_at = db.Column(db.DateTime, default=datetime.utcnow)
    _last_used_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, user_id, credential_id, public_key, sign_count=0, transports=None, name=None):
        self._user_id = user_id
        self._credential_id = credential_id
        self._public_key = public_key
        self._sign_count = sign_count or 0
        self._transports = transports
        self._name = name or 'Passkey'

    # --- accessors ---
    @property
    def user_id(self):
        return self._user_id

    @property
    def credential_id(self):
        return self._credential_id

    @property
    def public_key(self):
        return self._public_key

    @property
    def sign_count(self):
        return self._sign_count

    def read(self):
        return {
            'id': self.id,
            'name': self._name,
            'created_at': self._created_at.isoformat() if self._created_at else None,
            'last_used_at': self._last_used_at.isoformat() if self._last_used_at else None,
        }

    # --- persistence ---
    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def update_sign_count(self, new_count):
        self._sign_count = int(new_count)
        self._last_used_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return self

    def delete(self):
        try:
            db.session.delete(self)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return None

    # --- queries ---
    @staticmethod
    def for_user(user_id):
        return UserPasskey.query.filter_by(_user_id=user_id).all()

    @staticmethod
    def by_credential_id(credential_id):
        return UserPasskey.query.filter_by(_credential_id=credential_id).first()


def initUserPasskeys():
    """Create the table. Safe to call repeatedly; mirrors other model init funcs."""
    with app.app_context():
        db.create_all()
