"""
DTIP v2 — Moderated Digital Tasks & Earning Platform
Single-file production-ready Flask app with Google OAuth, SocketIO, M-Pesa
Deploy: Railway / Render / Heroku
Run: python app.py | gunicorn -k eventlet -w 1 app:app
"""

import os, sys, json, re, uuid, hashlib, hmac, logging, time, secrets
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode
import urllib.request

from flask import (Flask, request, jsonify, redirect, url_for,
                   session, render_template_string, make_response)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
import jwt
import bcrypt
import requests as http_requests
from sqlalchemy import event

# ─────────────────────────────────────────
# APP CONFIGURATION
# ─────────────────────────────────────────

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', secrets.token_hex(32)),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///dtip.db').replace('postgres://', 'postgresql://'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True, 'pool_recycle': 300},
    JWT_SECRET=os.environ.get('JWT_SECRET', secrets.token_hex(32)),
    JWT_EXPIRY_HOURS=int(os.environ.get('JWT_EXPIRY_HOURS', 24)),
    GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID', ''),
    GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
    GOOGLE_REDIRECT_URI=os.environ.get('GOOGLE_REDIRECT_URI', 'http://localhost:5000/auth/google/callback'),
    INTASEND_API_KEY=os.environ.get('INTASEND_API_KEY', 'demo'),
    INTASEND_PUBLIC_KEY=os.environ.get('INTASEND_PUBLIC_KEY', 'demo'),
    DEMO_MODE=os.environ.get('DEMO_MODE', 'true').lower() == 'true',
    UPLOAD_FOLDER=os.environ.get('UPLOAD_FOLDER', 'uploads'),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    CACHE_TYPE='SimpleCache',
    CACHE_DEFAULT_TIMEOUT=300,
    ADMIN_EMAIL=os.environ.get('ADMIN_EMAIL', 'admin@dtip.co.ke'),
    ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD', 'Admin@DTIP2024!'),
    REFERRAL_BONUS=float(os.environ.get('REFERRAL_BONUS', 50.0)),
    WITHDRAWAL_FEE_PCT=float(os.environ.get('WITHDRAWAL_FEE_PCT', 5.0)),
)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',
                    logger=False, engineio_logger=False)
cache = Cache(app)
limiter = Limiter(key_func=get_remote_address, app=app,
                  default_limits=["200 per day", "50 per hour"],
                  storage_uri="memory://")

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

SUSPICIOUS_KEYWORDS = ['porn','xxx','drugs','weapon','hack','phishing','scam','casino','bet','gambling']

# ─────────────────────────────────────────
# DATABASE MODELS
# ─────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'users'
    id           = db.Column(db.Integer, primary_key=True)
    email        = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username     = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash= db.Column(db.String(255), nullable=True)
    google_id    = db.Column(db.String(120), unique=True, nullable=True)
    avatar_url   = db.Column(db.String(500), nullable=True)
    role         = db.Column(db.String(20), default='worker')   # admin|client|worker
    tier         = db.Column(db.String(20), default='free')     # free|gold|diamond
    is_active    = db.Column(db.Boolean, default=True)
    is_verified  = db.Column(db.Boolean, default=False)
    referral_code= db.Column(db.String(20), unique=True, nullable=True)
    referred_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_login   = db.Column(db.DateTime, nullable=True)

    wallet       = db.relationship('Wallet', backref='user', uselist=False, lazy='joined')
    sent_msgs    = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy='dynamic')
    recv_msgs    = db.relationship('Message', foreign_keys='Message.receiver_id', backref='receiver', lazy='dynamic')

    def set_password(self, pw):
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    def check_password(self, pw):
        if not self.password_hash: return False
        return bcrypt.checkpw(pw.encode(), self.password_hash.encode())

    def to_dict(self, include_private=False):
        d = dict(id=self.id, email=self.email, username=self.username,
                 role=self.role, tier=self.tier, is_active=self.is_active,
                 is_verified=self.is_verified, avatar_url=self.avatar_url,
                 referral_code=self.referral_code,
                 created_at=self.created_at.isoformat(),
                 last_login=self.last_login.isoformat() if self.last_login else None)
        if include_private:
            d['google_id'] = self.google_id
        return d

class Wallet(db.Model):
    __tablename__ = 'wallets'
    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    balance  = db.Column(db.Float, default=0.0)
    escrow   = db.Column(db.Float, default=0.0)
    total_earned = db.Column(db.Float, default=0.0)
    total_spent  = db.Column(db.Float, default=0.0)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return dict(balance=round(self.balance,2), escrow=round(self.escrow,2),
                    total_earned=round(self.total_earned,2),
                    total_spent=round(self.total_spent,2))

class WalletLedger(db.Model):
    __tablename__ = 'wallet_ledger'
    id          = db.Column(db.Integer, primary_key=True)
    wallet_id   = db.Column(db.Integer, db.ForeignKey('wallets.id'), nullable=False)
    type        = db.Column(db.String(30), nullable=False)  # deposit|withdraw|escrow|release|bonus|fee
    amount      = db.Column(db.Float, nullable=False)
    balance_after= db.Column(db.Float, nullable=False)
    description = db.Column(db.String(255))
    reference   = db.Column(db.String(100), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=round(self.amount,2),
                    balance_after=round(self.balance_after,2),
                    description=self.description, reference=self.reference,
                    created_at=self.created_at.isoformat())

class Task(db.Model):
    __tablename__ = 'tasks'
    id           = db.Column(db.Integer, primary_key=True)
    title        = db.Column(db.String(200), nullable=False)
    description  = db.Column(db.Text, nullable=False)
    category     = db.Column(db.String(80), nullable=False)
    budget       = db.Column(db.Float, nullable=False)
    currency     = db.Column(db.String(10), default='KES')
    deadline     = db.Column(db.DateTime, nullable=True)
    slots        = db.Column(db.Integer, default=1)
    status       = db.Column(db.String(30), default='pending_approval')
    client_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_approved  = db.Column(db.Boolean, default=False)
    approved_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at  = db.Column(db.DateTime, nullable=True)
    is_flagged   = db.Column(db.Boolean, default=False)
    flag_reason  = db.Column(db.String(255), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client       = db.relationship('User', foreign_keys=[client_id], backref='posted_tasks')
    approver     = db.relationship('User', foreign_keys=[approved_by])
    applications = db.relationship('TaskApplication', backref='task', lazy='dynamic', cascade='all,delete')

    def to_dict(self, include_apps=False):
        d = dict(id=self.id, title=self.title, description=self.description,
                 category=self.category, budget=self.budget, currency=self.currency,
                 deadline=self.deadline.isoformat() if self.deadline else None,
                 slots=self.slots, status=self.status,
                 client_id=self.client_id,
                 client_name=self.client.username if self.client else None,
                 client_verified=self.client.is_verified if self.client else False,
                 is_approved=self.is_approved, is_flagged=self.is_flagged,
                 flag_reason=self.flag_reason,
                 approved_at=self.approved_at.isoformat() if self.approved_at else None,
                 created_at=self.created_at.isoformat(),
                 application_count=self.applications.count())
        if include_apps:
            d['applications'] = [a.to_dict() for a in self.applications]
        return d

class TaskApplication(db.Model):
    __tablename__ = 'task_applications'
    id          = db.Column(db.Integer, primary_key=True)
    task_id     = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False)
    worker_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    proposal    = db.Column(db.Text, nullable=False)
    status      = db.Column(db.String(30), default='pending')  # pending|accepted|rejected|completed|review
    submission  = db.Column(db.Text, nullable=True)
    submitted_at= db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    worker      = db.relationship('User', backref='applications')

    def to_dict(self):
        return dict(id=self.id, task_id=self.task_id, worker_id=self.worker_id,
                    worker_name=self.worker.username if self.worker else None,
                    proposal=self.proposal, status=self.status,
                    submission=self.submission,
                    submitted_at=self.submitted_at.isoformat() if self.submitted_at else None,
                    created_at=self.created_at.isoformat())

class Message(db.Model):
    __tablename__ = 'messages'
    id             = db.Column(db.Integer, primary_key=True)
    sender_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    message        = db.Column(db.Text, nullable=False)
    attachment_url = db.Column(db.String(500), nullable=True)
    is_read        = db.Column(db.Boolean, default=False)
    is_broadcast   = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, sender_id=self.sender_id,
                    sender_name=self.sender.username if self.sender else None,
                    sender_avatar=self.sender.avatar_url if self.sender else None,
                    receiver_id=self.receiver_id, message=self.message,
                    attachment_url=self.attachment_url, is_read=self.is_read,
                    is_broadcast=self.is_broadcast,
                    created_at=self.created_at.isoformat())

class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(50), default='info')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', backref='notifications')

    def to_dict(self):
        return dict(id=self.id, title=self.title, body=self.body,
                    type=self.type, is_read=self.is_read,
                    created_at=self.created_at.isoformat())

class Payment(db.Model):
    __tablename__ = 'payments'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    type        = db.Column(db.String(30), nullable=False)  # deposit|withdrawal
    amount      = db.Column(db.Float, nullable=False)
    fee         = db.Column(db.Float, default=0.0)
    net_amount  = db.Column(db.Float, nullable=False)
    phone       = db.Column(db.String(20), nullable=True)
    reference   = db.Column(db.String(100), nullable=True)
    status      = db.Column(db.String(30), default='pending')
    provider    = db.Column(db.String(50), default='mpesa')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    user        = db.relationship('User', backref='payments')

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=self.amount,
                    fee=self.fee, net_amount=self.net_amount,
                    phone=self.phone, reference=self.reference,
                    status=self.status, provider=self.provider,
                    created_at=self.created_at.isoformat())

# ─────────────────────────────────────────
# HELPERS & MIDDLEWARE
# ─────────────────────────────────────────

def gen_referral_code():
    return ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))

def auto_moderate(task):
    text = (task.title + ' ' + task.description).lower()
    for kw in SUSPICIOUS_KEYWORDS:
        if kw in text:
            task.is_flagged = True
            task.flag_reason = f'Suspicious keyword: {kw}'
            return
    if task.budget < 10:
        task.is_flagged = True
        task.flag_reason = 'Budget extremely low (< KES 10)'
    elif task.budget > 500000:
        task.is_flagged = True
        task.flag_reason = 'Budget extremely high (> KES 500,000)'

def create_notification(user_id, title, body, ntype='info'):
    n = Notification(user_id=user_id, title=title, body=body, type=ntype)
    db.session.add(n)
    try:
        socketio.emit('notification', n.to_dict(), room=f'user_{user_id}')
    except: pass

def ledger_entry(wallet, tx_type, amount, description, reference=None):
    wallet.updated_at = datetime.utcnow()
    entry = WalletLedger(wallet_id=wallet.id, type=tx_type,
                         amount=amount, balance_after=wallet.balance,
                         description=description, reference=reference)
    db.session.add(entry)

def make_token(user_id, role):
    payload = {'sub': user_id, 'role': role,
               'exp': datetime.utcnow() + timedelta(hours=app.config['JWT_EXPIRY_HOURS']),
               'iat': datetime.utcnow()}
    return jwt.encode(payload, app.config['JWT_SECRET'], algorithm='HS256')

def decode_token(token):
    return jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])

def get_current_user():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '').strip()
    if not token:
        token = request.args.get('token', '')
    if not token:
        return None
    try:
        data = decode_token(token)
        return User.query.get(data['sub'])
    except:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_active:
            return jsonify(error='Unauthorized'), 401
        return f(user, *args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user.role != 'admin':
            return jsonify(error='Admin access required'), 403
        return f(user, *args, **kwargs)
    return decorated

def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            if not user or user.role not in roles:
                return jsonify(error='Insufficient permissions'), 403
            return f(user, *args, **kwargs)
        return decorated
    return decorator

# ─────────────────────────────────────────
# GOOGLE OAUTH
# ─────────────────────────────────────────

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

@app.route('/auth/google')
def google_login():
    if not app.config['GOOGLE_CLIENT_ID']:
        return jsonify(error='Google OAuth not configured'), 400
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = dict(client_id=app.config['GOOGLE_CLIENT_ID'],
                  redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
                  response_type='code', scope='openid email profile',
                  state=state, access_type='offline', prompt='select_account')
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@app.route('/auth/google/callback')
def google_callback():
    error = request.args.get('error')
    if error:
        return redirect('/?error=' + error)
    code = request.args.get('code')
    state = request.args.get('state')
    if state != session.pop('oauth_state', None):
        return redirect('/?error=invalid_state')
    try:
        resp = http_requests.post(GOOGLE_TOKEN_URL, data=dict(
            code=code, client_id=app.config['GOOGLE_CLIENT_ID'],
            client_secret=app.config['GOOGLE_CLIENT_SECRET'],
            redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
            grant_type='authorization_code'), timeout=10)
        tokens = resp.json()
        access_token = tokens['access_token']
        ui = http_requests.get(GOOGLE_USERINFO_URL,
                               headers={'Authorization': f'Bearer {access_token}'}, timeout=10).json()
        g_id = ui['sub']
        email = ui.get('email', '')
        name = ui.get('name', email.split('@')[0])
        picture = ui.get('picture', '')

        user = User.query.filter_by(google_id=g_id).first()
        if not user:
            user = User.query.filter_by(email=email).first()
        if not user:
            username = re.sub(r'[^a-z0-9_]', '', name.lower().replace(' ','_'))[:20]
            if User.query.filter_by(username=username).first():
                username = username + str(int(time.time()))[-4:]
            user = User(email=email, username=username, google_id=g_id,
                        avatar_url=picture, role='worker',
                        referral_code=gen_referral_code())
            db.session.add(user)
            db.session.flush()
            wallet = Wallet(user_id=user.id)
            db.session.add(wallet)
            db.session.commit()
        else:
            user.google_id = g_id
            if picture: user.avatar_url = picture
            user.last_login = datetime.utcnow()
            db.session.commit()

        token = make_token(user.id, user.role)
        return redirect(f'/?token={token}&user_id={user.id}')
    except Exception as e:
        logger.error(f'Google OAuth error: {e}')
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
    password = d.get('password', '')
    referral  = d.get('referral_code', '').strip().upper()

    if not email or not username or not password:
        return jsonify(error='Email, username and password required'), 400
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return jsonify(error='Invalid email'), 400
    if len(password) < 8:
        return jsonify(error='Password must be at least 8 characters'), 400
    if User.query.filter_by(email=email).first():
        return jsonify(error='Email already registered'), 409
    if User.query.filter_by(username=username).first():
        return jsonify(error='Username taken'), 409

    referred_by = None
    if referral:
        ref_user = User.query.filter_by(referral_code=referral).first()
        if ref_user: referred_by = ref_user.id

    user = User(email=email, username=username, role=d.get('role','worker'),
                referral_code=gen_referral_code(), referred_by=referred_by)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    wallet = Wallet(user_id=user.id)
    db.session.add(wallet)
    db.session.flush()

    if referred_by:
        ref_wallet = Wallet.query.filter_by(user_id=referred_by).first()
        if ref_wallet:
            bonus = app.config['REFERRAL_BONUS']
            ref_wallet.balance += bonus
            ref_wallet.total_earned += bonus
            ledger_entry(ref_wallet, 'bonus', bonus, f'Referral bonus from {username}')
            create_notification(referred_by, '🎉 Referral Bonus!',
                                f'You earned KES {bonus} for referring {username}', 'success')
    db.session.commit()
    token = make_token(user.id, user.role)
    return jsonify(token=token, user=user.to_dict()), 201

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("20 per hour")
def login():
    d = request.get_json() or {}
    email = (d.get('email') or '').strip().lower()
    password = d.get('password', '')
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify(error='Invalid credentials'), 401
    if not user.is_active:
        return jsonify(error='Account suspended'), 403
    user.last_login = datetime.utcnow()
    db.session.commit()
    token = make_token(user.id, user.role)
    return jsonify(token=token, user=user.to_dict())

@app.route('/api/auth/me')
@require_auth
def me(user):
    return jsonify(user=user.to_dict(include_private=True),
                   wallet=user.wallet.to_dict() if user.wallet else None)

# ─────────────────────────────────────────
# USER ROUTES
# ─────────────────────────────────────────

@app.route('/api/users/<int:uid>')
@require_auth
def get_user(current_user, uid):
    u = User.query.get_or_404(uid)
    return jsonify(user=u.to_dict())

@app.route('/api/users/<int:uid>/verify', methods=['POST'])
@require_admin
def verify_user(admin, uid):
    u = User.query.get_or_404(uid)
    u.is_verified = not u.is_verified
    db.session.commit()
    create_notification(uid, '✔ Account Verified',
                        'Your account has been verified by admin!', 'success')
    return jsonify(user=u.to_dict())

@app.route('/api/users/<int:uid>/suspend', methods=['POST'])
@require_admin
def suspend_user(admin, uid):
    u = User.query.get_or_404(uid)
    if u.role == 'admin':
        return jsonify(error='Cannot suspend admin'), 400
    u.is_active = not u.is_active
    db.session.commit()
    return jsonify(user=u.to_dict())

@app.route('/api/users/<int:uid>/role', methods=['POST'])
@require_admin
def change_role(admin, uid):
    u = User.query.get_or_404(uid)
    role = request.get_json().get('role','worker')
    if role not in ('admin','client','worker'):
        return jsonify(error='Invalid role'), 400
    u.role = role
    db.session.commit()
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users')
@require_admin
def admin_users(admin):
    page = request.args.get('page', 1, int)
    q = request.args.get('q', '')
    query = User.query
    if q:
        query = query.filter(User.username.ilike(f'%{q}%') | User.email.ilike(f'%{q}%'))
    users = query.order_by(User.created_at.desc()).paginate(page=page, per_page=20)
    return jsonify(users=[u.to_dict() for u in users.items],
                   total=users.total, pages=users.pages, page=page)

# ─────────────────────────────────────────
# TASK ROUTES
# ─────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    page     = request.args.get('page', 1, int)
    cat      = request.args.get('category', '')
    q        = request.args.get('q', '')
    status   = request.args.get('status', 'open')
    sort     = request.args.get('sort', 'newest')

    query = Task.query.filter_by(is_approved=True)
    if status:
        query = query.filter_by(status=status)
    if cat:
        query = query.filter_by(category=cat)
    if q:
        query = query.filter(Task.title.ilike(f'%{q}%') | Task.description.ilike(f'%{q}%'))
    if sort == 'budget_high':
        query = query.order_by(Task.budget.desc())
    elif sort == 'budget_low':
        query = query.order_by(Task.budget.asc())
    else:
        query = query.order_by(Task.created_at.desc())

    tasks = query.paginate(page=page, per_page=20)
    return jsonify(tasks=[t.to_dict() for t in tasks.items],
                   total=tasks.total, pages=tasks.pages, page=page)

@app.route('/api/tasks', methods=['POST'])
@require_role('client', 'admin')
def create_task(user):
    d = request.get_json() or {}
    required = ['title','description','category','budget']
    for f in required:
        if not d.get(f):
            return jsonify(error=f'{f} is required'), 400

    deadline = None
    if d.get('deadline'):
        try: deadline = datetime.fromisoformat(d['deadline'])
        except: pass

    task = Task(title=d['title'].strip(), description=d['description'].strip(),
                category=d['category'], budget=float(d['budget']),
                deadline=deadline, slots=int(d.get('slots', 1)),
                client_id=user.id)

    auto_moderate(task)

    if user.role == 'admin':
        task.is_approved = True
        task.approved_by = user.id
        task.approved_at = datetime.utcnow()
        task.status = 'open'
    else:
        task.status = 'pending_approval'

    db.session.add(task)
    db.session.commit()

    if user.role != 'admin':
        admins = User.query.filter_by(role='admin').all()
        for admin in admins:
            create_notification(admin.id, '📋 New Task Pending',
                                f'Task "{task.title}" needs review', 'info')
    return jsonify(task=task.to_dict()), 201

@app.route('/api/tasks/<int:tid>')
def get_task(tid):
    task = Task.query.get_or_404(tid)
    return jsonify(task=task.to_dict(include_apps=True))

@app.route('/api/tasks/<int:tid>/approve', methods=['POST'])
@require_admin
def approve_task(admin, tid):
    task = Task.query.get_or_404(tid)
    action = request.get_json().get('action','approve')
    if action == 'approve':
        task.is_approved = True
        task.approved_by = admin.id
        task.approved_at = datetime.utcnow()
        task.status = 'open'
        msg = 'approved'; ntype = 'success'; emoji = '✅'
    else:
        task.status = 'rejected'
        msg = 'rejected'; ntype = 'error'; emoji = '❌'
    db.session.commit()
    create_notification(task.client_id, f'{emoji} Task {msg.title()}',
                        f'Your task "{task.title}" was {msg}', ntype)
    return jsonify(task=task.to_dict())

@app.route('/api/tasks/<int:tid>/flag', methods=['POST'])
@require_admin
def flag_task(admin, tid):
    task = Task.query.get_or_404(tid)
    d = request.get_json() or {}
    task.is_flagged = not task.is_flagged
    task.flag_reason = d.get('reason', '') if task.is_flagged else None
    db.session.commit()
    return jsonify(task=task.to_dict())

@app.route('/api/tasks/<int:tid>/apply', methods=['POST'])
@require_role('worker')
def apply_task(user, tid):
    task = Task.query.get_or_404(tid)
    if task.status != 'open':
        return jsonify(error='Task not available'), 400
    if task.client_id == user.id:
        return jsonify(error='Cannot apply to own task'), 400
    existing = TaskApplication.query.filter_by(task_id=tid, worker_id=user.id).first()
    if existing:
        return jsonify(error='Already applied'), 409
    d = request.get_json() or {}
    app_obj = TaskApplication(task_id=tid, worker_id=user.id,
                               proposal=d.get('proposal','').strip())
    db.session.add(app_obj)
    db.session.commit()
    create_notification(task.client_id, '📩 New Application',
                        f'{user.username} applied to "{task.title}"', 'info')
    return jsonify(application=app_obj.to_dict()), 201

@app.route('/api/tasks/<int:tid>/applications/<int:aid>/accept', methods=['POST'])
@require_role('client','admin')
def accept_application(user, tid, aid):
    task = Task.query.get_or_404(tid)
    if task.client_id != user.id and user.role != 'admin':
        return jsonify(error='Forbidden'), 403
    app_obj = TaskApplication.query.get_or_404(aid)
    app_obj.status = 'accepted'
    task.status = 'in_progress'
    db.session.commit()
    create_notification(app_obj.worker_id, '🎉 Application Accepted!',
                        f'You got the job: "{task.title}"', 'success')
    return jsonify(application=app_obj.to_dict())

@app.route('/api/tasks/<int:tid>/applications/<int:aid>/submit', methods=['POST'])
@require_role('worker')
def submit_work(user, tid, aid):
    app_obj = TaskApplication.query.get_or_404(aid)
    if app_obj.worker_id != user.id:
        return jsonify(error='Forbidden'), 403
    d = request.get_json() or {}
    app_obj.submission = d.get('submission','')
    app_obj.submitted_at = datetime.utcnow()
    app_obj.status = 'review'
    task = app_obj.task
    task.status = 'review'
    db.session.commit()
    create_notification(task.client_id, '📤 Work Submitted',
                        f'{user.username} submitted work for "{task.title}"', 'info')
    return jsonify(application=app_obj.to_dict())

@app.route('/api/tasks/<int:tid>/applications/<int:aid>/complete', methods=['POST'])
@require_role('client','admin')
def complete_task(user, tid, aid):
    task = Task.query.get_or_404(tid)
    if task.client_id != user.id and user.role != 'admin':
        return jsonify(error='Forbidden'), 403
    app_obj = TaskApplication.query.get_or_404(aid)
    app_obj.status = 'completed'
    task.status = 'completed'

    worker_wallet = Wallet.query.filter_by(user_id=app_obj.worker_id).first()
    if worker_wallet:
        worker_wallet.balance += task.budget
        worker_wallet.total_earned += task.budget
        ledger_entry(worker_wallet, 'release', task.budget,
                     f'Payment for task: {task.title}', str(task.id))
    db.session.commit()
    create_notification(app_obj.worker_id, '💰 Payment Received!',
                        f'KES {task.budget} for "{task.title}"', 'success')
    return jsonify(task=task.to_dict())

@app.route('/api/admin/tasks/pending')
@require_admin
def pending_tasks(admin):
    tasks = Task.query.filter_by(status='pending_approval').order_by(Task.created_at).all()
    return jsonify(tasks=[t.to_dict() for t in tasks])

@app.route('/api/admin/tasks/flagged')
@require_admin
def flagged_tasks(admin):
    tasks = Task.query.filter_by(is_flagged=True).order_by(Task.created_at.desc()).all()
    return jsonify(tasks=[t.to_dict() for t in tasks])

# ─────────────────────────────────────────
# WALLET ROUTES
# ─────────────────────────────────────────

@app.route('/api/wallet')
@require_auth
def get_wallet(user):
    wallet = user.wallet
    ledger = WalletLedger.query.filter_by(wallet_id=wallet.id)\
                               .order_by(WalletLedger.created_at.desc()).limit(30).all()
    return jsonify(wallet=wallet.to_dict(), ledger=[l.to_dict() for l in ledger])

@app.route('/api/wallet/deposit', methods=['POST'])
@require_auth
@limiter.limit("10 per hour")
def deposit(user):
    d = request.get_json() or {}
    amount = float(d.get('amount', 0))
    phone  = d.get('phone', '').strip()
    if amount < 10:
        return jsonify(error='Minimum deposit is KES 10'), 400
    if not re.match(r'^(07|01|\+254)\d{8,9}$', phone):
        return jsonify(error='Invalid phone number'), 400

    reference = f'DTIP-DEP-{uuid.uuid4().hex[:10].upper()}'
    status = 'pending'

    if app.config['DEMO_MODE']:
        # Demo: credit directly
        wallet = user.wallet
        wallet.balance += amount
        wallet.total_earned += amount
        ledger_entry(wallet, 'deposit', amount, f'Demo deposit via M-Pesa', reference)
        status = 'completed'
    else:
        try:
            resp = http_requests.post('https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/',
                headers={'Authorization': f'Bearer {app.config["INTASEND_API_KEY"]}',
                         'Content-Type': 'application/json'},
                json=dict(amount=amount, phone_number=phone,
                          api_ref=reference, currency='KES'), timeout=15)
            if resp.status_code not in (200, 201):
                return jsonify(error='Payment initiation failed'), 502
        except Exception as e:
            return jsonify(error='Payment service unavailable'), 503

    pay = Payment(user_id=user.id, type='deposit', amount=amount,
                  fee=0, net_amount=amount, phone=phone,
                  reference=reference, status=status)
    db.session.add(pay)
    db.session.commit()

    if status == 'completed':
        create_notification(user.id, '💳 Deposit Successful',
                            f'KES {amount:.2f} added to your wallet', 'success')
    return jsonify(payment=pay.to_dict(), demo=app.config['DEMO_MODE'])

@app.route('/api/wallet/withdraw', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def withdraw(user):
    d = request.get_json() or {}
    amount = float(d.get('amount', 0))
    phone  = d.get('phone', '').strip()
    if amount < 50:
        return jsonify(error='Minimum withdrawal is KES 50'), 400
    fee = round(amount * app.config['WITHDRAWAL_FEE_PCT'] / 100, 2)
    net = amount - fee
    wallet = user.wallet
    if wallet.balance < amount:
        return jsonify(error='Insufficient balance'), 400
    wallet.balance -= amount
    wallet.total_spent += amount
    reference = f'DTIP-WIT-{uuid.uuid4().hex[:10].upper()}'
    ledger_entry(wallet, 'withdraw', -amount, f'Withdrawal to {phone}', reference)
    pay = Payment(user_id=user.id, type='withdrawal', amount=amount,
                  fee=fee, net_amount=net, phone=phone,
                  reference=reference, status='pending' if not app.config['DEMO_MODE'] else 'completed')
    db.session.add(pay)
    db.session.commit()
    create_notification(user.id, '💸 Withdrawal Initiated',
                        f'KES {net:.2f} will be sent to {phone}', 'info')
    return jsonify(payment=pay.to_dict())

# ─────────────────────────────────────────
# MESSAGE ROUTES
# ─────────────────────────────────────────

@app.route('/api/messages/<int:other_user_id>')
@require_auth
def get_messages(user, other_user_id):
    other = User.query.get_or_404(other_user_id)
    msgs = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id==user.id, Message.receiver_id==other_user_id),
            db.and_(Message.sender_id==other_user_id, Message.receiver_id==user.id)
        )
    ).order_by(Message.created_at.asc()).limit(100).all()
    # Mark as read
    for m in msgs:
        if m.receiver_id == user.id and not m.is_read:
            m.is_read = True
    db.session.commit()
    return jsonify(messages=[m.to_dict() for m in msgs])

@app.route('/api/messages/conversations')
@require_auth
def conversations(user):
    # Return list of unique conversation partners
    sent_to = db.session.query(Message.receiver_id).filter_by(sender_id=user.id, is_broadcast=False).distinct()
    recv_from = db.session.query(Message.sender_id).filter_by(receiver_id=user.id, is_broadcast=False).distinct()
    user_ids = set([r[0] for r in sent_to] + [r[0] for r in recv_from])
    user_ids.discard(user.id)
    convs = []
    for uid in user_ids:
        u = User.query.get(uid)
        if u:
            last_msg = Message.query.filter(
                db.or_(
                    db.and_(Message.sender_id==user.id, Message.receiver_id==uid),
                    db.and_(Message.sender_id==uid, Message.receiver_id==user.id)
                )
            ).order_by(Message.created_at.desc()).first()
            unread = Message.query.filter_by(sender_id=uid, receiver_id=user.id, is_read=False).count()
            convs.append(dict(user=u.to_dict(), last_message=last_msg.to_dict() if last_msg else None, unread=unread))
    convs.sort(key=lambda x: x['last_message']['created_at'] if x['last_message'] else '', reverse=True)
    return jsonify(conversations=convs)

@app.route('/api/messages/broadcast')
@require_auth
def get_broadcasts(user):
    msgs = Message.query.filter_by(is_broadcast=True)\
                        .order_by(Message.created_at.desc()).limit(50).all()
    return jsonify(messages=[m.to_dict() for m in msgs])

@app.route('/api/admin/broadcast', methods=['POST'])
@require_admin
def broadcast(admin):
    d = request.get_json() or {}
    text = d.get('message','').strip()
    if not text:
        return jsonify(error='Message required'), 400
    msg = Message(sender_id=admin.id, receiver_id=None,
                  message=text, is_broadcast=True)
    db.session.add(msg)
    db.session.flush()
    users = User.query.filter_by(is_active=True).all()
    for u in users:
        create_notification(u.id, '📢 Announcement', text, 'info')
    db.session.commit()
    socketio.emit('broadcast', msg.to_dict(), broadcast=True)
    return jsonify(message=msg.to_dict())

# ─────────────────────────────────────────
# NOTIFICATION ROUTES
# ─────────────────────────────────────────

@app.route('/api/notifications')
@require_auth
def get_notifications(user):
    notifs = Notification.query.filter_by(user_id=user.id)\
                               .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify(notifications=[n.to_dict() for n in notifs],
                   unread=sum(1 for n in notifs if not n.is_read))

@app.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def read_notifications(user):
    Notification.query.filter_by(user_id=user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify(ok=True)

# ─────────────────────────────────────────
# ADMIN STATS
# ─────────────────────────────────────────

@app.route('/api/admin/stats')
@require_admin
@cache.cached(timeout=60, key_prefix='admin_stats')
def admin_stats(admin):
    total_users   = User.query.count()
    total_tasks   = Task.query.count()
    pending_tasks = Task.query.filter_by(status='pending_approval').count()
    open_tasks    = Task.query.filter_by(status='open').count()
    total_wallets = db.session.query(db.func.sum(Wallet.balance)).scalar() or 0
    total_payments= Payment.query.filter_by(status='completed').count()
    new_users_today = User.query.filter(User.created_at >= datetime.utcnow().replace(hour=0,minute=0,second=0)).count()
    return jsonify(total_users=total_users, total_tasks=total_tasks,
                   pending_tasks=pending_tasks, open_tasks=open_tasks,
                   total_wallet_balance=round(float(total_wallets),2),
                   total_payments=total_payments, new_users_today=new_users_today)

# ─────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify(status='ok', version='2.0.0', timestamp=datetime.utcnow().isoformat())

@app.route('/api/stats')
@cache.cached(timeout=300)
def public_stats():
    return jsonify(
        users=User.query.filter_by(is_active=True).count(),
        tasks=Task.query.filter_by(status='open').count(),
        completed=Task.query.filter_by(status='completed').count(),
        categories=db.session.query(Task.category).distinct().count()
    )

# ─────────────────────────────────────────
# SOCKETIO EVENTS
# ─────────────────────────────────────────

def socket_auth():
    token = request.args.get('token', '')
    if not token:
        return None
    try:
        data = decode_token(token)
        return User.query.get(data['sub'])
    except:
        return None

@socketio.on('connect')
def on_connect():
    user = socket_auth()
    if not user:
        disconnect()
        return False
    join_room(f'user_{user.id}')
    if user.role == 'admin':
        join_room('admins')
    emit('connected', {'user_id': user.id, 'username': user.username})
    logger.info(f'Socket connected: {user.username}')

@socketio.on('disconnect')
def on_disconnect():
    logger.info('Socket disconnected')

@socketio.on('send_message')
def on_send_message(data):
    user = socket_auth()
    if not user: return
    receiver_id = data.get('receiver_id')
    text = (data.get('message') or '').strip()
    if not text: return

    # Non-admin can only message admin
    if user.role != 'admin':
        admin = User.query.filter_by(role='admin').first()
        if not admin: return
        receiver_id = admin.id

    receiver = User.query.get(receiver_id) if receiver_id else None
    if not receiver: return

    msg = Message(sender_id=user.id, receiver_id=receiver_id, message=text)
    db.session.add(msg)
    db.session.commit()

    msg_data = msg.to_dict()
    emit('receive_message', msg_data, room=f'user_{receiver_id}')
    emit('receive_message', msg_data, room=f'user_{user.id}')

    create_notification(receiver_id, f'💬 New message from {user.username}',
                        text[:100], 'message')

@socketio.on('mark_as_read')
def on_mark_read(data):
    user = socket_auth()
    if not user: return
    sender_id = data.get('sender_id')
    Message.query.filter_by(sender_id=sender_id, receiver_id=user.id, is_read=False)\
                 .update({'is_read': True})
    db.session.commit()

@socketio.on('admin_broadcast')
def on_admin_broadcast(data):
    user = socket_auth()
    if not user or user.role != 'admin': return
    text = (data.get('message') or '').strip()
    if not text: return
    msg = Message(sender_id=user.id, message=text, is_broadcast=True)
    db.session.add(msg)
    db.session.commit()
    emit('broadcast', msg.to_dict(), broadcast=True)

# ─────────────────────────────────────────
# FRONTEND — SINGLE PAGE APP
# ─────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DTIP v2 — Digital Tasks & Earning Platform</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
:root {
  --bg: #0a0b0f;
  --surface: #111318;
  --surface2: #181c24;
  --surface3: #1e2230;
  --border: #252a38;
  --accent: #00e5ff;
  --accent2: #7c3aed;
  --gold: #f59e0b;
  --green: #10b981;
  --red: #ef4444;
  --orange: #f97316;
  --text: #e8eaf0;
  --muted: #6b7280;
  --card-glow: 0 0 30px rgba(0,229,255,0.04);
  --font-display: 'Syne', sans-serif;
  --font-body: 'DM Sans', sans-serif;
  --radius: 12px;
  --radius-lg: 20px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font-body);font-size:14px;min-height:100vh;overflow-x:hidden}
a{color:var(--accent);text-decoration:none}
input,textarea,select{background:var(--surface3);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 14px;font-family:var(--font-body);font-size:14px;width:100%;outline:none;transition:.2s}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,229,255,.1)}
button{cursor:pointer;font-family:var(--font-body);border:none;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:500;transition:.2s}
.btn{background:var(--accent);color:#000;font-weight:700}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn-danger{background:var(--red);color:#fff}
.btn-success{background:var(--green);color:#fff}
.btn-sm{padding:6px 14px;font-size:12px}
/* LAYOUT */
.app{display:flex;height:100vh}
.sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;z-index:100}
.logo{padding:24px 20px 16px;border-bottom:1px solid var(--border)}
.logo h1{font-family:var(--font-display);font-size:20px;font-weight:800;letter-spacing:-.5px}
.logo span{color:var(--accent)}
.logo small{display:block;font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-top:2px}
.nav{flex:1;padding:16px 12px;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;color:var(--muted);margin-bottom:2px;transition:.15s;font-size:13px;font-weight:500}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:rgba(0,229,255,.08);color:var(--accent)}
.nav-item .icon{font-size:16px;width:20px;text-align:center}
.nav-item .badge{margin-left:auto;background:var(--red);color:#fff;border-radius:20px;padding:1px 7px;font-size:10px;font-weight:700}
.nav-section{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;padding:12px 12px 6px;margin-top:8px}
.user-panel{padding:12px;border-top:1px solid var(--border)}
.user-info{display:flex;align-items:center;gap:10px;padding:10px;background:var(--surface2);border-radius:10px}
.avatar{width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,var(--accent2),var(--accent));display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex-shrink:0;overflow:hidden}
.avatar img{width:100%;height:100%;object-fit:cover}
.user-meta{min-width:0}
.user-name{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-role{font-size:10px;color:var(--muted);text-transform:capitalize}
/* MAIN */
.main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}
.topbar{display:flex;align-items:center;gap:12px;padding:14px 24px;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.topbar-title{font-family:var(--font-display);font-weight:700;font-size:18px}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.notif-btn{position:relative;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:16px;cursor:pointer}
.notif-dot{position:absolute;top:4px;right:4px;width:8px;height:8px;background:var(--red);border-radius:50%;border:2px solid var(--surface)}
.content{flex:1;overflow-y:auto;padding:24px}
/* CARDS */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px;box-shadow:var(--card-glow)}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-title{font-family:var(--font-display);font-weight:700;font-size:15px}
/* STATS GRID */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;right:0;width:80px;height:80px;border-radius:50%;filter:blur(40px);opacity:.3}
.stat-card.blue::before{background:var(--accent)}
.stat-card.purple::before{background:var(--accent2)}
.stat-card.gold::before{background:var(--gold)}
.stat-card.green::before{background:var(--green)}
.stat-icon{font-size:24px;margin-bottom:10px}
.stat-value{font-family:var(--font-display);font-size:28px;font-weight:800;line-height:1}
.stat-label{font-size:12px;color:var(--muted);margin-top:4px}
/* TASKS */
.task-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.task-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px;transition:.2s;cursor:pointer}
.task-card:hover{border-color:var(--accent);box-shadow:0 0 20px rgba(0,229,255,.06)}
.task-header{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:10px}
.task-title{font-weight:700;font-size:14px;line-height:1.3}
.task-badge{padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;flex-shrink:0}
.badge-open{background:rgba(16,185,129,.15);color:var(--green)}
.badge-pending{background:rgba(245,158,11,.15);color:var(--gold)}
.badge-rejected{background:rgba(239,68,68,.15);color:var(--red)}
.badge-review{background:rgba(124,58,237,.15);color:#a78bfa}
.badge-completed{background:rgba(0,229,255,.1);color:var(--accent)}
.badge-progress{background:rgba(249,115,22,.15);color:var(--orange)}
.task-desc{font-size:12px;color:var(--muted);line-height:1.5;margin-bottom:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.task-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.task-cat{font-size:11px;color:var(--muted);background:var(--surface2);padding:3px 8px;border-radius:20px}
.task-budget{font-family:var(--font-display);font-weight:700;color:var(--accent);font-size:14px;margin-left:auto}
.task-footer{display:flex;align-items:center;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.task-client{font-size:11px;color:var(--muted)}
.verified-badge{color:var(--accent);font-size:12px}
.flag-badge{color:var(--red);font-size:12px;margin-left:4px}
/* TABLE */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 14px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:12px 14px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.01)}
/* MODAL */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:1000;display:none;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:28px;width:90%;max-width:540px;max-height:90vh;overflow-y:auto}
.modal-title{font-family:var(--font-display);font-weight:700;font-size:18px;margin-bottom:20px}
.form-group{margin-bottom:16px}
.form-label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:500}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
/* CHAT */
.chat-wrap{display:flex;height:calc(100vh - 110px);gap:0}
.chat-list{width:260px;border-right:1px solid var(--border);overflow-y:auto;flex-shrink:0}
.chat-list-header{padding:16px;border-bottom:1px solid var(--border);font-family:var(--font-display);font-weight:700;font-size:14px}
.conv-item{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--border);transition:.15s}
.conv-item:hover,.conv-item.active{background:var(--surface2)}
.conv-meta{min-width:0;flex:1}
.conv-name{font-weight:600;font-size:13px}
.conv-preview{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv-unread{background:var(--accent);color:#000;border-radius:20px;padding:1px 7px;font-size:10px;font-weight:700;margin-left:auto}
.chat-main{flex:1;display:flex;flex-direction:column;min-width:0}
.chat-header{padding:14px 20px;border-bottom:1px solid var(--border);font-weight:700;display:flex;align-items:center;gap:10px}
.chat-messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.msg-bubble{max-width:70%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5}
.msg-bubble.sent{background:var(--accent);color:#000;border-bottom-right-radius:4px;align-self:flex-end}
.msg-bubble.recv{background:var(--surface3);border-bottom-left-radius:4px;align-self:flex-start}
.msg-time{font-size:10px;opacity:.6;margin-top:4px}
.msg-broadcast{background:rgba(124,58,237,.12);border:1px solid rgba(124,58,237,.2);border-radius:10px;padding:12px;font-size:13px;text-align:center}
.msg-broadcast .broadcast-badge{font-size:10px;font-weight:700;color:#a78bfa;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.chat-input-area{padding:16px;border-top:1px solid var(--border);display:flex;gap:10px}
.chat-input-area input{flex:1}
/* AUTH */
.auth-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--bg)}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:24px;padding:40px;width:380px}
.auth-logo{text-align:center;margin-bottom:28px}
.auth-logo h1{font-family:var(--font-display);font-size:28px;font-weight:800}
.auth-logo span{color:var(--accent)}
.auth-logo p{color:var(--muted);font-size:13px;margin-top:4px}
.auth-tabs{display:flex;background:var(--surface2);border-radius:10px;padding:4px;margin-bottom:24px}
.auth-tab{flex:1;padding:8px;border-radius:7px;text-align:center;cursor:pointer;font-weight:600;font-size:13px;color:var(--muted);transition:.15s}
.auth-tab.active{background:var(--accent);color:#000}
.google-btn{display:flex;align-items:center;justify-content:center;gap:10px;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:11px;width:100%;font-weight:600;font-size:13px;margin-bottom:16px;cursor:pointer;transition:.2s}
.google-btn:hover{border-color:var(--accent)}
.divider{display:flex;align-items:center;gap:12px;margin-bottom:16px;color:var(--muted);font-size:12px}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:var(--border)}
/* WALLET */
.wallet-hero{background:linear-gradient(135deg,var(--accent2),#1d4ed8);border-radius:20px;padding:28px;margin-bottom:20px;position:relative;overflow:hidden}
.wallet-hero::after{content:'';position:absolute;top:-30px;right:-30px;width:150px;height:150px;border-radius:50%;background:rgba(255,255,255,.06)}
.wallet-balance{font-family:var(--font-display);font-size:42px;font-weight:800}
.wallet-label{font-size:12px;opacity:.7;letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}
.wallet-actions{display:flex;gap:10px;margin-top:20px}
.wallet-action-btn{flex:1;background:rgba(255,255,255,.15);color:#fff;border-radius:10px;padding:12px;font-weight:600;font-size:13px;backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.2)}
.wallet-action-btn:hover{background:rgba(255,255,255,.25)}
/* TOAST */
.toast-container{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:10px}
.toast{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 16px;min-width:260px;font-size:13px;animation:slideIn .2s ease;display:flex;align-items:center;gap:10px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.toast.success{border-left:3px solid var(--green)}
.toast.error{border-left:3px solid var(--red)}
.toast.info{border-left:3px solid var(--accent)}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
/* BROADCAST BANNER */
.broadcast-bar{background:rgba(124,58,237,.15);border-bottom:1px solid rgba(124,58,237,.25);padding:10px 24px;font-size:13px;display:none;align-items:center;gap:10px}
.broadcast-bar.show{display:flex}
/* MISC */
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-icon{font-size:40px;margin-bottom:12px}
.spinner{width:32px;height:32px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;margin:40px auto}
@keyframes spin{to{transform:rotate(360deg)}}
.pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
.pill-gold{background:rgba(245,158,11,.15);color:var(--gold)}
.pill-blue{background:rgba(0,229,255,.1);color:var(--accent)}
.pill-green{background:rgba(16,185,129,.15);color:var(--green)}
.search-bar{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.search-bar input{max-width:300px}
.search-bar select{max-width:160px}
.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:0}
.tab{padding:10px 18px;cursor:pointer;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;transition:.15s;font-weight:500;margin-bottom:-1px}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:768px){
  .sidebar{width:60px}
  .logo h1,.logo small,.nav-item span,.user-meta,.nav-section{display:none}
  .two-col{grid-template-columns:1fr}
  .form-row{grid-template-columns:1fr}
  .task-grid{grid-template-columns:1fr}
  .chat-list{width:60px}
  .conv-meta{display:none}
  .topbar-title{font-size:15px}
}
</style>
</head>
<body>
<div class="toast-container" id="toasts"></div>

<!-- AUTH SCREEN -->
<div id="authScreen" class="auth-wrap" style="display:none">
  <div class="auth-card">
    <div class="auth-logo">
      <h1>D<span>TIP</span></h1>
      <p>Kenya's Digital Task Marketplace</p>
    </div>
    <div class="auth-tabs">
      <div class="auth-tab active" id="loginTab" onclick="switchTab('login')">Sign In</div>
      <div class="auth-tab" id="registerTab" onclick="switchTab('register')">Register</div>
    </div>
    <!-- Google -->
    <button class="google-btn" onclick="googleLogin()">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.707c-.18-.54-.282-1.117-.282-1.707s.102-1.167.282-1.707V4.961H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.039l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.961L3.964 6.293C4.672 4.166 6.656 3.58 9 3.58z"/></svg>
      Continue with Google
    </button>
    <div class="divider">or</div>
    <div id="loginForm">
      <div class="form-group"><label class="form-label">Email</label><input type="email" id="loginEmail" placeholder="you@example.com"></div>
      <div class="form-group"><label class="form-label">Password</label><input type="password" id="loginPassword" placeholder="••••••••"></div>
      <button class="btn" style="width:100%" onclick="doLogin()">Sign In</button>
    </div>
    <div id="registerForm" style="display:none">
      <div class="form-row">
        <div class="form-group"><label class="form-label">Username</label><input type="text" id="regUsername" placeholder="johndoe"></div>
        <div class="form-group"><label class="form-label">Role</label>
          <select id="regRole"><option value="worker">Worker</option><option value="client">Client</option></select></div>
      </div>
      <div class="form-group"><label class="form-label">Email</label><input type="email" id="regEmail" placeholder="you@example.com"></div>
      <div class="form-group"><label class="form-label">Password</label><input type="password" id="regPassword" placeholder="Min 8 characters"></div>
      <div class="form-group"><label class="form-label">Referral Code (optional)</label><input type="text" id="regReferral" placeholder="XXXXXXXX"></div>
      <button class="btn" style="width:100%" onclick="doRegister()">Create Account</button>
    </div>
  </div>
</div>

<!-- MAIN APP -->
<div id="appScreen" class="app" style="display:none">
  <div class="sidebar">
    <div class="logo"><h1>D<span>TIP</span></h1><small>v2.0 Kenya</small></div>
    <nav class="nav">
      <div class="nav-item active" onclick="showPage('dashboard')" id="nav-dashboard">
        <span class="icon">🏠</span><span>Dashboard</span></div>
      <div class="nav-item" onclick="showPage('tasks')" id="nav-tasks">
        <span class="icon">📋</span><span>Tasks</span></div>
      <div class="nav-item" onclick="showPage('wallet')" id="nav-wallet">
        <span class="icon">💰</span><span>Wallet</span></div>
      <div class="nav-item" onclick="showPage('messages')" id="nav-messages">
        <span class="icon">💬</span><span>Messages</span>
        <span class="badge" id="msgBadge" style="display:none">0</span></div>
      <div class="nav-item" onclick="showPage('notifications')" id="nav-notifications">
        <span class="icon">🔔</span><span>Alerts</span>
        <span class="badge" id="notifBadge" style="display:none">0</span></div>
      <div id="adminNav" style="display:none">
        <div class="nav-section">Admin</div>
        <div class="nav-item" onclick="showPage('admin')" id="nav-admin">
          <span class="icon">⚙️</span><span>Admin Panel</span></div>
        <div class="nav-item" onclick="showPage('adminUsers')" id="nav-adminUsers">
          <span class="icon">👥</span><span>Users</span></div>
      </div>
    </nav>
    <div class="user-panel">
      <div class="user-info">
        <div class="avatar" id="sidebarAvatar"></div>
        <div class="user-meta">
          <div class="user-name" id="sidebarName">—</div>
          <div class="user-role" id="sidebarRole">—</div>
        </div>
      </div>
      <button class="btn-ghost" style="width:100%;margin-top:8px;font-size:12px" onclick="logout()">Sign Out</button>
    </div>
  </div>

  <div class="main">
    <div class="broadcast-bar" id="broadcastBar">
      <span>📢</span><span id="broadcastText"></span>
      <button class="btn-ghost btn-sm" style="margin-left:auto" onclick="document.getElementById('broadcastBar').classList.remove('show')">✕</button>
    </div>
    <div class="topbar">
      <div class="topbar-title" id="pageTitle">Dashboard</div>
      <div class="topbar-right">
        <button class="notif-btn" onclick="showPage('notifications')">
          🔔<span class="notif-dot" id="notifDot" style="display:none"></span>
        </button>
        <span id="topbarUser" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>

    <div class="content" id="pageContent">
      <div class="spinner"></div>
    </div>
  </div>
</div>

<!-- MODALS -->
<div class="modal-overlay" id="taskModal">
  <div class="modal">
    <div class="modal-title">📋 Task Details</div>
    <div id="taskModalContent"></div>
    <div style="margin-top:20px;display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-ghost" onclick="closeModal('taskModal')">Close</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="postTaskModal">
  <div class="modal">
    <div class="modal-title">➕ Post New Task</div>
    <div class="form-group"><label class="form-label">Title</label><input type="text" id="taskTitle" placeholder="What do you need done?"></div>
    <div class="form-group"><label class="form-label">Description</label><textarea id="taskDesc" rows="4" placeholder="Describe the task in detail..."></textarea></div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">Category</label>
        <select id="taskCategory">
          <option>Data Entry</option><option>Writing</option><option>Design</option>
          <option>Social Media</option><option>Research</option><option>Tech</option>
          <option>Marketing</option><option>Other</option>
        </select></div>
      <div class="form-group"><label class="form-label">Budget (KES)</label><input type="number" id="taskBudget" placeholder="500" min="10"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">Slots</label><input type="number" id="taskSlots" value="1" min="1"></div>
      <div class="form-group"><label class="form-label">Deadline</label><input type="datetime-local" id="taskDeadline"></div>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">
      <button class="btn-ghost" onclick="closeModal('postTaskModal')">Cancel</button>
      <button class="btn" onclick="submitTask()">Submit for Review</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="depositModal">
  <div class="modal">
    <div class="modal-title">💳 Deposit via M-Pesa</div>
    <div class="form-group"><label class="form-label">Amount (KES)</label><input type="number" id="depAmount" placeholder="100" min="10"></div>
    <div class="form-group"><label class="form-label">Phone Number</label><input type="tel" id="depPhone" placeholder="0712345678"></div>
    <p style="font-size:12px;color:var(--muted);margin-bottom:16px" id="demoNote"></p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-ghost" onclick="closeModal('depositModal')">Cancel</button>
      <button class="btn btn-success" onclick="doDeposit()">Deposit</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="withdrawModal">
  <div class="modal">
    <div class="modal-title">💸 Withdraw to M-Pesa</div>
    <div class="form-group"><label class="form-label">Amount (KES)</label><input type="number" id="witAmount" placeholder="100" min="50"></div>
    <div class="form-group"><label class="form-label">Phone Number</label><input type="tel" id="witPhone" placeholder="0712345678"></div>
    <p style="font-size:12px;color:var(--muted);margin-bottom:16px">Fee: 5% • Minimum: KES 50</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-ghost" onclick="closeModal('withdrawModal')">Cancel</button>
      <button class="btn" onclick="doWithdraw()">Withdraw</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="applyModal">
  <div class="modal">
    <div class="modal-title">✉️ Apply for Task</div>
    <input type="hidden" id="applyTaskId">
    <div class="form-group"><label class="form-label">Your Proposal</label>
      <textarea id="applyProposal" rows="5" placeholder="Explain why you're the best fit..."></textarea></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-ghost" onclick="closeModal('applyModal')">Cancel</button>
      <button class="btn" onclick="submitApplication()">Send Application</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="broadcastModal">
  <div class="modal">
    <div class="modal-title">📢 Send Broadcast</div>
    <div class="form-group"><label class="form-label">Message</label>
      <textarea id="broadcastMsg" rows="4" placeholder="Message to all users..."></textarea></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-ghost" onclick="closeModal('broadcastModal')">Cancel</button>
      <button class="btn" onclick="sendBroadcast()">Send to All</button>
    </div>
  </div>
</div>

<script>
// ── STATE ──
const S = {token:null, user:null, socket:null, activePage:'dashboard',
            activeChat:null, chatUser:null, unreadNotif:0, unreadMsg:0};

// ── INIT ──
window.addEventListener('DOMContentLoaded', async () => {
  const urlToken = new URLSearchParams(location.search).get('token');
  if(urlToken){ localStorage.setItem('token', urlToken); history.replaceState({}, '', '/'); }
  S.token = localStorage.getItem('token');
  if(S.token){
    try {
      const r = await api('/api/auth/me');
      S.user = r.user;
      showApp();
    } catch { showAuth(); }
  } else { showAuth(); }
});

// ── API ──
async function api(url, method='GET', body=null){
  const opts = { method, headers:{'Content-Type':'application/json'} };
  if(S.token) opts.headers['Authorization'] = 'Bearer ' + S.token;
  if(body) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  const d = await r.json();
  if(!r.ok) throw new Error(d.error || 'Request failed');
  return d;
}

function toast(msg, type='info'){
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${type==='success'?'✅':type==='error'?'❌':'ℹ️'}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>el.remove(), 4000);
}

// ── AUTH ──
function showAuth(){ document.getElementById('authScreen').style.display='flex'; document.getElementById('appScreen').style.display='none'; }
function showApp(){
  document.getElementById('authScreen').style.display='none';
  document.getElementById('appScreen').style.display='flex';
  initApp();
}

function switchTab(t){
  document.getElementById('loginForm').style.display = t==='login'?'block':'none';
  document.getElementById('registerForm').style.display = t==='register'?'block':'none';
  document.getElementById('loginTab').className = 'auth-tab'+(t==='login'?' active':'');
  document.getElementById('registerTab').className = 'auth-tab'+(t==='register'?' active':'');
}

function googleLogin(){ window.location.href = '/auth/google'; }

async function doLogin(){
  const email = document.getElementById('loginEmail').value;
  const password = document.getElementById('loginPassword').value;
  try {
    const r = await api('/api/auth/login','POST',{email,password});
    S.token = r.token; S.user = r.user;
    localStorage.setItem('token', S.token);
    showApp();
  } catch(e){ toast(e.message,'error'); }
}

async function doRegister(){
  const data = {
    username: document.getElementById('regUsername').value,
    email: document.getElementById('regEmail').value,
    password: document.getElementById('regPassword').value,
    role: document.getElementById('regRole').value,
    referral_code: document.getElementById('regReferral').value
  };
  try {
    const r = await api('/api/auth/register','POST',data);
    S.token = r.token; S.user = r.user;
    localStorage.setItem('token', S.token);
    showApp();
    toast('Account created! Welcome to DTIP.','success');
  } catch(e){ toast(e.message,'error'); }
}

function logout(){
  S.token = null; S.user = null;
  localStorage.removeItem('token');
  if(S.socket) S.socket.disconnect();
  showAuth();
}

// ── APP INIT ──
function initApp(){
  const u = S.user;
  document.getElementById('sidebarName').textContent = u.username;
  document.getElementById('sidebarRole').textContent = u.role + (u.is_verified?' ✔':'');
  document.getElementById('topbarUser').textContent = u.email;
  const av = document.getElementById('sidebarAvatar');
  if(u.avatar_url){ av.innerHTML = `<img src="${u.avatar_url}" alt="">`; }
  else { av.textContent = u.username[0].toUpperCase(); }
  if(u.role==='admin') document.getElementById('adminNav').style.display='block';
  initSocket();
  loadNotifCount();
  showPage('dashboard');
}

function initSocket(){
  S.socket = io({ auth:{ token: S.token }, query:{token: S.token} });
  S.socket.on('connected', d => console.log('Socket connected:', d));
  S.socket.on('receive_message', msg => {
    if(S.activePage==='messages' && S.activeChat===msg.sender_id){
      appendMessage(msg, false);
    } else {
      S.unreadMsg++;
      updateMsgBadge();
      toast(`💬 New message from ${msg.sender_name}`,'info');
    }
  });
  S.socket.on('broadcast', msg => {
    const bar = document.getElementById('broadcastBar');
    document.getElementById('broadcastText').textContent = msg.message;
    bar.classList.add('show');
    toast('📢 Broadcast: ' + msg.message.substring(0,60),'info');
  });
  S.socket.on('notification', n => {
    S.unreadNotif++;
    updateNotifBadge();
    toast(n.title + ': ' + n.body.substring(0,60), n.type||'info');
  });
}

function updateNotifBadge(){
  const b = document.getElementById('notifBadge');
  const d = document.getElementById('notifDot');
  b.textContent = S.unreadNotif;
  b.style.display = S.unreadNotif>0?'block':'none';
  d.style.display = S.unreadNotif>0?'block':'none';
}
function updateMsgBadge(){
  const b = document.getElementById('msgBadge');
  b.textContent = S.unreadMsg;
  b.style.display = S.unreadMsg>0?'block':'none';
}
async function loadNotifCount(){
  try {
    const r = await api('/api/notifications');
    S.unreadNotif = r.unread;
    updateNotifBadge();
  } catch{}
}

// ── NAVIGATION ──
function showPage(page){
  S.activePage = page;
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const navEl = document.getElementById('nav-'+page);
  if(navEl) navEl.classList.add('active');
  const titles = {dashboard:'Dashboard',tasks:'Task Marketplace',wallet:'Wallet',
    messages:'Messages',notifications:'Notifications',admin:'Admin Panel',adminUsers:'User Management'};
  document.getElementById('pageTitle').textContent = titles[page]||page;
  const content = document.getElementById('pageContent');
  content.innerHTML = '<div class="spinner"></div>';
  ({dashboard,tasks,wallet,messages,notifications,admin,adminUsers})[page]?.();
}

function openModal(id){ document.getElementById(id).classList.add('show'); }
function closeModal(id){ document.getElementById(id).classList.remove('show'); }

// ── PAGES ──
async function dashboard(){
  const content = document.getElementById('pageContent');
  const [me, pubStats] = await Promise.all([
    api('/api/auth/me'), api('/api/stats')
  ]);
  const wallet = me.wallet;
  let adminStats = null;
  if(S.user.role==='admin'){ try{ adminStats = await api('/api/admin/stats'); }catch{} }

  let html = `<div class="stats-grid">
    <div class="stat-card blue"><div class="stat-icon">💰</div>
      <div class="stat-value">KES ${(wallet?.balance||0).toFixed(2)}</div>
      <div class="stat-label">Wallet Balance</div></div>
    <div class="stat-card green"><div class="stat-icon">📋</div>
      <div class="stat-value">${pubStats.tasks}</div>
      <div class="stat-label">Open Tasks</div></div>
    <div class="stat-card purple"><div class="stat-icon">✅</div>
      <div class="stat-value">${pubStats.completed}</div>
      <div class="stat-label">Completed Tasks</div></div>
    <div class="stat-card gold"><div class="stat-icon">👥</div>
      <div class="stat-value">${pubStats.users}</div>
      <div class="stat-label">Active Users</div></div>
  </div>`;

  if(adminStats){
    html += `<div class="stats-grid">
      <div class="stat-card"><div class="stat-icon">⏳</div>
        <div class="stat-value">${adminStats.pending_tasks}</div>
        <div class="stat-label">Pending Approval</div></div>
      <div class="stat-card"><div class="stat-icon">💸</div>
        <div class="stat-value">KES ${adminStats.total_wallet_balance.toLocaleString()}</div>
        <div class="stat-label">Platform Wallet Balance</div></div>
      <div class="stat-card"><div class="stat-icon">🆕</div>
        <div class="stat-value">${adminStats.new_users_today}</div>
        <div class="stat-label">New Users Today</div></div>
    </div>`;
  }

  html += `<div class="two-col"><div>
    <div class="card">
      <div class="card-header"><div class="card-title">Quick Actions</div></div>
      <div style="display:flex;flex-direction:column;gap:10px">
        ${S.user.role!=='worker'?`<button class="btn" onclick="openModal('postTaskModal')">➕ Post a Task</button>`:''}
        <button class="btn-ghost" onclick="showPage('tasks')">🔍 Browse Tasks</button>
        <button class="btn-ghost" onclick="showPage('wallet')">💰 Manage Wallet</button>
        <button class="btn-ghost" onclick="showPage('messages')">💬 Messages</button>
        ${S.user.role==='admin'?`<button class="btn-ghost" onclick="showPage('admin')">⚙️ Admin Panel</button>`:''}
      </div>
    </div>
  </div><div>
    <div class="card">
      <div class="card-header"><div class="card-title">Your Info</div></div>
      <table>
        <tr><td style="color:var(--muted)">Username</td><td><strong>${S.user.username}</strong></td></tr>
        <tr><td style="color:var(--muted)">Role</td><td><strong>${S.user.role}</strong> ${S.user.is_verified?'<span class="verified-badge">✔ Verified</span>':''}</td></tr>
        <tr><td style="color:var(--muted)">Tier</td><td><span class="pill pill-${S.user.tier==='gold'?'gold':S.user.tier==='diamond'?'blue':'green'}">${S.user.tier}</span></td></tr>
        <tr><td style="color:var(--muted)">Referral Code</td><td><code style="color:var(--accent)">${S.user.referral_code||'—'}</code></td></tr>
        <tr><td style="color:var(--muted)">Escrow</td><td>KES ${(wallet?.escrow||0).toFixed(2)}</td></tr>
        <tr><td style="color:var(--muted)">Total Earned</td><td>KES ${(wallet?.total_earned||0).toFixed(2)}</td></tr>
      </table>
    </div>
  </div></div>`;

  content.innerHTML = html;
}

async function tasks(){
  const content = document.getElementById('pageContent');
  content.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
      <div class="search-bar" style="margin-bottom:0;flex:1">
        <input type="text" id="taskSearch" placeholder="Search tasks..." onkeyup="filterTasks()">
        <select id="taskCatFilter" onchange="filterTasks()">
          <option value="">All Categories</option>
          <option>Data Entry</option><option>Writing</option><option>Design</option>
          <option>Social Media</option><option>Research</option><option>Tech</option>
          <option>Marketing</option><option>Other</option>
        </select>
        <select id="taskSort" onchange="filterTasks()">
          <option value="newest">Newest</option>
          <option value="budget_high">Budget: High→Low</option>
          <option value="budget_low">Budget: Low→High</option>
        </select>
      </div>
      ${S.user.role!=='worker'?`<button class="btn" style="margin-left:12px" onclick="openModal('postTaskModal')">➕ Post Task</button>`:''}
    </div>
    <div class="task-grid" id="taskGrid"><div class="spinner"></div></div>
    <div style="text-align:center;margin-top:20px" id="taskPager"></div>
  `;
  loadTasks(1);
}

let taskPage = 1;
async function loadTasks(page=1){
  taskPage = page;
  const q = document.getElementById('taskSearch')?.value||'';
  const cat = document.getElementById('taskCatFilter')?.value||'';
  const sort = document.getElementById('taskSort')?.value||'newest';
  try {
    const r = await api(`/api/tasks?page=${page}&q=${encodeURIComponent(q)}&category=${encodeURIComponent(cat)}&sort=${sort}`);
    const grid = document.getElementById('taskGrid');
    if(!r.tasks.length){ grid.innerHTML = '<div class="empty"><div class="empty-icon">📭</div><p>No tasks found</p></div>'; return; }
    grid.innerHTML = r.tasks.map(t => taskCard(t)).join('');
    const pager = document.getElementById('taskPager');
    if(r.pages>1){
      pager.innerHTML = Array.from({length:r.pages},(_,i)=>
        `<button class="${i+1===page?'btn':'btn-ghost'} btn-sm" style="margin:2px" onclick="loadTasks(${i+1})">${i+1}</button>`
      ).join('');
    }
  } catch(e){ toast(e.message,'error'); }
}

function filterTasks(){ loadTasks(1); }

function taskCard(t){
  const statusClass = {open:'open',pending_approval:'pending',rejected:'rejected',
    in_progress:'progress',review:'review',completed:'completed'}[t.status]||'pending';
  const flagged = t.is_flagged ? '<span class="flag-badge" title="'+t.flag_reason+'">🚩</span>':'';
  return `<div class="task-card" onclick="viewTask(${t.id})">
    <div class="task-header">
      <div class="task-title">${esc(t.title)}</div>
      <span class="task-badge badge-${statusClass}">${t.status.replace('_',' ')}</span>
    </div>
    <div class="task-desc">${esc(t.description)}</div>
    <div class="task-meta">
      <span class="task-cat">${esc(t.category)}</span>
      ${t.slots>1?`<span class="task-cat">👥 ${t.slots} slots</span>`:''}
      <span class="task-budget">KES ${t.budget.toLocaleString()}</span>
    </div>
    <div class="task-footer">
      <span class="task-client">by ${esc(t.client_name||'—')}
        ${t.client_verified?'<span class="verified-badge">✔</span>':''}
        ${flagged}
      </span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)">${t.application_count} applied</span>
    </div>
  </div>`;
}

async function viewTask(id){
  try {
    const r = await api('/api/tasks/'+id);
    const t = r.task;
    const isAdmin = S.user.role==='admin';
    const isClient = S.user.id===t.client_id;
    const hasApplied = t.applications?.some(a=>a.worker_id===S.user.id);
    const myApp = t.applications?.find(a=>a.worker_id===S.user.id);
    const statusClass = {open:'open',pending_approval:'pending',rejected:'rejected',
      in_progress:'progress',review:'review',completed:'completed'}[t.status]||'pending';

    let appsHtml = '';
    if((isClient||isAdmin) && t.applications?.length){
      appsHtml = `<div style="margin-top:16px"><div style="font-weight:700;margin-bottom:10px">Applications (${t.applications.length})</div>
      ${t.applications.map(a=>`
        <div style="background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <strong>${esc(a.worker_name)}</strong>
            <span class="task-badge badge-${a.status==='accepted'?'open':a.status==='completed'?'completed':'pending'}">${a.status}</span>
          </div>
          <p style="font-size:12px;color:var(--muted)">${esc(a.proposal)}</p>
          ${a.submission?`<p style="font-size:12px;margin-top:8px;color:var(--accent)">📤 Submitted: ${esc(a.submission)}</p>`:''}
          <div style="display:flex;gap:8px;margin-top:10px">
            ${a.status==='pending'&&(isClient||isAdmin)?`<button class="btn btn-sm btn-success" onclick="acceptApp(${t.id},${a.id})">Accept</button>`:''}
            ${a.status==='review'&&(isClient||isAdmin)?`<button class="btn btn-sm" onclick="completeApp(${t.id},${a.id})">Mark Complete</button>`:''}
          </div>
        </div>`).join('')}
      </div>`;
    }

    let myAppHtml = '';
    if(myApp && myApp.status==='accepted'){
      myAppHtml = `<div style="margin-top:16px">
        <div style="font-weight:700;margin-bottom:8px">Your Submission</div>
        <textarea id="submissionText" rows="3" placeholder="Describe your completed work...">${myApp.submission||''}</textarea>
        <button class="btn btn-sm btn-success" style="margin-top:8px" onclick="submitWork(${t.id},${myApp.id})">Submit Work</button>
      </div>`;
    }

    document.getElementById('taskModalContent').innerHTML = `
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
        <span class="task-badge badge-${statusClass}">${t.status.replace('_',' ')}</span>
        ${t.is_flagged?`<span style="color:var(--red);font-size:12px">🚩 ${esc(t.flag_reason||'Flagged')}</span>`:''}
        ${t.client_verified?'<span class="verified-badge">✔ Verified Client</span>':''}
      </div>
      <h2 style="font-family:var(--font-display);margin-bottom:10px">${esc(t.title)}</h2>
      <p style="color:var(--muted);line-height:1.6;margin-bottom:16px">${esc(t.description)}</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
        <div style="background:var(--surface2);border-radius:10px;padding:14px">
          <div style="font-size:11px;color:var(--muted)">Budget</div>
          <div style="font-family:var(--font-display);font-size:22px;font-weight:700;color:var(--accent)">KES ${t.budget.toLocaleString()}</div>
        </div>
        <div style="background:var(--surface2);border-radius:10px;padding:14px">
          <div style="font-size:11px;color:var(--muted)">Category</div>
          <div style="font-weight:600">${esc(t.category)}</div>
        </div>
      </div>
      ${S.user.role==='worker'&&t.status==='open'&&!hasApplied?
        `<button class="btn" onclick="closeModal('taskModal');openApply(${t.id})">Apply for This Task</button>`:''}
      ${hasApplied&&myApp.status==='pending'?'<div style="color:var(--gold)">⏳ Application pending review</div>':''}
      ${myAppHtml}
      ${isAdmin&&t.status==='pending_approval'?`
        <div style="display:flex;gap:10px;margin-top:16px">
          <button class="btn btn-success" onclick="moderateTask(${t.id},'approve')">✅ Approve</button>
          <button class="btn btn-danger" onclick="moderateTask(${t.id},'reject')">❌ Reject</button>
          <button class="btn-ghost btn-sm" onclick="toggleFlag(${t.id})">${t.is_flagged?'🚩 Unflag':'🚩 Flag'}</button>
        </div>`:''}
      ${appsHtml}
    `;
    openModal('taskModal');
  } catch(e){ toast(e.message,'error'); }
}

function openApply(tid){
  document.getElementById('applyTaskId').value = tid;
  openModal('applyModal');
}
async function submitApplication(){
  const tid = document.getElementById('applyTaskId').value;
  const proposal = document.getElementById('applyProposal').value;
  try {
    await api('/api/tasks/'+tid+'/apply','POST',{proposal});
    closeModal('applyModal');
    toast('Application submitted!','success');
  } catch(e){ toast(e.message,'error'); }
}
async function moderateTask(tid,action){
  try {
    await api('/api/tasks/'+tid+'/approve','POST',{action});
    closeModal('taskModal');
    toast(action==='approve'?'Task approved!':'Task rejected.', action==='approve'?'success':'info');
    if(S.activePage==='admin') admin();
    else if(S.activePage==='tasks') loadTasks(taskPage);
  } catch(e){ toast(e.message,'error'); }
}
async function toggleFlag(tid){
  try {
    await api('/api/tasks/'+tid+'/flag','POST',{reason:'Admin flagged'});
    viewTask(tid);
    toast('Flag updated','info');
  } catch(e){ toast(e.message,'error'); }
}
async function acceptApp(tid,aid){
  try {
    await api('/api/tasks/'+tid+'/applications/'+aid+'/accept','POST');
    viewTask(tid); toast('Application accepted!','success');
  } catch(e){ toast(e.message,'error'); }
}
async function completeApp(tid,aid){
  try {
    await api('/api/tasks/'+tid+'/applications/'+aid+'/complete','POST');
    closeModal('taskModal'); toast('Task completed & paid!','success');
    if(S.activePage==='wallet') wallet();
  } catch(e){ toast(e.message,'error'); }
}
async function submitWork(tid,aid){
  const sub = document.getElementById('submissionText').value;
  try {
    await api('/api/tasks/'+tid+'/applications/'+aid+'/submit','POST',{submission:sub});
    viewTask(tid); toast('Work submitted!','success');
  } catch(e){ toast(e.message,'error'); }
}
async function submitTask(){
  const data = {
    title: document.getElementById('taskTitle').value,
    description: document.getElementById('taskDesc').value,
    category: document.getElementById('taskCategory').value,
    budget: document.getElementById('taskBudget').value,
    slots: document.getElementById('taskSlots').value,
    deadline: document.getElementById('taskDeadline').value
  };
  try {
    await api('/api/tasks','POST',data);
    closeModal('postTaskModal');
    toast(S.user.role==='admin'?'Task published!':'Task submitted for review!','success');
    if(S.activePage==='tasks') loadTasks(1);
  } catch(e){ toast(e.message,'error'); }
}

// ── WALLET ──
async function wallet(){
  const content = document.getElementById('pageContent');
  try {
    const r = await api('/api/wallet');
    const w = r.wallet;
    content.innerHTML = `
      <div class="wallet-hero">
        <div class="wallet-label">Available Balance</div>
        <div class="wallet-balance">KES ${w.balance.toFixed(2)}</div>
        <div style="font-size:12px;opacity:.7;margin-top:4px">
          Escrow: KES ${w.escrow.toFixed(2)} &nbsp;|&nbsp; Earned: KES ${w.total_earned.toFixed(2)}</div>
        <div class="wallet-actions">
          <button class="wallet-action-btn" onclick="openDeposit()">⬆ Deposit</button>
          <button class="wallet-action-btn" onclick="openWithdraw()">⬇ Withdraw</button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Transaction History</div></div>
        ${r.ledger.length?`<div class="table-wrap"><table>
          <thead><tr><th>Type</th><th>Amount</th><th>Balance After</th><th>Description</th><th>Date</th></tr></thead>
          <tbody>${r.ledger.map(l=>`<tr>
            <td><span class="pill ${l.type==='deposit'||l.type==='bonus'||l.type==='release'?'pill-green':'pill-gold'}">${l.type}</span></td>
            <td style="color:${l.amount>=0?'var(--green)':'var(--red)'}">${l.amount>=0?'+':''}KES ${Math.abs(l.amount).toFixed(2)}</td>
            <td>KES ${l.balance_after.toFixed(2)}</td>
            <td style="color:var(--muted)">${esc(l.description||'')}</td>
            <td style="color:var(--muted)">${new Date(l.created_at).toLocaleDateString()}</td>
          </tr>`).join('')}</tbody>
        </table></div>`:'<div class="empty"><div class="empty-icon">📊</div><p>No transactions yet</p></div>'}
      </div>`;
  } catch(e){ content.innerHTML = '<div class="empty">Error loading wallet</div>'; }
}
function openDeposit(){
  document.getElementById('demoNote').textContent = document.querySelector('meta[name=demo]')?.content==='true'?
    '⚡ Demo mode: funds credited instantly.':'';
  openModal('depositModal');
}
function openWithdraw(){ openModal('withdrawModal'); }
async function doDeposit(){
  const amount = document.getElementById('depAmount').value;
  const phone = document.getElementById('depPhone').value;
  try {
    const r = await api('/api/wallet/deposit','POST',{amount:parseFloat(amount),phone});
    closeModal('depositModal');
    toast(r.demo?'Demo deposit successful! KES '+amount+' added.':'STK Push sent to '+phone,'success');
    wallet();
  } catch(e){ toast(e.message,'error'); }
}
async function doWithdraw(){
  const amount = document.getElementById('witAmount').value;
  const phone = document.getElementById('witPhone').value;
  try {
    await api('/api/wallet/withdraw','POST',{amount:parseFloat(amount),phone});
    closeModal('withdrawModal');
    toast('Withdrawal initiated!','success');
    wallet();
  } catch(e){ toast(e.message,'error'); }
}

// ── MESSAGES ──
async function messages(){
  const content = document.getElementById('pageContent');
  S.unreadMsg = 0; updateMsgBadge();
  content.innerHTML = `<div class="chat-wrap">
    <div class="chat-list">
      <div class="chat-list-header">Conversations</div>
      <div id="convList"><div class="spinner"></div></div>
    </div>
    <div class="chat-main">
      <div class="chat-header" id="chatHeader">Select a conversation</div>
      <div class="chat-messages" id="chatMessages">
        <div class="empty"><div class="empty-icon">💬</div><p>Select a conversation</p></div>
      </div>
      <div class="chat-input-area" id="chatInputArea" style="display:none">
        <input type="text" id="chatInput" placeholder="Type a message..." onkeypress="if(event.key==='Enter')sendMsg()">
        <button class="btn" onclick="sendMsg()">Send</button>
      </div>
    </div>
  </div>`;
  loadConversations();
}

async function loadConversations(){
  try {
    const r = await api('/api/messages/conversations');
    const list = document.getElementById('convList');
    // Add admin as first option if user is not admin
    let convs = r.conversations;
    if(S.user.role==='admin'){
      // Show all users who messaged admin
      list.innerHTML = convs.length ? convs.map(c=>`
        <div class="conv-item${S.activeChat===c.user.id?' active':''}" onclick="openChat(${c.user.id},'${esc(c.user.username)}')">
          <div class="avatar" style="width:32px;height:32px;font-size:12px">${c.user.username[0].toUpperCase()}</div>
          <div class="conv-meta">
            <div class="conv-name">${esc(c.user.username)}</div>
            <div class="conv-preview">${esc(c.last_message?.message||'')}</div>
          </div>
          ${c.unread>0?`<span class="conv-unread">${c.unread}</span>`:''}
        </div>`).join('') : '<div style="padding:20px;color:var(--muted);font-size:12px">No conversations</div>';
    } else {
      // Workers/clients can only message admin
      list.innerHTML = `<div class="conv-item${S.activeChat===-1?' active':''}" onclick="openChatWithAdmin()">
        <div class="avatar" style="width:32px;height:32px;font-size:12px;background:var(--accent);color:#000">A</div>
        <div class="conv-meta">
          <div class="conv-name">Admin Support</div>
          <div class="conv-preview">Ask us anything</div>
        </div>
      </div>
      ${convs.map(c=>`
        <div class="conv-item${S.activeChat===c.user.id?' active':''}" onclick="openChat(${c.user.id},'${esc(c.user.username)}')">
          <div class="avatar" style="width:32px;height:32px;font-size:12px">${c.user.username[0].toUpperCase()}</div>
          <div class="conv-meta">
            <div class="conv-name">${esc(c.user.username)}</div>
            <div class="conv-preview">${esc(c.last_message?.message||'')}</div>
          </div>
          ${c.unread>0?`<span class="conv-unread">${c.unread}</span>`:''}
        </div>`).join('')}`;
    }
  } catch{}
}

async function openChatWithAdmin(){
  // Find admin user
  try {
    const r = await api('/api/admin/users?q=admin');
    const admin = r.users.find(u=>u.role==='admin');
    if(admin) openChat(admin.id, 'Admin Support');
  } catch{ toast('Could not load admin contact','error'); }
}

async function openChat(userId, username){
  S.activeChat = userId;
  S.chatUser = username;
  document.getElementById('chatHeader').innerHTML = `
    <div class="avatar" style="width:32px;height:32px;font-size:12px">${username[0].toUpperCase()}</div>
    <span>${esc(username)}</span>`;
  document.getElementById('chatInputArea').style.display='flex';
  document.querySelectorAll('.conv-item').forEach(el=>{
    el.classList.toggle('active', el.onclick?.toString().includes(userId));
  });
  try {
    const r = await api('/api/messages/'+userId);
    const box = document.getElementById('chatMessages');
    box.innerHTML = r.messages.map(m=>msgBubble(m)).join('');
    box.scrollTop = box.scrollHeight;
    if(S.socket) S.socket.emit('mark_as_read', {sender_id: userId});
  } catch(e){ toast(e.message,'error'); }
}

function msgBubble(m){
  const isMine = m.sender_id===S.user.id;
  return `<div style="display:flex;flex-direction:column;align-items:${isMine?'flex-end':'flex-start'}">
    <div class="msg-bubble ${isMine?'sent':'recv'}">${esc(m.message)}</div>
    <div class="msg-time">${new Date(m.created_at).toLocaleTimeString()}</div>
  </div>`;
}

function appendMessage(m, isMine=true){
  const box = document.getElementById('chatMessages');
  if(!box) return;
  const div = document.createElement('div');
  div.style.cssText = `display:flex;flex-direction:column;align-items:${isMine?'flex-end':'flex-start'}`;
  div.innerHTML = `<div class="msg-bubble ${isMine?'sent':'recv'}">${esc(m.message)}</div>
    <div class="msg-time">${new Date(m.created_at).toLocaleTimeString()}</div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function sendMsg(){
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if(!text || !S.activeChat) return;
  if(S.socket){
    S.socket.emit('send_message', {receiver_id: S.activeChat, message: text});
    appendMessage({message:text, created_at:new Date().toISOString(), sender_id:S.user.id}, true);
    input.value='';
  }
}

// ── NOTIFICATIONS ──
async function notifications(){
  const content = document.getElementById('pageContent');
  try {
    await api('/api/notifications/read-all','POST');
    S.unreadNotif = 0; updateNotifBadge();
    const r = await api('/api/notifications');
    const typeIcon = {success:'✅',error:'❌',info:'ℹ️',message:'💬'};
    content.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">Notifications</div></div>
        ${r.notifications.length?r.notifications.map(n=>`
          <div style="display:flex;gap:12px;padding:14px 0;border-bottom:1px solid var(--border);align-items:flex-start">
            <span style="font-size:20px">${typeIcon[n.type]||'🔔'}</span>
            <div>
              <div style="font-weight:600;margin-bottom:4px">${esc(n.title)}</div>
              <div style="font-size:13px;color:var(--muted)">${esc(n.body)}</div>
              <div style="font-size:11px;color:var(--muted);margin-top:4px">${new Date(n.created_at).toLocaleString()}</div>
            </div>
          </div>`).join(''):'<div class="empty"><div class="empty-icon">🔔</div><p>No notifications</p></div>'}
      </div>`;
  } catch{}
}

// ── ADMIN ──
async function admin(){
  const content = document.getElementById('pageContent');
  try {
    const [pending, flagged, stats] = await Promise.all([
      api('/api/admin/tasks/pending'),
      api('/api/admin/tasks/flagged'),
      api('/api/admin/stats')
    ]);
    content.innerHTML = `
      <div class="stats-grid" style="margin-bottom:24px">
        <div class="stat-card blue"><div class="stat-icon">👥</div><div class="stat-value">${stats.total_users}</div><div class="stat-label">Total Users</div></div>
        <div class="stat-card gold"><div class="stat-icon">⏳</div><div class="stat-value">${stats.pending_tasks}</div><div class="stat-label">Pending Tasks</div></div>
        <div class="stat-card green"><div class="stat-icon">📋</div><div class="stat-value">${stats.open_tasks}</div><div class="stat-label">Open Tasks</div></div>
        <div class="stat-card purple"><div class="stat-icon">💳</div><div class="stat-value">${stats.total_payments}</div><div class="stat-label">Payments</div></div>
      </div>
      <div style="display:flex;gap:10px;margin-bottom:20px">
        <button class="btn" onclick="openModal('postTaskModal')">➕ Create Task</button>
        <button class="btn-ghost" onclick="openModal('broadcastModal')">📢 Broadcast</button>
        <button class="btn-ghost" onclick="showPage('adminUsers')">👥 Manage Users</button>
      </div>
      <div class="tabs">
        <div class="tab active" id="tab-pending" onclick="adminSwitchTab('pending')">⏳ Pending (${pending.tasks.length})</div>
        <div class="tab" id="tab-flagged" onclick="adminSwitchTab('flagged')">🚩 Flagged (${flagged.tasks.length})</div>
      </div>
      <div id="adminTaskTab">
        ${pending.tasks.length?`<div class="task-grid">${pending.tasks.map(t=>adminTaskCard(t)).join('')}</div>`:
          '<div class="empty"><div class="empty-icon">✅</div><p>No pending tasks</p></div>'}
      </div>
      <div id="adminFlaggedTab" style="display:none">
        ${flagged.tasks.length?`<div class="task-grid">${flagged.tasks.map(t=>adminTaskCard(t,true)).join('')}</div>`:
          '<div class="empty"><div class="empty-icon">🚩</div><p>No flagged tasks</p></div>'}
      </div>`;
  } catch(e){ content.innerHTML='<div class="empty">Error loading admin panel</div>'; }
}
function adminSwitchTab(t){
  document.getElementById('adminTaskTab').style.display = t==='pending'?'block':'none';
  document.getElementById('adminFlaggedTab').style.display = t==='flagged'?'block':'none';
  document.querySelectorAll('.tabs .tab').forEach(el=>{
    el.classList.toggle('active', el.id==='tab-'+t);
  });
}
function adminTaskCard(t, showFlag=false){
  return `<div class="task-card">
    <div class="task-header">
      <div class="task-title">${esc(t.title)}</div>
      <span class="task-badge badge-pending">${t.status.replace('_',' ')}</span>
    </div>
    <div class="task-desc">${esc(t.description)}</div>
    <div class="task-meta">
      <span class="task-cat">${esc(t.category)}</span>
      <span class="task-budget">KES ${t.budget.toLocaleString()}</span>
    </div>
    ${t.is_flagged?`<div style="color:var(--red);font-size:12px;margin-top:8px">🚩 ${esc(t.flag_reason||'Flagged')}</div>`:''}
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn btn-sm btn-success" onclick="moderateTask(${t.id},'approve')">✅ Approve</button>
      <button class="btn btn-sm btn-danger" onclick="moderateTask(${t.id},'reject')">❌ Reject</button>
      <button class="btn-ghost btn-sm" onclick="viewTask(${t.id})">View</button>
    </div>
  </div>`;
}

async function sendBroadcast(){
  const msg = document.getElementById('broadcastMsg').value;
  try {
    await api('/api/admin/broadcast','POST',{message:msg});
    closeModal('broadcastModal');
    toast('Broadcast sent to all users!','success');
    document.getElementById('broadcastMsg').value='';
  } catch(e){ toast(e.message,'error'); }
}

async function adminUsers(){
  const content = document.getElementById('pageContent');
  content.innerHTML = `
    <div class="search-bar"><input type="text" id="userSearch" placeholder="Search users..." onkeyup="loadAdminUsers()"></div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>User</th><th>Role</th><th>Tier</th><th>Status</th><th>Joined</th><th>Actions</th></tr></thead>
      <tbody id="usersTable"><tr><td colspan="6"><div class="spinner" style="margin:20px auto"></div></td></tr></tbody>
    </table></div></div>`;
  loadAdminUsers();
}
async function loadAdminUsers(){
  const q = document.getElementById('userSearch')?.value||'';
  try {
    const r = await api('/api/admin/users?q='+encodeURIComponent(q));
    document.getElementById('usersTable').innerHTML = r.users.map(u=>`
      <tr>
        <td>
          <div style="display:flex;align-items:center;gap:8px">
            <div class="avatar" style="width:28px;height:28px;font-size:11px">${u.username[0].toUpperCase()}</div>
            <div>
              <div style="font-weight:600">${esc(u.username)}</div>
              <div style="font-size:11px;color:var(--muted)">${esc(u.email)}</div>
            </div>
          </div>
        </td>
        <td><span class="pill pill-${u.role==='admin'?'gold':u.role==='client'?'blue':'green'}">${u.role}</span></td>
        <td>${u.tier}</td>
        <td><span style="color:${u.is_active?'var(--green)':'var(--red)'}">${u.is_active?'Active':'Suspended'}</span>
          ${u.is_verified?'<span class="verified-badge" style="margin-left:6px">✔</span>':''}</td>
        <td style="color:var(--muted)">${new Date(u.created_at).toLocaleDateString()}</td>
        <td>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn-ghost btn-sm" onclick="toggleVerify(${u.id})">${u.is_verified?'Unverify':'Verify'}</button>
            <button class="btn-ghost btn-sm" onclick="toggleSuspend(${u.id})">${u.is_active?'Suspend':'Unsuspend'}</button>
            <button class="btn-ghost btn-sm" onclick="openChat(${u.id},'${esc(u.username)}');showPage('messages')">💬</button>
          </div>
        </td>
      </tr>`).join('');
  } catch{}
}
async function toggleVerify(uid){
  try { await api('/api/users/'+uid+'/verify','POST'); loadAdminUsers(); toast('Verification updated','success'); }
  catch(e){ toast(e.message,'error'); }
}
async function toggleSuspend(uid){
  try { await api('/api/users/'+uid+'/suspend','POST'); loadAdminUsers(); toast('User status updated','info'); }
  catch(e){ toast(e.message,'error'); }
}

// ── UTILS ──
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML)

# ─────────────────────────────────────────
# DB INIT & SEED
# ─────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            admin = User(email=app.config['ADMIN_EMAIL'],
                         username='admin', role='admin', is_verified=True,
                         referral_code=gen_referral_code())
            admin.set_password(app.config['ADMIN_PASSWORD'])
            db.session.add(admin)
            db.session.flush()
            db.session.add(Wallet(user_id=admin.id, balance=1000.0))
            db.session.commit()
            logger.info(f'Admin created: {app.config["ADMIN_EMAIL"]} / {app.config["ADMIN_PASSWORD"]}')

# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    logger.info(f'DTIP v2 starting on port {port}')
    logger.info(f'Admin: {app.config["ADMIN_EMAIL"]}')
    logger.info(f'Demo mode: {app.config["DEMO_MODE"]}')
    socketio.run(app, host='0.0.0.0', port=port, debug=debug)
else:
    init_db()
