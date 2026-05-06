"""
DTIP — Google OAuth Route
Fixed issues:
  1. State is always enforced (no bypass).
  2. Token is delivered via HttpOnly cookie ONLY — never in URL params.
  3. Frontend reads auth state from /api/auth/me, not from URL token.
  4. Session lifetime extended to survive the OAuth round-trip.
  5. Proper error logging and user-friendly redirects.
"""
import re
import secrets
import logging
from datetime import datetime
from urllib.parse import urlencode

import requests as http_req
from flask import (Blueprint, request, redirect, session,
                   current_app, make_response)

from extensions import db
from models import User, Wallet
from utils.security import make_token, set_auth_cookie
from utils.helpers import gen_code

logger   = logging.getLogger(__name__)
oauth_bp = Blueprint('oauth', __name__)

GOOGLE_AUTH_URL      = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL     = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL  = 'https://www.googleapis.com/oauth2/v3/userinfo'

# ─────────────────────────────────────────
# STEP 1 — Redirect to Google
# ─────────────────────────────────────────

@oauth_bp.route('/auth/google')
def google_login():
    if not current_app.config.get('GOOGLE_CLIENT_ID'):
        return redirect('/?error=google_not_configured')

    state = secrets.token_urlsafe(32)   # 256 bits of entropy
    session['oauth_state'] = state
    session.permanent = True            # keep session alive for the round-trip

    # Persist pending referral code so it survives the redirect
    ref = request.args.get('ref', '')
    if ref:
        session['pending_ref'] = ref

    params = dict(
        client_id=current_app.config['GOOGLE_CLIENT_ID'],
        redirect_uri=current_app.config['GOOGLE_REDIRECT_URI'],
        response_type='code',
        scope='openid email profile',
        state=state,
        access_type='offline',
        prompt='select_account',
    )
    logger.info(f'Google OAuth initiated, state={state[:8]}…')
    return redirect(f'{GOOGLE_AUTH_URL}?{urlencode(params)}')


# ─────────────────────────────────────────
# STEP 2 — Callback from Google
# ─────────────────────────────────────────

@oauth_bp.route('/auth/google/callback')
def google_callback():
    # ── Error from Google ──────────────────────────────────────────────
    if request.args.get('error'):
        logger.warning(f'Google OAuth error: {request.args.get("error")}')
        return redirect('/?error=oauth_cancelled')

    incoming_state = request.args.get('state', '')
    saved_state    = session.pop('oauth_state', None)

    # ── State validation — ALWAYS enforced ────────────────────────────
    if not saved_state:
        logger.warning('OAuth callback: no state in session (possible session loss)')
        return redirect('/?error=session_expired&hint=enable_cookies')

    if not secrets.compare_digest(incoming_state, saved_state):
        logger.warning(
            f'OAuth state mismatch: got={incoming_state[:8]}… '
            f'expected={saved_state[:8]}…'
        )
        return redirect('/?error=oauth_state_mismatch')

    # ── Exchange code for tokens ───────────────────────────────────────
    code = request.args.get('code', '')
    if not code:
        return redirect('/?error=missing_code')

    try:
        token_resp = http_req.post(
            GOOGLE_TOKEN_URL,
            data=dict(
                code=code,
                client_id=current_app.config['GOOGLE_CLIENT_ID'],
                client_secret=current_app.config['GOOGLE_CLIENT_SECRET'],
                redirect_uri=current_app.config['GOOGLE_REDIRECT_URI'],
                grant_type='authorization_code',
            ),
            timeout=10,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as exc:
        logger.error(f'Google token exchange failed: {exc}')
        return redirect('/?error=token_exchange_failed')

    access_token = token_data.get('access_token')
    if not access_token:
        logger.error(f'No access_token in Google response: {token_data}')
        return redirect('/?error=token_exchange_failed')

    # ── Fetch user profile ─────────────────────────────────────────────
    try:
        ui = http_req.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        ).json()
    except Exception as exc:
        logger.error(f'Google userinfo fetch failed: {exc}')
        return redirect('/?error=profile_fetch_failed')

    g_id  = ui.get('sub', '')
    email = ui.get('email', '')
    name  = ui.get('name', '')
    pic   = ui.get('picture', '')

    if not g_id or not email:
        return redirect('/?error=missing_profile')

    # ── Upsert user ────────────────────────────────────────────────────
    try:
        user = (
            User.query.filter_by(google_id=g_id).first()
            or User.query.filter_by(email=email).first()
        )

        if not user:
            # New user — generate safe username
            base  = re.sub(r'[^a-z0-9_]', '', name.lower().replace(' ', '_'))[:20] or 'user'
            uname = base
            while User.query.filter_by(username=uname).first():
                uname = base[:16] + secrets.token_hex(2)

            ref_code   = session.pop('pending_ref', None)
            referred_by = None
            if ref_code:
                ref_user = User.query.filter_by(referral_code=ref_code).first()
                if ref_user:
                    referred_by = ref_user.id

            user = User(
                email=email, username=uname, google_id=g_id,
                avatar_url=pic, referral_code=gen_code(),
                referred_by=referred_by,
                is_verified=True,          # Google email already verified
            )
            db.session.add(user)
            db.session.flush()
            db.session.add(Wallet(user_id=user.id))
            logger.info(f'New user via Google OAuth: {email}')
        else:
            if not user.google_id:
                user.google_id = g_id
            if pic:
                user.avatar_url = pic
            user.last_login = datetime.utcnow()
            # Mark verified since Google vouches for the email
            if not user.is_verified:
                user.is_verified = True

        db.session.commit()

    except Exception as exc:
        db.session.rollback()
        logger.exception(f'DB error during Google OAuth upsert: {exc}')
        return redirect('/?error=database_error')

    # ── Issue JWT — delivered via HttpOnly cookie ONLY ────────────────
    # Never put the token in the URL — it would appear in server logs,
    # browser history, and Referer headers.
    # The frontend calls /api/auth/me on page load to detect the session.
    token = make_token(user.id, user.role)
    resp  = make_response(redirect('/?login=google'))
    set_auth_cookie(resp, token)
    return resp
