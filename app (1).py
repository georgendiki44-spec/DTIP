"""
Digital Tasks & Investing Platform (DTIP)
Single-file Flask app — deploy to Railway, Render, or any Python host.
Author: DTIP / Vibe Coded
"""

import sqlite3, hashlib, hmac, secrets, json, csv, io
import datetime, uuid, functools, threading, time, random
import os, re
from flask import (Flask, request, session, redirect, url_for,
                   jsonify, g, Response, render_template_string)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
DATABASE = os.environ.get("DATABASE_URL", "dtip.db")
INTASEND_API_KEY = os.environ.get("INTASEND_API_KEY", "DEMO_KEY")
INTASEND_SECRET  = os.environ.get("INTASEND_SECRET", "DEMO_SECRET")
PLATFORM_FEE_PCT = float(os.environ.get("PLATFORM_FEE_PCT", "8"))
MAX_ACTIVE_TASKS = int(os.environ.get("MAX_ACTIVE_TASKS", "5"))
ADMIN_EMAIL      = os.environ.get("ADMIN_EMAIL", "admin@dtip.co.ke")
ADMIN_PASS       = os.environ.get("ADMIN_PASS", "Admin1234!")

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'worker',
            membership TEXT DEFAULT 'free',
            wallet_balance REAL DEFAULT 0,
            wallet_held REAL DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by TEXT,
            kyc_status TEXT DEFAULT 'none',
            is_active INTEGER DEFAULT 1,
            is_banned INTEGER DEFAULT 0,
            login_attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            avatar_color TEXT DEFAULT '#6366f1'
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT,
            description TEXT,
            steps TEXT,
            budget REAL NOT NULL,
            deadline TEXT,
            status TEXT DEFAULT 'open',
            escrow_held INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            attachment TEXT,
            worker_id TEXT,
            FOREIGN KEY(client_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            message TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(worker_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            content TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'pending',
            feedback TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            description TEXT,
            ref TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            phone TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            phone TEXT,
            mpesa_ref TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS activity_feed (
            id TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            user_id TEXT,
            is_fake INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ratings (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            from_user TEXT,
            to_user TEXT,
            score INTEGER,
            comment TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id TEXT PRIMARY KEY,
            referrer_id TEXT,
            referred_id TEXT,
            bonus_paid REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS disputes (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            raised_by TEXT,
            reason TEXT,
            status TEXT DEFAULT 'open',
            resolution TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            label TEXT,
            type TEXT DEFAULT 'text'
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        db.commit()
        _seed_admin(db)
        _seed_settings(db)
        _seed_fake_users(db)

def _seed_admin(db):
    existing = db.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if not existing:
        uid = str(uuid.uuid4())
        db.execute("""INSERT INTO users(id,email,name,password_hash,role,wallet_balance,referral_code,avatar_color)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (uid, ADMIN_EMAIL, "Admin", _hash_pw(ADMIN_PASS), "admin", 0,
                    _gen_ref(), "#ef4444"))
        db.commit()

def _seed_settings(db):
    defaults = [
        ("platform_fee_pct", str(PLATFORM_FEE_PCT), "Platform Fee (%)", "number"),
        ("max_active_tasks", str(MAX_ACTIVE_TASKS), "Max Active Tasks/Worker", "number"),
        ("withdrawal_fee_pct", "2", "Withdrawal Fee (%)", "number"),
        ("min_withdrawal", "200", "Min Withdrawal (KES)", "number"),
        ("gold_price", "500", "Gold Membership Price (KES/mo)", "number"),
        ("diamond_price", "1500", "Diamond Membership Price (KES/mo)", "number"),
        ("referral_bonus", "50", "Referral Bonus (KES)", "number"),
        ("site_name", "DTIP", "Site Name", "text"),
        ("site_tagline", "Digital Tasks & Investing Platform", "Site Tagline", "text"),
        ("maintenance_mode", "0", "Maintenance Mode", "toggle"),
        ("allow_registrations", "1", "Allow New Registrations", "toggle"),
        ("fake_activity_enabled", "1", "Show Fake Activity Feed", "toggle"),
        ("mpesa_paybill", "522522", "M-Pesa Paybill", "text"),
    ]
    for key, val, label, typ in defaults:
        db.execute("INSERT OR IGNORE INTO site_settings(key,value,label,type) VALUES(?,?,?,?)",
                   (key, val, label, typ))
    db.commit()

FAKE_NAMES = ["Wanjiku M.","Otieno K.","Akinyi A.","Kamau J.","Njoroge P.",
               "Chebet L.","Mutua S.","Waweru N.","Adhiambo F.","Kipchoge R.",
               "Muthoni G.","Omondi D.","Nyambura C.","Kariuki T.","Auma B."]

def _seed_fake_users(db):
    cats = ["Design","Data Entry","Writing","Research","Translation","Social Media","Dev"]
    for i, name in enumerate(FAKE_NAMES):
        uid = f"fake-{i}"
        ex = db.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if ex: continue
        email = f"user{i}@dtip.fake"
        colors = ["#6366f1","#8b5cf6","#ec4899","#f59e0b","#10b981","#3b82f6","#ef4444"]
        db.execute("""INSERT INTO users(id,email,phone,name,password_hash,role,wallet_balance,referral_code,avatar_color)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (uid, email, f"+254700{i:06d}", name, _hash_pw("fake"), 
                    random.choice(["worker","client"]),
                    round(random.uniform(0,5000),2), _gen_ref(), random.choice(colors)))
    db.commit()
    # Seed fake tasks
    task_titles = [
        "Design a logo for my business","Transcribe 10-minute audio","Write 5 blog posts",
        "Research competitor pricing","Translate document to Swahili","Manage Instagram page",
        "Build a simple landing page","Data entry from PDF to Excel","Video editing (3 min)",
        "SEO audit for website","Create social media posts","Proofread my thesis",
    ]
    for i, title in enumerate(task_titles):
        tid = f"fake-task-{i}"
        ex = db.execute("SELECT id FROM tasks WHERE id=?", (tid,)).fetchone()
        if ex: continue
        client = f"fake-{random.randint(0,14)}"
        budget = random.choice([200,300,500,750,1000,1500,2000])
        status = random.choice(["open","open","open","in_progress","completed"])
        db.execute("""INSERT INTO tasks(id,client_id,title,category,description,budget,status,escrow_held,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (tid, client, title, random.choice(["Design","Writing","Research","Dev","Data"]),
                    f"Looking for a professional to handle: {title}. Must deliver within deadline.",
                    budget, status, budget if status!="open" else 0,
                    (datetime.datetime.now()-datetime.timedelta(days=random.randint(0,30))).isoformat()))
    db.commit()
    # Seed activity feed
    acts = [
        ("🎉 {name} just completed a task and earned KES {amt}!", "success"),
        ("📋 New task posted: '{task}' — KES {amt}", "info"),
        ("💰 {name} deposited KES {amt} via M-Pesa", "deposit"),
        ("⭐ {name} received a 5-star rating!", "rating"),
        ("🚀 {name} joined DTIP and started their journey", "join"),
        ("✅ Task '{task}' approved and payment released", "approval"),
        ("🏆 {name} upgraded to Gold membership!", "upgrade"),
    ]
    for i in range(30):
        aid = f"fake-act-{i}"
        ex = db.execute("SELECT id FROM activity_feed WHERE id=?", (aid,)).fetchone()
        if ex: continue
        tmpl, typ = random.choice(acts)
        msg = tmpl.format(
            name=random.choice(FAKE_NAMES),
            amt=random.choice([200,500,750,1000,1500,2000,3000]),
            task=random.choice(task_titles)
        )
        created = (datetime.datetime.now()-datetime.timedelta(minutes=random.randint(1,1440))).isoformat()
        db.execute("INSERT INTO activity_feed(id,message,type,is_fake,created_at) VALUES(?,?,?,1,?)",
                   (aid, msg, typ, created))
    db.commit()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()
def _gen_ref(): return secrets.token_urlsafe(6).upper()
def _uid(): return str(uuid.uuid4())
def _now(): return datetime.datetime.now().isoformat()

def get_setting(key, default=""):
    db = get_db()
    row = db.execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def credit_wallet(db, user_id, amount, desc, ref=None):
    user = db.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,)).fetchone()
    new_bal = (user["wallet_balance"] or 0) + amount
    db.execute("UPDATE users SET wallet_balance=? WHERE id=?", (new_bal, user_id))
    db.execute("INSERT INTO ledger(id,user_id,type,amount,balance_after,description,ref) VALUES(?,?,?,?,?,?,?)",
               (_uid(), user_id, "credit", amount, new_bal, desc, ref))

def debit_wallet(db, user_id, amount, desc, ref=None):
    user = db.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,)).fetchone()
    new_bal = (user["wallet_balance"] or 0) - amount
    db.execute("UPDATE users SET wallet_balance=? WHERE id=?", (new_bal, user_id))
    db.execute("INSERT INTO ledger(id,user_id,type,amount,balance_after,description,ref) VALUES(?,?,?,?,?,?,?)",
               (_uid(), user_id, "debit", -amount, new_bal, desc, ref))

def add_activity(db, message, typ="info", user_id=None, is_fake=0):
    db.execute("INSERT INTO activity_feed(id,message,type,user_id,is_fake,created_at) VALUES(?,?,?,?,?,?)",
               (_uid(), message, typ, user_id, is_fake, _now()))

def notify(db, user_id, message):
    db.execute("INSERT INTO notifications(id,user_id,message) VALUES(?,?,?)",
               (_uid(), user_id, message))

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if "user_id" not in session: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def unread_count():
    if "user_id" not in session: return 0
    r = get_db().execute("SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0",
                         (session["user_id"],)).fetchone()
    return r["c"] if r else 0

# ─────────────────────────────────────────────
# BACKGROUND FAKE ACTIVITY
# ─────────────────────────────────────────────
_fake_msgs = [
    ("🎉 {name} just earned KES {amt} completing a task!", "success"),
    ("📋 New task posted: budget KES {amt}", "info"),
    ("💰 {name} deposited KES {amt} via M-Pesa", "deposit"),
    ("⭐ {name} received a 5-star review!", "rating"),
    ("🏆 {name} upgraded to Gold membership!", "upgrade"),
    ("✅ Task approved — KES {amt} released to worker", "approval"),
    ("🚀 Someone new just joined DTIP!", "join"),
    ("💼 {name} applied for a new task", "info"),
    ("🔥 {name} completed their 10th task this month!", "success"),
    ("📊 Platform processed KES {amt} in payouts today", "info"),
]

def _bg_fake_activity():
    while True:
        time.sleep(random.randint(25, 90))
        try:
            with app.app_context():
                db = get_db()
                if get_setting("fake_activity_enabled", "1") == "1":
                    tmpl, typ = random.choice(_fake_msgs)
                    msg = tmpl.format(name=random.choice(FAKE_NAMES),
                                      amt=random.choice([200,500,750,1000,1500,2000]))
                    add_activity(db, msg, typ, is_fake=1)
                    db.commit()
        except: pass

threading.Thread(target=_bg_fake_activity, daemon=True).start()

# ─────────────────────────────────────────────
# CSS / DESIGN SYSTEM
# ─────────────────────────────────────────────
BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,300&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07080d;--bg2:#0e1018;--bg3:#151821;--bg4:#1d2130;
  --border:#252a3a;--border2:#2f3547;
  --accent:#6c63ff;--accent2:#a78bfa;--accent3:#38bdf8;
  --green:#10b981;--red:#ef4444;--amber:#f59e0b;--pink:#ec4899;
  --text:#e8eaf0;--text2:#9299b0;--text3:#5a607a;
  --card-shadow:0 4px 24px rgba(0,0,0,.45);
  --radius:14px;--radius-sm:8px;
  font-size:15px;
}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6}
h1,h2,h3,h4,h5{font-family:'Syne',sans-serif;line-height:1.25}
a{color:var(--accent2);text-decoration:none}
a:hover{color:var(--accent)}
/* scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg2)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}

/* LAYOUT */
.app-layout{display:flex;min-height:100vh}
.sidebar{width:240px;background:var(--bg2);border-right:1px solid var(--border);
  display:flex;flex-direction:column;position:fixed;top:0;left:0;height:100vh;z-index:100;
  transition:.3s}
.sidebar-logo{padding:24px 20px 16px;border-bottom:1px solid var(--border)}
.sidebar-logo h2{font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:800;
  background:linear-gradient(135deg,var(--accent),var(--accent3));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sidebar-logo span{font-size:.7rem;color:var(--text3);letter-spacing:.08em;text-transform:uppercase}
.sidebar-nav{flex:1;padding:12px 12px;overflow-y:auto}
.nav-section{margin-bottom:8px}
.nav-label{font-size:.65rem;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);
  padding:8px 10px 4px;font-weight:600}
.nav-link{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:var(--radius-sm);
  color:var(--text2);font-size:.875rem;transition:.15s;cursor:pointer;margin-bottom:2px}
.nav-link:hover,.nav-link.active{background:var(--bg3);color:var(--text)}
.nav-link.active{color:var(--accent2)}
.nav-link .icon{width:18px;text-align:center;opacity:.8}
.sidebar-footer{padding:16px;border-top:1px solid var(--border)}
.user-chip{display:flex;align-items:center;gap:10px;padding:8px;border-radius:var(--radius-sm);
  background:var(--bg3)}
.avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:.8rem;font-weight:700;flex-shrink:0}
.avatar.lg{width:52px;height:52px;font-size:1.1rem}
.user-chip-info{min-width:0}
.user-chip-name{font-size:.82rem;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-chip-role{font-size:.7rem;color:var(--text3);text-transform:capitalize}

.main-content{margin-left:240px;flex:1;display:flex;flex-direction:column;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 28px;
  height:60px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:50}
.topbar-title{font-family:'Syne',sans-serif;font-size:1.05rem;font-weight:700;color:var(--text)}
.topbar-actions{display:flex;align-items:center;gap:12px}
.page-body{padding:28px;flex:1}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;box-shadow:var(--card-shadow)}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-title{font-size:.9rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}

/* STATS GRID */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 20px;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:-20px;right:-20px;width:80px;height:80px;
  border-radius:50%;opacity:.08}
.stat-card.green::before{background:var(--green)}
.stat-card.accent::before{background:var(--accent)}
.stat-card.amber::before{background:var(--amber)}
.stat-card.pink::before{background:var(--pink)}
.stat-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:6px}
.stat-value{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;color:var(--text)}
.stat-sub{font-size:.75rem;color:var(--text3);margin-top:4px}

/* TABLES */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{text-align:left;padding:10px 14px;color:var(--text3);font-size:.7rem;text-transform:uppercase;
  letter-spacing:.07em;border-bottom:1px solid var(--border);font-weight:600}
td{padding:12px 14px;border-bottom:1px solid var(--border);color:var(--text2);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg3)}

/* BADGES */
.badge{display:inline-flex;align-items:center;padding:2px 10px;border-radius:99px;font-size:.7rem;
  font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.badge-green{background:rgba(16,185,129,.15);color:var(--green)}
.badge-red{background:rgba(239,68,68,.15);color:var(--red)}
.badge-amber{background:rgba(245,158,11,.15);color:var(--amber)}
.badge-blue{background:rgba(56,189,248,.15);color:var(--accent3)}
.badge-purple{background:rgba(108,99,255,.15);color:var(--accent2)}
.badge-gray{background:var(--bg3);color:var(--text3)}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;border-radius:var(--radius-sm);
  font-size:.85rem;font-weight:600;cursor:pointer;border:none;transition:.15s;font-family:'DM Sans',sans-serif}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#7c74ff;color:#fff}
.btn-secondary{background:var(--bg3);color:var(--text);border:1px solid var(--border2)}
.btn-secondary:hover{background:var(--bg4)}
.btn-success{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.btn-danger{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3)}
.btn-sm{padding:5px 12px;font-size:.78rem}
.btn-lg{padding:12px 28px;font-size:.95rem;border-radius:var(--radius)}
.btn:disabled{opacity:.5;cursor:not-allowed}

/* FORMS */
.form-group{margin-bottom:16px}
.form-label{display:block;margin-bottom:6px;font-size:.82rem;color:var(--text2);font-weight:500}
.form-control{width:100%;background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius-sm);
  padding:10px 14px;color:var(--text);font-size:.875rem;font-family:'DM Sans',sans-serif;transition:.15s}
.form-control:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(108,99,255,.15)}
.form-control::placeholder{color:var(--text3)}
select.form-control option{background:var(--bg3)}
textarea.form-control{resize:vertical;min-height:90px}

/* FLASH MESSAGES */
.flash{padding:12px 18px;border-radius:var(--radius-sm);margin-bottom:16px;font-size:.875rem;font-weight:500}
.flash-success{background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.3);color:var(--green)}
.flash-error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);color:var(--red)}
.flash-info{background:rgba(108,99,255,.15);border:1px solid rgba(108,99,255,.3);color:var(--accent2)}

/* ACTIVITY FEED */
.activity-item{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)}
.activity-item:last-child{border-bottom:none}
.activity-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:7px}
.activity-dot.success{background:var(--green)}
.activity-dot.info{background:var(--accent3)}
.activity-dot.deposit{background:var(--amber)}
.activity-dot.rating{background:var(--pink)}
.activity-dot.join{background:var(--accent2)}
.activity-dot.approval{background:var(--green)}
.activity-dot.upgrade{background:var(--amber)}
.activity-msg{font-size:.82rem;color:var(--text2);flex:1}
.activity-time{font-size:.7rem;color:var(--text3);flex-shrink:0}

/* MODAL */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;
  align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:var(--radius);
  padding:28px;width:90%;max-width:520px;max-height:90vh;overflow-y:auto}
.modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.modal-title{font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700}

/* TABS */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:20px}
.tab{padding:8px 16px;font-size:.85rem;font-weight:600;color:var(--text3);cursor:pointer;
  border-bottom:2px solid transparent;transition:.15s;margin-bottom:-1px}
.tab.active,.tab:hover{color:var(--accent2)}
.tab.active{border-bottom-color:var(--accent2)}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* GRADIENT HEADER */
.page-header{background:linear-gradient(135deg,var(--bg3),var(--bg2));border:1px solid var(--border);
  border-radius:var(--radius);padding:24px 28px;margin-bottom:24px;position:relative;overflow:hidden}
.page-header::after{content:'';position:absolute;top:-40px;right:-40px;width:150px;height:150px;
  border-radius:50%;background:var(--accent);opacity:.05}
.page-header h1{font-size:1.5rem;font-weight:800;margin-bottom:4px}
.page-header p{color:var(--text2);font-size:.875rem}

/* MEMBERSHIP CARDS */
.mem-card{border-radius:var(--radius);padding:20px;text-align:center;position:relative;overflow:hidden}
.mem-card.gold{background:linear-gradient(135deg,#92400e20,#f59e0b10);border:1px solid #f59e0b40}
.mem-card.diamond{background:linear-gradient(135deg,#1e1b4b,#312e81);border:1px solid var(--accent)}
.mem-card.free{background:var(--bg3);border:1px solid var(--border)}
.mem-price{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;margin:8px 0}
.mem-price span{font-size:.9rem;font-weight:400;color:var(--text3)}

/* TASK CARD */
.task-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px;transition:.2s;cursor:pointer}
.task-card:hover{border-color:var(--border2);transform:translateY(-2px);box-shadow:var(--card-shadow)}
.task-budget{font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700;color:var(--green)}

/* GRID */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
@media(max-width:900px){.grid-2,.grid-3{grid-template-columns:1fr}.sidebar{width:60px}.sidebar-logo h2,.sidebar-logo span,.nav-label,.nav-link span,.user-chip-info{display:none}.main-content{margin-left:60px}}

/* NOTIFICATION BADGE */
.notif-badge{background:var(--red);color:#fff;border-radius:99px;font-size:.65rem;
  padding:1px 6px;font-weight:700;min-width:18px;text-align:center}

/* PILL TAG */
.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.68rem;font-weight:600;
  background:var(--bg4);color:var(--text3);margin:2px}

/* AUTH PAGES */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:var(--bg);position:relative;overflow:hidden}
.auth-wrap::before{content:'';position:absolute;top:-200px;left:-200px;width:600px;height:600px;
  border-radius:50%;background:var(--accent);opacity:.04}
.auth-wrap::after{content:'';position:absolute;bottom:-200px;right:-200px;width:500px;height:500px;
  border-radius:50%;background:var(--accent3);opacity:.03}
.auth-card{width:100%;max-width:420px;background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);padding:36px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.auth-logo{text-align:center;margin-bottom:28px}
.auth-logo h1{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;
  background:linear-gradient(135deg,var(--accent),var(--accent3));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.auth-logo p{color:var(--text3);font-size:.85rem;margin-top:4px}

/* DIVIDER */
.divider{height:1px;background:var(--border);margin:20px 0}

/* WALLET DISPLAY */
.wallet-hero{background:linear-gradient(135deg,#1e1b4b,#312e81,#0f172a);border-radius:var(--radius);
  padding:28px;position:relative;overflow:hidden;margin-bottom:24px}
.wallet-hero::before{content:'KES';position:absolute;right:20px;top:50%;transform:translateY(-50%);
  font-family:'Syne',sans-serif;font-size:6rem;font-weight:900;opacity:.04;color:#fff;
  letter-spacing:-.05em}
.wallet-bal{font-family:'Syne',sans-serif;font-size:2.5rem;font-weight:800;color:#fff}
.wallet-label{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.5);margin-bottom:8px}

/* PROGRESS BAR */
.progress{height:6px;background:var(--bg3);border-radius:99px;overflow:hidden}
.progress-bar{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent),var(--accent3))}

/* SEARCH */
.search-box{display:flex;align-items:center;gap:8px;background:var(--bg3);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:8px 14px}
.search-box input{background:none;border:none;outline:none;color:var(--text);font-size:.875rem;flex:1;font-family:'DM Sans',sans-serif}
.search-box input::placeholder{color:var(--text3)}

/* TOGGLE SWITCH */
.toggle{position:relative;display:inline-block;width:42px;height:22px}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;cursor:pointer;inset:0;background:var(--bg4);border-radius:99px;transition:.3s}
.toggle-slider:before{content:'';position:absolute;height:16px;width:16px;left:3px;bottom:3px;
  background:#fff;border-radius:50%;transition:.3s}
.toggle input:checked+.toggle-slider{background:var(--accent)}
.toggle input:checked+.toggle-slider:before{transform:translateX(20px)}

/* EMPTY STATE */
.empty-state{text-align:center;padding:48px 20px;color:var(--text3)}
.empty-state .icon{font-size:3rem;margin-bottom:12px}
.empty-state h3{font-size:1rem;color:var(--text2);margin-bottom:6px}
.empty-state p{font-size:.85rem}
"""

# ─────────────────────────────────────────────
# TEMPLATE HELPERS
# ─────────────────────────────────────────────
def render_layout(content, title="DTIP", active_nav=""):
    u = current_user()
    site_name = get_setting("site_name", "DTIP")
    unc = unread_count()
    initials = (u["name"][0] if u else "?").upper()
    role = u["role"] if u else ""
    avatar_color = u["avatar_color"] if u else "#6366f1"

    admin_nav = ""
    if role == "admin":
        admin_nav = """
        <div class="nav-section">
          <div class="nav-label">Admin</div>
          <a href="/admin" class="nav-link {an_dash}"><span class="icon">🛡️</span><span>Admin Panel</span></a>
          <a href="/admin/users" class="nav-link {an_users}"><span class="icon">👥</span><span>Manage Users</span></a>
          <a href="/admin/tasks" class="nav-link {an_tasks}"><span class="icon">📋</span><span>All Tasks</span></a>
          <a href="/admin/payments" class="nav-link {an_pay}"><span class="icon">💳</span><span>Payments</span></a>
          <a href="/admin/withdrawals" class="nav-link {an_with}"><span class="icon">💸</span><span>Withdrawals</span></a>
          <a href="/admin/settings" class="nav-link {an_set}"><span class="icon">⚙️</span><span>Settings</span></a>
          <a href="/admin/activity" class="nav-link {an_act}"><span class="icon">📢</span><span>Activity Feed</span></a>
        </div>""".format(
            an_dash="active" if active_nav=="admin" else "",
            an_users="active" if active_nav=="admin_users" else "",
            an_tasks="active" if active_nav=="admin_tasks" else "",
            an_pay="active" if active_nav=="admin_pay" else "",
            an_with="active" if active_nav=="admin_with" else "",
            an_set="active" if active_nav=="admin_set" else "",
            an_act="active" if active_nav=="admin_act" else "",
        )

    nav_links = [
        ("dashboard","🏠","Dashboard","/dashboard"),
        ("tasks","📋","Browse Tasks","/tasks"),
        ("my_tasks","📁","My Tasks","/my-tasks"),
        ("wallet","💰","Wallet","/wallet"),
        ("membership","👑","Membership","/membership"),
        ("referrals","🔗","Referrals","/referrals"),
        ("leaderboard","🏆","Leaderboard","/leaderboard"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="nav-link {"active" if active_nav==key else ""}"><span class="icon">{icon}</span><span>{label}</span></a>'
        for key,icon,label,href in nav_links
    )

    notif_badge = f'<span class="notif-badge">{unc}</span>' if unc > 0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {site_name}</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="app-layout">
  <aside class="sidebar">
    <div class="sidebar-logo">
      <h2>{site_name}</h2>
      <span>{get_setting('site_tagline','Digital Tasks & Investing Platform')}</span>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-section">
        <div class="nav-label">Main</div>
        {nav_html}
        <a href="/notifications" class="nav-link {"active" if active_nav=="notifications" else ""}"><span class="icon">🔔</span><span>Notifications {notif_badge}</span></a>
      </div>
      {admin_nav}
    </nav>
    <div class="sidebar-footer">
      <div class="user-chip">
        <div class="avatar" style="background:{avatar_color}">{initials}</div>
        <div class="user-chip-info">
          <div class="user-chip-name">{u["name"] if u else ""}</div>
          <div class="user-chip-role">{role}</div>
        </div>
      </div>
      <a href="/logout" class="btn btn-secondary btn-sm" style="width:100%;margin-top:8px;justify-content:center">Logout</a>
    </div>
  </aside>
  <div class="main-content">
    <div class="topbar">
      <span class="topbar-title">{title}</span>
      <div class="topbar-actions">
        <a href="/notifications" style="color:var(--text2);font-size:1.1rem;position:relative">
          🔔{f'<span class="notif-badge" style="position:absolute;top:-4px;right:-6px">{unc}</span>' if unc else ""}
        </a>
        <span style="font-size:.8rem;color:var(--text3)">KES <strong style="color:var(--green)">{"{:,.0f}".format(u["wallet_balance"] if u else 0)}</strong></span>
      </div>
    </div>
    <div class="page-body">
      {content}
    </div>
  </div>
</div>
<script>
document.querySelectorAll('.tab').forEach(function(tab){{
  tab.addEventListener('click',function(){{
    var target=tab.dataset.tab;
    tab.closest('.tabs').querySelectorAll('.tab').forEach(function(t){{t.classList.remove('active');}});
    tab.classList.add('active');
    document.querySelectorAll('.tab-pane').forEach(function(p){{
      p.classList.toggle('active',p.id===target);
    }});
  }});
}});
document.querySelectorAll('[data-modal]').forEach(function(btn){{
  btn.addEventListener('click',function(){{
    document.getElementById(btn.dataset.modal).classList.add('open');
  }});
}});
document.querySelectorAll('.modal-bg').forEach(function(el){{
  el.addEventListener('click',function(e){{
    if(e.target===el)el.classList.remove('open');
  }});
}});
</script>
</body></html>"""

def flash_html(msgs):
    if not msgs: return ""
    types = {"error":"error","success":"success","info":"info"}
    return "".join("<div class='flash flash-" + types.get(t, "info") + "'>"+m+"</div>" for m,t in msgs)

def _status_badge(s):
    m = {"open":"blue","in_progress":"amber","completed":"green","cancelled":"red",
         "pending":"gray","approved":"green","rejected":"red","active":"green"}
    return f'<span class="badge badge-{m.get(s,"gray")}">{s.replace("_"," ")}</span>'

def _mem_badge(m):
    colors = {"gold":"amber","diamond":"purple","free":"gray"}
    return f'<span class="badge badge-{colors.get(m,"gray")}">{m}</span>'

def _rel_time(dt_str):
    try:
        dt = datetime.datetime.fromisoformat(dt_str)
        diff = datetime.datetime.now() - dt
        s = diff.total_seconds()
        if s < 60: return "just now"
        if s < 3600: return f"{int(s//60)}m ago"
        if s < 86400: return f"{int(s//3600)}h ago"
        return f"{int(s//86400)}d ago"
    except: return ""

# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session: return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))

@app.route("/login", methods=["GET","POST"])
def login_page():
    if "user_id" in session: return redirect(url_for("dashboard"))
    errors = []
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw = request.form.get("password","")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or user["password_hash"] != _hash_pw(pw):
            errors.append("Invalid email or password.")
        elif user["is_banned"]:
            errors.append("Account suspended. Contact support.")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            db.execute("UPDATE users SET login_attempts=0 WHERE id=?", (user["id"],))
            db.commit()
            return redirect(url_for("dashboard"))
    err_html = "".join(f'<div class="flash flash-error">{e}</div>' for e in errors)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — DTIP</title><style>{BASE_CSS}</style></head><body>
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo">
      <h1>DTIP</h1>
      <p>Digital Tasks &amp; Investing Platform</p>
    </div>
    {err_html}
    <form method="POST">
      <div class="form-group">
        <label class="form-label">Email Address</label>
        <input name="email" type="email" class="form-control" placeholder="you@example.com" required>
      </div>
      <div class="form-group">
        <label class="form-label">Password</label>
        <input name="password" type="password" class="form-control" placeholder="••••••••" required>
      </div>
      <button class="btn btn-primary btn-lg" style="width:100%;margin-top:4px">Sign In</button>
    </form>
    <div class="divider"></div>
    <p style="text-align:center;font-size:.85rem;color:var(--text3)">
      Don't have an account? <a href="/register">Create one</a>
    </p>
  </div>
</div></body></html>"""

@app.route("/register", methods=["GET","POST"])
def register_page():
    errors = []
    if get_setting("allow_registrations","1") == "0":
        return redirect(url_for("login_page"))
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        phone = request.form.get("phone","").strip()
        pw = request.form.get("password","")
        ref_code = request.form.get("ref_code","").strip().upper()
        role = request.form.get("role","worker")
        if not name or not email or not pw:
            errors.append("Name, email and password required.")
        elif len(pw) < 6:
            errors.append("Password must be at least 6 characters.")
        else:
            db = get_db()
            ex = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if ex:
                errors.append("Email already registered.")
            else:
                uid = _uid()
                ref = _gen_ref()
                colors = ["#6366f1","#8b5cf6","#ec4899","#f59e0b","#10b981","#3b82f6"]
                db.execute("""INSERT INTO users(id,email,phone,name,password_hash,role,referral_code,avatar_color)
                              VALUES(?,?,?,?,?,?,?,?)""",
                           (uid, email, phone, name, _hash_pw(pw), role, ref, random.choice(colors)))
                # Handle referral
                if ref_code:
                    referrer = db.execute("SELECT id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
                    if referrer:
                        bonus = float(get_setting("referral_bonus","50"))
                        credit_wallet(db, referrer["id"], bonus, f"Referral bonus: {name} joined", uid)
                        db.execute("INSERT INTO referrals(id,referrer_id,referred_id,bonus_paid) VALUES(?,?,?,?)",
                                   (_uid(), referrer["id"], uid, bonus))
                        notify(db, referrer["id"], f"You earned KES {bonus:.0f} referral bonus from {name}!")
                db.commit()
                session["user_id"] = uid
                session["role"] = role
                add_activity(db, f"🚀 {name} just joined DTIP and started their journey!", "join", uid)
                db.commit()
                return redirect(url_for("dashboard"))
    err_html = "".join(f'<div class="flash flash-error">{e}</div>' for e in errors)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Register — DTIP</title><style>{BASE_CSS}</style></head><body>
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo"><h1>DTIP</h1><p>Create your account</p></div>
    {err_html}
    <form method="POST">
      <div class="form-group">
        <label class="form-label">Full Name</label>
        <input name="name" class="form-control" placeholder="Jane Doe" required>
      </div>
      <div class="form-group">
        <label class="form-label">Email</label>
        <input name="email" type="email" class="form-control" placeholder="you@example.com" required>
      </div>
      <div class="form-group">
        <label class="form-label">Phone (M-Pesa)</label>
        <input name="phone" class="form-control" placeholder="+254700000000">
      </div>
      <div class="form-group">
        <label class="form-label">I want to</label>
        <select name="role" class="form-control">
          <option value="worker">Find tasks & earn</option>
          <option value="client">Post tasks & hire</option>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">Password</label>
        <input name="password" type="password" class="form-control" placeholder="Min 6 characters" required>
      </div>
      <div class="form-group">
        <label class="form-label">Referral Code (optional)</label>
        <input name="ref_code" class="form-control" placeholder="XXXXXX">
      </div>
      <button class="btn btn-primary btn-lg" style="width:100%;margin-top:4px">Create Account</button>
    </form>
    <div class="divider"></div>
    <p style="text-align:center;font-size:.85rem;color:var(--text3)">
      Already have an account? <a href="/login">Sign in</a>
    </p>
  </div>
</div></body></html>"""

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    u = current_user()
    uid = u["id"]

    # Real stats
    total_users  = db.execute("SELECT COUNT(*) as c FROM users WHERE role!='admin'").fetchone()["c"]
    total_tasks  = db.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
    open_tasks   = db.execute("SELECT COUNT(*) as c FROM tasks WHERE status='open'").fetchone()["c"]
    total_payouts= db.execute("SELECT COALESCE(SUM(amount),0) as s FROM ledger WHERE type='credit' AND description LIKE '%Task approved%'").fetchone()["s"]

    my_tasks_done = db.execute("SELECT COUNT(*) as c FROM tasks WHERE worker_id=? AND status='completed'", (uid,)).fetchone()["c"]
    my_earnings   = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM ledger WHERE user_id=? AND type='credit'", (uid,)).fetchone()["s"]

    # Activity feed
    acts = db.execute("SELECT * FROM activity_feed ORDER BY created_at DESC LIMIT 18").fetchall()
    act_html = ""
    for a in acts:
        t = a["type"] or "info"
        act_html += f"""<div class="activity-item">
          <div class="activity-dot {t}"></div>
          <div class="activity-msg">{a["message"]}</div>
          <div class="activity-time">{_rel_time(a["created_at"])}</div>
        </div>"""

    # Recent tasks
    recent = db.execute("SELECT * FROM tasks WHERE status='open' ORDER BY created_at DESC LIMIT 6").fetchall()
    task_html = ""
    for t in recent:
        task_html += f"""<div class="task-card" onclick="location.href='/tasks/{t["id"]}'">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <div style="font-weight:600;font-size:.9rem;color:var(--text)">{t["title"]}</div>
              <div style="font-size:.75rem;color:var(--text3);margin-top:2px">{t["category"] or "General"}</div>
            </div>
            <div class="task-budget">KES {t["budget"]:,.0f}</div>
          </div>
          <p style="font-size:.8rem;color:var(--text2);margin-bottom:10px">{(t["description"] or "")[:90]}...</p>
          <div style="display:flex;gap:8px;align-items:center">
            {_status_badge(t["status"])}
            <span style="font-size:.72rem;color:var(--text3)">{_rel_time(t["created_at"])}</span>
          </div>
        </div>"""

    if not task_html:
        task_html = '<div class="empty-state"><div class="icon">📋</div><h3>No open tasks yet</h3><p>Check back soon</p></div>'

    content = f"""
    <div class="page-header">
      <h1>Welcome back, {u["name"].split()[0]}! 👋</h1>
      <p>Here's what's happening on DTIP today</p>
    </div>

    <div class="stats-grid">
      <div class="stat-card green">
        <div class="stat-label">Wallet Balance</div>
        <div class="stat-value">KES {u["wallet_balance"]:,.0f}</div>
        <div class="stat-sub">KES {u["wallet_held"]:,.2f} on hold</div>
      </div>
      <div class="stat-card accent">
        <div class="stat-label">My Earnings</div>
        <div class="stat-value">KES {my_earnings:,.0f}</div>
        <div class="stat-sub">Total credited</div>
      </div>
      <div class="stat-card amber">
        <div class="stat-label">Tasks Completed</div>
        <div class="stat-value">{my_tasks_done}</div>
        <div class="stat-sub">All time</div>
      </div>
      <div class="stat-card pink">
        <div class="stat-label">Membership</div>
        <div class="stat-value" style="font-size:1.2rem">{u["membership"].title()}</div>
        <div class="stat-sub"><a href="/membership" style="color:var(--accent2)">Upgrade →</a></div>
      </div>
    </div>

    <div style="margin-bottom:16px;display:flex;align-items:center;justify-content:space-between">
      <div style="font-size:.7rem;color:var(--text3);text-transform:uppercase;letter-spacing:.08em">
        Platform Stats — <span style="color:var(--green)">Live</span>
      </div>
      <div style="display:flex;gap:20px">
        <span style="font-size:.8rem;color:var(--text2)">👥 <strong>{total_users:,}</strong> members</span>
        <span style="font-size:.8rem;color:var(--text2)">📋 <strong>{open_tasks:,}</strong> open tasks</span>
        <span style="font-size:.8rem;color:var(--text2)">💰 <strong>KES {total_payouts:,.0f}</strong> paid out</span>
      </div>
    </div>

    <div class="grid-2">
      <div>
        <div style="font-family:'Syne',sans-serif;font-size:1rem;font-weight:700;margin-bottom:14px">
          🔥 Open Tasks
        </div>
        <div style="display:flex;flex-direction:column;gap:12px">{task_html}</div>
        <div style="margin-top:14px"><a href="/tasks" class="btn btn-secondary" style="width:100%;justify-content:center">View All Tasks →</a></div>
      </div>
      <div>
        <div class="card" style="height:100%">
          <div class="card-header">
            <span class="card-title">Live Activity</span>
            <span style="font-size:.7rem;color:var(--green)">● Live</span>
          </div>
          {act_html}
        </div>
      </div>
    </div>
    """
    return render_layout(content, "Dashboard", "dashboard")

# ─────────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────────
@app.route("/tasks")
@login_required
def tasks_page():
    db = get_db()
    q = request.args.get("q","")
    cat = request.args.get("cat","")
    cats = [r["category"] for r in db.execute("SELECT DISTINCT category FROM tasks WHERE category IS NOT NULL").fetchall()]

    query = "SELECT * FROM tasks WHERE status='open'"
    params = []
    if q:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if cat:
        query += " AND category=?"
        params.append(cat)
    query += " ORDER BY created_at DESC"
    tasks = db.execute(query, params).fetchall()

    cat_opts = "".join(f'<option value="{c}" {"selected" if cat==c else ""}>{c}</option>' for c in cats)
    task_cards = ""
    for t in tasks:
        task_cards += f"""<div class="task-card" onclick="location.href='/tasks/{t["id"]}'">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;color:var(--text);margin-bottom:4px">{t["title"]}</div>
              <div style="font-size:.75rem;color:var(--text3)">{t["category"] or "General"}</div>
            </div>
            <div class="task-budget" style="flex-shrink:0;margin-left:12px">KES {t["budget"]:,.0f}</div>
          </div>
          <p style="font-size:.82rem;color:var(--text2);margin-bottom:12px">{(t["description"] or "")[:120]}...</p>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            {_status_badge(t["status"])}
            <span class="pill">{t["category"] or "General"}</span>
            <span style="font-size:.72rem;color:var(--text3);margin-left:auto">{_rel_time(t["created_at"])}</span>
          </div>
        </div>"""

    if not task_cards:
        task_cards = '<div class="empty-state"><div class="icon">🔍</div><h3>No tasks found</h3><p>Try a different search</p></div>'

    content = f"""
    <div class="page-header">
      <h1>Task Marketplace</h1>
      <p>Browse available tasks and start earning</p>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
      <form method="GET" style="display:flex;gap:12px;flex:1;flex-wrap:wrap">
        <div class="search-box" style="flex:1;min-width:200px">
          <span>🔍</span>
          <input name="q" value="{q}" placeholder="Search tasks...">
        </div>
        <select name="cat" class="form-control" style="width:180px">
          <option value="">All Categories</option>
          {cat_opts}
        </select>
        <button class="btn btn-primary">Search</button>
      </form>
      <a href="/post-task" class="btn btn-success">+ Post Task</a>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px">
      {task_cards}
    </div>"""
    return render_layout(content, "Browse Tasks", "tasks")

@app.route("/tasks/<task_id>")
@login_required
def task_detail(task_id):
    db = get_db()
    t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not t: return redirect(url_for("tasks_page"))
    u = current_user()
    uid = u["id"]
    client = db.execute("SELECT * FROM users WHERE id=?", (t["client_id"],)).fetchone()
    my_app = db.execute("SELECT * FROM applications WHERE task_id=? AND worker_id=?", (task_id, uid)).fetchone()
    apps = db.execute("SELECT a.*,u.name,u.email FROM applications a JOIN users u ON a.worker_id=u.id WHERE task_id=?", (task_id,)).fetchall()
    sub = db.execute("SELECT * FROM submissions WHERE task_id=? AND worker_id=?", (task_id, uid)).fetchone()

    action_html = ""
    if uid == t["client_id"]:
        action_html = f'<a href="/manage-task/{task_id}" class="btn btn-primary">Manage Task</a>'
    elif not my_app and t["status"]=="open":
        action_html = f"""<form method="POST" action="/apply/{task_id}">
          <textarea name="message" class="form-control" placeholder="Why are you a great fit?" rows="3" required style="margin-bottom:10px"></textarea>
          <button class="btn btn-primary">Apply for Task</button>
        </form>"""
    elif my_app:
        action_html = f'<div class="flash flash-info">Application status: <strong>{my_app["status"]}</strong></div>'

    if t["worker_id"]==uid and t["status"]=="in_progress" and not sub:
        action_html += f"""<form method="POST" action="/submit/{task_id}" style="margin-top:12px">
          <textarea name="content" class="form-control" placeholder="Describe your completed work..." rows="4" required style="margin-bottom:10px"></textarea>
          <button class="btn btn-success">Submit Work</button>
        </form>"""

    content = f"""
    <div style="max-width:720px">
      <div style="margin-bottom:16px"><a href="/tasks" class="btn btn-secondary btn-sm">← Back</a></div>
      <div class="card" style="margin-bottom:16px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
          <div>
            <h1 style="font-size:1.3rem;margin-bottom:6px">{t["title"]}</h1>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              {_status_badge(t["status"])}
              <span class="pill">{t["category"] or "General"}</span>
              <span style="font-size:.75rem;color:var(--text3)">{_rel_time(t["created_at"])}</span>
            </div>
          </div>
          <div class="task-budget" style="font-size:1.5rem">KES {t["budget"]:,.0f}</div>
        </div>
        <div class="divider"></div>
        <div style="font-size:.88rem;color:var(--text2);line-height:1.7">{t["description"] or ""}</div>
        {"<div class='divider'></div><div style='font-size:.85rem;color:var(--text2)'><strong>Steps:</strong><br>" + (t["steps"] or "").replace(chr(10),"<br>") + "</div>" if t["steps"] else ""}
        <div class="divider"></div>
        <div style="display:flex;gap:24px;flex-wrap:wrap">
          <div><div style="font-size:.7rem;color:var(--text3);text-transform:uppercase">Client</div>
            <div style="font-size:.875rem;font-weight:600">{client["name"] if client else "Unknown"}</div></div>
          {"<div><div style='font-size:.7rem;color:var(--text3);text-transform:uppercase'>Deadline</div><div style='font-size:.875rem'>"+t['deadline']+"</div></div>" if t["deadline"] else ""}
          <div><div style="font-size:.7rem;color:var(--text3);text-transform:uppercase">Applications</div>
            <div style="font-size:.875rem;font-weight:600">{len(apps)}</div></div>
        </div>
      </div>
      <div class="card">{action_html}</div>
    </div>"""
    return render_layout(content, t["title"], "tasks")

@app.route("/apply/<task_id>", methods=["POST"])
@login_required
def apply_task(task_id):
    db = get_db()
    uid = session["user_id"]
    msg = request.form.get("message","")
    ex = db.execute("SELECT id FROM applications WHERE task_id=? AND worker_id=?", (task_id, uid)).fetchone()
    if not ex:
        db.execute("INSERT INTO applications(id,task_id,worker_id,message) VALUES(?,?,?,?)",
                   (_uid(), task_id, uid, msg))
        t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t:
            notify(db, t["client_id"], f"New application received for: {t['title']}")
        db.commit()
    return redirect(url_for("task_detail", task_id=task_id))

@app.route("/submit/<task_id>", methods=["POST"])
@login_required
def submit_work(task_id):
    db = get_db()
    uid = session["user_id"]
    content = request.form.get("content","")
    db.execute("INSERT INTO submissions(id,task_id,worker_id,content) VALUES(?,?,?,?)",
               (_uid(), task_id, uid, content))
    t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t:
        notify(db, t["client_id"], f"Work submitted for your task: {t['title']}")
    db.commit()
    return redirect(url_for("task_detail", task_id=task_id))

@app.route("/post-task", methods=["GET","POST"])
@login_required
def post_task():
    u = current_user()
    errors = []
    if request.method == "POST":
        title = request.form.get("title","").strip()
        budget = request.form.get("budget","0")
        cat = request.form.get("category","")
        desc = request.form.get("description","").strip()
        steps = request.form.get("steps","").strip()
        deadline = request.form.get("deadline","")
        try: budget = float(budget)
        except: budget = 0
        if not title or budget < 1:
            errors.append("Title and budget required.")
        elif u["wallet_balance"] < budget:
            errors.append(f"Insufficient wallet balance. Need KES {budget:,.0f}. <a href='/wallet'>Deposit now</a>.")
        else:
            db = get_db()
            tid = _uid()
            db.execute("""INSERT INTO tasks(id,client_id,title,category,description,steps,budget,deadline,escrow_held)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (tid, u["id"], title, cat, desc, steps, budget, deadline, budget))
            # Hold funds
            db.execute("UPDATE users SET wallet_balance=wallet_balance-?,wallet_held=wallet_held+? WHERE id=?",
                       (budget, budget, u["id"]))
            db.execute("INSERT INTO ledger(id,user_id,type,amount,balance_after,description) VALUES(?,?,?,?,?,?)",
                       (_uid(), u["id"], "hold", -budget, u["wallet_balance"]-budget, f"Escrow hold: {title}"))
            add_activity(db, f"📋 New task posted: '{title}' — KES {budget:,.0f}", "info", u["id"])
            db.commit()
            return redirect(url_for("task_detail", task_id=tid))
    err_html = "".join(f'<div class="flash flash-error">{e}</div>' for e in errors)
    content = f"""
    <div style="max-width:640px">
      <div class="page-header"><h1>Post a Task</h1><p>Funds are held in escrow until work is approved</p></div>
      {err_html}
      <div class="card">
        <form method="POST">
          <div class="form-group">
            <label class="form-label">Task Title *</label>
            <input name="title" class="form-control" placeholder="e.g. Design a logo for my startup" required>
          </div>
          <div class="grid-2">
            <div class="form-group">
              <label class="form-label">Budget (KES) *</label>
              <input name="budget" type="number" class="form-control" min="50" placeholder="500" required>
            </div>
            <div class="form-group">
              <label class="form-label">Category</label>
              <select name="category" class="form-control">
                <option>Design</option><option>Writing</option><option>Research</option>
                <option>Data Entry</option><option>Dev</option><option>Translation</option>
                <option>Social Media</option><option>Other</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Description *</label>
            <textarea name="description" class="form-control" rows="4" placeholder="Describe what you need in detail..." required></textarea>
          </div>
          <div class="form-group">
            <label class="form-label">Steps / Requirements</label>
            <textarea name="steps" class="form-control" rows="3" placeholder="Step 1: ...&#10;Step 2: ..."></textarea>
          </div>
          <div class="form-group">
            <label class="form-label">Deadline</label>
            <input name="deadline" type="date" class="form-control">
          </div>
          <div class="flash flash-info" style="margin-bottom:16px">
            ℹ️ Budget will be held in escrow and released when you approve the work.
            Your current balance: <strong>KES {u["wallet_balance"]:,.0f}</strong>
          </div>
          <button class="btn btn-primary btn-lg">Post Task & Hold Funds</button>
        </form>
      </div>
    </div>"""
    return render_layout(content, "Post Task", "tasks")

@app.route("/manage-task/<task_id>", methods=["GET","POST"])
@login_required
def manage_task(task_id):
    db = get_db()
    u = current_user()
    t = db.execute("SELECT * FROM tasks WHERE id=? AND client_id=?", (task_id, u["id"])).fetchone()
    if not t: return redirect(url_for("my_tasks"))
    apps = db.execute("SELECT a.*,u.name,u.email,u.avatar_color FROM applications a JOIN users u ON a.worker_id=u.id WHERE a.task_id=?", (task_id,)).fetchall()
    sub = db.execute("SELECT s.*,u.name FROM submissions s JOIN users u ON s.worker_id=u.id WHERE s.task_id=? ORDER BY s.created_at DESC LIMIT 1", (task_id,)).fetchone()
    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "approve_app":
            wid = request.form.get("worker_id")
            db.execute("UPDATE tasks SET status='in_progress',worker_id=? WHERE id=?", (wid, task_id))
            db.execute("UPDATE applications SET status='approved' WHERE task_id=? AND worker_id=?", (task_id, wid))
            db.execute("UPDATE applications SET status='rejected' WHERE task_id=? AND worker_id!=?", (task_id, wid))
            notify(db, wid, f"Your application was approved! Start working on: {t['title']}")
            db.commit()
            msg = "Worker approved!"
        elif action == "approve_work":
            if sub:
                fee_pct = float(get_setting("platform_fee_pct","8"))/100
                fee = t["budget"] * fee_pct
                payout = t["budget"] - fee
                credit_wallet(db, sub["worker_id"], payout, f"Task approved: {t['title']}", task_id)
                db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                db.execute("UPDATE submissions SET status='approved' WHERE id=?", (sub["id"],))
                db.execute("UPDATE users SET wallet_held=wallet_held-? WHERE id=?", (t["budget"], u["id"]))
                add_activity(db, f"✅ Task '{t['title']}' approved — KES {payout:,.0f} released to worker", "approval", u["id"])
                notify(db, sub["worker_id"], f"Work approved! KES {payout:,.0f} added to your wallet.")
                db.commit()
                msg = f"Work approved! KES {payout:,.0f} sent to worker."
        elif action == "reject_work":
            if sub:
                feedback = request.form.get("feedback","")
                db.execute("UPDATE submissions SET status='rejected',feedback=? WHERE id=?", (feedback, sub["id"]))
                notify(db, sub["worker_id"], f"Submission rejected for: {t['title']}. Feedback: {feedback}")
                db.commit()
                msg = "Submission rejected."

    app_rows = ""
    for a in apps:
        ini = a["name"][0].upper()
        app_rows += f"""<tr>
          <td><div style="display:flex;align-items:center;gap:8px">
            <div class="avatar" style="background:{a["avatar_color"]};width:28px;height:28px;font-size:.7rem">{ini}</div>
            <div><div style="font-weight:600;font-size:.85rem">{a["name"]}</div>
            <div style="font-size:.72rem;color:var(--text3)">{a["email"]}</div></div>
          </div></td>
          <td style="font-size:.82rem">{a["message"] or "—"}</td>
          <td>{_status_badge(a["status"])}</td>
          <td>{f'<form method="POST"><input type="hidden" name="action" value="approve_app"><input type="hidden" name="worker_id" value="{a["worker_id"]}"><button class="btn btn-success btn-sm">Hire</button></form>' if a["status"]=="pending" and t["status"]=="open" else ""}</td>
        </tr>"""

    sub_html = ""
    if sub:
        sub_html = f"""<div class="card" style="margin-top:16px">
          <div class="card-header"><span class="card-title">Work Submission</span>{_status_badge(sub["status"])}</div>
          <div style="font-size:.875rem;color:var(--text2);margin-bottom:16px">{sub["content"]}</div>
          {"<div style='display:flex;gap:10px'><form method='POST'><input type='hidden' name='action' value='approve_work'><button class='btn btn-success'>✅ Approve & Release Payment</button></form><form method='POST'><input type='hidden' name='action' value='reject_work'><input name='feedback' class='form-control' placeholder='Feedback...' style='display:inline;width:200px;margin-right:8px'><button class='btn btn-danger'>❌ Reject</button></form></div>" if sub["status"]=="pending" else ""}
        </div>"""

    content = f"""
    <div style="max-width:800px">
      <div style="margin-bottom:16px"><a href="/my-tasks" class="btn btn-secondary btn-sm">← My Tasks</a></div>
      {"<div class='flash flash-success'>"+msg+"</div>" if msg else ""}
      <div class="card" style="margin-bottom:16px">
        <h2 style="margin-bottom:6px">{t["title"]}</h2>
        <div style="display:flex;gap:8px;margin-bottom:12px">{_status_badge(t["status"])}<span class="task-budget">KES {t["budget"]:,.0f}</span></div>
        <p style="font-size:.875rem;color:var(--text2)">{t["description"] or ""}</p>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Applications ({len(apps)})</span></div>
        <div class="table-wrap">
          <table><thead><tr><th>Worker</th><th>Message</th><th>Status</th><th>Action</th></tr></thead>
          <tbody>{app_rows or "<tr><td colspan='4' style='text-align:center;color:var(--text3);padding:24px'>No applications yet</td></tr>"}</tbody>
          </table>
        </div>
      </div>
      {sub_html}
    </div>"""
    return render_layout(content, "Manage Task", "my_tasks")

@app.route("/my-tasks")
@login_required
def my_tasks():
    db = get_db()
    u = current_user()
    uid = u["id"]
    posted = db.execute("SELECT * FROM tasks WHERE client_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    working = db.execute("""SELECT t.*,a.status as app_status FROM tasks t
                            JOIN applications a ON a.task_id=t.id
                            WHERE a.worker_id=? ORDER BY t.created_at DESC""", (uid,)).fetchall()
    def task_rows(tasks, show_manage=False):
        if not tasks:
            return '<div class="empty-state"><div class="icon">📁</div><h3>No tasks here yet</h3></div>'
        html = ""
        for t in tasks:
            html += f"""<div class="task-card" style="margin-bottom:12px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div><div style="font-weight:600">{t["title"]}</div>
                <div style="font-size:.75rem;color:var(--text3)">{_rel_time(t["created_at"])}</div></div>
                <div style="display:flex;gap:8px;align-items:center">
                  <span class="task-budget">KES {t["budget"]:,.0f}</span>
                  {_status_badge(t["status"])}
                  {"<a href='/manage-task/"+t["id"]+"' class='btn btn-secondary btn-sm'>Manage</a>" if show_manage else "<a href='/tasks/"+t["id"]+"' class='btn btn-secondary btn-sm'>View</a>"}
                </div>
              </div>
            </div>"""
        return html

    content = f"""
    <div class="page-header"><h1>My Tasks</h1><p>Track your posted and applied tasks</p></div>
    <div class="tabs">
      <div class="tab active" data-tab="posted">Posted ({len(posted)})</div>
      <div class="tab" data-tab="working">Working On ({len(working)})</div>
    </div>
    <div id="posted" class="tab-pane active">{task_rows(posted, True)}</div>
    <div id="working" class="tab-pane">{task_rows(working, False)}</div>"""
    return render_layout(content, "My Tasks", "my_tasks")

# ─────────────────────────────────────────────
# WALLET
# ─────────────────────────────────────────────
@app.route("/wallet", methods=["GET","POST"])
@login_required
def wallet():
    db = get_db()
    u = current_user()
    uid = u["id"]
    msg = ""
    msg_type = "info"
    if request.method == "POST":
        action = request.form.get("action")
        if action == "deposit":
            amount = float(request.form.get("amount",0))
            phone = request.form.get("phone","").strip()
            if amount < 10:
                msg = "Minimum deposit is KES 10"; msg_type="error"
            else:
                # In production: call IntaSend STK push here
                # For demo: simulate successful deposit
                pay_id = _uid()
                db.execute("INSERT INTO payments(id,user_id,amount,phone,mpesa_ref,status) VALUES(?,?,?,?,?,?)",
                           (pay_id, uid, amount, phone, f"DEMO{random.randint(100000,999999)}", "completed"))
                credit_wallet(db, uid, amount, "M-Pesa deposit (demo)", pay_id)
                add_activity(db, f"💰 {u['name']} deposited KES {amount:,.0f} via M-Pesa", "deposit", uid)
                notify(db, uid, f"KES {amount:,.0f} deposited to your wallet successfully!")
                db.commit()
                msg = f"KES {amount:,.0f} credited to your wallet! (Demo mode)"
                msg_type = "success"
        elif action == "withdraw":
            amount = float(request.form.get("amount",0))
            phone = request.form.get("w_phone","").strip()
            fee_pct = float(get_setting("withdrawal_fee_pct","2"))/100
            min_w = float(get_setting("min_withdrawal","200"))
            fee = amount * fee_pct
            net = amount - fee
            u_fresh = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if amount < min_w:
                msg = f"Minimum withdrawal is KES {min_w:,.0f}"; msg_type="error"
            elif u_fresh["wallet_balance"] < amount:
                msg = "Insufficient balance"; msg_type="error"
            else:
                wid = _uid()
                db.execute("INSERT INTO withdrawals(id,user_id,amount,phone,status) VALUES(?,?,?,?,?)",
                           (wid, uid, amount, phone, "pending"))
                debit_wallet(db, uid, amount, f"Withdrawal request: KES {amount:,.0f}", wid)
                notify(db, uid, f"Withdrawal of KES {amount:,.0f} submitted. Processing...")
                db.commit()
                msg = f"Withdrawal of KES {net:,.0f} (after KES {fee:,.0f} fee) submitted for processing."
                msg_type = "success"

    u = current_user()
    ledger = db.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY created_at DESC LIMIT 25", (uid,)).fetchall()
    withdrawals = db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (uid,)).fetchall()

    ledger_rows = ""
    for l in ledger:
        color = "var(--green)" if l["type"]=="credit" else "var(--red)"
        sign = "+" if l["type"]=="credit" else "-"
        ledger_rows += f"""<tr>
          <td style="font-size:.82rem">{l["description"]}</td>
          <td style="color:{color};font-weight:600">{sign}KES {abs(l["amount"]):,.2f}</td>
          <td style="font-size:.8rem;color:var(--text3)">KES {l["balance_after"]:,.2f}</td>
          <td style="font-size:.75rem;color:var(--text3)">{_rel_time(l["created_at"])}</td>
        </tr>"""

    w_rows = ""
    for w in withdrawals:
        w_rows += f"""<tr>
          <td>KES {w["amount"]:,.2f}</td>
          <td>{w["phone"]}</td>
          <td>{_status_badge(w["status"])}</td>
          <td style="font-size:.75rem;color:var(--text3)">{_rel_time(w["created_at"])}</td>
        </tr>"""

    content = f"""
    <div class="page-header"><h1>💰 Wallet</h1><p>Manage your funds securely</p></div>
    {"<div class='flash flash-"+msg_type+"'>"+msg+"</div>" if msg else ""}
    <div class="wallet-hero">
      <div class="wallet-label">Available Balance</div>
      <div class="wallet-bal">KES {u["wallet_balance"]:,.2f}</div>
      <div style="margin-top:8px;font-size:.82rem;color:rgba(255,255,255,.5)">
        On Hold (Escrow): KES {u["wallet_held"]:,.2f}
      </div>
    </div>
    <div class="grid-2" style="margin-bottom:24px">
      <div class="card">
        <div class="card-header"><span class="card-title">Deposit via M-Pesa</span></div>
        <div class="flash flash-info" style="margin-bottom:14px">
          📱 Demo mode — deposits are simulated. In production, M-Pesa STK push will be triggered.
        </div>
        <form method="POST">
          <input type="hidden" name="action" value="deposit">
          <div class="form-group">
            <label class="form-label">Amount (KES)</label>
            <input name="amount" type="number" class="form-control" min="10" placeholder="500" required>
          </div>
          <div class="form-group">
            <label class="form-label">M-Pesa Phone</label>
            <input name="phone" class="form-control" value="{u["phone"] or ""}" placeholder="+254700000000">
          </div>
          <button class="btn btn-primary">Deposit via M-Pesa</button>
        </form>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Withdraw Funds</span></div>
        <div style="margin-bottom:12px;font-size:.82rem;color:var(--text3)">
          Fee: {get_setting("withdrawal_fee_pct","2")}% · Min: KES {get_setting("min_withdrawal","200")}
        </div>
        <form method="POST">
          <input type="hidden" name="action" value="withdraw">
          <div class="form-group">
            <label class="form-label">Amount (KES)</label>
            <input name="amount" type="number" class="form-control" min="{get_setting("min_withdrawal","200")}" placeholder="500" required>
          </div>
          <div class="form-group">
            <label class="form-label">M-Pesa Phone</label>
            <input name="w_phone" class="form-control" value="{u["phone"] or ""}" placeholder="+254700000000" required>
          </div>
          <button class="btn btn-secondary">Request Withdrawal</button>
        </form>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Transaction History</span>
        <a href="/wallet/export" class="btn btn-secondary btn-sm">📥 Export CSV</a>
      </div>
      <div class="table-wrap">
        <table><thead><tr><th>Description</th><th>Amount</th><th>Balance</th><th>Time</th></tr></thead>
        <tbody>{ledger_rows or "<tr><td colspan='4' style='text-align:center;padding:24px;color:var(--text3)'>No transactions yet</td></tr>"}</tbody>
        </table>
      </div>
    </div>
    {"<div class='card' style='margin-top:16px'><div class='card-header'><span class='card-title'>Withdrawals</span></div><div class='table-wrap'><table><thead><tr><th>Amount</th><th>Phone</th><th>Status</th><th>Time</th></tr></thead><tbody>"+w_rows+"</tbody></table></div></div>" if w_rows else ""}"""
    return render_layout(content, "Wallet", "wallet")

@app.route("/wallet/export")
@login_required
def wallet_export():
    db = get_db()
    uid = session["user_id"]
    ledger = db.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Date","Type","Amount","Balance After","Description","Ref"])
    for l in ledger:
        w.writerow([l["created_at"],l["type"],l["amount"],l["balance_after"],l["description"],l["ref"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment;filename=dtip_statement.csv"})

# ─────────────────────────────────────────────
# M-PESA WEBHOOK (IntaSend)
# ─────────────────────────────────────────────
@app.route("/payments/stk-push", methods=["POST"])
def stk_push():
    data = request.json or {}
    user_id = data.get("user_id")
    amount = data.get("amount",0)
    phone = data.get("phone","")
    if not user_id or not amount:
        return jsonify({"error":"Missing fields"}), 400
    with app.app_context():
        db = get_db()
        pay_id = _uid()
        db.execute("INSERT INTO payments(id,user_id,amount,phone,status) VALUES(?,?,?,?,?)",
                   (pay_id, user_id, amount, phone, "pending"))
        db.commit()
    # In production: call IntaSend API here
    return jsonify({"success":True,"payment_id":pay_id,"message":"STK push initiated (demo)"})

@app.route("/payments/callback", methods=["POST"])
def payment_callback():
    data = request.json or {}
    ref = data.get("reference","")
    status = data.get("status","")
    if status.lower() == "complete":
        with app.app_context():
            db = get_db()
            pay = db.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (ref,)).fetchone()
            if pay:
                db.execute("UPDATE payments SET status='completed',mpesa_ref=? WHERE id=?",
                           (data.get("mpesa_ref",""), ref))
                credit_wallet(db, pay["user_id"], pay["amount"], "M-Pesa deposit", ref)
                db.commit()
    return jsonify({"status":"received"})

# ─────────────────────────────────────────────
# MEMBERSHIP
# ─────────────────────────────────────────────
@app.route("/membership", methods=["GET","POST"])
@login_required
def membership():
    u = current_user()
    db = get_db()
    msg = ""
    if request.method == "POST":
        plan = request.form.get("plan")
        prices = {"gold": float(get_setting("gold_price","500")),
                  "diamond": float(get_setting("diamond_price","1500"))}
        price = prices.get(plan, 0)
        u_fresh = db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if u_fresh["wallet_balance"] < price:
            msg = f"error:Insufficient balance. Need KES {price:,.0f}."
        elif plan in prices:
            debit_wallet(db, u["id"], price, f"{plan.title()} membership subscription", None)
            db.execute("UPDATE users SET membership=? WHERE id=?", (plan, u["id"]))
            add_activity(db, f"🏆 {u['name']} upgraded to {plan.title()} membership!", "upgrade", u["id"])
            notify(db, u["id"], f"Welcome to {plan.title()} membership! Enjoy your benefits.")
            db.commit()
            msg = f"success:Successfully upgraded to {plan.title()} membership!"

    u = current_user()
    msg_html = ""
    if msg:
        t, m = msg.split(":",1)
        msg_html = f'<div class="flash flash-{t}">{m}</div>'

    gold_p = get_setting("gold_price","500")
    dia_p = get_setting("diamond_price","1500")

    content = f"""
    <div class="page-header"><h1>👑 Membership Plans</h1><p>Unlock premium features and save on fees</p></div>
    {msg_html}
    <div class="grid-3" style="margin-bottom:24px">
      <div class="mem-card free">
        <div style="font-size:1.5rem;margin-bottom:8px">🆓</div>
        <h3 style="font-family:'Syne',sans-serif">Free</h3>
        <div class="mem-price">KES 0<span>/mo</span></div>
        <div class="divider"></div>
        <ul style="list-style:none;font-size:.85rem;color:var(--text2);text-align:left;margin-bottom:16px">
          <li style="padding:4px 0">✓ Up to {get_setting("max_active_tasks","5")} active tasks</li>
          <li style="padding:4px 0">✓ Standard platform fee ({get_setting("platform_fee_pct","8")}%)</li>
          <li style="padding:4px 0">✓ Basic support</li>
          <li style="padding:4px 0;color:var(--text3)">✗ Priority listings</li>
          <li style="padding:4px 0;color:var(--text3)">✗ Advanced analytics</li>
        </ul>
        {"<span class='badge badge-gray'>Current Plan</span>" if u["membership"]=="free" else ""}
      </div>
      <div class="mem-card gold">
        <div style="font-size:1.5rem;margin-bottom:8px">🥇</div>
        <h3 style="font-family:'Syne',sans-serif;color:var(--amber)">Gold</h3>
        <div class="mem-price" style="color:var(--amber)">KES {gold_p}<span>/mo</span></div>
        <div class="divider"></div>
        <ul style="list-style:none;font-size:.85rem;color:var(--text2);text-align:left;margin-bottom:16px">
          <li style="padding:4px 0">✓ Up to 10 active tasks</li>
          <li style="padding:4px 0">✓ Reduced fee (5%)</li>
          <li style="padding:4px 0">✓ Priority support</li>
          <li style="padding:4px 0">✓ Featured profile badge</li>
          <li style="padding:4px 0;color:var(--text3)">✗ Advanced analytics</li>
        </ul>
        {"<span class='badge badge-amber'>Current Plan</span>" if u["membership"]=="gold" else f'<form method="POST"><input type="hidden" name="plan" value="gold"><button class="btn btn-primary" style="width:100%">Upgrade to Gold</button></form>'}
      </div>
      <div class="mem-card diamond">
        <div style="font-size:1.5rem;margin-bottom:8px">💎</div>
        <h3 style="font-family:'Syne',sans-serif;color:var(--accent2)">Diamond</h3>
        <div class="mem-price">KES {dia_p}<span>/mo</span></div>
        <div class="divider"></div>
        <ul style="list-style:none;font-size:.85rem;color:var(--text2);text-align:left;margin-bottom:16px">
          <li style="padding:4px 0">✓ Unlimited active tasks</li>
          <li style="padding:4px 0">✓ Lowest fee (3%)</li>
          <li style="padding:4px 0">✓ VIP support (24/7)</li>
          <li style="padding:4px 0">✓ Featured profile + badge</li>
          <li style="padding:4px 0">✓ Advanced analytics</li>
        </ul>
        {"<span class='badge badge-purple'>Current Plan</span>" if u["membership"]=="diamond" else f'<form method="POST"><input type="hidden" name="plan" value="diamond"><button class="btn btn-primary" style="width:100%">Upgrade to Diamond</button></form>'}
      </div>
    </div>
    <div class="card">
      <div style="font-size:.8rem;color:var(--text3);line-height:1.8">
        ⚠️ <strong>Disclosure:</strong> Membership fees are for access to platform services only. They do not represent investments and carry no guaranteed returns or profits. Benefits are service-based (lower fees, higher limits, priority support). By subscribing, you agree to our Terms of Service.
      </div>
    </div>"""
    return render_layout(content, "Membership", "membership")

# ─────────────────────────────────────────────
# REFERRALS
# ─────────────────────────────────────────────
@app.route("/referrals")
@login_required
def referrals():
    db = get_db()
    u = current_user()
    refs = db.execute("""SELECT r.*,u.name,u.created_at as joined FROM referrals r
                         JOIN users u ON r.referred_id=u.id
                         WHERE r.referrer_id=? ORDER BY r.created_at DESC""", (u["id"],)).fetchall()
    total_bonus = sum(r["bonus_paid"] for r in refs)
    ref_link = f"{request.host_url}register?ref={u['referral_code']}"
    rows = "".join(f"""<tr>
      <td style="font-weight:500">{r["name"]}</td>
      <td style="color:var(--green)">KES {r["bonus_paid"]:,.0f}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(r["joined"])}</td>
    </tr>""" for r in refs)
    content = f"""
    <div class="page-header"><h1>🔗 Referrals</h1><p>Invite friends and earn KES {get_setting("referral_bonus","50")} per signup</p></div>
    <div class="stats-grid">
      <div class="stat-card green"><div class="stat-label">Total Referrals</div><div class="stat-value">{len(refs)}</div></div>
      <div class="stat-card accent"><div class="stat-label">Bonus Earned</div><div class="stat-value">KES {total_bonus:,.0f}</div></div>
      <div class="stat-card amber"><div class="stat-label">Per Referral</div><div class="stat-value">KES {get_setting("referral_bonus","50")}</div></div>
    </div>
    <div class="card" style="margin-bottom:20px">
      <div class="card-title" style="margin-bottom:12px">Your Referral Link</div>
      <div style="display:flex;gap:10px;align-items:center">
        <div class="form-control" style="flex:1;font-family:monospace;font-size:.8rem;overflow:hidden;text-overflow:ellipsis">{ref_link}</div>
        <button onclick="navigator.clipboard.writeText('{ref_link}');this.textContent='Copied!'" class="btn btn-primary">Copy</button>
      </div>
      <div style="margin-top:8px;font-size:.78rem;color:var(--text3)">Code: <strong style="color:var(--accent2)">{u["referral_code"]}</strong></div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Referral History</span></div>
      <div class="table-wrap">
        <table><thead><tr><th>Name</th><th>Bonus Paid</th><th>Joined</th></tr></thead>
        <tbody>{rows or "<tr><td colspan='3' style='text-align:center;padding:24px;color:var(--text3)'>No referrals yet — share your link!</td></tr>"}</tbody>
        </table>
      </div>
    </div>"""
    return render_layout(content, "Referrals", "referrals")

# ─────────────────────────────────────────────
# LEADERBOARD
# ─────────────────────────────────────────────
@app.route("/leaderboard")
@login_required
def leaderboard():
    db = get_db()
    top = db.execute("""SELECT u.name,u.avatar_color,u.membership,
                        COUNT(t.id) as tasks_done,
                        COALESCE(SUM(l.amount),0) as total_earned
                        FROM users u
                        LEFT JOIN tasks t ON t.worker_id=u.id AND t.status='completed'
                        LEFT JOIN ledger l ON l.user_id=u.id AND l.type='credit'
                        WHERE u.role!='admin' AND u.is_active=1
                        GROUP BY u.id ORDER BY total_earned DESC LIMIT 20""").fetchall()
    rows = ""
    for i, u in enumerate(top):
        medal = ["🥇","🥈","🥉"][i] if i < 3 else f"#{i+1}"
        ini = u["name"][0].upper()
        rows += f"""<tr>
          <td style="font-family:'Syne',sans-serif;font-size:1.1rem">{medal}</td>
          <td><div style="display:flex;align-items:center;gap:8px">
            <div class="avatar" style="background:{u["avatar_color"]}">{ini}</div>
            <span style="font-weight:600">{u["name"]}</span>
            {_mem_badge(u["membership"])}
          </div></td>
          <td style="color:var(--green);font-weight:600">KES {u["total_earned"]:,.0f}</td>
          <td style="font-weight:600">{u["tasks_done"]}</td>
        </tr>"""

    content = f"""
    <div class="page-header"><h1>🏆 Leaderboard</h1><p>Top earners on DTIP — all metrics are real</p></div>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>Rank</th><th>User</th><th>Total Earned</th><th>Tasks Done</th></tr></thead>
        <tbody>{rows or "<tr><td colspan='4' style='text-align:center;padding:24px;color:var(--text3)'>No data yet</td></tr>"}</tbody>
        </table>
      </div>
    </div>"""
    return render_layout(content, "Leaderboard", "leaderboard")

# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────
@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    uid = session["user_id"]
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
    db.commit()
    notifs = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (uid,)).fetchall()
    rows = "".join(f"""<div style="padding:14px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:.875rem;color:var(--text)">{n["message"]}</div>
      <div style="font-size:.72rem;color:var(--text3);margin-top:4px">{_rel_time(n["created_at"])}</div>
    </div>""" for n in notifs)
    content = f"""
    <div class="page-header"><h1>🔔 Notifications</h1></div>
    <div class="card">{rows or "<div class='empty-state'><div class='icon'>🔔</div><h3>No notifications</h3></div>"}</div>"""
    return render_layout(content, "Notifications", "notifications")

# ─────────────────────────────────────────────
# ADMIN ROUTES
# ─────────────────────────────────────────────
@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    db = get_db()
    total_users = db.execute("SELECT COUNT(*) as c FROM users WHERE role!='admin'").fetchone()["c"]
    total_tasks = db.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
    total_revenue = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM payments WHERE status='completed'").fetchone()["s"]
    pending_w = db.execute("SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'").fetchone()["c"]
    open_disputes = db.execute("SELECT COUNT(*) as c FROM disputes WHERE status='open'").fetchone()["c"]
    recent_users = db.execute("SELECT * FROM users WHERE role!='admin' ORDER BY created_at DESC LIMIT 5").fetchall()

    user_rows = "".join(f"""<tr>
      <td><div style="display:flex;align-items:center;gap:8px">
        <div class="avatar" style="background:{u["avatar_color"]};width:28px;height:28px;font-size:.7rem">{u["name"][0].upper()}</div>
        <div><div style="font-size:.85rem;font-weight:600">{u["name"]}</div>
        <div style="font-size:.72rem;color:var(--text3)">{u["email"]}</div></div>
      </div></td>
      <td>{_status_badge(u["role"])}</td>
      <td style="color:var(--green)">KES {u["wallet_balance"]:,.0f}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(u["created_at"])}</td>
    </tr>""" for u in recent_users)

    content = f"""
    <div class="page-header">
      <h1>🛡️ Admin Panel</h1>
      <p>Full control over the DTIP platform</p>
    </div>
    <div class="stats-grid">
      <div class="stat-card accent"><div class="stat-label">Total Users</div><div class="stat-value">{total_users:,}</div></div>
      <div class="stat-card green"><div class="stat-label">Total Revenue</div><div class="stat-value">KES {total_revenue:,.0f}</div></div>
      <div class="stat-card amber"><div class="stat-label">Total Tasks</div><div class="stat-value">{total_tasks:,}</div></div>
      <div class="stat-card pink"><div class="stat-label">Pending Withdrawals</div><div class="stat-value">{pending_w}</div>
        {"<div class='stat-sub'><a href='/admin/withdrawals' style='color:var(--pink)'>Review →</a></div>" if pending_w else ""}</div>
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Recent Users</span>
          <a href="/admin/users" class="btn btn-secondary btn-sm">View All</a></div>
        <table><thead><tr><th>User</th><th>Role</th><th>Balance</th><th>Joined</th></tr></thead>
        <tbody>{user_rows}</tbody></table>
      </div>
      <div class="card">
        <div class="card-title" style="margin-bottom:16px">Quick Actions</div>
        <div style="display:flex;flex-direction:column;gap:10px">
          <a href="/admin/users" class="btn btn-secondary">👥 Manage Users</a>
          <a href="/admin/tasks" class="btn btn-secondary">📋 Manage Tasks</a>
          <a href="/admin/withdrawals" class="btn btn-secondary">💸 Process Withdrawals {f'<span class="notif-badge">{pending_w}</span>' if pending_w else ""}</a>
          <a href="/admin/settings" class="btn btn-secondary">⚙️ Site Settings</a>
          <a href="/admin/activity" class="btn btn-secondary">📢 Activity Feed</a>
          <a href="/admin/fake-activity" class="btn btn-secondary">🤖 Post Fake Activity</a>
        </div>
      </div>
    </div>"""
    return render_layout(content, "Admin Panel", "admin")

@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    db = get_db()
    q = request.args.get("q","")
    query = "SELECT * FROM users WHERE 1=1"
    params = []
    if q:
        query += " AND (name LIKE ? OR email LIKE ?)"
        params += [f"%{q}%",f"%{q}%"]
    query += " ORDER BY created_at DESC"
    users = db.execute(query, params).fetchall()

    rows = "".join(f"""<tr>
      <td><div style="display:flex;align-items:center;gap:8px">
        <div class="avatar" style="background:{u["avatar_color"]};width:28px;height:28px;font-size:.7rem">{u["name"][0].upper()}</div>
        <div><div style="font-size:.85rem;font-weight:600">{u["name"]}</div>
        <div style="font-size:.72rem;color:var(--text3)">{u["email"]}</div></div>
      </div></td>
      <td>{u["phone"] or "—"}</td>
      <td>{_status_badge(u["role"])}</td>
      <td>{_mem_badge(u["membership"])}</td>
      <td style="color:var(--green)">KES {u["wallet_balance"]:,.2f}</td>
      <td>{"<span class='badge badge-red'>Banned</span>" if u["is_banned"] else "<span class='badge badge-green'>Active</span>"}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(u["created_at"])}</td>
      <td>
        <a href="/admin/edit-user/{u["id"]}" class="btn btn-secondary btn-sm">Edit</a>
        <a href="/admin/credit-user/{u["id"]}" class="btn btn-success btn-sm">+ Credit</a>
      </td>
    </tr>""" for u in users)

    content = f"""
    <div class="page-header"><h1>👥 Manage Users</h1><p>{len(users)} users total</p></div>
    <form method="GET" style="margin-bottom:16px;display:flex;gap:10px">
      <div class="search-box" style="flex:1"><span>🔍</span><input name="q" value="{q}" placeholder="Search by name or email..."></div>
      <button class="btn btn-primary">Search</button>
    </form>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>User</th><th>Phone</th><th>Role</th><th>Plan</th><th>Balance</th><th>Status</th><th>Joined</th><th>Actions</th></tr></thead>
        <tbody>{rows}</tbody></table>
      </div>
    </div>"""
    return render_layout(content, "Manage Users", "admin_users")

@app.route("/admin/edit-user/<uid>", methods=["GET","POST"])
@login_required
@admin_required
def admin_edit_user(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u: return redirect(url_for("admin_users"))
    msg = ""
    if request.method == "POST":
        name = request.form.get("name",u["name"])
        email = request.form.get("email",u["email"])
        role = request.form.get("role",u["role"])
        membership = request.form.get("membership",u["membership"])
        is_banned = 1 if request.form.get("is_banned") else 0
        is_active = 1 if request.form.get("is_active") else 0
        new_pw = request.form.get("new_password","")
        db.execute("UPDATE users SET name=?,email=?,role=?,membership=?,is_banned=?,is_active=? WHERE id=?",
                   (name,email,role,membership,is_banned,is_active,uid))
        if new_pw:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(new_pw), uid))
        db.commit()
        msg = "User updated successfully!"
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    content = f"""
    <div style="max-width:600px">
      <div style="margin-bottom:16px"><a href="/admin/users" class="btn btn-secondary btn-sm">← Back</a></div>
      {"<div class='flash flash-success'>"+msg+"</div>" if msg else ""}
      <div class="card">
        <div class="card-header"><span class="card-title">Edit User: {u["name"]}</span></div>
        <form method="POST">
          <div class="form-group"><label class="form-label">Full Name</label>
            <input name="name" class="form-control" value="{u["name"]}"></div>
          <div class="form-group"><label class="form-label">Email</label>
            <input name="email" class="form-control" value="{u["email"]}"></div>
          <div class="grid-2">
            <div class="form-group"><label class="form-label">Role</label>
              <select name="role" class="form-control">
                {"".join(f'<option value="{r}" {"selected" if u["role"]==r else ""}>{r.title()}</option>' for r in ["worker","client","admin"])}
              </select></div>
            <div class="form-group"><label class="form-label">Membership</label>
              <select name="membership" class="form-control">
                {"".join(f'<option value="{m}" {"selected" if u["membership"]==m else ""}>{m.title()}</option>' for m in ["free","gold","diamond"])}
              </select></div>
          </div>
          <div class="form-group"><label class="form-label">New Password (leave blank to keep)</label>
            <input name="new_password" type="password" class="form-control" placeholder="Leave blank to keep current"></div>
          <div style="display:flex;gap:20px;margin-bottom:16px">
            <label style="display:flex;align-items:center;gap:8px;font-size:.875rem;cursor:pointer">
              <input type="checkbox" name="is_active" {"checked" if u["is_active"] else ""}> Active</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:.875rem;cursor:pointer">
              <input type="checkbox" name="is_banned" {"checked" if u["is_banned"] else ""}> Banned</label>
          </div>
          <button class="btn btn-primary">Save Changes</button>
          <a href="/admin/credit-user/{uid}" class="btn btn-success" style="margin-left:10px">+ Credit Wallet</a>
        </form>
      </div>
    </div>"""
    return render_layout(content, f"Edit {u['name']}", "admin_users")

@app.route("/admin/credit-user/<uid>", methods=["GET","POST"])
@login_required
@admin_required
def admin_credit_user(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    msg = ""
    if request.method == "POST":
        amount = float(request.form.get("amount",0))
        desc = request.form.get("desc","Admin credit")
        typ = request.form.get("type","credit")
        if typ == "credit":
            credit_wallet(db, uid, amount, f"Admin credit: {desc}")
        else:
            debit_wallet(db, uid, amount, f"Admin debit: {desc}")
        db.commit()
        msg = f"KES {amount:,.0f} {'credited to' if typ=='credit' else 'debited from'} wallet."
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    content = f"""
    <div style="max-width:480px">
      <div style="margin-bottom:16px"><a href="/admin/edit-user/{uid}" class="btn btn-secondary btn-sm">← Back</a></div>
      {"<div class='flash flash-success'>"+msg+"</div>" if msg else ""}
      <div class="card">
        <div class="card-header"><span class="card-title">Credit/Debit Wallet: {u["name"]}</span></div>
        <div style="margin-bottom:16px">Current Balance: <strong style="color:var(--green)">KES {u["wallet_balance"]:,.2f}</strong></div>
        <form method="POST">
          <div class="form-group"><label class="form-label">Type</label>
            <select name="type" class="form-control">
              <option value="credit">Credit (Add)</option>
              <option value="debit">Debit (Remove)</option>
            </select></div>
          <div class="form-group"><label class="form-label">Amount (KES)</label>
            <input name="amount" type="number" class="form-control" min="1" required></div>
          <div class="form-group"><label class="form-label">Description</label>
            <input name="desc" class="form-control" value="Admin adjustment"></div>
          <button class="btn btn-primary">Apply</button>
        </form>
      </div>
    </div>"""
    return render_layout(content, "Credit User", "admin_users")

@app.route("/admin/tasks")
@login_required
@admin_required
def admin_tasks():
    db = get_db()
    tasks = db.execute("""SELECT t.*,u.name as client_name FROM tasks t
                          JOIN users u ON t.client_id=u.id
                          ORDER BY t.created_at DESC""").fetchall()
    rows = "".join(f"""<tr>
      <td style="font-weight:500;max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{t["title"]}</td>
      <td style="font-size:.82rem">{t["client_name"]}</td>
      <td style="color:var(--green)">KES {t["budget"]:,.0f}</td>
      <td>{_status_badge(t["status"])}</td>
      <td><span class="pill">{t["category"] or "General"}</span></td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(t["created_at"])}</td>
      <td>
        <a href="/tasks/{t["id"]}" class="btn btn-secondary btn-sm">View</a>
        <a href="/admin/edit-task/{t["id"]}" class="btn btn-secondary btn-sm">Edit</a>
      </td>
    </tr>""" for t in tasks)
    content = f"""
    <div class="page-header"><h1>📋 All Tasks ({len(tasks)})</h1></div>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>Title</th><th>Client</th><th>Budget</th><th>Status</th><th>Category</th><th>Posted</th><th>Actions</th></tr></thead>
        <tbody>{rows}</tbody></table>
      </div>
    </div>"""
    return render_layout(content, "All Tasks", "admin_tasks")

@app.route("/admin/edit-task/<tid>", methods=["GET","POST"])
@login_required
@admin_required
def admin_edit_task(tid):
    db = get_db()
    t = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    msg = ""
    if request.method == "POST":
        title = request.form.get("title",t["title"])
        status = request.form.get("status",t["status"])
        budget = float(request.form.get("budget",t["budget"]))
        cat = request.form.get("category",t["category"])
        desc = request.form.get("description",t["description"])
        db.execute("UPDATE tasks SET title=?,status=?,budget=?,category=?,description=? WHERE id=?",
                   (title,status,budget,cat,desc,tid))
        db.commit()
        msg = "Task updated!"
    t = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    content = f"""
    <div style="max-width:600px">
      <div style="margin-bottom:16px"><a href="/admin/tasks" class="btn btn-secondary btn-sm">← Back</a></div>
      {"<div class='flash flash-success'>"+msg+"</div>" if msg else ""}
      <div class="card">
        <div class="card-header"><span class="card-title">Edit Task</span></div>
        <form method="POST">
          <div class="form-group"><label class="form-label">Title</label>
            <input name="title" class="form-control" value="{t["title"]}"></div>
          <div class="grid-2">
            <div class="form-group"><label class="form-label">Status</label>
              <select name="status" class="form-control">
                {"".join(f'<option value="{s}" {"selected" if t["status"]==s else ""}>{s.replace("_"," ").title()}</option>' for s in ["open","in_progress","completed","cancelled"])}
              </select></div>
            <div class="form-group"><label class="form-label">Budget (KES)</label>
              <input name="budget" type="number" class="form-control" value="{t["budget"]}"></div>
          </div>
          <div class="form-group"><label class="form-label">Category</label>
            <input name="category" class="form-control" value="{t["category"] or ""}"></div>
          <div class="form-group"><label class="form-label">Description</label>
            <textarea name="description" class="form-control" rows="4">{t["description"] or ""}</textarea></div>
          <button class="btn btn-primary">Save Changes</button>
        </form>
      </div>
    </div>"""
    return render_layout(content, "Edit Task", "admin_tasks")

@app.route("/admin/payments")
@login_required
@admin_required
def admin_payments():
    db = get_db()
    payments = db.execute("""SELECT p.*,u.name,u.email FROM payments p
                             JOIN users u ON p.user_id=u.id
                             ORDER BY p.created_at DESC""").fetchall()
    rows = "".join(f"""<tr>
      <td style="font-size:.82rem">{p["name"]}</td>
      <td style="color:var(--green)">KES {p["amount"]:,.2f}</td>
      <td>{p["phone"] or "—"}</td>
      <td style="font-size:.75rem;font-family:monospace">{p["mpesa_ref"] or "—"}</td>
      <td>{_status_badge(p["status"])}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(p["created_at"])}</td>
    </tr>""" for p in payments)
    content = f"""
    <div class="page-header"><h1>💳 Payments ({len(payments)})</h1></div>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>User</th><th>Amount</th><th>Phone</th><th>M-Pesa Ref</th><th>Status</th><th>Time</th></tr></thead>
        <tbody>{rows or "<tr><td colspan='6' style='text-align:center;padding:24px;color:var(--text3)'>No payments yet</td></tr>"}</tbody>
        </table>
      </div>
    </div>"""
    return render_layout(content, "Payments", "admin_pay")

@app.route("/admin/withdrawals", methods=["GET","POST"])
@login_required
@admin_required
def admin_withdrawals():
    db = get_db()
    if request.method == "POST":
        wid = request.form.get("wid")
        action = request.form.get("action")
        note = request.form.get("note","")
        status = "completed" if action=="approve" else "rejected"
        w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        db.execute("UPDATE withdrawals SET status=?,admin_note=? WHERE id=?", (status,note,wid))
        if w:
            if action == "reject":
                # Refund
                credit_wallet(db, w["user_id"], w["amount"], f"Withdrawal refunded: {note}", wid)
            notify(db, w["user_id"], f"Withdrawal of KES {w['amount']:,.0f} {'approved' if action=='approve' else 'rejected'}. {note}")
        db.commit()

    withdrawals = db.execute("""SELECT w.*,u.name,u.email FROM withdrawals w
                                JOIN users u ON w.user_id=u.id
                                ORDER BY w.created_at DESC""").fetchall()
    rows = "".join(f"""<tr>
      <td style="font-size:.82rem;font-weight:500">{w["name"]}</td>
      <td style="color:var(--green)">KES {w["amount"]:,.2f}</td>
      <td>{w["phone"]}</td>
      <td>{_status_badge(w["status"])}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(w["created_at"])}</td>
      <td>{"<form method='POST' style='display:flex;gap:6px;align-items:center'><input type='hidden' name='wid' value='"+w["id"]+"'><input name='note' class='form-control' style='width:140px' placeholder='Note...'><button name='action' value='approve' class='btn btn-success btn-sm'>✓ Approve</button><button name='action' value='reject' class='btn btn-danger btn-sm'>✗ Reject</button></form>" if w["status"]=="pending" else w["admin_note"] or "—"}</td>
    </tr>""" for w in withdrawals)

    content = f"""
    <div class="page-header"><h1>💸 Withdrawals ({len(withdrawals)})</h1></div>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>User</th><th>Amount</th><th>Phone</th><th>Status</th><th>Time</th><th>Action / Note</th></tr></thead>
        <tbody>{rows or "<tr><td colspan='6' style='text-align:center;padding:24px;color:var(--text3)'>No withdrawals yet</td></tr>"}</tbody>
        </table>
      </div>
    </div>"""
    return render_layout(content, "Withdrawals", "admin_with")

@app.route("/admin/settings", methods=["GET","POST"])
@login_required
@admin_required
def admin_settings():
    db = get_db()
    if request.method == "POST":
        settings = db.execute("SELECT * FROM site_settings").fetchall()
        for s in settings:
            val = request.form.get(s["key"],"")
            if s["type"] == "toggle":
                val = "1" if request.form.get(s["key"]) else "0"
            db.execute("UPDATE site_settings SET value=? WHERE key=?", (val, s["key"]))
        db.commit()

    settings = db.execute("SELECT * FROM site_settings ORDER BY key").fetchall()
    fields = ""
    for s in settings:
        if s["type"] == "toggle":
            checked = "checked" if s["value"]=="1" else ""
            fields += f"""<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">
              <div><div style="font-size:.875rem;font-weight:500">{s["label"]}</div>
              <div style="font-size:.72rem;color:var(--text3)">{s["key"]}</div></div>
              <label class="toggle"><input type="checkbox" name="{s["key"]}" {checked}><span class="toggle-slider"></span></label>
            </div>"""
        elif s["type"] == "number":
            fields += f"""<div class="form-group">
              <label class="form-label">{s["label"]} <span style="color:var(--text3);font-size:.75rem">({s["key"]})</span></label>
              <input name="{s["key"]}" type="number" class="form-control" value="{s["value"]}">
            </div>"""
        else:
            fields += f"""<div class="form-group">
              <label class="form-label">{s["label"]} <span style="color:var(--text3);font-size:.75rem">({s["key"]})</span></label>
              <input name="{s["key"]}" class="form-control" value="{s["value"]}">
            </div>"""

    content = f"""
    <div class="page-header"><h1>⚙️ Site Settings</h1><p>Control every aspect of the platform</p></div>
    <div class="card" style="max-width:720px">
      <form method="POST">
        {fields}
        <div class="divider"></div>
        <button class="btn btn-primary btn-lg">Save All Settings</button>
      </form>
    </div>"""
    return render_layout(content, "Settings", "admin_set")

@app.route("/admin/activity", methods=["GET","POST"])
@login_required
@admin_required
def admin_activity():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            aid = request.form.get("aid")
            db.execute("DELETE FROM activity_feed WHERE id=?", (aid,))
        elif action == "post":
            msg = request.form.get("message","")
            typ = request.form.get("type","info")
            is_fake = int(request.form.get("is_fake","0"))
            if msg:
                add_activity(db, msg, typ, is_fake=is_fake)
        db.commit()

    acts = db.execute("SELECT * FROM activity_feed ORDER BY created_at DESC LIMIT 50").fetchall()
    rows = "".join(f"""<tr>
      <td style="font-size:.82rem">{a["message"]}</td>
      <td><span class="badge badge-{"gray" if a["is_fake"] else "green"}">{"Fake" if a["is_fake"] else "Real"}</span></td>
      <td>{a["type"]}</td>
      <td style="font-size:.75rem;color:var(--text3)">{_rel_time(a["created_at"])}</td>
      <td><form method="POST"><input type="hidden" name="action" value="delete"><input type="hidden" name="aid" value="{a["id"]}">
        <button class="btn btn-danger btn-sm">Delete</button></form></td>
    </tr>""" for a in acts)

    content = f"""
    <div class="page-header"><h1>📢 Activity Feed Manager</h1></div>
    <div class="card" style="margin-bottom:20px">
      <div class="card-title" style="margin-bottom:14px">Post Activity</div>
      <form method="POST" style="display:flex;gap:10px;flex-wrap:wrap">
        <input type="hidden" name="action" value="post">
        <div class="search-box" style="flex:1;min-width:250px"><span>✍️</span>
          <input name="message" placeholder="Activity message..." required></div>
        <select name="type" class="form-control" style="width:140px">
          <option value="info">Info</option>
          <option value="success">Success</option>
          <option value="deposit">Deposit</option>
          <option value="rating">Rating</option>
          <option value="join">Join</option>
          <option value="upgrade">Upgrade</option>
        </select>
        <select name="is_fake" class="form-control" style="width:120px">
          <option value="1">Fake</option>
          <option value="0">Real</option>
        </select>
        <button class="btn btn-primary">Post</button>
      </form>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table><thead><tr><th>Message</th><th>Type</th><th>Category</th><th>Time</th><th>Delete</th></tr></thead>
        <tbody>{rows}</tbody></table>
      </div>
    </div>"""
    return render_layout(content, "Activity Feed", "admin_act")

@app.route("/admin/fake-activity", methods=["GET","POST"])
@login_required
@admin_required
def admin_fake_activity():
    db = get_db()
    if request.method == "POST":
        count = int(request.form.get("count",5))
        for _ in range(count):
            tmpl, typ = random.choice(_fake_msgs)
            msg = tmpl.format(name=random.choice(FAKE_NAMES),
                              amt=random.choice([200,500,750,1000,1500,2000]))
            add_activity(db, msg, typ, is_fake=1)
        db.commit()
        return redirect(url_for("admin_activity"))
    content = f"""
    <div class="page-header"><h1>🤖 Generate Fake Activity</h1></div>
    <div class="card" style="max-width:480px">
      <form method="POST">
        <div class="form-group"><label class="form-label">How many fake events?</label>
          <input name="count" type="number" class="form-control" value="10" min="1" max="100"></div>
        <button class="btn btn-primary">Generate</button>
      </form>
    </div>"""
    return render_layout(content, "Fake Activity", "admin_act")

# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status":"ok","version":"1.0.0","platform":"DTIP"})

# ─────────────────────────────────────────────
# BOOT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("FLASK_DEBUG","0") == "1"
    print(f"🚀 DTIP starting on port {port}")
    print(f"🔑 Admin: {ADMIN_EMAIL} / {ADMIN_PASS}")
    app.run(host="0.0.0.0", port=port, debug=debug)
