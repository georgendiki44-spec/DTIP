"""
DTIP v2.1 — Digital Tasks & Earning Platform
Single-file: Flask backend + SPA frontend
Fixes: gevent (not eventlet), referral links, shares, activation fees,
       admin-only task posting, daily limits, premium, dark/light UI
Run:    python app.py
Deploy: gunicorn -k gevent -w 1 --bind 0.0.0.0:$PORT app:app
"""

import os, re, uuid, secrets, logging, time
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import urlencode

from flask import Flask, request, jsonify, redirect, session, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, disconnect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
import jwt, bcrypt
import requests as http_req

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
app = Flask(__name__)
app.config.update(
    SECRET_KEY            = os.environ.get('SECRET_KEY', secrets.token_hex(32)),
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL','sqlite:///dtip.db').replace('postgres://','postgresql://'),
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 300},
    JWT_SECRET            = os.environ.get('JWT_SECRET', secrets.token_hex(32)),
    JWT_EXPIRY_HOURS      = int(os.environ.get('JWT_EXPIRY_HOURS', 24)),
    GOOGLE_CLIENT_ID      = os.environ.get('GOOGLE_CLIENT_ID', ''),
    GOOGLE_CLIENT_SECRET  = os.environ.get('GOOGLE_CLIENT_SECRET', ''),
    GOOGLE_REDIRECT_URI   = os.environ.get('GOOGLE_REDIRECT_URI', 'http://localhost:5000/auth/google/callback'),
    INTASEND_API_KEY      = os.environ.get('INTASEND_API_KEY', 'demo'),
    DEMO_MODE             = os.environ.get('DEMO_MODE', 'true').lower() == 'true',
    CACHE_TYPE            = 'SimpleCache',
    CACHE_DEFAULT_TIMEOUT = 300,
    ADMIN_EMAIL           = os.environ.get('ADMIN_EMAIL', 'admin@dtip.co.ke'),
    ADMIN_PASSWORD        = os.environ.get('ADMIN_PASSWORD', 'Admin@DTIP2024!'),
    BASE_URL              = os.environ.get('BASE_URL', 'http://localhost:5000'),
    # Defaults (admin can override via DB settings)
    DEF_ACTIVATION_FEE    = float(os.environ.get('ACTIVATION_FEE', '299')),
    DEF_REFERRAL_BONUS    = float(os.environ.get('REFERRAL_BONUS',  '100')),
    DEF_PREMIUM_FEE       = float(os.environ.get('PREMIUM_FEE',     '499')),
    DEF_WITHDRAW_FEE_PCT  = float(os.environ.get('WITHDRAW_FEE_PCT','5')),
    DEF_FREE_LIMIT        = int(os.environ.get('FREE_LIMIT',  '3')),
    DEF_PREMIUM_LIMIT     = int(os.environ.get('PREMIUM_LIMIT','10')),
    DEF_SHARE_PRICE       = float(os.environ.get('SHARE_PRICE','100')),
)

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent',
                    logger=False, engineio_logger=False)
cache    = Cache(app)
limiter  = Limiter(key_func=get_remote_address, app=app,
                   default_limits=["500 per day","100 per hour"],
                   storage_uri="memory://")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GOOGLE_AUTH_URL     = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL    = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
BAD_WORDS = ['porn','xxx','drugs','weapon','hack','phishing','scam','casino','gambling']

# ──────────────────────────────────────────────
# MODELS
# ──────────────────────────────────────────────

class Setting(db.Model):
    __tablename__ = 'settings'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

    @staticmethod
    def get(key, default=None):
        s = Setting.query.filter_by(key=key).first()
        return s.value if s else default

    @staticmethod
    def set_val(key, value):
        s = Setting.query.filter_by(key=key).first()
        if s: s.value = str(value)
        else: db.session.add(Setting(key=key, value=str(value)))


class User(db.Model):
    __tablename__ = 'users'
    id               = db.Column(db.Integer, primary_key=True)
    email            = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username         = db.Column(db.String(80),  unique=True, nullable=False, index=True)
    password_hash    = db.Column(db.String(255))
    google_id        = db.Column(db.String(120), unique=True)
    avatar_url       = db.Column(db.String(500))
    role             = db.Column(db.String(20),  default='member')   # admin | member
    tier             = db.Column(db.String(20),  default='free')     # free  | premium
    is_active        = db.Column(db.Boolean, default=True)
    is_verified      = db.Column(db.Boolean, default=False)
    is_activated     = db.Column(db.Boolean, default=False)
    activation_at    = db.Column(db.DateTime)
    referral_code    = db.Column(db.String(20), unique=True)
    referred_by      = db.Column(db.Integer, db.ForeignKey('users.id'))
    premium_expires  = db.Column(db.DateTime)
    tasks_done_today = db.Column(db.Integer, default=0)
    last_task_date   = db.Column(db.Date)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    last_login       = db.Column(db.DateTime)
    wallet = db.relationship('Wallet', backref='user', uselist=False, lazy='joined')

    def set_pw(self, pw):
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    def check_pw(self, pw):
        if not self.password_hash: return False
        return bcrypt.checkpw(pw.encode(), self.password_hash.encode())

    @property
    def ref_link(self):
        base = Setting.get('base_url', app.config['BASE_URL']).rstrip('/')
        return f"{base}/?ref={self.referral_code}"

    @property
    def is_premium(self):
        if self.tier != 'premium': return False
        if self.premium_expires and self.premium_expires < datetime.utcnow():
            return False
        return True

    def daily_done(self):
        if self.last_task_date != date.today(): return 0
        return self.tasks_done_today or 0

    def daily_limit(self):
        if self.is_premium:
            return int(Setting.get('premium_limit', app.config['DEF_PREMIUM_LIMIT']))
        return int(Setting.get('free_limit', app.config['DEF_FREE_LIMIT']))

    def bump_daily(self):
        today = date.today()
        if self.last_task_date != today:
            self.tasks_done_today = 1
            self.last_task_date   = today
        else:
            self.tasks_done_today = (self.tasks_done_today or 0) + 1

    def to_dict(self):
        return dict(
            id=self.id, email=self.email, username=self.username,
            role=self.role, tier=self.tier,
            is_active=self.is_active, is_verified=self.is_verified,
            is_activated=self.is_activated, is_premium=self.is_premium,
            avatar_url=self.avatar_url,
            referral_code=self.referral_code, ref_link=self.ref_link,
            daily_limit=self.daily_limit(), daily_done=self.daily_done(),
            premium_expires=self.premium_expires.isoformat() if self.premium_expires else None,
            created_at=self.created_at.isoformat(),
            last_login=self.last_login.isoformat() if self.last_login else None,
        )


class Wallet(db.Model):
    __tablename__ = 'wallets'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True)
    balance      = db.Column(db.Float, default=0.0)
    escrow       = db.Column(db.Float, default=0.0)
    total_earned = db.Column(db.Float, default=0.0)
    total_spent  = db.Column(db.Float, default=0.0)

    def to_dict(self):
        return dict(balance=round(self.balance,2), escrow=round(self.escrow,2),
                    total_earned=round(self.total_earned,2), total_spent=round(self.total_spent,2))


class Ledger(db.Model):
    __tablename__ = 'ledger'
    id            = db.Column(db.Integer, primary_key=True)
    wallet_id     = db.Column(db.Integer, db.ForeignKey('wallets.id'))
    type          = db.Column(db.String(30))
    amount        = db.Column(db.Float)
    balance_after = db.Column(db.Float)
    description   = db.Column(db.String(255))
    ref           = db.Column(db.String(100))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=round(self.amount,2),
                    balance_after=round(self.balance_after,2),
                    description=self.description, ref=self.ref,
                    created_at=self.created_at.isoformat())


class Task(db.Model):
    __tablename__ = 'tasks'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200))
    description = db.Column(db.Text)
    category    = db.Column(db.String(80))
    reward      = db.Column(db.Float)
    is_active   = db.Column(db.Boolean, default=True)
    is_flagged  = db.Column(db.Boolean, default=False)
    flag_reason = db.Column(db.String(255))
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'))
    deadline    = db.Column(db.DateTime)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    creator     = db.relationship('User', foreign_keys=[created_by])
    completions = db.relationship('Completion', backref='task', lazy='dynamic', cascade='all,delete')

    def to_dict(self):
        return dict(id=self.id, title=self.title, description=self.description,
                    category=self.category, reward=self.reward,
                    is_active=self.is_active, is_flagged=self.is_flagged,
                    flag_reason=self.flag_reason,
                    deadline=self.deadline.isoformat() if self.deadline else None,
                    completion_count=self.completions.count(),
                    created_at=self.created_at.isoformat())


class Completion(db.Model):
    __tablename__ = 'completions'
    id         = db.Column(db.Integer, primary_key=True)
    task_id    = db.Column(db.Integer, db.ForeignKey('tasks.id'))
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'))
    proof      = db.Column(db.Text)
    status     = db.Column(db.String(20), default='approved')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', backref='completions')

    def to_dict(self):
        return dict(id=self.id, task_id=self.task_id, user_id=self.user_id,
                    username=self.user.username if self.user else None,
                    proof=self.proof, status=self.status,
                    created_at=self.created_at.isoformat())


class Share(db.Model):
    __tablename__ = 'shares'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'))
    quantity     = db.Column(db.Integer)
    price_each   = db.Column(db.Float)
    total_paid   = db.Column(db.Float)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)
    user         = db.relationship('User', backref='shares')

    def to_dict(self):
        return dict(id=self.id, quantity=self.quantity, price_each=self.price_each,
                    total_paid=self.total_paid,
                    purchased_at=self.purchased_at.isoformat())


class Message(db.Model):
    __tablename__ = 'messages'
    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey('users.id'))
    receiver_id  = db.Column(db.Integer, db.ForeignKey('users.id'))
    message      = db.Column(db.Text)
    is_read      = db.Column(db.Boolean, default=False)
    is_broadcast = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    sender   = db.relationship('User', foreign_keys=[sender_id],  backref='sent')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='received')

    def to_dict(self):
        return dict(id=self.id, sender_id=self.sender_id,
                    sender_name=self.sender.username if self.sender else 'System',
                    receiver_id=self.receiver_id, message=self.message,
                    is_read=self.is_read, is_broadcast=self.is_broadcast,
                    created_at=self.created_at.isoformat())


class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'))
    title      = db.Column(db.String(200))
    body       = db.Column(db.Text)
    type       = db.Column(db.String(20), default='info')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, title=self.title, body=self.body,
                    type=self.type, is_read=self.is_read,
                    created_at=self.created_at.isoformat())


class Payment(db.Model):
    __tablename__ = 'payments'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'))
    type       = db.Column(db.String(30))
    amount     = db.Column(db.Float)
    fee        = db.Column(db.Float, default=0.0)
    net_amount = db.Column(db.Float)
    phone      = db.Column(db.String(20))
    reference  = db.Column(db.String(100))
    status     = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=self.amount,
                    fee=self.fee, net_amount=self.net_amount,
                    phone=self.phone, reference=self.reference,
                    status=self.status, created_at=self.created_at.isoformat())

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def gen_code():
    return ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))

def make_token(uid, role):
    return jwt.encode({'sub': uid, 'role': role,
        'exp': datetime.utcnow()+timedelta(hours=app.config['JWT_EXPIRY_HOURS']),
        'iat': datetime.utcnow()}, app.config['JWT_SECRET'], algorithm='HS256')

def decode_token(tok):
    return jwt.decode(tok, app.config['JWT_SECRET'], algorithms=['HS256'])

def current_user():
    h = request.headers.get('Authorization','')
    tok = h.replace('Bearer ','').strip() or request.args.get('token','')
    if not tok: return None
    try: return User.query.get(decode_token(tok)['sub'])
    except: return None

def require_auth(f):
    @wraps(f)
    def d(*a,**kw):
        u = current_user()
        if not u or not u.is_active: return jsonify(error='Unauthorized'),401
        return f(u,*a,**kw)
    return d

def require_admin(f):
    @wraps(f)
    def d(*a,**kw):
        u = current_user()
        if not u or u.role!='admin': return jsonify(error='Admin only'),403
        return f(u,*a,**kw)
    return d

def notify(uid, title, body, ntype='info'):
    n = Notification(user_id=uid, title=title, body=body, type=ntype)
    db.session.add(n)
    try: socketio.emit('notification', n.to_dict(), room=f'u{uid}')
    except: pass

def add_ledger(wallet, tx_type, amount, desc, ref=None):
    db.session.add(Ledger(wallet_id=wallet.id, type=tx_type,
        amount=amount, balance_after=wallet.balance, description=desc, ref=ref))

def cfloat(key, default): 
    v = Setting.get(key)
    return float(v) if v is not None else default

def pay_referral_bonus(user):
    if not user.referred_by: return
    ref = User.query.get(user.referred_by)
    if not ref or not ref.wallet: return
    bonus = cfloat('referral_bonus', app.config['DEF_REFERRAL_BONUS'])
    ref.wallet.balance      += bonus
    ref.wallet.total_earned += bonus
    add_ledger(ref.wallet,'referral_bonus', bonus, f'Referral — {user.username} activated')
    notify(ref.id,'💰 Referral Bonus!',
           f'{user.username} activated! You earned KES {bonus:.0f}','success')

# ──────────────────────────────────────────────
# GOOGLE OAUTH
# ──────────────────────────────────────────────

@app.route('/auth/google')
def google_login():
    if not app.config['GOOGLE_CLIENT_ID']:
        return redirect('/?error=google_not_configured')
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = dict(client_id=app.config['GOOGLE_CLIENT_ID'],
                  redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
                  response_type='code', scope='openid email profile',
                  state=state, access_type='offline', prompt='select_account')
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@app.route('/auth/google/callback')
def google_callback():
    if request.args.get('error') or request.args.get('state') != session.pop('oauth_state',None):
        return redirect('/?error=oauth_failed')
    try:
        tokens = http_req.post(GOOGLE_TOKEN_URL, data=dict(
            code=request.args['code'], client_id=app.config['GOOGLE_CLIENT_ID'],
            client_secret=app.config['GOOGLE_CLIENT_SECRET'],
            redirect_uri=app.config['GOOGLE_REDIRECT_URI'],
            grant_type='authorization_code'), timeout=10).json()
        ui = http_req.get(GOOGLE_USERINFO_URL,
             headers={'Authorization':f'Bearer {tokens["access_token"]}'}, timeout=10).json()
        g_id,email,name,pic = ui['sub'],ui.get('email',''),ui.get('name',''),ui.get('picture','')
        
        user = User.query.filter_by(google_id=g_id).first() or \
               User.query.filter_by(email=email).first()
        if not user:
            uname = re.sub(r'[^a-z0-9_]','',name.lower().replace(' ','_'))[:20] or 'user'
            if User.query.filter_by(username=uname).first():
                uname += str(int(time.time()))[-4:]
            user = User(email=email, username=uname, google_id=g_id,
                        avatar_url=pic, referral_code=gen_code())
            db.session.add(user); db.session.flush()
            db.session.add(Wallet(user_id=user.id))
            db.session.commit()
        else:
            user.google_id = g_id
            if pic: user.avatar_url = pic
            user.last_login = datetime.utcnow()
            db.session.commit()
        return redirect(f'/?token={make_token(user.id,user.role)}')
    except Exception as e:
        log.error(f'Google OAuth: {e}')
        return redirect('/?error=oauth_failed')

# ──────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    d = request.get_json() or {}
    email    = (d.get('email') or '').strip().lower()
    username = (d.get('username') or '').strip()
    password = d.get('password','')
    ref_code = (d.get('ref_code') or '').strip().upper()

    if not email or not username or not password:
        return jsonify(error='All fields required'),400
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$',email):
        return jsonify(error='Invalid email'),400
    if len(password) < 8:
        return jsonify(error='Password min 8 chars'),400
    if User.query.filter_by(email=email).first():
        return jsonify(error='Email already registered'),409
    if User.query.filter_by(username=username).first():
        return jsonify(error='Username taken'),409

    referred_by = None
    if ref_code:
        ru = User.query.filter_by(referral_code=ref_code).first()
        if ru: referred_by = ru.id

    user = User(email=email, username=username, referral_code=gen_code(), referred_by=referred_by)
    user.set_pw(password)
    db.session.add(user); db.session.flush()
    db.session.add(Wallet(user_id=user.id))
    db.session.commit()
    return jsonify(token=make_token(user.id,user.role), user=user.to_dict()), 201

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("20 per hour")
def login():
    d = request.get_json() or {}
    user = User.query.filter_by(email=(d.get('email') or '').strip().lower()).first()
    if not user or not user.check_pw(d.get('password','')):
        return jsonify(error='Invalid credentials'),401
    if not user.is_active: return jsonify(error='Account suspended'),403
    user.last_login = datetime.utcnow()
    db.session.commit()
    return jsonify(token=make_token(user.id,user.role), user=user.to_dict())

@app.route('/api/auth/me')
@require_auth
def me(user):
    return jsonify(user=user.to_dict(), wallet=user.wallet.to_dict() if user.wallet else None)

# ──────────────────────────────────────────────
# SETTINGS
# ──────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
def get_settings_public():
    """Public settings needed by frontend (fees, limits)"""
    keys = ['activation_fee','referral_bonus','premium_fee','withdraw_fee_pct',
            'free_limit','premium_limit','share_price']
    defaults = {'activation_fee': app.config['DEF_ACTIVATION_FEE'],
                'referral_bonus': app.config['DEF_REFERRAL_BONUS'],
                'premium_fee':    app.config['DEF_PREMIUM_FEE'],
                'withdraw_fee_pct': app.config['DEF_WITHDRAW_FEE_PCT'],
                'free_limit':     app.config['DEF_FREE_LIMIT'],
                'premium_limit':  app.config['DEF_PREMIUM_LIMIT'],
                'share_price':    app.config['DEF_SHARE_PRICE']}
    return jsonify({k: Setting.get(k, defaults.get(k,'')) for k in keys})

@app.route('/api/admin/settings', methods=['GET','POST'])
@require_admin
def admin_settings(admin):
    if request.method == 'GET':
        keys = ['activation_fee','referral_bonus','premium_fee','withdraw_fee_pct',
                'free_limit','premium_limit','share_price','base_url']
        defaults = {'activation_fee': app.config['DEF_ACTIVATION_FEE'],
                    'referral_bonus': app.config['DEF_REFERRAL_BONUS'],
                    'premium_fee':    app.config['DEF_PREMIUM_FEE'],
                    'withdraw_fee_pct': app.config['DEF_WITHDRAW_FEE_PCT'],
                    'free_limit':     app.config['DEF_FREE_LIMIT'],
                    'premium_limit':  app.config['DEF_PREMIUM_LIMIT'],
                    'share_price':    app.config['DEF_SHARE_PRICE'],
                    'base_url':       app.config['BASE_URL']}
        return jsonify({k: Setting.get(k, defaults.get(k,'')) for k in keys})
    for k,v in (request.get_json() or {}).items():
        Setting.set_val(k,v)
    db.session.commit()
    cache.clear()
    return jsonify(ok=True)

# ──────────────────────────────────────────────
# ACTIVATION & PREMIUM
# ──────────────────────────────────────────────

@app.route('/api/activate', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def activate(user):
    if user.is_activated: return jsonify(error='Already activated'),400
    d     = request.get_json() or {}
    phone = (d.get('phone') or '').strip()
    fee   = cfloat('activation_fee', app.config['DEF_ACTIVATION_FEE'])
    ref   = f'ACT-{uuid.uuid4().hex[:10].upper()}'
    status = 'completed' if app.config['DEMO_MODE'] else 'pending'

    if app.config['DEMO_MODE']:
        user.is_activated  = True
        user.activation_at = datetime.utcnow()
        pay_referral_bonus(user)
    else:
        try:
            r = http_req.post('https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/',
                headers={'Authorization':f'Bearer {app.config["INTASEND_API_KEY"]}',
                         'Content-Type':'application/json'},
                json=dict(amount=fee,phone_number=phone,api_ref=ref,currency='KES'),timeout=15)
            if r.status_code not in (200,201):
                return jsonify(error='Payment initiation failed'),502
        except: return jsonify(error='Payment service unavailable'),503

    db.session.add(Payment(user_id=user.id,type='activation',amount=fee,
                           fee=0,net_amount=fee,phone=phone,reference=ref,status=status))
    db.session.commit()
    if status == 'completed':
        notify(user.id,'🎉 Account Activated!',
               f'You can now earn on DTIP! KES {fee:.0f} activation fee paid.','success')
    return jsonify(ok=True, demo=app.config['DEMO_MODE'])

@app.route('/api/premium', methods=['POST'])
@require_auth
def upgrade_premium(user):
    if not user.is_activated: return jsonify(error='Activate account first'),400
    d     = request.get_json() or {}
    phone = (d.get('phone') or '').strip()
    fee   = cfloat('premium_fee', app.config['DEF_PREMIUM_FEE'])
    ref   = f'PREM-{uuid.uuid4().hex[:10].upper()}'
    status = 'completed' if app.config['DEMO_MODE'] else 'pending'

    if app.config['DEMO_MODE']:
        user.tier            = 'premium'
        user.premium_expires = datetime.utcnow() + timedelta(days=30)

    db.session.add(Payment(user_id=user.id,type='premium',amount=fee,
                           fee=0,net_amount=fee,phone=phone,reference=ref,status=status))
    db.session.commit()
    lim = int(Setting.get('premium_limit', app.config['DEF_PREMIUM_LIMIT']))
    notify(user.id,'⭐ Premium Activated!',
           f'You can now do up to {lim} tasks/day for 30 days!','success')
    return jsonify(ok=True, user=user.to_dict())

# ──────────────────────────────────────────────
# TASKS
# ──────────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    page = request.args.get('page',1,int)
    cat  = request.args.get('category','')
    q    = request.args.get('q','')
    qry  = Task.query.filter_by(is_active=True)
    if cat: qry = qry.filter_by(category=cat)
    if q:   qry = qry.filter(Task.title.ilike(f'%{q}%')|Task.description.ilike(f'%{q}%'))
    pag  = qry.order_by(Task.created_at.desc()).paginate(page=page,per_page=20)
    # attach user completion if authed
    user = current_user()
    result = []
    for t in pag.items:
        td = t.to_dict()
        if user:
            c = Completion.query.filter_by(task_id=t.id,user_id=user.id).first()
            td['user_done'] = bool(c)
        result.append(td)
    return jsonify(tasks=result, total=pag.total, pages=pag.pages, page=page)

@app.route('/api/tasks', methods=['POST'])
@require_admin
def create_task(admin):
    d = request.get_json() or {}
    for f in ('title','description','category','reward'):
        if not d.get(f): return jsonify(error=f'{f} required'),400
    text    = (d['title']+' '+d['description']).lower()
    flagged = any(bw in text for bw in BAD_WORDS)
    deadline = None
    if d.get('deadline'):
        try: deadline = datetime.fromisoformat(d['deadline'])
        except: pass
    task = Task(title=d['title'].strip(), description=d['description'].strip(),
                category=d['category'], reward=float(d['reward']),
                deadline=deadline, created_by=admin.id,
                is_flagged=flagged, flag_reason='Auto-flagged' if flagged else None)
    db.session.add(task); db.session.commit()
    try:
        socketio.emit('new_task', task.to_dict(), broadcast=True)
        for u in User.query.filter_by(is_active=True,is_activated=True).all():
            notify(u.id,f'🆕 New Task: {task.title[:40]}',
                   f'Earn KES {task.reward:.0f} — {task.category}','info')
    except: pass
    return jsonify(task=task.to_dict()), 201

@app.route('/api/tasks/<int:tid>', methods=['GET'])
def get_task(tid):
    task = Task.query.get_or_404(tid)
    td = task.to_dict()
    user = current_user()
    if user:
        c = Completion.query.filter_by(task_id=tid,user_id=user.id).first()
        td['user_done'] = bool(c)
        td['completion'] = c.to_dict() if c else None
    return jsonify(task=td)

@app.route('/api/tasks/<int:tid>', methods=['PUT'])
@require_admin
def update_task(admin, tid):
    task = Task.query.get_or_404(tid)
    d = request.get_json() or {}
    for f in ('title','description','category','reward','is_active','is_flagged'):
        if f in d: setattr(task,f,d[f])
    db.session.commit()
    return jsonify(task=task.to_dict())

@app.route('/api/tasks/<int:tid>/do', methods=['POST'])
@require_auth
def do_task(user, tid):
    if not user.is_activated:
        return jsonify(error='activate_required',
                       message='Pay activation fee to start earning'),403
    task = Task.query.get_or_404(tid)
    if not task.is_active: return jsonify(error='Task not active'),400
    done_today = user.daily_done()
    limit      = user.daily_limit()
    if done_today >= limit:
        return jsonify(error='daily_limit',
                       message=f'Daily limit reached ({done_today}/{limit}). Upgrade to Premium!',
                       done=done_today, limit=limit), 429
    if Completion.query.filter_by(task_id=tid,user_id=user.id).first():
        return jsonify(error='Already completed this task'),409
    proof = (request.get_json() or {}).get('proof','')
    comp  = Completion(task_id=tid, user_id=user.id, proof=proof)
    db.session.add(comp)
    w = user.wallet
    w.balance      += task.reward
    w.total_earned += task.reward
    add_ledger(w,'task_reward', task.reward, f'Task: {task.title[:60]}', str(tid))
    user.bump_daily()
    db.session.commit()
    notify(user.id,'✅ Task Completed!',
           f'KES {task.reward:.0f} added for "{task.title[:40]}"','success')
    return jsonify(completion=comp.to_dict(), earned=task.reward)

@app.route('/api/my/completions')
@require_auth
def my_completions(user):
    cs = Completion.query.filter_by(user_id=user.id)\
                         .order_by(Completion.created_at.desc()).limit(50).all()
    return jsonify(completions=[c.to_dict() for c in cs])

# ──────────────────────────────────────────────
# SHARES
# ──────────────────────────────────────────────

@app.route('/api/shares', methods=['GET'])
@require_auth
def my_shares(user):
    shares = Share.query.filter_by(user_id=user.id).all()
    price  = cfloat('share_price', app.config['DEF_SHARE_PRICE'])
    total  = sum(s.quantity for s in shares)
    return jsonify(shares=[s.to_dict() for s in shares], total_shares=total,
                   share_price=price, portfolio_value=round(total*price,2))

@app.route('/api/shares/buy', methods=['POST'])
@require_auth
def buy_shares(user):
    if not user.is_activated: return jsonify(error='Activate account first'),403
    qty   = int((request.get_json() or {}).get('quantity',1))
    if qty < 1: return jsonify(error='Min 1 share'),400
    price = cfloat('share_price', app.config['DEF_SHARE_PRICE'])
    total = qty * price
    w = user.wallet
    if w.balance < total: return jsonify(error=f'Need KES {total:.0f}'),400
    w.balance     -= total
    w.total_spent += total
    add_ledger(w,'share_purchase',-total,f'Bought {qty} share(s) @ KES {price:.0f}')
    db.session.add(Share(user_id=user.id,quantity=qty,price_each=price,total_paid=total))
    db.session.commit()
    notify(user.id,'📈 Shares Purchased!',f'You own {qty} share(s) worth KES {total:.0f}','success')
    return jsonify(ok=True, wallet=w.to_dict())

@app.route('/api/admin/shares')
@require_admin
def all_shares(admin):
    rows = db.session.query(Share,User).join(User,Share.user_id==User.id).all()
    return jsonify(shares=[{**s.to_dict(),'username':u.username} for s,u in rows])

# ──────────────────────────────────────────────
# WALLET
# ──────────────────────────────────────────────

@app.route('/api/wallet')
@require_auth
def get_wallet(user):
    rows = Ledger.query.filter_by(wallet_id=user.wallet.id)\
                       .order_by(Ledger.created_at.desc()).limit(30).all()
    return jsonify(wallet=user.wallet.to_dict(), ledger=[r.to_dict() for r in rows])

@app.route('/api/wallet/deposit', methods=['POST'])
@require_auth
@limiter.limit("10 per hour")
def deposit(user):
    d = request.get_json() or {}
    amount = float(d.get('amount',0))
    phone  = (d.get('phone') or '').strip()
    if amount < 10: return jsonify(error='Minimum KES 10'),400
    ref = f'DEP-{uuid.uuid4().hex[:10].upper()}'
    if app.config['DEMO_MODE']:
        w = user.wallet
        w.balance += amount; w.total_earned += amount
        add_ledger(w,'deposit',amount,'M-Pesa deposit',ref)
    db.session.add(Payment(user_id=user.id,type='deposit',amount=amount,
        fee=0,net_amount=amount,phone=phone,reference=ref,
        status='completed' if app.config['DEMO_MODE'] else 'pending'))
    db.session.commit()
    notify(user.id,'💳 Deposit','KES {:.0f} added to wallet'.format(amount),'success')
    return jsonify(ok=True, demo=app.config['DEMO_MODE'])

@app.route('/api/wallet/withdraw', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def withdraw(user):
    if not user.is_activated: return jsonify(error='Activate account first'),403
    d = request.get_json() or {}
    amount = float(d.get('amount',0))
    phone  = (d.get('phone') or '').strip()
    if amount < 50: return jsonify(error='Minimum KES 50'),400
    fee_pct = cfloat('withdraw_fee_pct', app.config['DEF_WITHDRAW_FEE_PCT'])
    fee = round(amount * fee_pct / 100, 2)
    net = amount - fee
    w = user.wallet
    if w.balance < amount: return jsonify(error='Insufficient balance'),400
    w.balance -= amount; w.total_spent += amount
    ref = f'WIT-{uuid.uuid4().hex[:10].upper()}'
    add_ledger(w,'withdraw',-amount,f'Withdrawal to {phone}',ref)
    db.session.add(Payment(user_id=user.id,type='withdrawal',amount=amount,
        fee=fee,net_amount=net,phone=phone,reference=ref,
        status='completed' if app.config['DEMO_MODE'] else 'pending'))
    db.session.commit()
    notify(user.id,'💸 Withdrawal',f'KES {net:.0f} sent to {phone}','info')
    return jsonify(ok=True)

# ──────────────────────────────────────────────
# MESSAGES
# ──────────────────────────────────────────────

@app.route('/api/messages/<int:other_id>')
@require_auth
def get_msgs(user, other_id):
    msgs = Message.query.filter(
        db.or_(db.and_(Message.sender_id==user.id,Message.receiver_id==other_id),
               db.and_(Message.sender_id==other_id,Message.receiver_id==user.id))
    ).order_by(Message.created_at.asc()).limit(100).all()
    for m in msgs:
        if m.receiver_id == user.id and not m.is_read: m.is_read = True
    db.session.commit()
    return jsonify(messages=[m.to_dict() for m in msgs])

@app.route('/api/messages/conversations')
@require_auth
def conversations(user):
    sent  = db.session.query(Message.receiver_id).filter_by(sender_id=user.id,is_broadcast=False).distinct()
    recv  = db.session.query(Message.sender_id).filter_by(receiver_id=user.id,is_broadcast=False).distinct()
    uids  = set([r[0] for r in sent]+[r[0] for r in recv]); uids.discard(user.id)
    convs = []
    for uid in uids:
        u = User.query.get(uid)
        if not u: continue
        last   = Message.query.filter(
            db.or_(db.and_(Message.sender_id==user.id,Message.receiver_id==uid),
                   db.and_(Message.sender_id==uid,Message.receiver_id==user.id))
        ).order_by(Message.created_at.desc()).first()
        unread = Message.query.filter_by(sender_id=uid,receiver_id=user.id,is_read=False).count()
        convs.append(dict(user=u.to_dict(),last=last.to_dict() if last else None,unread=unread))
    convs.sort(key=lambda x:x['last']['created_at'] if x['last'] else '',reverse=True)
    return jsonify(conversations=convs)

@app.route('/api/admin/broadcast', methods=['POST'])
@require_admin
def broadcast(admin):
    text = (request.get_json() or {}).get('message','').strip()
    if not text: return jsonify(error='Message required'),400
    msg = Message(sender_id=admin.id,message=text,is_broadcast=True)
    db.session.add(msg); db.session.flush()
    for u in User.query.filter_by(is_active=True).all():
        notify(u.id,'📢 Announcement',text,'info')
    db.session.commit()
    socketio.emit('broadcast',msg.to_dict(),broadcast=True)
    return jsonify(ok=True)

# ──────────────────────────────────────────────
# NOTIFICATIONS
# ──────────────────────────────────────────────

@app.route('/api/notifications')
@require_auth
def get_notifs(user):
    ns = Notification.query.filter_by(user_id=user.id)\
                           .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify(notifications=[n.to_dict() for n in ns],
                   unread=sum(1 for n in ns if not n.is_read))

@app.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def read_all(user):
    Notification.query.filter_by(user_id=user.id,is_read=False).update({'is_read':True})
    db.session.commit(); return jsonify(ok=True)

# ──────────────────────────────────────────────
# ADMIN
# ──────────────────────────────────────────────

@app.route('/api/admin/stats')
@require_admin
@cache.cached(timeout=60, key_prefix='admin_stats')
def admin_stats(admin):
    return jsonify(
        total_users   = User.query.count(),
        activated     = User.query.filter_by(is_activated=True).count(),
        premium_users = User.query.filter_by(tier='premium').count(),
        active_tasks  = Task.query.filter_by(is_active=True).count(),
        completions   = Completion.query.count(),
        total_shares  = db.session.query(db.func.sum(Share.quantity)).scalar() or 0,
        wallet_total  = round(float(db.session.query(db.func.sum(Wallet.balance)).scalar() or 0),2),
        today_completions = Completion.query.filter(
            Completion.created_at >= datetime.utcnow().replace(hour=0,minute=0,second=0)).count()
    )

@app.route('/api/admin/users')
@require_admin
def admin_users(admin):
    page = request.args.get('page',1,int)
    q    = request.args.get('q','')
    qry  = User.query
    if q: qry = qry.filter(User.username.ilike(f'%{q}%')|User.email.ilike(f'%{q}%'))
    pag  = qry.order_by(User.created_at.desc()).paginate(page=page,per_page=20)
    return jsonify(users=[u.to_dict() for u in pag.items],total=pag.total,pages=pag.pages)

@app.route('/api/admin/users/<int:uid>/verify', methods=['POST'])
@require_admin
def toggle_verify(admin, uid):
    u = User.query.get_or_404(uid)
    u.is_verified = not u.is_verified
    db.session.commit()
    return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/active', methods=['POST'])
@require_admin
def toggle_active(admin, uid):
    u = User.query.get_or_404(uid)
    if u.role=='admin': return jsonify(error='Cannot suspend admin'),400
    u.is_active = not u.is_active
    db.session.commit(); return jsonify(user=u.to_dict())

@app.route('/api/admin/users/<int:uid>/premium', methods=['POST'])
@require_admin
def grant_premium(admin, uid):
    u = User.query.get_or_404(uid)
    u.tier = 'premium'
    u.premium_expires = datetime.utcnow()+timedelta(days=30)
    db.session.commit()
    notify(uid,'⭐ Premium Granted','Admin gave you 30 days premium!','success')
    return jsonify(user=u.to_dict())

@app.route('/api/admin/completions')
@require_admin
def admin_completions(admin):
    cs = Completion.query.order_by(Completion.created_at.desc()).limit(100).all()
    return jsonify(completions=[c.to_dict() for c in cs])

# ──────────────────────────────────────────────
# PUBLIC
# ──────────────────────────────────────────────

@app.route('/api/health')
def health(): return jsonify(status='ok',version='2.1.0',ts=datetime.utcnow().isoformat())

@app.route('/api/stats')
@cache.cached(timeout=120)
def pub_stats():
    return jsonify(
        users=User.query.filter_by(is_active=True).count(),
        tasks=Task.query.filter_by(is_active=True).count(),
        completions=Completion.query.count(),
        paid_out=round(float(db.session.query(db.func.sum(Wallet.total_earned)).scalar() or 0),2)
    )

# ──────────────────────────────────────────────
# SOCKETIO
# ──────────────────────────────────────────────

def sock_user():
    tok = request.args.get('token','')
    if not tok: return None
    try: return User.query.get(decode_token(tok)['sub'])
    except: return None

@socketio.on('connect')
def on_connect():
    u = sock_user()
    if not u: disconnect(); return False
    join_room(f'u{u.id}')
    if u.role=='admin': join_room('admins')
    emit('connected',{'user_id':u.id,'username':u.username})

@socketio.on('send_message')
def on_message(data):
    u = sock_user()
    if not u: return
    text = (data.get('message') or '').strip()
    rid  = data.get('receiver_id')
    if not text: return
    if u.role != 'admin':          # non-admin can only message admin
        admin = User.query.filter_by(role='admin').first()
        if not admin: return
        rid = admin.id
    recv = User.query.get(rid) if rid else None
    if not recv: return
    msg = Message(sender_id=u.id, receiver_id=rid, message=text)
    db.session.add(msg); db.session.commit()
    md = msg.to_dict()
    emit('receive_message', md, room=f'u{rid}')
    emit('receive_message', md, room=f'u{u.id}')

@socketio.on('mark_read')
def on_mark_read(data):
    u = sock_user()
    if not u: return
    Message.query.filter_by(sender_id=data.get('sender_id'),
        receiver_id=u.id, is_read=False).update({'is_read':True})
    db.session.commit()

# ──────────────────────────────────────────────
# FRONTEND — SINGLE PAGE APP
# ──────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DTIP — Earn Online in Kenya</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;800;900&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
/* ═══════════════ TOKENS ═══════════════ */
:root {
  --r: 12px; --r-lg: 18px; --r-xl: 24px;
  --transition: all .2s cubic-bezier(.4,0,.2,1);
}
[data-theme="dark"] {
  --bg:       #080b12;
  --bg2:      #0c101a;
  --surface:  #101520;
  --sf2:      #161d2c;
  --sf3:      #1c2436;
  --border:   #222b3e;
  --border2:  #2d3a52;
  --text:     #e2e8f8;
  --text2:    #8a97b8;
  --text3:    #4e5c78;
  --green:    #34d399;
  --green-d:  rgba(52,211,153,.12);
  --purple:   #a78bfa;
  --purple-d: rgba(167,139,250,.12);
  --gold:     #fbbf24;
  --gold-d:   rgba(251,191,36,.12);
  --red:      #f87171;
  --red-d:    rgba(248,113,113,.12);
  --blue:     #60a5fa;
  --blue-d:   rgba(96,165,250,.12);
  --shadow:   0 4px 24px rgba(0,0,0,.5);
  --shadow-lg:0 12px 48px rgba(0,0,0,.6);
}
[data-theme="light"] {
  --bg:       #f1f4fb;
  --bg2:      #e8ecf5;
  --surface:  #ffffff;
  --sf2:      #f5f7fc;
  --sf3:      #ebeef7;
  --border:   #dde2ef;
  --border2:  #c8cfdf;
  --text:     #0d1526;
  --text2:    #4a5470;
  --text3:    #8890a8;
  --green:    #059669;
  --green-d:  rgba(5,150,105,.1);
  --purple:   #7c3aed;
  --purple-d: rgba(124,58,237,.1);
  --gold:     #d97706;
  --gold-d:   rgba(217,119,6,.1);
  --red:      #dc2626;
  --red-d:    rgba(220,38,38,.1);
  --blue:     #2563eb;
  --blue-d:   rgba(37,99,235,.1);
  --shadow:   0 2px 12px rgba(0,0,0,.08);
  --shadow-lg:0 8px 32px rgba(0,0,0,.12);
}

/* ═══════════════ RESET ═══════════════ */
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif;
     font-size:14px;line-height:1.5;min-height:100vh;transition:background .3s,color .3s}
a{color:var(--green);text-decoration:none}
h1,h2,h3,h4,.num{font-family:'Cabinet Grotesk',sans-serif}
img{display:block}

/* ═══════════════ FORMS ═══════════════ */
input,textarea,select{
  background:var(--sf3);border:1.5px solid var(--border);color:var(--text);
  border-radius:var(--r);padding:11px 14px;font-family:'Instrument Sans',sans-serif;
  font-size:14px;width:100%;outline:none;transition:var(--transition)
}
input:focus,textarea:focus,select:focus{border-color:var(--green);box-shadow:0 0 0 3px var(--green-d)}
select option{background:var(--surface)}
textarea{resize:vertical;min-height:80px}

/* ═══════════════ BUTTONS ═══════════════ */
button{cursor:pointer;font-family:'Instrument Sans',sans-serif;border:none;
       border-radius:var(--r);padding:11px 22px;font-size:14px;font-weight:600;
       transition:var(--transition);letter-spacing:-.01em;line-height:1}
.btn        {background:var(--green);color:#fff}
.btn:hover  {filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 6px 20px var(--green-d)}
.btn-purple {background:var(--purple);color:#fff}
.btn-purple:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn-ghost  {background:transparent;border:1.5px solid var(--border2);color:var(--text2)}
.btn-ghost:hover{border-color:var(--green);color:var(--green);background:var(--green-d)}
.btn-danger {background:var(--red);color:#fff}
.btn-danger:hover{filter:brightness(1.1)}
.btn-gold   {background:var(--gold);color:#000}
.btn-gold:hover{filter:brightness(1.1)}
.btn-sm {padding:7px 16px;font-size:12px;border-radius:9px}
.btn-xs {padding:4px 10px;font-size:11px;border-radius:7px}
.btn-block{width:100%}
.icon-btn{background:var(--sf2);border:1px solid var(--border);color:var(--text2);
          border-radius:var(--r);padding:9px 11px;font-size:16px;cursor:pointer;
          position:relative;transition:var(--transition)}
.icon-btn:hover{border-color:var(--green);color:var(--green)}
.dot{position:absolute;top:5px;right:5px;width:8px;height:8px;background:var(--red);
     border-radius:50%;border:2px solid var(--surface);pointer-events:none}

/* ═══════════════ LAYOUT ═══════════════ */
.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}
.sidebar{background:var(--surface);border-right:1px solid var(--border);
         display:flex;flex-direction:column;height:100vh;position:sticky;top:0;overflow-y:auto}
.main{display:flex;flex-direction:column;min-width:0;overflow:hidden}

/* ═══════════════ SIDEBAR ═══════════════ */
.brand{padding:22px 18px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.brand-mark{width:38px;height:38px;background:var(--green);border-radius:11px;
            display:flex;align-items:center;justify-content:center;
            font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:17px;
            color:#fff;flex-shrink:0}
[data-theme="dark"] .brand-mark{color:#0a1f14}
.brand-name{font-family:'Cabinet Grotesk',sans-serif;font-size:19px;font-weight:900;letter-spacing:-.4px}
.brand-sub{font-size:10px;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase;margin-top:1px}
.nav{flex:1;padding:14px 10px;display:flex;flex-direction:column;gap:2px}
.nav-sec{font-size:10px;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase;
         padding:12px 10px 5px;font-weight:700}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--r);
          cursor:pointer;color:var(--text2);transition:var(--transition);font-size:13.5px;font-weight:500}
.nav-item:hover{background:var(--sf2);color:var(--text)}
.nav-item.active{background:var(--green-d);color:var(--green);font-weight:700}
.nav-ic{font-size:17px;width:22px;text-align:center;flex-shrink:0}
.nav-badge{margin-left:auto;background:var(--red);color:#fff;border-radius:20px;
           padding:2px 7px;font-size:10px;font-weight:700}
.sb-foot{padding:14px 10px;border-top:1px solid var(--border);margin-top:auto}
.user-tile{background:var(--sf2);border:1px solid var(--border);border-radius:var(--r);
           padding:11px;display:flex;align-items:center;gap:10px;margin-bottom:10px}
.u-avatar{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,var(--purple),var(--green));
          display:flex;align-items:center;justify-content:center;font-weight:800;
          font-size:14px;flex-shrink:0;overflow:hidden;color:#fff}
.u-avatar img{width:100%;height:100%;object-fit:cover}
.u-name{font-weight:700;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.u-role{font-size:11px;color:var(--text3);margin-top:1px}
.sb-btns{display:flex;gap:7px}

/* ═══════════════ TOPBAR ═══════════════ */
.topbar{background:var(--surface);border-bottom:1px solid var(--border);
        padding:14px 26px;display:flex;align-items:center;gap:12px;
        position:sticky;top:0;z-index:50;flex-shrink:0}
.page-title{font-family:'Cabinet Grotesk',sans-serif;font-size:20px;font-weight:900;letter-spacing:-.4px}
.tb-right{margin-left:auto;display:flex;align-items:center;gap:8px}

/* ═══════════════ CONTENT ═══════════════ */
.content{flex:1;padding:26px;overflow-y:auto;max-height:calc(100vh - 60px)}

/* ═══════════════ CARDS ═══════════════ */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);
      box-shadow:var(--shadow);overflow:hidden}
.card-hd{display:flex;align-items:center;justify-content:space-between;
         padding:18px 22px;border-bottom:1px solid var(--border)}
.card-title{font-family:'Cabinet Grotesk',sans-serif;font-weight:800;font-size:16px}
.card-body{padding:22px}

/* ═══════════════ STATS GRID ═══════════════ */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:22px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);
      padding:22px;position:relative;overflow:hidden;transition:var(--transition);cursor:default}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.stat-glow{position:absolute;top:-30px;right:-30px;width:120px;height:120px;
           border-radius:50%;opacity:.12;filter:blur(35px);pointer-events:none}
.stat-icon{font-size:24px;margin-bottom:12px}
.stat-val{font-family:'Cabinet Grotesk',sans-serif;font-size:30px;font-weight:900;
          line-height:1;letter-spacing:-.5px}
.stat-lbl{font-size:12px;color:var(--text3);margin-top:5px;font-weight:600;letter-spacing:.2px;text-transform:uppercase}

/* ═══════════════ TASK CARDS ═══════════════ */
.task-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.task-card{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--r-xl);
           padding:20px;transition:var(--transition);cursor:pointer;position:relative;overflow:hidden}
.task-card:hover{border-color:var(--border2);box-shadow:var(--shadow);transform:translateY(-2px)}
.task-card::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;
                  background:linear-gradient(90deg,var(--green),var(--purple));
                  opacity:0;transition:var(--transition)}
.task-card:hover::after{opacity:1}
.task-ic{width:42px;height:42px;border-radius:11px;background:var(--green-d);
         display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:12px}
.task-title{font-family:'Cabinet Grotesk',sans-serif;font-weight:800;font-size:15px;
            line-height:1.3;margin-bottom:8px}
.task-desc{font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:14px;
           display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.task-foot{display:flex;align-items:center;gap:8px;border-top:1px solid var(--border);padding-top:14px;flex-wrap:wrap}
.task-reward{font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:19px;color:var(--green)}
.task-cat{background:var(--sf2);color:var(--text3);padding:4px 10px;border-radius:20px;
          font-size:11px;font-weight:700;border:1px solid var(--border)}

/* ═══════════════ BADGES ═══════════════ */
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;
       border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.2px;white-space:nowrap}
.badge-g  {background:var(--green-d);color:var(--green)}
.badge-p  {background:var(--purple-d);color:var(--purple)}
.badge-gold{background:var(--gold-d);color:var(--gold)}
.badge-r  {background:var(--red-d);color:var(--red)}
.badge-b  {background:var(--blue-d);color:var(--blue)}
.badge-dim{background:var(--sf3);color:var(--text3)}

/* ═══════════════ TABLE ═══════════════ */
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:11px 16px;font-size:11px;color:var(--text3);
   text-transform:uppercase;letter-spacing:.8px;font-weight:700;
   border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:13px 16px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--sf2)}

/* ═══════════════ MODAL ═══════════════ */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(8px);
         z-index:900;display:none;align-items:center;justify-content:center;padding:20px}
.overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);
       padding:30px;width:100%;max-width:520px;max-height:90vh;overflow-y:auto;
       box-shadow:var(--shadow-lg);animation:modalIn .2s ease}
@keyframes modalIn{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
.modal-title{font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:20px;margin-bottom:22px}
.form-grp{margin-bottom:16px}
.form-lbl{display:block;font-size:12px;font-weight:700;color:var(--text2);
          margin-bottom:7px;letter-spacing:.2px;text-transform:uppercase}
.form-hint{font-size:11px;color:var(--text3);margin-top:5px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal-foot{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}

/* ═══════════════ WALLET HERO ═══════════════ */
.w-hero{background:linear-gradient(135deg,#0f2027 0%,#203a43 50%,#2c5364 100%);
        border-radius:var(--r-xl);padding:30px;margin-bottom:22px;position:relative;overflow:hidden}
.w-hero::before{content:'';position:absolute;width:250px;height:250px;border-radius:50%;
                background:rgba(255,255,255,.05);top:-80px;right:-80px}
.w-lbl{font-size:12px;color:rgba(255,255,255,.55);letter-spacing:1.5px;
       text-transform:uppercase;font-weight:700;margin-bottom:8px}
.w-balance{font-family:'Cabinet Grotesk',sans-serif;font-size:48px;font-weight:900;
           color:#fff;letter-spacing:-2px;line-height:1}
.w-sub{font-size:13px;color:rgba(255,255,255,.45);margin-top:8px}
.w-actions{display:flex;gap:10px;margin-top:22px;flex-wrap:wrap}
.w-btn{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.2);
       border-radius:11px;padding:12px 20px;font-weight:700;font-size:13px;
       backdrop-filter:blur(10px)}
.w-btn:hover{background:rgba(255,255,255,.22)}

/* ═══════════════ REFERRAL ═══════════════ */
.ref-box{background:var(--green-d);border:1.5px solid var(--green);
         border-radius:var(--r-lg);padding:22px;margin-bottom:22px}
.ref-link{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
          padding:12px 16px;font-size:13px;color:var(--blue);word-break:break-all;
          margin:10px 0;font-family:monospace;cursor:pointer;transition:var(--transition)}
.ref-link:hover{border-color:var(--green)}

/* ═══════════════ SHARE CARD ═══════════════ */
.share-hero{background:linear-gradient(135deg,var(--purple-d),var(--blue-d));
            border:1.5px solid var(--border2);border-radius:var(--r-xl);padding:24px;margin-bottom:22px}

/* ═══════════════ CHAT ═══════════════ */
.chat-shell{display:grid;grid-template-columns:265px 1fr;
            height:calc(100vh - 112px);background:var(--surface);
            border-radius:var(--r-xl);border:1px solid var(--border);overflow:hidden}
.conv-list{border-right:1px solid var(--border);overflow-y:auto;display:flex;flex-direction:column}
.conv-hd{padding:16px 18px;border-bottom:1px solid var(--border);
         font-family:'Cabinet Grotesk',sans-serif;font-weight:800;font-size:15px;flex-shrink:0}
.conv-item{display:flex;align-items:center;gap:11px;padding:13px 16px;
           cursor:pointer;border-bottom:1px solid var(--border);transition:background .12s}
.conv-item:hover,.conv-item.active{background:var(--sf2)}
.conv-av{width:38px;height:38px;border-radius:10px;background:var(--purple-d);
         display:flex;align-items:center;justify-content:center;
         font-weight:800;font-size:14px;color:var(--purple);flex-shrink:0}
.conv-meta{min-width:0;flex:1}
.conv-name{font-weight:700;font-size:13px}
.conv-prev{font-size:12px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.conv-unread{background:var(--green);color:#fff;border-radius:20px;padding:2px 8px;font-size:10px;font-weight:800}
[data-theme="dark"] .conv-unread{color:#0a1f14}
.chat-area{display:flex;flex-direction:column;min-width:0}
.chat-top{padding:15px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0}
.chat-msgs{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.bbl-wrap{display:flex;flex-direction:column}
.bbl-wrap.mine{align-items:flex-end}
.bbl{padding:10px 15px;border-radius:14px;font-size:13.5px;line-height:1.55;
     max-width:70%;word-break:break-word}
.bbl.mine  {background:var(--green);color:#fff;border-bottom-right-radius:4px}
[data-theme="dark"] .bbl.mine{color:#0a1f14}
.bbl.theirs{background:var(--sf3);border-bottom-left-radius:4px}
.bbl-time{font-size:10px;color:var(--text3);margin-top:4px;padding:0 2px}
.chat-input-area{padding:16px 18px;border-top:1px solid var(--border);display:flex;gap:10px;flex-shrink:0}
.chat-input-area input{flex:1}

/* ═══════════════ PROGRESS BAR ═══════════════ */
.prog-bar{height:6px;background:var(--sf3);border-radius:3px;overflow:hidden;margin-top:8px}
.prog-fill{height:100%;background:var(--green);border-radius:3px;transition:width .4s ease}

/* ═══════════════ DAILY BAR ═══════════════ */
.daily-bar{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);
           padding:14px 18px;display:flex;align-items:center;gap:16px;margin-bottom:20px}
.daily-meta{flex:1}
.daily-lbl{font-size:11px;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.daily-count{font-family:'Cabinet Grotesk',sans-serif;font-size:20px;font-weight:900}

/* ═══════════════ AUTH ═══════════════ */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;
           background:var(--bg);padding:20px;position:relative;overflow:hidden}
.auth-wrap::before{content:'';position:absolute;width:500px;height:500px;border-radius:50%;
  background:radial-gradient(circle,var(--green-d),transparent 70%);top:-150px;right:-150px}
.auth-wrap::after{content:'';position:absolute;width:400px;height:400px;border-radius:50%;
  background:radial-gradient(circle,var(--purple-d),transparent 70%);bottom:-100px;left:-100px}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);
           padding:42px;width:100%;max-width:420px;position:relative;z-index:1;box-shadow:var(--shadow-lg)}
.auth-logo{text-align:center;margin-bottom:30px}
.auth-mark{width:58px;height:58px;background:var(--green);border-radius:16px;
           display:inline-flex;align-items:center;justify-content:center;
           font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:24px;
           color:#fff;margin-bottom:14px}
[data-theme="dark"] .auth-mark{color:#0a1f14}
.auth-logo h1{font-family:'Cabinet Grotesk',sans-serif;font-size:28px;font-weight:900;letter-spacing:-.5px}
.auth-logo p{color:var(--text3);font-size:13.5px;margin-top:5px}
.auth-tabs{display:flex;background:var(--sf2);border-radius:12px;padding:4px;
           margin-bottom:24px;border:1px solid var(--border)}
.auth-tab{flex:1;padding:9px;border-radius:9px;text-align:center;cursor:pointer;
          font-weight:700;font-size:13px;color:var(--text3);transition:var(--transition)}
.auth-tab.active{background:var(--green);color:#fff}
[data-theme="dark"] .auth-tab.active{color:#0a1f14}
.g-btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
       padding:12px;background:var(--sf2);border:1.5px solid var(--border);color:var(--text);
       border-radius:11px;font-weight:700;font-size:13.5px;cursor:pointer;
       transition:var(--transition);margin-bottom:18px}
.g-btn:hover{border-color:var(--green);background:var(--green-d)}
.or-divider{display:flex;align-items:center;gap:12px;color:var(--text3);
            font-size:12px;margin-bottom:18px;font-weight:600}
.or-divider::before,.or-divider::after{content:'';flex:1;height:1px;background:var(--border)}

/* ═══════════════ GATE ═══════════════ */
.gate{background:var(--surface);border:2px solid var(--gold);border-radius:var(--r-xl);
      padding:36px;text-align:center;max-width:460px;margin:30px auto;box-shadow:var(--shadow)}
.gate-icon{font-size:52px;margin-bottom:16px}
.gate h2{font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:24px;margin-bottom:10px}
.gate p{color:var(--text2);font-size:13.5px;line-height:1.7;margin-bottom:22px}
.gate-fee{background:var(--gold-d);border:1px solid var(--gold);border-radius:var(--r-lg);
          padding:14px 24px;margin-bottom:22px;display:inline-block}
.gate-fee-val{font-family:'Cabinet Grotesk',sans-serif;font-size:36px;font-weight:900;color:var(--gold)}
.gate-fee-lbl{font-size:12px;color:var(--text3);margin-top:2px}

/* ═══════════════ BROADCAST BAR ═══════════════ */
.bcast-bar{background:var(--purple-d);border-bottom:1px solid var(--border2);
           padding:10px 26px;display:none;align-items:center;gap:10px;font-size:13px}
.bcast-bar.show{display:flex}

/* ═══════════════ SETTINGS ═══════════════ */
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* ═══════════════ MISC ═══════════════ */
.empty{text-align:center;padding:60px 20px;color:var(--text3)}
.empty-ic{font-size:44px;margin-bottom:14px}
.empty h3{font-family:'Cabinet Grotesk',sans-serif;font-size:18px;font-weight:800;
          margin-bottom:6px;color:var(--text2)}
.spinner{width:34px;height:34px;border:3px solid var(--border);
         border-top-color:var(--green);border-radius:50%;
         animation:spin .6s linear infinite;margin:50px auto}
@keyframes spin{to{transform:rotate(360deg)}}
.tabs{display:flex;border-bottom:2px solid var(--border);margin-bottom:20px}
.tab{padding:10px 18px;cursor:pointer;font-size:13.5px;color:var(--text3);
     border-bottom:2.5px solid transparent;margin-bottom:-2px;transition:var(--transition);font-weight:600}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.divh{height:1px;background:var(--border);margin:18px 0}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.three-col{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.steps{display:flex;flex-direction:column;gap:18px}
.step{display:flex;gap:14px;align-items:flex-start}
.step-num{width:34px;height:34px;border-radius:50%;background:var(--green-d);
          display:flex;align-items:center;justify-content:center;
          font-weight:900;color:var(--green);flex-shrink:0;font-family:'Cabinet Grotesk',sans-serif}
.step-body strong{display:block;margin-bottom:3px}
.step-body span{font-size:13px;color:var(--text3)}

/* ═══════════════ TOAST ═══════════════ */
.toasts{position:fixed;bottom:24px;right:24px;z-index:9999;
        display:flex;flex-direction:column;gap:10px;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);
       padding:13px 18px;min-width:280px;font-size:13.5px;
       display:flex;align-items:center;gap:11px;box-shadow:var(--shadow-lg);
       pointer-events:auto;animation:toastIn .25s ease;font-weight:500}
.toast.success{border-left:3px solid var(--green)}
.toast.error  {border-left:3px solid var(--red)}
.toast.info   {border-left:3px solid var(--blue)}
.toast.warn   {border-left:3px solid var(--gold)}
@keyframes toastIn{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}

/* ═══════════════ RESPONSIVE ═══════════════ */
@media(max-width:900px){
  .shell{grid-template-columns:58px 1fr}
  .brand-name,.brand-sub,.nav-item span:not(.nav-ic),.sb-foot .u-name,.sb-foot .u-role,.nav-sec,
  .sb-foot .sb-btns .btn-ghost span{display:none}
  .two-col,.three-col,.form-row,.settings-grid{grid-template-columns:1fr}
  .task-grid{grid-template-columns:1fr}
  .chat-shell{grid-template-columns:55px 1fr}
  .conv-meta,.conv-hd{display:none}
}
@media(max-width:600px){
  .content{padding:14px}
  .stats{grid-template-columns:1fr 1fr}
  .w-balance{font-size:36px}
  .auth-card{padding:28px}
}
</style>
</head>
<body>

<div class="toasts" id="toasts"></div>

<!-- ════════════════════════════════════
     AUTH SCREEN
════════════════════════════════════ -->
<div id="authScreen" style="display:none">
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-logo">
        <div class="auth-mark">D</div>
        <h1>DTIP</h1>
        <p>Kenya's #1 Digital Earning Platform</p>
      </div>
      <div class="auth-tabs">
        <div class="auth-tab active" id="tabL" onclick="switchTab('login')">Sign In</div>
        <div class="auth-tab" id="tabR" onclick="switchTab('register')">Register</div>
      </div>
      <button class="g-btn" onclick="location.href='/auth/google'">
        <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.707A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.707V4.961H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.039l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.961L3.964 6.293C4.672 4.166 6.656 3.58 9 3.58z"/></svg>
        Continue with Google
      </button>
      <div class="or-divider">or continue with email</div>
      <div id="formLogin">
        <div class="form-grp"><label class="form-lbl">Email</label>
          <input id="lEmail" type="email" placeholder="you@example.com"></div>
        <div class="form-grp"><label class="form-lbl">Password</label>
          <input id="lPass" type="password" placeholder="••••••••"
                 onkeypress="if(event.key==='Enter')doLogin()"></div>
        <button class="btn btn-block" onclick="doLogin()" style="margin-top:4px">Sign In</button>
      </div>
      <div id="formReg" style="display:none">
        <div class="form-row">
          <div class="form-grp"><label class="form-lbl">Username</label>
            <input id="rUser" type="text" placeholder="johndoe"></div>
          <div class="form-grp"><label class="form-lbl">Email</label>
            <input id="rEmail" type="email" placeholder="you@example.com"></div>
        </div>
        <div class="form-grp"><label class="form-lbl">Password</label>
          <input id="rPass" type="password" placeholder="Min 8 characters"></div>
        <div class="form-grp"><label class="form-lbl">Referral Code (optional)</label>
          <input id="rRef" type="text" placeholder="XXXXXXXX"></div>
        <button class="btn btn-block" onclick="doRegister()" style="margin-top:4px">Create Account</button>
      </div>
    </div>
  </div>
</div>

<!-- ════════════════════════════════════
     MAIN APP
════════════════════════════════════ -->
<div id="appScreen" class="shell" style="display:none">
  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">D</div>
      <div><div class="brand-name">DTIP</div><div class="brand-sub">Earn Online</div></div>
    </div>
    <nav class="nav">
      <div class="nav-item active" id="nav-home"          onclick="go('home')">         <div class="nav-ic">🏠</div><span>Dashboard</span></div>
      <div class="nav-item"        id="nav-tasks"         onclick="go('tasks')">        <div class="nav-ic">📋</div><span>Tasks</span></div>
      <div class="nav-item"        id="nav-wallet"        onclick="go('wallet')">       <div class="nav-ic">💰</div><span>Wallet</span></div>
      <div class="nav-item"        id="nav-shares"        onclick="go('shares')">       <div class="nav-ic">📈</div><span>Shares</span></div>
      <div class="nav-item"        id="nav-referrals"     onclick="go('referrals')">    <div class="nav-ic">🔗</div><span>Referrals</span></div>
      <div class="nav-item"        id="nav-messages"      onclick="go('messages')">     <div class="nav-ic">💬</div><span>Messages</span><span class="nav-badge" id="msgBadge" style="display:none">0</span></div>
      <div class="nav-item"        id="nav-notifications" onclick="go('notifications')"><div class="nav-ic">🔔</div><span>Alerts</span>  <span class="nav-badge" id="notifBadge" style="display:none">0</span></div>
      <div id="adminNav" style="display:none">
        <div class="nav-sec">Admin</div>
        <div class="nav-item" id="nav-admin"         onclick="go('admin')">        <div class="nav-ic">⚙️</div><span>Panel</span></div>
        <div class="nav-item" id="nav-adminUsers"    onclick="go('adminUsers')">   <div class="nav-ic">👥</div><span>Users</span></div>
        <div class="nav-item" id="nav-adminSettings" onclick="go('adminSettings')"><div class="nav-ic">🛠</div><span>Settings</span></div>
      </div>
    </nav>
    <div class="sb-foot">
      <div class="user-tile">
        <div class="u-avatar" id="sbAv"></div>
        <div><div class="u-name" id="sbName">—</div><div class="u-role" id="sbRole">—</div></div>
      </div>
      <div class="sb-btns">
        <button class="btn-ghost btn-sm" style="flex:1" onclick="toggleTheme()">☀️ <span>Theme</span></button>
        <button class="btn-ghost btn-sm" style="flex:1" onclick="logout()">← <span>Out</span></button>
      </div>
    </div>
  </aside>

  <!-- MAIN CONTENT -->
  <div class="main">
    <div class="bcast-bar" id="bcastBar">
      <span>📢</span><span id="bcastTxt"></span>
      <button class="btn-ghost btn-xs" style="margin-left:auto" onclick="this.closest('.bcast-bar').classList.remove('show')">✕</button>
    </div>
    <header class="topbar">
      <div class="page-title" id="pageTitle">Dashboard</div>
      <div class="tb-right">
        <button class="icon-btn" onclick="go('notifications')" id="notifBtn">
          🔔<span class="dot" id="notifDot" style="display:none"></span>
        </button>
        <button class="icon-btn" onclick="toggleTheme()" title="Toggle theme">☀️</button>
      </div>
    </header>
    <div class="content" id="content"><div class="spinner"></div></div>
  </div>
</div>

<!-- ════════════════════════════════════
     MODALS
════════════════════════════════════ -->
<!-- Do Task -->
<div class="overlay" id="mDoTask">
  <div class="modal">
    <div class="modal-title" id="mDoTitle">Complete Task</div>
    <div id="mDoInfo" style="margin-bottom:16px"></div>
    <div class="form-grp">
      <label class="form-lbl">Proof of Completion <span style="color:var(--text3);font-weight:400;text-transform:none">(optional)</span></label>
      <textarea id="mDoProof" placeholder="Describe what you did, paste a link, screenshot URL, etc."></textarea>
    </div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mDoTask')">Cancel</button>
      <button class="btn" onclick="submitDoTask()">✅ Submit & Earn</button>
    </div>
  </div>
</div>

<!-- Activate -->
<div class="overlay" id="mActivate">
  <div class="modal">
    <div class="modal-title">🔓 Activate Your Account</div>
    <div style="text-align:center;margin-bottom:20px">
      <div style="font-size:13px;color:var(--text2);margin-bottom:8px">One-Time Activation Fee</div>
      <div class="gate-fee" style="display:block"><div class="gate-fee-val" id="mActFee">KES 299</div><div class="gate-fee-lbl">Pay once, earn forever</div></div>
    </div>
    <div class="form-grp"><label class="form-lbl">M-Pesa Phone</label><input id="mActPhone" type="tel" placeholder="07XXXXXXXX"></div>
    <p class="form-hint" style="margin-bottom:18px">After activation you can complete tasks and withdraw earnings. Referrer gets a bonus when you activate!</p>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mActivate')">Cancel</button>
      <button class="btn btn-gold" onclick="doActivate()">Pay & Activate</button>
    </div>
  </div>
</div>

<!-- Premium -->
<div class="overlay" id="mPremium">
  <div class="modal">
    <div class="modal-title">⭐ Upgrade to Premium</div>
    <div style="text-align:center;margin-bottom:20px">
      <div style="font-size:13px;color:var(--text2);margin-bottom:8px">Monthly Premium — 30 Days</div>
      <div style="font-family:'Cabinet Grotesk',sans-serif;font-size:40px;font-weight:900;color:var(--purple)" id="mPremFee">KES 499</div>
    </div>
    <div style="background:var(--purple-d);border:1px solid var(--border2);border-radius:var(--r);padding:16px;margin-bottom:18px;font-size:13.5px;color:var(--text2);line-height:1.8">
      ✅ Up to <strong id="mPremLimit">10</strong> tasks per day<br>
      ✅ Priority chat support<br>
      ✅ ⭐ Premium badge on profile<br>
      ✅ Unlock higher earning potential
    </div>
    <div class="form-grp"><label class="form-lbl">M-Pesa Phone</label><input id="mPremPhone" type="tel" placeholder="07XXXXXXXX"></div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mPremium')">Cancel</button>
      <button class="btn-purple" onclick="doUpgradePremium()">Upgrade Now</button>
    </div>
  </div>
</div>

<!-- Deposit -->
<div class="overlay" id="mDeposit">
  <div class="modal">
    <div class="modal-title">💳 Deposit via M-Pesa</div>
    <div id="mDepDemo" style="background:var(--gold-d);border:1px solid var(--gold);border-radius:var(--r);padding:12px;font-size:13px;color:var(--gold);margin-bottom:18px;display:none">⚡ Demo Mode — funds credited instantly</div>
    <div class="form-grp"><label class="form-lbl">Amount (KES)</label><input id="mDepAmt" type="number" placeholder="Min. KES 10" min="10"></div>
    <div class="form-grp"><label class="form-lbl">Phone Number</label><input id="mDepPhone" type="tel" placeholder="07XXXXXXXX"></div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mDeposit')">Cancel</button>
      <button class="btn" onclick="doDeposit()">Deposit Now</button>
    </div>
  </div>
</div>

<!-- Withdraw -->
<div class="overlay" id="mWithdraw">
  <div class="modal">
    <div class="modal-title">💸 Withdraw to M-Pesa</div>
    <div class="form-grp"><label class="form-lbl">Amount (KES)</label><input id="mWitAmt" type="number" placeholder="Min. KES 50" min="50"></div>
    <div class="form-grp"><label class="form-lbl">Phone Number</label><input id="mWitPhone" type="tel" placeholder="07XXXXXXXX"></div>
    <p class="form-hint" style="margin-bottom:16px">A 5% fee applies. Minimum withdrawal: KES 50</p>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mWithdraw')">Cancel</button>
      <button class="btn" onclick="doWithdraw()">Withdraw</button>
    </div>
  </div>
</div>

<!-- New Task (Admin) -->
<div class="overlay" id="mNewTask">
  <div class="modal">
    <div class="modal-title">➕ Create New Task</div>
    <div class="form-grp"><label class="form-lbl">Title</label><input id="ntTitle" type="text" placeholder="What should members do?"></div>
    <div class="form-grp"><label class="form-lbl">Instructions</label><textarea id="ntDesc" rows="4" placeholder="Step-by-step instructions..."></textarea></div>
    <div class="form-row">
      <div class="form-grp"><label class="form-lbl">Category</label>
        <select id="ntCat">
          <option>Data Entry</option><option>Writing</option><option>Design</option>
          <option>Social Media</option><option>Research</option><option>Tech</option>
          <option>Marketing</option><option>Survey</option><option>Other</option>
        </select></div>
      <div class="form-grp"><label class="form-lbl">Reward per completion (KES)</label>
        <input id="ntReward" type="number" placeholder="50" min="1"></div>
    </div>
    <div class="form-grp"><label class="form-lbl">Deadline (optional)</label><input id="ntDeadline" type="datetime-local"></div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mNewTask')">Cancel</button>
      <button class="btn" onclick="submitTask()">Publish Task</button>
    </div>
  </div>
</div>

<!-- Broadcast -->
<div class="overlay" id="mBroadcast">
  <div class="modal">
    <div class="modal-title">📢 Broadcast Message</div>
    <div class="form-grp"><label class="form-lbl">Message</label>
      <textarea id="mBcastMsg" rows="4" placeholder="Message sent to all users..."></textarea></div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mBroadcast')">Cancel</button>
      <button class="btn" onclick="sendBroadcast()">Send to Everyone</button>
    </div>
  </div>
</div>

<!-- Buy Shares -->
<div class="overlay" id="mBuyShares">
  <div class="modal">
    <div class="modal-title">📈 Buy Platform Shares</div>
    <div id="mSharePrice" style="text-align:center;margin-bottom:20px"></div>
    <div class="form-grp"><label class="form-lbl">Number of Shares</label>
      <input id="mShareQty" type="number" value="1" min="1" oninput="updateShareTotal()"></div>
    <div id="mShareTotal" style="background:var(--green-d);border:1px solid var(--green);border-radius:var(--r);padding:14px;text-align:center;margin-bottom:14px;font-family:'Cabinet Grotesk',sans-serif;font-size:22px;font-weight:900;color:var(--green)">Total: KES 100</div>
    <p class="form-hint" style="margin-bottom:14px">Deducted from your wallet balance</p>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeM('mBuyShares')">Cancel</button>
      <button class="btn" onclick="doBuyShares()">Buy Shares</button>
    </div>
  </div>
</div>

<script>
/* ════════════════════════════════════
   STATE & BOOT
════════════════════════════════════ */
const S = {
  token:null, user:null, socket:null, page:'home',
  unreadNotif:0, unreadMsg:0,
  activeChat:null, chatPartner:null,
  sharePrice:100, settings:{}
};

document.addEventListener('DOMContentLoaded', async () => {
  const p = new URLSearchParams(location.search);
  if (p.get('token')) { localStorage.setItem('tok', p.get('token')); history.replaceState({},'',' /'); }
  if (p.get('ref'))   localStorage.setItem('pending_ref', p.get('ref'));

  S.token = localStorage.getItem('tok');
  document.documentElement.setAttribute('data-theme', localStorage.getItem('theme')||'dark');

  if (S.token) {
    try { const r = await api('/api/auth/me'); S.user=r.user; showApp(); }
    catch { localStorage.removeItem('tok'); showAuth(); }
  } else showAuth();
});

/* ════════════════════════════════════
   UTILS
════════════════════════════════════ */
async function api(url, method='GET', body=null) {
  const opts = { method, headers:{'Content-Type':'application/json'} };
  if (S.token) opts.headers['Authorization'] = 'Bearer '+S.token;
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || d.message || 'Request failed');
  return d;
}

function toast(msg, type='info') {
  const ico = {success:'✅',error:'❌',info:'ℹ️',warn:'⚠️'};
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${ico[type]||'🔔'}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>el.remove(), 4500);
}

const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmt = d => d ? new Date(d).toLocaleDateString('en-KE',{day:'2-digit',month:'short',year:'numeric'}) : '—';
const fmtT = d => d ? new Date(d).toLocaleTimeString('en-KE',{hour:'2-digit',minute:'2-digit'}) : '';

function toggleTheme() {
  const t = document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',t);
  localStorage.setItem('theme',t);
}
function openM(id){ document.getElementById(id).classList.add('show'); }
function closeM(id){ document.getElementById(id).classList.remove('show'); }

/* ════════════════════════════════════
   AUTH
════════════════════════════════════ */
function showAuth(){ document.getElementById('authScreen').style.display='block'; document.getElementById('appScreen').style.display='none'; }
function showApp(){ document.getElementById('authScreen').style.display='none'; document.getElementById('appScreen').style.display='grid'; initApp(); }

function switchTab(t) {
  document.getElementById('formLogin').style.display = t==='login'?'block':'none';
  document.getElementById('formReg').style.display   = t==='register'?'block':'none';
  document.getElementById('tabL').className = 'auth-tab'+(t==='login'?' active':'');
  document.getElementById('tabR').className = 'auth-tab'+(t==='register'?' active':'');
}

async function doLogin() {
  try {
    const r = await api('/api/auth/login','POST',{
      email: document.getElementById('lEmail').value,
      password: document.getElementById('lPass').value
    });
    S.token=r.token; S.user=r.user;
    localStorage.setItem('tok',S.token);
    showApp();
  } catch(e){ toast(e.message,'error'); }
}

async function doRegister() {
  const ref = document.getElementById('rRef').value.trim() || localStorage.getItem('pending_ref')||'';
  try {
    const r = await api('/api/auth/register','POST',{
      username: document.getElementById('rUser').value.trim(),
      email:    document.getElementById('rEmail').value.trim(),
      password: document.getElementById('rPass').value,
      ref_code: ref
    });
    S.token=r.token; S.user=r.user;
    localStorage.setItem('tok',S.token);
    localStorage.removeItem('pending_ref');
    showApp();
    toast('Welcome to DTIP! 🎉','success');
  } catch(e){ toast(e.message,'error'); }
}

function logout(){
  S.token=null; S.user=null;
  localStorage.removeItem('tok');
  S.socket?.disconnect();
  showAuth();
}

/* ════════════════════════════════════
   APP INIT
════════════════════════════════════ */
function initApp() {
  const u = S.user;
  const av = document.getElementById('sbAv');
  av.innerHTML = u.avatar_url ? `<img src="${u.avatar_url}">` : u.username[0].toUpperCase();
  document.getElementById('sbName').textContent = u.username;
  document.getElementById('sbRole').textContent = u.role==='admin'?'👑 Admin':
    (u.is_premium?'⭐ Premium':'🆓 Free')+(u.is_activated?'':' · Inactive');
  if (u.role==='admin') document.getElementById('adminNav').style.display='block';
  initSocket();
  loadNotifCount();
  go('home');
}

function initSocket() {
  S.socket = io({ query:{ token: S.token } });
  S.socket.on('connected', ()=>{});
  S.socket.on('receive_message', msg => {
    if (S.page==='messages' && S.activeChat===msg.sender_id) appendBubble(msg,false);
    else { S.unreadMsg++; badges(); toast(`💬 ${msg.sender_name}: ${msg.message.slice(0,50)}`,'info'); }
  });
  S.socket.on('broadcast', msg => {
    document.getElementById('bcastTxt').textContent = msg.message;
    document.getElementById('bcastBar').classList.add('show');
    toast('📢 '+msg.message.slice(0,60),'info');
  });
  S.socket.on('notification', n => { S.unreadNotif++; badges(); toast(n.title, n.type||'info'); });
  S.socket.on('new_task', t => { if(S.user.is_activated) toast(`🆕 ${t.title} — KES ${t.reward}`,'success'); });
}

function badges(){
  const mb=document.getElementById('msgBadge'), nb=document.getElementById('notifBadge'), nd=document.getElementById('notifDot');
  mb.style.display=S.unreadMsg>0?'block':'none';   mb.textContent=S.unreadMsg;
  nb.style.display=S.unreadNotif>0?'block':'none'; nb.textContent=S.unreadNotif;
  nd.style.display=S.unreadNotif>0?'block':'none';
}

async function loadNotifCount(){
  try{ const r=await api('/api/notifications'); S.unreadNotif=r.unread; badges(); }catch{}
}

/* ════════════════════════════════════
   NAVIGATION
════════════════════════════════════ */
function go(page){
  S.page=page;
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('nav-'+page)?.classList.add('active');
  const titles={home:'Dashboard',tasks:'Tasks',wallet:'Wallet',shares:'Shares',
    referrals:'Referral Program',messages:'Messages',notifications:'Notifications',
    admin:'Admin Panel',adminUsers:'User Management',adminSettings:'Platform Settings'};
  document.getElementById('pageTitle').textContent = titles[page]||page;
  document.getElementById('content').innerHTML = '<div class="spinner"></div>';
  const pages={home,tasks,wallet,shares,referrals,messages,notifications,admin,adminUsers,adminSettings};
  pages[page]?.();
}

/* ════════════════════════════════════
   DASHBOARD
════════════════════════════════════ */
async function home(){
  const el=document.getElementById('content');
  try{
    const [me,pub] = await Promise.all([api('/api/auth/me'), api('/api/stats')]);
    S.user = me.user;
    const u=S.user, w=me.wallet||{balance:0,total_earned:0,escrow:0};
    const done=u.daily_done||0, lim=u.daily_limit||3;
    const pct=Math.min((done/lim)*100,100);

    let adminBlock='';
    if(u.role==='admin'){
      try{
        const st=await api('/api/admin/stats');
        adminBlock=`<div class="stats" style="margin-top:22px">
          <div class="stat"><div class="stat-glow" style="background:var(--purple)"></div><div class="stat-icon">👥</div><div class="stat-val">${st.total_users}</div><div class="stat-lbl">Total Users</div></div>
          <div class="stat"><div class="stat-glow" style="background:var(--gold)"></div><div class="stat-icon">🔓</div><div class="stat-val">${st.activated}</div><div class="stat-lbl">Activated</div></div>
          <div class="stat"><div class="stat-glow" style="background:var(--green)"></div><div class="stat-icon">✅</div><div class="stat-val">${st.completions}</div><div class="stat-lbl">Completions</div></div>
          <div class="stat"><div class="stat-glow" style="background:var(--blue)"></div><div class="stat-icon">💰</div><div class="stat-val" style="font-size:22px">KES ${st.wallet_total.toLocaleString()}</div><div class="stat-lbl">Platform Balance</div></div>
        </div>`;
      }catch{}
    }

    // Load settings for fee display
    try{ const s=await api('/api/settings'); S.settings=s; S.sharePrice=parseFloat(s.share_price)||100; }catch{}

    el.innerHTML = `
      ${!u.is_activated && u.role!=='admin' ? `
      <div class="gate">
        <div class="gate-icon">🔒</div>
        <h2>Activate to Start Earning</h2>
        <p>Pay a one-time activation fee to unlock task earning on DTIP. Start completing tasks and withdrawing money right away.</p>
        <div class="gate-fee"><div class="gate-fee-val">KES ${parseFloat(S.settings.activation_fee||299).toFixed(0)}</div><div class="gate-fee-lbl">One-time activation fee</div></div>
        <button class="btn btn-gold btn-block" style="font-size:15px;padding:14px" onclick="openActivate()">🚀 Pay & Activate Now</button>
      </div>` : ''}

      <div class="stats">
        <div class="stat"><div class="stat-glow" style="background:var(--green)"></div><div class="stat-icon">💰</div><div class="stat-val">KES ${(w.balance||0).toFixed(0)}</div><div class="stat-lbl">Wallet Balance</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--blue)"></div><div class="stat-icon">🎯</div><div class="stat-val">KES ${(w.total_earned||0).toFixed(0)}</div><div class="stat-lbl">Total Earned</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--purple)"></div><div class="stat-icon">📋</div><div class="stat-val">${pub.tasks}</div><div class="stat-lbl">Available Tasks</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--gold)"></div><div class="stat-icon">✅</div><div class="stat-val">${pub.completions}</div><div class="stat-lbl">Platform Completions</div></div>
      </div>

      ${u.is_activated && u.role!=='admin' ? `
      <div class="daily-bar">
        <div class="stat-icon" style="font-size:28px;margin:0">🎯</div>
        <div class="daily-meta">
          <div class="daily-lbl">Tasks Today</div>
          <div class="daily-count">${done} / ${lim} ${u.is_premium?'<span class="badge badge-gold" style="font-size:10px;margin-left:6px">⭐ Premium</span>':''}</div>
          <div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>
        </div>
        ${!u.is_premium?`<button class="btn-purple btn-sm" onclick="loadPremFee();openM('mPremium')">⭐ Upgrade</button>`:''}
      </div>` : ''}

      <div class="two-col">
        <div class="card">
          <div class="card-hd"><div class="card-title">Quick Actions</div></div>
          <div class="card-body" style="display:flex;flex-direction:column;gap:10px">
            <button class="btn btn-block" onclick="go('tasks')">📋 Browse & Complete Tasks</button>
            <button class="btn-ghost btn-block" onclick="go('wallet')">💰 Manage Wallet</button>
            <button class="btn-ghost btn-block" onclick="go('referrals')">🔗 Share Referral Link</button>
            <button class="btn-ghost btn-block" onclick="go('shares')">📈 Buy Platform Shares</button>
            ${!u.is_premium&&u.is_activated?`<button class="btn-purple btn-block" onclick="loadPremFee();openM('mPremium')">⭐ Upgrade to Premium</button>`:''}
            ${u.role==='admin'?`<button class="btn btn-block" onclick="openM('mNewTask')">➕ Create Task</button>`:''}
          </div>
        </div>
        <div class="card">
          <div class="card-hd"><div class="card-title">Account Overview</div></div>
          <div class="card-body">
            <table>
              <tr><td style="color:var(--text3);width:46%">Username</td><td><strong>${esc(u.username)}</strong></td></tr>
              <tr><td style="color:var(--text3)">Status</td><td>${u.is_activated?'<span class="badge badge-g">✅ Active</span>':'<span class="badge badge-gold">⚠️ Inactive</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Tier</td><td>${u.is_premium?'<span class="badge badge-gold">⭐ Premium</span>':'<span class="badge badge-dim">Free</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Verified</td><td>${u.is_verified?'<span class="badge badge-g">✔ Verified</span>':'<span class="badge badge-dim">Not verified</span>'}</td></tr>
              <tr><td style="color:var(--text3)">Referral Code</td><td><code style="color:var(--blue);font-size:13px">${u.referral_code}</code></td></tr>
              <tr><td style="color:var(--text3)">Escrow</td><td>KES ${(w.escrow||0).toFixed(2)}</td></tr>
            </table>
          </div>
        </div>
      </div>
      ${adminBlock}`;
  }catch(e){ document.getElementById('content').innerHTML=`<div class="empty"><div class="empty-ic">⚠️</div><h3>Error</h3><p>${e.message}</p></div>`; }
}

/* ════════════════════════════════════
   TASKS PAGE
════════════════════════════════════ */
async function tasks(){
  const el=document.getElementById('content');
  el.innerHTML=`
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap">
      <input type="text" id="tQ" placeholder="Search tasks…" style="max-width:260px" oninput="loadTasks()">
      <select id="tCat" style="max-width:170px" onchange="loadTasks()">
        <option value="">All Categories</option>
        <option>Data Entry</option><option>Writing</option><option>Design</option>
        <option>Social Media</option><option>Research</option><option>Tech</option>
        <option>Marketing</option><option>Survey</option><option>Other</option>
      </select>
      ${S.user.role==='admin'?`<button class="btn" style="margin-left:auto" onclick="openM('mNewTask')">➕ Create Task</button>`:''}
    </div>
    ${!S.user.is_activated&&S.user.role!=='admin'?`
    <div style="background:var(--gold-d);border:1.5px solid var(--gold);border-radius:var(--r-lg);padding:16px 20px;margin-bottom:20px;display:flex;align-items:center;gap:14px">
      <span style="font-size:26px">🔒</span>
      <div><div style="font-weight:700;margin-bottom:3px">Activate to Start Earning</div>
      <div style="font-size:13px;color:var(--text2)">Pay a one-time activation fee to complete tasks and earn money.</div></div>
      <button class="btn btn-gold btn-sm" style="margin-left:auto;white-space:nowrap" onclick="openActivate()">Activate →</button>
    </div>`:''}
    <div id="tGrid" class="task-grid"></div>
    <div id="tPager" style="text-align:center;margin-top:20px"></div>`;
  loadTasks();
}

const taskIcons={'Data Entry':'📊','Writing':'✍️','Design':'🎨','Social Media':'📱','Research':'🔬','Tech':'💻','Marketing':'📣','Survey':'📝','Other':'⚡'};
let currentPage=1;

async function loadTasks(page=1){
  currentPage=page;
  const q=document.getElementById('tQ')?.value||'';
  const cat=document.getElementById('tCat')?.value||'';
  try{
    const r=await api(`/api/tasks?page=${page}&q=${encodeURIComponent(q)}&category=${encodeURIComponent(cat)}`);
    const grid=document.getElementById('tGrid');
    if(!r.tasks.length){ grid.innerHTML='<div class="empty" style="grid-column:1/-1"><div class="empty-ic">📭</div><h3>No Tasks Found</h3><p>Check back soon!</p></div>'; return; }
    grid.innerHTML=r.tasks.map(t=>`
      <div class="task-card" onclick="openTask(${t.id})">
        <div class="task-ic">${taskIcons[t.category]||'⚡'}</div>
        <div class="task-title">${esc(t.title)}</div>
        <div class="task-desc">${esc(t.description)}</div>
        <div class="task-foot">
          <div class="task-reward">KES ${t.reward.toLocaleString()}</div>
          <span class="task-cat">${esc(t.category)}</span>
          ${t.user_done?'<span class="badge badge-g">✅ Done</span>':''}
          ${t.is_flagged?'<span class="badge badge-r">🚩</span>':''}
          <span style="font-size:11px;color:var(--text3);margin-left:auto">${t.completion_count} done</span>
        </div>
      </div>`).join('');
    const pager=document.getElementById('tPager');
    pager.innerHTML=r.pages>1?Array.from({length:r.pages},(_,i)=>
      `<button class="${i+1===page?'btn':'btn-ghost'} btn-sm" style="margin:3px" onclick="loadTasks(${i+1})">${i+1}</button>`
    ).join(''):'';
  }catch(e){toast(e.message,'error');}
}

async function openTask(id){
  try{
    const r=await api('/api/tasks/'+id);
    const t=r.task;
    document.getElementById('mDoTitle').textContent = esc(t.title);
    document.getElementById('mDoInfo').innerHTML=`
      <div style="background:var(--sf2);border-radius:var(--r);padding:16px">
        <div style="font-size:13px;color:var(--text2);line-height:1.65;margin-bottom:12px">${esc(t.description)}</div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="task-reward">KES ${t.reward.toLocaleString()}</span>
          <span class="task-cat">${esc(t.category)}</span>
          ${t.user_done?'<span class="badge badge-g">✅ Already Completed</span>':''}
          ${t.deadline?`<span class="badge badge-dim">📅 ${fmt(t.deadline)}</span>`:''}
        </div>
        ${S.user.role==='admin'?`<div style="margin-top:12px;display:flex;gap:8px">
          <button class="btn-danger btn-xs" onclick="closeM('mDoTask');deactivateTask(${id})">Deactivate</button>
          <button class="btn-ghost btn-xs" onclick="closeM('mDoTask');toggleFlag(${id},${t.is_flagged})">${t.is_flagged?'Unflag':'🚩 Flag'}</button>
        </div>`:''}
      </div>`;
    const proof=document.getElementById('mDoProof');
    proof.value = t.completion?.proof||'';
    proof.disabled = !!t.user_done;
    document.querySelector('#mDoTask .modal-foot .btn').style.display = t.user_done?'none':'';
    // store task id
    document.getElementById('mDoTask').dataset.tid = id;
    openM('mDoTask');
  }catch(e){toast(e.message,'error');}
}

async function submitDoTask(){
  const tid=document.getElementById('mDoTask').dataset.tid;
  const proof=document.getElementById('mDoProof').value;
  try{
    const r=await api(`/api/tasks/${tid}/do`,'POST',{proof});
    closeM('mDoTask');
    toast(`✅ Done! KES ${r.earned} added to wallet`,'success');
    S.user.daily_done=(S.user.daily_done||0)+1;
    loadTasks(currentPage);
  }catch(e){
    if(e.message.includes('activate')){ closeM('mDoTask'); openActivate(); }
    else if(e.message.includes('limit')||e.message.includes('Daily')){
      toast(e.message,'warn');
      setTimeout(()=>{ loadPremFee(); openM('mPremium'); },600);
    } else toast(e.message,'error');
  }
}

async function deactivateTask(id){ try{ await api('/api/tasks/'+id,'PUT',{is_active:false}); toast('Task deactivated','info'); loadTasks(); }catch(e){toast(e.message,'error');} }
async function toggleFlag(id,flagged){ try{ await api('/api/tasks/'+id,'PUT',{is_flagged:!flagged}); toast('Flag updated','info'); loadTasks(); }catch(e){toast(e.message,'error');} }

async function submitTask(){
  try{
    await api('/api/tasks','POST',{
      title:       document.getElementById('ntTitle').value,
      description: document.getElementById('ntDesc').value,
      category:    document.getElementById('ntCat').value,
      reward:      parseFloat(document.getElementById('ntReward').value),
      deadline:    document.getElementById('ntDeadline').value||null
    });
    closeM('mNewTask');
    toast('Task published!','success');
    if(S.page==='tasks') loadTasks();
    if(S.page==='admin')  admin();
  }catch(e){toast(e.message,'error');}
}

/* ════════════════════════════════════
   WALLET
════════════════════════════════════ */
async function wallet(){
  const el=document.getElementById('content');
  try{
    const r=await api('/api/wallet');
    const w=r.wallet;
    el.innerHTML=`
      <div class="w-hero">
        <div class="w-lbl">Available Balance</div>
        <div class="w-balance">KES ${w.balance.toFixed(2)}</div>
        <div class="w-sub">
          Escrow: KES ${w.escrow.toFixed(2)} &nbsp;·&nbsp;
          Earned: KES ${w.total_earned.toFixed(2)} &nbsp;·&nbsp;
          Spent: KES ${w.total_spent.toFixed(2)}
        </div>
        <div class="w-actions">
          <button class="w-btn" onclick="document.getElementById('mDepDemo').style.display='block';openM('mDeposit')">⬆ Deposit</button>
          <button class="w-btn" onclick="openM('mWithdraw')">⬇ Withdraw</button>
          <button class="w-btn" onclick="go('shares')">📈 Buy Shares</button>
        </div>
      </div>
      <div class="card">
        <div class="card-hd"><div class="card-title">Transaction History</div></div>
        <div class="card-body">
          ${r.ledger.length?`<div class="tbl-wrap"><table>
            <thead><tr><th>Type</th><th>Amount</th><th>Balance After</th><th>Description</th><th>Date</th></tr></thead>
            <tbody>${r.ledger.map(l=>`<tr>
              <td><span class="badge ${['deposit','task_reward','referral_bonus'].includes(l.type)?'badge-g':'badge-r'}">${l.type.replace('_',' ')}</span></td>
              <td><strong style="color:${l.amount>=0?'var(--green)':'var(--red)'}">${l.amount>=0?'+':''}KES ${Math.abs(l.amount).toFixed(2)}</strong></td>
              <td>KES ${l.balance_after.toFixed(2)}</td>
              <td style="color:var(--text3);font-size:12px">${esc(l.description||'')}</td>
              <td style="color:var(--text3);font-size:12px">${fmt(l.created_at)}</td>
            </tr>`).join('')}</tbody>
          </table></div>`:'<div class="empty"><div class="empty-ic">📊</div><h3>No Transactions</h3></div>'}
        </div>
      </div>`;
  }catch(e){document.getElementById('content').innerHTML=`<p style="color:var(--red);padding:20px">${e.message}</p>`;}
}

async function doDeposit(){
  try{
    await api('/api/wallet/deposit','POST',{
      amount:parseFloat(document.getElementById('mDepAmt').value),
      phone:document.getElementById('mDepPhone').value
    });
    closeM('mDeposit');
    toast('Deposit successful!','success');
    if(S.page==='wallet') wallet();
  }catch(e){toast(e.message,'error');}
}

async function doWithdraw(){
  try{
    await api('/api/wallet/withdraw','POST',{
      amount:parseFloat(document.getElementById('mWitAmt').value),
      phone:document.getElementById('mWitPhone').value
    });
    closeM('mWithdraw');
    toast('Withdrawal initiated!','success');
    if(S.page==='wallet') wallet();
  }catch(e){toast(e.message,'error');}
}

/* ════════════════════════════════════
   SHARES
════════════════════════════════════ */
async function shares(){
  const el=document.getElementById('content');
  try{
    const sets=await api('/api/settings');
    S.settings=sets; S.sharePrice=parseFloat(sets.share_price)||100;
    const [r,w]=await Promise.all([api('/api/shares'),api('/api/wallet')]);
    el.innerHTML=`
      <div class="share-hero">
        <div style="font-size:11px;color:var(--text2);letter-spacing:1.5px;text-transform:uppercase;font-weight:700;margin-bottom:8px">YOUR PORTFOLIO</div>
        <div style="font-family:'Cabinet Grotesk',sans-serif;font-size:42px;font-weight:900;margin-bottom:4px">
          ${r.total_shares} <span style="font-size:18px;font-weight:600;color:var(--text2)">shares</span>
        </div>
        <div style="color:var(--text3);font-size:13px;margin-bottom:4px">
          Portfolio Value: <strong style="color:var(--green)">KES ${r.portfolio_value.toLocaleString()}</strong>
        </div>
        <div style="color:var(--text3);font-size:13px">Share Price: <strong>KES ${r.share_price} each</strong></div>
        <div style="margin-top:20px"><button class="btn" onclick="openBuyShares()">📈 Buy More Shares</button></div>
      </div>
      <div class="two-col" style="margin-bottom:20px">
        <div class="card">
          <div class="card-hd"><div class="card-title">About Shares</div></div>
          <div class="card-body" style="font-size:13.5px;color:var(--text2);line-height:1.75">
            💹 Buy shares using your wallet balance<br>
            📈 Share value grows with the platform<br>
            🎯 Early investors get the best prices<br>
            💎 No maximum — buy as many as you want<br>
            🔒 Shares are deducted from your balance
          </div>
        </div>
        <div class="card">
          <div class="card-hd"><div class="card-title">Wallet Balance</div></div>
          <div class="card-body">
            <div style="font-family:'Cabinet Grotesk',sans-serif;font-size:34px;font-weight:900;color:var(--green);margin-bottom:6px">KES ${(w.wallet.balance||0).toFixed(0)}</div>
            <div style="color:var(--text3);font-size:12px;margin-bottom:16px">Available for share purchase</div>
            <button class="btn-ghost btn-sm" onclick="document.getElementById('mDepDemo').style.display='block';openM('mDeposit')">⬆ Add Funds</button>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-hd"><div class="card-title">Purchase History</div></div>
        <div class="card-body">
          ${r.shares.length?`<div class="tbl-wrap"><table>
            <thead><tr><th>Shares</th><th>Price Each</th><th>Total Paid</th><th>Date</th></tr></thead>
            <tbody>${r.shares.map(s=>`<tr>
              <td><strong>${s.quantity}</strong></td>
              <td>KES ${s.price_each.toFixed(0)}</td>
              <td>KES ${s.total_paid.toFixed(0)}</td>
              <td style="color:var(--text3)">${fmt(s.purchased_at)}</td>
            </tr>`).join('')}</tbody>
          </table></div>`:'<div class="empty"><div class="empty-ic">📈</div><h3>No Shares Yet</h3><p>Buy your first shares above!</p></div>'}
        </div>
      </div>`;
  }catch(e){document.getElementById('content').innerHTML=`<p style="color:var(--red);padding:20px">${e.message}</p>`;}
}

function openBuyShares(){
  const p=S.sharePrice;
  document.getElementById('mSharePrice').innerHTML=`
    <div style="font-size:12px;color:var(--text3);margin-bottom:4px">Current Price</div>
    <div style="font-family:'Cabinet Grotesk',sans-serif;font-size:38px;font-weight:900;color:var(--purple)">KES ${p}</div>`;
  document.getElementById('mShareQty').value=1;
  document.getElementById('mShareTotal').textContent=`Total: KES ${p}`;
  openM('mBuyShares');
}
function updateShareTotal(){
  const qty=parseInt(document.getElementById('mShareQty').value)||0;
  document.getElementById('mShareTotal').textContent=`Total: KES ${(qty*S.sharePrice).toLocaleString()}`;
}
async function doBuyShares(){
  const qty=parseInt(document.getElementById('mShareQty').value);
  if(!qty||qty<1){toast('Enter at least 1 share','warn');return;}
  try{
    await api('/api/shares/buy','POST',{quantity:qty});
    closeM('mBuyShares');
    toast(`📈 Bought ${qty} share(s)!`,'success');
    if(S.page==='shares') shares();
  }catch(e){toast(e.message,'error');}
}

/* ════════════════════════════════════
   REFERRALS
════════════════════════════════════ */
async function referrals(){
  const el=document.getElementById('content');
  const u=S.user;
  let s={activation_fee:299,referral_bonus:100};
  try{s=await api('/api/settings');}catch{}
  const bonus=parseFloat(s.referral_bonus||100).toFixed(0);
  const actFee=parseFloat(s.activation_fee||299).toFixed(0);
  const link=u.ref_link||`${location.origin}/?ref=${u.referral_code}`;

  el.innerHTML=`
    <div class="ref-box">
      <div style="font-family:'Cabinet Grotesk',sans-serif;font-weight:900;font-size:20px;margin-bottom:8px">🔗 Your Referral Link</div>
      <div style="font-size:13px;color:var(--text2);margin-bottom:10px">
        Share your link. When someone registers through it and pays the activation fee, you earn <strong style="color:var(--green)">KES ${bonus}</strong> automatically!
      </div>
      <div class="ref-link" id="refLink" onclick="copyLink()">${esc(link)}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">
        <button class="btn btn-sm" onclick="copyLink()">📋 Copy Link</button>
        <button class="btn-ghost btn-sm" onclick="shareWA()">💬 WhatsApp</button>
        <button class="btn-ghost btn-sm" onclick="shareNative()">📤 Share</button>
      </div>
    </div>
    <div class="stats" style="margin-bottom:22px">
      <div class="stat"><div class="stat-glow" style="background:var(--green)"></div><div class="stat-icon">🏷️</div>
        <div style="font-family:'Cabinet Grotesk',sans-serif;font-size:22px;font-weight:900;letter-spacing:2px;color:var(--green)">${u.referral_code}</div>
        <div class="stat-lbl">Your Code</div></div>
      <div class="stat"><div class="stat-glow" style="background:var(--gold)"></div><div class="stat-icon">💰</div>
        <div class="stat-val">KES ${bonus}</div><div class="stat-lbl">Per Activation</div></div>
    </div>
    <div class="card">
      <div class="card-hd"><div class="card-title">How the Referral Program Works</div></div>
      <div class="card-body">
        <div class="steps">
          <div class="step"><div class="step-num">1</div><div class="step-body"><strong>Share your unique link</strong><span>Send it to friends, post on social media, WhatsApp groups, etc.</span></div></div>
          <div class="step"><div class="step-num">2</div><div class="step-body"><strong>Friend registers using your link</strong><span>They click your link and create an account on DTIP</span></div></div>
          <div class="step"><div class="step-num">3</div><div class="step-body"><strong>Friend pays activation fee</strong><span>They pay the KES ${actFee} one-time activation fee to start earning</span></div></div>
          <div class="step"><div class="step-num">4</div><div class="step-body"><strong>You earn KES ${bonus}!</strong><span>Automatically credited to your wallet the moment they activate</span></div></div>
        </div>
        <div class="divh"></div>
        <div style="background:var(--sf2);border-radius:var(--r);padding:14px;font-size:13px;color:var(--text2)">
          💡 <strong>Pro tip:</strong> Share in WhatsApp groups, Facebook, Twitter, and TikTok. There's no limit to how many people you can refer!
        </div>
      </div>
    </div>`;
}

function copyLink(){
  const l=document.getElementById('refLink').textContent;
  navigator.clipboard?.writeText(l).then(()=>toast('Link copied!','success')).catch(()=>{
    const t=document.createElement('textarea');t.value=l;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();toast('Link copied!','success');
  });
}
function shareNative(){
  const l=document.getElementById('refLink').textContent;
  if(navigator.share) navigator.share({title:'Join DTIP',text:'Earn money online in Kenya!',url:l});
  else copyLink();
}
function shareWA(){
  const l=encodeURIComponent(document.getElementById('refLink').textContent);
  window.open(`https://wa.me/?text=Join%20DTIP%20and%20earn%20money%20online%20in%20Kenya!%20${l}`,'_blank');
}

/* ════════════════════════════════════
   MESSAGES
════════════════════════════════════ */
async function messages(){
  S.unreadMsg=0; badges();
  const el=document.getElementById('content');
  el.innerHTML=`
    <div class="chat-shell">
      <div class="conv-list">
        <div class="conv-hd">Conversations</div>
        <div id="convList"><div class="spinner" style="margin:20px auto"></div></div>
      </div>
      <div class="chat-area">
        <div class="chat-top" id="chatTop"><span style="color:var(--text3)">Select a conversation to start chatting</span></div>
        <div class="chat-msgs" id="chatMsgs">
          <div class="empty"><div class="empty-ic">💬</div><h3>No Chat Open</h3><p>Pick a contact on the left.</p></div>
        </div>
        <div class="chat-input-area" id="chatInputArea" style="display:none">
          <input type="text" id="msgInput" placeholder="Type a message…" onkeypress="if(event.key==='Enter')sendMsg()">
          <button class="btn" onclick="sendMsg()">Send</button>
        </div>
      </div>
    </div>`;
  loadConvs();
}

async function loadConvs(){
  try{
    const r=await api('/api/messages/conversations');
    const el=document.getElementById('convList');
    let html='';
    if(S.user.role!=='admin'){
      // Members can always message admin support
      html+=`<div class="conv-item" onclick="startChat(1,'Admin Support')">
        <div class="conv-av" style="background:var(--gold-d);color:var(--gold)">👑</div>
        <div class="conv-meta"><div class="conv-name">Admin Support</div><div class="conv-prev">Tap to chat</div></div>
      </div>`;
    }
    html+=r.conversations.map(c=>`
      <div class="conv-item${S.activeChat===c.user.id?' active':''}" onclick="startChat(${c.user.id},'${esc(c.user.username)}')">
        <div class="conv-av">${c.user.username[0].toUpperCase()}</div>
        <div class="conv-meta">
          <div class="conv-name">${esc(c.user.username)}${c.user.is_verified?' ✔':''}</div>
          <div class="conv-prev">${esc(c.last?.message||'No messages yet')}</div>
        </div>
        ${c.unread>0?`<div class="conv-unread">${c.unread}</div>`:''}
      </div>`).join('');
    el.innerHTML=html||'<div style="padding:20px;color:var(--text3);text-align:center;font-size:13px">No conversations</div>';
  }catch{}
}

async function startChat(uid,name){
  S.activeChat=uid; S.chatPartner=name;
  document.getElementById('chatTop').innerHTML=`
    <div class="conv-av">${name[0].toUpperCase()}</div>
    <strong>${esc(name)}</strong>`;
  document.getElementById('chatInputArea').style.display='flex';
  document.querySelectorAll('.conv-item').forEach(el=>{
    el.classList.toggle('active', el.onclick?.toString().includes(`(${uid},`));
  });
  try{
    const r=await api('/api/messages/'+uid);
    const box=document.getElementById('chatMsgs');
    if(!r.messages.length){
      box.innerHTML='<div class="empty"><div class="empty-ic">💬</div><p>No messages yet. Say hello!</p></div>';
    }else{
      box.innerHTML=r.messages.map(m=>bubbleHTML(m)).join('');
      box.scrollTop=box.scrollHeight;
    }
    S.socket?.emit('mark_read',{sender_id:uid});
  }catch(e){toast(e.message,'error');}
}

function bubbleHTML(m){
  const mine=m.sender_id===S.user.id;
  return `<div class="bbl-wrap ${mine?'mine':''}">
    <div class="bbl ${mine?'mine':'theirs'}">${esc(m.message)}</div>
    <div class="bbl-time">${fmtT(m.created_at)}</div>
  </div>`;
}

function appendBubble(m,mine=false){
  const box=document.getElementById('chatMsgs');
  if(!box)return;
  box.insertAdjacentHTML('beforeend',bubbleHTML({...m,sender_id:mine?S.user.id:m.sender_id}));
  box.scrollTop=box.scrollHeight;
}

function sendMsg(){
  const inp=document.getElementById('msgInput');
  const txt=inp.value.trim();
  if(!txt||!S.activeChat)return;
  S.socket?.emit('send_message',{receiver_id:S.activeChat,message:txt});
  appendBubble({message:txt,sender_id:S.user.id,created_at:new Date().toISOString()},true);
  inp.value='';
}

/* ════════════════════════════════════
   NOTIFICATIONS
════════════════════════════════════ */
async function notifications(){
  const el=document.getElementById('content');
  try{
    await api('/api/notifications/read-all','POST');
    S.unreadNotif=0; badges();
    const r=await api('/api/notifications');
    const ico={success:'✅',error:'❌',info:'ℹ️',warn:'⚠️',message:'💬'};
    el.innerHTML=`<div class="card">
      <div class="card-hd"><div class="card-title">Notifications</div></div>
      <div class="card-body">
        ${r.notifications.length?r.notifications.map(n=>`
          <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--border);align-items:flex-start">
            <span style="font-size:22px">${ico[n.type]||'🔔'}</span>
            <div>
              <div style="font-weight:700;margin-bottom:3px">${esc(n.title)}</div>
              <div style="color:var(--text2);font-size:13px">${esc(n.body)}</div>
              <div style="color:var(--text3);font-size:11px;margin-top:4px">${fmt(n.created_at)} ${fmtT(n.created_at)}</div>
            </div>
          </div>`).join(''):'<div class="empty"><div class="empty-ic">🔔</div><h3>All Clear</h3><p>No notifications yet.</p></div>'}
      </div>
    </div>`;
  }catch{}
}

/* ════════════════════════════════════
   ADMIN PANEL
════════════════════════════════════ */
async function admin(){
  const el=document.getElementById('content');
  try{
    const st=await api('/api/admin/stats');
    el.innerHTML=`
      <div style="display:flex;gap:10px;margin-bottom:22px;flex-wrap:wrap">
        <button class="btn" onclick="openM('mNewTask')">➕ Create Task</button>
        <button class="btn-ghost" onclick="openM('mBroadcast')">📢 Broadcast</button>
        <button class="btn-ghost" onclick="go('adminUsers')">👥 Users</button>
        <button class="btn-ghost" onclick="go('adminSettings')">🛠 Settings</button>
      </div>
      <div class="stats" style="margin-bottom:22px">
        <div class="stat"><div class="stat-glow" style="background:var(--purple)"></div><div class="stat-icon">👥</div><div class="stat-val">${st.total_users}</div><div class="stat-lbl">Total Users</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--green)"></div><div class="stat-icon">🔓</div><div class="stat-val">${st.activated}</div><div class="stat-lbl">Activated</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--gold)"></div><div class="stat-icon">⭐</div><div class="stat-val">${st.premium_users}</div><div class="stat-lbl">Premium</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--blue)"></div><div class="stat-icon">📋</div><div class="stat-val">${st.active_tasks}</div><div class="stat-lbl">Active Tasks</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--green)"></div><div class="stat-icon">✅</div><div class="stat-val">${st.completions}</div><div class="stat-lbl">Completions</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--purple)"></div><div class="stat-icon">📈</div><div class="stat-val">${st.total_shares}</div><div class="stat-lbl">Shares Sold</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--gold)"></div><div class="stat-icon">🎯</div><div class="stat-val">${st.today_completions}</div><div class="stat-lbl">Today Completions</div></div>
        <div class="stat"><div class="stat-glow" style="background:var(--red)"></div><div class="stat-icon">💰</div><div class="stat-val" style="font-size:20px">KES ${st.wallet_total.toLocaleString()}</div><div class="stat-lbl">Platform Balance</div></div>
      </div>
      <div class="two-col">
        <div class="card">
          <div class="card-hd"><div class="card-title">Recent Completions</div></div>
          <div class="card-body" id="recentComps"><div class="spinner"></div></div>
        </div>
        <div class="card">
          <div class="card-hd"><div class="card-title">Active Tasks</div><button class="btn btn-sm" onclick="openM('mNewTask')">➕ New</button></div>
          <div class="card-body" id="activeTasks"><div class="spinner"></div></div>
        </div>
      </div>`;

    const [comps,tasks]=await Promise.all([api('/api/admin/completions'),api('/api/tasks')]);
    document.getElementById('recentComps').innerHTML=comps.completions.slice(0,8).map(c=>
      `<div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">
        <span>✅</span><div style="flex:1"><div style="font-weight:600;font-size:13px">${esc(c.username||'?')}</div><div style="font-size:11px;color:var(--text3)">${fmt(c.created_at)}</div></div>
      </div>`
    ).join('')||'<p style="color:var(--text3);font-size:13px">No completions yet</p>';
    document.getElementById('activeTasks').innerHTML=tasks.tasks.slice(0,6).map(t=>
      `<div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1"><div style="font-weight:600;font-size:13px">${esc(t.title)}</div><div style="font-size:11px;color:var(--text3)">${t.category} · ${t.completion_count} done</div></div>
        <strong style="color:var(--green);white-space:nowrap">KES ${t.reward}</strong>
        <button class="btn-danger btn-xs" onclick="deactivateTask(${t.id})">✕</button>
      </div>`
    ).join('')||'<p style="color:var(--text3);font-size:13px">No tasks</p>';
  }catch(e){document.getElementById('content').innerHTML=`<p style="color:var(--red);padding:20px">${e.message}</p>`;}
}

async function adminUsers(){
  const el=document.getElementById('content');
  el.innerHTML=`
    <div style="display:flex;gap:12px;margin-bottom:20px">
      <input type="text" id="uQ" placeholder="Search users…" style="max-width:280px" oninput="loadUsers()">
    </div>
    <div class="card">
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>User</th><th>Status</th><th>Tier</th><th>Role</th><th>Joined</th><th>Actions</th></tr></thead>
          <tbody id="uTbody"><tr><td colspan="6"><div class="spinner" style="margin:20px auto"></div></td></tr></tbody>
        </table>
      </div>
    </div>`;
  loadUsers();
}

async function loadUsers(){
  const q=document.getElementById('uQ')?.value||'';
  try{
    const r=await api(`/api/admin/users?q=${encodeURIComponent(q)}`);
    document.getElementById('uTbody').innerHTML=r.users.map(u=>`<tr>
      <td>
        <div style="display:flex;align-items:center;gap:10px">
          <div class="u-avatar" style="width:32px;height:32px;font-size:12px;border-radius:8px;flex-shrink:0">${u.username[0].toUpperCase()}</div>
          <div>
            <div style="font-weight:700">${esc(u.username)} ${u.is_verified?'<span style="color:var(--green)">✔</span>':''}</div>
            <div style="font-size:11px;color:var(--text3)">${esc(u.email)}</div>
          </div>
        </div>
      </td>
      <td>
        ${u.is_activated?'<span class="badge badge-g">Active</span>':'<span class="badge badge-gold">Inactive</span>'}
        ${!u.is_active?'<span class="badge badge-r" style="margin-left:4px">Suspended</span>':''}
      </td>
      <td>${u.tier==='premium'?'<span class="badge badge-gold">⭐ Premium</span>':'<span class="badge badge-dim">Free</span>'}</td>
      <td><span class="badge ${u.role==='admin'?'badge-p':'badge-dim'}">${u.role}</span></td>
      <td style="color:var(--text3);font-size:12px">${fmt(u.created_at)}</td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn-ghost btn-xs" onclick="adminVerify(${u.id})">${u.is_verified?'Unverify':'Verify'}</button>
          <button class="btn-ghost btn-xs" onclick="adminToggleActive(${u.id})">${u.is_active?'Suspend':'Restore'}</button>
          <button class="btn-ghost btn-xs" onclick="adminGivePremium(${u.id})">⭐</button>
          <button class="btn-ghost btn-xs" onclick="startChat(${u.id},'${esc(u.username)}');go('messages')">💬</button>
        </div>
      </td>
    </tr>`).join('');
  }catch{}
}

async function adminVerify(uid){try{await api('/api/admin/users/'+uid+'/verify','POST');loadUsers();toast('Updated','success');}catch(e){toast(e.message,'error');}}
async function adminToggleActive(uid){try{await api('/api/admin/users/'+uid+'/active','POST');loadUsers();toast('Updated','info');}catch(e){toast(e.message,'error');}}
async function adminGivePremium(uid){try{await api('/api/admin/users/'+uid+'/premium','POST');loadUsers();toast('Premium granted!','success');}catch(e){toast(e.message,'error');}}

async function adminSettings(){
  const el=document.getElementById('content');
  try{
    const r=await api('/api/admin/settings');
    el.innerHTML=`
      <div class="card">
        <div class="card-hd">
          <div class="card-title">Platform Settings</div>
          <button class="btn btn-sm" onclick="saveSettings()">💾 Save All</button>
        </div>
        <div class="card-body">
          <div class="settings-grid">
            <div class="form-grp">
              <label class="form-lbl">Activation Fee (KES)</label>
              <input type="number" id="s_activation_fee" value="${r.activation_fee}">
              <div class="form-hint">One-time fee to unlock earning</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Referral Bonus (KES)</label>
              <input type="number" id="s_referral_bonus" value="${r.referral_bonus}">
              <div class="form-hint">Paid when referred user activates</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Premium Monthly Fee (KES)</label>
              <input type="number" id="s_premium_fee" value="${r.premium_fee}">
              <div class="form-hint">Monthly premium subscription</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Withdrawal Fee (%)</label>
              <input type="number" id="s_withdraw_fee_pct" value="${r.withdraw_fee_pct}">
              <div class="form-hint">% deducted on withdrawal</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Free Daily Task Limit</label>
              <input type="number" id="s_free_limit" value="${r.free_limit}">
              <div class="form-hint">Max tasks/day for free users</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Premium Daily Task Limit</label>
              <input type="number" id="s_premium_limit" value="${r.premium_limit}">
              <div class="form-hint">Max tasks/day for premium users</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Share Price (KES)</label>
              <input type="number" id="s_share_price" value="${r.share_price}">
              <div class="form-hint">Current price per platform share</div>
            </div>
            <div class="form-grp">
              <label class="form-lbl">Base URL</label>
              <input type="text" id="s_base_url" value="${esc(r.base_url||'')}">
              <div class="form-hint">Used in referral links (e.g. https://dtip.co.ke)</div>
            </div>
          </div>
        </div>
      </div>`;
  }catch(e){document.getElementById('content').innerHTML=`<p style="color:var(--red);padding:20px">${e.message}</p>`;}
}

async function saveSettings(){
  const keys=['activation_fee','referral_bonus','premium_fee','withdraw_fee_pct','free_limit','premium_limit','share_price','base_url'];
  const data={};
  keys.forEach(k=>{const el=document.getElementById('s_'+k);if(el)data[k]=el.value;});
  try{await api('/api/admin/settings','POST',data);toast('Settings saved!','success');}
  catch(e){toast(e.message,'error');}
}

async function sendBroadcast(){
  try{
    await api('/api/admin/broadcast','POST',{message:document.getElementById('mBcastMsg').value});
    closeM('mBroadcast');
    document.getElementById('mBcastMsg').value='';
    toast('Broadcast sent!','success');
  }catch(e){toast(e.message,'error');}
}

/* ════════════════════════════════════
   ACTIVATION & PREMIUM
════════════════════════════════════ */
async function openActivate(){
  try{
    const s=await api('/api/settings');
    document.getElementById('mActFee').textContent='KES '+parseFloat(s.activation_fee||299).toFixed(0);
  }catch{}
  openM('mActivate');
}

async function doActivate(){
  const phone=document.getElementById('mActPhone').value;
  try{
    await api('/api/activate','POST',{phone});
    closeM('mActivate');
    S.user.is_activated=true;
    toast('🎉 Account activated! Start earning now!','success');
    go('home');
  }catch(e){toast(e.message,'error');}
}

async function loadPremFee(){
  try{
    const s=await api('/api/settings');
    document.getElementById('mPremFee').textContent='KES '+parseFloat(s.premium_fee||499).toFixed(0);
    document.getElementById('mPremLimit').textContent=s.premium_limit||10;
  }catch{}
}

async function doUpgradePremium(){
  const phone=document.getElementById('mPremPhone').value;
  try{
    const r=await api('/api/premium','POST',{phone});
    closeM('mPremium');
    S.user=r.user;
    toast('⭐ Premium activated!','success');
    go('home');
  }catch(e){toast(e.message,'error');}
}
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML)

# ──────────────────────────────────────────────
# DB INIT + SEED
# ──────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        defaults = [
            ('activation_fee', str(app.config['DEF_ACTIVATION_FEE'])),
            ('referral_bonus', str(app.config['DEF_REFERRAL_BONUS'])),
            ('premium_fee',    str(app.config['DEF_PREMIUM_FEE'])),
            ('withdraw_fee_pct', str(app.config['DEF_WITHDRAW_FEE_PCT'])),
            ('free_limit',     str(app.config['DEF_FREE_LIMIT'])),
            ('premium_limit',  str(app.config['DEF_PREMIUM_LIMIT'])),
            ('share_price',    str(app.config['DEF_SHARE_PRICE'])),
            ('base_url',       app.config['BASE_URL']),
        ]
        for k,v in defaults:
            if not Setting.query.filter_by(key=k).first():
                db.session.add(Setting(key=k,value=v))

        if not User.query.filter_by(role='admin').first():
            admin = User(email=app.config['ADMIN_EMAIL'], username='admin',
                         role='admin', is_verified=True, is_activated=True,
                         referral_code=gen_code())
            admin.set_pw(app.config['ADMIN_PASSWORD'])
            db.session.add(admin); db.session.flush()
            db.session.add(Wallet(user_id=admin.id, balance=5000.0))
            log.info(f'Admin created: {app.config["ADMIN_EMAIL"]} / {app.config["ADMIN_PASSWORD"]}')

        db.session.commit()

# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG','false').lower() == 'true'
    log.info(f'DTIP v2.1 starting on :{port}')
    log.info(f'Admin: {app.config["ADMIN_EMAIL"]} | Demo: {app.config["DEMO_MODE"]}')
    socketio.run(app, host='0.0.0.0', port=port, debug=debug)
else:
    init_db()
