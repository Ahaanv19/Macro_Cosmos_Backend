"""
WebAuthn passkeys — passwordless / biometric login (Face ID, Touch ID, Windows
Hello, security keys). Additive and opt-in: nothing here affects existing
password/MFA login.

Config (env, per environment — passkeys are domain-bound):
  WEBAUTHN_RP_ID    relying-party id = the FRONTEND host (e.g. ahaanv19.github.io)
  WEBAUTHN_ORIGIN   expected origin(s), comma-separated (e.g. https://ahaanv19.github.io)
  WEBAUTHN_RP_NAME  display name (default "Macro Cosmos")
Defaults target local dev (localhost / http://localhost:4887).

Endpoints:
  POST /api/webauthn/register/begin     (auth)  -> creation options
  POST /api/webauthn/register/complete  (auth)  -> store the new passkey
  POST /api/webauthn/login/begin                -> request options for a uid
  POST /api/webauthn/login/complete             -> verify + issue JWT cookie
  GET  /api/webauthn/credentials        (auth)  -> list my passkeys
  DEL  /api/webauthn/credentials/<id>   (auth)  -> remove a passkey
"""

import os
import json
import time

import jwt
from flask import Blueprint, request, jsonify, g, session, current_app

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
)

from api.jwt_authorize import token_required
from model.user import User
from model.passkey import UserPasskey

webauthn_api = Blueprint('webauthn_api', __name__, url_prefix='/api/webauthn')


def _rp_id():
    return os.environ.get('WEBAUTHN_RP_ID', 'localhost')


def _rp_name():
    return os.environ.get('WEBAUTHN_RP_NAME', 'Macro Cosmos')


def _expected_origin():
    raw = os.environ.get('WEBAUTHN_ORIGIN', 'http://localhost:4887')
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    if len(parts) > 1:
        return parts
    return parts[0] if parts else 'http://localhost:4887'


def _sign_challenge(chal_b64, purpose, uid=None):
    """Sign the WebAuthn challenge into a short-lived stateless token.

    The challenge is already sent to (and visible to) the client as part of the
    options, so signing it back leaks nothing. This removes the dependency on a
    cross-site session cookie for the challenge — cookies are dropped SameSite
    cross-origin and blocked entirely by Safari/iOS, which broke passkeys on the
    deployed (github.io -> backend) setup. HS256 signature + short expiry make it
    tamper-proof and single-window.
    """
    payload = {"chal": chal_b64, "purpose": purpose, "exp": int(time.time()) + 300}
    if uid is not None:
        payload["uid"] = uid
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def _read_challenge_token(token, purpose, uid=None):
    """Verify a challenge token and return the challenge (or None if invalid)."""
    if not token:
        return None
    try:
        data = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except Exception:
        return None
    if data.get("purpose") != purpose:
        return None
    if uid is not None and data.get("uid") != uid:
        return None
    return data.get("chal")


def _issue_jwt_cookie(user, message):
    """Mirror /api/authenticate's token + cookie so passkey login is a real login."""
    token = jwt.encode({"_uid": user._uid}, current_app.config["SECRET_KEY"], algorithm="HS256")
    resp = jsonify({"message": message, "uid": user._uid})
    resp.set_cookie(
        current_app.config["JWT_TOKEN_NAME"], token,
        max_age=3600, secure=True, httponly=True, path='/', samesite='None',
    )
    return resp


@webauthn_api.route('/register/begin', methods=['POST'])
@token_required()
def register_begin():
    user = g.current_user
    existing = UserPasskey.for_user(user.id)
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id)) for p in existing]
    opts = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=str(user.id).encode('utf-8'),
        user_name=str(user.uid),
        user_display_name=str(user.name or user.uid),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    chal_b64 = bytes_to_base64url(opts.challenge)
    session['wa_reg_chal'] = chal_b64  # kept as same-origin fallback
    options_json = json.loads(options_to_json(opts))
    # Stateless challenge token so registration survives cross-origin (no cookie).
    options_json['challengeToken'] = _sign_challenge(chal_b64, 'reg', user.id)
    return jsonify(options_json)


@webauthn_api.route('/register/complete', methods=['POST'])
@token_required()
def register_complete():
    user = g.current_user
    body = request.get_json(silent=True) or {}
    # Prefer the stateless token (works cross-origin); fall back to session cookie.
    chal = _read_challenge_token(body.get('challenge_token'), 'reg', user.id)
    if not chal:
        chal = session.pop('wa_reg_chal', None)
    if not chal:
        return jsonify({'error': 'No passkey registration in progress'}), 400
    credential = body.get('credential') or body
    name = (body.get('name') or '').strip() or 'Passkey'
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            require_user_verification=False,
        )
    except Exception as e:
        return jsonify({'error': f'Passkey verification failed: {e}'}), 400

    pk = UserPasskey(
        user_id=user.id,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        name=name[:120],
    )
    if not pk.create():
        return jsonify({'error': 'This passkey is already registered'}), 400
    return jsonify({'message': 'Passkey registered', 'passkey': pk.read()})


@webauthn_api.route('/login/begin', methods=['POST'])
def login_begin():
    body = request.get_json(silent=True) or {}
    uid = (body.get('uid') or '').strip()
    user = User.query.filter_by(_uid=uid).first() if uid else None
    creds = UserPasskey.for_user(user.id) if user else []
    if not user or not creds:
        return jsonify({'error': 'No passkeys found for this account'}), 404
    allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in creds]
    opts = generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    chal_b64 = bytes_to_base64url(opts.challenge)
    session['wa_auth_chal'] = chal_b64  # kept as same-origin fallback
    options_json = json.loads(options_to_json(opts))
    # Stateless challenge token so login survives cross-origin (no cookie).
    options_json['challengeToken'] = _sign_challenge(chal_b64, 'auth')
    return jsonify(options_json)


@webauthn_api.route('/login/complete', methods=['POST'])
def login_complete():
    body = request.get_json(silent=True) or {}
    credential = body.get('credential') or body
    # Prefer the stateless token (works cross-origin); fall back to session cookie.
    chal = _read_challenge_token(body.get('challenge_token'), 'auth')
    if not chal:
        chal = session.pop('wa_auth_chal', None)
    if not chal:
        return jsonify({'error': 'No passkey login in progress'}), 400
    raw_id = credential.get('rawId') or credential.get('id')
    if not raw_id:
        return jsonify({'error': 'Invalid passkey response'}), 400
    pk = UserPasskey.by_credential_id(raw_id)
    if not pk:
        return jsonify({'error': 'Unknown passkey'}), 404
    user = User.query.get(pk.user_id)
    if not user:
        return jsonify({'error': 'Account not found'}), 404
    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            credential_public_key=base64url_to_bytes(pk.public_key),
            credential_current_sign_count=pk.sign_count,
            require_user_verification=False,
        )
    except Exception as e:
        return jsonify({'error': f'Passkey verification failed: {e}'}), 401

    pk.update_sign_count(verification.new_sign_count)
    return _issue_jwt_cookie(user, f'Passkey login for {user._uid} successful')


@webauthn_api.route('/credentials', methods=['GET'])
@token_required()
def list_credentials():
    return jsonify([p.read() for p in UserPasskey.for_user(g.current_user.id)])


@webauthn_api.route('/credentials/<int:passkey_id>', methods=['DELETE'])
@token_required()
def delete_credential(passkey_id):
    pk = UserPasskey.query.get(passkey_id)
    if not pk or pk.user_id != g.current_user.id:
        return jsonify({'error': 'Passkey not found'}), 404
    pk.delete()
    return jsonify({'message': 'Passkey removed'})
