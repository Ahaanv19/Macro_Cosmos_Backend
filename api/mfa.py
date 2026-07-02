"""
MFA / 2FA endpoints (TOTP) — additive, opt-in.

Flow:
  POST /api/mfa/setup   -> returns a new secret + otpauth URL (show as QR)
  POST /api/mfa/verify  -> confirm a 6-digit code to enable MFA
  POST /api/mfa/disable -> turn MFA off (requires a valid code)
  GET  /api/mfa/status  -> { enabled, pending }

Login enforcement lives in ``verify_user_otp`` which the /api/authenticate
endpoint calls. Users without MFA are entirely unaffected.
"""

import os
import secrets

import pyotp
from flask import Blueprint, request, jsonify, g

from api.jwt_authorize import token_required
from model.mfa import UserMFA
from model.backup_codes import UserBackupCode

mfa_api = Blueprint('mfa_api', __name__, url_prefix='/api/mfa')

ISSUER = 'Macro Cosmos'


def mfa_required_for_user(user):
    """
    Policy: does this account REQUIRE 2FA? Controlled by the MFA_ENFORCED env var:
      - "admins" (default): required for Admin accounts (admin hardening / #6)
      - "all":              required for every account
      - "off":              not required (opt-in only)

    Enforcement is SOFT — login still succeeds so the user can reach the setup
    page; the response just flags that setup is required. This avoids locking out
    accounts (incl. the existing admin) that haven't enrolled yet.
    """
    scope = (os.environ.get('MFA_ENFORCED', 'all') or '').strip().lower()
    if scope == 'all':
        return True
    if scope == 'admins':
        return getattr(user, 'role', None) == 'Admin'
    return False


def user_has_second_factor(user_id):
    """
    True if the account already has a strong second factor set up — either TOTP
    OR a registered passkey. A passkey (device + biometric) is itself 2FA, so it
    satisfies the requirement without the authenticator app.
    """
    rec = UserMFA.for_user(user_id)
    if rec and rec.enabled:
        return True
    try:
        from model.passkey import UserPasskey
        return len(UserPasskey.for_user(user_id)) > 0
    except Exception:
        return False


def verify_user_otp(user_id, code):
    """
    Return (mfa_enabled, otp_ok).

    - If the user has NOT enabled MFA, returns (False, True) so login proceeds
      exactly as before.
    - If enabled, otp_ok reflects whether the supplied TOTP code is valid.
    """
    rec = UserMFA.for_user(user_id)
    if not rec or not rec.enabled or not rec.secret:
        return (False, True)
    if not code:
        return (True, False)
    try:
        return (True, pyotp.TOTP(rec.secret).verify(str(code).strip(), valid_window=1))
    except Exception:
        return (True, False)


@mfa_api.route('/status', methods=['GET'])
@token_required()
def mfa_status():
    rec = UserMFA.for_user(g.current_user.id)
    # "required" = policy requires 2FA AND no second factor (TOTP or passkey) yet.
    still_needs = mfa_required_for_user(g.current_user) and not user_has_second_factor(g.current_user.id)
    return jsonify({
        'enabled': bool(rec and rec.enabled),
        'pending': bool(rec and rec.secret and not rec.enabled),
        'required': still_needs,
    })


@mfa_api.route('/setup', methods=['POST'])
@token_required()
def mfa_setup():
    """Generate a fresh TOTP secret for the current user (not yet enabled)."""
    user = g.current_user
    rec = UserMFA.for_user(user.id) or UserMFA(user_id=user.id)
    secret = pyotp.random_base32()
    if rec.set_secret(secret) is None:
        return jsonify({'error': 'Could not start MFA setup'}), 500
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=str(user.uid), issuer_name=ISSUER)
    return jsonify({'secret': secret, 'otpauth_url': uri})


@mfa_api.route('/verify', methods=['POST'])
@token_required()
def mfa_verify():
    """Confirm a code to enable MFA."""
    user = g.current_user
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    rec = UserMFA.for_user(user.id)
    if not rec or not rec.secret:
        return jsonify({'error': 'No MFA setup in progress'}), 400
    if not pyotp.TOTP(rec.secret).verify(code, valid_window=1):
        return jsonify({'error': 'Invalid code'}), 400
    rec.enable()
    return jsonify({'message': 'Two-factor authentication enabled', 'enabled': True})


# ----------------------------------------------------------------------------
# Recovery: one-time backup codes + admin reset (safety net for hard 2FA)
# ----------------------------------------------------------------------------

@mfa_api.route('/backup-codes', methods=['POST'])
@token_required()
def generate_backup_codes():
    """Generate a fresh set of 10 one-time recovery codes (shown ONCE)."""
    user = g.current_user
    raw = [secrets.token_hex(4).upper() for _ in range(10)]  # 8 hex chars each
    UserBackupCode.replace_all(user.id, raw)
    formatted = [c[:4] + '-' + c[4:] for c in raw]  # display as XXXX-XXXX
    return jsonify({'codes': formatted, 'count': len(formatted)})


@mfa_api.route('/backup-codes', methods=['GET'])
@token_required()
def backup_codes_status():
    return jsonify({'remaining': UserBackupCode.unused_count(g.current_user.id)})


@mfa_api.route('/admin/reset', methods=['POST'])
@token_required(roles=["Admin"])
def admin_reset_2fa():
    """
    Admin: clear ALL second factors for a user (passkeys, TOTP, backup codes) so
    a locked-out user can sign in with their password and re-enroll.
    """
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip()
    from model.user import User
    from model.passkey import UserPasskey
    target = User.query.filter_by(_uid=uid).first()
    if not target:
        return jsonify({'error': 'User not found'}), 404
    for p in UserPasskey.for_user(target.id):
        p.delete()
    rec = UserMFA.for_user(target.id)
    if rec:
        rec.disable()
    UserBackupCode.clear_for_user(target.id)
    return jsonify({'message': f"2FA reset for '{uid}'. They can now sign in with their password and set up 2FA again."})


@mfa_api.route('/disable', methods=['POST'])
@token_required()
def mfa_disable():
    """Disable MFA (requires a current valid code to prevent lockout abuse)."""
    user = g.current_user
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    rec = UserMFA.for_user(user.id)
    if not rec or not rec.enabled:
        return jsonify({'message': 'Two-factor authentication is not enabled', 'enabled': False})
    if not pyotp.TOTP(rec.secret).verify(code, valid_window=1):
        return jsonify({'error': 'Invalid code'}), 400
    rec.disable()
    return jsonify({'message': 'Two-factor authentication disabled', 'enabled': False})
