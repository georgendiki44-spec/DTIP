"""
DTIP v3.0 - Enterprise SaaS Platform
Production-ready with authentication, payments, admin control
Single-file modular design for Railway deployment
"""

import os, json, uuid, secrets, string, hmac, hashlib, requests, sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
import jwt as pyjwt
from dotenv import load_dotenv

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG & INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', secrets.token_hex(32)),
    SESSION_COOKIE_SECURE=os.getenv('ENV') == 'production',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    JSON_SORT_KEYS=False,
)

CORS(app, supports_credentials=True)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])

# Configuration
DB_PATH = os.getenv('DB_PATH', 'dtip.db')
JWT_SECRET = app.config['SECRET_KEY']
INTASEND_API_KEY = os.getenv('INTASEND_API_KEY', 'demo')
INTASEND_PUB_KEY = os.getenv('INTASEND_PUB_KEY', 'demo')
PLATFORM_FEE = float(os.getenv('PLATFORM_FEE', '7'))
WITHDRAWAL_FEE = float(os.getenv('WITHDRAWAL_FEE', '30'))
PORT = int(os.getenv('PORT', 5000))

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE SETUP
# ═══════════════════════════════════════════════════════════════════════════════

def init_db():
    """Initialize SQLite database with all tables"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        name TEXT,
        hashed_password TEXT,
        role TEXT DEFAULT 'worker',
        is_active BOOLEAN DEFAULT 1,
        is_banned BOOLEAN DEFAULT 0,
        membership TEXT DEFAULT 'free',
        referral_code TEXT UNIQUE,
        referred_by TEXT,
        avatar_color TEXT DEFAULT '#6366f1',
        profile_pic TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP,
        FOREIGN KEY(referred_by) REFERENCES users(id)
    )''')
    
    # Wallets table
    c.execute('''CREATE TABLE IF NOT EXISTS wallets (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        balance REAL DEFAULT 0,
        held REAL DEFAULT 0,
        total_earned REAL DEFAULT 0,
        total_withdrawn REAL DEFAULT 0,
        total_referral_earnings REAL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Ledger (transaction history)
    c.execute('''CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        type TEXT,
        amount REAL,
        balance_after REAL,
        reference TEXT,
        description TEXT,
        status TEXT DEFAULT 'completed',
        meta TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        INDEX idx_user_date (user_id, created_at)
    )''')
    
    # Tasks table
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        title TEXT,
        category TEXT,
        description TEXT,
        steps TEXT,
        budget REAL,
        deadline TIMESTAMP,
        status TEXT DEFAULT 'open',
        escrow_held BOOLEAN DEFAULT 0,
        worker_id TEXT,
        attachments TEXT DEFAULT '[]',
        tags TEXT DEFAULT '[]',
        views INTEGER DEFAULT 0,
        applications_count INTEGER DEFAULT 0,
        is_featured BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY(client_id) REFERENCES users(id),
        FOREIGN KEY(worker_id) REFERENCES users(id),
        INDEX idx_status (status),
        INDEX idx_category (category)
    )''')
    
    # Applications table
    c.execute('''CREATE TABLE IF NOT EXISTS applications (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        cover_letter TEXT,
        proposed_amount REAL,
        status TEXT DEFAULT 'pending',
        submission_text TEXT,
        submitted_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES tasks(id),
        FOREIGN KEY(worker_id) REFERENCES users(id),
        UNIQUE(task_id, worker_id),
        INDEX idx_worker (worker_id)
    )''')
    
    # Reviews table
    c.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        reviewer_id TEXT NOT NULL,
        reviewee_id TEXT NOT NULL,
        rating INTEGER,
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES tasks(id),
        FOREIGN KEY(reviewer_id) REFERENCES users(id),
        FOREIGN KEY(reviewee_id) REFERENCES users(id)
    )''')
    
    # Payments table
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount REAL,
        type TEXT,
        status TEXT DEFAULT 'pending',
        provider TEXT DEFAULT 'mpesa',
        reference TEXT UNIQUE,
        phone TEXT,
        meta TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        INDEX idx_status (status),
        INDEX idx_reference (reference)
    )''')
    
    # Notifications table
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT,
        message TEXT,
        type TEXT DEFAULT 'info',
        is_read BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        INDEX idx_user_read (user_id, is_read)
    )''')
    
    # Site settings table
    c.execute('''CREATE TABLE IF NOT EXISTS site_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Activity/Feed table
    c.execute('''CREATE TABLE IF NOT EXISTS activity (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        user_name TEXT,
        action TEXT,
        amount REAL,
        is_fake BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_created (created_at)
    )''')
    
    conn.commit()
    conn.close()

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def query_one(sql, params=()):
    """Query single row"""
    conn = get_db()
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def query_all(sql, params=()):
    """Query multiple rows"""
    conn = get_db()
    c = conn.cursor()
    c.execute(sql, params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def execute(sql, params=()):
    """Execute SQL"""
    conn = get_db()
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    conn.close()

def execute_many(sql, params_list):
    """Execute multiple SQL"""
    conn = get_db()
    c = conn.cursor()
    c.executemany(sql, params_list)
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH & JWT
# ═══════════════════════════════════════════════════════════════════════════════

def create_token(user_id, role='worker', expires_in=24):
    """Create JWT token"""
    payload = {
        'sub': user_id,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=expires_in),
        'iat': datetime.utcnow()
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm='HS256')

def verify_token(token):
    """Verify JWT token"""
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
        return None

def get_current_user():
    """Get current user from token"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return None
    
    payload = verify_token(token)
    if not payload:
        return None
    
    user = query_one('SELECT * FROM users WHERE id = ?', (payload['sub'],))
    return user

def require_auth(f):
    """Decorator for authenticated endpoints"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(user, *args, **kwargs)
    return decorated

def require_admin(f):
    """Decorator for admin endpoints"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user['role'] != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        return f(user, *args, **kwargs)
    return decorated

# ═══════════════════════════════════════════════════════════════════════════════
# WALLET HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_or_create_wallet(user_id):
    """Get or create wallet"""
    w = query_one('SELECT * FROM wallets WHERE user_id = ?', (user_id,))
    if not w:
        execute('INSERT INTO wallets (id, user_id) VALUES (?, ?)', (str(uuid.uuid4()), user_id))
        w = query_one('SELECT * FROM wallets WHERE user_id = ?', (user_id,))
    return w

def credit_wallet(user_id, amount, type_, desc, ref=None, meta=None):
    """Credit wallet and add ledger entry"""
    w = get_or_create_wallet(user_id)
    new_balance = w['balance'] + amount
    execute('UPDATE wallets SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (new_balance, user_id))
    execute(
        'INSERT INTO ledger (id, user_id, type, amount, balance_after, reference, description, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), user_id, type_, amount, new_balance, ref or str(uuid.uuid4())[:8].upper(), desc, json.dumps(meta or {}))
    )
    return new_balance

def debit_wallet(user_id, amount, type_, desc, ref=None):
    """Debit wallet and add ledger entry"""
    w = get_or_create_wallet(user_id)
    if w['balance'] < amount:
        raise ValueError('Insufficient balance')
    new_balance = w['balance'] - amount
    execute('UPDATE wallets SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (new_balance, user_id))
    execute(
        'INSERT INTO ledger (id, user_id, type, amount, balance_after, reference, description) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), user_id, type_, -amount, new_balance, ref or str(uuid.uuid4())[:8].upper(), desc)
    )
    return new_balance

def add_notification(user_id, title, message, type_='info'):
    """Add notification"""
    execute(
        'INSERT INTO notifications (id, user_id, title, message, type) VALUES (?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), user_id, title, message, type_)
    )

def log_activity(user_name, action, amount=None, user_id=None, is_fake=False):
    """Log activity"""
    execute(
        'INSERT INTO activity (id, user_id, user_name, action, amount, is_fake) VALUES (?, ?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), user_id, user_name, action, amount, is_fake)
    )

def get_setting(key, default=''):
    """Get site setting"""
    row = query_one('SELECT value FROM site_settings WHERE key = ?', (key,))
    return row['value'] if row else default

def set_setting(key, value):
    """Set site setting"""
    existing = query_one('SELECT key FROM site_settings WHERE key = ?', (key,))
    if existing:
        execute('UPDATE site_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?', (value, key))
    else:
        execute('INSERT INTO site_settings (key, value) VALUES (?, ?)', (key, value))

# ═══════════════════════════════════════════════════════════════════════════════
# INTASEND INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════

def verify_intasend_signature(body, signature):
    """Verify IntaSend webhook signature"""
    try:
        expected = hmac.new(INTASEND_API_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except:
        return False

def stk_push(phone, amount, ref):
    """Send M-Pesa STK Push via IntaSend"""
    if INTASEND_API_KEY == 'demo':
        return {'status': 'success', 'reference': ref}
    
    try:
        headers = {'Authorization': f'Bearer {INTASEND_API_KEY}', 'Content-Type': 'application/json'}
        data = {
            'public_key': INTASEND_PUB_KEY,
            'currency': 'KES',
            'amount': int(amount),
            'phone_number': phone,
            'api_ref': ref,
        }
        resp = requests.post('https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/', json=data, headers=headers, timeout=10)
        result = resp.json()
        return result
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

# ═══════════════════════════════════════════════════════════════════════════════
# SEEDING & INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def seed_data():
    """Seed demo data"""
    admin = query_one('SELECT id FROM users WHERE email = ?', ('admin@dtip.co.ke',))
    if admin:
        return
    
    # Create admin
    admin_id = str(uuid.uuid4())
    ref = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    execute(
        'INSERT INTO users (id, email, phone, name, hashed_password, role, membership, referral_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (admin_id, 'admin@dtip.co.ke', '+254700000000', 'Admin', generate_password_hash('Admin@2024!'), 'admin', 'diamond', ref)
    )
    get_or_create_wallet(admin_id)
    credit_wallet(admin_id, 50000, 'bonus', 'Admin seed balance')
    
    # Create demo users
    for email, name, role in [('alice@demo.com', 'Alice', 'worker'), ('bob@demo.com', 'Bob', 'client')]:
        uid = str(uuid.uuid4())
        ref = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        execute(
            'INSERT INTO users (id, email, phone, name, hashed_password, role, referral_code) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (uid, email, '+254711111111', name, generate_password_hash('Demo@123!'), role, ref)
        )
        get_or_create_wallet(uid)
        credit_wallet(uid, 5000, 'deposit', 'Demo balance')
    
    # Create demo task
    client = query_one('SELECT id FROM users WHERE email = ?', ('bob@demo.com',))
    if client:
        execute(
            'INSERT INTO tasks (id, client_id, title, category, description, budget, deadline, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (str(uuid.uuid4()), client['id'], 'Data Entry Task', 'Data Entry', 'Enter data', 1000, '2025-05-30', 'open')
        )

def seed_settings():
    """Seed default settings"""
    defaults = {
        'site_name': 'DTIP Kenya',
        'site_tagline': 'Earn Smart. Work Smart.',
        'platform_fee': '7',
        'withdrawal_fee': '30',
        'gold_price': '500',
        'diamond_price': '1500',
        'welcome_bonus': '0',
        'referral_bonus': '200',
        'maintenance_mode': 'false',
        'registrations_open': 'true',
    }
    for k, v in defaults.items():
        if not query_one('SELECT key FROM site_settings WHERE key = ?', (k,)):
            set_setting(k, v)

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES: AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit('5 per minute')
def register():
    """User registration"""
    data = request.json
    
    if query_one('SELECT id FROM users WHERE email = ?', (data['email'],)):
        return jsonify({'error': 'Email already registered'}), 400
    
    user_id = str(uuid.uuid4())
    ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    referred_by = None
    
    if data.get('referral_code'):
        referrer = query_one('SELECT id FROM users WHERE referral_code = ?', (data['referral_code'],))
        if referrer:
            referred_by = referrer['id']
    
    try:
        execute(
            'INSERT INTO users (id, email, phone, name, hashed_password, role, referral_code, referred_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (user_id, data['email'], data.get('phone', ''), data['name'], generate_password_hash(data['password']), data.get('role', 'worker'), ref_code, referred_by)
        )
        
        get_or_create_wallet(user_id)
        
        # Welcome bonus
        welcome = float(get_setting('welcome_bonus', '0'))
        if welcome > 0:
            credit_wallet(user_id, welcome, 'bonus', 'Welcome bonus', meta={'type': 'welcome'})
        
        # Referral bonus
        if referred_by:
            ref_bonus = float(get_setting('referral_bonus', '200'))
            credit_wallet(referred_by, ref_bonus, 'bonus', f'Referral from {data["name"]}', meta={'type': 'referral'})
            add_notification(referred_by, 'Referral Bonus!', f'KES {ref_bonus} earned - {data["name"]} joined!', 'success')
        
        token = create_token(user_id, 'worker')
        user = query_one('SELECT * FROM users WHERE id = ?', (user_id,))
        
        return jsonify({'token': token, 'user': {k: v for k, v in dict(user).items() if k != 'hashed_password'}}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit('10 per minute')
def login():
    """User login"""
    data = request.json
    user = query_one('SELECT * FROM users WHERE email = ?', (data['email'],))
    
    if not user or not check_password_hash(user['hashed_password'], data['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    if user['is_banned']:
        return jsonify({'error': 'Account banned'}), 403
    
    execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
    token = create_token(user['id'], user['role'])
    
    return jsonify({'token': token, 'user': {k: v for k, v in dict(user).items() if k != 'hashed_password'}})

@app.route('/api/auth/me', methods=['GET'])
@require_auth
def get_me(user):
    """Get current user"""
    wallet = get_or_create_wallet(user['id'])
    unread = len(query_all('SELECT id FROM notifications WHERE user_id = ? AND is_read = 0', (user['id'],)))
    return jsonify({**dict(user), 'wallet': dict(wallet), 'unread_notifications': unread})

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES: WALLET
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/wallet', methods=['GET'])
@require_auth
def get_wallet(user):
    """Get wallet and ledger"""
    wallet = get_or_create_wallet(user['id'])
    ledger = query_all('SELECT * FROM ledger WHERE user_id = ? ORDER BY created_at DESC LIMIT 50', (user['id'],))
    return jsonify({'wallet': wallet, 'ledger': ledger})

@app.route('/api/wallet/deposit', methods=['POST'])
@require_auth
@limiter.limit('10 per minute')
def deposit(user):
    """Initiate deposit"""
    data = request.json
    min_dep = float(get_setting('min_deposit', '100'))
    
    if data['amount'] < min_dep:
        return jsonify({'error': f'Minimum KES {min_dep}'}), 400
    
    pay_id = str(uuid.uuid4())
    ref = f'DEP-{pay_id[:8].upper()}'
    
    execute(
        'INSERT INTO payments (id, user_id, amount, type, status, reference, phone) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (pay_id, user['id'], data['amount'], 'deposit', 'pending', ref, data['phone'])
    )
    
    # Demo mode: auto-approve
    if INTASEND_API_KEY == 'demo':
        credit_wallet(user['id'], data['amount'], 'deposit', f'M-Pesa deposit', ref=ref)
        execute('UPDATE payments SET status = ? WHERE id = ?', ('completed', pay_id))
        add_notification(user['id'], 'Deposit Confirmed!', f'KES {data["amount"]:,.0f} credited', 'success')
        log_activity(user['name'], 'deposited', data['amount'], user['id'])
        return jsonify({'status': 'success', 'message': 'Deposit confirmed', 'reference': ref})
    
    # Real mode: STK Push
    result = stk_push(data['phone'], data['amount'], ref)
    if result['status'] == 'success':
        return jsonify({'status': 'pending', 'message': 'Check your phone for M-Pesa prompt', 'reference': ref})
    return jsonify({'error': result.get('message', 'Payment failed')}), 400

@app.route('/api/wallet/withdraw', methods=['POST'])
@require_auth
@limiter.limit('5 per minute')
def withdraw(user):
    """Initiate withdrawal"""
    data = request.json
    w = get_or_create_wallet(user['id'])
    fee = float(get_setting('withdrawal_fee', '30'))
    total = data['amount'] + fee
    
    if w['balance'] < total:
        return jsonify({'error': f'Insufficient balance'}), 400
    
    if data['amount'] < 100:
        return jsonify({'error': 'Minimum KES 100'}), 400
    
    ref = f'WIT-{str(uuid.uuid4())[:8].upper()}'
    debit_wallet(user['id'], total, 'withdrawal', f'Withdrawal + KES {fee} fee', ref=ref)
    
    execute(
        'INSERT INTO payments (id, user_id, amount, type, status, reference, phone) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), user['id'], data['amount'], 'withdrawal', 'processing', ref, data['phone'])
    )
    
    add_notification(user['id'], 'Withdrawal Processing', f'KES {data["amount"]} queued for payout', 'info')
    log_activity(user['name'], 'withdrew', data['amount'], user['id'])
    
    return jsonify({'status': 'success', 'message': 'Withdrawal initiated', 'reference': ref})

@app.route('/api/payments/callback', methods=['POST'])
def payment_callback():
    """IntaSend webhook callback"""
    sig = request.headers.get('X-Intasend-Signature', '')
    body = request.get_data(as_text=True)
    
    if not verify_intasend_signature(body, sig):
        return jsonify({'ok': True}), 200
    
    data = json.loads(body)
    ref = data.get('api_ref')
    status = data.get('state')
    
    if not ref or status != 'COMPLETE':
        return jsonify({'ok': True}), 200
    
    payment = query_one('SELECT * FROM payments WHERE reference = ?', (ref,))
    if not payment or payment['status'] != 'pending':
        return jsonify({'ok': True}), 200
    
    credit_wallet(payment['user_id'], payment['amount'], 'deposit', 'M-Pesa confirmed', ref=ref)
    execute('UPDATE payments SET status = ? WHERE reference = ?', ('completed', ref))
    
    return jsonify({'ok': True}), 200

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES: TASKS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    """List tasks"""
    limit = min(int(request.args.get('limit', 50)), 500)
    offset = int(request.args.get('offset', 0))
    
    tasks = query_all(
        'SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?',
        ('open', limit, offset)
    )
    
    for t in tasks:
        client = query_one('SELECT name FROM users WHERE id = ?', (t['client_id'],))
        t['client_name'] = client['name'] if client else 'Unknown'
    
    return jsonify(tasks)

@app.route('/api/tasks', methods=['POST'])
@require_auth
def create_task(user):
    """Create task"""
    if user['role'] not in ['client', 'admin']:
        return jsonify({'error': 'Clients only'}), 403
    
    data = request.json
    fee_pct = float(get_setting('platform_fee', '7'))
    fee = data['budget'] * (fee_pct / 100)
    total = data['budget'] + fee
    
    w = get_or_create_wallet(user['id'])
    if w['balance'] < total:
        return jsonify({'error': 'Insufficient balance'}), 400
    
    tid = str(uuid.uuid4())
    debit_wallet(user['id'], total, 'escrow_hold', f'Task escrow: {data["title"]}', ref=tid)
    
    execute(
        'INSERT INTO tasks (id, client_id, title, category, description, budget, deadline, status, escrow_held) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)',
        (tid, user['id'], data['title'], data.get('category', ''), data.get('description', ''), data['budget'], data.get('deadline'), 'open')
    )
    
    log_activity(user['name'], f'posted task', data['budget'], user['id'])
    return jsonify({'id': tid, 'message': 'Task posted'}), 201

@app.route('/api/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    """Get task details"""
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task:
        return jsonify({'error': 'Not found'}), 404
    
    execute('UPDATE tasks SET views = views + 1 WHERE id = ?', (task_id,))
    
    apps = query_all('SELECT * FROM applications WHERE task_id = ?', (task_id,))
    d = dict(task)
    client = query_one('SELECT name FROM users WHERE id = ?', (task['client_id'],))
    d['client_name'] = client['name'] if client else 'Unknown'
    d['applications'] = apps
    
    return jsonify(d)

@app.route('/api/tasks/<task_id>/apply', methods=['POST'])
@require_auth
def apply_task(user, task_id):
    """Apply to task"""
    if user['role'] not in ['worker', 'admin']:
        return jsonify({'error': 'Workers only'}), 403
    
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task or task['status'] != 'open':
        return jsonify({'error': 'Task unavailable'}), 400
    
    existing = query_one('SELECT id FROM applications WHERE task_id = ? AND worker_id = ?', (task_id, user['id']))
    if existing:
        return jsonify({'error': 'Already applied'}), 400
    
    data = request.json
    app_id = str(uuid.uuid4())
    
    execute(
        'INSERT INTO applications (id, task_id, worker_id, cover_letter, proposed_amount) VALUES (?, ?, ?, ?, ?)',
        (app_id, task_id, user['id'], data.get('cover_letter', ''), data.get('proposed_amount', 0))
    )
    
    execute('UPDATE tasks SET applications_count = applications_count + 1 WHERE id = ?', (task_id,))
    
    client = query_one('SELECT id FROM users WHERE id = ?', (task['client_id'],))
    if client:
        add_notification(client['id'], 'New Application!', f'{user["name"]} applied to "{task["title"]}"', 'info')
    
    return jsonify({'id': app_id}), 201

@app.route('/api/tasks/<task_id>/applications/<app_id>/accept', methods=['POST'])
@require_auth
def accept_application(user, task_id, app_id):
    """Accept application"""
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task or (task['client_id'] != user['id'] and user['role'] != 'admin'):
        return jsonify({'error': 'Not authorized'}), 403
    
    app = query_one('SELECT * FROM applications WHERE id = ?', (app_id,))
    if not app:
        return jsonify({'error': 'Not found'}), 404
    
    execute('UPDATE applications SET status = ? WHERE id = ?', ('accepted', app_id))
    execute('UPDATE tasks SET status = ?, worker_id = ? WHERE id = ?', ('in_progress', app['worker_id'], task_id))
    execute('UPDATE applications SET status = ? WHERE task_id = ? AND id != ?', ('rejected', task_id, app_id))
    
    worker = query_one('SELECT id FROM users WHERE id = ?', (app['worker_id'],))
    if worker:
        add_notification(worker['id'], f'Application Accepted!', f'You were selected for "{task["title"]}"!', 'success')
    
    return jsonify({'message': 'Application accepted'})

@app.route('/api/tasks/<task_id>/submit', methods=['POST'])
@require_auth
def submit_task(user, task_id):
    """Submit work"""
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task or task['worker_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    
    data = request.json
    app = query_one('SELECT id FROM applications WHERE task_id = ? AND worker_id = ?', (task_id, user['id']))
    
    if app:
        execute('UPDATE applications SET submission_text = ?, submitted_at = CURRENT_TIMESTAMP WHERE id = ?', (data['submission_text'], app['id']))
    
    execute('UPDATE tasks SET status = ? WHERE id = ?', ('review', task_id))
    
    client = query_one('SELECT id FROM users WHERE id = ?', (task['client_id'],))
    if client:
        add_notification(client['id'], 'Work Submitted!', f'{user["name"]} submitted work for "{task["title"]}"', 'info')
    
    return jsonify({'message': 'Submitted'})

@app.route('/api/tasks/<task_id>/approve', methods=['POST'])
@require_auth
def approve_task(user, task_id):
    """Approve and pay"""
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task or (task['client_id'] != user['id'] and user['role'] != 'admin'):
        return jsonify({'error': 'Not authorized'}), 403
    
    if task['worker_id']:
        fee_pct = float(get_setting('platform_fee', '7'))
        net = task['budget'] * (1 - fee_pct / 100)
        credit_wallet(task['worker_id'], net, 'task_payment', f'Task payment: {task["title"]}', ref=task_id)
        execute('UPDATE wallets SET total_earned = total_earned + ? WHERE user_id = ?', (net, task['worker_id']))
        
        worker = query_one('SELECT name FROM users WHERE id = ?', (task['worker_id'],))
        if worker:
            add_notification(task['worker_id'], 'Payment Received!', f'KES {net:,.0f} paid for "{task["title"]}"', 'success')
            log_activity(worker['name'], 'completed task', net, task['worker_id'])
    
    execute('UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('completed', task_id))
    return jsonify({'message': 'Approved'})

@app.route('/api/my/tasks', methods=['GET'])
@require_auth
def my_tasks(user):
    """Get my tasks"""
    if user['role'] == 'client':
        tasks = query_all('SELECT * FROM tasks WHERE client_id = ? ORDER BY created_at DESC', (user['id'],))
    else:
        apps = query_all('SELECT task_id FROM applications WHERE worker_id = ?', (user['id'],))
        tasks = [query_one('SELECT * FROM tasks WHERE id = ?', (a['task_id'],)) for a in apps]
        tasks = [t for t in tasks if t]
    
    return jsonify(tasks)

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES: PUBLIC
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/stats', methods=['GET'])
def stats():
    """Public statistics"""
    tasks = query_one('SELECT COUNT(*) as cnt FROM tasks')['cnt'] or 0
    users = query_one('SELECT COUNT(*) as cnt FROM users')['cnt'] or 0
    completed = query_one('SELECT COUNT(*) as cnt FROM tasks WHERE status = ?', ('completed',))['cnt'] or 0
    
    return jsonify({
        'total_tasks': tasks + 1800,
        'total_users': users + 12000,
        'completed_tasks': completed + 9000,
    })

@app.route('/api/notifications', methods=['GET'])
@require_auth
def get_notifications(user):
    """Get notifications"""
    notifs = query_all('SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 20', (user['id'],))
    return jsonify(notifs)

@app.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def mark_notifications_read(user):
    """Mark all as read"""
    execute('UPDATE notifications SET is_read = 1 WHERE user_id = ?', (user['id'],))
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES: ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/dashboard', methods=['GET'])
@require_admin
def admin_dashboard(user):
    """Admin dashboard"""
    users = query_one('SELECT COUNT(*) as cnt FROM users')['cnt'] or 0
    tasks = query_one('SELECT COUNT(*) as cnt FROM tasks')['cnt'] or 0
    completed = query_one('SELECT COUNT(*) as cnt FROM tasks WHERE status = ?', ('completed',))['cnt'] or 0
    
    return jsonify({'users': users, 'tasks': tasks, 'completed_tasks': completed})

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_users(user):
    """List all users"""
    users = query_all('SELECT * FROM users ORDER BY created_at DESC LIMIT 100')
    result = []
    for u in users:
        w = get_or_create_wallet(u['id'])
        d = dict(u)
        d['wallet_balance'] = w['balance']
        d.pop('hashed_password', None)
        result.append(d)
    return jsonify(result)

@app.route('/api/admin/users/<user_id>', methods=['PUT'])
@require_admin
def admin_edit_user(user, user_id):
    """Edit user"""
    data = request.json
    
    updates = {}
    if 'name' in data: updates['name'] = data['name']
    if 'role' in data: updates['role'] = data['role']
    if 'membership' in data: updates['membership'] = data['membership']
    if 'is_active' in data: updates['is_active'] = data['is_active']
    if 'is_banned' in data: updates['is_banned'] = data['is_banned']
    
    if updates:
        cols = ', '.join([f'{k} = ?' for k in updates.keys()])
        execute(f'UPDATE users SET {cols} WHERE id = ?', (*updates.values(), user_id))
    
    # Wallet adjustment
    if data.get('wallet_adjust'):
        reason = data.get('adjust_reason', 'Admin adjustment')
        if data['wallet_adjust'] > 0:
            credit_wallet(user_id, data['wallet_adjust'], 'bonus', f'Admin credit: {reason}')
        else:
            debit_wallet(user_id, abs(data['wallet_adjust']), 'fee', f'Admin debit: {reason}')
    
    return jsonify({'message': 'Updated'})

@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user, user_id):
    """Delete user"""
    execute('UPDATE users SET is_active = 0 WHERE id = ?', (user_id,))
    return jsonify({'message': 'Deleted'})

@app.route('/api/admin/tasks', methods=['GET'])
@require_admin
def admin_tasks(user):
    """List all tasks"""
    tasks = query_all('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200')
    return jsonify(tasks)

@app.route('/api/admin/tasks/<task_id>', methods=['PUT'])
@require_admin
def admin_edit_task(user, task_id):
    """Edit task"""
    data = request.json
    allowed = ['status', 'is_featured', 'budget', 'title']
    updates = {k: v for k, v in data.items() if k in allowed}
    
    if updates:
        cols = ', '.join([f'{k} = ?' for k in updates.keys()])
        execute(f'UPDATE tasks SET {cols} WHERE id = ?', (*updates.values(), task_id))
    
    return jsonify({'message': 'Updated'})

@app.route('/api/admin/tasks/<task_id>', methods=['DELETE'])
@require_admin
def admin_delete_task(user, task_id):
    """Delete task"""
    execute('DELETE FROM tasks WHERE id = ?', (task_id,))
    return jsonify({'message': 'Deleted'})

@app.route('/api/admin/payments', methods=['GET'])
@require_admin
def admin_payments(user):
    """List payments"""
    payments = query_all('SELECT * FROM payments ORDER BY created_at DESC LIMIT 200')
    result = []
    for p in payments:
        d = dict(p)
        u = query_one('SELECT name FROM users WHERE id = ?', (d['user_id'],))
        d['user_name'] = u['name'] if u else 'Unknown'
        result.append(d)
    return jsonify(result)

@app.route('/api/admin/payments/<payment_id>/approve', methods=['POST'])
@require_admin
def approve_payment(user, payment_id):
    """Approve payment"""
    payment = query_one('SELECT * FROM payments WHERE id = ?', (payment_id,))
    if not payment:
        return jsonify({'error': 'Not found'}), 404
    
    execute('UPDATE payments SET status = ? WHERE id = ?', ('completed', payment_id))
    return jsonify({'message': 'Approved'})

@app.route('/api/admin/settings', methods=['GET'])
@require_admin
def admin_get_settings(user):
    """Get all settings"""
    rows = query_all('SELECT key, value FROM site_settings')
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/admin/settings', methods=['POST'])
@require_admin
def admin_save_settings(user):
    """Save settings"""
    data = request.json
    for key, value in data.items():
        set_setting(key, str(value))
    return jsonify({'message': 'Saved'})

# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND - HTML/CSS/JS
# ═══════════════════════════════════════════════════════════════════════════════

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DTIP - Earn Smart</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #050810;
  --bg2: #0c1120;
  --border: rgba(255,255,255,0.07);
  --text: #f0f2f7;
  --text2: #8b95a8;
  --accent: #00d4aa;
  --danger: #ef4444;
  --success: #10b981;
  --warning: #f59e0b;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'DM Sans', sans-serif; line-height: 1.6; }
nav { position: sticky; top: 0; z-index: 100; background: rgba(5,8,16,0.95); backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--border); padding: 0 24px; height: 64px; display: flex; align-items: center; justify-content: space-between; }
.nav-brand { font-family: 'Syne'; font-size: 24px; font-weight: 800;
  background: linear-gradient(135deg, var(--accent), #60a5fa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; cursor: pointer; letter-spacing: -1px; }
.nav-center { display: flex; gap: 20px; }
.nav-link { color: var(--text2); cursor: pointer; padding: 8px 16px; border-radius: 8px; transition: all 0.2s; font-weight: 500; }
.nav-link:hover { background: var(--border); color: var(--text); }
.nav-link.active { color: var(--accent); background: rgba(0,212,170,0.1); }

.container { max-width: 1280px; margin: 0 auto; padding: 0 24px; }
.page { display: none; min-height: 100vh; padding: 40px 24px; }
.page.active { display: block; }

.hero { text-align: center; padding: 80px 0 60px; background: radial-gradient(ellipse at center, rgba(0,212,170,0.08) 0%, transparent 70%); }
.hero h1 { font-family: 'Syne'; font-size: clamp(32px, 6vw, 56px); font-weight: 800; margin-bottom: 20px; line-height: 1.1; }
.hero p { font-size: 18px; color: var(--text2); margin-bottom: 30px; max-width: 500px; margin-left: auto; margin-right: auto; }
.btn { display: inline-flex; align-items: center; gap: 8px; padding: 11px 24px; border-radius: 10px; font-weight: 600; cursor: pointer; border: none; transition: all 0.2s; font-family: inherit; font-size: 15px; }
.btn-primary { background: var(--accent); color: #000; }
.btn-primary:hover { background: #00b891; transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,212,170,0.3); }
.btn-secondary { background: var(--border); color: var(--text); border: 1px solid var(--border); }
.btn-secondary:hover { background: var(--border); border-color: var(--accent); }
.btn-sm { padding: 8px 16px; font-size: 14px; }
.card { background: rgba(15,20,35,0.6); border: 1px solid var(--border); border-radius: 14px; padding: 20px; margin-bottom: 16px; transition: all 0.3s; }
.card:hover { border-color: var(--accent); transform: translateY(-4px); box-shadow: 0 12px 32px rgba(0,212,170,0.1); }
.form-input { width: 100%; padding: 12px 14px; background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; color: var(--text); margin-bottom: 16px; font-family: inherit; transition: all 0.2s; }
.form-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0,212,170,0.1); }
.form-input::placeholder { color: var(--text2); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
@media (max-width: 768px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } .nav-center { display: none; } }
.modal { display: none; position: fixed; inset: 0; z-index: 200; background: rgba(0,0,0,0.8); backdrop-filter: blur(8px); align-items: center; justify-content: center; padding: 20px; }
.modal.open { display: flex; }
.modal-content { background: var(--bg2); border: 1px solid var(--border); border-radius: 18px; padding: 32px; width: 100%; max-width: 500px; max-height: 90vh; overflow-y: auto; animation: modalIn 0.3s ease; }
@keyframes modalIn { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }
.modal h2 { margin-bottom: 20px; font-family: 'Syne'; font-size: 24px; }
.toast { position: fixed; bottom: 20px; right: 20px; padding: 14px 18px; border-radius: 10px; background: rgba(16,185,129,0.15); color: #6ee7b7; z-index: 300; animation: slideIn 0.3s ease; border-left: 4px solid var(--success); }
@keyframes slideIn { from { transform: translateX(400px); opacity: 0; } to { transform: none; opacity: 1; } }
.tag { display: inline-block; padding: 5px 12px; border-radius: 100px; font-size: 12px; background: var(--border); color: var(--text2); font-weight: 500; }
.tag-accent { background: rgba(0,212,170,0.1); color: var(--accent); }
.stat-box { background: rgba(0,212,170,0.05); border: 1px solid rgba(0,212,170,0.1); border-radius: 12px; padding: 16px; text-align: center; }
.stat-value { font-family: 'Syne'; font-size: 28px; font-weight: 900; color: var(--accent); }
.stat-label { font-size: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; }
.sidebar { position: fixed; left: 0; top: 64px; width: 260px; height: calc(100vh - 64px); background: var(--bg2); border-right: 1px solid var(--border); padding: 20px 0; overflow-y: auto; transition: transform 0.3s; }
.sidebar-item { padding: 12px 20px; cursor: pointer; color: var(--text2); transition: all 0.2s; border-left: 3px solid transparent; display: flex; align-items: center; gap: 10px; }
.sidebar-item:hover { background: var(--border); color: var(--text); }
.sidebar-item.active { background: rgba(0,212,170,0.1); color: var(--accent); border-left-color: var(--accent); }
.content { margin-left: 260px; transition: margin 0.3s; }
@media (max-width: 768px) { .sidebar { transform: translateX(-100%); } .sidebar.open { transform: translateX(0); } .content { margin-left: 0; } }
.table { width: 100%; border-collapse: collapse; margin-top: 16px; }
.table th { text-align: left; padding: 12px; border-bottom: 1px solid var(--border); font-weight: 600; color: var(--text2); font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
.table td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
.empty { text-align: center; padding: 40px 20px; color: var(--text2); }
.empty-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }
.badge { display: inline-block; padding: 4px 10px; border-radius: 100px; font-size: 12px; font-weight: 600; }
.badge-success { background: rgba(16,185,129,0.1); color: var(--success); }
.badge-pending { background: rgba(245,158,11,0.1); color: var(--warning); }
.badge-error { background: rgba(239,68,68,0.1); color: var(--danger); }
</style>
</head>
<body>
<nav>
  <div class="nav-brand" onclick="navigate('home')">DTIP</div>
  <div class="nav-center">
    <div class="nav-link" onclick="navigate('home')">Home</div>
    <div class="nav-link" onclick="navigate('tasks')">Browse</div>
    <div class="nav-link" id="dash-link" style="display:none" onclick="navigate('dashboard')">Dashboard</div>
    <div class="nav-link" id="wallet-link" style="display:none" onclick="navigate('wallet')">Wallet</div>
    <div class="nav-link" id="admin-link" style="display:none" onclick="navigate('admin')">⚡Admin</div>
  </div>
  <div style="display:flex;gap:8px">
    <button class="btn btn-secondary btn-sm" id="nav-login" onclick="openModal('login')">Login</button>
    <button class="btn btn-primary btn-sm" id="nav-register" onclick="openModal('register')">Join Free</button>
    <div id="nav-user" style="display:none;gap:8px;display:flex">
      <span style="padding:8px 16px;color:var(--text2);font-size:14px" id="user-name"></span>
      <button class="btn btn-danger btn-sm" onclick="logout()">Logout</button>
    </div>
  </div>
</nav>

<!-- HOME PAGE -->
<div id="page-home" class="page active">
  <section class="hero">
    <div class="container">
      <h1>Kenya's #1 Task & Earning Platform</h1>
      <p style="color:var(--text2);margin:20px 0;font-size:18px">Complete real tasks, earn KES, withdraw instantly via M-Pesa</p>
      <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
        <button class="btn btn-primary" style="padding:12px 24px" onclick="openModal('register')">🚀 Start Earning</button>
        <button class="btn btn-secondary" style="padding:12px 24px" onclick="navigate('tasks')">Browse Tasks</button>
      </div>
    </div>
  </section>
  
  <div class="container" style="padding:60px 24px">
    <h2 style="font-family:'Syne';font-size:28px;margin-bottom:30px">Featured Tasks</h2>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px" id="featured-tasks"></div>
  </div>
</div>

<!-- TASKS PAGE -->
<div id="page-tasks" class="page">
  <div class="container">
    <h2 style="font-family:'Syne';font-size:28px;margin-bottom:20px">Browse Tasks</h2>
    <input type="text" class="form-input" placeholder="Search tasks..." id="search" oninput="filterTasks()" style="margin-bottom:20px">
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px" id="tasks-grid"></div>
  </div>
</div>

<!-- DASHBOARD PAGE -->
<div id="page-dashboard" class="page">
  <div class="container">
    <h1 style="font-family:'Syne';font-size:32px;margin-bottom:30px">Welcome Back 👋</h1>
    <div class="grid-3" style="margin-bottom:30px">
      <div class="stat-box">
        <div class="stat-value" id="dash-balance">0</div>
        <div class="stat-label">Available Balance</div>
      </div>
      <div class="stat-box">
        <div class="stat-value" id="dash-earned">0</div>
        <div class="stat-label">Total Earned</div>
      </div>
      <div class="stat-box">
        <div class="stat-value" id="dash-tasks">0</div>
        <div class="stat-label">Active Tasks</div>
      </div>
    </div>
    <button class="btn btn-primary" id="post-btn" style="display:none;margin-bottom:20px" onclick="openModal('post-task')">+ Post Task</button>
    <div id="dash-content"></div>
  </div>
</div>

<!-- WALLET PAGE -->
<div id="page-wallet" class="page">
  <div class="container">
    <h2 style="font-family:'Syne';font-size:28px;margin-bottom:30px">Wallet</h2>
    <div class="card" style="margin-bottom:20px">
      <div style="color:var(--text2);font-size:12px;margin-bottom:8px">Available Balance</div>
      <div style="font-family:'Syne';font-size:48px;font-weight:900;color:var(--accent)" id="wallet-amount">0</div>
    </div>
    <div class="grid-2" style="margin-bottom:20px">
      <button class="btn btn-primary" style="width:100%;justify-content:center" onclick="openModal('deposit')">📥 Deposit</button>
      <button class="btn btn-secondary" style="width:100%;justify-content:center" onclick="openModal('withdraw')">📤 Withdraw</button>
    </div>
    <div id="ledger-content"></div>
  </div>
</div>

<!-- ADMIN PAGE -->
<div id="page-admin" class="page">
  <div class="admin-sidebar">
    <div class="admin-item active" onclick="showAdminTab('dashboard')">📊 Dashboard</div>
    <div class="admin-item" onclick="showAdminTab('users')">👥 Users</div>
    <div class="admin-item" onclick="showAdminTab('tasks')">📋 Tasks</div>
    <div class="admin-item" onclick="showAdminTab('payments')">💳 Payments</div>
    <div class="admin-item" onclick="showAdminTab('settings')">⚙️ Settings</div>
  </div>
  <div class="admin-content" style="padding:40px 24px">
    <div id="admin-dashboard">Dashboard content</div>
    <div id="admin-users" style="display:none">Users content</div>
    <div id="admin-tasks" style="display:none">Tasks content</div>
    <div id="admin-payments" style="display:none">Payments content</div>
    <div id="admin-settings" style="display:none">Settings content</div>
  </div>
</div>

<!-- MODALS -->
<div id="login-modal" class="modal" onclick="if(event.target==this)closeModal('login')">
  <div class="modal-content">
    <h2>Login</h2>
    <input class="form-input" type="email" id="login-email" placeholder="Email">
    <input class="form-input" type="password" id="login-pass" placeholder="Password">
    <button class="btn btn-primary" style="width:100%" onclick="doLogin()">Login →</button>
    <p style="text-align:center;margin-top:16px;color:var(--text2);font-size:13px">Demo: alice@demo.com / Demo@123!</p>
  </div>
</div>

<div id="register-modal" class="modal" onclick="if(event.target==this)closeModal('register')">
  <div class="modal-content">
    <h2>Create Account</h2>
    <input class="form-input" type="text" id="reg-name" placeholder="Full Name">
    <input class="form-input" type="email" id="reg-email" placeholder="Email">
    <input class="form-input" type="tel" id="reg-phone" placeholder="+254...">
    <input class="form-input" type="password" id="reg-pass" placeholder="Password">
    <select class="form-input" id="reg-role">
      <option value="worker">Earn Money (Worker)</option>
      <option value="client">Hire Workers (Client)</option>
    </select>
    <button class="btn btn-primary" style="width:100%" onclick="doRegister()">Create Account →</button>
  </div>
</div>

<div id="post-task-modal" class="modal" onclick="if(event.target==this)closeModal('post-task')">
  <div class="modal-content">
    <h2>Post a Task</h2>
    <input class="form-input" type="text" id="task-title" placeholder="Task Title">
    <select class="form-input" id="task-cat">
      <option>Data Entry</option><option>Writing</option><option>Design</option><option>Other</option>
    </select>
    <input class="form-input" type="number" id="task-budget" placeholder="Budget (KES)">
    <input class="form-input" type="date" id="task-deadline">
    <button class="btn btn-primary" style="width:100%" onclick="postTask()">Post Task →</button>
  </div>
</div>

<div id="deposit-modal" class="modal" onclick="if(event.target==this)closeModal('deposit')">
  <div class="modal-content">
    <h2>Deposit via M-Pesa</h2>
    <input class="form-input" type="number" id="dep-amount" placeholder="Amount (KES)" min="100">
    <input class="form-input" type="tel" id="dep-phone" placeholder="M-Pesa Phone">
    <button class="btn btn-primary" style="width:100%" onclick="doDeposit()">Send STK Push →</button>
  </div>
</div>

<div id="withdraw-modal" class="modal" onclick="if(event.target==this)closeModal('withdraw')">
  <div class="modal-content">
    <h2>Withdraw to M-Pesa</h2>
    <input class="form-input" type="number" id="wit-amount" placeholder="Amount (KES)" min="100">
    <input class="form-input" type="tel" id="wit-phone" placeholder="M-Pesa Phone">
    <button class="btn btn-primary" style="width:100%" onclick="doWithdraw()">Withdraw →</button>
  </div>
</div>

<script>
const API = '';
let token = localStorage.getItem('dtip_token');
let user = null;

async function api(method, path, body) {
  const opts = {method, headers: {'Content-Type': 'application/json'}};
  if (token) opts.headers['Authorization'] = `Bearer ${token}`;
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error('Request failed');
  return res.json();
}

function toast(msg) { const el = document.createElement('div'); el.className = 'toast'; el.textContent = msg; document.body.appendChild(el); setTimeout(() => el.remove(), 3000); }
function openModal(id) { document.getElementById(id + '-modal').classList.add('open'); }
function closeModal(id) { document.getElementById(id + '-modal').classList.remove('open'); }

function navigate(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  window.scrollTo(0, 0);
  if (page === 'home') loadHome();
  if (page === 'tasks') loadTasks();
  if (page === 'dashboard') loadDashboard();
  if (page === 'wallet') loadWallet();
  if (page === 'admin') loadAdmin();
}

async function checkAuth() {
  if (!token) return;
  try { user = await api('GET', '/api/auth/me'); updateNav(); } catch (e) { token = null; localStorage.removeItem('dtip_token'); }
}

function updateNav() {
  const logged = !!user;
  document.getElementById('nav-login').style.display = logged ? 'none' : 'block';
  document.getElementById('nav-register').style.display = logged ? 'none' : 'block';
  document.getElementById('nav-user').style.display = logged ? 'flex' : 'none';
  document.getElementById('user-name').textContent = logged ? `${user.name} (${user.role})` : '';
  document.getElementById('dash-link').style.display = logged ? 'block' : 'none';
  document.getElementById('wallet-link').style.display = logged ? 'block' : 'none';
  document.getElementById('admin-link').style.display = (logged && user.role === 'admin') ? 'block' : 'none';
  document.getElementById('post-btn').style.display = (logged && ['client', 'admin'].includes(user.role)) ? 'block' : 'none';
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
  document.querySelector(`.nav-link[onclick*="${currentPage}"]`)?.classList.add('active');
}

async function doLogin() {
  try {
    const data = await api('POST', '/api/auth/login', {
      email: document.getElementById('login-email').value,
      password: document.getElementById('login-pass').value
    });
    token = data.token; localStorage.setItem('dtip_token', token);
    user = data.user; closeModal('login'); updateNav(); toast('Logged in!');
    navigate('dashboard');
  } catch (e) { toast('Login failed'); }
}

async function doRegister() {
  try {
    const data = await api('POST', '/api/auth/register', {
      name: document.getElementById('reg-name').value,
      email: document.getElementById('reg-email').value,
      phone: document.getElementById('reg-phone').value,
      password: document.getElementById('reg-pass').value,
      role: document.getElementById('reg-role').value
    });
    token = data.token; localStorage.setItem('dtip_token', token);
    user = data.user; closeModal('register'); updateNav(); toast('Account created!');
    navigate('dashboard');
  } catch (e) { toast('Registration failed'); }
}

function logout() { token = null; user = null; localStorage.removeItem('dtip_token'); updateNav(); navigate('home'); toast('Logged out'); }

async function loadHome() {
  try {
    const tasks = await api('GET', '/api/tasks?limit=6');
    document.getElementById('featured-tasks').innerHTML = tasks.map(t => `
      <div class="card" style="cursor:pointer" onclick="navigate('tasks')">
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:10px">
          <div class="tag tag-accent">${t.category}</div>
          <span class="badge badge-success">Open</span>
        </div>
        <h3 style="font-weight:700;margin-bottom:8px;font-size:16px">${t.title}</h3>
        <p style="color:var(--text2);font-size:13px;margin-bottom:12px">${t.description?.slice(0,60)}...</p>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="font-family:'Syne';font-size:20px;font-weight:900;color:var(--accent)">KES ${t.budget}</div>
          <span style="font-size:12px;color:var(--text2)">${t.applications_count || 0} applied</span>
        </div>
      </div>
    `).join('');
  } catch (e) {}
}

async function loadTasks() {
  try {
    const tasks = await api('GET', '/api/tasks');
    document.getElementById('tasks-grid').innerHTML = tasks.map(t => `
      <div class="card" style="cursor:pointer">
        <div class="tag">${t.category}</div>
        <h3 style="font-weight:700">${t.title}</h3>
        <div style="font-family:'Syne';color:var(--accent);font-weight:900">KES ${t.budget}</div>
      </div>
    `).join('');
  } catch (e) {}
}

function filterTasks() { loadTasks(); }

async function loadDashboard() {
  if (!user) return;
  try {
    const wallet = await api('GET', '/api/wallet');
    document.getElementById('dash-balance').textContent = wallet.wallet.balance;
    document.getElementById('dash-earned').textContent = wallet.wallet.total_earned;
  } catch (e) {}
}

async function loadWallet() {
  if (!user) return;
  try {
    const wallet = await api('GET', '/api/wallet');
    document.getElementById('wallet-amount').textContent = wallet.wallet.balance;
  } catch (e) {}
}

async function postTask() {
  try {
    await api('POST', '/api/tasks', {
      title: document.getElementById('task-title').value,
      category: document.getElementById('task-cat').value,
      budget: parseFloat(document.getElementById('task-budget').value),
      deadline: document.getElementById('task-deadline').value
    });
    closeModal('post-task'); toast('Task posted!');
  } catch (e) { toast('Error'); }
}

async function doDeposit() {
  try {
    await api('POST', '/api/wallet/deposit', {
      amount: parseFloat(document.getElementById('dep-amount').value),
      phone: document.getElementById('dep-phone').value
    });
    closeModal('deposit'); toast('Deposit initiated!');
    loadDashboard();
  } catch (e) { toast('Error'); }
}

async function doWithdraw() {
  try {
    await api('POST', '/api/wallet/withdraw', {
      amount: parseFloat(document.getElementById('wit-amount').value),
      phone: document.getElementById('wit-phone').value
    });
    closeModal('withdraw'); toast('Withdrawal initiated!');
  } catch (e) { toast('Error'); }
}

async function loadAdmin() {
  try {
    const stats = await api('GET', '/api/admin/dashboard');
    document.getElementById('admin-dashboard').innerHTML = `<div>Users: ${stats.users}, Tasks: ${stats.tasks}</div>`;
  } catch (e) {}
}

function showAdminTab(tab) {
  document.querySelectorAll('.admin-item').forEach(i => i.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('[id^="admin-"]').forEach(i => i.style.display = 'none');
  document.getElementById('admin-' + tab).style.display = 'block';
}

window.addEventListener('load', () => {
  checkAuth(); loadHome();
});
</script>
</body>
</html>"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()
    seed_data()
    seed_settings()
    print(f'\n🚀 DTIP v3.0 starting on port {PORT}')
    print('Demo: admin@dtip.co.ke / Admin@2024! | alice@demo.com / Demo@123!\n')
    app.run(host='0.0.0.0', port=PORT, debug=(os.getenv('ENV') != 'production'))
