"""
DTIP — Configuration
Fails loudly if required environment variables are missing.
"""
import os
import sys
from datetime import timedelta


def _require(key: str) -> str:
    """Fetch an env var or abort with a clear error message."""
    val = os.environ.get(key)
    if not val:
        print(f"\n[DTIP] FATAL: Required environment variable '{key}' is missing.\n"
              f"       Copy .env.example to .env and fill in all values.\n", file=sys.stderr)
        sys.exit(1)
    return val


class BaseConfig:
    # ── Security ──────────────────────────────────────────────────────────
    SECRET_KEY = _require('SECRET_KEY')
    JWT_SECRET = _require('JWT_SECRET')
    JWT_EXPIRY_HOURS = int(os.environ.get('JWT_EXPIRY_HOURS', 24))

    # ── Database ──────────────────────────────────────────────────────────
    _db_url = _require('DATABASE_URL').replace('postgres://', 'postgresql://')
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_size': 10,
        'max_overflow': 20,
    }

    # ── Google OAuth ──────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID = _require('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = _require('GOOGLE_CLIENT_SECRET')
    GOOGLE_REDIRECT_URI = _require('GOOGLE_REDIRECT_URI')

    # ── IntaSend (live only) ──────────────────────────────────────────────
    INTASEND_API_KEY = _require('INTASEND_API_KEY')
    INTASEND_SECRET = _require('INTASEND_SECRET')
    INTASEND_BASE = 'https://payment.intasend.com'   # always live

    # ── Admin ─────────────────────────────────────────────────────────────
    ADMIN_EMAIL = _require('ADMIN_EMAIL')
    ADMIN_PASSWORD = _require('ADMIN_PASSWORD')

    # ── Platform defaults ─────────────────────────────────────────────────
    DEFAULT_ACTIVATION_FEE = float(os.environ.get('ACTIVATION_FEE', '299.0'))
    DEFAULT_REFERRAL_BONUS = float(os.environ.get('REFERRAL_BONUS', '100.0'))
    DEFAULT_PREMIUM_FEE = float(os.environ.get('PREMIUM_FEE', '499.0'))
    DEFAULT_WITHDRAWAL_FEE_PCT = float(os.environ.get('WITHDRAWAL_FEE_PCT', '5.0'))
    FREE_DAILY_LIMIT = int(os.environ.get('FREE_DAILY_LIMIT', '3'))
    PREMIUM_DAILY_LIMIT = int(os.environ.get('PREMIUM_DAILY_LIMIT', '10'))
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')

    # ── File uploads ──────────────────────────────────────────────────────
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024   # 16 MB hard limit

    # ── Cache ─────────────────────────────────────────────────────────────
    CACHE_TYPE = 'SimpleCache'
    CACHE_DEFAULT_TIMEOUT = 300

    # ── Session / Cookie ──────────────────────────────────────────────────
    SESSION_COOKIE_NAME = 'dtip_session'
    SESSION_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=10)

    # Subclasses override these
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = 'Lax'
    AUTH_COOKIE_SECURE = False
    AUTH_COOKIE_SAMESITE = 'Lax'


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = 'Lax'
    AUTH_COOKIE_SECURE = False
    AUTH_COOKIE_SAMESITE = 'Lax'


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
    AUTH_COOKIE_SECURE = True
    AUTH_COOKIE_SAMESITE = 'None'


def get_config():
    env = os.environ.get('FLASK_ENV', 'production').lower()
    return DevelopmentConfig if env == 'development' else ProductionConfig
