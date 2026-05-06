"""
DTIP — Auth Routes
/api/auth/*
"""
import re
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, make_response

from extensions import db, limiter
from models import User, Wallet
from utils.security import (make_token, require_auth, validate_password,
                             set_auth_cookie, sanitize)
from utils.helpers import gen_code

logger  = logging.getLogger(__name__)
auth_bp = Blueprint('auth', __name__)

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@auth_bp.route('/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    d        = request.get_json() or {}
    email    = (d.get('email') or '').strip().lower()
    username = (d.get('username') or '').strip()
    password = d.get('password', '')
    ref_code = (d.get('ref_code') or '').strip().upper()

    if not email or not username or not password:
        return jsonify(error='email, username and password are required'), 400
    if not _EMAIL_RE.match(email):
        return jsonify(error='Invalid email address'), 400
    if not re.match(r'^[a-zA-Z0-9_]{3,30}$', username):
        return jsonify(error='Username: 3-30 chars, letters/numbers/underscore only'), 400

    pw_err = validate_password(password)
    if pw_err:
        return jsonify(error=pw_err), 400

    if User.query.filter_by(email=email).first():
        return jsonify(error='Email already registered'), 409
    if User.query.filter_by(username=username).first():
        return jsonify(error='Username already taken'), 409

    referred_by = None
    if ref_code:
        ref_user = User.query.filter_by(referral_code=ref_code).first()
        if ref_user:
            referred_by = ref_user.id

    user = User(
        email=email,
        username=username,
        referral_code=gen_code(),
        referred_by=referred_by,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    db.session.add(Wallet(user_id=user.id))
    db.session.commit()

    token = make_token(user.id, user.role)
    return jsonify(token=token, user=user.to_dict()), 201


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("20 per hour")
def login():
    d     = request.get_json() or {}
    email = (d.get('email') or '').strip().lower()
    pw    = d.get('password', '')

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(pw):
        return jsonify(error='Invalid credentials'), 401
    if not user.is_active:
        return jsonify(error='Account inactive'), 403
    if user.is_suspended:
        return jsonify(
            error=f'Account suspended: {user.suspension_reason or "Contact support"}'
        ), 403

    user.last_login = datetime.utcnow()
    db.session.commit()

    token = make_token(user.id, user.role)
    resp  = make_response(jsonify(token=token, user=user.to_dict()))
    set_auth_cookie(resp, token)
    return resp


@auth_bp.route('/me')
@require_auth
def me(user):
    return jsonify(
        user=user.to_dict(),
        wallet=user.wallet.to_dict() if user.wallet else None,
    )


@auth_bp.route('/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify(ok=True))
    resp.delete_cookie('dtip_auth', path='/')
    return resp


@auth_bp.route('/refresh', methods=['POST'])
@require_auth
def refresh(user):
    token = make_token(user.id, user.role)
    resp  = make_response(jsonify(token=token, user=user.to_dict()))
    set_auth_cookie(resp, token)
    return resp
