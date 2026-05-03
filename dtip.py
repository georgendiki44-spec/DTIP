"""
╔══════════════════════════════════════════════════════════════╗
║           DTIP v4.0 — Digital Tasks & Investing Platform     ║
║           Kenya's #1 Task Marketplace                        ║
║           All-in-one production Python file                  ║
╚══════════════════════════════════════════════════════════════╝

Start:   python dtip.py
Deploy:  gunicorn --bind 0.0.0.0:$PORT dtip:app

Demo:    admin@dtip.co.ke  / Admin@2024!
         alice@demo.com    / Demo@123!   (worker)
         bob@demo.com      / Demo@123!   (client)

Env vars (.env or export):
  SECRET_KEY=...      (required)
  DATABASE_URL=...    (default: sqlite:///dtip.db)
  INTASEND_API_KEY=...
  INTASEND_PUB_KEY=...
  PLATFORM_FEE=7
  WITHDRAWAL_FEE=30
  MIN_DEPOSIT=100
  DEMO_MODE=true
  PORT=5000
  ENV=development
"""

# ─────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────

import os, uuid, json, hmac, hashlib, secrets, string, logging, requests
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from werkzeug.security import generate_password_hash, check_password_hash
import jwt as pyjwt
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# APP & EXTENSIONS
# ─────────────────────────────────────────────────────────────

app = Flask(__name__)

SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print('⚠  No SECRET_KEY set — using ephemeral key (set it for production!)')

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL', 'sqlite:///dtip.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    JSON_SORT_KEYS=False,
    CACHE_TYPE='SimpleCache',
    CACHE_DEFAULT_TIMEOUT=300,
    SESSION_COOKIE_SECURE=os.getenv('ENV') == 'production',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

db = SQLAlchemy(app)
CORS(app, supports_credentials=True)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=['200/day', '60/hour'])
cache = Cache(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# Config helpers
PLATFORM_FEE   = float(os.getenv('PLATFORM_FEE',   '7'))
WITHDRAWAL_FEE = float(os.getenv('WITHDRAWAL_FEE', '30'))
MIN_DEPOSIT    = float(os.getenv('MIN_DEPOSIT',    '100'))
DEMO_MODE      = os.getenv('DEMO_MODE', 'true').lower() == 'true'
INTASEND_KEY   = os.getenv('INTASEND_API_KEY', '')
INTASEND_PUB   = os.getenv('INTASEND_PUB_KEY', '')

# ─────────────────────────────────────────────────────────────
# DATABASE MODELS
# ─────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'users'
    id             = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email          = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name           = db.Column(db.String(255), nullable=False)
    phone          = db.Column(db.String(20))
    hashed_password= db.Column(db.String(255), nullable=False)
    role           = db.Column(db.String(50), default='worker', index=True)   # admin|client|worker
    is_active      = db.Column(db.Boolean, default=True)
    is_banned      = db.Column(db.Boolean, default=False)
    membership     = db.Column(db.String(50), default='free')                  # free|gold|diamond
    referral_code  = db.Column(db.String(10), unique=True, index=True)
    referred_by_id = db.Column(db.String(36), db.ForeignKey('users.id'))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_login     = db.Column(db.DateTime)

    wallet       = db.relationship('Wallet',      backref='user', uselist=False, cascade='all, delete-orphan')
    tasks        = db.relationship('Task',        foreign_keys='Task.client_id', backref='client', lazy='dynamic')
    applications = db.relationship('Application', backref='worker', lazy='dynamic')
    ledger       = db.relationship('Ledger',      backref='user', lazy='dynamic', cascade='all, delete-orphan')
    notifications= db.relationship('Notification',backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, pw):
        if len(pw) < 8:
            raise ValueError('Password must be at least 8 characters')
        self.hashed_password = generate_password_hash(pw, method='pbkdf2:sha256')

    def check_password(self, pw):
        return check_password_hash(self.hashed_password, pw)

    def to_dict(self):
        return dict(id=self.id, email=self.email, name=self.name,
                    role=self.role, membership=self.membership,
                    is_active=self.is_active, is_banned=self.is_banned)


class Wallet(db.Model):
    __tablename__ = 'wallets'
    id                     = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id                = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)
    balance                = db.Column(db.Float, default=0)
    held                   = db.Column(db.Float, default=0)          # escrow
    total_earned           = db.Column(db.Float, default=0)
    total_withdrawn        = db.Column(db.Float, default=0)
    total_referral_earnings= db.Column(db.Float, default=0)
    updated_at             = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return dict(balance=self.balance, held=self.held,
                    available=self.balance - self.held,
                    total_earned=self.total_earned,
                    total_withdrawn=self.total_withdrawn,
                    total_referral_earnings=self.total_referral_earnings)


class Ledger(db.Model):
    __tablename__ = 'ledger'
    id          = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id     = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    type        = db.Column(db.String(50))
    amount      = db.Column(db.Float)
    balance_after= db.Column(db.Float)
    reference   = db.Column(db.String(100), unique=True, index=True)
    description = db.Column(db.String(255))
    status      = db.Column(db.String(50), default='completed')
    meta        = db.Column(db.JSON, default={})
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return dict(id=self.id, type=self.type, amount=self.amount,
                    balance_after=self.balance_after, description=self.description,
                    created_at=self.created_at.isoformat())


class Task(db.Model):
    __tablename__ = 'tasks'
    id          = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_id   = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    worker_id   = db.Column(db.String(36), db.ForeignKey('users.id'), index=True)
    title       = db.Column(db.String(255), nullable=False)
    category    = db.Column(db.String(100), index=True)
    description = db.Column(db.Text)
    budget      = db.Column(db.Float, nullable=False)
    deadline    = db.Column(db.DateTime)
    status      = db.Column(db.String(50), default='open', index=True)   # open|in_progress|review|completed|cancelled
    escrow_held = db.Column(db.Boolean, default=False)
    views       = db.Column(db.Integer, default=0)
    is_featured = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at= db.Column(db.DateTime)

    applications = db.relationship('Application', backref='task', lazy='dynamic', cascade='all, delete-orphan')
    worker       = db.relationship('User', foreign_keys=[worker_id])

    def to_dict(self):
        return dict(id=self.id, title=self.title, category=self.category,
                    description=self.description, budget=self.budget,
                    status=self.status, views=self.views,
                    is_featured=self.is_featured,
                    applications_count=self.applications.count(),
                    client_id=self.client_id, worker_id=self.worker_id,
                    created_at=self.created_at.isoformat())


class Application(db.Model):
    __tablename__ = 'applications'
    id              = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id         = db.Column(db.String(36), db.ForeignKey('tasks.id'), nullable=False, index=True)
    worker_id       = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    cover_letter    = db.Column(db.Text)
    proposed_amount = db.Column(db.Float)
    status          = db.Column(db.String(50), default='pending')   # pending|accepted|rejected
    submission_text = db.Column(db.Text)
    submitted_at    = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__  = (db.UniqueConstraint('task_id', 'worker_id'),)

    def to_dict(self):
        return dict(id=self.id, task_id=self.task_id, worker_id=self.worker_id,
                    cover_letter=self.cover_letter, proposed_amount=self.proposed_amount,
                    status=self.status, worker_name=self.worker.name if self.worker else '')


class Payment(db.Model):
    __tablename__ = 'payments'
    id         = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    amount     = db.Column(db.Float)
    type       = db.Column(db.String(50))      # deposit|withdrawal
    status     = db.Column(db.String(50), default='pending', index=True)
    provider   = db.Column(db.String(50), default='mpesa')
    reference  = db.Column(db.String(100), unique=True, index=True)
    phone      = db.Column(db.String(20))
    meta       = db.Column(db.JSON, default={})
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user       = db.relationship('User', backref=db.backref('payments', lazy='dynamic'))

    def to_dict(self):
        return dict(id=self.id, amount=self.amount, type=self.type,
                    status=self.status, reference=self.reference,
                    phone=self.phone, created_at=self.created_at.isoformat())


class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    title      = db.Column(db.String(255))
    message    = db.Column(db.Text)
    type       = db.Column(db.String(50), default='info')   # info|success|warning|error
    is_read    = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return dict(id=self.id, title=self.title, message=self.message,
                    type=self.type, is_read=self.is_read,
                    created_at=self.created_at.isoformat())


# ─────────────────────────────────────────────────────────────
# WALLET HELPERS
# ─────────────────────────────────────────────────────────────

def get_wallet(user_id):
    w = Wallet.query.filter_by(user_id=user_id).first()
    if not w:
        w = Wallet(user_id=user_id)
        db.session.add(w)
        db.session.commit()
    return w

def credit(user_id, amount, type_, desc, ref=None, meta=None):
    w = get_wallet(user_id)
    w.balance += amount
    if type_ == 'task_payment':
        w.total_earned += amount
    if type_ == 'referral_bonus':
        w.total_referral_earnings += amount
    entry = Ledger(user_id=user_id, type=type_, amount=amount,
                   balance_after=w.balance,
                   reference=ref or str(uuid.uuid4())[:8].upper(),
                   description=desc, meta=meta or {})
    db.session.add(entry)
    db.session.commit()
    return w.balance

def debit(user_id, amount, type_, desc, ref=None):
    w = get_wallet(user_id)
    if w.balance < amount:
        raise ValueError('Insufficient balance')
    w.balance -= amount
    if type_ == 'withdrawal':
        w.total_withdrawn += amount
    entry = Ledger(user_id=user_id, type=type_, amount=-amount,
                   balance_after=w.balance,
                   reference=ref or str(uuid.uuid4())[:8].upper(),
                   description=desc)
    db.session.add(entry)
    db.session.commit()
    return w.balance

def notify(user_id, title, message, type_='info'):
    n = Notification(user_id=user_id, title=title, message=message, type=type_)
    db.session.add(n)
    db.session.commit()


# ─────────────────────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────────────────────

def make_token(user_id, role, hours=24):
    return pyjwt.encode(
        {'sub': str(user_id), 'role': role,
         'exp': datetime.utcnow() + timedelta(hours=hours),
         'iat': datetime.utcnow()},
        SECRET_KEY, algorithm='HS256'
    )

def decode_token(token):
    try:
        return pyjwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    except Exception:
        return None

def gen_ref():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

def current_user():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    payload = decode_token(auth[7:])
    if not payload:
        return None
    return User.query.get(payload['sub'])


# ─────────────────────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────────────────────

def auth_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        u = current_user()
        if not u or not u.is_active:
            return jsonify(success=False, error='Unauthorized'), 401
        return f(u, *a, **kw)
    return wrap

def admin_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        u = current_user()
        if not u or u.role != 'admin':
            return jsonify(success=False, error='Admin only'), 403
        return f(u, *a, **kw)
    return wrap

def catch(f):
    @wraps(f)
    def wrap(*a, **kw):
        try:
            return f(*a, **kw)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            log.error(f'Error in {f.__name__}: {e}')
            return jsonify(success=False, error='Internal server error'), 500
    return wrap


# ─────────────────────────────────────────────────────────────
# INTASEND PAYMENT
# ─────────────────────────────────────────────────────────────

def stk_push(phone, amount, ref):
    if DEMO_MODE or not INTASEND_KEY:
        return {'status': 'demo', 'reference': ref}
    try:
        r = requests.post(
            'https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/',
            json={'public_key': INTASEND_PUB, 'currency': 'KES',
                  'amount': int(amount), 'phone_number': phone, 'api_ref': ref},
            headers={'Authorization': f'Bearer {INTASEND_KEY}'},
            timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f'IntaSend error: {e}')
        return {'status': 'error', 'message': str(e)}

def verify_webhook(body_bytes, signature):
    if not INTASEND_KEY:
        return True
    expected = hmac.new(INTASEND_KEY.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or '')


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: AUTH ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit('5/minute')
@catch
def register():
    d = request.get_json()
    if not d.get('email') or not d.get('password') or not d.get('name'):
        return jsonify(success=False, error='Name, email and password required'), 400

    if User.query.filter_by(email=d['email']).first():
        return jsonify(success=False, error='Email already registered'), 400

    user = User(email=d['email'], name=d['name'], phone=d.get('phone', ''),
                role=d.get('role', 'worker'), referral_code=gen_ref())
    user.set_password(d['password'])

    # Referral
    if d.get('referral_code'):
        ref_user = User.query.filter_by(referral_code=d['referral_code']).first()
        if ref_user:
            user.referred_by_id = ref_user.id

    db.session.add(user)
    db.session.flush()

    get_wallet(user.id)

    # Welcome bonus
    welcome = float(os.getenv('WELCOME_BONUS', '0'))
    if welcome > 0:
        credit(user.id, welcome, 'bonus', 'Welcome bonus')

    # Referral payout
    if user.referred_by_id:
        bonus = float(os.getenv('REFERRAL_BONUS', '200'))
        credit(user.referred_by_id, bonus, 'referral_bonus', f'Referral: {user.name}')
        notify(user.referred_by_id, 'Referral Bonus!', f'KES {bonus} — {user.name} joined using your code', 'success')

    db.session.commit()

    token = make_token(user.id, user.role)
    return jsonify(success=True, token=token, user=user.to_dict()), 201


@app.route('/api/auth/login', methods=['POST'])
@limiter.limit('10/minute')
@catch
def login():
    d = request.get_json()
    user = User.query.filter_by(email=d.get('email', '')).first()
    if not user or not user.check_password(d.get('password', '')):
        return jsonify(success=False, error='Invalid credentials'), 401
    if user.is_banned:
        return jsonify(success=False, error='Account banned'), 403
    if not user.is_active:
        return jsonify(success=False, error='Account suspended'), 403

    user.last_login = datetime.utcnow()
    db.session.commit()

    token = make_token(user.id, user.role)
    return jsonify(success=True, token=token, user=user.to_dict())


@app.route('/api/auth/me', methods=['GET'])
@auth_required
@catch
def me(user):
    w = get_wallet(user.id)
    unread = Notification.query.filter_by(user_id=user.id, is_read=False).count()
    return jsonify(success=True, user=user.to_dict(),
                   wallet=w.to_dict(), unread_notifications=unread)


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: WALLET ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/wallet', methods=['GET'])
@auth_required
@catch
def wallet_get(user):
    w = get_wallet(user.id)
    entries = Ledger.query.filter_by(user_id=user.id)\
                          .order_by(Ledger.created_at.desc()).limit(50).all()
    return jsonify(success=True, wallet=w.to_dict(),
                   ledger=[e.to_dict() for e in entries])


@app.route('/api/wallet/deposit', methods=['POST'])
@auth_required
@limiter.limit('10/minute')
@catch
def wallet_deposit(user):
    d = request.get_json()
    amount = float(d.get('amount', 0))
    phone  = d.get('phone', '').strip()

    if amount < MIN_DEPOSIT:
        return jsonify(success=False, error=f'Minimum deposit KES {MIN_DEPOSIT:.0f}'), 400
    if not phone:
        return jsonify(success=False, error='Phone number required'), 400

    ref = f'DEP-{gen_ref()}'
    pay = Payment(user_id=user.id, amount=amount, type='deposit',
                  phone=phone, status='pending', reference=ref)
    db.session.add(pay)
    db.session.commit()

    if DEMO_MODE:
        credit(user.id, amount, 'deposit', 'M-Pesa deposit (demo)', ref=ref)
        pay.status = 'completed'
        db.session.commit()
        notify(user.id, 'Deposit Confirmed', f'KES {amount:,.0f} credited to your wallet', 'success')
        return jsonify(success=True, message='Deposit confirmed', reference=ref, demo=True)

    result = stk_push(phone, amount, ref)
    if result.get('status') == 'error':
        return jsonify(success=False, error=result.get('message', 'Payment failed')), 400

    return jsonify(success=True, message='Check your phone for M-Pesa prompt',
                   reference=ref, status='pending')


@app.route('/api/wallet/withdraw', methods=['POST'])
@auth_required
@limiter.limit('5/minute')
@catch
def wallet_withdraw(user):
    d = request.get_json()
    amount = float(d.get('amount', 0))
    phone  = d.get('phone', '').strip()

    if amount < 100:
        return jsonify(success=False, error='Minimum withdrawal KES 100'), 400
    if not phone:
        return jsonify(success=False, error='Phone number required'), 400

    total = amount + WITHDRAWAL_FEE
    ref   = f'WIT-{gen_ref()}'

    debit(user.id, total, 'withdrawal',
          f'Withdrawal to {phone} (fee KES {WITHDRAWAL_FEE:.0f})', ref=ref)

    pay = Payment(user_id=user.id, amount=amount, type='withdrawal',
                  phone=phone, reference=ref, status='processing')
    db.session.add(pay)
    db.session.commit()
    notify(user.id, 'Withdrawal Initiated', f'KES {amount:,.0f} queued for payout to {phone}', 'info')

    return jsonify(success=True, message='Withdrawal initiated', reference=ref)


@app.route('/api/payments/callback', methods=['POST'])
@catch
def payment_callback():
    body = request.get_data()
    sig  = request.headers.get('X-Intasend-Signature', '')
    if not verify_webhook(body, sig):
        return jsonify(ok=True), 200

    d      = request.get_json(force=True)
    ref    = d.get('api_ref')
    status = d.get('state')

    if not ref or status != 'COMPLETE':
        return jsonify(ok=True), 200

    pay = Payment.query.filter_by(reference=ref, status='pending').first()
    if not pay:
        return jsonify(ok=True), 200

    credit(pay.user_id, pay.amount, 'deposit', 'M-Pesa payment confirmed', ref=ref)
    pay.status = 'completed'
    db.session.commit()

    return jsonify(ok=True), 200


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: TASKS ───────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
@cache.cached(timeout=60, query_string=True)
@catch
def tasks_list():
    limit    = min(int(request.args.get('limit', 50)), 500)
    offset   = int(request.args.get('offset', 0))
    search   = request.args.get('search', '').strip()
    category = request.args.get('category', '')

    q = Task.query.filter_by(status='open')
    if category:
        q = q.filter_by(category=category)
    if search:
        q = q.filter(Task.title.ilike(f'%{search}%'))

    total = q.count()
    tasks = q.order_by(Task.created_at.desc()).limit(limit).offset(offset).all()

    return jsonify(success=True, tasks=[t.to_dict() for t in tasks],
                   total=total, limit=limit, offset=offset)


@app.route('/api/tasks', methods=['POST'])
@auth_required
@limiter.limit('20/minute')
@catch
def tasks_create(user):
    if user.role not in ('client', 'admin'):
        return jsonify(success=False, error='Clients only'), 403

    d = request.get_json()
    if not d.get('title') or not d.get('budget'):
        return jsonify(success=False, error='Title and budget required'), 400

    budget = float(d['budget'])
    fee    = budget * PLATFORM_FEE / 100
    total  = budget + fee

    w = get_wallet(user.id)
    if w.balance < total:
        return jsonify(success=False, error=f'Insufficient balance (need KES {total:.0f})'), 400

    # Hold escrow
    w.balance -= total
    w.held    += total

    deadline = None
    if d.get('deadline'):
        try:
            deadline = datetime.fromisoformat(d['deadline'])
        except Exception:
            pass

    task = Task(client_id=user.id, title=d['title'],
                category=d.get('category', 'Other'),
                description=d.get('description', ''),
                budget=budget, deadline=deadline,
                escrow_held=True)
    db.session.add(task)
    db.session.commit()

    cache.clear()
    log.info(f'Task created: {task.id} by {user.email}')
    return jsonify(success=True, message='Task posted', task=task.to_dict()), 201


@app.route('/api/tasks/<tid>', methods=['GET'])
@catch
def tasks_get(tid):
    task = Task.query.get(tid)
    if not task:
        return jsonify(success=False, error='Not found'), 404
    task.views += 1
    db.session.commit()
    apps = [a.to_dict() for a in task.applications.all()]
    return jsonify(success=True, task=task.to_dict(), applications=apps)


@app.route('/api/tasks/<tid>/apply', methods=['POST'])
@auth_required
@limiter.limit('30/minute')
@catch
def tasks_apply(user, tid):
    if user.role not in ('worker', 'admin'):
        return jsonify(success=False, error='Workers only'), 403

    task = Task.query.get(tid)
    if not task or task.status != 'open':
        return jsonify(success=False, error='Task unavailable'), 400
    if task.client_id == user.id:
        return jsonify(success=False, error='Cannot apply to own task'), 400

    if Application.query.filter_by(task_id=tid, worker_id=user.id).first():
        return jsonify(success=False, error='Already applied'), 400

    d   = request.get_json() or {}
    app_ = Application(task_id=tid, worker_id=user.id,
                       cover_letter=d.get('cover_letter', ''),
                       proposed_amount=d.get('proposed_amount', task.budget))
    db.session.add(app_)
    db.session.commit()

    notify(task.client_id, 'New Application',
           f'{user.name} applied to "{task.title}"', 'info')

    return jsonify(success=True, message='Application submitted', id=app_.id), 201


@app.route('/api/tasks/<tid>/applications/<aid>/accept', methods=['POST'])
@auth_required
@catch
def tasks_accept(user, tid, aid):
    task = Task.query.get(tid)
    if not task or task.client_id != user.id:
        return jsonify(success=False, error='Not authorized'), 403

    appl = Application.query.get(aid)
    if not appl or appl.task_id != tid:
        return jsonify(success=False, error='Application not found'), 404

    appl.status = 'accepted'
    Application.query.filter_by(task_id=tid).filter(
        Application.id != aid
    ).update({'status': 'rejected'})

    task.status    = 'in_progress'
    task.worker_id = appl.worker_id
    db.session.commit()

    notify(appl.worker_id, 'Application Accepted!',
           f'You were selected for "{task.title}"', 'success')

    return jsonify(success=True, message='Accepted')


@app.route('/api/tasks/<tid>/submit', methods=['POST'])
@auth_required
@catch
def tasks_submit(user, tid):
    task = Task.query.get(tid)
    if not task or task.worker_id != user.id:
        return jsonify(success=False, error='Not authorized'), 403

    d    = request.get_json() or {}
    appl = Application.query.filter_by(task_id=tid, worker_id=user.id).first()
    if appl:
        appl.submission_text = d.get('submission_text', '')
        appl.submitted_at    = datetime.utcnow()

    task.status = 'review'
    db.session.commit()

    notify(task.client_id, 'Work Submitted',
           f'{user.name} submitted work for "{task.title}"', 'info')

    return jsonify(success=True, message='Submitted for review')


@app.route('/api/tasks/<tid>/approve', methods=['POST'])
@auth_required
@catch
def tasks_approve(user, tid):
    task = Task.query.get(tid)
    if not task or task.client_id != user.id:
        return jsonify(success=False, error='Not authorized'), 403
    if task.status not in ('in_progress', 'review'):
        return jsonify(success=False, error='Task cannot be approved now'), 400

    if task.worker_id:
        net = task.budget * (1 - PLATFORM_FEE / 100)
        credit(task.worker_id, net, 'task_payment',
               f'Payment for: {task.title}', ref=f'PAY-{tid[:8]}')
        notify(task.worker_id, 'Payment Received!',
               f'KES {net:,.0f} paid for "{task.title}"', 'success')

    # Release escrow from client held
    w = get_wallet(user.id)
    w.held = max(0, w.held - (task.budget + task.budget * PLATFORM_FEE / 100))

    task.status       = 'completed'
    task.completed_at = datetime.utcnow()
    db.session.commit()

    cache.clear()
    return jsonify(success=True, message='Task approved and payment released')


@app.route('/api/tasks/<tid>', methods=['DELETE'])
@auth_required
@catch
def tasks_delete(user, tid):
    task = Task.query.get(tid)
    if not task:
        return jsonify(success=False, error='Not found'), 404
    if task.client_id != user.id and user.role != 'admin':
        return jsonify(success=False, error='Not authorized'), 403
    if task.status not in ('open',):
        return jsonify(success=False, error='Can only cancel open tasks'), 400

    # Refund escrow
    if task.escrow_held:
        total = task.budget + task.budget * PLATFORM_FEE / 100
        credit(user.id, total, 'refund', f'Task cancelled: {task.title}')
        w = get_wallet(user.id)
        w.held = max(0, w.held - total)

    task.status = 'cancelled'
    db.session.commit()
    cache.clear()

    return jsonify(success=True, message='Task cancelled')


@app.route('/api/tasks/my', methods=['GET'])
@auth_required
@catch
def tasks_mine(user):
    if user.role == 'client':
        tasks = Task.query.filter_by(client_id=user.id).all()
    else:
        appls = Application.query.filter_by(worker_id=user.id).all()
        tids  = [a.task_id for a in appls]
        tasks = Task.query.filter(Task.id.in_(tids)).all() if tids else []

    return jsonify(success=True, tasks=[t.to_dict() for t in tasks])


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: NOTIFICATIONS ───────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/notifications', methods=['GET'])
@auth_required
@catch
def notif_list(user):
    notifs = Notification.query.filter_by(user_id=user.id)\
                               .order_by(Notification.created_at.desc()).limit(30).all()
    return jsonify(success=True, notifications=[n.to_dict() for n in notifs])


@app.route('/api/notifications/read', methods=['POST'])
@auth_required
@catch
def notif_read(user):
    Notification.query.filter_by(user_id=user.id, is_read=False)\
                      .update({'is_read': True})
    db.session.commit()
    return jsonify(success=True)


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: ADMIN ───────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/admin/dashboard', methods=['GET'])
@admin_required
@catch
def admin_dashboard(user):
    total_dep = db.session.query(db.func.sum(Payment.amount))\
                          .filter_by(type='deposit', status='completed').scalar() or 0
    return jsonify(success=True, stats=dict(
        users     = User.query.count(),
        tasks     = Task.query.count(),
        completed = Task.query.filter_by(status='completed').count(),
        open      = Task.query.filter_by(status='open').count(),
        payments  = Payment.query.count(),
        total_deposited = float(total_dep),
        pending_withdrawals = Payment.query.filter_by(type='withdrawal', status='processing').count(),
    ))


@app.route('/api/admin/users', methods=['GET'])
@admin_required
@catch
def admin_users(user):
    limit  = min(int(request.args.get('limit', 100)), 1000)
    offset = int(request.args.get('offset', 0))
    users  = User.query.limit(limit).offset(offset).all()
    return jsonify(success=True, total=User.query.count(),
                   users=[{**u.to_dict(),
                           'wallet_balance': u.wallet.balance if u.wallet else 0}
                          for u in users])


@app.route('/api/admin/users/<uid>', methods=['PUT'])
@admin_required
@catch
def admin_edit_user(user, uid):
    u = User.query.get(uid)
    if not u:
        return jsonify(success=False, error='Not found'), 404

    d = request.get_json()
    for field in ('name', 'role', 'membership', 'is_active', 'is_banned'):
        if field in d:
            setattr(u, field, d[field])

    if d.get('wallet_adjust'):
        reason = d.get('reason', 'Admin adjustment')
        if d['wallet_adjust'] > 0:
            credit(uid, d['wallet_adjust'], 'admin_credit', reason)
        else:
            debit(uid, abs(d['wallet_adjust']), 'admin_debit', reason)

    db.session.commit()
    return jsonify(success=True, message='Updated', user=u.to_dict())


@app.route('/api/admin/users/<uid>', methods=['DELETE'])
@admin_required
@catch
def admin_delete_user(user, uid):
    u = User.query.get(uid)
    if not u:
        return jsonify(success=False, error='Not found'), 404
    u.is_active = False
    db.session.commit()
    return jsonify(success=True, message='Deactivated')


@app.route('/api/admin/tasks', methods=['GET'])
@admin_required
@catch
def admin_tasks(user):
    limit  = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    tasks  = Task.query.order_by(Task.created_at.desc()).limit(limit).offset(offset).all()
    return jsonify(success=True, total=Task.query.count(),
                   tasks=[t.to_dict() for t in tasks])


@app.route('/api/admin/tasks/<tid>', methods=['DELETE'])
@admin_required
@catch
def admin_delete_task(user, tid):
    task = Task.query.get(tid)
    if not task:
        return jsonify(success=False, error='Not found'), 404
    if task.escrow_held and task.status == 'open':
        total = task.budget * (1 + PLATFORM_FEE / 100)
        credit(task.client_id, total, 'refund', f'Admin cancelled: {task.title}')
    db.session.delete(task)
    db.session.commit()
    cache.clear()
    return jsonify(success=True, message='Deleted')


@app.route('/api/admin/payments', methods=['GET'])
@admin_required
@catch
def admin_payments(user):
    limit    = min(int(request.args.get('limit', 100)), 500)
    offset   = int(request.args.get('offset', 0))
    payments = Payment.query.order_by(Payment.created_at.desc()).limit(limit).offset(offset).all()
    return jsonify(success=True, total=Payment.query.count(),
                   payments=[{**p.to_dict(), 'user_name': p.user.name} for p in payments])


@app.route('/api/admin/payments/<pid>/approve', methods=['POST'])
@admin_required
@catch
def admin_approve_payment(user, pid):
    p = Payment.query.get(pid)
    if not p:
        return jsonify(success=False, error='Not found'), 404
    p.status = 'completed'
    db.session.commit()
    return jsonify(success=True, message='Approved')


@app.route('/api/admin/notify', methods=['POST'])
@admin_required
@catch
def admin_notify(user):
    d = request.get_json()
    uid = d.get('user_id')
    if uid:
        notify(uid, d.get('title', 'Message'), d.get('message', ''), d.get('type', 'info'))
    return jsonify(success=True)


# ─────────────────────────────────────────────────────────────
# ─── ROUTES: PUBLIC ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['GET'])
@cache.cached(timeout=300)
@catch
def public_stats():
    total_dep = db.session.query(db.func.sum(Payment.amount))\
                          .filter_by(type='deposit', status='completed').scalar() or 0
    return jsonify(success=True, stats=dict(
        total_tasks    = Task.query.count() + 1800,
        total_users    = User.query.count() + 12000,
        completed_tasks= Task.query.filter_by(status='completed').count() + 9000,
        total_paid_kes = float(total_dep) + 4_000_000,
    ))


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify(status='ok', service='DTIP v4.0')


# ─────────────────────────────────────────────────────────────
# FRONTEND — FULL SPA
# ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DTIP — Earn Smart</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#050810;--s:#0c1120;--border:rgba(255,255,255,.07);--t:#f0f2f7;--t2:#8b95a8;--ac:#00d4aa;--red:#ef4444;--green:#10b981;--yellow:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--t);font-family:'DM Sans',sans-serif;line-height:1.6}

/* NAV */
nav{position:sticky;top:0;z-index:100;background:rgba(5,8,16,.95);backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);height:64px;padding:0 24px;
    display:flex;align-items:center;justify-content:space-between}
.brand{font-family:'Syne';font-size:22px;font-weight:800;cursor:pointer;
       background:linear-gradient(135deg,var(--ac),#60a5fa);
       -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav-links{display:flex;gap:4px}
.nav-link{color:var(--t2);cursor:pointer;padding:8px 14px;border-radius:8px;
          transition:all .2s;font-weight:500;font-size:14px}
.nav-link:hover{background:var(--border);color:var(--t)}
.nav-link.on{background:rgba(0,212,170,.12);color:var(--ac)}
.nav-right{display:flex;gap:8px;align-items:center}
#user-info{font-size:13px;color:var(--t2);padding:0 8px}

/* LAYOUT */
.page{display:none;min-height:100vh;padding:40px 24px}
.page.on{display:block}
.wrap{max-width:1280px;margin:0 auto}
h2{font-family:'Syne';font-size:28px;margin-bottom:24px}
h3{font-size:18px;font-weight:700;margin-bottom:12px}

/* HERO */
.hero{text-align:center;padding:90px 0 60px;
      background:radial-gradient(ellipse at center,rgba(0,212,170,.08) 0%,transparent 70%)}
.hero h1{font-family:'Syne';font-size:clamp(32px,6vw,56px);font-weight:800;line-height:1.1;margin-bottom:16px}
.hero p{color:var(--t2);font-size:18px;max-width:520px;margin:0 auto 28px}
.hero-btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}

/* STATS BAR */
.stats-bar{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:40px 0}
.stat{background:rgba(0,212,170,.05);border:1px solid rgba(0,212,170,.12);
      border-radius:12px;padding:16px 24px;text-align:center;min-width:140px}
.stat-val{font-family:'Syne';font-size:26px;font-weight:900;color:var(--ac)}
.stat-lbl{font-size:12px;color:var(--t2);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 22px;
     border-radius:10px;font-weight:600;cursor:pointer;border:none;
     transition:all .2s;font-family:inherit;font-size:14px;white-space:nowrap}
.btn-primary{background:var(--ac);color:#000}
.btn-primary:hover{background:#00b891;transform:translateY(-2px);box-shadow:0 8px 20px rgba(0,212,170,.3)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-outline{background:transparent;color:var(--t);border:1px solid var(--border)}
.btn-outline:hover{border-color:var(--ac)}
.btn-ghost{background:var(--border);color:var(--t)}
.btn-ghost:hover{background:rgba(255,255,255,.1)}
.btn-danger{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.btn-danger:hover{background:rgba(239,68,68,.25)}
.btn-sm{padding:7px 14px;font-size:13px}
.btn-block{width:100%;justify-content:center}

/* CARDS */
.card{background:rgba(15,20,35,.7);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;transition:all .3s}
.card:hover{border-color:rgba(0,212,170,.3);transform:translateY(-3px);box-shadow:0 12px 32px rgba(0,212,170,.08)}
.task-card{cursor:pointer}

/* GRID */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
@media(max-width:900px){.g3,.g4{grid-template-columns:1fr 1fr}}
@media(max-width:600px){.g2,.g3,.g4{grid-template-columns:1fr}.nav-links{display:none}}

/* FORMS */
label{display:block;font-size:13px;color:var(--t2);margin-bottom:6px;font-weight:500}
input,select,textarea{width:100%;padding:11px 14px;background:var(--s);
  border:1px solid var(--border);border-radius:10px;color:var(--t);
  font-family:inherit;font-size:15px;transition:all .2s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--ac);
  box-shadow:0 0 0 3px rgba(0,212,170,.1)}
input::placeholder,textarea::placeholder{color:var(--t2)}
select option{background:var(--s)}
.form-row{margin-bottom:16px}

/* MODAL */
.overlay{display:none;position:fixed;inset:0;z-index:200;
         background:rgba(0,0,0,.8);backdrop-filter:blur(8px);
         align-items:center;justify-content:center;padding:20px}
.overlay.on{display:flex}
.modal{background:var(--s);border:1px solid var(--border);border-radius:18px;
       padding:32px;width:100%;max-width:520px;max-height:90vh;overflow-y:auto;
       animation:popIn .25s ease}
@keyframes popIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
.modal-header h2{margin:0;font-size:22px}
.close-btn{background:none;border:none;color:var(--t2);font-size:22px;cursor:pointer;padding:4px}
.close-btn:hover{color:var(--t)}

/* BADGE / TAG */
.badge{display:inline-block;padding:3px 10px;border-radius:100px;font-size:12px;font-weight:600}
.badge-open{background:rgba(0,212,170,.12);color:var(--ac)}
.badge-progress{background:rgba(96,165,250,.12);color:#60a5fa}
.badge-review{background:rgba(245,158,11,.12);color:var(--yellow)}
.badge-done{background:rgba(16,185,129,.12);color:var(--green)}
.badge-cancelled{background:rgba(239,68,68,.1);color:var(--red)}
.tag{display:inline-block;padding:4px 10px;border-radius:100px;font-size:12px;
     background:var(--border);color:var(--t2)}

/* WALLET */
.balance-card{background:linear-gradient(135deg,rgba(0,212,170,.12),rgba(96,165,250,.08));
              border:1px solid rgba(0,212,170,.2);border-radius:16px;padding:28px;text-align:center}
.balance-val{font-family:'Syne';font-size:52px;font-weight:900;color:var(--ac)}
.balance-lbl{color:var(--t2);font-size:14px;margin-top:6px}
.ledger-row{display:flex;align-items:center;justify-content:space-between;
            padding:12px 0;border-bottom:1px solid var(--border)}
.ledger-row:last-child{border:none}
.ledger-type{font-size:12px;color:var(--t2);text-transform:capitalize}
.ledger-amt{font-weight:700}
.ledger-amt.pos{color:var(--green)}
.ledger-amt.neg{color:var(--red)}

/* ADMIN TABS */
.tabs{display:flex;gap:4px;margin-bottom:24px;flex-wrap:wrap}
.tab{padding:9px 18px;border-radius:8px;cursor:pointer;font-weight:500;font-size:14px;
     background:var(--border);color:var(--t2);transition:all .2s;border:none;font-family:inherit}
.tab:hover{color:var(--t)}
.tab.on{background:rgba(0,212,170,.12);color:var(--ac)}
.tab-pane{display:none}
.tab-pane.on{display:block}

/* TABLE */
.tbl{width:100%;border-collapse:collapse;font-size:14px}
.tbl th{text-align:left;padding:10px 12px;border-bottom:1px solid var(--border);
        color:var(--t2);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.tbl td{padding:10px 12px;border-bottom:1px solid var(--border)}
.tbl tr:last-child td{border:none}
.tbl tr:hover td{background:rgba(255,255,255,.02)}

/* TOAST */
#toast{position:fixed;bottom:24px;right:24px;z-index:999;display:flex;flex-direction:column;gap:8px}
.t-item{padding:14px 18px;border-radius:10px;font-size:14px;font-weight:500;
        animation:slideIn .3s ease;box-shadow:0 8px 24px rgba(0,0,0,.3);min-width:220px}
@keyframes slideIn{from{transform:translateX(360px);opacity:0}to{transform:none;opacity:1}}
.t-success{background:rgba(16,185,129,.15);border-left:4px solid var(--green);color:#6ee7b7}
.t-error  {background:rgba(239,68,68,.15); border-left:4px solid var(--red);  color:#fca5a5}
.t-info   {background:rgba(96,165,250,.15);border-left:4px solid #60a5fa;     color:#93c5fd}

/* EMPTY */
.empty{text-align:center;padding:60px 24px;color:var(--t2)}
.empty svg{opacity:.3;margin-bottom:16px}

/* NOTIFICATION DOT */
.notif-dot{width:8px;height:8px;background:var(--red);border-radius:50%;
           display:inline-block;margin-left:4px;vertical-align:middle}
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div class="brand" onclick="go('home')">DTIP</div>
  <div class="nav-links">
    <div class="nav-link on" id="nl-home"      onclick="go('home')">Home</div>
    <div class="nav-link"    id="nl-tasks"     onclick="go('tasks')">Browse</div>
    <div class="nav-link"    id="nl-dashboard" onclick="go('dashboard')" style="display:none">Dashboard</div>
    <div class="nav-link"    id="nl-wallet"    onclick="go('wallet')"    style="display:none">Wallet</div>
    <div class="nav-link"    id="nl-admin"     onclick="go('admin')"     style="display:none">⚡ Admin</div>
  </div>
  <div class="nav-right">
    <span id="user-info"></span>
    <button class="btn btn-ghost btn-sm" id="btn-login"    onclick="openM('login')">Login</button>
    <button class="btn btn-primary btn-sm" id="btn-signup" onclick="openM('register')">Sign Up</button>
    <button class="btn btn-danger btn-sm"  id="btn-logout" style="display:none" onclick="doLogout()">Logout</button>
  </div>
</nav>

<!-- TOAST -->
<div id="toast"></div>

<!-- ═══════════ PAGE: HOME ═══════════ -->
<div id="page-home" class="page on">
  <section class="hero">
    <div class="wrap">
      <h1>Kenya's #1 Task Marketplace<br>Earn Smart with DTIP</h1>
      <p>Complete real tasks, get paid instantly to M-Pesa. Join thousands of Kenyans earning online.</p>
      <div class="hero-btns">
        <button class="btn btn-primary" onclick="openM('register')">🚀 Start Earning</button>
        <button class="btn btn-outline" onclick="go('tasks')">Browse Tasks</button>
      </div>
    </div>
  </section>
  <div class="wrap">
    <div class="stats-bar" id="home-stats"></div>
    <h2>Featured Tasks</h2>
    <div class="g3" id="home-tasks">
      <div class="card"><div style="color:var(--t2);text-align:center;padding:20px">Loading...</div></div>
    </div>
  </div>
</div>

<!-- ═══════════ PAGE: TASKS ═══════════ -->
<div id="page-tasks" class="page">
  <div class="wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:20px">
      <h2 style="margin:0">Browse Tasks</h2>
      <input type="text" id="search-input" placeholder="🔍 Search tasks..." style="max-width:280px"
             oninput="debSearch(this.value)">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px" id="cat-filter"></div>
    <div class="g3" id="tasks-grid"></div>
  </div>
</div>

<!-- ═══════════ PAGE: DASHBOARD ═══════════ -->
<div id="page-dashboard" class="page">
  <div class="wrap">
    <h2>Dashboard</h2>
    <div class="g4" style="margin-bottom:28px">
      <div class="stat"><div class="stat-val" id="db-bal">0</div><div class="stat-lbl">Balance</div></div>
      <div class="stat"><div class="stat-val" id="db-earned">0</div><div class="stat-lbl">Earned</div></div>
      <div class="stat"><div class="stat-val" id="db-tasks">0</div><div class="stat-lbl">My Tasks</div></div>
      <div class="stat"><div class="stat-val" id="db-apps">0</div><div class="stat-lbl">Applications</div></div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap">
      <button class="btn btn-primary" id="post-task-btn" style="display:none" onclick="openM('post-task')">+ Post Task</button>
      <button class="btn btn-ghost"   onclick="go('wallet')">💳 Wallet</button>
    </div>
    <div id="db-tasks-list"></div>
  </div>
</div>

<!-- ═══════════ PAGE: WALLET ═══════════ -->
<div id="page-wallet" class="page">
  <div class="wrap">
    <h2>Wallet</h2>
    <div class="balance-card" style="margin-bottom:20px">
      <div class="balance-val" id="wal-balance">0</div>
      <div class="balance-lbl">KES Available</div>
    </div>
    <div class="g2" style="margin-bottom:24px">
      <button class="btn btn-primary btn-block" onclick="openM('deposit')">📥 Deposit</button>
      <button class="btn btn-outline btn-block"  onclick="openM('withdraw')">📤 Withdraw</button>
    </div>
    <div class="card">
      <h3>Transaction History</h3>
      <div id="ledger-list"></div>
    </div>
  </div>
</div>

<!-- ═══════════ PAGE: ADMIN ═══════════ -->
<div id="page-admin" class="page">
  <div class="wrap">
    <h2>⚡ Admin Panel</h2>
    <div class="tabs">
      <button class="tab on" onclick="adminTab('overview',this)">Overview</button>
      <button class="tab"    onclick="adminTab('users',this)">Users</button>
      <button class="tab"    onclick="adminTab('tasks',this)">Tasks</button>
      <button class="tab"    onclick="adminTab('payments',this)">Payments</button>
    </div>
    <div id="admin-overview" class="tab-pane on"></div>
    <div id="admin-users"    class="tab-pane"></div>
    <div id="admin-tasks"    class="tab-pane"></div>
    <div id="admin-payments" class="tab-pane"></div>
  </div>
</div>

<!-- ═══════════ MODALS ═══════════ -->

<!-- Login -->
<div class="overlay" id="m-login" onclick="bgClose(event,'login')">
  <div class="modal">
    <div class="modal-header">
      <h2>Welcome Back</h2>
      <button class="close-btn" onclick="closeM('login')">✕</button>
    </div>
    <div class="form-row"><label>Email</label><input type="email" id="l-email" placeholder="you@example.com"></div>
    <div class="form-row"><label>Password</label><input type="password" id="l-pass" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn btn-primary btn-block" id="login-btn" onclick="doLogin()">Login →</button>
    <p style="text-align:center;margin-top:16px;color:var(--t2);font-size:13px">
      Demo: <b>alice@demo.com</b> / <b>Demo@123!</b>
    </p>
  </div>
</div>

<!-- Register -->
<div class="overlay" id="m-register" onclick="bgClose(event,'register')">
  <div class="modal">
    <div class="modal-header">
      <h2>Create Account</h2>
      <button class="close-btn" onclick="closeM('register')">✕</button>
    </div>
    <div class="form-row"><label>Full Name</label><input type="text" id="r-name" placeholder="John Doe"></div>
    <div class="form-row"><label>Email</label><input type="email" id="r-email" placeholder="you@example.com"></div>
    <div class="form-row"><label>Phone</label><input type="tel" id="r-phone" placeholder="+254..."></div>
    <div class="form-row"><label>Password</label><input type="password" id="r-pass" placeholder="Min 8 characters"></div>
    <div class="form-row">
      <label>Account Type</label>
      <select id="r-role">
        <option value="worker">Worker — I want to earn money</option>
        <option value="client">Client — I want to hire workers</option>
      </select>
    </div>
    <div class="form-row"><label>Referral Code (optional)</label><input type="text" id="r-ref" placeholder="ABC12345"></div>
    <button class="btn btn-primary btn-block" id="register-btn" onclick="doRegister()">Create Account →</button>
  </div>
</div>

<!-- Post Task -->
<div class="overlay" id="m-post-task" onclick="bgClose(event,'post-task')">
  <div class="modal">
    <div class="modal-header">
      <h2>Post a Task</h2>
      <button class="close-btn" onclick="closeM('post-task')">✕</button>
    </div>
    <div class="form-row"><label>Title</label><input type="text" id="pt-title" placeholder="e.g. Data Entry — 1000 records"></div>
    <div class="form-row">
      <label>Category</label>
      <select id="pt-cat">
        <option>Data Entry</option><option>Writing</option><option>Design</option>
        <option>Research</option><option>Social Media</option><option>Translation</option>
        <option>Admin</option><option>Other</option>
      </select>
    </div>
    <div class="form-row"><label>Description</label><textarea id="pt-desc" rows="3" placeholder="Describe the task in detail..."></textarea></div>
    <div class="g2">
      <div class="form-row"><label>Budget (KES)</label><input type="number" id="pt-budget" placeholder="500"></div>
      <div class="form-row"><label>Deadline</label><input type="date" id="pt-deadline"></div>
    </div>
    <p style="font-size:12px;color:var(--t2);margin-bottom:16px">Platform fee: 7% held in escrow</p>
    <button class="btn btn-primary btn-block" id="post-btn" onclick="doPostTask()">Post Task →</button>
  </div>
</div>

<!-- Deposit -->
<div class="overlay" id="m-deposit" onclick="bgClose(event,'deposit')">
  <div class="modal">
    <div class="modal-header">
      <h2>Deposit via M-Pesa</h2>
      <button class="close-btn" onclick="closeM('deposit')">✕</button>
    </div>
    <div class="form-row"><label>Amount (KES)</label><input type="number" id="dep-amount" placeholder="Min 100" min="100"></div>
    <div class="form-row"><label>M-Pesa Phone</label><input type="tel" id="dep-phone" placeholder="+254..."></div>
    <button class="btn btn-primary btn-block" id="dep-btn" onclick="doDeposit()">Send STK Push →</button>
    <p style="text-align:center;margin-top:12px;color:var(--t2);font-size:13px">Demo mode: auto-credits instantly</p>
  </div>
</div>

<!-- Withdraw -->
<div class="overlay" id="m-withdraw" onclick="bgClose(event,'withdraw')">
  <div class="modal">
    <div class="modal-header">
      <h2>Withdraw to M-Pesa</h2>
      <button class="close-btn" onclick="closeM('withdraw')">✕</button>
    </div>
    <div class="form-row"><label>Amount (KES)</label><input type="number" id="wit-amount" placeholder="Min 100" min="100"></div>
    <div class="form-row"><label>M-Pesa Phone</label><input type="tel" id="wit-phone" placeholder="+254..."></div>
    <p style="font-size:12px;color:var(--t2);margin-bottom:16px">Processing fee: KES 30</p>
    <button class="btn btn-primary btn-block" id="wit-btn" onclick="doWithdraw()">Withdraw →</button>
  </div>
</div>

<!-- Task Detail -->
<div class="overlay" id="m-task-detail" onclick="bgClose(event,'task-detail')">
  <div class="modal" id="task-detail-body"></div>
</div>

<script>
/* ──────────────────────────────────────────────────────
   STATE
────────────────────────────────────────────────────── */
const BASE = window.location.origin;
let TOKEN = localStorage.getItem('tk');
let ME    = null;
let PAGE  = 'home';

/* ──────────────────────────────────────────────────────
   API
────────────────────────────────────────────────────── */
async function api(method, path, body) {
  const h = {'Content-Type':'application/json'};
  if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
  try {
    const r = await fetch(BASE + '/api' + path, {
      method, headers: h, body: body ? JSON.stringify(body) : undefined
    });
    const d = await r.json();
    if (r.status === 401) { doLogout(); return d; }
    return d;
  } catch(e) {
    console.error(e);
    return {success: false, error: 'Network error'};
  }
}

/* ──────────────────────────────────────────────────────
   TOAST
────────────────────────────────────────────────────── */
function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = 't-item t-' + type;
  el.textContent = msg;
  document.getElementById('toast').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

/* ──────────────────────────────────────────────────────
   MODALS
────────────────────────────────────────────────────── */
function openM(id) { document.getElementById('m-'+id).classList.add('on'); }
function closeM(id){ document.getElementById('m-'+id).classList.remove('on'); }
function bgClose(e, id) { if (e.target === e.currentTarget) closeM(id); }

/* ──────────────────────────────────────────────────────
   NAVIGATION
────────────────────────────────────────────────────── */
function go(page) {
  PAGE = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.getElementById('page-' + page).classList.add('on');
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('on'));
  const nl = document.getElementById('nl-' + page);
  if (nl) nl.classList.add('on');
  window.scrollTo(0, 0);
  const loaders = {home:loadHome, tasks:loadTasks, dashboard:loadDashboard,
                   wallet:loadWallet, admin:loadAdmin};
  if (loaders[page]) loaders[page]();
}

/* ──────────────────────────────────────────────────────
   AUTH
────────────────────────────────────────────────────── */
async function boot() {
  if (!TOKEN) return updateNav();
  const d = await api('GET', '/auth/me');
  if (d.success) { ME = d.user; }
  else { TOKEN = null; localStorage.removeItem('tk'); }
  updateNav();
}

function updateNav() {
  const loggedIn = !!ME;
  const roles = { admin: true, client: true, worker: true };
  document.getElementById('btn-login').style.display  = loggedIn ? 'none' : '';
  document.getElementById('btn-signup').style.display = loggedIn ? 'none' : '';
  document.getElementById('btn-logout').style.display = loggedIn ? '' : 'none';
  document.getElementById('user-info').textContent    = loggedIn ? `${ME.name} (${ME.role})` : '';
  ['dashboard','wallet'].forEach(p => {
    document.getElementById('nl-'+p).style.display = loggedIn ? '' : 'none';
  });
  document.getElementById('nl-admin').style.display = (loggedIn && ME.role==='admin') ? '' : 'none';
  if (document.getElementById('post-task-btn'))
    document.getElementById('post-task-btn').style.display =
      (loggedIn && ['client','admin'].includes(ME.role)) ? '' : 'none';
}

async function doLogin() {
  const btn = document.getElementById('login-btn');
  btn.disabled = true; btn.textContent = 'Logging in...';
  const d = await api('POST', '/auth/login', {
    email: document.getElementById('l-email').value,
    password: document.getElementById('l-pass').value
  });
  btn.disabled = false; btn.textContent = 'Login →';
  if (!d.success) { toast(d.error || 'Login failed', 'error'); return; }
  TOKEN = d.token; ME = d.user;
  localStorage.setItem('tk', TOKEN);
  closeM('login'); updateNav(); toast('Welcome back, ' + ME.name + '!');
  go('dashboard');
}

async function doRegister() {
  const btn = document.getElementById('register-btn');
  btn.disabled = true; btn.textContent = 'Creating account...';
  const d = await api('POST', '/auth/register', {
    name:         document.getElementById('r-name').value,
    email:        document.getElementById('r-email').value,
    phone:        document.getElementById('r-phone').value,
    password:     document.getElementById('r-pass').value,
    role:         document.getElementById('r-role').value,
    referral_code:document.getElementById('r-ref').value
  });
  btn.disabled = false; btn.textContent = 'Create Account →';
  if (!d.success) { toast(d.error || 'Registration failed', 'error'); return; }
  TOKEN = d.token; ME = d.user;
  localStorage.setItem('tk', TOKEN);
  closeM('register'); updateNav(); toast('Account created! Welcome ' + ME.name);
  go('dashboard');
}

function doLogout() {
  TOKEN = null; ME = null;
  localStorage.removeItem('tk');
  updateNav();
  go('home');
  toast('Logged out', 'info');
}

/* ──────────────────────────────────────────────────────
   HOME
────────────────────────────────────────────────────── */
async function loadHome() {
  const [stats, tasks] = await Promise.all([
    api('GET', '/stats'),
    api('GET', '/tasks?limit=6')
  ]);

  if (stats.success) {
    const s = stats.stats;
    document.getElementById('home-stats').innerHTML = `
      <div class="stat"><div class="stat-val">${fmt(s.total_users)}+</div><div class="stat-lbl">Members</div></div>
      <div class="stat"><div class="stat-val">${fmt(s.total_tasks)}+</div><div class="stat-lbl">Tasks Posted</div></div>
      <div class="stat"><div class="stat-val">${fmt(s.completed_tasks)}+</div><div class="stat-lbl">Completed</div></div>
      <div class="stat"><div class="stat-val">KES ${fmt(s.total_paid_kes)}+</div><div class="stat-lbl">Paid Out</div></div>
    `;
  }

  if (tasks.success) {
    document.getElementById('home-tasks').innerHTML =
      tasks.tasks.length
        ? tasks.tasks.map(t => taskCard(t)).join('')
        : '<div class="empty"><p>No tasks yet</p></div>';
  }
}

/* ──────────────────────────────────────────────────────
   TASKS
────────────────────────────────────────────────────── */
const CATS = ['All','Data Entry','Writing','Design','Research','Social Media','Translation','Admin','Other'];
let searchTimer;
let activeFilter = 'All';

function debSearch(v) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadTasks(v), 350);
}

async function loadTasks(search='') {
  document.getElementById('tasks-grid').innerHTML =
    '<div style="color:var(--t2);text-align:center;padding:40px">Loading...</div>';

  // Category filter bar (once)
  if (!document.getElementById('cat-filter').children.length) {
    document.getElementById('cat-filter').innerHTML = CATS.map(c =>
      `<button class="btn btn-ghost btn-sm ${c===activeFilter?'on':''}"
               onclick="setCat('${c}')">${c}</button>`
    ).join('');
  }

  const cat = activeFilter === 'All' ? '' : activeFilter;
  const d = await api('GET', `/tasks?limit=50&search=${encodeURIComponent(search)}&category=${encodeURIComponent(cat)}`);

  if (!d.success) {
    document.getElementById('tasks-grid').innerHTML =
      '<div class="empty"><p>Could not load tasks</p></div>';
    return;
  }

  document.getElementById('tasks-grid').innerHTML =
    d.tasks.length
      ? d.tasks.map(t => taskCard(t)).join('')
      : `<div class="empty" style="grid-column:1/-1"><p>No tasks found</p></div>`;
}

function setCat(c) {
  activeFilter = c;
  document.querySelectorAll('#cat-filter .btn').forEach(b => {
    b.classList.toggle('on', b.textContent === c);
  });
  loadTasks(document.getElementById('search-input')?.value || '');
}

function taskCard(t) {
  const badge = {open:'badge-open',in_progress:'badge-progress',review:'badge-review',
                 completed:'badge-done',cancelled:'badge-cancelled'}[t.status] || 'badge-open';
  return `
  <div class="card task-card" onclick="openTask('${t.id}')">
    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:10px">
      <span class="tag">${t.category||'Other'}</span>
      <span class="badge ${badge}">${t.status}</span>
    </div>
    <h3 style="margin-bottom:8px;font-size:16px;font-weight:700">${esc(t.title)}</h3>
    <p style="color:var(--t2);font-size:13px;margin-bottom:14px;
              display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">
      ${esc(t.description||'No description')}
    </p>
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-family:'Syne';font-size:22px;font-weight:900;color:var(--ac)">KES ${fmt(t.budget)}</span>
      <span style="font-size:12px;color:var(--t2)">👥 ${t.applications_count} applied</span>
    </div>
  </div>`;
}

async function openTask(id) {
  const d = await api('GET', '/tasks/' + id);
  if (!d.success) { toast('Could not load task', 'error'); return; }
  const t    = d.task;
  const apps = d.applications || [];

  const canApply  = ME && ME.role === 'worker' && t.status === 'open';
  const isClient  = ME && ME.id === t.client_id;
  const isWorker  = ME && ME.id === t.worker_id;

  document.getElementById('task-detail-body').innerHTML = `
    <div class="modal-header">
      <h2>${esc(t.title)}</h2>
      <button class="close-btn" onclick="closeM('task-detail')">✕</button>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      <span class="tag">${t.category}</span>
      <span class="badge ${t.status==='open'?'badge-open':'badge-progress'}">${t.status}</span>
      ${t.is_featured ? '<span class="badge" style="background:rgba(245,158,11,.15);color:var(--yellow)">⭐ Featured</span>' : ''}
    </div>
    <p style="color:var(--t2);margin-bottom:20px">${esc(t.description||'No description provided')}</p>
    <div class="g2" style="margin-bottom:20px">
      <div class="stat"><div class="stat-val">KES ${fmt(t.budget)}</div><div class="stat-lbl">Budget</div></div>
      <div class="stat"><div class="stat-val">${t.applications_count}</div><div class="stat-lbl">Applicants</div></div>
    </div>

    ${canApply ? `
      <div style="border-top:1px solid var(--border);padding-top:16px">
        <h3>Apply for this Task</h3>
        <div class="form-row">
          <label>Cover Letter</label>
          <textarea id="apply-letter" rows="3" placeholder="Why are you the best for this task?"></textarea>
        </div>
        <button class="btn btn-primary btn-block" onclick="doApply('${t.id}')">Apply Now →</button>
      </div>
    ` : ''}

    ${isWorker && t.status === 'in_progress' ? `
      <div style="border-top:1px solid var(--border);padding-top:16px">
        <h3>Submit Your Work</h3>
        <div class="form-row">
          <textarea id="submit-text" rows="4" placeholder="Describe what you've done, share links, etc..."></textarea>
        </div>
        <button class="btn btn-primary btn-block" onclick="doSubmit('${t.id}')">Submit Work →</button>
      </div>
    ` : ''}

    ${isClient && apps.length > 0 ? `
      <div style="border-top:1px solid var(--border);padding-top:16px">
        <h3>Applications (${apps.length})</h3>
        ${apps.map(a => `
          <div class="card" style="margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <strong>${esc(a.worker_name)}</strong>
              <span class="badge ${a.status==='accepted'?'badge-done':'badge-open'}">${a.status}</span>
            </div>
            ${a.cover_letter ? `<p style="color:var(--t2);font-size:13px;margin-top:8px">${esc(a.cover_letter)}</p>` : ''}
            ${t.status === 'open' ? `
              <button class="btn btn-primary btn-sm" style="margin-top:10px"
                      onclick="doAcceptApp('${t.id}','${a.id}')">Accept</button>
            ` : ''}
          </div>
        `).join('')}
      </div>
    ` : ''}

    ${isClient && t.status === 'review' ? `
      <div style="border-top:1px solid var(--border);padding-top:16px">
        <button class="btn btn-primary btn-block" onclick="doApprove('${t.id}')">
          ✅ Approve & Release Payment
        </button>
      </div>
    ` : ''}
  `;

  openM('task-detail');
}

async function doApply(tid) {
  const d = await api('POST', '/tasks/' + tid + '/apply', {
    cover_letter: document.getElementById('apply-letter').value
  });
  if (d.success) { closeM('task-detail'); toast('Application submitted!'); }
  else toast(d.error || 'Error', 'error');
}

async function doSubmit(tid) {
  const d = await api('POST', '/tasks/' + tid + '/submit', {
    submission_text: document.getElementById('submit-text').value
  });
  if (d.success) { closeM('task-detail'); toast('Work submitted!'); loadDashboard(); }
  else toast(d.error || 'Error', 'error');
}

async function doAcceptApp(tid, aid) {
  const d = await api('POST', '/tasks/' + tid + '/applications/' + aid + '/accept');
  if (d.success) { closeM('task-detail'); toast('Application accepted!'); loadDashboard(); }
  else toast(d.error || 'Error', 'error');
}

async function doApprove(tid) {
  const d = await api('POST', '/tasks/' + tid + '/approve');
  if (d.success) { closeM('task-detail'); toast('Payment released!'); loadDashboard(); loadWallet(); }
  else toast(d.error || 'Error', 'error');
}

/* ──────────────────────────────────────────────────────
   DASHBOARD
────────────────────────────────────────────────────── */
async function loadDashboard() {
  if (!ME) return;
  const [walData, tasksData] = await Promise.all([
    api('GET', '/wallet'),
    api('GET', '/tasks/my')
  ]);
  if (walData.success) {
    const w = walData.wallet;
    document.getElementById('db-bal').textContent    = 'KES ' + fmt(w.balance);
    document.getElementById('db-earned').textContent = 'KES ' + fmt(w.total_earned);
  }
  if (tasksData.success) {
    const ts = tasksData.tasks;
    document.getElementById('db-tasks').textContent = ts.length;
    document.getElementById('db-apps').textContent  = ts.filter(t => t.status==='in_progress').length;
    document.getElementById('db-tasks-list').innerHTML = ts.length
      ? `<h3>My Tasks</h3><div class="g2">${ts.map(t => taskCard(t)).join('')}</div>`
      : `<div class="empty"><p>${ME.role==='client'?'No tasks posted yet. Click "Post Task" to start.':'No tasks yet. <span style="color:var(--ac);cursor:pointer" onclick="go(\'tasks\')">Browse available tasks →</span>'}</p></div>`;
  }
}

/* ──────────────────────────────────────────────────────
   WALLET
────────────────────────────────────────────────────── */
async function loadWallet() {
  if (!ME) return;
  const d = await api('GET', '/wallet');
  if (!d.success) return;
  const w = d.wallet;
  document.getElementById('wal-balance').textContent = fmt(w.balance);
  document.getElementById('ledger-list').innerHTML = d.ledger.length
    ? d.ledger.map(e => {
        const pos = e.amount > 0;
        return `
          <div class="ledger-row">
            <div>
              <div style="font-weight:600;font-size:14px">${esc(e.description||e.type)}</div>
              <div class="ledger-type">${e.type} · ${new Date(e.created_at).toLocaleDateString()}</div>
            </div>
            <div class="ledger-amt ${pos?'pos':'neg'}">${pos?'+':''}${fmt(e.amount)} KES</div>
          </div>`;
      }).join('')
    : '<div class="empty"><p>No transactions yet</p></div>';
}

async function doDeposit() {
  const btn = document.getElementById('dep-btn');
  btn.disabled = true; btn.textContent = 'Processing...';
  const d = await api('POST', '/wallet/deposit', {
    amount: parseFloat(document.getElementById('dep-amount').value) || 0,
    phone:  document.getElementById('dep-phone').value
  });
  btn.disabled = false; btn.textContent = 'Send STK Push →';
  if (d.success) { closeM('deposit'); toast(d.message || 'Deposit initiated!'); loadWallet(); loadDashboard(); }
  else toast(d.error || 'Error', 'error');
}

async function doWithdraw() {
  const btn = document.getElementById('wit-btn');
  btn.disabled = true; btn.textContent = 'Processing...';
  const d = await api('POST', '/wallet/withdraw', {
    amount: parseFloat(document.getElementById('wit-amount').value) || 0,
    phone:  document.getElementById('wit-phone').value
  });
  btn.disabled = false; btn.textContent = 'Withdraw →';
  if (d.success) { closeM('withdraw'); toast('Withdrawal initiated!'); loadWallet(); }
  else toast(d.error || 'Error', 'error');
}

async function doPostTask() {
  const btn = document.getElementById('post-btn');
  btn.disabled = true; btn.textContent = 'Posting...';
  const d = await api('POST', '/tasks', {
    title:       document.getElementById('pt-title').value,
    category:    document.getElementById('pt-cat').value,
    description: document.getElementById('pt-desc').value,
    budget:      parseFloat(document.getElementById('pt-budget').value) || 0,
    deadline:    document.getElementById('pt-deadline').value
  });
  btn.disabled = false; btn.textContent = 'Post Task →';
  if (d.success) { closeM('post-task'); toast('Task posted!'); loadDashboard(); }
  else toast(d.error || 'Error', 'error');
}

/* ──────────────────────────────────────────────────────
   ADMIN
────────────────────────────────────────────────────── */
function adminTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('on'));
  el.classList.add('on');
  document.getElementById('admin-'+name).classList.add('on');
  const loaders = {overview:loadAdminOverview, users:loadAdminUsers,
                   tasks:loadAdminTasks, payments:loadAdminPayments};
  if (loaders[name]) loaders[name]();
}

async function loadAdmin() {
  if (!ME || ME.role !== 'admin') return;
  loadAdminOverview();
}

async function loadAdminOverview() {
  const d = await api('GET', '/admin/dashboard');
  if (!d.success) return;
  const s = d.stats;
  document.getElementById('admin-overview').innerHTML = `
    <div class="g4" style="margin-bottom:24px">
      <div class="stat"><div class="stat-val">${s.users}</div><div class="stat-lbl">Users</div></div>
      <div class="stat"><div class="stat-val">${s.open}</div><div class="stat-lbl">Open Tasks</div></div>
      <div class="stat"><div class="stat-val">${s.completed}</div><div class="stat-lbl">Completed</div></div>
      <div class="stat"><div class="stat-val">KES ${fmt(s.total_deposited)}</div><div class="stat-lbl">Total Deposited</div></div>
    </div>
    <div class="g2">
      <div class="card">
        <h3>Platform Status</h3>
        <p style="color:var(--t2)">Total tasks: ${s.tasks}</p>
        <p style="color:var(--t2)">Pending withdrawals: ${s.pending_withdrawals}</p>
      </div>
      <div class="card">
        <h3>Quick Actions</h3>
        <button class="btn btn-ghost btn-sm" onclick="adminTab('users', document.querySelectorAll('.tab')[1])">Manage Users</button>
      </div>
    </div>`;
}

async function loadAdminUsers() {
  const d = await api('GET', '/admin/users?limit=100');
  if (!d.success) return;
  document.getElementById('admin-users').innerHTML = `
    <p style="color:var(--t2);margin-bottom:12px">${d.total} users total</p>
    <div style="overflow-x:auto">
    <table class="tbl">
      <thead><tr>
        <th>Name</th><th>Email</th><th>Role</th><th>Balance</th><th>Status</th><th>Actions</th>
      </tr></thead>
      <tbody>
      ${d.users.map(u => `
        <tr>
          <td>${esc(u.name)}</td>
          <td style="color:var(--t2)">${esc(u.email)}</td>
          <td><span class="badge badge-open">${u.role}</span></td>
          <td>KES ${fmt(u.wallet_balance)}</td>
          <td>
            <span class="badge ${u.is_active?'badge-done':'badge-cancelled'}">
              ${u.is_active?(u.is_banned?'Banned':'Active'):'Inactive'}
            </span>
          </td>
          <td>
            <button class="btn btn-danger btn-sm"
              onclick="adminBanUser('${u.id}',${!u.is_banned})">
              ${u.is_banned?'Unban':'Ban'}
            </button>
          </td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
}

async function loadAdminTasks() {
  const d = await api('GET', '/admin/tasks?limit=100');
  if (!d.success) return;
  document.getElementById('admin-tasks').innerHTML = `
    <p style="color:var(--t2);margin-bottom:12px">${d.total} tasks total</p>
    <div style="overflow-x:auto">
    <table class="tbl">
      <thead><tr><th>Title</th><th>Category</th><th>Budget</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
      ${d.tasks.map(t => `
        <tr>
          <td>${esc(t.title)}</td>
          <td><span class="tag">${t.category}</span></td>
          <td>KES ${fmt(t.budget)}</td>
          <td><span class="badge badge-${t.status==='open'?'open':t.status==='completed'?'done':'progress'}">${t.status}</span></td>
          <td>
            <button class="btn btn-danger btn-sm"
              onclick="adminDelTask('${t.id}')">Delete</button>
          </td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
}

async function loadAdminPayments() {
  const d = await api('GET', '/admin/payments?limit=100');
  if (!d.success) return;
  document.getElementById('admin-payments').innerHTML = `
    <p style="color:var(--t2);margin-bottom:12px">${d.total} payments total</p>
    <div style="overflow-x:auto">
    <table class="tbl">
      <thead><tr><th>User</th><th>Amount</th><th>Type</th><th>Status</th><th>Reference</th><th>Actions</th></tr></thead>
      <tbody>
      ${d.payments.map(p => `
        <tr>
          <td>${esc(p.user_name)}</td>
          <td>KES ${fmt(p.amount)}</td>
          <td>${p.type}</td>
          <td><span class="badge ${p.status==='completed'?'badge-done':p.status==='processing'?'badge-review':'badge-open'}">${p.status}</span></td>
          <td style="color:var(--t2);font-size:12px">${p.reference}</td>
          <td>
            ${p.status==='processing'?`
              <button class="btn btn-primary btn-sm"
                onclick="adminApprovePayment('${p.id}')">Approve</button>` : ''}
          </td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
}

async function adminBanUser(id, ban) {
  const d = await api('PUT', '/admin/users/' + id, {is_banned: ban});
  if (d.success) { toast(ban?'User banned':'User unbanned'); loadAdminUsers(); }
  else toast(d.error, 'error');
}

async function adminDelTask(id) {
  if (!confirm('Delete this task?')) return;
  const d = await api('DELETE', '/admin/tasks/' + id);
  if (d.success) { toast('Task deleted'); loadAdminTasks(); }
  else toast(d.error, 'error');
}

async function adminApprovePayment(id) {
  const d = await api('POST', '/admin/payments/' + id + '/approve');
  if (d.success) { toast('Payment approved'); loadAdminPayments(); }
  else toast(d.error, 'error');
}

/* ──────────────────────────────────────────────────────
   UTILS
────────────────────────────────────────────────────── */
function fmt(n) {
  if (n == null) return '0';
  const num = parseFloat(n);
  if (isNaN(num)) return '0';
  return num >= 1000000 ? (num/1000000).toFixed(1)+'M'
       : num >= 1000    ? (num/1000).toFixed(1)+'K'
       : num.toLocaleString('en-KE');
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* ──────────────────────────────────────────────────────
   BOOT
────────────────────────────────────────────────────── */
boot();
loadHome();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# SERVE FRONTEND
# ─────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML)

@app.route('/health', methods=['GET'])
def _health():
    return jsonify(status='ok')

# ─────────────────────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_req(e):
    return jsonify(success=False, error='Bad request'), 400

@app.errorhandler(401)
def unauth(e):
    return jsonify(success=False, error='Unauthorized'), 401

@app.errorhandler(403)
def forbid(e):
    return jsonify(success=False, error='Forbidden'), 403

@app.errorhandler(404)
def notfound(e):
    return jsonify(success=False, error='Not found'), 404

@app.errorhandler(429)
def ratelimit(e):
    return jsonify(success=False, error='Too many requests — slow down'), 429

@app.errorhandler(500)
def server_err(e):
    log.error(f'500: {e}')
    return jsonify(success=False, error='Internal server error'), 500


# ─────────────────────────────────────────────────────────────
# DATABASE SEED
# ─────────────────────────────────────────────────────────────

def seed():
    if User.query.filter_by(email='admin@dtip.co.ke').first():
        return

    # Admin
    admin = User(email='admin@dtip.co.ke', name='Admin', role='admin', referral_code=gen_ref())
    admin.set_password('Admin@2024!')
    db.session.add(admin)
    db.session.flush()
    credit(admin.id, 50000, 'bonus', 'Seed balance')

    # Worker
    worker = User(email='alice@demo.com', name='Alice Wanjiru', role='worker', referral_code=gen_ref())
    worker.set_password('Demo@123!')
    db.session.add(worker)
    db.session.flush()
    credit(worker.id, 5000, 'deposit', 'Seed balance')

    # Client
    client = User(email='bob@demo.com', name='Bob Kamau', role='client', referral_code=gen_ref())
    client.set_password('Demo@123!')
    db.session.add(client)
    db.session.flush()
    credit(client.id, 10000, 'deposit', 'Seed balance')

    # Sample tasks
    for i, (title, cat, budget, desc) in enumerate([
        ('Data Entry — 500 Product Records',   'Data Entry',  1000,
         'Enter 500 product records into Excel with proper formatting and validation.'),
        ('Social Media Posts — 30 Days',        'Social Media', 1500,
         'Write and schedule 30 social media posts for Instagram and Facebook.'),
        ('Translate English to Swahili (2000w)', 'Translation',  800,
         'Translate a 2000-word marketing document from English to Swahili.'),
        ('Research — 10 Kenya Counties Report', 'Research',     1200,
         'Compile economic data for 10 Kenya counties into a structured report.'),
        ('Product Descriptions — 100 Items',    'Writing',       900,
         'Write 100 compelling product descriptions for an e-commerce site.'),
        ('Logo Design — Startup Brand',          'Design',       2000,
         'Create a professional logo and brand identity for a tech startup.'),
    ]):
        task = Task(client_id=client.id, title=title, category=cat,
                    description=desc, budget=budget, status='open',
                    escrow_held=True, views=5+i*7, is_featured=(i < 3),
                    deadline=datetime.utcnow() + timedelta(days=7+i))
        db.session.add(task)

    db.session.commit()
    log.info('✅ Database seeded with demo data')


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed()

    port = int(os.getenv('PORT', 5000))
    print(f"""
╔══════════════════════════════════════════════════════════╗
║           DTIP v4.0  —  Running on port {port:<5}          ║
╠══════════════════════════════════════════════════════════╣
║  Admin:   admin@dtip.co.ke    /  Admin@2024!             ║
║  Worker:  alice@demo.com      /  Demo@123!               ║
║  Client:  bob@demo.com        /  Demo@123!               ║
╠══════════════════════════════════════════════════════════╣
║  http://localhost:{port}                                   ║
╚══════════════════════════════════════════════════════════╝
""")
    app.run(host='0.0.0.0', port=port,
            debug=(os.getenv('ENV', 'development') == 'development'))
