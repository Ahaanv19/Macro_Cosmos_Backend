"""
One-time 2FA backup / recovery codes — additive safety net for hard 2FA.

If a user loses their passkey device they can sign in with their password + a
backup code (each code works once). Codes are stored HASHED (never plaintext),
like passwords. Own table, so the existing schema is untouched.
"""

import re
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

from __init__ import app, db


def _canon(code):
    """Normalize a code: strip non-alphanumerics, uppercase (so 'abcd-efgh' == 'ABCDEFGH')."""
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


class UserBackupCode(db.Model):
    __tablename__ = 'user_backup_codes'

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column(db.Integer, nullable=False, index=True)
    _code_hash = db.Column(db.String(255), nullable=False)
    _used = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=datetime.utcnow)
    _used_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, user_id, code_hash):
        self._user_id = user_id
        self._code_hash = code_hash
        self._used = False

    @staticmethod
    def unused_count(user_id):
        return UserBackupCode.query.filter_by(_user_id=user_id, _used=False).count()

    @staticmethod
    def replace_all(user_id, plaintext_codes):
        """Delete any existing codes and store fresh hashed ones."""
        UserBackupCode.query.filter_by(_user_id=user_id).delete()
        for code in plaintext_codes:
            db.session.add(UserBackupCode(user_id=user_id, code_hash=generate_password_hash(_canon(code))))
        db.session.commit()

    @staticmethod
    def verify_and_consume(user_id, code):
        """Return True and mark the code used if it matches an unused code."""
        canon = _canon(code)
        if not canon:
            return False
        for row in UserBackupCode.query.filter_by(_user_id=user_id, _used=False).all():
            if check_password_hash(row._code_hash, canon):
                row._used = True
                row._used_at = datetime.utcnow()
                db.session.commit()
                return True
        return False

    @staticmethod
    def clear_for_user(user_id):
        UserBackupCode.query.filter_by(_user_id=user_id).delete()
        db.session.commit()


def initUserBackupCodes():
    """Create the table. Safe to call repeatedly."""
    with app.app_context():
        db.create_all()
