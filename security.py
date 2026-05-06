"""
DTIP — Security Utilities
JWT helpers, password policy, file validation, input sanitization.
"""
import os
import re
import secrets
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import request, jsonify, current_app
import bleach

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# PASSWORD POLICY
# ─────────────────────────────────────────

_PW_MIN = 8
_PW_RE  = re.compile(
    r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^a-zA-Z\d]).{8,}$'
)

def validate_password(pw: str) -> str | None:
    """Return error string or None if valid."""
    if len(pw) < _PW_MIN:
        return f'Password must be at least {_PW_MIN} characters'
    if not re.search(r'[A-Z]', pw):
        return 'Password must contain an uppercase letter'
    if not re.search(r'[a-z]', pw):
        return 'Password must contain a lowercase letter'
    if not re.search(r'\d', pw):
        return 'Password must contain a digit'
    if not re.search(r'[^a-zA-Z\d]', pw):
        return 'Password must contain a special character'
    return None


# ─────────────────────────────────────────
# JWT
# ─────────────────────────────────────────

def make_token(uid: int, role: str) -> str:
    now = datetime.utcnow()
    payload = {
        'sub': uid,
        'role': role,
        'iat': now,
        'exp': now + timedelta(hours=current_app.config['JWT_EXPIRY_HOURS']),
        'jti': secrets.token_hex(8),   # unique ID to allow future revocation
    }
    return jwt.encode(payload, current_app.config['JWT_SECRET'], algorithm='HS256')


def decode_token(token: str) -> dict:
    return jwt.decode(token, current_app.config['JWT_SECRET'], algorithms=['HS256'])


def get_current_user():
    """
    Resolve the authenticated User from:
      1. Authorization: Bearer <token> header
      2. Secure HttpOnly cookie  (set after Google OAuth)
    Never read token from URL params — prevents token leakage in logs.
    """
    from models import User

    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '').strip()
    if not token:
        token = request.cookies.get('dtip_auth', '')
    if not token:
        return None
    try:
        data = decode_token(token)
        user = User.query.get(data['sub'])
        if user and (user.is_suspended or not user.is_active):
            return None
        return user
    except jwt.ExpiredSignatureError:
        logger.debug('JWT expired')
        return None
    except Exception:
        return None


# ─────────────────────────────────────────
# AUTH DECORATORS
# ─────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def dec(*a, **kw):
        u = get_current_user()
        if not u or not u.is_active or u.is_suspended:
            return jsonify(error='Unauthorized'), 401
        return f(u, *a, **kw)
    return dec


def require_admin(f):
    @wraps(f)
    def dec(*a, **kw):
        u = get_current_user()
        if not u or u.role != 'admin':
            return jsonify(error='Admin only'), 403
        return f(u, *a, **kw)
    return dec


def require_moderator(f):
    @wraps(f)
    def dec(*a, **kw):
        u = get_current_user()
        if not u or u.role not in ('admin', 'moderator'):
            return jsonify(error='Moderator access required'), 403
        return f(u, *a, **kw)
    return dec


# ─────────────────────────────────────────
# INPUT SANITIZATION
# ─────────────────────────────────────────

_ALLOWED_TAGS: list[str] = []   # strip all HTML from user-facing text fields

def sanitize(text: str, max_len: int = 2000) -> str:
    """Strip HTML/JS, collapse whitespace, enforce length."""
    if not text:
        return ''
    clean = bleach.clean(str(text), tags=_ALLOWED_TAGS, strip=True)
    return clean.strip()[:max_len]


# ─────────────────────────────────────────
# FILE UPLOAD SECURITY
# ─────────────────────────────────────────

_PDF_MAGIC = b'%PDF'
_MAX_BYTES = 16 * 1024 * 1024   # 16 MB


def validate_pdf(file_storage) -> str | None:
    """
    Return error string or None.
    Checks:  MIME magic bytes, size, extension (belt-and-suspenders).
    """
    if not file_storage or not file_storage.filename:
        return 'No file provided'
    if not file_storage.filename.lower().endswith('.pdf'):
        return 'File must have a .pdf extension'
    # Read first 4 bytes for magic check
    header = file_storage.stream.read(4)
    file_storage.stream.seek(0)
    if header != _PDF_MAGIC:
        return 'File is not a valid PDF (bad magic bytes)'
    # Size check
    file_storage.stream.seek(0, 2)   # seek to end
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > _MAX_BYTES:
        return f'File too large (max {_MAX_BYTES // (1024*1024)} MB)'
    return None


def safe_filename(original: str) -> str:
    """
    Return a UUID-based filename that is safe to store on disk.
    Preserves .pdf extension only.
    Prevents path traversal completely.
    """
    import uuid
    return f'{uuid.uuid4().hex}.pdf'


def hash_file(file_storage) -> str:
    """Return SHA-256 hex digest of file contents (for duplicate detection)."""
    h = hashlib.sha256()
    file_storage.stream.seek(0)
    while True:
        chunk = file_storage.stream.read(8192)
        if not chunk:
            break
        h.update(chunk)
    file_storage.stream.seek(0)
    return h.hexdigest()


# ─────────────────────────────────────────
# WEBHOOK SIGNATURE VERIFICATION
# ─────────────────────────────────────────

def verify_intasend_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 comparison."""
    expected = hmac.new(
        secret.encode(),
        raw_body,
        'sha256'
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ─────────────────────────────────────────
# COOKIE HELPER
# ─────────────────────────────────────────

def set_auth_cookie(response, token: str):
    cfg = current_app.config
    response.set_cookie(
        'dtip_auth',
        token,
        httponly=cfg.get('AUTH_COOKIE_HTTPONLY', True),
        secure=cfg.get('AUTH_COOKIE_SECURE', False),
        samesite=cfg.get('AUTH_COOKIE_SAMESITE', 'Lax'),
        max_age=cfg['JWT_EXPIRY_HOURS'] * 3600,
        path='/',
    )
