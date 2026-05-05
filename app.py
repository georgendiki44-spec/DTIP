"""
DTIP v3 — Full-featured Digital Tasks & Earning Platform
Features: PDF uploads, AI detection, IntaSend webhooks, moderators, share graphs,
          live settings sync, alerts, premium suspension, advanced admin controls
Run: python app.py | gunicorn -w 1 --threads 4 app:app
"""

import os, re, uuid, secrets, logging, time, base64, json
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import urlencode

from flask import Flask, request, jsonify, redirect, session, render_template_string, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, disconnect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
import jwt, bcrypt
import requests as http_req

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
app = Flask(__name__)
# ── Stable secret key: persisted to disk if not in env so tokens survive restarts ──
_secret_key = os.environ.get('SECRET_KEY', '')
_secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
if not _secret_key:
    if os.path.exists(_secret_file):
        try:
            _secret_key = open(_secret_file).read().strip()
        except Exception:
            _secret_key = ''
    if not _secret_key:
        _secret_key = secrets.token_hex(32)
        try:
            with open(_secret_file, 'w') as _sf:
                _sf.write(_secret_key)
        except Exception:
            pass  # can't write — that's ok, will regenerate next restart
    import logging as _log
    _log.getLogger('dtip').warning(
        'SECRET_KEY not set in env — using persisted key from .secret_key file. '
        'Set SECRET_KEY env var in production!'
    )

app.config.update(
    SECRET_KEY=_secret_key,
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL','sqlite:///dtip.db').replace('postgres://','postgresql://'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    # FIX: robust DB pool so connections are recycled and pre-pinged
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_size': int(os.environ.get('DB_POOL_SIZE', 5)),
        'max_overflow': int(os.environ.get('DB_MAX_OVERFLOW', 10)),
        'connect_args': {'connect_timeout': 10} if 'postgresql' in os.environ.get('DATABASE_URL','') else {},
    },
    JWT_SECRET=os.environ.get('JWT_SECRET', _secret_key),
    JWT_EXPIRY_HOURS=int(os.environ.get('JWT_EXPIRY_HOURS',24)),
    GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID',''),
    GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET',''),
    GOOGLE_REDIRECT_URI=os.environ.get('GOOGLE_REDIRECT_URI','http://localhost:5000/auth/google/callback'),
    INTASEND_API_KEY=os.environ.get('INTASEND_API_KEY','demo'),
    INTASEND_SECRET=os.environ.get('INTASEND_SECRET',''),
    INTASEND_ENV=os.environ.get('INTASEND_ENV','sandbox'),  # sandbox or live
    DEMO_MODE=os.environ.get('DEMO_MODE','true').lower()=='true',
    CACHE_TYPE='SimpleCache',
    CACHE_DEFAULT_TIMEOUT=300,
    ADMIN_EMAIL=os.environ.get('ADMIN_EMAIL','admin@dtip.co.ke'),
    ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD','Admin@DTIP2024!'),
    DEFAULT_ACTIVATION_FEE=float(os.environ.get('ACTIVATION_FEE','299.0')),
    DEFAULT_REFERRAL_BONUS=float(os.environ.get('REFERRAL_BONUS','100.0')),
    DEFAULT_PREMIUM_FEE=float(os.environ.get('PREMIUM_FEE','499.0')),
    DEFAULT_WITHDRAWAL_FEE_PCT=float(os.environ.get('WITHDRAWAL_FEE_PCT','5.0')),
    FREE_DAILY_LIMIT=int(os.environ.get('FREE_DAILY_LIMIT','3')),
    PREMIUM_DAILY_LIMIT=int(os.environ.get('PREMIUM_DAILY_LIMIT','10')),
    BASE_URL=os.environ.get('BASE_URL','http://localhost:5000'),
    UPLOAD_FOLDER=os.environ.get('UPLOAD_FOLDER','uploads'),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB max upload
    # FIX: session cookie settings so OAuth state persists across the Google redirect
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',      # 'Lax' allows the cookie on the redirect-back from Google
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE','false').lower()=='true',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=10),  # short window for OAuth state
)

# IntaSend URLs
INTASEND_BASE = 'https://sandbox.intasend.com' if app.config['INTASEND_ENV'] == 'sandbox' else 'https://payment.intasend.com'

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                    logger=False, engineio_logger=False)
cache = Cache(app)
limiter = Limiter(key_func=get_remote_address, app=app,
                  default_limits=["500 per day","100 per hour"],
                  storage_uri="memory://")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
SUSPICIOUS_KEYWORDS = ['porn','xxx','drugs','weapon','hack','phishing','scam','casino','bet','gambling']

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class PlatformSettings(db.Model):
    __tablename__ = 'platform_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.String(1000), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get(key, default=None):
        s = PlatformSettings.query.filter_by(key=key).first()
        return s.value if s else default

    @staticmethod
    def set(key, value):
        s = PlatformSettings.query.filter_by(key=key).first()
        if s:
            s.value = str(value)
            s.updated_at = datetime.utcnow()
        else:
            db.session.add(PlatformSettings(key=key, value=str(value)))

class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username      = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    google_id     = db.Column(db.String(120), unique=True, nullable=True)
    avatar_url    = db.Column(db.String(500), nullable=True)
    role          = db.Column(db.String(20), default='member')  # admin|moderator|member
    tier          = db.Column(db.String(20), default='free')
    is_active     = db.Column(db.Boolean, default=True)
    is_suspended  = db.Column(db.Boolean, default=False)
    suspension_reason = db.Column(db.String(500), nullable=True)
    is_verified   = db.Column(db.Boolean, default=False)
    is_activated  = db.Column(db.Boolean, default=False)
    activation_paid_at = db.Column(db.DateTime, nullable=True)
    referral_code = db.Column(db.String(20), unique=True, nullable=True)
    referred_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    tasks_done_today = db.Column(db.Integer, default=0)
    last_task_date   = db.Column(db.Date, nullable=True)
    premium_expires  = db.Column(db.DateTime, nullable=True)
    premium_suspended = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_login    = db.Column(db.DateTime, nullable=True)
    wallet        = db.relationship('Wallet', backref='user', uselist=False, lazy='joined')

    def set_password(self, pw):
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    def check_password(self, pw):
        if not self.password_hash: return False
        return bcrypt.checkpw(pw.encode(), self.password_hash.encode())

    @property
    def referral_link(self):
        base = PlatformSettings.get('base_url', app.config['BASE_URL'])
        return f"{base}/?ref={self.referral_code}"

    @property
    def is_premium_active(self):
        if self.premium_suspended: return False
        if self.tier == 'premium':
            if self.premium_expires and self.premium_expires > datetime.utcnow():
                return True
            elif not self.premium_expires:
                return True
        return False

    def get_daily_tasks_done(self):
        today = date.today()
        if self.last_task_date != today:
            return 0
        return self.tasks_done_today or 0

    def increment_task_count(self):
        today = date.today()
        if self.last_task_date != today:
            self.tasks_done_today = 1
            self.last_task_date = today
        else:
            self.tasks_done_today = (self.tasks_done_today or 0) + 1

    def daily_limit(self):
        if self.is_premium_active:
            return int(PlatformSettings.get('premium_daily_limit', app.config['PREMIUM_DAILY_LIMIT']))
        return int(PlatformSettings.get('free_daily_limit', app.config['FREE_DAILY_LIMIT']))

    def to_dict(self):
        return dict(id=self.id, email=self.email, username=self.username,
                    role=self.role, tier=self.tier, is_active=self.is_active,
                    is_suspended=self.is_suspended, suspension_reason=self.suspension_reason,
                    is_verified=self.is_verified, is_activated=self.is_activated,
                    avatar_url=self.avatar_url, referral_code=self.referral_code,
                    referral_link=self.referral_link,
                    is_premium=self.is_premium_active,
                    premium_suspended=self.premium_suspended,
                    premium_expires=self.premium_expires.isoformat() if self.premium_expires else None,
                    daily_limit=self.daily_limit(),
                    tasks_done_today=self.get_daily_tasks_done(),
                    created_at=self.created_at.isoformat(),
                    last_login=self.last_login.isoformat() if self.last_login else None)

class Wallet(db.Model):
    __tablename__ = 'wallets'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    balance      = db.Column(db.Float, default=0.0)
    escrow       = db.Column(db.Float, default=0.0)
    total_earned = db.Column(db.Float, default=0.0)
    total_spent  = db.Column(db.Float, default=0.0)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(balance=round(self.balance,2), escrow=round(self.escrow,2),
                    total_earned=round(self.total_earned,2), total_spent=round(self.total_spent,2))

class WalletLedger(db.Model):
    __tablename__ = 'wallet_ledger'
    id            = db.Column(db.Integer, primary_key=True)
    wallet_id     = db.Column(db.Integer, db.ForeignKey('wallets.id'), nullable=False)
    type          = db.Column(db.String(30), nullable=False)
    amount        = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float, nullable=False)
    description   = db.Column(db.String(255))
    reference     = db.Column(db.String(100))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=round(self.amount,2),
                    balance_after=round(self.balance_after,2), description=self.description,
                    reference=self.reference, created_at=self.created_at.isoformat())

class Task(db.Model):
    __tablename__ = 'tasks'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    instructions= db.Column(db.Text, nullable=True)  # Detailed step-by-step
    category    = db.Column(db.String(80), nullable=False)
    reward      = db.Column(db.Float, nullable=False)
    requires_pdf= db.Column(db.Boolean, default=True)  # Require PDF submission
    is_active   = db.Column(db.Boolean, default=True)
    is_flagged  = db.Column(db.Boolean, default=False)
    flag_reason = db.Column(db.String(255))
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    deadline    = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    creator     = db.relationship('User', foreign_keys=[created_by])
    completions = db.relationship('TaskCompletion', backref='task', lazy='dynamic', cascade='all,delete')

    def to_dict(self):
        return dict(id=self.id, title=self.title, description=self.description,
                    instructions=self.instructions, category=self.category, reward=self.reward,
                    requires_pdf=self.requires_pdf, is_active=self.is_active,
                    is_flagged=self.is_flagged, flag_reason=self.flag_reason,
                    deadline=self.deadline.isoformat() if self.deadline else None,
                    completion_count=self.completions.count(),
                    created_by=self.created_by,
                    created_at=self.created_at.isoformat())

class TaskCompletion(db.Model):
    __tablename__ = 'task_completions'
    id             = db.Column(db.Integer, primary_key=True)
    task_id        = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    proof_text     = db.Column(db.Text, nullable=True)
    pdf_filename   = db.Column(db.String(255), nullable=True)  # Stored PDF filename
    pdf_original   = db.Column(db.String(255), nullable=True)  # Original filename
    status         = db.Column(db.String(20), default='pending')  # pending|approved|rejected
    rejection_reason = db.Column(db.Text, nullable=True)
    ai_score       = db.Column(db.Float, nullable=True)  # AI detection score 0-100
    ai_result      = db.Column(db.String(20), nullable=True)  # human|ai|mixed
    ai_checked     = db.Column(db.Boolean, default=False)
    reviewed_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at    = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    user           = db.relationship('User', foreign_keys=[user_id], backref='completions')
    reviewer       = db.relationship('User', foreign_keys=[reviewed_by])

    def to_dict(self):
        return dict(id=self.id, task_id=self.task_id, user_id=self.user_id,
                    username=self.user.username if self.user else None,
                    proof_text=self.proof_text, pdf_filename=self.pdf_filename,
                    pdf_original=self.pdf_original,
                    pdf_url=f'/uploads/{self.pdf_filename}' if self.pdf_filename else None,
                    status=self.status, rejection_reason=self.rejection_reason,
                    ai_score=self.ai_score, ai_result=self.ai_result,
                    ai_checked=self.ai_checked,
                    reviewed_by=self.reviewed_by,
                    reviewer_name=self.reviewer.username if self.reviewer else None,
                    reviewed_at=self.reviewed_at.isoformat() if self.reviewed_at else None,
                    created_at=self.created_at.isoformat())

class Share(db.Model):
    __tablename__ = 'shares'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    quantity    = db.Column(db.Integer, nullable=False)
    price_each  = db.Column(db.Float, nullable=False)
    total_paid  = db.Column(db.Float, nullable=False)
    status      = db.Column(db.String(20), default='active')
    purchased_at= db.Column(db.DateTime, default=datetime.utcnow)
    user        = db.relationship('User', backref='shares')

    def to_dict(self):
        return dict(id=self.id, user_id=self.user_id, quantity=self.quantity,
                    price_each=self.price_each, total_paid=self.total_paid,
                    status=self.status, purchased_at=self.purchased_at.isoformat())

class Payment(db.Model):
    __tablename__ = 'payments'
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    type             = db.Column(db.String(30), nullable=False)
    amount           = db.Column(db.Float, nullable=False)
    fee              = db.Column(db.Float, default=0.0)
    net_amount       = db.Column(db.Float, nullable=False)
    phone            = db.Column(db.String(20))
    reference        = db.Column(db.String(100))
    intasend_id      = db.Column(db.String(100), nullable=True)
    status           = db.Column(db.String(20), default='pending')
    webhook_received = db.Column(db.Boolean, default=False)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    user             = db.relationship('User', backref='payments')

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=self.amount, fee=self.fee,
                    net_amount=self.net_amount, phone=self.phone,
                    reference=self.reference, status=self.status,
                    created_at=self.created_at.isoformat())

class Message(db.Model):
    __tablename__ = 'messages'
    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    message      = db.Column(db.Text, nullable=False)
    is_read      = db.Column(db.Boolean, default=False)
    is_broadcast = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    sender       = db.relationship('User', foreign_keys=[sender_id], backref='sent_msgs')
    receiver     = db.relationship('User', foreign_keys=[receiver_id], backref='recv_msgs')

    def to_dict(self):
        return dict(id=self.id, sender_id=self.sender_id,
                    sender_name=self.sender.username if self.sender else 'System',
                    sender_avatar=self.sender.avatar_url if self.sender else None,
                    receiver_id=self.receiver_id, message=self.message,
                    is_read=self.is_read, is_broadcast=self.is_broadcast,
                    created_at=self.created_at.isoformat())

class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(20), default='info')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, title=self.title, body=self.body,
                    type=self.type, is_read=self.is_read,
                    created_at=self.created_at.isoformat())

class Alert(db.Model):
    """Admin-sent platform-wide alerts"""
    __tablename__ = 'alerts'
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(20), default='info')  # info|warning|danger|success
    is_active  = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    creator    = db.relationship('User')

    def to_dict(self):
        return dict(id=self.id, title=self.title, message=self.message,
                    type=self.type, is_active=self.is_active,
                    created_at=self.created_at.isoformat(),
                    expires_at=self.expires_at.isoformat() if self.expires_at else None)

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def gen_code():
    return ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))

def make_token(uid, role):
    payload = {'sub': uid, 'role': role,
               'exp': datetime.utcnow()+timedelta(hours=app.config['JWT_EXPIRY_HOURS']),
               'iat': datetime.utcnow()}
    return jwt.encode(payload, app.config['JWT_SECRET'], algorithm='HS256')

def decode_token(token):
    return jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])

def get_current_user():
    auth = request.headers.get('Authorization', '')
    # Handle both "Bearer <token>" and bare token values
    if auth.lower().startswith('bearer '):
        token = auth[7:].strip()
    else:
        token = auth.strip()
    # Fallback to query param (used by Socket.IO and some redirects)
    if not token:
        token = request.args.get('token', '').strip()
    if not token:
        return None
    try:
        data = decode_token(token)
        user = User.query.get(data['sub'])
        if user is None:
            logger.warning(f'Token valid but user {data.get("sub")} not found in DB')
        return user
    except jwt.ExpiredSignatureError:
        logger.debug('Rejected expired JWT')
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f'Rejected invalid JWT: {e}')
        return None
    except Exception as e:
        logger.error(f'get_current_user unexpected error: {e}')
        return None

def require_auth(f):
    @wraps(f)
    def dec(*a,**kw):
        u = get_current_user()
        if not u or not u.is_active or u.is_suspended:
            return jsonify(error='Unauthorized'),401
        return f(u,*a,**kw)
    return dec

def require_admin(f):
    @wraps(f)
    def dec(*a,**kw):
        u = get_current_user()
        if not u or u.role not in ('admin',):
            return jsonify(error='Admin only'),403
        return f(u,*a,**kw)
    return dec

def require_moderator(f):
    @wraps(f)
    def dec(*a,**kw):
        u = get_current_user()
        if not u or u.role not in ('admin','moderator'):
            return jsonify(error='Moderator access required'),403
        return f(u,*a,**kw)
    return dec

def notify(user_id, title, body, ntype='info'):
    n = Notification(user_id=user_id, title=title, body=body, type=ntype)
    db.session.add(n)
    try: socketio.emit('notification', n.to_dict(), room=f'user_{user_id}')
    except: pass

def ledger(wallet, tx_type, amount, desc, ref=None):
    wallet.updated_at = datetime.utcnow()
    db.session.add(WalletLedger(wallet_id=wallet.id, type=tx_type,
        amount=amount, balance_after=wallet.balance, description=desc, reference=ref))

def get_setting(key, default):
    v = PlatformSettings.get(key, None)
    if v is None: return default
    try: return float(v)
    except: return v

def broadcast_settings_update():
    """Push updated settings to all connected clients"""
    try:
        keys = ['activation_fee','referral_bonus','premium_fee','withdrawal_fee_pct',
                'free_daily_limit','premium_daily_limit','share_price','base_url']
        defaults = {'activation_fee': app.config['DEFAULT_ACTIVATION_FEE'],
                    'referral_bonus': app.config['DEFAULT_REFERRAL_BONUS'],
                    'premium_fee': app.config['DEFAULT_PREMIUM_FEE'],
                    'withdrawal_fee_pct': app.config['DEFAULT_WITHDRAWAL_FEE_PCT'],
                    'free_daily_limit': app.config['FREE_DAILY_LIMIT'],
                    'premium_daily_limit': app.config['PREMIUM_DAILY_LIMIT'],
                    'share_price': '100.0', 'base_url': app.config['BASE_URL']}
        settings = {k: PlatformSettings.get(k, defaults.get(k,'')) for k in keys}
        socketio.emit('settings_update', settings, to=None)
    except: pass

# ─────────────────────────────────────────
# AI DETECTION
# ─────────────────────────────────────────

def detect_ai_content(text):
    """
    Uses Sapling AI detector (free tier) or fallback heuristic.
    Returns dict: {score: 0-100, result: 'human'|'ai'|'mixed', details: str}
    """
    if not text or len(text.strip()) < 50:
        return {'score': 0, 'result': 'insufficient', 'details': 'Text too short for analysis'}

    # Try Sapling AI detector (free, no signup for basic usage)
    try:
        r = http_req.post(
            'https://api.sapling.ai/api/v1/aidetect',
            json={'key': os.environ.get('SAPLING_API_KEY', 'demo'), 'text': text[:5000]},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            score = round(data.get('score', 0) * 100, 1)
            result = 'ai' if score >= 70 else ('mixed' if score >= 40 else 'human')
            return {'score': score, 'result': result, 'details': f'Sapling AI: {score}% AI probability'}
    except: pass

    # Fallback: GPTZero-style heuristic analysis
    try:
        sentences = re.split(r'[.!?]+', text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

        # Burstiness (variance in sentence length)
        lengths = [len(s) for s in sentences]
        if len(lengths) > 1:
            mean_len = sum(lengths) / len(lengths)
            variance = sum((l - mean_len)**2 for l in lengths) / len(lengths)
            burstiness = variance / (mean_len + 1)
        else:
            burstiness = 0

        # AI phrases detection
        ai_phrases = ['furthermore','moreover','in conclusion','it is worth noting','it should be noted',
                      'in summary','to summarize','as a result','therefore','consequently',
                      'in addition','additionally','nevertheless','however','thus']
        ai_phrase_count = sum(1 for p in ai_phrases if p.lower() in text.lower())

        # Score based on heuristics
        score = 0
        if burstiness < 50: score += 30   # Low variance = AI
        if ai_phrase_count >= 3: score += 30
        if len(sentences) > 5 and all(40 < len(s) < 200 for s in sentences[:5]): score += 20
        score = min(score, 95)

        result = 'ai' if score >= 70 else ('mixed' if score >= 40 else 'human')
        return {'score': score, 'result': result,
                'details': f'Heuristic analysis: {score}% AI probability (burstiness={burstiness:.0f}, AI phrases={ai_phrase_count})'}
    except Exception as e:
        return {'score': 0, 'result': 'error', 'details': str(e)}

# ─────────────────────────────────────────
# INTASEND PAYMENTS
# ─────────────────────────────────────────

def intasend_stk_push(phone, amount, ref, currency='KES'):
    """Initiate M-Pesa STK Push via IntaSend"""
    if app.config['DEMO_MODE']:
        return {'status': 'demo', 'id': f'DEMO-{ref}'}

    try:
        r = http_req.post(
            f'{INTASEND_BASE}/api/v1/payment/mpesa-stk-push/',
            headers={
                'Authorization': f'Bearer {app.config["INTASEND_API_KEY"]}',
                'Content-Type': 'application/json'
            },
            json=dict(amount=amount, phone_number=phone, api_ref=ref, currency=currency),
            timeout=15
        )
        data = r.json()
        if r.status_code in (200, 201):
            return {'status': 'pending', 'id': data.get('id', ''), 'data': data}
        return {'status': 'error', 'message': data.get('detail', 'Payment failed')}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def intasend_b2c(phone, amount, ref):
    """Send money to user via IntaSend B2C"""
    if app.config['DEMO_MODE']:
        return {'status': 'demo', 'id': f'DEMO-B2C-{ref}'}
    try:
        r = http_req.post(
            f'{INTASEND_BASE}/api/v1/send-money/mpesa/',
            headers={
                'Authorization': f'Bearer {app.config["INTASEND_API_KEY"]}',
                'Content-Type': 'application/json'
            },
            json=dict(currency='KES', transactions=[
                {'name': 'User', 'account': phone, 'amount': amount}
            ], api_ref=ref),
            timeout=15
        )
        data = r.json()
        return {'status': 'pending' if r.status_code in (200,201) else 'error', 'data': data}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

# ─────────────────────────────────────────
# INTASEND WEBHOOK
# ─────────────────────────────────────────

@app.route('/webhook/intasend', methods=['POST'])
def intasend_webhook():
    """IntaSend webhook handler for payment status updates"""
    import hmac as _hmac, hashlib as _hashlib
    # FIX: read raw body BEFORE calling get_json() — get_data() returns empty after get_json()
    raw_body = request.get_data()
    try:
        payload = request.get_json(force=True) or {}
        logger.info(f'IntaSend webhook: {payload}')

        # FIX: hmac.new does not exist — correct function is hmac.new → hmac.new is wrong;
        # the correct call is hmac.new() → actually Python's hmac module uses hmac.new()
        # which IS valid in Python, but let's use the explicit constructor for clarity.
        if app.config['INTASEND_SECRET']:
            sig = request.headers.get('X-IntaSend-Signature', '')
            # FIX: was `hmac.new(...)` — Python hmac module exposes `hmac.new()` but the
            # idiomatic and correct form that definitely works is shown below.
            expected = _hmac.new(
                app.config['INTASEND_SECRET'].encode(),
                raw_body,
                _hashlib.sha256
            ).hexdigest()
            if not _hmac.compare_digest(sig, expected):
                logger.warning('IntaSend webhook signature mismatch')
                return jsonify(error='Invalid signature'), 403

        invoice_id = payload.get('invoice_id') or payload.get('id') or ''
        state = payload.get('state', '').upper()
        api_ref = payload.get('api_ref', '')

        # Find payment by reference
        pay = Payment.query.filter_by(reference=api_ref).first()
        if not pay:
            pay = Payment.query.filter_by(intasend_id=invoice_id).first()

        if pay:
            # FIX: idempotency — skip if already processed to prevent duplicate payouts
            if state == 'COMPLETE':
                if pay.status == 'completed':
                    logger.info(f'Webhook idempotency: payment {pay.id} already completed, skipping')
                    return jsonify(status='already_processed'), 200
                pay.status = 'completed'
                pay.webhook_received = True
                db.session.flush()
                # Handle based on payment type
                _process_completed_payment(pay)
            elif state in ('FAILED', 'CANCELLED'):
                if pay.status not in ('completed',):  # don't downgrade a completed payment
                    pay.status = 'failed'
                    pay.webhook_received = True
                    notify(pay.user_id, '❌ Payment Failed',
                           f'Your {pay.type} payment of KES {pay.amount:.0f} failed.', 'error')
            db.session.commit()
        else:
            logger.warning(f'IntaSend webhook: no payment found for ref={api_ref} invoice={invoice_id}')

        return jsonify(status='received'), 200
    except Exception as e:
        logger.error(f'Webhook error: {e}')
        db.session.rollback()
        return jsonify(error='internal_error'), 500

def _process_completed_payment(pay):
    """Process a completed payment"""
    user = User.query.get(pay.user_id)
    if not user: return

    if pay.type == 'activation' and not user.is_activated:
        user.is_activated = True
        user.activation_paid_at = datetime.utcnow()
        _pay_referral_bonus(user, pay.amount)
        notify(user.id, '🎉 Account Activated!',
               f'Your account is now active! KES {pay.amount:.0f} paid.', 'success')

    elif pay.type == 'premium' and not user.is_premium_active:
        user.tier = 'premium'
        user.premium_expires = datetime.utcnow() + timedelta(days=30)
        notify(user.id, '⭐ Premium Activated!', '30 days of premium access unlocked!', 'success')

    elif pay.type == 'deposit':
        w = user.wallet
        w.balance += pay.amount
        w.total_earned += pay.amount
        ledger(w, 'deposit', pay.amount, 'M-Pesa deposit', pay.reference)
        notify(user.id, '💳 Deposit Received', f'KES {pay.amount:.0f} added to wallet', 'success')

def _pay_referral_bonus(user, activation_fee):
    if not user.referred_by: return
    ref_user = User.query.get(user.referred_by)
    if not ref_user: return
    bonus = get_setting('referral_bonus', app.config['DEFAULT_REFERRAL_BONUS'])
    wallet = ref_user.wallet
    if not wallet: return
    wallet.balance += bonus
    wallet.total_earned += bonus
    ledger(wallet, 'referral', bonus, f'Referral bonus — {user.username} activated')
    notify(ref_user.id, '💰 Referral Bonus!',
           f'{user.username} activated! You earned KES {bonus:.0f}', 'success')

# ─────────────────────────────────────────
# FILE UPLOADS
# ─────────────────────────────────────────

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ─────────────────────────────────────────
# GOOGLE OAUTH
# ─────────────────────────────────────────

@app.route('/auth/google')
def google_login():
    if not app.config['GOOGLE_CLIENT_ID']:
        return redirect('/?error=google_not_configured')
    state = secrets.token_urlsafe(32)
    # FIX: mark session permanent so the state cookie is returned after Google redirects back.
    # Without this, default non-permanent sessions may not send the cookie depending on browser
    # SameSite settings, causing the state check in the callback to always fail.
    session.permanent = True
    session['oauth_state'] = state
    # Carry pending referral code through the OAuth flow
    ref = request.args.get('ref','')
    if ref:
        session['pending_ref'] = ref
    params = dict(client_id=app.config['GOOGLE_CLIENT_ID'],
                  redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
                  response_type='code', scope='openid email profile',
                  state=state, access_type='offline', prompt='select_account')
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@app.route('/auth/google/callback')
def google_callback():
    # FIX: check for error first, then compare state — avoids a timing issue where
    # session.pop() is called even when there is no state to compare against.
    error = request.args.get('error')
    received_state = request.args.get('state','')
    expected_state = session.pop('oauth_state', None)
    if error or not expected_state or received_state != expected_state:
        logger.warning(f'OAuth state mismatch: error={error}, received={received_state}, expected={expected_state}')
        return redirect('/?error=oauth_failed')
    try:
        resp = http_req.post(GOOGLE_TOKEN_URL, data=dict(
            code=request.args['code'], client_id=app.config['GOOGLE_CLIENT_ID'],
            client_secret=app.config['GOOGLE_CLIENT_SECRET'],
            redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
            grant_type='authorization_code'), timeout=10).json()
        if 'error' in resp:
            logger.error(f'Google token exchange error: {resp}')
            return redirect('/?error=oauth_token_failed')
        ui = http_req.get(GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {resp["access_token"]}'}, timeout=10).json()
        g_id, email, name, pic = ui['sub'], ui.get('email',''), ui.get('name',''), ui.get('picture','')
        if not email:
            return redirect('/?error=oauth_no_email')
        ref_code = session.pop('pending_ref', None)
        user = User.query.filter_by(google_id=g_id).first() or User.query.filter_by(email=email).first()
        if not user:
            uname = re.sub(r'[^a-z0-9_]','', name.lower().replace(' ','_'))[:20] or 'user'
            if User.query.filter_by(username=uname).first():
                uname += str(int(time.time()))[-4:]
            referred_by = None
            if ref_code:
                ref_user = User.query.filter_by(referral_code=ref_code).first()
                if ref_user: referred_by = ref_user.id
            user = User(email=email, username=uname, google_id=g_id, avatar_url=pic,
                        referral_code=gen_code(), referred_by=referred_by)
            db.session.add(user)
            db.session.flush()
            db.session.add(Wallet(user_id=user.id))
            db.session.commit()
        else:
            user.google_id = g_id
            if pic: user.avatar_url = pic
            user.last_login = datetime.utcnow()
            db.session.commit()
        token = make_token(user.id, user.role)
        return redirect(f'/?token={token}')
    except Exception as e:
        logger.error(f'Google OAuth: {e}')
        return redirect('/?error=oauth_failed')

# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    d = request.get_json() or {}
    email = (d.get('email') or '').strip().lower()
    username = (d.get('username') or '').strip()
    password = d.get('password','')
    ref_code = (d.get('ref_code') or '').strip().upper()

    if not email or not username or not password:
        return jsonify(error='All fields required'),400
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return jsonify(error='Invalid email'),400
    if len(password) < 8:
        return jsonify(error='Password min 8 chars'),400
    if User.query.filter_by(email=email).first():
        return jsonify(error='Email already registered'),409
    if User.query.filter_by(username=username).first():
        return jsonify(error='Username taken'),409

    referred_by = None
    if ref_code:
        ref_user = User.query.filter_by(referral_code=ref_code).first()
        if ref_user: referred_by = ref_user.id

    user = User(email=email, username=username, referral_code=gen_code(), referred_by=referred_by)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    db.session.add(Wallet(user_id=user.id))
    db.session.commit()
    token = make_token(user.id, user.role)
    return jsonify(token=token, user=user.to_dict()), 201

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("20 per hour")
def login():
    d = request.get_json() or {}
    user = User.query.filter_by(email=(d.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(d.get('password','')):
        return jsonify(error='Invalid credentials'),401
    if not user.is_active: return jsonify(error='Account suspended'),403
    if user.is_suspended: return jsonify(error=f'Account suspended: {user.suspension_reason or "Contact admin"}'),403
    user.last_login = datetime.utcnow()
    db.session.commit()
    return jsonify(token=make_token(user.id, user.role), user=user.to_dict())

@app.route('/api/auth/me')
@require_auth
def me(user):
    return jsonify(user=user.to_dict(), wallet=user.wallet.to_dict() if user.wallet else None)

# ─────────────────────────────────────────
# PLATFORM SETTINGS
# ─────────────────────────────────────────

@app.route('/api/admin/settings', methods=['GET','POST'])
@require_admin
def platform_settings(admin):
    keys = ['activation_fee','referral_bonus','premium_fee','withdrawal_fee_pct',
            'free_daily_limit','premium_daily_limit','share_price','base_url']
    defaults = {'activation_fee': app.config['DEFAULT_ACTIVATION_FEE'],
                'referral_bonus': app.config['DEFAULT_REFERRAL_BONUS'],
                'premium_fee': app.config['DEFAULT_PREMIUM_FEE'],
                'withdrawal_fee_pct': app.config['DEFAULT_WITHDRAWAL_FEE_PCT'],
                'free_daily_limit': app.config['FREE_DAILY_LIMIT'],
                'premium_daily_limit': app.config['PREMIUM_DAILY_LIMIT'],
                'share_price': '100.0', 'base_url': app.config['BASE_URL']}
    if request.method == 'GET':
        return jsonify({k: PlatformSettings.get(k, defaults.get(k,'')) for k in keys})
    d = request.get_json() or {}
    for k,v in d.items():
        if k in keys: PlatformSettings.set(k, v)
    db.session.commit()
    cache.clear()
    broadcast_settings_update()  # Push to all clients
    return jsonify(ok=True)

@app.route('/api/settings/public')
def public_settings():
    """Public settings endpoint (non-sensitive)"""
    return jsonify({
        'activation_fee': PlatformSettings.get('activation_fee', app.config['DEFAULT_ACTIVATION_FEE']),
        'referral_bonus': PlatformSettings.get('referral_bonus', app.config['DEFAULT_REFERRAL_BONUS']),
        'premium_fee': PlatformSettings.get('premium_fee', app.config['DEFAULT_PREMIUM_FEE']),
        'free_daily_limit': PlatformSettings.get('free_daily_limit', app.config['FREE_DAILY_LIMIT']),
        'premium_daily_limit': PlatformSettings.get('premium_daily_limit', app.config['PREMIUM_DAILY_LIMIT']),
        'share_price': PlatformSettings.get('share_price', '100.0'),
    })

# ─────────────────────────────────────────
# ACTIVATION & PREMIUM
# ─────────────────────────────────────────

@app.route('/api/activate', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def activate(user):
    if user.is_activated:
        return jsonify(error='Already activated'),400
    d = request.get_json() or {}
    phone = d.get('phone','').strip()
    fee = get_setting('activation_fee', app.config['DEFAULT_ACTIVATION_FEE'])
    ref = f'ACT-{uuid.uuid4().hex[:10].upper()}'

    result = intasend_stk_push(phone, fee, ref)
    status = 'completed' if (app.config['DEMO_MODE'] or result.get('status') == 'demo') else 'pending'

    if status == 'completed':
        user.is_activated = True
        user.activation_paid_at = datetime.utcnow()
        _pay_referral_bonus(user, fee)
        notify(user.id, '🎉 Account Activated!',
               f'You can now earn on DTIP. KES {fee:.0f} activation fee paid.', 'success')

    pay = Payment(user_id=user.id, type='activation', amount=fee, fee=0,
                  net_amount=fee, phone=phone, reference=ref,
                  intasend_id=result.get('id',''), status=status)
    db.session.add(pay)
    db.session.commit()
    return jsonify(ok=True, demo=app.config['DEMO_MODE'], payment=pay.to_dict())

@app.route('/api/upgrade-premium', methods=['POST'])
@require_auth
def upgrade_premium(user):
    if not user.is_activated:
        return jsonify(error='Activate account first'),400
    d = request.get_json() or {}
    phone = d.get('phone','').strip()
    fee = get_setting('premium_fee', app.config['DEFAULT_PREMIUM_FEE'])
    ref = f'PREM-{uuid.uuid4().hex[:10].upper()}'

    result = intasend_stk_push(phone, fee, ref)
    status = 'completed' if (app.config['DEMO_MODE'] or result.get('status') == 'demo') else 'pending'

    if status == 'completed':
        user.tier = 'premium'
        user.premium_suspended = False
        user.premium_expires = datetime.utcnow() + timedelta(days=30)
        notify(user.id, '⭐ Premium Activated!',
               f'30 days premium! Up to {int(get_setting("premium_daily_limit", 10))} tasks/day.', 'success')

    pay = Payment(user_id=user.id, type='premium', amount=fee, fee=0,
                  net_amount=fee, phone=phone, reference=ref,
                  intasend_id=result.get('id',''), status=status)
    db.session.add(pay)
    db.session.commit()
    return jsonify(ok=True, user=user.to_dict())

# ─────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    page = request.args.get('page',1,int)
    cat  = request.args.get('category','')
    q    = request.args.get('q','')
    qry  = Task.query.filter_by(is_active=True)
    if cat: qry = qry.filter_by(category=cat)
    if q:   qry = qry.filter(Task.title.ilike(f'%{q}%') | Task.description.ilike(f'%{q}%'))
    tasks = qry.order_by(Task.created_at.desc()).paginate(page=page, per_page=20)
    return jsonify(tasks=[t.to_dict() for t in tasks.items],
                   total=tasks.total, pages=tasks.pages, page=page)

@app.route('/api/tasks', methods=['POST'])
@require_admin
def create_task(admin):
    d = request.get_json() or {}
    if not d.get('title') or not d.get('description') or not d.get('category') or not d.get('reward'):
        return jsonify(error='title, description, category, reward required'),400

    text = (d['title'] + ' ' + d['description']).lower()
    flagged = any(kw in text for kw in SUSPICIOUS_KEYWORDS)

    deadline = None
    if d.get('deadline'):
        try: deadline = datetime.fromisoformat(d['deadline'])
        except: pass

    task = Task(title=d['title'].strip(), description=d['description'].strip(),
                instructions=d.get('instructions','').strip(),
                category=d['category'], reward=float(d['reward']),
                requires_pdf=d.get('requires_pdf', True),
                deadline=deadline, created_by=admin.id,
                is_flagged=flagged, flag_reason='Auto-flagged: suspicious content' if flagged else None)
    db.session.add(task)
    db.session.commit()

    try:
        uids = [u.id for u in User.query.filter_by(is_active=True, is_activated=True).all()]
        for uid in uids:
            notify(uid, f'🆕 New Task: {task.title}',
                   f'Earn KES {task.reward:.0f} — {task.category}', 'info')
        socketio.emit('new_task', task.to_dict(), to=None)
    except: pass
    return jsonify(task=task.to_dict()), 201

@app.route('/api/tasks/<int:tid>', methods=['GET'])
def get_task(tid):
    task = Task.query.get_or_404(tid)
    d = task.to_dict()
    user = get_current_user()
    if user:
        done = TaskCompletion.query.filter_by(task_id=tid, user_id=user.id).first()
        d['user_completion'] = done.to_dict() if done else None
    return jsonify(task=d)

@app.route('/api/tasks/<int:tid>/submit', methods=['POST'])
@require_auth
def submit_task(user, tid):
    """Submit task with PDF proof"""
    if not user.is_activated:
        return jsonify(error='activate_required', message='Pay activation fee to start earning'),403

    task = Task.query.get_or_404(tid)
    if not task.is_active:
        return jsonify(error='Task is not active'),400

    done_today = user.get_daily_tasks_done()
    limit = user.daily_limit()
    if done_today >= limit:
        return jsonify(error='daily_limit',
                       message=f'Daily limit reached ({limit}/day). Upgrade to premium for more tasks.',
                       limit=limit, done=done_today),429

    existing = TaskCompletion.query.filter_by(task_id=tid, user_id=user.id).first()
    if existing:
        return jsonify(error='Already submitted this task'),409

    # Handle PDF upload
    pdf_filename = None
    pdf_original = None
    if 'pdf' in request.files:
        f = request.files['pdf']
        if f and f.filename:
            # FIX: validate by both extension AND magic bytes to prevent disguised uploads
            if not f.filename.lower().endswith('.pdf'):
                return jsonify(error='Only PDF files are accepted'),400
            # Check PDF magic bytes (%PDF-)
            header = f.read(5)
            f.seek(0)  # rewind after peeking
            if header != b'%PDF-':
                return jsonify(error='File does not appear to be a valid PDF'),400
            # FIX: sanitize the stored filename — never use the user-supplied name directly
            pdf_original = re.sub(r'[^a-zA-Z0-9._\- ]', '', f.filename)[:200] or 'upload.pdf'
            pdf_filename = f'{uuid.uuid4().hex}.pdf'
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], pdf_filename)
            try:
                f.save(save_path)
            except OSError as save_err:
                logger.error(f'PDF save error: {save_err}')
                return jsonify(error='Failed to save uploaded file, please try again'),500

    if task.requires_pdf and not pdf_filename:
        return jsonify(error='This task requires a PDF submission'),400

    proof_text = request.form.get('proof_text','').strip()

    comp = TaskCompletion(task_id=tid, user_id=user.id,
                          proof_text=proof_text,
                          pdf_filename=pdf_filename,
                          pdf_original=pdf_original,
                          status='pending')
    db.session.add(comp)
    user.increment_task_count()
    db.session.commit()

    # Run AI detection on proof text
    if proof_text and len(proof_text) >= 50:
        _run_ai_detection(comp.id, proof_text)

    # Notify moderators/admins
    mods = User.query.filter(User.role.in_(['admin','moderator']), User.is_active==True).all()
    for mod in mods:
        notify(mod.id, f'📥 New Submission: {task.title}',
               f'{user.username} submitted task for review', 'info')

    notify(user.id, '📤 Submission Received',
           f'Your submission for "{task.title}" is pending review.', 'info')

    return jsonify(completion=comp.to_dict(), message='Submitted for review')

def _run_ai_detection(comp_id, text):
    """Run AI detection and update completion"""
    try:
        result = detect_ai_content(text)
        comp = TaskCompletion.query.get(comp_id)
        if comp:
            comp.ai_score = result['score']
            comp.ai_result = result['result']
            comp.ai_checked = True
            db.session.commit()

            # Alert if high AI score
            if result['score'] >= 70:
                mods = User.query.filter(User.role.in_(['admin','moderator'])).all()
                for mod in mods:
                    notify(mod.id, '🤖 AI Content Detected',
                           f'Submission #{comp_id} by {comp.user.username if comp.user else "?"} — '
                           f'{result["score"]}% AI probability. Review required.', 'warn')
                socketio.emit('ai_alert', {
                    'completion_id': comp_id,
                    'score': result['score'],
                    'result': result['result'],
                    'username': comp.user.username if comp.user else '?'
                }, room='moderators')
    except Exception as e:
        logger.error(f'AI detection error: {e}')

@app.route('/api/completions/<int:cid>/review', methods=['POST'])
@require_moderator
def review_completion(mod, cid):
    """Approve or reject a task completion"""
    comp = TaskCompletion.query.get_or_404(cid)
    d = request.get_json() or {}
    action = d.get('action')  # 'approve' or 'reject'
    reason = d.get('reason','').strip()

    if action not in ('approve','reject'):
        return jsonify(error='action must be approve or reject'),400

    comp.status = 'approved' if action == 'approve' else 'rejected'
    comp.rejection_reason = reason if action == 'reject' else None
    comp.reviewed_by = mod.id
    comp.reviewed_at = datetime.utcnow()

    if action == 'approve':
        # Pay reward
        task = Task.query.get(comp.task_id)
        user = User.query.get(comp.user_id)
        if task and user and user.wallet:
            w = user.wallet
            w.balance += task.reward
            w.total_earned += task.reward
            ledger(w, 'task_reward', task.reward, f'Task reward: {task.title}', str(task.id))
            notify(user.id, '✅ Task Approved & Paid!',
                   f'KES {task.reward:.0f} added for "{task.title}"', 'success')
    else:
        user = User.query.get(comp.user_id)
        task = Task.query.get(comp.task_id)
        if user:
            notify(user.id, '❌ Submission Rejected',
                   f'Your submission for "{task.title if task else "task"}" was rejected. {reason}', 'error')

    db.session.commit()
    return jsonify(completion=comp.to_dict())

@app.route('/api/completions/pending')
@require_moderator
def pending_completions(mod):
    comps = TaskCompletion.query.filter_by(status='pending')\
                                .order_by(TaskCompletion.created_at.desc()).limit(100).all()
    return jsonify(completions=[c.to_dict() for c in comps])

@app.route('/api/completions/<int:cid>/ai-check', methods=['POST'])
@require_moderator
def rerun_ai_check(mod, cid):
    """Re-run AI detection on a submission"""
    comp = TaskCompletion.query.get_or_404(cid)
    text = comp.proof_text or ''
    if not text:
        return jsonify(error='No text to analyze'),400
    result = detect_ai_content(text)
    comp.ai_score = result['score']
    comp.ai_result = result['result']
    comp.ai_checked = True
    db.session.commit()
    return jsonify(ai_result=result, completion=comp.to_dict())

@app.route('/api/tasks/<int:tid>', methods=['PUT'])
@require_admin
def update_task(admin, tid):
    task = Task.query.get_or_404(tid)
    d = request.get_json() or {}
    for f in ('title','description','instructions','category','reward','is_active','requires_pdf'):
        if f in d: setattr(task, f, d[f])
    db.session.commit()
    return jsonify(task=task.to_dict())

@app.route('/api/tasks/<int:tid>', methods=['DELETE'])
@require_admin
def delete_task(admin, tid):
    task = Task.query.get_or_404(tid)
    task.is_active = False
    db.session.commit()
    return jsonify(ok=True)

@app.route('/api/my/completions')
@require_auth
def my_completions(user):
    comps = TaskCompletion.query.filter_by(user_id=user.id)\
                                .order_by(TaskCompletion.created_at.desc()).limit(50).all()
    return jsonify(completions=[c.to_dict() for c in comps])

# ─────────────────────────────────────────
# SHARES
# ─────────────────────────────────────────

@app.route('/api/shares', methods=['GET'])
@require_auth
def my_shares(user):
    shares = Share.query.filter_by(user_id=user.id).all()
    price = get_setting('share_price', 100.0)
    total_qty = sum(s.quantity for s in shares)
    return jsonify(shares=[s.to_dict() for s in shares],
                   total_shares=total_qty, share_price=price,
                   portfolio_value=round(total_qty * price, 2))

@app.route('/api/shares/history')
def shares_history():
    """Get share purchase history for graph (public aggregate)"""
    try:
        # Aggregate by date
        from sqlalchemy import func
        rows = db.session.query(
            func.date(Share.purchased_at).label('date'),
            func.sum(Share.quantity).label('qty'),
            func.sum(Share.total_paid).label('revenue')
        ).filter_by(status='active').group_by(func.date(Share.purchased_at)).order_by('date').all()
        data = [{'date': str(r.date), 'qty': int(r.qty or 0), 'revenue': float(r.revenue or 0)} for r in rows]
        # Add cumulative
        total = 0
        for d in data:
            total += d['qty']
            d['total'] = total
        return jsonify(history=data, current_price=get_setting('share_price', 100.0))
    except Exception as e:
        return jsonify(history=[], current_price=100.0)

@app.route('/api/shares/buy', methods=['POST'])
@require_auth
def buy_shares(user):
    if not user.is_activated:
        return jsonify(error='Activate account first'),403
    d = request.get_json() or {}
    qty = int(d.get('quantity', 1))
    if qty < 1: return jsonify(error='Minimum 1 share'),400
    price = get_setting('share_price', 100.0)
    total = qty * price
    wallet = user.wallet
    if wallet.balance < total:
        return jsonify(error=f'Insufficient balance. Need KES {total:.0f}'),400
    wallet.balance -= total
    wallet.total_spent += total
    ledger(wallet, 'share_purchase', -total, f'Bought {qty} share(s) @ KES {price:.0f} each')
    share = Share(user_id=user.id, quantity=qty, price_each=price, total_paid=total)
    db.session.add(share)
    db.session.commit()
    notify(user.id, '📈 Shares Purchased!',
           f'You bought {qty} share(s) for KES {total:.0f}', 'success')
    socketio.emit('shares_update', {}, to=None)
    return jsonify(share=share.to_dict(), wallet=wallet.to_dict())

@app.route('/api/admin/shares')
@require_admin
def all_shares(admin):
    shares = db.session.query(Share, User).join(User, Share.user_id==User.id).all()
    return jsonify(shares=[{**s.to_dict(), 'username': u.username} for s,u in shares])

# ─────────────────────────────────────────
# WALLET
# ─────────────────────────────────────────

@app.route('/api/wallet')
@require_auth
def get_wallet(user):
    ledger_rows = WalletLedger.query.filter_by(wallet_id=user.wallet.id)\
                              .order_by(WalletLedger.created_at.desc()).limit(30).all()
    return jsonify(wallet=user.wallet.to_dict(), ledger=[l.to_dict() for l in ledger_rows])

@app.route('/api/wallet/deposit', methods=['POST'])
@require_auth
@limiter.limit("10 per hour")
def deposit(user):
    d = request.get_json() or {}
    amount = float(d.get('amount',0))
    phone = d.get('phone','').strip()
    if amount < 10: return jsonify(error='Minimum KES 10'),400
    ref = f'DEP-{uuid.uuid4().hex[:10].upper()}'

    result = intasend_stk_push(phone, amount, ref)
    status = 'completed' if (app.config['DEMO_MODE'] or result.get('status') == 'demo') else 'pending'

    if status == 'completed':
        w = user.wallet
        w.balance += amount; w.total_earned += amount
        ledger(w,'deposit',amount,'M-Pesa deposit',ref)
        notify(user.id,'💳 Deposit Successful',f'KES {amount:.0f} added to wallet','success')

    pay = Payment(user_id=user.id, type='deposit', amount=amount, fee=0,
                  net_amount=amount, phone=phone, reference=ref,
                  intasend_id=result.get('id',''), status=status)
    db.session.add(pay); db.session.commit()
    return jsonify(payment=pay.to_dict(), demo=app.config['DEMO_MODE'])

@app.route('/api/wallet/withdraw', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def withdraw(user):
    if not user.is_activated: return jsonify(error='Activate account first'),403
    d = request.get_json() or {}
    amount = float(d.get('amount',0))
    phone = d.get('phone','').strip()
    if amount < 50: return jsonify(error='Minimum KES 50'),400
    fee_pct = get_setting('withdrawal_fee_pct', app.config['DEFAULT_WITHDRAWAL_FEE_PCT'])
    fee = round(amount * fee_pct / 100, 2)
    net = amount - fee
    w = user.wallet
    if w.balance < amount: return jsonify(error='Insufficient balance'),400
    w.balance -= amount; w.total_spent += amount
    ref = f'WIT-{uuid.uuid4().hex[:10].upper()}'
    ledger(w,'withdraw',-amount,f'Withdrawal to {phone}',ref)

    # Initiate B2C
    b2c_result = intasend_b2c(phone, net, ref)

    pay = Payment(user_id=user.id, type='withdrawal', amount=amount,
                  fee=fee, net_amount=net, phone=phone, reference=ref,
                  intasend_id=b2c_result.get('id',''),
                  status='completed' if app.config['DEMO_MODE'] else 'pending')
    db.session.add(pay); db.session.commit()
    notify(user.id,'💸 Withdrawal Initiated',f'KES {net:.0f} sent to {phone} (fee: KES {fee:.0f})','info')
    return jsonify(payment=pay.to_dict())

# ─────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """Get active alerts"""
    now = datetime.utcnow()
    alerts = Alert.query.filter_by(is_active=True).filter(
        db.or_(Alert.expires_at == None, Alert.expires_at > now)
    ).order_by(Alert.created_at.desc()).limit(10).all()
    return jsonify(alerts=[a.to_dict() for a in alerts])

@app.route('/api/admin/alerts', methods=['GET','POST'])
@require_admin
def manage_alerts(admin):
    if request.method == 'GET':
        alerts = Alert.query.order_by(Alert.created_at.desc()).limit(50).all()
        return jsonify(alerts=[a.to_dict() for a in alerts])
    d = request.get_json() or {}
    if not d.get('title') or not d.get('message'):
        return jsonify(error='title and message required'),400
    expires_at = None
    if d.get('expires_hours'):
        expires_at = datetime.utcnow() + timedelta(hours=int(d['expires_hours']))
    alert = Alert(title=d['title'], message=d['message'],
                  type=d.get('type','info'), created_by=admin.id,
                  expires_at=expires_at)
    db.session.add(alert)
    db.session.commit()
    # Push to all connected users
    socketio.emit('new_alert', alert.to_dict(), to=None)
    # Also notify all users
    for u in User.query.filter_by(is_active=True).all():
        notify(u.id, f'📢 {alert.title}', alert.message, alert.type)
    return jsonify(alert=alert.to_dict()), 201

@app.route('/api/admin/alerts/<int:aid>', methods=['DELETE'])
@require_admin
def delete_alert(admin, aid):
    alert = Alert.query.get_or_404(aid)
    alert.is_active = False
    db.session.commit()
    return jsonify(ok=True)

# ─────────────────────────────────────────
# MESSAGES / CHAT
# ─────────────────────────────────────────

@app.route('/api/messages/<int:other_id>')
@require_auth
def get_messages(user, other_id):
    msgs = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id==user.id, Message.receiver_id==other_id),
            db.and_(Message.sender_id==other_id, Message.receiver_id==user.id)
        )
    ).order_by(Message.created_at.asc()).limit(100).all()
    for m in msgs:
        if m.receiver_id==user.id and not m.is_read: m.is_read=True
    db.session.commit()
    return jsonify(messages=[m.to_dict() for m in msgs])

@app.route('/api/messages/conversations')
@require_auth
def conversations(user):
    sent = db.session.query(Message.receiver_id).filter_by(sender_id=user.id,is_broadcast=False).distinct()
    recv = db.session.query(Message.sender_id).filter_by(receiver_id=user.id,is_broadcast=False).distinct()
    uids = set([r[0] for r in sent]+[r[0] for r in recv]); uids.discard(user.id)
    convs=[]
    for uid in uids:
        u = User.query.get(uid)
        if not u: continue
        last = Message.query.filter(
            db.or_(db.and_(Message.sender_id==user.id,Message.receiver_id==uid),
                   db.and_(Message.sender_id==uid,Message.receiver_id==user.id))
        ).order_by(Message.created_at.desc()).first()
        unread = Message.query.filter_by(sender_id=uid,receiver_id=user.id,is_read=False).count()
        convs.append(dict(user=u.to_dict(),last_message=last.to_dict() if last else None,unread=unread))
    convs.sort(key=lambda x: x['last_message']['created_at'] if x['last_message'] else '',reverse=True)
    return jsonify(conversations=convs)

@app.route('/api/admin/broadcast', methods=['POST'])
@require_admin
def broadcast_msg(admin):
    text = (request.get_json() or {}).get('message','').strip()
    if not text: return jsonify(error='Message required'),400
    msg = Message(sender_id=admin.id, message=text, is_broadcast=True)
    db.session.add(msg); db.session.flush()
    for u in User.query.filter_by(is_active=True).all():
        notify(u.id,'📢 Announcement', text,'info')
    db.session.commit()
    socketio.emit('broadcast', msg.to_dict(), to=None)
    return jsonify(ok=True)

# ─────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────

@app.route('/api/notifications')
@require_auth
def get_notifications(user):
    ns = Notification.query.filter_by(user_id=user.id)\
                           .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify(notifications=[n.to_dict() for n in ns], unread=sum(1 for n in ns if not n.is_read))

@app.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def read_all(user):
    Notification.query.filter_by(user_id=user.id,is_read=False).update({'is_read':True})
    db.session.commit(); return jsonify(ok=True)

# ─────────────────────────────────────────
# ADMIN ROUTES
# ─────────────────────────────────────────

@app.route('/api/admin/stats')
@require_admin
def admin_stats(admin):
    return jsonify(
        total_users=User.query.count(),
        activated_users=User.query.filter_by(is_activated=True).count(),
        premium_users=User.query.filter(User.tier=='premium', User.premium_suspended==False).count(),
        suspended_users=User.query.filter_by(is_suspended=True).count(),
        total_tasks=Task.query.count(),
        active_tasks=Task.query.filter_by(is_active=True).count(),
        total_completions=TaskCompletion.query.count(),
        pending_completions=TaskCompletion.query.filter_by(status='pending').count(),
        total_shares=db.session.query(db.func.sum(Share.quantity)).scalar() or 0,
        total_wallet=round(float(db.session.query(db.func.sum(Wallet.balance)).scalar() or 0),2),
        today_completions=TaskCompletion.query.filter(
            TaskCompletion.created_at>=datetime.utcnow().replace(hour=0,minute=0,second=0)).count()
    )

@app.route('/api/admin/users')
@require_moderator
def admin_users(mod):
    page = request.args.get('page',1,int)
    q    = request.args.get('q','')
    qry  = User.query
    if q: qry = qry.filter(User.username.ilike(f'%{q}%')|User.email.ilike(f'%{q}%'))
    users = qry.order_by(User.created_at.desc()).paginate(page=page,per_page=20)
    return jsonify(users=[u.to_dict() for u in users.items], total=users.total, pages=users.pages)

@app.route('/api/admin/users/<int:uid>/suspend', methods=['POST'])
@require_admin
def suspend_user(admin, uid):
    u = User.query.get_or_404(uid)
    if u.role=='admin': return jsonify(error='Cannot suspend admin'),400
    d = request.get_json() or {}
    u.is_suspended = True
    u.suspension_reason = d.get('reason','Suspended by admin')
    db.session.commit()
    notify(uid,'⛔ Account Suspended',f'Your account has been suspended. Reason: {u.suspension_reason}','error')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/unsuspend', methods=['POST'])
@require_admin
def unsuspend_user(admin, uid):
    u = User.query.get_or_404(uid)
    u.is_suspended = False
    u.suspension_reason = None
    db.session.commit()
    notify(uid,'✅ Account Restored','Your account has been restored.','success')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/remove-premium', methods=['POST'])
@require_admin
def remove_premium(admin, uid):
    u = User.query.get_or_404(uid)
    u.premium_suspended = True
    db.session.commit()
    notify(uid,'⭐ Premium Suspended','Your premium features have been suspended by admin.','warn')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/restore-premium', methods=['POST'])
@require_admin
def restore_premium(admin, uid):
    u = User.query.get_or_404(uid)
    u.premium_suspended = False
    db.session.commit()
    notify(uid,'⭐ Premium Restored','Your premium access has been restored.','success')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/toggle-verify', methods=['POST'])
@require_admin
def toggle_verify(admin, uid):
    u = User.query.get_or_404(uid)
    u.is_verified = not u.is_verified
    db.session.commit()
    notify(uid,'✔ Verification Updated',f'Account {"verified" if u.is_verified else "unverified"}','info')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/set-role', methods=['POST'])
@require_admin
def set_role(admin, uid):
    u = User.query.get_or_404(uid)
    if u.id == admin.id: return jsonify(error='Cannot change own role'),400
    d = request.get_json() or {}
    role = d.get('role')
    if role not in ('member','moderator','admin'):
        return jsonify(error='Invalid role'),400
    u.role = role
    db.session.commit()
    notify(uid, '👤 Role Updated', f'Your role has been changed to {role}.', 'info')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/grant-premium', methods=['POST'])
@require_admin
def grant_premium(admin, uid):
    u = User.query.get_or_404(uid)
    u.tier='premium'; u.premium_suspended=False
    u.premium_expires=datetime.utcnow()+timedelta(days=30)
    db.session.commit()
    notify(uid,'⭐ Premium Granted','Admin granted you 30 days premium!','success')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/completions')
@require_moderator
def admin_completions(mod):
    status = request.args.get('status','')
    qry = TaskCompletion.query
    if status: qry = qry.filter_by(status=status)
    comps = qry.order_by(TaskCompletion.created_at.desc()).limit(100).all()
    return jsonify(completions=[c.to_dict() for c in comps])

# ─────────────────────────────────────────
# PUBLIC
# ─────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify(status='ok', version='3.0.0', ts=datetime.utcnow().isoformat())

@app.route('/api/stats')
@cache.cached(timeout=120)
def pub_stats():
    return jsonify(
        users=User.query.filter_by(is_active=True).count(),
        tasks=Task.query.filter_by(is_active=True).count(),
        completions=TaskCompletion.query.count(),
        paid_out=round(float(db.session.query(db.func.sum(Wallet.total_earned)).scalar() or 0),2)
    )

# ─────────────────────────────────────────
# SOCKETIO
# ─────────────────────────────────────────

def sock_auth():
    # FIX: Socket.IO passes handshake query params in request.args during connect;
    # for subsequent events they are stored in request.environ.  Support both.
    token = (request.args.get('token','') or
             request.environ.get('HTTP_AUTHORIZATION','').replace('Bearer ','').strip())
    if not token:
        return None
    try:
        data = decode_token(token)
        user = User.query.get(data['sub'])
        if user and user.is_active and not user.is_suspended:
            return user
        return None
    except Exception:
        return None

@socketio.on('connect')
def on_connect():
    user = sock_auth()
    if not user: disconnect(); return False
    join_room(f'user_{user.id}')
    if user.role in ('admin','moderator'): join_room('moderators')
    if user.role == 'admin': join_room('admins')
    emit('connected',{'user_id':user.id,'username':user.username,'role':user.role})

@socketio.on('send_message')
def on_send(data):
    user = sock_auth()
    if not user: return
    text = (data.get('message') or '').strip()
    rid  = data.get('receiver_id')
    if not text: return
    if user.role not in ('admin','moderator'):
        admin = User.query.filter_by(role='admin').first()
        if not admin: return
        rid = admin.id
    receiver = User.query.get(rid) if rid else None
    if not receiver: return
    msg = Message(sender_id=user.id, receiver_id=rid, message=text)
    db.session.add(msg); db.session.commit()
    md = msg.to_dict()
    emit('receive_message', md, room=f'user_{rid}')
    emit('receive_message', md, room=f'user_{user.id}')

@socketio.on('mark_read')
def on_mark(data):
    user = sock_auth()
    if not user: return
    Message.query.filter_by(sender_id=data.get('sender_id'), receiver_id=user.id, is_read=False)\
                 .update({'is_read':True})
    db.session.commit()


# ─────────────────────────────────────────
# FRONTEND HTML
# ─────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DTIP — Earn Online in Kenya</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,500;0,9..40,700;0,9..40,900;1,9..40,400&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --font-display: 'Space Grotesk', sans-serif;
  --font-body: 'DM Sans', sans-serif;
}
[data-theme="dark"] {
  --bg: #06080f;
  --bg2: #0a0d17;
  --surface: #0f1320;
  --surface2: #141828;
  --surface3: #1a2035;
  --border: #1e2540;
  --border2: #28304e;
  --text: #e2e8ff;
  --text2: #8b93b8;
  --text3: #4a5270;
  --accent: #00e5a0;
  --accent-dim: rgba(0,229,160,.1);
  --accent-glow: rgba(0,229,160,.25);
  --accent2: #7b6eff;
  --accent2-dim: rgba(123,110,255,.12);
  --gold: #ffb930;
  --gold-dim: rgba(255,185,48,.12);
  --red: #ff5566;
  --red-dim: rgba(255,85,102,.12);
  --orange: #ff8c42;
  --orange-dim: rgba(255,140,66,.12);
  --card-shadow: 0 8px 40px rgba(0,0,0,.5);
}
[data-theme="light"] {
  --bg: #f2f4fc;
  --bg2: #eaecf8;
  --surface: #ffffff;
  --surface2: #f5f7ff;
  --surface3: #eef0fb;
  --border: #dde0f0;
  --border2: #c8ccdf;
  --text: #0d1030;
  --text2: #454870;
  --text3: #8890b0;
  --accent: #00b87a;
  --accent-dim: rgba(0,184,122,.1);
  --accent-glow: rgba(0,184,122,.2);
  --accent2: #5b4de8;
  --accent2-dim: rgba(91,77,232,.1);
  --gold: #d4870e;
  --gold-dim: rgba(212,135,14,.1);
  --red: #e0294a;
  --red-dim: rgba(224,41,74,.1);
  --orange: #e06a1a;
  --orange-dim: rgba(224,106,26,.1);
  --card-shadow: 0 4px 24px rgba(0,0,0,.08);
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font-body);font-size:14px;min-height:100vh;transition:background .3s,color .3s;line-height:1.5}
a{color:var(--accent);text-decoration:none}
h1,h2,h3,.display{font-family:var(--font-display)}
input,textarea,select{background:var(--surface3);border:1.5px solid var(--border);color:var(--text);border-radius:10px;padding:11px 14px;font-family:var(--font-body);font-size:14px;width:100%;outline:none;transition:border .2s,box-shadow .2s}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim)}
select option{background:var(--surface)}
button{cursor:pointer;font-family:var(--font-display);border:none;border-radius:10px;padding:11px 22px;font-size:14px;font-weight:600;transition:all .18s;letter-spacing:-.01em}
.btn{background:var(--accent);color:#000}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 8px 24px var(--accent-glow)}
.btn-v2{background:var(--accent2);color:#fff}
.btn-v2:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn-outline{background:transparent;border:1.5px solid var(--border2);color:var(--text2)}
.btn-outline:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-dim)}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover{filter:brightness(1.1)}
.btn-gold{background:var(--gold);color:#000}
.btn-sm{padding:7px 14px;font-size:12px;border-radius:8px}
.btn-xs{padding:4px 10px;font-size:11px;border-radius:6px}
/* ── LAYOUT ── */
.app{display:grid;grid-template-columns:240px 1fr;min-height:100vh}
.sidebar{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto}
.main{display:flex;flex-direction:column;min-width:0}
/* ── SIDEBAR ── */
.logo-wrap{padding:24px 18px 20px;border-bottom:1px solid var(--border)}
.logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:38px;height:38px;background:var(--accent);border-radius:12px;display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-weight:700;font-size:18px;color:#000;flex-shrink:0}
.logo-text{font-family:var(--font-display);font-size:20px;font-weight:700;letter-spacing:-.5px}
.logo-sub{font-size:10px;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase;margin-top:1px}
.nav{flex:1;padding:14px 10px;display:flex;flex-direction:column;gap:1px}
.nav-section{font-size:10px;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase;padding:14px 10px 6px;font-weight:600}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;color:var(--text2);transition:.15s;font-size:13.5px;font-weight:500;position:relative}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:var(--accent-dim);color:var(--accent);font-weight:600}
.nav-item.active::before{content:'';position:absolute;left:0;top:25%;height:50%;width:3px;background:var(--accent);border-radius:0 3px 3px 0}
.nav-icon{font-size:16px;width:22px;text-align:center;flex-shrink:0}
.nav-badge{margin-left:auto;background:var(--red);color:#fff;border-radius:20px;padding:2px 7px;font-size:10px;font-weight:700;min-width:18px;text-align:center}
.sidebar-bottom{padding:12px 10px;border-top:1px solid var(--border);margin-top:auto}
.user-card{background:var(--surface2);border-radius:12px;padding:12px;display:flex;align-items:center;gap:10px;margin-bottom:10px;border:1px solid var(--border)}
.user-avatar{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;flex-shrink:0;overflow:hidden;color:#fff}
.user-avatar img{width:100%;height:100%;object-fit:cover}
.user-info-wrap{min-width:0}
.user-name{font-weight:700;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-tier{font-size:11px;color:var(--text3);margin-top:1px}
/* ── TOPBAR ── */
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:50}
.page-title{font-family:var(--font-display);font-size:18px;font-weight:700;letter-spacing:-.5px}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.icon-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text2);border-radius:10px;padding:8px 10px;font-size:15px;cursor:pointer;position:relative;transition:.15s;min-width:38px;text-align:center}
.icon-btn:hover{border-color:var(--accent);color:var(--accent)}
.icon-btn .dot{position:absolute;top:4px;right:4px;width:7px;height:7px;background:var(--red);border-radius:50%;border:2px solid var(--surface)}
/* ── CONTENT ── */
.content{flex:1;padding:24px;overflow-y:auto}
/* ── CARDS ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:var(--card-shadow);overflow:hidden}
.card-body{padding:20px}
.card-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border)}
.card-title{font-family:var(--font-display);font-weight:700;font-size:15px;letter-spacing:-.3px}
/* ── STATS GRID ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;position:relative;overflow:hidden;transition:.2s}
.stat-card:hover{border-color:var(--border2);transform:translateY(-2px);box-shadow:var(--card-shadow)}
.stat-bg{position:absolute;top:-15px;right:-15px;width:80px;height:80px;border-radius:50%;opacity:.12;filter:blur(20px)}
.stat-icon{font-size:20px;margin-bottom:10px}
.stat-value{font-family:var(--font-display);font-size:28px;font-weight:700;line-height:1;letter-spacing:-.5px}
.stat-label{font-size:12px;color:var(--text3);margin-top:5px;font-weight:500}
/* ── TASK CARDS ── */
.tasks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.task-card{background:var(--surface);border:1.5px solid var(--border);border-radius:14px;padding:18px;transition:.2s;cursor:pointer;position:relative;overflow:hidden}
.task-card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent2));opacity:0;transition:.2s}
.task-card:hover{border-color:var(--border2);box-shadow:var(--card-shadow);transform:translateY(-2px)}
.task-card:hover::after{opacity:1}
.task-icon-wrap{width:42px;height:42px;border-radius:10px;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;margin-bottom:12px}
.task-title{font-family:var(--font-display);font-weight:700;font-size:14px;line-height:1.4;margin-bottom:8px}
.task-desc{font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.task-footer{display:flex;align-items:center;gap:8px;padding-top:12px;border-top:1px solid var(--border)}
.task-reward{font-family:var(--font-display);font-weight:700;font-size:17px;color:var(--accent)}
.task-cat-tag{background:var(--surface3);color:var(--text3);padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid var(--border)}
/* ── BADGES ── */
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700}
.badge-green{background:var(--accent-dim);color:var(--accent)}
.badge-purple{background:var(--accent2-dim);color:var(--accent2)}
.badge-gold{background:var(--gold-dim);color:var(--gold)}
.badge-red{background:var(--red-dim);color:var(--red)}
.badge-orange{background:var(--orange-dim);color:var(--orange)}
.badge-gray{background:var(--surface3);color:var(--text3)}
/* ── TABLES ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 16px;font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;font-weight:700;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:12px 16px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
/* ── MODAL ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(8px);z-index:1000;display:none;align-items:center;justify-content:center;padding:20px}
.overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:28px;width:100%;max-width:560px;max-height:90vh;overflow-y:auto;box-shadow:0 24px 80px rgba(0,0,0,.6)}
.modal-title{font-family:var(--font-display);font-weight:700;font-size:20px;margin-bottom:20px;letter-spacing:-.5px}
.form-group{margin-bottom:14px}
.form-label{display:block;font-size:12px;font-weight:700;color:var(--text2);margin-bottom:6px;letter-spacing:.2px;text-transform:uppercase}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-hint{font-size:11px;color:var(--text3);margin-top:4px}
/* ── CHAT ── */
.chat-layout{display:grid;grid-template-columns:260px 1fr;height:calc(100vh - 105px);background:var(--surface);border-radius:16px;border:1px solid var(--border);overflow:hidden}
.conv-list{border-right:1px solid var(--border);overflow-y:auto;display:flex;flex-direction:column}
.conv-header{padding:16px 18px;border-bottom:1px solid var(--border);font-family:var(--font-display);font-weight:700;font-size:14px;flex-shrink:0}
.conv-item{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--border);transition:.15s}
.conv-item:hover,.conv-item.active-conv{background:var(--surface2)}
.conv-avatar{width:36px;height:36px;border-radius:10px;background:var(--accent2-dim);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex-shrink:0;color:var(--accent2)}
.chat-messages{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.bubble-wrap{display:flex;flex-direction:column}
.bubble-wrap.mine{align-items:flex-end}
.bubble{padding:10px 14px;border-radius:14px;font-size:13.5px;line-height:1.55;max-width:72%;word-break:break-word}
.bubble.mine{background:var(--accent);color:#000;border-bottom-right-radius:4px}
.bubble.theirs{background:var(--surface3);border-bottom-left-radius:4px}
.bubble-time{font-size:10px;color:var(--text3);margin-top:3px;padding:0 2px}
.chat-input-wrap{padding:14px 18px;border-top:1px solid var(--border);display:flex;gap:10px;flex-shrink:0}
/* ── WALLET ── */
.wallet-hero{background:linear-gradient(135deg,#061422,#0a2040,#0d2d5a);border-radius:20px;padding:28px;margin-bottom:20px;position:relative;overflow:hidden;border:1px solid rgba(0,229,160,.15)}
.wallet-hero::before{content:'';position:absolute;top:-60px;right:-60px;width:250px;height:250px;border-radius:50%;background:radial-gradient(circle,rgba(0,229,160,.15),transparent 70%)}
[data-theme="light"] .wallet-hero{background:linear-gradient(135deg,#0f2840,#1a3d6e,#0e2d55)}
.wallet-balance-label{font-size:11px;color:rgba(255,255,255,.5);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;font-weight:600}
.wallet-balance-amount{font-family:var(--font-display);font-size:46px;font-weight:700;color:#fff;letter-spacing:-2px;line-height:1}
.wallet-sub{font-size:13px;color:rgba(255,255,255,.4);margin-top:8px}
.wallet-btns{display:flex;gap:10px;margin-top:22px}
.wallet-btn{flex:1;background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.2);border-radius:12px;padding:12px;font-weight:600;font-size:13px;backdrop-filter:blur(10px)}
.wallet-btn:hover{background:rgba(255,255,255,.2)}
/* ── PROGRESS ── */
.progress-bar{height:5px;background:var(--surface3);border-radius:3px;overflow:hidden;margin-top:6px}
.progress-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .4s}
/* ── AUTH ── */
.auth-bg{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);padding:20px;position:relative;overflow:hidden}
.auth-bg::before{content:'';position:absolute;width:500px;height:500px;border-radius:50%;background:radial-gradient(circle,var(--accent-dim),transparent 70%);top:-200px;right:-100px}
.auth-bg::after{content:'';position:absolute;width:400px;height:400px;border-radius:50%;background:radial-gradient(circle,var(--accent2-dim),transparent 70%);bottom:-100px;left:-100px}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:22px;padding:40px;width:100%;max-width:420px;position:relative;z-index:1;box-shadow:var(--card-shadow)}
.auth-logo{text-align:center;margin-bottom:28px}
.auth-logo-mark{width:54px;height:54px;background:var(--accent);border-radius:15px;display:inline-flex;align-items:center;justify-content:center;font-family:var(--font-display);font-weight:700;font-size:22px;color:#000;margin-bottom:14px}
.auth-logo h1{font-family:var(--font-display);font-size:26px;font-weight:700;letter-spacing:-.5px}
.auth-tabs{display:flex;background:var(--surface2);border-radius:12px;padding:4px;margin-bottom:22px;border:1px solid var(--border)}
.auth-tab{flex:1;padding:9px;border-radius:9px;text-align:center;cursor:pointer;font-weight:700;font-size:13px;color:var(--text3);transition:.15s}
.auth-tab.active{background:var(--accent);color:#000}
.google-btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:12px;background:var(--surface2);border:1.5px solid var(--border);color:var(--text);border-radius:11px;font-weight:600;font-size:13.5px;cursor:pointer;transition:.2s;margin-bottom:16px}
.google-btn:hover{border-color:var(--accent);background:var(--accent-dim)}
.divider-text{display:flex;align-items:center;gap:12px;color:var(--text3);font-size:12px;margin-bottom:16px;font-weight:600}
.divider-text::before,.divider-text::after{content:'';flex:1;height:1px;background:var(--border)}
/* ── ACTIVATION GATE ── */
.activate-gate{background:var(--surface);border:1.5px solid var(--gold);border-radius:18px;padding:32px;text-align:center;max-width:480px;margin:0 auto 24px}
.gate-icon{font-size:44px;margin-bottom:14px}
.activate-gate h2{font-family:var(--font-display);font-weight:700;font-size:22px;margin-bottom:8px}
.fee-display{background:var(--gold-dim);border:1px solid var(--gold);border-radius:12px;padding:14px 24px;font-family:var(--font-display);font-size:30px;font-weight:700;color:var(--gold);margin-bottom:18px}
/* ── REFERRAL ── */
.referral-box{background:var(--accent-dim);border:1.5px solid var(--accent);border-radius:14px;padding:18px}
.referral-link{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:12px;color:var(--accent2);word-break:break-all;margin:10px 0;font-family:monospace}
/* ── ALERTS ── */
.alert-banner{padding:12px 24px;display:flex;align-items:center;gap:12px;font-size:13.5px;font-weight:500;border-bottom:1px solid transparent}
.alert-banner.info{background:var(--accent2-dim);border-color:var(--accent2);color:var(--accent2)}
.alert-banner.warning{background:var(--gold-dim);border-color:var(--gold);color:var(--gold)}
.alert-banner.danger{background:var(--red-dim);border-color:var(--red);color:var(--red)}
.alert-banner.success{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
/* ── AI DETECTION ── */
.ai-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700}
.ai-human{background:var(--accent-dim);color:var(--accent)}
.ai-mixed{background:var(--gold-dim);color:var(--gold)}
.ai-ai{background:var(--red-dim);color:var(--red)}
/* ── DAILY BAR ── */
.daily-bar{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin-bottom:18px;display:flex;align-items:center;gap:14px}
.daily-info{flex:1}
.daily-label{font-size:11px;color:var(--text3);font-weight:700;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.daily-count{font-family:var(--font-display);font-size:18px;font-weight:700}
/* ── TOAST ── */
.toast-wrap{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;min-width:290px;font-size:13.5px;display:flex;align-items:center;gap:10px;box-shadow:0 8px 32px rgba(0,0,0,.4);pointer-events:auto;animation:toastIn .25s ease;font-weight:500}
.toast.success{border-left:3px solid var(--accent)}
.toast.error{border-left:3px solid var(--red)}
.toast.info{border-left:3px solid var(--accent2)}
.toast.warn{border-left:3px solid var(--gold)}
@keyframes toastIn{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
/* ── MISC ── */
.empty-state{text-align:center;padding:50px 20px;color:var(--text3)}
.empty-icon{font-size:40px;margin-bottom:12px}
.empty-state h3{font-family:var(--font-display);font-size:17px;font-weight:700;margin-bottom:5px;color:var(--text2)}
.spinner{width:32px;height:32px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;margin:50px auto}
@keyframes spin{to{transform:rotate(360deg)}}
.tabs{display:flex;gap:0;border-bottom:1.5px solid var(--border);margin-bottom:20px}
.tab{padding:10px 20px;cursor:pointer;font-size:13.5px;color:var(--text3);border-bottom:2.5px solid transparent;margin-bottom:-2px;transition:.15s;font-weight:600}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.divider-h{height:1px;background:var(--border);margin:16px 0}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.copy-btn{background:var(--surface3);border:1px solid var(--border);color:var(--text2);border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
.copy-btn:hover{border-color:var(--accent);color:var(--accent)}
/* ── PDF UPLOAD ── */
.pdf-drop{border:2px dashed var(--border);border-radius:12px;padding:28px;text-align:center;cursor:pointer;transition:.2s}
.pdf-drop:hover,.pdf-drop.drag-over{border-color:var(--accent);background:var(--accent-dim)}
.pdf-preview{background:var(--surface3);border:1px solid var(--border);border-radius:10px;padding:12px 16px;display:flex;align-items:center;gap:12px;margin-top:10px}
/* ── SHARES CHART ── */
.chart-container{position:relative;height:280px;width:100%}
/* ── SUSPENSION BANNER ── */
.suspended-banner{background:var(--red-dim);border:1.5px solid var(--red);border-radius:14px;padding:20px;text-align:center;margin-bottom:20px}
/* ── RESPONSIVE ── */
@media(max-width:900px){
  .app{grid-template-columns:58px 1fr}
  .logo-text,.logo-sub,.sidebar-bottom .user-info-wrap,.nav-section{display:none}
  .nav-item span:last-child{display:none}
  .two-col,.three-col,.form-row,.settings-grid{grid-template-columns:1fr}
  .tasks-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:1fr 1fr}
  .chat-layout{grid-template-columns:54px 1fr}
  .conv-info,.conv-header{display:none}
}
@media(max-width:600px){
  .content{padding:14px}
  .stats-grid{grid-template-columns:1fr}
  .wallet-balance-amount{font-size:36px}
  .modal{padding:22px}
}
</style>
</head>
<body>

<div class="toast-wrap" id="toasts"></div>

<!-- AUTH -->
<div id="authScreen" style="display:none">
  <div class="auth-bg">
    <div class="auth-card">
      <div class="auth-logo">
        <div class="auth-logo-mark">D</div>
        <h1>DTIP</h1>
        <p style="color:var(--text3);font-size:13.5px;margin-top:5px">Kenya's Digital Earning Platform</p>
      </div>
      <div class="auth-tabs">
        <div class="auth-tab active" id="tabLogin" onclick="authTab('login')">Sign In</div>
        <div class="auth-tab" id="tabReg" onclick="authTab('register')">Register</div>
      </div>
      <button class="google-btn" onclick="googleAuth()">
        <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.707c-.18-.54-.282-1.117-.282-1.707s.102-1.167.282-1.707V4.961H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.039l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.961L3.964 6.293C4.672 4.166 6.656 3.58 9 3.58z"/></svg>
        Continue with Google
      </button>
      <div class="divider-text">or continue with email</div>
      <div id="loginForm">
        <div class="form-group"><label class="form-label">Email</label><input type="email" id="lEmail" placeholder="you@example.com" onkeypress="if(event.key==='Enter')doLogin()"></div>
        <div class="form-group"><label class="form-label">Password</label><input type="password" id="lPass" placeholder="••••••••" onkeypress="if(event.key==='Enter')doLogin()"></div>
        <button class="btn" style="width:100%;margin-top:4px" onclick="doLogin()">Sign In</button>
      </div>
      <div id="registerForm" style="display:none">
        <div class="form-row">
          <div class="form-group"><label class="form-label">Username</label><input type="text" id="rUser" placeholder="johndoe"></div>
          <div class="form-group"><label class="form-label">Email</label><input type="email" id="rEmail" placeholder="you@example.com"></div>
        </div>
        <div class="form-group"><label class="form-label">Password</label><input type="password" id="rPass" placeholder="Min 8 characters"></div>
        <div class="form-group"><label class="form-label">Referral Code (optional)</label><input type="text" id="rRef" placeholder="Enter referral code"></div>
        <button class="btn" style="width:100%;margin-top:4px" onclick="doRegister()">Create Account</button>
      </div>
    </div>
  </div>
</div>

<!-- MAIN APP -->
<div id="appScreen" class="app" style="display:none">
  <aside class="sidebar">
    <div class="logo-wrap">
      <div class="logo">
        <div class="logo-mark">D</div>
        <div><div class="logo-text">DTIP</div><div class="logo-sub">Earn Online</div></div>
      </div>
    </div>
    <nav class="nav">
      <div class="nav-item active" id="nav-home" onclick="go('home')"><div class="nav-icon">🏠</div><span>Dashboard</span></div>
      <div class="nav-item" id="nav-tasks" onclick="go('tasks')"><div class="nav-icon">📋</div><span>Tasks</span></div>
      <div class="nav-item" id="nav-myWork" onclick="go('myWork')"><div class="nav-icon">📤</div><span>My Submissions</span></div>
      <div class="nav-item" id="nav-wallet" onclick="go('wallet')"><div class="nav-icon">💰</div><span>Wallet</span></div>
      <div class="nav-item" id="nav-shares" onclick="go('shares')"><div class="nav-icon">📈</div><span>Shares</span></div>
      <div class="nav-item" id="nav-referrals" onclick="go('referrals')"><div class="nav-icon">🔗</div><span>Referrals</span></div>
      <div class="nav-item" id="nav-messages" onclick="go('messages')"><div class="nav-icon">💬</div><span>Messages</span><span class="nav-badge" id="msgBadge" style="display:none">0</span></div>
      <div class="nav-item" id="nav-notifications" onclick="go('notifications')"><div class="nav-icon">🔔</div><span>Alerts</span><span class="nav-badge" id="notifBadge" style="display:none">0</span></div>
      <div id="adminNav" style="display:none">
        <div class="nav-section">Admin / Mod</div>
        <div class="nav-item" id="nav-review" onclick="go('review')"><div class="nav-icon">📝</div><span>Review Queue</span><span class="nav-badge" id="reviewBadge" style="display:none">0</span></div>
        <div class="nav-item" id="nav-admin" onclick="go('admin')"><div class="nav-icon">⚙️</div><span>Admin Panel</span></div>
        <div class="nav-item" id="nav-adminUsers" onclick="go('adminUsers')"><div class="nav-icon">👥</div><span>Users</span></div>
        <div class="nav-item" id="nav-adminAlerts" onclick="go('adminAlerts')"><div class="nav-icon">📢</div><span>Alerts</span></div>
        <div class="nav-item" id="nav-adminSettings" onclick="go('adminSettings')"><div class="nav-icon">🛠</div><span>Settings</span></div>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <div class="user-card">
        <div class="user-avatar" id="sbAvatar"></div>
        <div class="user-info-wrap">
          <div class="user-name" id="sbName">—</div>
          <div class="user-tier" id="sbTier">—</div>
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn-outline btn-sm" style="flex:1" onclick="toggleTheme()">🌙</button>
        <button class="btn-outline btn-sm" style="flex:1" onclick="logout()">Sign Out</button>
      </div>
    </div>
  </aside>

  <div class="main">
    <div id="alertBanners"></div>
    <header class="topbar">
      <div class="page-title" id="pageTitle">Dashboard</div>
      <div class="topbar-right">
        <button class="icon-btn" onclick="go('notifications')" id="notifBtn">🔔<span class="dot" id="notifDot" style="display:none"></span></button>
        <button class="icon-btn" onclick="toggleTheme()">☀️</button>
      </div>
    </header>
    <div class="content" id="pageContent"><div class="spinner"></div></div>
  </div>
</div>

<!-- ═══════════════ MODALS ═══════════════ -->

<!-- Task Submit Modal -->
<div class="overlay" id="doTaskModal">
  <div class="modal" style="max-width:620px">
    <div class="modal-title">📤 Submit Task</div>
    <input type="hidden" id="doTaskId">
    <div id="doTaskInfo" style="margin-bottom:18px"></div>
    <div class="form-group">
      <label class="form-label">Proof / Notes (optional)</label>
      <textarea id="doTaskProof" rows="3" placeholder="Add any notes, links, or description of your work..."></textarea>
      <div class="form-hint">Write at least 50 characters for AI detection to work properly</div>
    </div>
    <div class="form-group">
      <label class="form-label">Upload PDF Result <span id="pdfRequired" style="color:var(--red)">(required)</span></label>
      <div class="pdf-drop" id="pdfDrop" onclick="document.getElementById('pdfInput').click()">
        <div style="font-size:28px;margin-bottom:8px">📄</div>
        <div style="font-weight:600;margin-bottom:4px">Click or drag a PDF here</div>
        <div style="font-size:12px;color:var(--text3)">Maximum 16MB</div>
      </div>
      <input type="file" id="pdfInput" accept=".pdf" style="display:none" onchange="handlePdfSelect(this)">
      <div id="pdfPreview" style="display:none" class="pdf-preview">
        <span style="font-size:22px">📄</span>
        <div>
          <div id="pdfName" style="font-weight:600;font-size:13px"></div>
          <div id="pdfSize" style="font-size:11px;color:var(--text3)"></div>
        </div>
        <button class="btn-outline btn-xs" style="margin-left:auto" onclick="clearPdf()">✕</button>
      </div>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:6px">
      <button class="btn-outline" onclick="closeModal('doTaskModal')">Cancel</button>
      <button class="btn" onclick="submitDoTask()" id="submitTaskBtn">Submit for Review</button>
    </div>
  </div>
</div>

<!-- Deposit Modal -->
<div class="overlay" id="depositModal">
  <div class="modal">
    <div class="modal-title">💳 Deposit via M-Pesa</div>
    <div id="demoNoteDep" style="background:var(--gold-dim);border:1px solid var(--gold);border-radius:10px;padding:12px;font-size:13px;color:var(--gold);margin-bottom:16px;display:none">⚡ Demo Mode — funds credited instantly</div>
    <div class="form-group"><label class="form-label">Amount (KES)</label><input type="number" id="depAmt" placeholder="Min. KES 10" min="10"></div>
    <div class="form-group"><label class="form-label">M-Pesa Phone</label><input type="tel" id="depPhone" placeholder="07XXXXXXXX"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:6px">
      <button class="btn-outline" onclick="closeModal('depositModal')">Cancel</button>
      <button class="btn" onclick="doDeposit()">Deposit Now</button>
    </div>
  </div>
</div>

<!-- Withdraw Modal -->
<div class="overlay" id="withdrawModal">
  <div class="modal">
    <div class="modal-title">💸 Withdraw to M-Pesa</div>
    <div class="form-group"><label class="form-label">Amount (KES)</label><input type="number" id="witAmt" placeholder="Min. KES 50" min="50"></div>
    <div class="form-group"><label class="form-label">M-Pesa Phone</label><input type="tel" id="witPhone" placeholder="07XXXXXXXX"></div>
    <p class="form-hint" style="margin-bottom:16px" id="witFeeInfo">A withdrawal fee applies. Min: KES 50</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('withdrawModal')">Cancel</button>
      <button class="btn" onclick="doWithdraw()">Withdraw</button>
    </div>
  </div>
</div>

<!-- Activate Modal -->
<div class="overlay" id="activateModal">
  <div class="modal">
    <div class="modal-title">🔓 Activate Your Account</div>
    <div style="text-align:center;margin-bottom:18px">
      <div style="font-size:12px;color:var(--text2);margin-bottom:8px">One-time Activation Fee</div>
      <div style="font-family:var(--font-display);font-size:38px;font-weight:700;color:var(--gold)" id="activateFeeAmt">KES 299</div>
    </div>
    <div class="form-group"><label class="form-label">M-Pesa Phone Number</label><input type="tel" id="actPhone" placeholder="07XXXXXXXX"></div>
    <p class="form-hint" style="margin-bottom:16px">Pay once to unlock task earning. Refer a friend and earn when they activate!</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('activateModal')">Cancel</button>
      <button class="btn-gold" onclick="doActivate()">Pay & Activate</button>
    </div>
  </div>
</div>

<!-- Premium Modal -->
<div class="overlay" id="premiumModal">
  <div class="modal">
    <div class="modal-title">⭐ Upgrade to Premium</div>
    <div style="text-align:center;margin-bottom:18px">
      <div style="font-size:12px;color:var(--text2);margin-bottom:8px">Monthly Premium Fee</div>
      <div style="font-family:var(--font-display);font-size:38px;font-weight:700;color:var(--accent2)" id="premiumFeeAmt">KES 499</div>
      <div style="font-size:12px;color:var(--text3);margin-top:4px">30 days access</div>
    </div>
    <div style="background:var(--accent2-dim);border:1px solid var(--border2);border-radius:12px;padding:14px;margin-bottom:16px;font-size:13px;color:var(--text2);line-height:1.8">
      ✅ Up to <strong id="premLimit">10</strong> tasks/day &nbsp;·&nbsp; ✅ Priority review &nbsp;·&nbsp; ✅ Premium badge &nbsp;·&nbsp; ✅ Higher earning
    </div>
    <div class="form-group"><label class="form-label">M-Pesa Phone Number</label><input type="tel" id="premPhone" placeholder="07XXXXXXXX"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('premiumModal')">Cancel</button>
      <button class="btn-v2" onclick="doUpgradePremium()">Upgrade Now</button>
    </div>
  </div>
</div>

<!-- New Task Modal -->
<div class="overlay" id="newTaskModal">
  <div class="modal" style="max-width:660px">
    <div class="modal-title">➕ Create New Task</div>
    <div class="form-group"><label class="form-label">Task Title</label><input type="text" id="ntTitle" placeholder="Clear, action-oriented title"></div>
    <div class="form-group"><label class="form-label">Brief Description</label><textarea id="ntDesc" rows="2" placeholder="Short overview (shown in task list)..."></textarea></div>
    <div class="form-group"><label class="form-label">Detailed Instructions (shown when task is opened)</label><textarea id="ntInstructions" rows="5" placeholder="Step-by-step instructions for completing this task. Be specific about what to submit in the PDF..."></textarea></div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">Category</label>
        <select id="ntCat">
          <option>Data Entry</option><option>Writing</option><option>Design</option>
          <option>Social Media</option><option>Research</option><option>Tech</option>
          <option>Marketing</option><option>Survey</option><option>Other</option>
        </select>
      </div>
      <div class="form-group"><label class="form-label">Reward (KES)</label><input type="number" id="ntReward" placeholder="50" min="1"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">Deadline (optional)</label><input type="datetime-local" id="ntDeadline"></div>
      <div class="form-group" style="display:flex;align-items:center;gap:10px;padding-top:22px">
        <input type="checkbox" id="ntRequiresPdf" checked style="width:auto;border-radius:4px">
        <label for="ntRequiresPdf" style="font-size:13px;cursor:pointer">Require PDF submission</label>
      </div>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:6px">
      <button class="btn-outline" onclick="closeModal('newTaskModal')">Cancel</button>
      <button class="btn" onclick="submitNewTask()">Publish Task</button>
    </div>
  </div>
</div>

<!-- Broadcast Modal -->
<div class="overlay" id="broadcastModal">
  <div class="modal">
    <div class="modal-title">📢 Broadcast to All Users</div>
    <div class="form-group"><label class="form-label">Message</label><textarea id="bcastMsg" rows="4" placeholder="Your message to all users..."></textarea></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('broadcastModal')">Cancel</button>
      <button class="btn" onclick="sendBroadcast()">Send to Everyone</button>
    </div>
  </div>
</div>

<!-- Buy Shares Modal -->
<div class="overlay" id="buySharesModal">
  <div class="modal">
    <div class="modal-title">📈 Buy Platform Shares</div>
    <div id="sharePriceInfo" style="text-align:center;margin-bottom:18px"></div>
    <div class="form-group"><label class="form-label">Number of Shares</label><input type="number" id="shareQty" value="1" min="1" oninput="updateShareTotal()"></div>
    <div id="shareTotalBox" style="background:var(--accent-dim);border:1px solid var(--accent);border-radius:10px;padding:14px;text-align:center;margin-bottom:14px;font-family:var(--font-display);font-size:20px;font-weight:700;color:var(--accent)"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('buySharesModal')">Cancel</button>
      <button class="btn" onclick="doBuyShares()">Buy Shares</button>
    </div>
  </div>
</div>

<!-- Suspend User Modal -->
<div class="overlay" id="suspendModal">
  <div class="modal">
    <div class="modal-title">⛔ Suspend Account</div>
    <input type="hidden" id="suspendUid">
    <div class="form-group"><label class="form-label">Reason for Suspension</label><textarea id="suspendReason" rows="3" placeholder="Explain why this account is being suspended..."></textarea></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('suspendModal')">Cancel</button>
      <button class="btn-danger" onclick="doSuspend()">Suspend Account</button>
    </div>
  </div>
</div>

<!-- Set Role Modal -->
<div class="overlay" id="roleModal">
  <div class="modal">
    <div class="modal-title">👤 Set User Role</div>
    <input type="hidden" id="roleUid">
    <div class="form-group">
      <label class="form-label">New Role</label>
      <select id="roleSelect">
        <option value="member">Member (Regular user)</option>
        <option value="moderator">Moderator (Can review tasks)</option>
        <option value="admin">Admin (Full access)</option>
      </select>
    </div>
    <p class="form-hint" style="margin-bottom:14px">⚠️ Moderators can approve/reject task submissions and view all users.</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('roleModal')">Cancel</button>
      <button class="btn" onclick="doSetRole()">Update Role</button>
    </div>
  </div>
</div>

<!-- Create Alert Modal -->
<div class="overlay" id="alertModal">
  <div class="modal">
    <div class="modal-title">📢 Create Alert</div>
    <div class="form-group"><label class="form-label">Alert Title</label><input type="text" id="altTitle" placeholder="Alert headline"></div>
    <div class="form-group"><label class="form-label">Message</label><textarea id="altMsg" rows="3" placeholder="Alert message..."></textarea></div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">Type</label>
        <select id="altType">
          <option value="info">Info (Blue)</option>
          <option value="warning">Warning (Gold)</option>
          <option value="danger">Danger (Red)</option>
          <option value="success">Success (Green)</option>
        </select>
      </div>
      <div class="form-group"><label class="form-label">Expires in (hours, optional)</label><input type="number" id="altExpires" placeholder="e.g. 24"></div>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('alertModal')">Cancel</button>
      <button class="btn" onclick="doCreateAlert()">Publish Alert</button>
    </div>
  </div>
</div>

<!-- Rejection Modal -->
<div class="overlay" id="rejectModal">
  <div class="modal">
    <div class="modal-title">❌ Reject Submission</div>
    <input type="hidden" id="rejectCompId">
    <div class="form-group"><label class="form-label">Reason for Rejection</label><textarea id="rejectReason" rows="3" placeholder="Explain why this submission is rejected..."></textarea></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-outline" onclick="closeModal('rejectModal')">Cancel</button>
      <button class="btn-danger" onclick="doReject()">Reject</button>
    </div>
  </div>
</div>

<script>
// ── STATE ──
const S = {
  token: null, user: null, socket: null,
  page: 'home', unreadNotif: 0, unreadMsg: 0,
  activeChat: null, chatPartner: null,
  sharePrice: 100, settings: {}, pendingPdf: null
};

// ── BOOT ──
document.addEventListener('DOMContentLoaded', async () => {
  const params = new URLSearchParams(location.search);
  const urlToken = params.get('token');
  const urlError = params.get('error');
  const ref = params.get('ref');

  if (ref) localStorage.setItem('pending_ref', ref);

  // FIX: Show OAuth errors surfaced by the backend
  if (urlError) {
    const msgs = {
      oauth_failed: 'Google sign-in failed. Please try again.',
      oauth_token_failed: 'Could not exchange Google code for token.',
      oauth_no_email: 'Google account did not share an email address.',
      google_not_configured: 'Google sign-in is not configured on this server.',
    };
    // Clean the URL first, then show the error
    history.replaceState({}, '', '/');
    setTimeout(() => toast(msgs[urlError] || 'Sign-in failed: ' + urlError, 'error'), 300);
  }

  // FIX: Store the token BEFORE calling history.replaceState so it is in localStorage
  // even if the /api/auth/me call below happens to fail the first time.
  // Then clean the URL so the token is not visible or bookmarkable.
  if (urlToken) {
    localStorage.setItem('tok', urlToken);
    history.replaceState({}, '', '/');   // safe to clean now — token is already stored
  }

  S.token = localStorage.getItem('tok');
  const theme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);

  if (S.token) {
    try {
      const r = await api('/api/auth/me');
      S.user = r.user;
      showApp();
    } catch(err) {
      // Token is invalid or expired — clear it and show login screen
      localStorage.removeItem('tok');
      S.token = null;
      showAuth();
      if (urlToken) {
        // Only show the error if this was a fresh OAuth redirect, not just an expired session
        toast('Authentication failed. Please sign in again.', 'error');
      }
    }
  } else { showAuth(); }
});

// ── API ──
async function api(url, method = 'GET', body = null) {
  // Always read token fresh from S (which is loaded from localStorage at boot)
  const tok = S.token || localStorage.getItem('tok');
  if (tok && !S.token) S.token = tok;  // re-hydrate if somehow lost

  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (tok) opts.headers['Authorization'] = 'Bearer ' + tok;
  if (body) opts.body = JSON.stringify(body);

  let r;
  try {
    r = await fetch(url, opts);
  } catch (networkErr) {
    throw new Error('Network error — check your connection');
  }

  // Handle non-JSON responses (e.g. server 500 HTML pages) gracefully
  let d;
  try {
    d = await r.json();
  } catch {
    if (r.status === 401) {
      // Token rejected — clear it so next page load shows login
      localStorage.removeItem('tok');
      S.token = null;
      throw new Error('Session expired. Please sign in again.');
    }
    throw new Error(`Server error (${r.status})`);
  }

  if (!r.ok) {
    if (r.status === 401) {
      localStorage.removeItem('tok');
      S.token = null;
    }
    throw new Error(d.error || d.message || `Request failed (${r.status})`);
  }
  return d;
}

async function apiForm(url, formData) {
  const tok = S.token || localStorage.getItem('tok');
  if (tok && !S.token) S.token = tok;

  const opts = { method: 'POST', headers: {} };
  if (tok) opts.headers['Authorization'] = 'Bearer ' + tok;
  opts.body = formData;

  let r;
  try {
    r = await fetch(url, opts);
  } catch (networkErr) {
    throw new Error('Network error — check your connection');
  }

  let d;
  try {
    d = await r.json();
  } catch {
    throw new Error(`Server error (${r.status})`);
  }

  if (!r.ok) throw new Error(d.error || d.message || `Request failed (${r.status})`);
  return d;
}

function toast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️', warn: '⚠️' };
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${icons[type]||'🔔'}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtDate(d) { return d ? new Date(d).toLocaleDateString('en-KE') : '—'; }
function fmtTime(d) { return d ? new Date(d).toLocaleTimeString('en-KE', {hour:'2-digit',minute:'2-digit'}) : ''; }
function fmtDT(d) { return d ? fmtDate(d) + ' ' + fmtTime(d) : '—'; }

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── SETTINGS CACHE ──
async function loadSettings() {
  try {
    const r = await api('/api/settings/public');
    S.settings = r;
    S.sharePrice = parseFloat(r.share_price) || 100;
    return r;
  } catch { return {}; }
}

// ── AUTH ──
function showAuth() {
  document.getElementById('authScreen').style.display = 'block';
  document.getElementById('appScreen').style.display = 'none';
}
function showApp() {
  document.getElementById('authScreen').style.display = 'none';
  document.getElementById('appScreen').style.display = 'grid';
  initApp();
}
function authTab(t) {
  document.getElementById('loginForm').style.display = t === 'login' ? 'block' : 'none';
  document.getElementById('registerForm').style.display = t === 'register' ? 'block' : 'none';
  document.getElementById('tabLogin').className = 'auth-tab' + (t === 'login' ? ' active' : '');
  document.getElementById('tabReg').className = 'auth-tab' + (t === 'register' ? ' active' : '');
}
function googleAuth() { window.location.href = '/auth/google'; }

async function doLogin() {
  const btn = document.querySelector('#loginForm button.btn') || document.querySelector('#loginForm .btn');
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.textContent = 'Signing in…'; btn.disabled = true; }
  try {
    const r = await api('/api/auth/login', 'POST', {
      email: document.getElementById('lEmail').value,
      password: document.getElementById('lPass').value
    });
    // Store token FIRST — before showApp() triggers any api() calls
    S.token = r.token;
    S.user = r.user;
    localStorage.setItem('tok', r.token);
    showApp();
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    if (btn) { btn.textContent = origText; btn.disabled = false; }
  }
}

async function doRegister() {
  const ref = document.getElementById('rRef').value.trim() || localStorage.getItem('pending_ref') || '';
  try {
    const r = await api('/api/auth/register', 'POST', {
      username: document.getElementById('rUser').value.trim(),
      email: document.getElementById('rEmail').value.trim(),
      password: document.getElementById('rPass').value,
      ref_code: ref
    });
    // Store token FIRST before showApp() triggers api() calls
    S.token = r.token;
    S.user = r.user;
    localStorage.setItem('tok', r.token);
    localStorage.removeItem('pending_ref');
    showApp();
    toast('Welcome to DTIP! 🎉', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

function logout() {
  S.token = null;
  S.user = null;
  S.unreadMsg = 0;
  S.unreadNotif = 0;
  localStorage.removeItem('tok');
  if (S.socket) { try { S.socket.disconnect(); } catch(e){} S.socket = null; }
  showAuth();
}

// ── APP INIT ──
async function initApp() {
  const u = S.user;
  const av = document.getElementById('sbAvatar');
  if (u.avatar_url) av.innerHTML = `<img src="${u.avatar_url}">`;
  else av.textContent = u.username[0].toUpperCase();
  document.getElementById('sbName').textContent = u.username;
  document.getElementById('sbTier').textContent = u.role === 'admin' ? '👑 Admin' :
    u.role === 'moderator' ? '🛡 Moderator' :
    (u.is_premium ? '⭐ Premium' : '🆓 Free');
  if (u.role === 'admin' || u.role === 'moderator') document.getElementById('adminNav').style.display = 'block';
  await loadSettings();
  initSocket();
  loadNotifCount();
  loadAlertBanners();
  go('home');
}

function initSocket() {
  // FIX: add reconnection config and pass token for authentication
  S.socket = io({
    query: { token: S.token },
    reconnection: true,
    reconnectionAttempts: 10,
    reconnectionDelay: 1500,
    reconnectionDelayMax: 10000,
    timeout: 20000,
  });
  // Re-authenticate after reconnect (token may have been refreshed)
  S.socket.on('reconnect', () => {
    S.socket.io.opts.query = { token: S.token };
  });
  S.socket.on('connect_error', (err) => {
    console.warn('Socket connect error:', err.message);
  });
  S.socket.on('receive_message', msg => {
    if (S.page === 'messages' && S.activeChat === msg.sender_id) renderMsg(msg, false);
    else { S.unreadMsg++; updateBadges(); toast(`💬 ${msg.sender_name}: ${msg.message.slice(0,50)}`, 'info'); }
  });
  S.socket.on('broadcast', msg => { toast('📢 ' + msg.message.slice(0,60), 'info'); loadAlertBanners(); });
  S.socket.on('notification', n => { S.unreadNotif++; updateBadges(); toast(n.title, n.type || 'info'); });
  S.socket.on('new_task', t => { toast(`🆕 New task: ${t.title} — KES ${t.reward}`, 'success'); });
  S.socket.on('new_alert', a => { loadAlertBanners(); toast(`📢 ${a.title}`, a.type === 'danger' ? 'error' : a.type === 'warning' ? 'warn' : 'info'); });
  S.socket.on('ai_alert', data => {
    if (S.user.role === 'admin' || S.user.role === 'moderator') {
      toast(`🤖 AI detected: ${data.username} — ${data.score}% AI (submission #${data.completion_id})`, 'warn');
      // Update review badge
      const rb = document.getElementById('reviewBadge');
      if (rb) { const c = parseInt(rb.textContent||0)+1; rb.textContent=c; rb.style.display='block'; }
    }
  });
  S.socket.on('settings_update', settings => {
    // Live-update all displayed settings
    S.settings = {...S.settings, ...settings};
    S.sharePrice = parseFloat(settings.share_price) || S.sharePrice;
    updateDisplayedSettings(settings);
  });
  S.socket.on('shares_update', () => { if (S.page === 'shares') shares(); });
}

function updateDisplayedSettings(settings) {
  // Update all dynamic value displays throughout the UI
  if (settings.activation_fee) {
    document.querySelectorAll('[data-setting="activation_fee"]').forEach(el => {
      el.textContent = `KES ${parseFloat(settings.activation_fee).toFixed(0)}`;
    });
    const afd = document.getElementById('activateFeeAmt');
    if (afd) afd.textContent = `KES ${parseFloat(settings.activation_fee).toFixed(0)}`;
  }
  if (settings.premium_fee) {
    const pfd = document.getElementById('premiumFeeAmt');
    if (pfd) pfd.textContent = `KES ${parseFloat(settings.premium_fee).toFixed(0)}`;
  }
  if (settings.withdrawal_fee_pct) {
    const wfi = document.getElementById('witFeeInfo');
    if (wfi) wfi.textContent = `A ${parseFloat(settings.withdrawal_fee_pct).toFixed(1)}% fee applies. Min: KES 50`;
  }
  if (settings.premium_daily_limit) {
    const pl = document.getElementById('premLimit');
    if (pl) pl.textContent = settings.premium_daily_limit;
  }
  // Refresh current page if it shows settings-derived content
  if (S.page === 'home' || S.page === 'adminSettings' || S.page === 'shares') {
    setTimeout(() => go(S.page), 100);
  }
}

function updateBadges() {
  // FIX: guard against elements not yet in DOM and NaN counts
  const mb = document.getElementById('msgBadge');
  const nb = document.getElementById('notifBadge');
  const nd = document.getElementById('notifDot');
  const msgs = Math.max(0, S.unreadMsg || 0);
  const notifs = Math.max(0, S.unreadNotif || 0);
  if (mb) { mb.style.display = msgs > 0 ? 'block' : 'none'; mb.textContent = msgs; }
  if (nb) { nb.style.display = notifs > 0 ? 'block' : 'none'; nb.textContent = notifs; }
  if (nd) { nd.style.display = notifs > 0 ? 'block' : 'none'; }
}

async function loadNotifCount() {
  try { const r = await api('/api/notifications'); S.unreadNotif = r.unread; updateBadges(); } catch {}
}

async function loadAlertBanners() {
  try {
    const r = await api('/api/alerts');
    const container = document.getElementById('alertBanners');
    if (!container) return;
    const iconMap = {info:'ℹ️', warning:'⚠️', danger:'🚨', success:'✅'};
    container.innerHTML = r.alerts.map(a => `
      <div class="alert-banner ${a.type}" id="alert-${a.id}">
        <span>${iconMap[a.type]||'📢'}</span>
        <strong>${esc(a.title)}:</strong> ${esc(a.message)}
        <button class="btn-outline btn-xs" style="margin-left:auto;border-color:currentColor;color:currentColor" onclick="document.getElementById('alert-${a.id}').remove()">✕</button>
      </div>`).join('');
  } catch {}
}

// ── NAVIGATION ──
function go(page) {
  S.page = page;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const el = document.getElementById('nav-' + page);
  if (el) el.classList.add('active');
  const titles = {
    home:'Dashboard', tasks:'Browse Tasks', myWork:'My Submissions', wallet:'Wallet',
    shares:'Platform Shares', referrals:'Referral Program', messages:'Messages',
    notifications:'Notifications', review:'Review Queue', admin:'Admin Panel',
    adminUsers:'User Management', adminAlerts:'Alert Center', adminSettings:'Platform Settings'
  };
  document.getElementById('pageTitle').textContent = titles[page] || page;
  document.getElementById('pageContent').innerHTML = '<div class="spinner"></div>';
  const pages = {home, tasks, myWork, wallet, shares, referrals, messages, notifications,
                 review, admin, adminUsers, adminAlerts, adminSettings};
  pages[page]?.();
}

function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }

// ─────────────────────────────────────────
// PAGES
// ─────────────────────────────────────────

async function home() {
  const el = document.getElementById('pageContent');
  try {
    const [me, pub, sets] = await Promise.all([
      api('/api/auth/me'), api('/api/stats'), loadSettings()
    ]);
    S.user = me.user;
    const u = S.user, w = me.wallet;
    // FIX: daily counter — use tasks_done_today from the *freshly* fetched me response,
    // not from the potentially stale cached S.user.  Also use the authoritative daily_limit.
    const doneToday = typeof u.tasks_done_today === 'number' ? u.tasks_done_today : 0;
    const limit = typeof u.daily_limit === 'number' ? u.daily_limit : 3;
    const pct = Math.min((doneToday / limit) * 100, 100);
    const actFee = parseFloat(sets.activation_fee || 299).toFixed(0);

    let adminBlock = '';
    if (u.role === 'admin') {
      try {
        const st = await api('/api/admin/stats');
        adminBlock = `<div class="card" style="margin-top:18px">
          <div class="card-header"><div class="card-title">Platform Overview</div></div>
          <div class="card-body">
          <div class="stats-grid" style="margin-bottom:0">
            ${[
              ['👥','Total Users',st.total_users,'var(--accent2)'],
              ['🔓','Activated',st.activated_users,'var(--accent)'],
              ['⭐','Premium',st.premium_users,'var(--gold)'],
              ['📋','Active Tasks',st.active_tasks,'var(--accent)'],
              ['✅','Completions',st.total_completions,'var(--accent2)'],
              ['📥','Pending Review',st.pending_completions,'var(--orange)'],
              ['⛔','Suspended',st.suspended_users,'var(--red)'],
              ['💰','Platform KES',st.total_wallet.toLocaleString(),'var(--gold)'],
            ].map(([icon,label,val,col]) => `<div class="stat-card">
              <div class="stat-bg" style="background:${col}"></div>
              <div class="stat-icon">${icon}</div>
              <div class="stat-value" style="font-size:${String(val).length>6?'20px':'28px'}">${val}</div>
              <div class="stat-label">${label}</div>
            </div>`).join('')}
          </div></div></div>`;
      } catch {}
    }

    el.innerHTML = `
      ${u.is_suspended ? `<div class="suspended-banner">
        <div style="font-size:28px;margin-bottom:8px">⛔</div>
        <div style="font-family:var(--font-display);font-weight:700;font-size:18px">Account Suspended</div>
        <div style="color:var(--text2);margin-top:6px">${esc(u.suspension_reason||'Contact admin for details')}</div>
      </div>` : ''}

      ${!u.is_activated && u.role === 'member' ? `
      <div class="activate-gate">
        <div class="gate-icon">🔒</div>
        <h2>Activate to Start Earning</h2>
        <p style="color:var(--text2);margin-bottom:18px">Pay a one-time fee to unlock task earning. Earn money by completing tasks!</p>
        <div class="fee-display" data-setting="activation_fee">KES ${actFee}</div>
        <button class="btn-gold" style="width:100%;font-size:15px;padding:14px" onclick="openActivate()">Pay Activation Fee & Start Earning</button>
      </div>` : ''}

      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-bg" style="background:var(--accent)"></div>
          <div class="stat-icon">💰</div>
          <div class="stat-value">KES ${(w?.balance||0).toFixed(0)}</div>
          <div class="stat-label">Wallet Balance</div>
        </div>
        <div class="stat-card">
          <div class="stat-bg" style="background:var(--accent2)"></div>
          <div class="stat-icon">✅</div>
          <div class="stat-value">${pub.completions}</div>
          <div class="stat-label">Platform Completions</div>
        </div>
        <div class="stat-card">
          <div class="stat-bg" style="background:var(--gold)"></div>
          <div class="stat-icon">📋</div>
          <div class="stat-value">${pub.tasks}</div>
          <div class="stat-label">Available Tasks</div>
        </div>
        <div class="stat-card">
          <div class="stat-bg" style="background:var(--red)"></div>
          <div class="stat-icon">🎯</div>
          <div class="stat-value">${(w?.total_earned||0).toFixed(0)}</div>
          <div class="stat-label">Total Earned (KES)</div>
        </div>
      </div>

      ${u.is_activated && u.role === 'member' ? `
      <div class="daily-bar">
        <div class="stat-icon" style="font-size:22px">🎯</div>
        <div class="daily-info">
          <div class="daily-label">Tasks Today</div>
          <div class="daily-count">${doneToday} / ${limit} tasks ${u.is_premium ? '<span class="badge badge-gold">⭐ Premium</span>' : ''}</div>
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
        </div>
        ${!u.is_premium ? `<button class="btn-v2 btn-sm" onclick="openPremium()">⭐ Upgrade</button>` : ''}
      </div>` : ''}

      <div class="two-col">
        <div class="card">
          <div class="card-header"><div class="card-title">Quick Actions</div></div>
          <div class="card-body" style="display:flex;flex-direction:column;gap:10px">
            <button class="btn" onclick="go('tasks')">📋 Browse & Do Tasks</button>
            <button class="btn-outline" onclick="go('wallet')">💰 Manage Wallet</button>
            <button class="btn-outline" onclick="go('referrals')">🔗 Share Referral Link</button>
            <button class="btn-outline" onclick="go('shares')">📈 Buy Platform Shares</button>
            ${!u.is_premium && u.is_activated && u.role === 'member' ? `<button class="btn-v2" onclick="openPremium()">⭐ Upgrade to Premium</button>` : ''}
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Account Status</div></div>
          <div class="card-body">
            <table>
              <tr><td style="color:var(--text3)">Username</td><td><strong>${esc(u.username)}</strong></td></tr>
              <tr><td style="color:var(--text3)">Role</td><td><span class="badge ${u.role==='admin'?'badge-gold':u.role==='moderator'?'badge-purple':'badge-gray'}">${u.role}</span></td></tr>
              <tr><td style="color:var(--text3)">Status</td><td>${u.is_activated?'<span class="badge badge-green">✅ Active</span>':'<span class="badge badge-gold">⚠️ Inactive</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Tier</td><td>${u.is_premium?'<span class="badge badge-gold">⭐ Premium</span>':'<span class="badge badge-gray">Free</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Verified</td><td>${u.is_verified?'<span class="badge badge-green">✔</span>':'<span class="badge badge-gray">No</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Total Earned</td><td><strong style="color:var(--accent)">KES ${(w?.total_earned||0).toFixed(0)}</strong></td></tr>
            </table>
          </div>
        </div>
      </div>
      ${adminBlock}`;
  } catch(e) { el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><h3>Error</h3><p>${e.message}</p></div>`; }
}

// ── TASKS PAGE ──
async function tasks() {
  const el = document.getElementById('pageContent');
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;flex-wrap:wrap">
      <input type="text" id="taskQ" placeholder="Search tasks..." style="max-width:260px" oninput="loadTasks()">
      <select id="taskCat" style="max-width:160px" onchange="loadTasks()">
        <option value="">All Categories</option>
        <option>Data Entry</option><option>Writing</option><option>Design</option>
        <option>Social Media</option><option>Research</option><option>Tech</option>
        <option>Marketing</option><option>Survey</option><option>Other</option>
      </select>
      ${S.user.role === 'admin' ? `<button class="btn" style="margin-left:auto" onclick="openModal('newTaskModal')">➕ Create Task</button>` : ''}
    </div>
    ${!S.user.is_activated && S.user.role === 'member' ? `
    <div style="background:var(--gold-dim);border:1.5px solid var(--gold);border-radius:12px;padding:16px;margin-bottom:18px;display:flex;align-items:center;gap:14px">
      <span style="font-size:22px">🔒</span>
      <div><strong>Activate to earn</strong> — Pay the one-time fee to complete tasks and earn money.</div>
      <button class="btn-gold btn-sm" style="margin-left:auto;white-space:nowrap" onclick="openActivate()">Activate</button>
    </div>` : ''}
    <div id="taskGrid" class="tasks-grid"></div>
    <div id="taskPager" style="text-align:center;margin-top:18px"></div>`;
  loadTasks();
}

async function loadTasks(page = 1) {
  const q = document.getElementById('taskQ')?.value || '';
  const cat = document.getElementById('taskCat')?.value || '';
  try {
    const r = await api(`/api/tasks?page=${page}&q=${encodeURIComponent(q)}&category=${encodeURIComponent(cat)}`);
    const grid = document.getElementById('taskGrid');
    if (!r.tasks.length) {
      grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">📭</div><h3>No Tasks Found</h3><p>Check back soon for new tasks.</p></div>';
      return;
    }
    const icons = {'Data Entry':'📊','Writing':'✍️','Design':'🎨','Social Media':'📱','Research':'🔬','Tech':'💻','Marketing':'📣','Survey':'📝','Other':'⚡'};
    grid.innerHTML = r.tasks.map(t => {
      const done = t.user_completion;
      return `<div class="task-card" onclick="openTask(${t.id})">
        <div class="task-icon-wrap">${icons[t.category]||'⚡'}</div>
        <div class="task-title">${esc(t.title)}</div>
        <div class="task-desc">${esc(t.description)}</div>
        <div class="task-footer">
          <div class="task-reward">KES ${t.reward.toLocaleString()}</div>
          <span class="task-cat-tag">${esc(t.category)}</span>
          ${t.requires_pdf ? '<span class="badge badge-purple" style="font-size:10px">📄 PDF</span>' : ''}
          ${done ? `<span class="badge badge-${done.status==='approved'?'green':done.status==='rejected'?'red':'orange'}" style="margin-left:auto">${done.status==='approved'?'✅':done.status==='rejected'?'❌':'⏳'} ${done.status}</span>` : ''}
          ${t.is_flagged ? `<span class="badge badge-red">🚩</span>` : ''}
        </div>
      </div>`;
    }).join('');
    const pager = document.getElementById('taskPager');
    pager.innerHTML = r.pages > 1 ? Array.from({length:r.pages},(_,i)=>
      `<button class="${i+1===page?'btn':'btn-outline'} btn-sm" style="margin:3px" onclick="loadTasks(${i+1})">${i+1}</button>`).join('') : '';
  } catch(e) { toast(e.message,'error'); }
}

async function openTask(id) {
  try {
    const r = await api('/api/tasks/' + id);
    const t = r.task;
    const done = t.user_completion;

    document.getElementById('doTaskId').value = id;
    document.getElementById('doTaskProof').value = '';
    clearPdf();

    const pdfReq = document.getElementById('pdfRequired');
    if (pdfReq) pdfReq.style.display = t.requires_pdf ? 'inline' : 'none';

    document.getElementById('doTaskInfo').innerHTML = `
      <div style="background:var(--surface2);border-radius:12px;padding:16px">
        <div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:12px">
          <div style="flex:1">
            <div style="font-family:var(--font-display);font-weight:700;font-size:17px;margin-bottom:6px">${esc(t.title)}</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
              <span class="task-reward">KES ${t.reward.toLocaleString()}</span>
              <span class="task-cat-tag">${esc(t.category)}</span>
              ${t.requires_pdf ? '<span class="badge badge-purple">📄 PDF required</span>' : ''}
              ${done ? `<span class="badge badge-${done.status==='approved'?'green':done.status==='rejected'?'red':'orange'}">${done.status}</span>` : ''}
            </div>
          </div>
        </div>
        ${t.deadline ? `<div style="font-size:12px;color:var(--gold);margin-bottom:10px">⏰ Deadline: ${fmtDT(t.deadline)}</div>` : ''}
        <div style="font-size:13px;color:var(--text2);line-height:1.7;margin-bottom:${t.instructions?'12px':'0'}">${esc(t.description)}</div>
        ${t.instructions ? `<div class="divider-h"></div><div style="font-size:12px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Instructions</div><div style="font-size:13px;color:var(--text2);line-height:1.75;white-space:pre-wrap">${esc(t.instructions)}</div>` : ''}
        ${done && done.status === 'rejected' && done.rejection_reason ? `<div style="background:var(--red-dim);border:1px solid var(--red);border-radius:8px;padding:10px 12px;margin-top:12px;font-size:12.5px;color:var(--red)">❌ Rejected: ${esc(done.rejection_reason)}</div>` : ''}
      </div>
      ${S.user.role === 'admin' ? `<div style="display:flex;gap:8px;margin-top:12px"><button class="btn-danger btn-sm" onclick="closeModal('doTaskModal');deactivateTask(${id})">Deactivate</button></div>` : ''}
    `;

    // If already done, show existing submission
    if (done && done.status !== 'rejected') {
      document.getElementById('doTaskProof').value = done.proof_text || '';
      document.getElementById('doTaskProof').disabled = true;
      document.getElementById('pdfDrop').style.display = 'none';
      document.getElementById('submitTaskBtn').disabled = true;
      document.getElementById('submitTaskBtn').textContent = done.status === 'approved' ? '✅ Approved' : '⏳ Under Review';
    } else {
      document.getElementById('doTaskProof').disabled = false;
      document.getElementById('pdfDrop').style.display = 'block';
      document.getElementById('submitTaskBtn').disabled = false;
      document.getElementById('submitTaskBtn').textContent = 'Submit for Review';
    }
    openModal('doTaskModal');
  } catch(e) { toast(e.message,'error'); }
}

// ── PDF HANDLING ──
function handlePdfSelect(input) {
  const file = input.files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) { toast('Only PDF files allowed','error'); return; }
  if (file.size > 16*1024*1024) { toast('File too large (max 16MB)','error'); return; }
  S.pendingPdf = file;
  document.getElementById('pdfPreview').style.display = 'flex';
  document.getElementById('pdfDrop').style.display = 'none';
  document.getElementById('pdfName').textContent = file.name;
  document.getElementById('pdfSize').textContent = (file.size/1024/1024).toFixed(2) + ' MB';
}

function clearPdf() {
  S.pendingPdf = null;
  document.getElementById('pdfInput').value = '';
  document.getElementById('pdfPreview').style.display = 'none';
  document.getElementById('pdfDrop').style.display = 'block';
}

// PDF Drop Zone
document.addEventListener('DOMContentLoaded', () => {
  const drop = document.getElementById('pdfDrop');
  if (!drop) return;
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) { document.getElementById('pdfInput').files = e.dataTransfer.files; handlePdfSelect(document.getElementById('pdfInput')); }
  });
});

async function submitDoTask() {
  const tid = document.getElementById('doTaskId').value;
  if (document.getElementById('submitTaskBtn').disabled) { closeModal('doTaskModal'); return; }
  const proof = document.getElementById('doTaskProof').value;

  const fd = new FormData();
  fd.append('proof_text', proof);
  if (S.pendingPdf) fd.append('pdf', S.pendingPdf);

  try {
    document.getElementById('submitTaskBtn').textContent = 'Submitting...';
    document.getElementById('submitTaskBtn').disabled = true;
    const r = await apiForm(`/api/tasks/${tid}/submit`, fd);
    closeModal('doTaskModal');
    clearPdf();
    toast('📤 Submitted for review! You\'ll be notified when approved.', 'success');
    loadTasks();
  } catch(e) {
    document.getElementById('submitTaskBtn').disabled = false;
    document.getElementById('submitTaskBtn').textContent = 'Submit for Review';
    if (e.message.includes('activate')) { closeModal('doTaskModal'); openActivate(); }
    else if (e.message.includes('daily_limit') || e.message.includes('Daily limit')) {
      toast(e.message, 'warn');
      setTimeout(() => openPremium(), 500);
    } else { toast(e.message, 'error'); }
  }
}

async function deactivateTask(id) {
  try { await api('/api/tasks/'+id,'DELETE'); toast('Task deactivated','info'); loadTasks(); }
  catch(e){toast(e.message,'error');}
}

async function submitNewTask() {
  try {
    await api('/api/tasks','POST',{
      title: document.getElementById('ntTitle').value,
      description: document.getElementById('ntDesc').value,
      instructions: document.getElementById('ntInstructions').value,
      category: document.getElementById('ntCat').value,
      reward: parseFloat(document.getElementById('ntReward').value),
      requires_pdf: document.getElementById('ntRequiresPdf').checked,
      deadline: document.getElementById('ntDeadline').value || null
    });
    closeModal('newTaskModal');
    toast('Task published!','success');
    if (S.page==='tasks') loadTasks();
  } catch(e){toast(e.message,'error');}
}

// ── MY SUBMISSIONS ──
async function myWork() {
  const el = document.getElementById('pageContent');
  try {
    const r = await api('/api/my/completions');
    const statusColor = {approved:'green',rejected:'red',pending:'orange'};
    const statusIcon = {approved:'✅',rejected:'❌',pending:'⏳'};
    el.innerHTML = r.completions.length ? `
      <div class="card">
        <div class="card-header"><div class="card-title">My Submissions (${r.completions.length})</div></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Task</th><th>Submitted</th><th>PDF</th><th>AI Check</th><th>Status</th><th>Notes</th></tr></thead>
            <tbody>${r.completions.map(c => `<tr>
              <td><strong>Task #${c.task_id}</strong></td>
              <td style="color:var(--text3)">${fmtDT(c.created_at)}</td>
              <td>${c.pdf_url ? `<a href="${c.pdf_url}" target="_blank" class="badge badge-purple">📄 View PDF</a>` : '<span style="color:var(--text3)">—</span>'}</td>
              <td>${c.ai_checked ? aiResultBadge(c.ai_score, c.ai_result) : '<span style="color:var(--text3)">—</span>'}</td>
              <td><span class="badge badge-${statusColor[c.status]||'gray'}">${statusIcon[c.status]||'?'} ${c.status}</span></td>
              <td style="color:var(--red);font-size:12px">${c.rejection_reason ? esc(c.rejection_reason) : ''}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>` :
      '<div class="empty-state"><div class="empty-icon">📤</div><h3>No Submissions Yet</h3><p>Complete a task to see your submissions here.</p></div>';
  } catch(e) { el.innerHTML = `<p style="color:var(--red)">${e.message}</p>`; }
}

function aiResultBadge(score, result) {
  if (!result) return '—';
  const cls = result === 'human' ? 'ai-human' : result === 'ai' ? 'ai-ai' : result === 'mixed' ? 'ai-mixed' : '';
  const icon = result === 'human' ? '🧠' : result === 'ai' ? '🤖' : '⚠️';
  return `<span class="ai-badge ${cls}">${icon} ${result} (${score||0}%)</span>`;
}

// ── WALLET PAGE ──
async function wallet() {
  const el = document.getElementById('pageContent');
  try {
    const [r, sets] = await Promise.all([api('/api/wallet'), loadSettings()]);
    const w = r.wallet;
    const feePct = parseFloat(sets.withdrawal_fee_pct || 5).toFixed(1);
    el.innerHTML = `
      <div class="wallet-hero">
        <div class="wallet-balance-label">Available Balance</div>
        <div class="wallet-balance-amount">KES ${w.balance.toFixed(2)}</div>
        <div class="wallet-sub">Escrow: KES ${w.escrow.toFixed(2)} · Total Earned: KES ${w.total_earned.toFixed(2)}</div>
        <div class="wallet-btns">
          <button class="wallet-btn" onclick="openDeposit()">⬆ Deposit</button>
          <button class="wallet-btn" onclick="openModal('withdrawModal')">⬇ Withdraw</button>
          <button class="wallet-btn" onclick="go('shares')">📈 Shares</button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Transaction History</div></div>
        <div class="card-body">
          ${r.ledger.length ? `<div class="tbl-wrap"><table>
            <thead><tr><th>Type</th><th>Amount</th><th>Balance</th><th>Description</th><th>Date</th></tr></thead>
            <tbody>${r.ledger.map(l=>`<tr>
              <td><span class="badge ${['deposit','referral','task_reward','bonus'].includes(l.type)?'badge-green':'badge-red'}">${l.type}</span></td>
              <td style="color:${l.amount>=0?'var(--accent)':'var(--red)'}"><strong>${l.amount>=0?'+':''}KES ${Math.abs(l.amount).toFixed(2)}</strong></td>
              <td>KES ${l.balance_after.toFixed(2)}</td>
              <td style="color:var(--text3);font-size:12px">${esc(l.description||'')}</td>
              <td style="color:var(--text3)">${fmtDate(l.created_at)}</td>
            </tr>`).join('')}</tbody>
          </table></div>` : '<div class="empty-state"><div class="empty-icon">📊</div><h3>No Transactions</h3></div>'}
        </div>
      </div>`;
    // Update fee info dynamically
    const wfi = document.getElementById('witFeeInfo');
    if (wfi) wfi.textContent = `A ${feePct}% fee applies. Min: KES 50`;
  } catch(e) { el.innerHTML = `<p style="color:var(--red)">${e.message}</p>`; }
}

function openDeposit() { document.getElementById('demoNoteDep').style.display = 'block'; openModal('depositModal'); }
async function doDeposit() {
  try {
    const r = await api('/api/wallet/deposit','POST',{
      amount: parseFloat(document.getElementById('depAmt').value),
      phone: document.getElementById('depPhone').value
    });
    closeModal('depositModal');
    toast(r.demo ? '✅ Demo deposit credited!' : 'Check your phone for STK push','success');
    wallet();
  } catch(e){toast(e.message,'error');}
}

async function doWithdraw() {
  try {
    await api('/api/wallet/withdraw','POST',{
      amount: parseFloat(document.getElementById('witAmt').value),
      phone: document.getElementById('witPhone').value
    });
    closeModal('withdrawModal');
    toast('Withdrawal initiated!','success');
    wallet();
  } catch(e){toast(e.message,'error');}
}

// ── SHARES PAGE ──
async function shares() {
  const el = document.getElementById('pageContent');
  try {
    const [sets, r, hist] = await Promise.all([loadSettings(), api('/api/shares'), api('/api/shares/history')]);
    const wallet = await api('/api/wallet');

    el.innerHTML = `
      <div class="two-col" style="margin-bottom:20px">
        <div style="background:linear-gradient(135deg,var(--accent2-dim),var(--accent-dim));border:1.5px solid var(--border2);border-radius:16px;padding:22px">
          <div style="font-size:12px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Your Portfolio</div>
          <div style="font-family:var(--font-display);font-size:36px;font-weight:700;margin-bottom:4px">${r.total_shares} <span style="font-size:16px;color:var(--text2)">shares</span></div>
          <div style="font-size:13px;color:var(--text3)">Value: <strong style="color:var(--accent)">KES ${r.portfolio_value.toLocaleString()}</strong></div>
          <div style="font-size:13px;color:var(--text3);margin-top:2px">Price: <strong>KES ${r.share_price} each</strong></div>
          <button class="btn" style="margin-top:16px" onclick="openBuyShares()">📈 Buy Shares</button>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Your Wallet</div></div>
          <div class="card-body">
            <div style="font-family:var(--font-display);font-size:28px;font-weight:700;color:var(--accent);margin-bottom:4px">KES ${wallet.wallet.balance.toFixed(0)}</div>
            <div style="color:var(--text3);font-size:13px;margin-bottom:14px">Available</div>
            <button class="btn-outline" onclick="openDeposit()">⬆ Add Funds</button>
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom:20px">
        <div class="card-header"><div class="card-title">📈 Share Activity</div></div>
        <div class="card-body">
          <div class="chart-container">
            <canvas id="sharesChart"></canvas>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><div class="card-title">Purchase History</div></div>
        <div class="card-body">
          ${r.shares.length ? `<div class="tbl-wrap"><table>
            <thead><tr><th>Shares</th><th>Price Each</th><th>Total Paid</th><th>Date</th></tr></thead>
            <tbody>${r.shares.map(s=>`<tr>
              <td><strong>${s.quantity}</strong></td>
              <td>KES ${s.price_each.toFixed(0)}</td>
              <td>KES ${s.total_paid.toFixed(0)}</td>
              <td style="color:var(--text3)">${fmtDate(s.purchased_at)}</td>
            </tr>`).join('')}</tbody>
          </table></div>` : '<div class="empty-state"><div class="empty-icon">📈</div><h3>No Shares Yet</h3><p>Buy your first shares!</p></div>'}
        </div>
      </div>`;

    // Render chart
    setTimeout(() => renderSharesChart(hist.history), 100);
  } catch(e){ el.innerHTML = `<p style="color:var(--red)">${e.message}</p>`; }
}

function renderSharesChart(history) {
  const ctx = document.getElementById('sharesChart');
  if (!ctx || !history.length) {
    if (ctx) ctx.parentElement.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><h3>No Data Yet</h3><p>Share purchases will appear here.</p></div>';
    return;
  }
  // FIX: destroy any existing Chart.js instance before creating a new one.
  // Without this, navigating away and back creates a new chart on top of the old
  // canvas, causing "Canvas is already in use" errors and visual glitches.
  if (ctx._chartInstance) {
    ctx._chartInstance.destroy();
    delete ctx._chartInstance;
  }
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const textCol = isDark ? '#8b93b8' : '#454870';
  const gridCol = isDark ? '#1e2540' : '#dde0f0';
  ctx._chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: history.map(d => d.date),
      datasets: [
        {
          label: 'Cumulative Shares',
          data: history.map(d => d.total),
          borderColor: '#00e5a0', backgroundColor: 'rgba(0,229,160,.1)',
          tension: 0.4, fill: true, pointRadius: 4, pointBackgroundColor: '#00e5a0'
        },
        {
          label: 'Daily Revenue (KES)',
          data: history.map(d => d.revenue),
          borderColor: '#7b6eff', backgroundColor: 'rgba(123,110,255,.08)',
          tension: 0.4, fill: true, pointRadius: 4, pointBackgroundColor: '#7b6eff',
          yAxisID: 'y2'
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: textCol, font: {family:'Space Grotesk'} } } },
      scales: {
        x: { ticks: { color: textCol }, grid: { color: gridCol } },
        y: { ticks: { color: '#00e5a0' }, grid: { color: gridCol } },
        y2: { position: 'right', ticks: { color: '#7b6eff' }, grid: { display: false } }
      }
    }
  });
}

function openBuyShares() {
  const price = S.sharePrice || 100;
  document.getElementById('sharePriceInfo').innerHTML = `
    <div style="font-size:12px;color:var(--text3)">Current Share Price</div>
    <div style="font-family:var(--font-display);font-size:34px;font-weight:700;color:var(--accent2)">KES ${price}</div>`;
  document.getElementById('shareQty').value = 1;
  document.getElementById('shareTotalBox').textContent = `Total: KES ${price}`;
  openModal('buySharesModal');
}

function updateShareTotal() {
  const qty = parseInt(document.getElementById('shareQty').value) || 0;
  document.getElementById('shareTotalBox').textContent = `Total: KES ${(qty * S.sharePrice).toLocaleString()}`;
}

async function doBuyShares() {
  const qty = parseInt(document.getElementById('shareQty').value);
  if (!qty || qty < 1) { toast('Enter at least 1 share','warn'); return; }
  try {
    await api('/api/shares/buy','POST',{quantity: qty});
    closeModal('buySharesModal');
    toast(`📈 Bought ${qty} share(s)!`,'success');
    shares();
  } catch(e){toast(e.message,'error');}
}

// ── REFERRALS ──
async function referrals() {
  const el = document.getElementById('pageContent');
  const u = S.user;
  const sets = await loadSettings();
  const bonus = parseFloat(sets.referral_bonus || 100).toFixed(0);
  const actFee = parseFloat(sets.activation_fee || 299).toFixed(0);

  el.innerHTML = `
    <div class="referral-box" style="margin-bottom:22px">
      <div style="font-family:var(--font-display);font-weight:700;font-size:19px;margin-bottom:8px">🔗 Your Referral Link</div>
      <div style="font-size:13px;color:var(--text2);margin-bottom:10px">Share this link. Earn <strong style="color:var(--accent)">KES <span data-setting="referral_bonus">${bonus}</span></strong> when friends activate!</div>
      <div class="referral-link" id="refLinkBox">${esc(u.referral_link)}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn btn-sm" onclick="copyRefLink()">📋 Copy</button>
        <button class="btn-outline btn-sm" onclick="shareLink()">📤 Share</button>
        <button class="btn-outline btn-sm" onclick="shareWhatsApp()">💬 WhatsApp</button>
      </div>
    </div>
    <div class="stats-grid" style="margin-bottom:22px">
      <div class="stat-card">
        <div class="stat-bg" style="background:var(--accent)"></div>
        <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Your Code</div>
        <div style="font-family:var(--font-display);font-size:24px;font-weight:700;letter-spacing:3px;color:var(--accent)">${u.referral_code}</div>
      </div>
      <div class="stat-card">
        <div class="stat-bg" style="background:var(--gold)"></div>
        <div class="stat-icon">💰</div>
        <div class="stat-value" style="font-size:24px">KES <span data-setting="referral_bonus">${bonus}</span></div>
        <div class="stat-label">Per Referral</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">How It Works</div></div>
      <div class="card-body">
        ${[
          ['Share','Share your unique referral link with friends and family'],
          ['Register','Friend registers using your link'],
          [`Activate & Earn`,`When they pay the KES <span data-setting="activation_fee">${actFee}</span> activation fee, you get KES <span data-setting="referral_bonus">${bonus}</span> instantly`]
        ].map(([t,d],i) => `<div style="display:flex;gap:14px;align-items:flex-start;padding:14px 0;${i<2?'border-bottom:1px solid var(--border)':''}">
          <div style="width:34px;height:34px;border-radius:50%;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;font-weight:700;color:var(--accent);flex-shrink:0">${i+1}</div>
          <div><strong>${t}</strong><br><span style="color:var(--text3);font-size:13px">${d}</span></div>
        </div>`).join('')}
      </div>
    </div>`;
}

function copyRefLink() {
  const link = document.getElementById('refLinkBox').textContent;
  navigator.clipboard.writeText(link).then(() => toast('Link copied!','success')).catch(() => toast('Copy manually','warn'));
}
function shareLink() {
  const link = document.getElementById('refLinkBox').textContent;
  if (navigator.share) navigator.share({title:'Join DTIP',text:'Earn money online!',url:link});
  else copyRefLink();
}
function shareWhatsApp() {
  const link = encodeURIComponent(document.getElementById('refLinkBox').textContent);
  window.open(`https://wa.me/?text=Join%20DTIP%20and%20earn%20money%20online%20in%20Kenya!%20${link}`,'_blank');
}

// ── MESSAGES ──
async function messages() {
  S.unreadMsg = 0; updateBadges();
  const el = document.getElementById('pageContent');
  el.innerHTML = `
    <div class="chat-layout">
      <div class="conv-list">
        <div class="conv-header">Conversations</div>
        <div id="convList"><div class="spinner" style="margin:20px auto"></div></div>
      </div>
      <div class="chat-area" style="display:flex;flex-direction:column">
        <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0" id="chatTop">
          <span style="color:var(--text3)">Select a conversation</span>
        </div>
        <div class="chat-messages" id="chatMsgs">
          <div class="empty-state"><div class="empty-icon">💬</div><h3>No Chat Selected</h3></div>
        </div>
        <div id="chatInput" style="display:none" class="chat-input-wrap">
          <input type="text" id="msgInput" placeholder="Type a message..." onkeypress="if(event.key==='Enter')sendMsg()">
          <button class="btn" onclick="sendMsg()">Send</button>
        </div>
      </div>
    </div>`;
  loadConvs();
}

async function loadConvs() {
  try {
    const r = await api('/api/messages/conversations');
    const el = document.getElementById('convList');
    if (S.user.role === 'member') {
      el.innerHTML = `<div class="conv-item" onclick="startChat(1,'Admin Support')">
        <div class="conv-avatar" style="background:var(--gold-dim);color:var(--gold)">👑</div>
        <div class="conv-info"><div class="conv-name">Admin Support</div><div class="conv-preview" style="font-size:11px;color:var(--text3)">Get help from admin</div></div>
      </div>` + r.conversations.map(c => convItem(c)).join('');
    } else {
      el.innerHTML = r.conversations.length ? r.conversations.map(c => convItem(c)).join('') :
        '<div style="padding:20px;color:var(--text3);font-size:13px;text-align:center">No conversations</div>';
    }
  } catch {}
}

function convItem(c) {
  return `<div class="conv-item" onclick="startChat(${c.user.id},'${esc(c.user.username)}')">
    <div class="conv-avatar">${c.user.username[0].toUpperCase()}</div>
    <div class="conv-info">
      <div class="conv-name">${esc(c.user.username)} ${c.user.is_verified?'✔':''}</div>
      <div class="conv-preview" style="font-size:11px;color:var(--text3)">${esc((c.last_message?.message||'').slice(0,40))}</div>
    </div>
    ${c.unread > 0 ? `<div class="conv-unread">${c.unread}</div>` : ''}
  </div>`;
}

async function startChat(uid, name) {
  S.activeChat = uid; S.chatPartner = name;
  document.getElementById('chatTop').innerHTML = `
    <div class="conv-avatar" style="width:32px;height:32px;font-size:12px">${name[0].toUpperCase()}</div>
    <strong>${esc(name)}</strong>`;
  document.getElementById('chatInput').style.display = 'flex';
  try {
    const r = await api('/api/messages/' + uid);
    const box = document.getElementById('chatMsgs');
    box.innerHTML = r.messages.length ? r.messages.map(m => bubbleHtml(m)).join('') :
      '<div class="empty-state"><div class="empty-icon">💬</div><p>Say hi!</p></div>';
    box.scrollTop = box.scrollHeight;
    if (S.socket) S.socket.emit('mark_read', {sender_id: uid});
  } catch(e){toast(e.message,'error');}
}

function bubbleHtml(m) {
  const mine = m.sender_id === S.user.id;
  return `<div class="bubble-wrap ${mine?'mine':''}">
    <div class="bubble ${mine?'mine':'theirs'}">${esc(m.message)}</div>
    <div class="bubble-time">${fmtTime(m.created_at)}</div>
  </div>`;
}

function renderMsg(m, mine=false) {
  const box = document.getElementById('chatMsgs');
  if (!box) return;
  box.insertAdjacentHTML('beforeend', bubbleHtml({...m, sender_id: mine?S.user.id:m.sender_id}));
  box.scrollTop = box.scrollHeight;
}

function sendMsg() {
  const input = document.getElementById('msgInput');
  const text = input.value.trim();
  if (!text || !S.activeChat) return;
  S.socket?.emit('send_message',{receiver_id: S.activeChat, message: text});
  renderMsg({message:text,sender_id:S.user.id,created_at:new Date().toISOString()}, true);
  input.value = '';
}

// ── NOTIFICATIONS ──
async function notifications() {
  const el = document.getElementById('pageContent');
  try {
    await api('/api/notifications/read-all','POST');
    S.unreadNotif = 0; updateBadges();
    const r = await api('/api/notifications');
    const icons = {success:'✅',error:'❌',info:'ℹ️',warn:'⚠️'};
    el.innerHTML = `<div class="card">
      <div class="card-header"><div class="card-title">Notifications (${r.notifications.length})</div></div>
      <div class="card-body">
        ${r.notifications.length ? r.notifications.map(n=>`
          <div style="display:flex;gap:12px;padding:13px 0;border-bottom:1px solid var(--border)">
            <span style="font-size:18px">${icons[n.type]||'🔔'}</span>
            <div>
              <div style="font-weight:700;margin-bottom:2px">${esc(n.title)}</div>
              <div style="color:var(--text2);font-size:13px">${esc(n.body)}</div>
              <div style="color:var(--text3);font-size:11px;margin-top:3px">${fmtDT(n.created_at)}</div>
            </div>
          </div>`).join('') : '<div class="empty-state"><div class="empty-icon">🔔</div><h3>All Clear</h3></div>'}
      </div>
    </div>`;
  } catch {}
}

// ── REVIEW QUEUE (Moderator/Admin) ──
async function review() {
  const el = document.getElementById('pageContent');
  const rb = document.getElementById('reviewBadge');
  if (rb) rb.style.display = 'none';

  let currentTab = 'pending';
  el.innerHTML = `
    <div class="tabs">
      <div class="tab active" id="rtab-pending" onclick="loadReviewTab('pending')">⏳ Pending</div>
      <div class="tab" id="rtab-approved" onclick="loadReviewTab('approved')">✅ Approved</div>
      <div class="tab" id="rtab-rejected" onclick="loadReviewTab('rejected')">❌ Rejected</div>
    </div>
    <div id="reviewContent"><div class="spinner"></div></div>`;
  loadReviewTab('pending');
}

async function loadReviewTab(status) {
  document.querySelectorAll('[id^="rtab-"]').forEach(t => t.classList.remove('active'));
  const tEl = document.getElementById('rtab-'+status);
  if (tEl) tEl.classList.add('active');
  try {
    const r = await api(`/api/admin/completions?status=${status}`);
    const el = document.getElementById('reviewContent');
    if (!r.completions.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><h3>Nothing here</h3></div>';
      return;
    }
    el.innerHTML = `<div style="display:flex;flex-direction:column;gap:14px">
      ${r.completions.map(c => `
        <div class="card">
          <div class="card-header">
            <div>
              <div class="card-title">#${c.id} — Task #${c.task_id} by <strong>${esc(c.username||'?')}</strong></div>
              <div style="font-size:12px;color:var(--text3);margin-top:2px">${fmtDT(c.created_at)}</div>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              ${c.ai_checked ? aiResultBadge(c.ai_score, c.ai_result) : `<button class="btn-outline btn-xs" onclick="rerunAI(${c.id})">🤖 AI Check</button>`}
              <span class="badge badge-${c.status==='approved'?'green':c.status==='rejected'?'red':'orange'}">${c.status}</span>
            </div>
          </div>
          <div class="card-body">
            ${c.proof_text ? `<div style="background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:12px;font-size:13px;color:var(--text2);line-height:1.7;max-height:120px;overflow-y:auto">${esc(c.proof_text)}</div>` : ''}
            ${c.pdf_url ? `<div style="margin-bottom:12px"><a href="${c.pdf_url}" target="_blank" class="badge badge-purple" style="font-size:12px;padding:6px 12px">📄 View PDF: ${esc(c.pdf_original||'submission.pdf')}</a></div>` : ''}
            ${c.rejection_reason ? `<div style="background:var(--red-dim);border:1px solid var(--red);border-radius:8px;padding:10px;font-size:12.5px;color:var(--red);margin-bottom:12px">Rejection reason: ${esc(c.rejection_reason)}</div>` : ''}
            ${c.reviewer_name ? `<div style="font-size:11px;color:var(--text3)">Reviewed by ${c.reviewer_name} on ${fmtDT(c.reviewed_at)}</div>` : ''}
            ${c.status === 'pending' ? `<div style="display:flex;gap:8px;margin-top:12px">
              <button class="btn btn-sm" onclick="approveComp(${c.id})">✅ Approve & Pay</button>
              <button class="btn-danger btn-sm" onclick="openReject(${c.id})">❌ Reject</button>
              <button class="btn-outline btn-xs" onclick="rerunAI(${c.id})">🤖 Re-check AI</button>
            </div>` : ''}
          </div>
        </div>`).join('')}
    </div>`;
  } catch(e) { document.getElementById('reviewContent').innerHTML = `<p style="color:var(--red)">${e.message}</p>`; }
}

async function approveComp(id) {
  try {
    await api(`/api/completions/${id}/review`, 'POST', {action:'approve'});
    toast('✅ Approved & reward paid!','success');
    loadReviewTab('pending');
  } catch(e){toast(e.message,'error');}
}

function openReject(id) {
  document.getElementById('rejectCompId').value = id;
  document.getElementById('rejectReason').value = '';
  openModal('rejectModal');
}

async function doReject() {
  const id = document.getElementById('rejectCompId').value;
  const reason = document.getElementById('rejectReason').value.trim();
  try {
    await api(`/api/completions/${id}/review`, 'POST', {action:'reject', reason});
    closeModal('rejectModal');
    toast('Submission rejected','info');
    loadReviewTab('pending');
  } catch(e){toast(e.message,'error');}
}

async function rerunAI(id) {
  try {
    const r = await api(`/api/completions/${id}/ai-check`, 'POST');
    toast(`🤖 AI Check: ${r.ai_result.result} (${r.ai_result.score}%) — ${r.ai_result.details}`, 'info');
    loadReviewTab('pending');
  } catch(e){toast(e.message,'error');}
}

// ── ADMIN PANEL ──
async function admin() {
  const el = document.getElementById('pageContent');
  try {
    const st = await api('/api/admin/stats');
    el.innerHTML = `
      <div style="display:flex;gap:10px;margin-bottom:22px;flex-wrap:wrap">
        <button class="btn" onclick="openModal('newTaskModal')">➕ Create Task</button>
        <button class="btn-outline" onclick="openModal('broadcastModal')">📢 Broadcast</button>
        <button class="btn-outline" onclick="go('review')">📝 Review Queue <span class="badge badge-orange">${st.pending_completions}</span></button>
        <button class="btn-outline" onclick="go('adminAlerts')">📢 Manage Alerts</button>
        <button class="btn-outline" onclick="go('adminUsers')">👥 Users</button>
        <button class="btn-outline" onclick="go('adminSettings')">🛠 Settings</button>
      </div>
      <div class="stats-grid">
        ${[
          ['👥','Users',st.total_users,'var(--accent2)'],
          ['🔓','Activated',st.activated_users,'var(--accent)'],
          ['⭐','Premium',st.premium_users,'var(--gold)'],
          ['⛔','Suspended',st.suspended_users,'var(--red)'],
          ['📋','Active Tasks',st.active_tasks,'var(--accent)'],
          ['📥','Pending Review',st.pending_completions,'var(--orange)'],
          ['✅','Completions',st.total_completions,'var(--accent2)'],
          ['💰','Platform KES',st.total_wallet.toLocaleString(),'var(--gold)'],
        ].map(([i,l,v,c]) => `<div class="stat-card">
          <div class="stat-bg" style="background:${c}"></div>
          <div class="stat-icon">${i}</div>
          <div class="stat-value" style="font-size:${String(v).length>6?'18px':'28px'}">${v}</div>
          <div class="stat-label">${l}</div>
        </div>`).join('')}
      </div>`;
  } catch(e){el.innerHTML=`<p style="color:var(--red)">${e.message}</p>`;}
}

// ── ADMIN USERS ──
async function adminUsers() {
  const el = document.getElementById('pageContent');
  el.innerHTML = `
    <div style="display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap">
      <input type="text" id="userQ" placeholder="Search users..." style="max-width:280px" oninput="loadAdminUsers()">
    </div>
    <div class="card">
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Tier</th><th>Joined</th><th>Actions</th></tr></thead>
          <tbody id="usersBody"><tr><td colspan="6"><div class="spinner" style="margin:20px auto"></div></td></tr></tbody>
        </table>
      </div>
    </div>`;
  loadAdminUsers();
}

async function loadAdminUsers() {
  const q = document.getElementById('userQ')?.value || '';
  try {
    const r = await api(`/api/admin/users?q=${encodeURIComponent(q)}`);
    document.getElementById('usersBody').innerHTML = r.users.map(u => `<tr>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="user-avatar" style="width:30px;height:30px;font-size:11px;border-radius:8px">${u.username[0].toUpperCase()}</div>
          <div>
            <div style="font-weight:700;font-size:13px">${esc(u.username)} ${u.is_verified?'<span style="color:var(--accent)">✔</span>':''}</div>
            <div style="font-size:11px;color:var(--text3)">${esc(u.email)}</div>
          </div>
        </div>
      </td>
      <td><span class="badge ${u.role==='admin'?'badge-gold':u.role==='moderator'?'badge-purple':'badge-gray'}">${u.role}</span></td>
      <td>
        ${u.is_suspended ? '<span class="badge badge-red">⛔ Suspended</span>' : u.is_activated ? '<span class="badge badge-green">Active</span>' : '<span class="badge badge-gray">Inactive</span>'}
      </td>
      <td>${u.tier==='premium' && !u.premium_suspended ? '<span class="badge badge-gold">⭐ Premium</span>' : u.premium_suspended ? '<span class="badge badge-red">Premium Off</span>' : '<span class="badge badge-gray">Free</span>'}</td>
      <td style="color:var(--text3);font-size:12px">${fmtDate(u.created_at)}</td>
      <td>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          <button class="btn-outline btn-xs" onclick="toggleVerify(${u.id})">${u.is_verified?'Unverify':'Verify'}</button>
          ${u.is_suspended ?
            `<button class="btn-outline btn-xs" onclick="unsuspendUser(${u.id})">Restore</button>` :
            `<button class="btn-xs btn-danger" onclick="openSuspend(${u.id})">Suspend</button>`}
          <button class="btn-outline btn-xs" onclick="openRoleModal(${u.id},'${esc(u.role)}')">👤 Role</button>
          ${u.tier!=='premium' || u.premium_suspended ?
            `<button class="btn-outline btn-xs" onclick="grantPremium(${u.id})">⭐ Grant</button>` :
            `<button class="btn-xs btn-danger" onclick="removePremium(${u.id})">⭐ Remove</button>`}
          <button class="btn-outline btn-xs" onclick="startChat(${u.id},'${esc(u.username)}');go('messages')">💬</button>
        </div>
      </td>
    </tr>`).join('');
  } catch(e){toast(e.message,'error');}
}

async function toggleVerify(uid){try{await api('/api/admin/users/'+uid+'/toggle-verify','POST');loadAdminUsers();toast('Updated','success');}catch(e){toast(e.message,'error');}}

function openSuspend(uid) {
  document.getElementById('suspendUid').value = uid;
  document.getElementById('suspendReason').value = '';
  openModal('suspendModal');
}

async function doSuspend() {
  const uid = document.getElementById('suspendUid').value;
  const reason = document.getElementById('suspendReason').value.trim();
  try {
    await api(`/api/admin/users/${uid}/suspend`, 'POST', {reason});
    closeModal('suspendModal');
    toast('Account suspended','info');
    loadAdminUsers();
  } catch(e){toast(e.message,'error');}
}

async function unsuspendUser(uid){try{await api('/api/admin/users/'+uid+'/unsuspend','POST');loadAdminUsers();toast('Account restored','success');}catch(e){toast(e.message,'error');}}

function openRoleModal(uid, currentRole) {
  document.getElementById('roleUid').value = uid;
  document.getElementById('roleSelect').value = currentRole;
  openModal('roleModal');
}

async function doSetRole() {
  const uid = document.getElementById('roleUid').value;
  const role = document.getElementById('roleSelect').value;
  try {
    await api(`/api/admin/users/${uid}/set-role`, 'POST', {role});
    closeModal('roleModal');
    toast(`Role updated to ${role}`, 'success');
    loadAdminUsers();
  } catch(e){toast(e.message,'error');}
}

async function grantPremium(uid){try{await api('/api/admin/users/'+uid+'/grant-premium','POST');loadAdminUsers();toast('Premium granted!','success');}catch(e){toast(e.message,'error');}}
async function removePremium(uid){try{await api('/api/admin/users/'+uid+'/remove-premium','POST');loadAdminUsers();toast('Premium suspended','info');}catch(e){toast(e.message,'error');}}

// ── ADMIN ALERTS ──
async function adminAlerts() {
  const el = document.getElementById('pageContent');
  el.innerHTML = `
    <div style="display:flex;justify-content:flex-end;margin-bottom:18px">
      <button class="btn" onclick="openModal('alertModal')">➕ Create Alert</button>
    </div>
    <div id="alertsList"><div class="spinner"></div></div>`;
  loadAlertsList();
}

async function loadAlertsList() {
  try {
    const r = await api('/api/admin/alerts');
    const typeColor = {info:'badge-purple', warning:'badge-gold', danger:'badge-red', success:'badge-green'};
    document.getElementById('alertsList').innerHTML = r.alerts.length ? `
      <div class="card">
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Type</th><th>Title</th><th>Message</th><th>Active</th><th>Created</th><th>Expires</th><th></th></tr></thead>
            <tbody>${r.alerts.map(a => `<tr>
              <td><span class="badge ${typeColor[a.type]||'badge-gray'}">${a.type}</span></td>
              <td><strong>${esc(a.title)}</strong></td>
              <td style="max-width:250px;color:var(--text2);font-size:12px">${esc(a.message)}</td>
              <td>${a.is_active ? '<span class="badge badge-green">Active</span>' : '<span class="badge badge-gray">Off</span>'}</td>
              <td style="color:var(--text3)">${fmtDate(a.created_at)}</td>
              <td style="color:var(--text3)">${a.expires_at ? fmtDate(a.expires_at) : 'Never'}</td>
              <td><button class="btn-danger btn-xs" onclick="deleteAlert(${a.id})">Delete</button></td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>` : '<div class="empty-state"><div class="empty-icon">📢</div><h3>No Alerts</h3></div>';
  } catch {}
}

async function doCreateAlert() {
  const title = document.getElementById('altTitle').value.trim();
  const message = document.getElementById('altMsg').value.trim();
  const type = document.getElementById('altType').value;
  const hours = document.getElementById('altExpires').value;
  if (!title || !message) { toast('Title and message required','error'); return; }
  try {
    await api('/api/admin/alerts', 'POST', {title, message, type, expires_hours: hours || null});
    closeModal('alertModal');
    toast('Alert published to all users!','success');
    loadAlertsList();
    loadAlertBanners();
  } catch(e){toast(e.message,'error');}
}

async function deleteAlert(id) {
  try { await api('/api/admin/alerts/'+id,'DELETE'); loadAlertsList(); loadAlertBanners(); toast('Alert removed','info'); }
  catch(e){toast(e.message,'error');}
}

// ── ADMIN SETTINGS ──
async function adminSettings() {
  const el = document.getElementById('pageContent');
  try {
    const r = await api('/api/admin/settings');
    el.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Platform Settings</div>
            <div style="font-size:12px;color:var(--text3);margin-top:2px">Changes apply immediately and update all users' displays in real-time</div>
          </div>
          <button class="btn btn-sm" onclick="saveSettings()">💾 Save All</button>
        </div>
        <div class="card-body">
          <div class="settings-grid">
            ${[
              ['activation_fee','Activation Fee (KES)','One-time fee users pay to start earning',r.activation_fee],
              ['referral_bonus','Referral Bonus (KES)','Paid to referrer when friend activates',r.referral_bonus],
              ['premium_fee','Premium Monthly Fee (KES)','Monthly fee for premium tier',r.premium_fee],
              ['withdrawal_fee_pct','Withdrawal Fee (%)','Deducted on every withdrawal',r.withdrawal_fee_pct],
              ['free_daily_limit','Free Daily Task Limit','Max tasks/day for free users',r.free_daily_limit],
              ['premium_daily_limit','Premium Daily Limit','Max tasks/day for premium users',r.premium_daily_limit],
              ['share_price','Share Price (KES)','Current price per platform share',r.share_price],
              ['base_url','Platform Base URL','Used in referral links',r.base_url],
            ].map(([key,label,hint,val]) => `
              <div class="form-group">
                <label class="form-label">${label}</label>
                <input type="${key.includes('url')?'text':'number'}" id="set_${key}" value="${esc(val)}">
                <div class="form-hint">${hint}</div>
              </div>`).join('')}
          </div>
          <div style="margin-top:20px;padding-top:18px;border-top:1px solid var(--border)">
            <div class="form-label" style="margin-bottom:14px">Quick Actions</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button class="btn-outline" onclick="openModal('broadcastModal')">📢 Broadcast Message</button>
              <button class="btn-outline" onclick="openModal('alertModal')">📢 Create Alert</button>
              <button class="btn-outline" onclick="openModal('newTaskModal')">➕ Create Task</button>
            </div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:18px">
        <div class="card-header"><div class="card-title">Webhook Information</div></div>
        <div class="card-body">
          <p style="font-size:13px;color:var(--text2);margin-bottom:12px">Configure IntaSend to send payment webhooks to:</p>
          <div style="background:var(--surface3);border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-family:monospace;font-size:13px;color:var(--accent2);word-break:break-all">${r.base_url}/webhook/intasend</div>
          <div style="font-size:12px;color:var(--text3);margin-top:8px">IntaSend Dashboard → Settings → Webhooks → Payment Events → Paste URL above</div>
          <div style="margin-top:12px;font-size:12px;color:var(--text3)">
            Events to enable: <span class="badge badge-green">PAYMENT_COMPLETE</span> <span class="badge badge-red">PAYMENT_FAILED</span> <span class="badge badge-orange">PAYMENT_CANCELLED</span>
          </div>
        </div>
      </div>`;
  } catch(e){el.innerHTML=`<p style="color:var(--red)">${e.message}</p>`;}
}

async function saveSettings() {
  const keys = ['activation_fee','referral_bonus','premium_fee','withdrawal_fee_pct','free_daily_limit','premium_daily_limit','share_price','base_url'];
  const data = {};
  keys.forEach(k => { const el = document.getElementById('set_'+k); if (el) data[k] = el.value; });
  try {
    await api('/api/admin/settings','POST',data);
    toast('✅ Settings saved & broadcast to all users!','success');
  } catch(e){toast(e.message,'error');}
}

async function sendBroadcast() {
  try {
    await api('/api/admin/broadcast','POST',{message: document.getElementById('bcastMsg').value});
    closeModal('broadcastModal');
    document.getElementById('bcastMsg').value = '';
    toast('Broadcast sent!','success');
  } catch(e){toast(e.message,'error');}
}

// ── ACTIVATION & PREMIUM ──
async function openActivate() {
  const sets = await loadSettings();
  const afd = document.getElementById('activateFeeAmt');
  if (afd) afd.textContent = `KES ${parseFloat(sets.activation_fee||299).toFixed(0)}`;
  openModal('activateModal');
}

async function doActivate() {
  const phone = document.getElementById('actPhone').value;
  try {
    const r = await api('/api/activate','POST',{phone});
    closeModal('activateModal');
    toast(r.demo ? '🎉 Activated! Start earning!' : 'Check your phone for M-Pesa prompt','success');
    S.user.is_activated = true;
    go('home');
  } catch(e){toast(e.message,'error');}
}

async function openPremium() {
  const sets = await loadSettings();
  const pfd = document.getElementById('premiumFeeAmt');
  const pl = document.getElementById('premLimit');
  if (pfd) pfd.textContent = `KES ${parseFloat(sets.premium_fee||499).toFixed(0)}`;
  if (pl) pl.textContent = sets.premium_daily_limit || 10;
  openModal('premiumModal');
}

async function doUpgradePremium() {
  const phone = document.getElementById('premPhone').value;
  try {
    const r = await api('/api/upgrade-premium','POST',{phone});
    closeModal('premiumModal');
    toast('⭐ Premium activated!','success');
    S.user = r.user;
    go('home');
  } catch(e){toast(e.message,'error');}
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)

# ─────────────────────────────────────────
# DB INIT
# ─────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        defaults = {
            'activation_fee': str(app.config['DEFAULT_ACTIVATION_FEE']),
            'referral_bonus': str(app.config['DEFAULT_REFERRAL_BONUS']),
            'premium_fee': str(app.config['DEFAULT_PREMIUM_FEE']),
            'withdrawal_fee_pct': str(app.config['DEFAULT_WITHDRAWAL_FEE_PCT']),
            'free_daily_limit': str(app.config['FREE_DAILY_LIMIT']),
            'premium_daily_limit': str(app.config['PREMIUM_DAILY_LIMIT']),
            'share_price': '100.0',
            'base_url': app.config['BASE_URL'],
        }
        for k, v in defaults.items():
            if not PlatformSettings.query.filter_by(key=k).first():
                db.session.add(PlatformSettings(key=k, value=v))

        if not User.query.filter_by(role='admin').first():
            admin = User(email=app.config['ADMIN_EMAIL'], username='admin',
                         role='admin', is_verified=True, is_activated=True,
                         referral_code=gen_code())
            admin.set_password(app.config['ADMIN_PASSWORD'])
            db.session.add(admin)
            db.session.flush()
            db.session.add(Wallet(user_id=admin.id, balance=5000.0))
            logger.info(f'Admin created: {app.config["ADMIN_EMAIL"]}')

        db.session.commit()

def validate_env():
    """Warn about missing or insecure environment variables at startup."""
    warnings = []
    if not os.environ.get('SECRET_KEY'):
        warnings.append('SECRET_KEY not set — sessions will not persist across restarts (set in production!)')
    if not os.environ.get('JWT_SECRET'):
        warnings.append('JWT_SECRET not set — using SECRET_KEY as fallback')
    if not os.environ.get('GOOGLE_CLIENT_ID') or not os.environ.get('GOOGLE_CLIENT_SECRET'):
        warnings.append('GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set — Google login disabled')
    if os.environ.get('DEMO_MODE','true').lower() == 'true':
        warnings.append('DEMO_MODE=true — payments are simulated; set DEMO_MODE=false in production')
    if not os.environ.get('DATABASE_URL'):
        warnings.append('DATABASE_URL not set — using local SQLite (not suitable for production)')
    for w in warnings:
        logger.warning(f'[CONFIG] {w}')

if __name__ == '__main__':
    validate_env()
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG','false').lower() == 'true'
    logger.info(f'DTIP v3.0 on :{port} | demo={app.config["DEMO_MODE"]}')
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, allow_unsafe_werkzeug=True)
else:
    validate_env()
    init_db()
