"""
TaskHive - Production-Ready SaaS Marketplace Platform
Single-file Flask app with full feature set
Deploy immediately on Railway
"""

import os
import hmac
import hashlib
import json
import secrets
import string
import requests
import logging
from datetime import datetime, timedelta
from functools import wraps
from math import ceil

from flask import (
    Flask, render_template_string, request, redirect, url_for,
    session, flash, jsonify, abort, g
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
from sqlalchemy import func, desc

# ─── App Setup ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///taskhive.db"
).replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_ENABLED"] = True
app.config["WTF_CSRF_SECRET_KEY"] = os.environ.get("CSRF_SECRET", secrets.token_hex(16))
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day"])
oauth = OAuth(app)

# Google OAuth
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# IntaSend config
INTASEND_API_KEY = os.environ.get("INTASEND_API_KEY", "")
INTASEND_PUBLISHABLE_KEY = os.environ.get("INTASEND_PUBLISHABLE_KEY", "")
INTASEND_BASE_URL = "https://sandbox.intasend.com" if os.environ.get("INTASEND_TEST", "true") == "true" else "https://payment.intasend.com"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Models ──────────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(256))
    profile_picture = db.Column(db.String(500))
    google_id = db.Column(db.String(100), unique=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    available_balance = db.Column(db.Float, default=0.0)
    held_balance = db.Column(db.Float, default=0.0)
    referral_code = db.Column(db.String(20), unique=True)
    referred_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    signup_bonus_paid = db.Column(db.Boolean, default=False)
    first_task_bonus_paid = db.Column(db.Boolean, default=False)
    first_deposit_bonus_paid = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(10), default="light")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks_posted = db.relationship("Task", foreign_keys="Task.poster_id", backref="poster", lazy=True)
    tasks_taken = db.relationship("Task", foreign_keys="Task.worker_id", backref="worker", lazy=True)
    referrals = db.relationship("User", foreign_keys="User.referred_by_id", lazy=True)
    notifications = db.relationship("Notification", backref="user", lazy=True)
    ledger_entries = db.relationship("LedgerEntry", backref="user", lazy=True)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return self.password_hash and check_password_hash(self.password_hash, pw)

    @property
    def total_balance(self):
        return self.available_balance + self.held_balance

    @property
    def unread_notifications(self):
        return Notification.query.filter_by(user_id=self.id, is_read=False).count()


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50))
    budget = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="open")  # open, assigned, completed, cancelled
    poster_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    platform_fee = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    bids = db.relationship("Bid", backref="task", lazy=True, cascade="all, delete-orphan")


class Bid(db.Model):
    __tablename__ = "bids"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    bidder_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    message = db.Column(db.Text)
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bidder = db.relationship("User", backref="bids")


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    invoice_id = db.Column(db.String(100), unique=True)
    tracking_id = db.Column(db.String(100))
    amount = db.Column(db.Float, nullable=False)
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default="pending")  # pending, completed, failed
    payment_type = db.Column(db.String(20), default="deposit")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship("User", backref="payments")


class Withdrawal(db.Model):
    __tablename__ = "withdrawals"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default="pending")  # pending, approved, rejected, paid
    admin_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime)
    user = db.relationship("User", backref="withdrawals")


class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    entry_type = db.Column(db.String(30))  # deposit, withdrawal, task_earning, escrow_hold, escrow_release, bonus, refund
    amount = db.Column(db.Float, nullable=False)
    balance_before = db.Column(db.Float)
    balance_after = db.Column(db.Float)
    description = db.Column(db.String(300))
    reference_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200))
    message = db.Column(db.Text)
    notif_type = db.Column(db.String(30), default="info")
    is_read = db.Column(db.Boolean, default=False)
    link = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SiteSetting(db.Model):
    __tablename__ = "site_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    description = db.Column(db.String(300))
    setting_type = db.Column(db.String(20), default="string")  # string, bool, float, int


class ReferralBonus(db.Model):
    __tablename__ = "referral_bonuses"
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    referred_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bonus_type = db.Column(db.String(50))
    amount = db.Column(db.Float)
    paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─── Helper Functions ─────────────────────────────────────────────────────────

def get_setting(key, default=None):
    s = SiteSetting.query.filter_by(key=key).first()
    if not s:
        return default
    if s.setting_type == "bool":
        return s.value.lower() in ("true", "1", "yes")
    if s.setting_type == "float":
        return float(s.value)
    if s.setting_type == "int":
        return int(s.value)
    return s.value


def generate_referral_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(8))
        if not User.query.filter_by(referral_code=code).first():
            return code


def add_ledger(user_id, entry_type, amount, description, reference_id=None):
    user = User.query.get(user_id)
    if not user:
        return
    bal_before = user.available_balance
    LedgerEntry(
        user_id=user_id,
        entry_type=entry_type,
        amount=amount,
        balance_before=bal_before,
        balance_after=bal_before + amount,
        description=description,
        reference_id=reference_id,
    )
    db.session.add(LedgerEntry(
        user_id=user_id, entry_type=entry_type, amount=amount,
        balance_before=bal_before, balance_after=bal_before + amount,
        description=description, reference_id=reference_id
    ))


def add_notification(user_id, title, message, notif_type="info", link=None):
    db.session.add(Notification(
        user_id=user_id, title=title, message=message,
        notif_type=notif_type, link=link
    ))


def credit_wallet(user, amount, entry_type, description, reference_id=None):
    bal_before = user.available_balance
    user.available_balance += amount
    db.session.add(LedgerEntry(
        user_id=user.id, entry_type=entry_type, amount=amount,
        balance_before=bal_before, balance_after=user.available_balance,
        description=description, reference_id=reference_id
    ))


def debit_wallet(user, amount, entry_type, description, reference_id=None):
    bal_before = user.available_balance
    user.available_balance -= amount
    db.session.add(LedgerEntry(
        user_id=user.id, entry_type=entry_type, amount=-amount,
        balance_before=bal_before, balance_after=user.available_balance,
        description=description, reference_id=reference_id
    ))


def pay_referral_bonuses(new_user):
    if not new_user.referred_by_id:
        return
    referrer = User.query.get(new_user.referred_by_id)
    if not referrer:
        return
    signup_bonus = float(get_setting("referral_signup_bonus", 0))
    if signup_bonus > 0:
        credit_wallet(referrer, signup_bonus, "bonus", f"Referral signup bonus from {new_user.name}", str(new_user.id))
        db.session.add(ReferralBonus(
            referrer_id=referrer.id, referred_id=new_user.id,
            bonus_type="signup", amount=signup_bonus, paid=True
        ))
        add_notification(referrer.id, "Referral Bonus!", f"You earned KES {signup_bonus} — {new_user.name} signed up using your link.", "success")


def init_settings():
    defaults = [
        ("platform_fee_percent", "10", "Platform fee %", "float"),
        ("referral_signup_bonus", "50", "Bonus for referring a new user (KES)", "float"),
        ("referral_first_task_bonus", "100", "Bonus when referred user completes first task", "float"),
        ("referral_first_deposit_bonus", "75", "Bonus when referred user makes first deposit", "float"),
        ("min_withdrawal", "200", "Minimum withdrawal amount (KES)", "float"),
        ("max_withdrawal", "50000", "Maximum withdrawal amount (KES)", "float"),
        ("registrations_enabled", "true", "Allow new user registrations", "bool"),
        ("withdrawals_enabled", "true", "Allow withdrawals", "bool"),
        ("maintenance_mode", "false", "Enable maintenance mode", "bool"),
        ("site_name", "TaskHive", "Platform name", "string"),
        ("membership_price", "500", "Monthly membership price (KES)", "float"),
    ]
    for key, value, desc, stype in defaults:
        if not SiteSetting.query.filter_by(key=key).first():
            db.session.add(SiteSetting(key=key, value=value, description=desc, setting_type=stype))
    db.session.commit()


# ─── Auth Decorators ──────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or user.is_banned:
            session.clear()
            flash("Account unavailable.", "danger")
            return redirect(url_for("login"))
        g.user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            abort(403)
        g.user = user
        return f(*args, **kwargs)
    return decorated


# ─── IntaSend Payment Helpers ─────────────────────────────────────────────────

def intasend_stk_push(phone, amount, invoice_id, email):
    url = f"{INTASEND_BASE_URL}/api/v1/payment/mpesa-stk-push/"
    headers = {
        "Authorization": f"Bearer {INTASEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "public_key": INTASEND_PUBLISHABLE_KEY,
        "currency": "KES",
        "amount": int(amount),
        "phone_number": phone,
        "email": email,
        "narrative": f"TaskHive Deposit #{invoice_id}",
        "api_ref": invoice_id,
        "redirect_url": url_for("payment_callback", _external=True),
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"IntaSend STK push error: {e}")
        return None


def verify_intasend_webhook(payload_bytes, signature):
    secret = INTASEND_API_KEY.encode()
    computed = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature or "")


# ─── HTML Template ────────────────────────────────────────────────────────────

BASE_HTML = '''<!DOCTYPE html>
<html lang="en" data-theme="{{ theme }}">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{% block title %}TaskHive{% endblock %} — TaskHive</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<style>
:root{
  --accent:#F97316;--accent2:#6366F1;--success:#22C55E;--danger:#EF4444;--warning:#EAB308;
  --bg:#F8FAFC;--surface:#FFFFFF;--surface2:#F1F5F9;--surface3:#E2E8F0;
  --text:#0F172A;--text2:#475569;--text3:#94A3B8;
  --border:#E2E8F0;--sidebar-w:260px;
  --radius:12px;--radius-sm:8px;--shadow:0 1px 3px rgba(0,0,0,.08),0 4px 16px rgba(0,0,0,.06);
  --shadow-lg:0 8px 32px rgba(0,0,0,.12);
  font-family:'Plus Jakarta Sans',sans-serif;
}
[data-theme="dark"]{
  --bg:#0D1117;--surface:#161B22;--surface2:#21262D;--surface3:#30363D;
  --text:#F0F6FC;--text2:#8B949E;--text3:#6E7681;
  --border:#30363D;--shadow:0 1px 3px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3);
  --shadow-lg:0 8px 32px rgba(0,0,0,.5);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);min-height:100vh;transition:background .2s,color .2s}
a{color:var(--accent);text-decoration:none}
a:hover{opacity:.85}

/* ── Sidebar ── */
.sidebar{
  position:fixed;left:0;top:0;bottom:0;width:var(--sidebar-w);
  background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;z-index:100;overflow:hidden;
  transition:transform .3s ease;
}
.sidebar-logo{
  padding:24px 20px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;
}
.sidebar-logo .logo-icon{
  width:38px;height:38px;background:var(--accent);border-radius:10px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;
}
.sidebar-logo .logo-text{font-size:18px;font-weight:800;letter-spacing:-.5px}
.sidebar-logo .logo-text span{color:var(--accent)}
.sidebar-nav{flex:1;overflow-y:auto;padding:12px 0;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.nav-section{padding:8px 16px 4px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text3)}
.nav-item{
  display:flex;align-items:center;gap:12px;
  padding:10px 20px;font-size:14px;font-weight:500;color:var(--text2);
  border-radius:0;cursor:pointer;transition:.15s;position:relative;
  border-left:3px solid transparent;
}
.nav-item:hover{background:var(--surface2);color:var(--text);text-decoration:none}
.nav-item.active{
  background:linear-gradient(90deg,rgba(249,115,22,.1),transparent);
  color:var(--accent);border-left-color:var(--accent);
}
.nav-item i{width:18px;text-align:center;font-size:15px}
.nav-item .badge{
  margin-left:auto;background:var(--danger);color:#fff;
  font-size:10px;font-weight:700;padding:2px 6px;border-radius:999px;
}
.sidebar-footer{padding:16px;border-top:1px solid var(--border)}
.user-mini{display:flex;align-items:center;gap:10px}
.user-mini .avatar{
  width:36px;height:36px;border-radius:50%;background:var(--accent);
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;
  overflow:hidden;flex-shrink:0;
}
.user-mini .avatar img{width:100%;height:100%;object-fit:cover}
.user-mini .user-info{flex:1;min-width:0}
.user-mini .user-name{font-size:13px;font-weight:600;truncate}
.user-mini .user-role{font-size:11px;color:var(--text3)}

/* ── Main layout ── */
.main-wrap{margin-left:var(--sidebar-w);min-height:100vh;display:flex;flex-direction:column}
.topbar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:0 28px;height:64px;display:flex;align-items:center;gap:16px;
  position:sticky;top:0;z-index:50;
}
.topbar-title{font-size:18px;font-weight:700;flex:1}
.topbar-actions{display:flex;align-items:center;gap:12px}
.page-body{padding:28px;flex:1}

/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.card-header{padding:20px 24px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-header h2{font-size:16px;font-weight:700}
.card-body{padding:24px}
.stat-card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:24px;display:flex;gap:16px;align-items:flex-start;box-shadow:var(--shadow);
  transition:.2s;
}
.stat-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.stat-icon{
  width:48px;height:48px;border-radius:12px;display:flex;
  align-items:center;justify-content:center;font-size:22px;flex-shrink:0;
}
.stat-body{}
.stat-label{font-size:12px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:26px;font-weight:800;line-height:1.1;margin-top:2px}
.stat-sub{font-size:12px;color:var(--text3);margin-top:4px}

/* ── Stats Grid ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-bottom:28px}

/* ── Buttons ── */
.btn{
  display:inline-flex;align-items:center;gap:8px;padding:9px 18px;
  border-radius:var(--radius-sm);font-size:14px;font-weight:600;
  border:none;cursor:pointer;transition:.15s;text-decoration:none;
  white-space:nowrap;
}
.btn:hover{text-decoration:none;opacity:.9}
.btn-primary{background:var(--accent);color:#fff}
.btn-secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-success{background:var(--success);color:#fff}
.btn-danger{background:var(--danger);color:#fff}
.btn-sm{padding:6px 12px;font-size:12px;border-radius:6px}
.btn-icon{padding:8px;border-radius:var(--radius-sm)}
.btn-outline{background:transparent;border:2px solid var(--accent);color:var(--accent)}

/* ── Forms ── */
.form-group{margin-bottom:18px}
.form-label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text2)}
.form-control{
  width:100%;padding:10px 14px;border:1.5px solid var(--border);
  border-radius:var(--radius-sm);background:var(--surface2);color:var(--text);
  font-size:14px;transition:.15s;font-family:inherit;
}
.form-control:focus{outline:none;border-color:var(--accent);background:var(--surface);box-shadow:0 0 0 3px rgba(249,115,22,.15)}
.form-control::placeholder{color:var(--text3)}
select.form-control option{background:var(--surface);color:var(--text)}

/* ── Tables ── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:12px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surface2)}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.badge-success{background:rgba(34,197,94,.15);color:var(--success)}
.badge-danger{background:rgba(239,68,68,.15);color:var(--danger)}
.badge-warning{background:rgba(234,179,8,.15);color:var(--warning)}
.badge-info{background:rgba(99,102,241,.15);color:var(--accent2)}
.badge-neutral{background:var(--surface3);color:var(--text2)}

/* ── Alerts ── */
.alert{
  padding:14px 18px;border-radius:var(--radius-sm);margin-bottom:18px;
  font-size:14px;font-weight:500;display:flex;align-items:center;gap:10px;
}
.alert-success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--success)}
.alert-danger{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:var(--danger)}
.alert-warning{background:rgba(234,179,8,.1);border:1px solid rgba(234,179,8,.3);color:var(--warning)}
.alert-info{background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.3);color:var(--accent2)}

/* ── Task card ── */
.task-card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:16px;transition:.2s;
}
.task-card:hover{border-color:var(--accent);box-shadow:var(--shadow-lg);transform:translateY(-1px)}
.task-title{font-size:16px;font-weight:700;margin-bottom:6px}
.task-meta{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--text3);margin-bottom:12px}
.task-budget{font-size:20px;font-weight:800;color:var(--accent)}

/* ── Notification dropdown ── */
.notif-btn{position:relative}
.notif-count{
  position:absolute;top:-4px;right:-4px;background:var(--danger);color:#fff;
  font-size:9px;font-weight:700;padding:2px 5px;border-radius:999px;min-width:16px;text-align:center;
}
.dropdown{position:relative;display:inline-block}
.dropdown-menu{
  position:absolute;right:0;top:calc(100% + 8px);min-width:320px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  box-shadow:var(--shadow-lg);z-index:200;overflow:hidden;display:none;
}
.dropdown.open .dropdown-menu{display:block}
.dropdown-item{
  padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:.15s;display:block;color:var(--text);
}
.dropdown-item:hover{background:var(--surface2);text-decoration:none}
.dropdown-item:last-child{border-bottom:none}
.notif-item .notif-title{font-size:13px;font-weight:600}
.notif-item .notif-msg{font-size:12px;color:var(--text3);margin-top:2px}
.notif-item .notif-time{font-size:11px;color:var(--text3);margin-top:4px}
.notif-item.unread{background:rgba(249,115,22,.05)}

/* ── Theme toggle ── */
.theme-toggle{
  background:var(--surface2);border:1px solid var(--border);
  width:44px;height:24px;border-radius:999px;position:relative;cursor:pointer;
  display:flex;align-items:center;padding:3px;transition:.2s;
}
.theme-toggle .thumb{
  width:18px;height:18px;border-radius:50%;background:var(--accent);
  transition:.2s;margin-left:0;
}
[data-theme="dark"] .theme-toggle .thumb{margin-left:20px}

/* ── Auth pages ── */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);padding:20px}
.auth-card{width:100%;max-width:440px;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px;box-shadow:var(--shadow-lg)}
.auth-logo{text-align:center;margin-bottom:28px}
.auth-logo .logo-big{font-size:32px;font-weight:900}
.auth-logo .logo-big span{color:var(--accent)}
.auth-title{font-size:22px;font-weight:800;text-align:center;margin-bottom:6px}
.auth-sub{font-size:14px;color:var(--text3);text-align:center;margin-bottom:28px}
.divider{display:flex;align-items:center;gap:12px;margin:20px 0;color:var(--text3);font-size:13px}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:var(--border)}
.google-btn{
  width:100%;padding:12px;border:1.5px solid var(--border);border-radius:var(--radius-sm);
  background:var(--surface);display:flex;align-items:center;justify-content:center;gap:10px;
  font-size:14px;font-weight:600;cursor:pointer;transition:.15s;color:var(--text);
}
.google-btn:hover{background:var(--surface2)}
.google-btn img{width:20px;height:20px}

/* ── Pagination ── */
.pagination{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.page-btn{
  padding:6px 12px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface);font-size:13px;font-weight:600;cursor:pointer;color:var(--text);text-decoration:none;
}
.page-btn:hover{background:var(--surface2)}
.page-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.page-btn.disabled{opacity:.4;cursor:not-allowed}

/* ── Progress bar ── */
.progress{height:8px;background:var(--surface3);border-radius:999px;overflow:hidden}
.progress-bar{height:100%;background:var(--accent);border-radius:999px;transition:width .3s}

/* ── Mobile ── */
.hamburger{display:none;background:none;border:none;cursor:pointer;padding:8px;color:var(--text);font-size:22px}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99}
@media(max-width:768px){
  .sidebar{transform:translateX(-100%)}
  .sidebar.open{transform:translateX(0)}
  .sidebar-overlay.open{display:block}
  .main-wrap{margin-left:0}
  .hamburger{display:block}
  .stats-grid{grid-template-columns:1fr 1fr}
  .page-body{padding:16px}
}
@media(max-width:480px){
  .stats-grid{grid-template-columns:1fr}
}

/* ── Utils ── */
.flex{display:flex}.items-center{align-items:center}.justify-between{justify-content:space-between}
.gap-2{gap:8px}.gap-3{gap:12px}.gap-4{gap:16px}
.mt-2{margin-top:8px}.mt-4{margin-top:16px}.mt-6{margin-top:24px}
.mb-2{margin-bottom:8px}.mb-4{margin-bottom:16px}
.text-sm{font-size:13px}.text-xs{font-size:11px}
.text-muted{color:var(--text3)}.font-bold{font-weight:700}
.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.w-full{width:100%}.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:600px){.grid-2{grid-template-columns:1fr}}
.empty-state{text-align:center;padding:60px 20px;color:var(--text3)}
.empty-state i{font-size:48px;margin-bottom:16px;display:block}
.monospace{font-family:'JetBrains Mono',monospace;font-size:13px}
</style>
</head>
<body>
{% if current_user %}
<div class="sidebar-overlay" id="overlay" onclick="closeSidebar()"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-logo">
    <div class="logo-icon"><i class="fas fa-layer-group"></i></div>
    <div class="logo-text">Task<span>Hive</span></div>
  </div>
  <nav class="sidebar-nav">
    <div class="nav-section">Main</div>
    <a href="{{ url_for('dashboard') }}" class="nav-item {% if request.endpoint == 'dashboard' %}active{% endif %}">
      <i class="fas fa-house"></i> Dashboard
    </a>
    <a href="{{ url_for('tasks') }}" class="nav-item {% if request.endpoint in ('tasks','task_detail') %}active{% endif %}">
      <i class="fas fa-list-check"></i> Browse Tasks
    </a>
    <a href="{{ url_for('post_task') }}" class="nav-item {% if request.endpoint == 'post_task' %}active{% endif %}">
      <i class="fas fa-plus-circle"></i> Post a Task
    </a>
    <a href="{{ url_for('my_tasks') }}" class="nav-item {% if request.endpoint == 'my_tasks' %}active{% endif %}">
      <i class="fas fa-briefcase"></i> My Tasks
    </a>
    <div class="nav-section">Finance</div>
    <a href="{{ url_for('wallet') }}" class="nav-item {% if request.endpoint == 'wallet' %}active{% endif %}">
      <i class="fas fa-wallet"></i> Wallet
    </a>
    <a href="{{ url_for('deposit') }}" class="nav-item {% if request.endpoint == 'deposit' %}active{% endif %}">
      <i class="fas fa-arrow-down-to-line"></i> Deposit
    </a>
    <a href="{{ url_for('withdraw') }}" class="nav-item {% if request.endpoint == 'withdraw' %}active{% endif %}">
      <i class="fas fa-money-bill-transfer"></i> Withdraw
    </a>
    <a href="{{ url_for('transactions') }}" class="nav-item {% if request.endpoint == 'transactions' %}active{% endif %}">
      <i class="fas fa-clock-rotate-left"></i> History
    </a>
    <div class="nav-section">Account</div>
    <a href="{{ url_for('referrals') }}" class="nav-item {% if request.endpoint == 'referrals' %}active{% endif %}">
      <i class="fas fa-users-rays"></i> Referrals
    </a>
    <a href="{{ url_for('notifications_page') }}" class="nav-item {% if request.endpoint == 'notifications_page' %}active{% endif %}">
      <i class="fas fa-bell"></i> Notifications
      {% if current_user.unread_notifications > 0 %}
      <span class="badge">{{ current_user.unread_notifications }}</span>
      {% endif %}
    </a>
    <a href="{{ url_for('profile') }}" class="nav-item {% if request.endpoint == 'profile' %}active{% endif %}">
      <i class="fas fa-circle-user"></i> Profile
    </a>
    {% if current_user.is_admin %}
    <div class="nav-section">Admin</div>
    <a href="{{ url_for('admin_dashboard') }}" class="nav-item {% if 'admin' in request.endpoint %}active{% endif %}">
      <i class="fas fa-shield-halved"></i> Admin Panel
    </a>
    {% endif %}
  </nav>
  <div class="sidebar-footer">
    <div class="user-mini">
      <div class="avatar">
        {% if current_user.profile_picture %}
        <img src="{{ current_user.profile_picture }}" alt="{{ current_user.name }}"/>
        {% else %}
        {{ current_user.name[0].upper() }}
        {% endif %}
      </div>
      <div class="user-info truncate">
        <div class="user-name truncate">{{ current_user.name }}</div>
        <div class="user-role">{% if current_user.is_admin %}Admin{% else %}Member{% endif %}</div>
      </div>
      <a href="{{ url_for('logout') }}" class="btn btn-icon btn-secondary" title="Logout"><i class="fas fa-right-from-bracket"></i></a>
    </div>
  </div>
</aside>
<div class="main-wrap">
  <header class="topbar">
    <button class="hamburger" onclick="openSidebar()"><i class="fas fa-bars"></i></button>
    <div class="topbar-title">{% block page_title %}{% endblock %}</div>
    <div class="topbar-actions">
      <!-- Theme toggle -->
      <div class="theme-toggle" onclick="toggleTheme()" title="Toggle theme">
        <div class="thumb"></div>
      </div>
      <!-- Notifications -->
      <div class="dropdown" id="notifDropdown">
        <button class="btn btn-icon btn-secondary notif-btn" onclick="toggleDropdown('notifDropdown')">
          <i class="fas fa-bell"></i>
          {% set unread = current_user.unread_notifications %}
          {% if unread > 0 %}<span class="notif-count">{{ unread }}</span>{% endif %}
        </button>
        <div class="dropdown-menu">
          <div class="dropdown-item" style="font-weight:700;font-size:13px;border-bottom:2px solid var(--border)">
            Notifications <a href="{{ url_for('mark_all_read') }}" style="float:right;font-weight:400;font-size:12px;color:var(--accent)">Mark all read</a>
          </div>
          {% set notifs = current_user.notifications|sort(attribute='created_at',reverse=True)|list %}
          {% for n in notifs[:6] %}
          <a href="{{ url_for('mark_read', nid=n.id) }}" class="dropdown-item notif-item {% if not n.is_read %}unread{% endif %}">
            <div class="notif-title">{{ n.title }}</div>
            <div class="notif-msg">{{ n.message[:80] }}</div>
            <div class="notif-time">{{ n.created_at.strftime('%b %d, %H:%M') }}</div>
          </a>
          {% else %}
          <div class="dropdown-item text-muted text-sm" style="text-align:center;padding:20px">No notifications</div>
          {% endfor %}
          <a href="{{ url_for('notifications_page') }}" class="dropdown-item" style="text-align:center;font-weight:600;color:var(--accent)">View all</a>
        </div>
      </div>
      <!-- Wallet balance -->
      <a href="{{ url_for('wallet') }}" class="btn btn-secondary" style="font-family:'JetBrains Mono',monospace;font-size:13px">
        <i class="fas fa-wallet"></i> KES {{ "%.2f"|format(current_user.available_balance) }}
      </a>
    </div>
  </header>
  <main class="page-body">
    {% with msgs = get_flashed_messages(with_categories=True) %}
    {% for cat, msg in msgs %}
    <div class="alert alert-{{ 'success' if cat == 'success' else 'danger' if cat == 'danger' else 'warning' if cat == 'warning' else 'info' }}">
      <i class="fas fa-{{ 'check-circle' if cat == 'success' else 'exclamation-circle' if cat in ('danger','warning') else 'info-circle' }}"></i>
      {{ msg }}
    </div>
    {% endfor %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>
</div>
{% else %}
<div style="min-height:100vh;background:var(--bg)">
  {% with msgs = get_flashed_messages(with_categories=True) %}
  {% for cat, msg in msgs %}
  <div class="alert alert-{{ cat }}" style="margin:16px;border-radius:var(--radius-sm)">
    <i class="fas fa-info-circle"></i> {{ msg }}
  </div>
  {% endfor %}
  {% endwith %}
  {% block content %}{% endblock %}
</div>
{% endif %}

<script>
function openSidebar(){
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}
function closeSidebar(){
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
}
function toggleDropdown(id){
  const el=document.getElementById(id);
  el.classList.toggle('open');
  document.addEventListener('click',function h(e){
    if(!el.contains(e.target)){el.classList.remove('open');document.removeEventListener('click',h)}
  });
}
function toggleTheme(){
  const html=document.documentElement;
  const isDark=html.getAttribute('data-theme')==='dark';
  html.setAttribute('data-theme',isDark?'light':'dark');
  fetch('/set-theme',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCsrf()},body:JSON.stringify({theme:isDark?'light':'dark'})});
}
function getCsrf(){return document.querySelector('meta[name="csrf-token"]')?.content||''}
// auto-hide alerts
setTimeout(()=>{document.querySelectorAll('.alert').forEach(a=>{a.style.transition='opacity .5s';a.style.opacity='0';setTimeout(()=>a.remove(),500)})},4000);
</script>
<meta name="csrf-token" content="{{ csrf_token() }}">
</body></html>
'''

# ─── Context Processor ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    user = None
    if "user_id" in session:
        user = User.query.get(session["user_id"])
    theme = user.theme if user else request.cookies.get("theme", "light")
    return dict(current_user=user, theme=theme)


# ─── Routes: Auth ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if get_setting("maintenance_mode", False):
        return render_template_string(BASE_HTML + '''{% block content %}
        <div class="auth-wrap"><div class="auth-card" style="text-align:center">
        <i class="fas fa-tools" style="font-size:48px;color:var(--warning);margin-bottom:20px"></i>
        <h2>Maintenance Mode</h2>
        <p class="text-muted mt-4">We'll be back shortly.</p>
        </div></div>{% endblock %}''')

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            if user.is_banned:
                flash("Your account has been banned.", "danger")
                return redirect(url_for("login"))
            session.permanent = True
            session["user_id"] = user.id
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")

    tpl = BASE_HTML + '''
{% block content %}
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo"><div class="logo-big">Task<span>Hive</span></div></div>
    <h1 class="auth-title">Welcome back</h1>
    <p class="auth-sub">Sign in to your account</p>
    <a href="{{ url_for('google_login') }}" class="google-btn">
      <img src="https://www.svgrepo.com/show/475656/google-color.svg" alt="Google"/>
      Continue with Google
    </a>
    <div class="divider">or continue with email</div>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
      <div class="form-group">
        <label class="form-label">Email Address</label>
        <input type="email" name="email" class="form-control" placeholder="you@example.com" required/>
      </div>
      <div class="form-group">
        <label class="form-label">Password</label>
        <input type="password" name="password" class="form-control" placeholder="••••••••" required/>
      </div>
      <button type="submit" class="btn btn-primary w-full" style="width:100%;justify-content:center">Sign In</button>
    </form>
    <p class="text-sm text-muted mt-4" style="text-align:center">
      Don't have an account? <a href="{{ url_for('register') }}">Create one</a>
    </p>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl)


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if not get_setting("registrations_enabled", True):
        flash("Registrations are currently disabled.", "warning")
        return redirect(url_for("login"))
    ref_code = request.args.get("ref", "")
    referrer = User.query.filter_by(referral_code=ref_code).first() if ref_code else None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
        else:
            user = User(
                name=name, email=email,
                referral_code=generate_referral_code(),
                referred_by_id=referrer.id if referrer else None
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            pay_referral_bonuses(user)
            db.session.commit()
            session.permanent = True
            session["user_id"] = user.id
            flash(f"Welcome to TaskHive, {name}!", "success")
            return redirect(url_for("dashboard"))

    tpl = BASE_HTML + '''
{% block content %}
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo"><div class="logo-big">Task<span>Hive</span></div></div>
    <h1 class="auth-title">Create Account</h1>
    <p class="auth-sub">{% if referrer %}Invited by {{ referrer.name }}{% else %}Join the marketplace{% endif %}</p>
    <a href="{{ url_for('google_login') }}" class="google-btn">
      <img src="https://www.svgrepo.com/show/475656/google-color.svg" alt="Google"/>
      Sign up with Google
    </a>
    <div class="divider">or use email</div>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
      <div class="form-group">
        <label class="form-label">Full Name</label>
        <input type="text" name="name" class="form-control" placeholder="Your name" required/>
      </div>
      <div class="form-group">
        <label class="form-label">Email</label>
        <input type="email" name="email" class="form-control" placeholder="you@example.com" required/>
      </div>
      <div class="form-group">
        <label class="form-label">Password</label>
        <input type="password" name="password" class="form-control" placeholder="Min 8 characters" required/>
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%;justify-content:center">Create Account</button>
    </form>
    <p class="text-sm text-muted mt-4" style="text-align:center">Already have an account? <a href="{{ url_for('login') }}">Sign in</a></p>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, referrer=referrer)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/google/login")
def google_login():
    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/google/callback")
def google_callback():
    try:
        token = google.authorize_access_token()
        userinfo = token.get("userinfo") or google.get("https://openidconnect.googleapis.com/v1/userinfo").json()
        google_id = userinfo.get("sub")
        email = userinfo.get("email", "").lower()
        name = userinfo.get("name", email.split("@")[0])
        picture = userinfo.get("picture")

        user = User.query.filter_by(google_id=google_id).first()
        if not user:
            user = User.query.filter_by(email=email).first()
            if user:
                user.google_id = google_id
                user.profile_picture = picture
            else:
                ref_code = session.pop("ref_code", None)
                referrer = User.query.filter_by(referral_code=ref_code).first() if ref_code else None
                user = User(
                    email=email, name=name, google_id=google_id,
                    profile_picture=picture, referral_code=generate_referral_code(),
                    referred_by_id=referrer.id if referrer else None
                )
                db.session.add(user)
                db.session.flush()
                pay_referral_bonuses(user)
        if user.is_banned:
            flash("Account banned.", "danger")
            return redirect(url_for("login"))
        db.session.commit()
        session.permanent = True
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))
    except Exception as e:
        logger.error(f"Google OAuth error: {e}")
        flash("Google login failed. Try again.", "danger")
        return redirect(url_for("login"))


@app.route("/set-theme", methods=["POST"])
def set_theme():
    data = request.get_json()
    theme = data.get("theme", "light")
    if "user_id" in session:
        user = User.query.get(session["user_id"])
        if user:
            user.theme = theme
            db.session.commit()
    return jsonify({"ok": True})


# ─── Routes: Dashboard ────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user = g.user
    open_tasks = Task.query.filter_by(status="open").count()
    my_posted = Task.query.filter_by(poster_id=user.id).count()
    my_working = Task.query.filter_by(worker_id=user.id, status="assigned").count()
    completed = Task.query.filter_by(worker_id=user.id, status="completed").count()
    recent_tasks = Task.query.filter_by(status="open").order_by(desc(Task.created_at)).limit(5).all()
    recent_ledger = LedgerEntry.query.filter_by(user_id=user.id).order_by(desc(LedgerEntry.created_at)).limit(5).all()

    tpl = BASE_HTML + '''
{% block page_title %}Dashboard{% endblock %}
{% block content %}
<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(249,115,22,.15);color:var(--accent)"><i class="fas fa-wallet"></i></div>
    <div class="stat-body">
      <div class="stat-label">Available Balance</div>
      <div class="stat-value monospace">KES {{ "%.2f"|format(user.available_balance) }}</div>
      <div class="stat-sub">Held: KES {{ "%.2f"|format(user.held_balance) }}</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(99,102,241,.15);color:var(--accent2)"><i class="fas fa-list-check"></i></div>
    <div class="stat-body">
      <div class="stat-label">Open Tasks</div>
      <div class="stat-value">{{ open_tasks }}</div>
      <div class="stat-sub">Available to work</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(34,197,94,.15);color:var(--success)"><i class="fas fa-briefcase"></i></div>
    <div class="stat-body">
      <div class="stat-label">My Active</div>
      <div class="stat-value">{{ my_working }}</div>
      <div class="stat-sub">In progress</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(234,179,8,.15);color:var(--warning)"><i class="fas fa-trophy"></i></div>
    <div class="stat-body">
      <div class="stat-label">Completed</div>
      <div class="stat-value">{{ completed }}</div>
      <div class="stat-sub">Tasks finished</div>
    </div>
  </div>
</div>
<div class="grid-2">
  <div class="card">
    <div class="card-header"><h2><i class="fas fa-fire" style="color:var(--accent)"></i> Latest Tasks</h2>
    <a href="{{ url_for('tasks') }}" class="btn btn-sm btn-secondary">View all</a></div>
    <div class="card-body" style="padding:0">
      {% for t in recent_tasks %}
      <a href="{{ url_for('task_detail', task_id=t.id) }}" style="display:block;padding:16px 20px;border-bottom:1px solid var(--border);transition:.15s;color:var(--text)">
        <div style="display:flex;justify-content:space-between;align-items:start">
          <div>
            <div style="font-weight:600;font-size:14px">{{ t.title }}</div>
            <div class="text-xs text-muted mt-2">{{ t.category or 'General' }} · {{ t.created_at.strftime('%b %d') }}</div>
          </div>
          <div class="monospace" style="color:var(--accent);font-weight:800">KES {{ t.budget|int }}</div>
        </div>
      </a>
      {% else %}
      <div class="empty-state"><i class="fas fa-inbox"></i>No tasks yet</div>
      {% endfor %}
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h2><i class="fas fa-clock-rotate-left" style="color:var(--accent2)"></i> Recent Transactions</h2>
    <a href="{{ url_for('transactions') }}" class="btn btn-sm btn-secondary">View all</a></div>
    <div class="card-body" style="padding:0">
      {% for e in recent_ledger %}
      <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:13px;font-weight:600">{{ e.description[:45] }}</div>
          <div class="text-xs text-muted mt-2">{{ e.created_at.strftime('%b %d, %H:%M') }}</div>
        </div>
        <div class="monospace {{ 'badge-success' if e.amount > 0 else 'badge-danger' }}" style="font-weight:700;font-size:13px">
          {{ '+' if e.amount > 0 else '' }}{{ "%.2f"|format(e.amount) }}
        </div>
      </div>
      {% else %}
      <div class="empty-state"><i class="fas fa-receipt"></i>No transactions yet</div>
      {% endfor %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, user=user, open_tasks=open_tasks, my_posted=my_posted,
                                  my_working=my_working, completed=completed,
                                  recent_tasks=recent_tasks, recent_ledger=recent_ledger)


# ─── Routes: Tasks ────────────────────────────────────────────────────────────

@app.route("/tasks")
@login_required
def tasks():
    page = request.args.get("page", 1, type=int)
    per_page = 10
    category = request.args.get("category", "")
    q = request.args.get("q", "")
    query = Task.query.filter_by(status="open")
    if category:
        query = query.filter_by(category=category)
    if q:
        query = query.filter(Task.title.ilike(f"%{q}%"))
    total = query.count()
    tasks_list = query.order_by(desc(Task.created_at)).offset((page - 1) * per_page).limit(per_page).all()
    total_pages = ceil(total / per_page) if total else 1
    categories = ["Writing", "Design", "Tech", "Marketing", "Data Entry", "Translation", "Other"]

    tpl = BASE_HTML + '''
{% block page_title %}Browse Tasks{% endblock %}
{% block content %}
<div class="card mb-4">
  <div class="card-body" style="padding:16px">
    <form method="GET" style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
      <input type="text" name="q" value="{{ q }}" class="form-control" placeholder="Search tasks..." style="flex:1;min-width:200px"/>
      <select name="category" class="form-control" style="width:160px">
        <option value="">All Categories</option>
        {% for c in categories %}<option value="{{ c }}" {{ 'selected' if category == c }}>{{ c }}</option>{% endfor %}
      </select>
      <button type="submit" class="btn btn-primary"><i class="fas fa-search"></i> Search</button>
      <a href="{{ url_for('tasks') }}" class="btn btn-secondary">Clear</a>
    </form>
  </div>
</div>
{% if tasks_list %}
{% for t in tasks_list %}
<div class="task-card">
  <div style="display:flex;justify-content:space-between;align-items:start;gap:16px">
    <div style="flex:1">
      <a href="{{ url_for('task_detail', task_id=t.id) }}" style="color:var(--text)">
        <div class="task-title">{{ t.title }}</div>
      </a>
      <div class="task-meta">
        <span><i class="fas fa-tag"></i> {{ t.category or 'General' }}</span>
        <span><i class="fas fa-user"></i> {{ t.poster.name }}</span>
        <span><i class="fas fa-calendar"></i> {{ t.created_at.strftime('%b %d, %Y') }}</span>
        <span><i class="fas fa-gavel"></i> {{ t.bids|length }} bids</span>
      </div>
      <p class="text-sm text-muted">{{ t.description[:140] }}{% if t.description|length > 140 %}…{% endif %}</p>
    </div>
    <div style="text-align:right;flex-shrink:0">
      <div class="task-budget">KES {{ t.budget|int }}</div>
      <a href="{{ url_for('task_detail', task_id=t.id) }}" class="btn btn-sm btn-primary mt-4">
        View <i class="fas fa-arrow-right"></i>
      </a>
    </div>
  </div>
</div>
{% endfor %}
<div class="pagination mt-4">
  {% if page > 1 %}<a href="?page={{ page-1 }}&q={{ q }}&category={{ category }}" class="page-btn">← Prev</a>{% endif %}
  {% for p in range(1, total_pages+1) %}
    <a href="?page={{ p }}&q={{ q }}&category={{ category }}" class="page-btn {{ 'active' if p == page }}">{{ p }}</a>
  {% endfor %}
  {% if page < total_pages %}<a href="?page={{ page+1 }}&q={{ q }}&category={{ category }}" class="page-btn">Next →</a>{% endif %}
</div>
{% else %}
<div class="empty-state"><i class="fas fa-search"></i><p>No tasks found</p>
<a href="{{ url_for('post_task') }}" class="btn btn-primary mt-4">Post the first task</a></div>
{% endif %}
{% endblock %}'''
    return render_template_string(tpl, tasks_list=tasks_list, page=page, total_pages=total_pages,
                                  categories=categories, category=category, q=q)


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    task = Task.query.get_or_404(task_id)
    user_bid = Bid.query.filter_by(task_id=task_id, bidder_id=g.user.id).first()

    tpl = BASE_HTML + '''
{% block page_title %}Task Detail{% endblock %}
{% block content %}
<div class="grid-2" style="grid-template-columns:2fr 1fr">
  <div>
    <div class="card mb-4">
      <div class="card-header">
        <div>
          <h2>{{ task.title }}</h2>
          <div class="task-meta mt-2">
            <span class="badge badge-{{ 'success' if task.status == 'open' else 'warning' if task.status == 'assigned' else 'info' if task.status == 'completed' else 'neutral' }}">{{ task.status|upper }}</span>
            <span class="text-muted text-sm"><i class="fas fa-tag"></i> {{ task.category or 'General' }}</span>
            <span class="text-muted text-sm"><i class="fas fa-calendar"></i> {{ task.created_at.strftime('%b %d, %Y') }}</span>
          </div>
        </div>
        <div class="task-budget">KES {{ task.budget|int }}</div>
      </div>
      <div class="card-body">
        <p style="line-height:1.7;white-space:pre-wrap">{{ task.description }}</p>
        <div class="mt-4 text-sm text-muted">
          Posted by: <strong>{{ task.poster.name }}</strong>
        </div>
      </div>
    </div>
    {% if task.status == 'open' and task.poster_id != current_user.id %}
    <div class="card mb-4">
      <div class="card-header"><h2>Place a Bid</h2></div>
      <div class="card-body">
        {% if user_bid %}
        <div class="alert alert-info"><i class="fas fa-check"></i> You already bid KES {{ user_bid.amount|int }} on this task ({{ user_bid.status }})</div>
        {% else %}
        <form method="POST" action="{{ url_for('place_bid', task_id=task.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
          <div class="grid-2">
            <div class="form-group">
              <label class="form-label">Your Bid (KES)</label>
              <input type="number" name="amount" class="form-control" placeholder="{{ task.budget|int }}" min="1" required/>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Message to Poster</label>
            <textarea name="message" class="form-control" rows="3" placeholder="Describe your approach, experience, and timeline..."></textarea>
          </div>
          <button type="submit" class="btn btn-primary">Submit Bid <i class="fas fa-paper-plane"></i></button>
        </form>
        {% endif %}
      </div>
    </div>
    {% endif %}
    <!-- Bids -->
    {% if task.poster_id == current_user.id or current_user.is_admin %}
    <div class="card">
      <div class="card-header"><h2>Bids ({{ task.bids|length }})</h2></div>
      <div class="card-body" style="padding:0">
        {% for bid in task.bids|sort(attribute='created_at', reverse=True) %}
        <div style="padding:16px 20px;border-bottom:1px solid var(--border)">
          <div style="display:flex;justify-content:space-between;align-items:start">
            <div>
              <div style="font-weight:600">{{ bid.bidder.name }}</div>
              <div class="text-sm text-muted mt-2">{{ bid.message or 'No message' }}</div>
              <div class="text-xs text-muted mt-2">{{ bid.created_at.strftime('%b %d, %H:%M') }}</div>
            </div>
            <div style="text-align:right">
              <div class="monospace" style="font-weight:800;color:var(--accent)">KES {{ bid.amount|int }}</div>
              {% if task.status == 'open' and task.poster_id == current_user.id %}
              <a href="{{ url_for('accept_bid', bid_id=bid.id) }}" class="btn btn-sm btn-success mt-2">Accept</a>
              {% endif %}
              <span class="badge badge-{{ 'success' if bid.status == 'accepted' else 'neutral' }} mt-2">{{ bid.status }}</span>
            </div>
          </div>
        </div>
        {% else %}
        <div class="empty-state" style="padding:30px"><i class="fas fa-inbox"></i>No bids yet</div>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  </div>
  <div>
    {% if task.worker_id == current_user.id and task.status == 'assigned' %}
    <div class="card mb-4">
      <div class="card-body" style="text-align:center">
        <i class="fas fa-circle-check" style="font-size:36px;color:var(--success);margin-bottom:12px"></i>
        <h3>You're working on this!</h3>
        <p class="text-sm text-muted mt-2">Complete the work and submit for review.</p>
        <a href="{{ url_for('complete_task', task_id=task.id) }}" class="btn btn-success mt-4" style="width:100%;justify-content:center">Mark Completed</a>
      </div>
    </div>
    {% endif %}
    {% if task.poster_id == current_user.id %}
    <div class="card">
      <div class="card-header"><h2>Task Actions</h2></div>
      <div class="card-body">
        {% if task.status == 'open' %}
        <a href="{{ url_for('cancel_task', task_id=task.id) }}" class="btn btn-danger" style="width:100%;justify-content:center">Cancel Task</a>
        {% elif task.status == 'assigned' %}
        <a href="{{ url_for('force_complete_task', task_id=task.id) }}" class="btn btn-success" style="width:100%;justify-content:center">Force Complete</a>
        {% endif %}
      </div>
    </div>
    {% endif %}
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, task=task, user_bid=user_bid)


@app.route("/tasks/post", methods=["GET", "POST"])
@login_required
def post_task():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "")
        budget = float(request.form.get("budget", 0))
        fee_pct = float(get_setting("platform_fee_percent", 10))
        fee = budget * fee_pct / 100

        if g.user.available_balance < budget + fee:
            flash(f"Insufficient balance. Need KES {budget + fee:.2f} (budget + {fee_pct}% fee).", "danger")
            return redirect(url_for("post_task"))

        task = Task(title=title, description=description, category=category,
                    budget=budget, poster_id=g.user.id, platform_fee=fee)
        debit_wallet(g.user, budget + fee, "escrow_hold",
                     f"Escrow hold for task: {title}", )
        g.user.held_balance += budget
        db.session.add(task)
        db.session.commit()
        flash("Task posted successfully!", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    categories = ["Writing", "Design", "Tech", "Marketing", "Data Entry", "Translation", "Other"]
    fee_pct = float(get_setting("platform_fee_percent", 10))

    tpl = BASE_HTML + '''
{% block page_title %}Post a Task{% endblock %}
{% block content %}
<div style="max-width:680px;margin:0 auto">
  <div class="card">
    <div class="card-header"><h2>Post a New Task</h2></div>
    <div class="card-body">
      <div class="alert alert-info">
        <i class="fas fa-info-circle"></i>
        A platform fee of <strong>{{ fee_pct }}%</strong> is held in escrow and deducted when the task is complete.
      </div>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
        <div class="form-group">
          <label class="form-label">Task Title</label>
          <input type="text" name="title" class="form-control" placeholder="e.g. Write a 500-word blog post" required/>
        </div>
        <div class="form-group">
          <label class="form-label">Description</label>
          <textarea name="description" class="form-control" rows="6" placeholder="Describe the task in detail, requirements, deliverables..." required></textarea>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label class="form-label">Category</label>
            <select name="category" class="form-control">
              {% for c in categories %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Budget (KES)</label>
            <input type="number" name="budget" class="form-control" placeholder="500" min="50" required/>
          </div>
        </div>
        <div class="alert alert-warning mt-2">
          <i class="fas fa-wallet"></i> Your balance: <strong>KES {{ "%.2f"|format(current_user.available_balance) }}</strong>
        </div>
        <button type="submit" class="btn btn-primary">Post Task <i class="fas fa-rocket"></i></button>
      </form>
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, categories=categories, fee_pct=fee_pct)


@app.route("/tasks/<int:task_id>/bid", methods=["POST"])
@login_required
def place_bid(task_id):
    task = Task.query.get_or_404(task_id)
    if task.status != "open" or task.poster_id == g.user.id:
        flash("Cannot bid on this task.", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    if Bid.query.filter_by(task_id=task_id, bidder_id=g.user.id).first():
        flash("You already placed a bid.", "warning")
        return redirect(url_for("task_detail", task_id=task_id))
    amount = float(request.form.get("amount", task.budget))
    message = request.form.get("message", "")
    db.session.add(Bid(task_id=task_id, bidder_id=g.user.id, amount=amount, message=message))
    db.session.commit()
    add_notification(task.poster_id, "New Bid!", f"{g.user.name} bid KES {amount:.0f} on '{task.title}'", "info", url_for("task_detail", task_id=task_id))
    db.session.commit()
    flash("Bid placed!", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/bids/<int:bid_id>/accept")
@login_required
def accept_bid(bid_id):
    bid = Bid.query.get_or_404(bid_id)
    task = bid.task
    if task.poster_id != g.user.id or task.status != "open":
        flash("Not authorized.", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    task.status = "assigned"
    task.worker_id = bid.bidder_id
    bid.status = "accepted"
    add_notification(bid.bidder_id, "Bid Accepted!", f"Your bid on '{task.title}' was accepted.", "success", url_for("task_detail", task_id=task.id))
    db.session.commit()
    flash("Bid accepted! Task assigned.", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/complete")
@login_required
def complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.worker_id != g.user.id or task.status != "assigned":
        flash("Not authorized.", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    return _do_complete_task(task)


def _do_complete_task(task):
    worker = User.query.get(task.worker_id)
    task.status = "completed"
    task.completed_at = datetime.utcnow()
    fee_pct = float(get_setting("platform_fee_percent", 10))
    fee = task.budget * fee_pct / 100
    earned = task.budget - fee
    task.poster.held_balance -= task.budget
    credit_wallet(worker, earned, "task_earning", f"Earned from task: {task.title}", str(task.id))
    # First task bonus
    if not worker.first_task_bonus_paid and worker.referred_by_id:
        bonus = float(get_setting("referral_first_task_bonus", 0))
        if bonus > 0:
            referrer = User.query.get(worker.referred_by_id)
            if referrer:
                credit_wallet(referrer, bonus, "bonus", f"First-task referral bonus from {worker.name}")
                add_notification(referrer.id, "Referral Bonus!", f"{worker.name} completed their first task. You earned KES {bonus}!", "success")
        worker.first_task_bonus_paid = True
    add_notification(task.poster_id, "Task Completed", f"'{task.title}' was marked complete by {worker.name}.", "success")
    add_notification(worker.id, "Payment Received", f"KES {earned:.2f} credited for '{task.title}'.", "success")
    db.session.commit()
    flash(f"Task completed! KES {earned:.2f} credited to your wallet.", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/cancel")
@login_required
def cancel_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.poster_id != g.user.id or task.status not in ("open", "assigned"):
        flash("Cannot cancel this task.", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    task.status = "cancelled"
    credit_wallet(g.user, task.budget, "refund", f"Refund for cancelled task: {task.title}", str(task_id))
    g.user.held_balance -= task.budget
    db.session.commit()
    flash("Task cancelled. Budget refunded.", "success")
    return redirect(url_for("my_tasks"))


@app.route("/tasks/<int:task_id>/force-complete")
@login_required
def force_complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.poster_id != g.user.id and not g.user.is_admin:
        abort(403)
    return _do_complete_task(task)


@app.route("/my-tasks")
@login_required
def my_tasks():
    posted = Task.query.filter_by(poster_id=g.user.id).order_by(desc(Task.created_at)).all()
    working = Task.query.filter_by(worker_id=g.user.id).order_by(desc(Task.created_at)).all()

    tpl = BASE_HTML + '''
{% block page_title %}My Tasks{% endblock %}
{% block content %}
<div class="grid-2">
  <div class="card">
    <div class="card-header"><h2>Tasks I Posted</h2><span class="badge badge-info">{{ posted|length }}</span></div>
    <div class="card-body" style="padding:0">
      {% for t in posted %}
      <a href="{{ url_for('task_detail', task_id=t.id) }}" style="display:block;padding:14px 20px;border-bottom:1px solid var(--border);color:var(--text)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <div style="font-weight:600;font-size:14px">{{ t.title[:45] }}</div>
            <div class="text-xs text-muted mt-2">{{ t.bids|length }} bids · {{ t.created_at.strftime('%b %d') }}</div>
          </div>
          <span class="badge badge-{{ 'success' if t.status == 'open' else 'warning' if t.status == 'assigned' else 'info' if t.status == 'completed' else 'danger' }}">{{ t.status }}</span>
        </div>
      </a>
      {% else %}<div class="empty-state" style="padding:30px"><i class="fas fa-inbox"></i>No posted tasks</div>{% endfor %}
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h2>Tasks I'm Working</h2><span class="badge badge-success">{{ working|length }}</span></div>
    <div class="card-body" style="padding:0">
      {% for t in working %}
      <a href="{{ url_for('task_detail', task_id=t.id) }}" style="display:block;padding:14px 20px;border-bottom:1px solid var(--border);color:var(--text)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <div style="font-weight:600;font-size:14px">{{ t.title[:45] }}</div>
            <div class="text-xs text-muted mt-2">KES {{ t.budget|int }} · {{ t.created_at.strftime('%b %d') }}</div>
          </div>
          <span class="badge badge-{{ 'success' if t.status == 'open' else 'warning' if t.status == 'assigned' else 'info' if t.status == 'completed' else 'danger' }}">{{ t.status }}</span>
        </div>
      </a>
      {% else %}<div class="empty-state" style="padding:30px"><i class="fas fa-inbox"></i>No active work</div>{% endfor %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, posted=posted, working=working)


# ─── Routes: Wallet & Payments ────────────────────────────────────────────────

@app.route("/wallet")
@login_required
def wallet():
    user = g.user
    pending_payments = Payment.query.filter_by(user_id=user.id, status="pending").count()

    tpl = BASE_HTML + '''
{% block page_title %}Wallet{% endblock %}
{% block content %}
<div class="stats-grid" style="grid-template-columns:repeat(3,1fr)">
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(34,197,94,.15);color:var(--success)"><i class="fas fa-circle-check"></i></div>
    <div class="stat-body">
      <div class="stat-label">Available</div>
      <div class="stat-value monospace">KES {{ "%.2f"|format(user.available_balance) }}</div>
      <div class="stat-sub">Ready to use</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(234,179,8,.15);color:var(--warning)"><i class="fas fa-lock"></i></div>
    <div class="stat-body">
      <div class="stat-label">Held (Escrow)</div>
      <div class="stat-value monospace">KES {{ "%.2f"|format(user.held_balance) }}</div>
      <div class="stat-sub">In active tasks</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(99,102,241,.15);color:var(--accent2)"><i class="fas fa-sigma"></i></div>
    <div class="stat-body">
      <div class="stat-label">Total Balance</div>
      <div class="stat-value monospace">KES {{ "%.2f"|format(user.total_balance) }}</div>
      <div class="stat-sub">Available + Held</div>
    </div>
  </div>
</div>
{% if pending_payments > 0 %}
<div class="alert alert-warning"><i class="fas fa-clock"></i> You have {{ pending_payments }} pending payment(s). They will be confirmed shortly.</div>
{% endif %}
<div class="flex gap-3 mb-4" style="flex-wrap:wrap">
  <a href="{{ url_for('deposit') }}" class="btn btn-primary"><i class="fas fa-plus"></i> Deposit</a>
  <a href="{{ url_for('withdraw') }}" class="btn btn-secondary"><i class="fas fa-arrow-up"></i> Withdraw</a>
  <a href="{{ url_for('transactions') }}" class="btn btn-secondary"><i class="fas fa-history"></i> Full History</a>
</div>
{% endblock %}'''
    return render_template_string(tpl, user=user, pending_payments=pending_payments)


@app.route("/deposit", methods=["GET", "POST"])
@login_required
def deposit():
    if request.method == "POST":
        amount = float(request.form.get("amount", 0))
        phone = request.form.get("phone", "").strip()
        if amount < 10:
            flash("Minimum deposit is KES 10.", "danger")
            return redirect(url_for("deposit"))

        invoice_id = f"TH-{secrets.token_hex(6).upper()}"
        payment = Payment(user_id=g.user.id, invoice_id=invoice_id,
                          amount=amount, phone=phone, status="pending")
        db.session.add(payment)
        db.session.commit()

        if INTASEND_API_KEY:
            result = intasend_stk_push(phone, amount, invoice_id, g.user.email)
            if result:
                payment.tracking_id = result.get("id") or result.get("invoice", {}).get("id", "")
                db.session.commit()
                flash(f"M-Pesa STK push sent to {phone}. Enter your PIN to complete.", "success")
            else:
                flash("Payment initiation failed. Try again.", "danger")
        else:
            # Sandbox: auto-credit for testing
            payment.status = "completed"
            credit_wallet(g.user, amount, "deposit", f"Deposit #{invoice_id}", invoice_id)
            if not g.user.first_deposit_bonus_paid and g.user.referred_by_id:
                bonus = float(get_setting("referral_first_deposit_bonus", 0))
                if bonus > 0:
                    referrer = User.query.get(g.user.referred_by_id)
                    if referrer:
                        credit_wallet(referrer, bonus, "bonus", f"First-deposit referral bonus from {g.user.name}")
                        add_notification(referrer.id, "Referral Bonus!", f"{g.user.name} made their first deposit. You earned KES {bonus}!", "success")
                g.user.first_deposit_bonus_paid = True
            add_notification(g.user.id, "Deposit Confirmed", f"KES {amount:.2f} added to your wallet.", "success")
            db.session.commit()
            flash(f"KES {amount:.2f} deposited successfully (test mode)!", "success")
        return redirect(url_for("wallet"))

    tpl = BASE_HTML + '''
{% block page_title %}Deposit Funds{% endblock %}
{% block content %}
<div style="max-width:500px;margin:0 auto">
  <div class="card">
    <div class="card-header"><h2><i class="fas fa-mobile-screen-button" style="color:var(--success)"></i> M-Pesa Deposit</h2></div>
    <div class="card-body">
      <div class="alert alert-info"><i class="fas fa-info-circle"></i> Enter your M-Pesa number. You'll receive a PIN prompt on your phone.</div>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
        <div class="form-group">
          <label class="form-label">M-Pesa Phone Number</label>
          <input type="tel" name="phone" class="form-control" placeholder="e.g. 0712345678" required/>
        </div>
        <div class="form-group">
          <label class="form-label">Amount (KES)</label>
          <input type="number" name="amount" class="form-control" placeholder="500" min="10" required/>
        </div>
        <button type="submit" class="btn btn-success" style="width:100%;justify-content:center">
          <i class="fas fa-mobile-screen-button"></i> Send STK Push
        </button>
      </form>
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl)


@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw():
    if not get_setting("withdrawals_enabled", True):
        flash("Withdrawals are currently disabled.", "warning")
        return redirect(url_for("wallet"))
    min_w = float(get_setting("min_withdrawal", 200))
    max_w = float(get_setting("max_withdrawal", 50000))

    if request.method == "POST":
        amount = float(request.form.get("amount", 0))
        phone = request.form.get("phone", "").strip()
        if amount < min_w:
            flash(f"Minimum withdrawal is KES {min_w:.0f}.", "danger")
        elif amount > max_w:
            flash(f"Maximum withdrawal is KES {max_w:.0f}.", "danger")
        elif g.user.available_balance < amount:
            flash("Insufficient balance.", "danger")
        else:
            debit_wallet(g.user, amount, "withdrawal", f"Withdrawal request KES {amount:.2f}")
            db.session.add(Withdrawal(user_id=g.user.id, amount=amount, phone=phone))
            db.session.commit()
            flash("Withdrawal request submitted. Processing within 24h.", "success")
            return redirect(url_for("wallet"))

    tpl = BASE_HTML + '''
{% block page_title %}Withdraw Funds{% endblock %}
{% block content %}
<div style="max-width:500px;margin:0 auto">
  <div class="card">
    <div class="card-header"><h2><i class="fas fa-money-bill-transfer" style="color:var(--accent)"></i> Withdraw</h2></div>
    <div class="card-body">
      <div class="alert alert-info"><i class="fas fa-info-circle"></i>
        Min: KES {{ min_w|int }} · Max: KES {{ max_w|int }} · Available: <strong>KES {{ "%.2f"|format(current_user.available_balance) }}</strong>
      </div>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
        <div class="form-group">
          <label class="form-label">M-Pesa Number</label>
          <input type="tel" name="phone" class="form-control" placeholder="0712345678" required/>
        </div>
        <div class="form-group">
          <label class="form-label">Amount (KES)</label>
          <input type="number" name="amount" class="form-control" placeholder="{{ min_w|int }}" min="{{ min_w|int }}" max="{{ max_w|int }}" required/>
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%;justify-content:center">Request Withdrawal</button>
      </form>
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, min_w=min_w, max_w=max_w)


@app.route("/transactions")
@login_required
def transactions():
    page = request.args.get("page", 1, type=int)
    per_page = 20
    entries = LedgerEntry.query.filter_by(user_id=g.user.id).order_by(desc(LedgerEntry.created_at))
    total = entries.count()
    entries = entries.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = ceil(total / per_page) if total else 1

    type_colors = {
        "deposit": "success", "withdrawal": "danger", "task_earning": "success",
        "escrow_hold": "warning", "escrow_release": "info", "bonus": "info",
        "refund": "warning"
    }

    tpl = BASE_HTML + '''
{% block page_title %}Transaction History{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2>Transaction History</h2><span class="badge badge-neutral">{{ total }} entries</span></div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Date</th><th>Type</th><th>Description</th><th>Amount</th><th>Balance After</th>
      </tr></thead>
      <tbody>
      {% for e in entries %}
      <tr>
        <td class="text-sm text-muted">{{ e.created_at.strftime('%b %d, %Y %H:%M') }}</td>
        <td><span class="badge badge-{{ type_colors.get(e.entry_type, 'neutral') }}">{{ e.entry_type.replace('_',' ') }}</span></td>
        <td class="text-sm">{{ e.description }}</td>
        <td class="monospace {{ 'badge-success' if e.amount > 0 else 'badge-danger' }}" style="font-weight:700">
          {{ '+' if e.amount > 0 else '' }}{{ "%.2f"|format(e.amount) }}
        </td>
        <td class="monospace text-sm">{{ "%.2f"|format(e.balance_after) }}</td>
      </tr>
      {% else %}
      <tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text3)">No transactions yet</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div style="padding:16px;border-top:1px solid var(--border)">
    <div class="pagination">
      {% if page > 1 %}<a href="?page={{ page-1 }}" class="page-btn">← Prev</a>{% endif %}
      {% for p in range(1, total_pages+1) %}
        <a href="?page={{ p }}" class="page-btn {{ 'active' if p == page }}">{{ p }}</a>
      {% endfor %}
      {% if page < total_pages %}<a href="?page={{ page+1 }}" class="page-btn">Next →</a>{% endif %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, entries=entries, page=page, total_pages=total_pages,
                                  total=total, type_colors=type_colors)


# ─── IntaSend Webhook ─────────────────────────────────────────────────────────

@app.route("/payment/callback", methods=["GET"])
def payment_callback():
    invoice_id = request.args.get("invoice_id") or request.args.get("api_ref")
    state = request.args.get("state", "")
    if invoice_id and state.upper() in ("COMPLETE", "COMPLETED"):
        payment = Payment.query.filter_by(invoice_id=invoice_id).first()
        if payment and payment.status == "pending":
            payment.status = "completed"
            user = User.query.get(payment.user_id)
            credit_wallet(user, payment.amount, "deposit", f"M-Pesa deposit #{invoice_id}", invoice_id)
            # First deposit bonus
            if not user.first_deposit_bonus_paid and user.referred_by_id:
                bonus = float(get_setting("referral_first_deposit_bonus", 0))
                if bonus > 0:
                    referrer = User.query.get(user.referred_by_id)
                    if referrer:
                        credit_wallet(referrer, bonus, "bonus", f"First-deposit bonus from {user.name}")
                        add_notification(referrer.id, "Referral Bonus!", f"{user.name} made their first deposit. You earned KES {bonus}!", "success")
                user.first_deposit_bonus_paid = True
            add_notification(user.id, "Payment Confirmed", f"KES {payment.amount:.2f} credited to your wallet.", "success")
            db.session.commit()
    return redirect(url_for("wallet"))


@app.route("/webhooks/intasend", methods=["POST"])
@csrf.exempt
def intasend_webhook():
    payload = request.get_data()
    signature = request.headers.get("X-IntaSend-Signature", "")
    if INTASEND_API_KEY and not verify_intasend_webhook(payload, signature):
        logger.warning("Invalid IntaSend webhook signature")
        return jsonify({"error": "Invalid signature"}), 401
    try:
        data = request.get_json(force=True) or {}
        invoice_id = data.get("api_ref") or data.get("invoice_id")
        state = str(data.get("state", "")).upper()
        if invoice_id and state in ("COMPLETE", "COMPLETED", "SUCCESS"):
            payment = Payment.query.filter_by(invoice_id=invoice_id).first()
            if payment and payment.status == "pending":
                payment.status = "completed"
                user = User.query.get(payment.user_id)
                credit_wallet(user, payment.amount, "deposit", f"M-Pesa deposit #{invoice_id}", invoice_id)
                if not user.first_deposit_bonus_paid and user.referred_by_id:
                    bonus = float(get_setting("referral_first_deposit_bonus", 0))
                    if bonus > 0:
                        referrer = User.query.get(user.referred_by_id)
                        if referrer:
                            credit_wallet(referrer, bonus, "bonus", f"First-deposit bonus from {user.name}")
                user.first_deposit_bonus_paid = True
                add_notification(user.id, "Deposit Confirmed", f"KES {payment.amount:.2f} credited.", "success")
                db.session.commit()
        elif invoice_id and state in ("FAILED", "CANCELLED"):
            payment = Payment.query.filter_by(invoice_id=invoice_id).first()
            if payment:
                payment.status = "failed"
                db.session.commit()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return jsonify({"status": "ok"})


# ─── Routes: Referrals ────────────────────────────────────────────────────────

@app.route("/referrals")
@login_required
def referrals():
    user = g.user
    referred_users = User.query.filter_by(referred_by_id=user.id).all()
    bonuses = ReferralBonus.query.filter_by(referrer_id=user.id).all()
    total_earned = sum(b.amount for b in bonuses if b.paid)
    ref_link = request.host_url.rstrip("/") + url_for("register") + f"?ref={user.referral_code}"
    signup_bonus = float(get_setting("referral_signup_bonus", 0))
    first_task_bonus = float(get_setting("referral_first_task_bonus", 0))
    first_deposit_bonus = float(get_setting("referral_first_deposit_bonus", 0))

    tpl = BASE_HTML + '''
{% block page_title %}Referrals{% endblock %}
{% block content %}
<div class="stats-grid" style="grid-template-columns:repeat(3,1fr)">
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(249,115,22,.15);color:var(--accent)"><i class="fas fa-users"></i></div>
    <div class="stat-body">
      <div class="stat-label">Total Referrals</div>
      <div class="stat-value">{{ referred_users|length }}</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(34,197,94,.15);color:var(--success)"><i class="fas fa-coins"></i></div>
    <div class="stat-body">
      <div class="stat-label">Total Earned</div>
      <div class="stat-value monospace">KES {{ "%.2f"|format(total_earned) }}</div>
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(99,102,241,.15);color:var(--accent2)"><i class="fas fa-gift"></i></div>
    <div class="stat-body">
      <div class="stat-label">Signup Bonus</div>
      <div class="stat-value monospace">KES {{ signup_bonus|int }}</div>
      <div class="stat-sub">Per referral</div>
    </div>
  </div>
</div>
<div class="card mb-4">
  <div class="card-header"><h2><i class="fas fa-link" style="color:var(--accent)"></i> Your Referral Link</h2></div>
  <div class="card-body">
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <input type="text" value="{{ ref_link }}" class="form-control monospace" readonly style="flex:1" id="refLink"/>
      <button class="btn btn-primary" onclick="copyRef()"><i class="fas fa-copy"></i> Copy</button>
    </div>
    <div class="mt-4 flex gap-3" style="flex-wrap:wrap">
      <div class="stat-card" style="flex:1;min-width:160px;padding:14px">
        <div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase">Signup Bonus</div>
        <div style="font-size:18px;font-weight:800;color:var(--accent)">KES {{ signup_bonus|int }}</div>
      </div>
      <div class="stat-card" style="flex:1;min-width:160px;padding:14px">
        <div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase">First Task</div>
        <div style="font-size:18px;font-weight:800;color:var(--success)">KES {{ first_task_bonus|int }}</div>
      </div>
      <div class="stat-card" style="flex:1;min-width:160px;padding:14px">
        <div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase">First Deposit</div>
        <div style="font-size:18px;font-weight:800;color:var(--accent2)">KES {{ first_deposit_bonus|int }}</div>
      </div>
    </div>
  </div>
</div>
<div class="card">
  <div class="card-header"><h2>Referred Users</h2></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Joined</th><th>First Task</th><th>First Deposit</th></tr></thead>
      <tbody>
      {% for u in referred_users %}
      <tr>
        <td><div style="font-weight:600">{{ u.name }}</div><div class="text-xs text-muted">{{ u.email }}</div></td>
        <td class="text-sm">{{ u.created_at.strftime('%b %d, %Y') }}</td>
        <td><span class="badge badge-{{ 'success' if u.first_task_bonus_paid else 'neutral' }}">{{ 'Done' if u.first_task_bonus_paid else 'Pending' }}</span></td>
        <td><span class="badge badge-{{ 'success' if u.first_deposit_bonus_paid else 'neutral' }}">{{ 'Done' if u.first_deposit_bonus_paid else 'Pending' }}</span></td>
      </tr>
      {% else %}
      <tr><td colspan="4" style="text-align:center;padding:40px;color:var(--text3)">No referrals yet. Share your link!</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
<script>
function copyRef(){
  navigator.clipboard.writeText(document.getElementById('refLink').value);
  const btn = event.target.closest('button');
  btn.innerHTML='<i class="fas fa-check"></i> Copied!';
  setTimeout(()=>btn.innerHTML='<i class="fas fa-copy"></i> Copy',2000);
}
</script>
{% endblock %}'''
    return render_template_string(tpl, referred_users=referred_users, total_earned=total_earned,
                                  ref_link=ref_link, signup_bonus=signup_bonus,
                                  first_task_bonus=first_task_bonus, first_deposit_bonus=first_deposit_bonus)


# ─── Routes: Notifications ────────────────────────────────────────────────────

@app.route("/notifications")
@login_required
def notifications_page():
    notifs = Notification.query.filter_by(user_id=g.user.id).order_by(desc(Notification.created_at)).all()
    Notification.query.filter_by(user_id=g.user.id, is_read=False).update({"is_read": True})
    db.session.commit()

    tpl = BASE_HTML + '''
{% block page_title %}Notifications{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2>All Notifications</h2>
  <a href="{{ url_for('mark_all_read') }}" class="btn btn-sm btn-secondary">Mark all read</a></div>
  <div class="card-body" style="padding:0">
    {% for n in notifs %}
    <div style="padding:16px 20px;border-bottom:1px solid var(--border);{{ 'background:rgba(249,115,22,.04)' if not n.is_read else '' }}">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div>
          <div style="font-weight:600;font-size:14px">{{ n.title }}</div>
          <div class="text-sm text-muted mt-2">{{ n.message }}</div>
        </div>
        <div class="text-xs text-muted" style="flex-shrink:0;margin-left:16px">{{ n.created_at.strftime('%b %d, %H:%M') }}</div>
      </div>
    </div>
    {% else %}
    <div class="empty-state" style="padding:60px"><i class="fas fa-bell-slash"></i><p>No notifications</p></div>
    {% endfor %}
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, notifs=notifs)


@app.route("/notifications/mark-read/<int:nid>")
@login_required
def mark_read(nid):
    n = Notification.query.get_or_404(nid)
    if n.user_id == g.user.id:
        n.is_read = True
        db.session.commit()
    return redirect(n.link or url_for("notifications_page"))


@app.route("/notifications/mark-all-read")
@login_required
def mark_all_read():
    Notification.query.filter_by(user_id=g.user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return redirect(url_for("notifications_page"))


# ─── Routes: Profile ──────────────────────────────────────────────────────────

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        new_password = request.form.get("new_password", "")
        if name:
            g.user.name = name
        if new_password:
            if len(new_password) < 8:
                flash("Password must be 8+ characters.", "danger")
                return redirect(url_for("profile"))
            g.user.set_password(new_password)
        db.session.commit()
        flash("Profile updated!", "success")
        return redirect(url_for("profile"))

    tpl = BASE_HTML + '''
{% block page_title %}Profile{% endblock %}
{% block content %}
<div style="max-width:560px;margin:0 auto">
  <div class="card">
    <div class="card-header"><h2>Edit Profile</h2></div>
    <div class="card-body">
      <div style="text-align:center;margin-bottom:24px">
        <div class="avatar" style="width:72px;height:72px;border-radius:50%;background:var(--accent);display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:28px;font-weight:700;overflow:hidden">
          {% if current_user.profile_picture %}
          <img src="{{ current_user.profile_picture }}" style="width:100%;height:100%;object-fit:cover"/>
          {% else %}{{ current_user.name[0].upper() }}{% endif %}
        </div>
        <div style="font-size:18px;font-weight:700;margin-top:10px">{{ current_user.name }}</div>
        <div class="text-sm text-muted">{{ current_user.email }}</div>
        <div class="text-xs text-muted mt-2">Member since {{ current_user.created_at.strftime('%B %Y') }}</div>
      </div>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
        <div class="form-group">
          <label class="form-label">Full Name</label>
          <input type="text" name="name" value="{{ current_user.name }}" class="form-control"/>
        </div>
        <div class="form-group">
          <label class="form-label">New Password <span class="text-muted">(leave blank to keep)</span></label>
          <input type="password" name="new_password" class="form-control" placeholder="Min 8 characters"/>
        </div>
        <button type="submit" class="btn btn-primary">Save Changes</button>
      </form>
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl)


# ─── Admin Routes ─────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "users": User.query.count(),
        "tasks": Task.query.count(),
        "open_tasks": Task.query.filter_by(status="open").count(),
        "payments": Payment.query.filter_by(status="completed").count(),
        "total_deposited": db.session.query(func.sum(Payment.amount)).filter_by(status="completed").scalar() or 0,
        "pending_withdrawals": Withdrawal.query.filter_by(status="pending").count(),
    }

    tpl = BASE_HTML + '''
{% block page_title %}Admin Dashboard{% endblock %}
{% block content %}
<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(99,102,241,.15);color:var(--accent2)"><i class="fas fa-users"></i></div>
    <div class="stat-body"><div class="stat-label">Total Users</div><div class="stat-value">{{ stats.users }}</div></div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(249,115,22,.15);color:var(--accent)"><i class="fas fa-list-check"></i></div>
    <div class="stat-body"><div class="stat-label">Tasks</div><div class="stat-value">{{ stats.tasks }}</div><div class="stat-sub">{{ stats.open_tasks }} open</div></div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(34,197,94,.15);color:var(--success)"><i class="fas fa-money-bill"></i></div>
    <div class="stat-body"><div class="stat-label">Total Deposited</div><div class="stat-value monospace">KES {{ "%.0f"|format(stats.total_deposited) }}</div></div>
  </div>
  <div class="stat-card">
    <div class="stat-icon" style="background:rgba(234,179,8,.15);color:var(--warning)"><i class="fas fa-clock"></i></div>
    <div class="stat-body"><div class="stat-label">Pending Withdrawals</div><div class="stat-value">{{ stats.pending_withdrawals }}</div></div>
  </div>
</div>
<div class="flex gap-3" style="flex-wrap:wrap">
  <a href="{{ url_for('admin_users') }}" class="btn btn-primary"><i class="fas fa-users"></i> Users</a>
  <a href="{{ url_for('admin_tasks') }}" class="btn btn-secondary"><i class="fas fa-list-check"></i> Tasks</a>
  <a href="{{ url_for('admin_payments') }}" class="btn btn-secondary"><i class="fas fa-credit-card"></i> Payments</a>
  <a href="{{ url_for('admin_withdrawals') }}" class="btn btn-secondary"><i class="fas fa-money-bill-transfer"></i> Withdrawals</a>
  <a href="{{ url_for('admin_settings') }}" class="btn btn-secondary"><i class="fas fa-gear"></i> Settings</a>
</div>
{% endblock %}'''
    return render_template_string(tpl, stats=stats)


@app.route("/admin/users")
@admin_required
def admin_users():
    page = request.args.get("page", 1, type=int)
    per_page = 20
    q = request.args.get("q", "")
    query = User.query
    if q:
        query = query.filter(User.email.ilike(f"%{q}%") | User.name.ilike(f"%{q}%"))
    total = query.count()
    users = query.order_by(desc(User.created_at)).offset((page - 1) * per_page).limit(per_page).all()
    total_pages = ceil(total / per_page) if total else 1

    tpl = BASE_HTML + '''
{% block page_title %}Admin — Users{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header">
    <h2>Users ({{ total }})</h2>
    <form method="GET" style="display:flex;gap:8px">
      <input type="text" name="q" value="{{ q }}" class="form-control" placeholder="Search..." style="width:220px"/>
      <button type="submit" class="btn btn-sm btn-primary">Search</button>
    </form>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Name / Email</th><th>Balance</th><th>Joined</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
      {% for u in users %}
      <tr>
        <td>
          <div style="font-weight:600">{{ u.name }} {% if u.is_admin %}<span class="badge badge-info">Admin</span>{% endif %}</div>
          <div class="text-xs text-muted">{{ u.email }}</div>
        </td>
        <td class="monospace text-sm">KES {{ "%.2f"|format(u.available_balance) }}</td>
        <td class="text-sm text-muted">{{ u.created_at.strftime('%b %d, %Y') }}</td>
        <td><span class="badge badge-{{ 'danger' if u.is_banned else 'success' }}">{{ 'Banned' if u.is_banned else 'Active' }}</span></td>
        <td>
          <div class="flex gap-2">
            <a href="{{ url_for('admin_toggle_ban', uid=u.id) }}" class="btn btn-sm {{ 'btn-success' if u.is_banned else 'btn-danger' }}">
              {{ 'Unban' if u.is_banned else 'Ban' }}
            </a>
            <a href="{{ url_for('admin_edit_user', uid=u.id) }}" class="btn btn-sm btn-secondary">Edit</a>
          </div>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div style="padding:16px">
    <div class="pagination">
      {% for p in range(1,total_pages+1) %}
      <a href="?page={{ p }}&q={{ q }}" class="page-btn {{ 'active' if p == page }}">{{ p }}</a>
      {% endfor %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, users=users, total=total, page=page, total_pages=total_pages, q=q)


@app.route("/admin/users/<int:uid>/toggle-ban")
@admin_required
def admin_toggle_ban(uid):
    user = User.query.get_or_404(uid)
    if user.id == g.user.id:
        flash("Cannot ban yourself.", "danger")
        return redirect(url_for("admin_users"))
    user.is_banned = not user.is_banned
    db.session.commit()
    flash(f"User {'banned' if user.is_banned else 'unbanned'}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_user(uid):
    user = User.query.get_or_404(uid)
    if request.method == "POST":
        balance = request.form.get("balance", "")
        new_password = request.form.get("new_password", "")
        is_admin = "is_admin" in request.form
        if balance:
            user.available_balance = float(balance)
        if new_password:
            user.set_password(new_password)
        user.is_admin = is_admin
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin_users"))

    tpl = BASE_HTML + '''
{% block page_title %}Edit User{% endblock %}
{% block content %}
<div style="max-width:500px;margin:0 auto">
  <div class="card">
    <div class="card-header"><h2>Edit {{ user.name }}</h2></div>
    <div class="card-body">
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
        <div class="form-group">
          <label class="form-label">Available Balance (KES)</label>
          <input type="number" name="balance" value="{{ user.available_balance }}" class="form-control" step="0.01"/>
        </div>
        <div class="form-group">
          <label class="form-label">Reset Password</label>
          <input type="password" name="new_password" class="form-control" placeholder="Leave blank to keep"/>
        </div>
        <div class="form-group" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" name="is_admin" id="is_admin" {{ 'checked' if user.is_admin }}/>
          <label for="is_admin" class="form-label" style="margin:0">Admin privileges</label>
        </div>
        <button type="submit" class="btn btn-primary">Save</button>
        <a href="{{ url_for('admin_users') }}" class="btn btn-secondary">Cancel</a>
      </form>
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, user=user)


@app.route("/admin/tasks")
@admin_required
def admin_tasks():
    page = request.args.get("page", 1, type=int)
    per_page = 20
    tasks = Task.query.order_by(desc(Task.created_at)).offset((page - 1) * per_page).limit(per_page).all()
    total = Task.query.count()
    total_pages = ceil(total / per_page) if total else 1

    tpl = BASE_HTML + '''
{% block page_title %}Admin — Tasks{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2>All Tasks ({{ total }})</h2></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Title</th><th>Poster</th><th>Budget</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
      {% for t in tasks %}
      <tr>
        <td><div style="font-weight:600;font-size:13px">{{ t.title[:50] }}</div><div class="text-xs text-muted">{{ t.created_at.strftime('%b %d') }}</div></td>
        <td class="text-sm">{{ t.poster.name }}</td>
        <td class="monospace text-sm">KES {{ t.budget|int }}</td>
        <td><span class="badge badge-{{ 'success' if t.status == 'open' else 'warning' if t.status == 'assigned' else 'info' if t.status == 'completed' else 'danger' }}">{{ t.status }}</span></td>
        <td>
          <div class="flex gap-2">
            {% if t.status in ('open','assigned') %}
            <a href="{{ url_for('admin_force_complete', tid=t.id) }}" class="btn btn-sm btn-success">Complete</a>
            {% endif %}
            <a href="{{ url_for('admin_delete_task', tid=t.id) }}" class="btn btn-sm btn-danger" onclick="return confirm('Delete this task?')">Delete</a>
          </div>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div style="padding:16px">
    <div class="pagination">
      {% for p in range(1,total_pages+1) %}
      <a href="?page={{ p }}" class="page-btn {{ 'active' if p == page }}">{{ p }}</a>
      {% endfor %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, tasks=tasks, total=total, page=page, total_pages=total_pages)


@app.route("/admin/tasks/<int:tid>/delete")
@admin_required
def admin_delete_task(tid):
    task = Task.query.get_or_404(tid)
    if task.status in ("open", "assigned") and task.poster:
        credit_wallet(task.poster, task.budget, "refund", f"Refund: Admin deleted task '{task.title}'")
        task.poster.held_balance -= task.budget
    db.session.delete(task)
    db.session.commit()
    flash("Task deleted.", "success")
    return redirect(url_for("admin_tasks"))


@app.route("/admin/tasks/<int:tid>/force-complete")
@admin_required
def admin_force_complete(tid):
    task = Task.query.get_or_404(tid)
    if task.status != "assigned":
        flash("Task must be assigned first.", "warning")
        return redirect(url_for("admin_tasks"))
    return _do_complete_task(task)


@app.route("/admin/payments")
@admin_required
def admin_payments():
    page = request.args.get("page", 1, type=int)
    per_page = 30
    payments = Payment.query.order_by(desc(Payment.created_at)).offset((page - 1) * per_page).limit(per_page).all()
    total = Payment.query.count()
    total_pages = ceil(total / per_page) if total else 1

    tpl = BASE_HTML + '''
{% block page_title %}Admin — Payments{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2>All Payments ({{ total }})</h2></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Invoice</th><th>User</th><th>Amount</th><th>Phone</th><th>Status</th><th>Date</th><th>Actions</th></tr></thead>
      <tbody>
      {% for p in payments %}
      <tr>
        <td class="monospace text-sm">{{ p.invoice_id }}</td>
        <td class="text-sm">{{ p.user.name }}</td>
        <td class="monospace text-sm">KES {{ p.amount|int }}</td>
        <td class="text-sm">{{ p.phone or '-' }}</td>
        <td><span class="badge badge-{{ 'success' if p.status == 'completed' else 'danger' if p.status == 'failed' else 'warning' }}">{{ p.status }}</span></td>
        <td class="text-sm text-muted">{{ p.created_at.strftime('%b %d, %H:%M') }}</td>
        <td>
          {% if p.status == 'pending' %}
          <a href="{{ url_for('admin_approve_payment', pid=p.id) }}" class="btn btn-sm btn-success">Approve</a>
          <a href="{{ url_for('admin_reject_payment', pid=p.id) }}" class="btn btn-sm btn-danger">Reject</a>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div style="padding:16px">
    <div class="pagination">
      {% for p in range(1,total_pages+1) %}
      <a href="?page={{ p }}" class="page-btn {{ 'active' if p == page }}">{{ p }}</a>
      {% endfor %}
    </div>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, payments=payments, total=total, page=page, total_pages=total_pages)


@app.route("/admin/payments/<int:pid>/approve")
@admin_required
def admin_approve_payment(pid):
    payment = Payment.query.get_or_404(pid)
    if payment.status == "pending":
        payment.status = "completed"
        user = User.query.get(payment.user_id)
        credit_wallet(user, payment.amount, "deposit", f"Manual approval: #{payment.invoice_id}", payment.invoice_id)
        add_notification(user.id, "Payment Approved", f"KES {payment.amount:.2f} credited to your wallet.", "success")
        db.session.commit()
        flash("Payment approved and wallet credited.", "success")
    return redirect(url_for("admin_payments"))


@app.route("/admin/payments/<int:pid>/reject")
@admin_required
def admin_reject_payment(pid):
    payment = Payment.query.get_or_404(pid)
    if payment.status == "pending":
        payment.status = "failed"
        db.session.commit()
        flash("Payment rejected.", "warning")
    return redirect(url_for("admin_payments"))


@app.route("/admin/withdrawals")
@admin_required
def admin_withdrawals():
    withdrawals = Withdrawal.query.order_by(desc(Withdrawal.created_at)).all()

    tpl = BASE_HTML + '''
{% block page_title %}Admin — Withdrawals{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2>Withdrawal Requests ({{ withdrawals|length }})</h2></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>User</th><th>Amount</th><th>Phone</th><th>Status</th><th>Requested</th><th>Actions</th></tr></thead>
      <tbody>
      {% for w in withdrawals %}
      <tr>
        <td><div style="font-weight:600;font-size:13px">{{ w.user.name }}</div><div class="text-xs text-muted">{{ w.user.email }}</div></td>
        <td class="monospace text-sm">KES {{ w.amount|int }}</td>
        <td class="text-sm">{{ w.phone }}</td>
        <td><span class="badge badge-{{ 'warning' if w.status == 'pending' else 'success' if w.status == 'approved' else 'danger' }}">{{ w.status }}</span></td>
        <td class="text-sm text-muted">{{ w.created_at.strftime('%b %d, %H:%M') }}</td>
        <td>
          {% if w.status == 'pending' %}
          <a href="{{ url_for('admin_approve_withdrawal', wid=w.id) }}" class="btn btn-sm btn-success">Approve</a>
          <a href="{{ url_for('admin_reject_withdrawal', wid=w.id) }}" class="btn btn-sm btn-danger">Reject</a>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, withdrawals=withdrawals)


@app.route("/admin/withdrawals/<int:wid>/approve")
@admin_required
def admin_approve_withdrawal(wid):
    w = Withdrawal.query.get_or_404(wid)
    if w.status == "pending":
        w.status = "approved"
        w.processed_at = datetime.utcnow()
        add_notification(w.user_id, "Withdrawal Approved", f"KES {w.amount:.2f} withdrawal has been approved. Processing now.", "success")
        db.session.commit()
        flash("Withdrawal approved.", "success")
    return redirect(url_for("admin_withdrawals"))


@app.route("/admin/withdrawals/<int:wid>/reject")
@admin_required
def admin_reject_withdrawal(wid):
    w = Withdrawal.query.get_or_404(wid)
    if w.status == "pending":
        w.status = "rejected"
        w.processed_at = datetime.utcnow()
        credit_wallet(w.user, w.amount, "refund", f"Rejected withdrawal refund KES {w.amount:.2f}")
        add_notification(w.user_id, "Withdrawal Rejected", f"KES {w.amount:.2f} has been refunded to your wallet.", "warning")
        db.session.commit()
        flash("Withdrawal rejected and funds refunded.", "warning")
    return redirect(url_for("admin_withdrawals"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        for key, value in request.form.items():
            if key == "csrf_token":
                continue
            s = SiteSetting.query.filter_by(key=key).first()
            if s:
                if s.setting_type == "bool":
                    s.value = "true" if value == "on" else "false"
                else:
                    s.value = value
        db.session.commit()
        flash("Settings saved!", "success")
        return redirect(url_for("admin_settings"))

    settings = SiteSetting.query.order_by(SiteSetting.key).all()

    tpl = BASE_HTML + '''
{% block page_title %}Admin — Settings{% endblock %}
{% block content %}
<div class="card">
  <div class="card-header"><h2><i class="fas fa-gear" style="color:var(--accent)"></i> Platform Settings</h2></div>
  <div class="card-body">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
      <div style="display:grid;gap:16px">
      {% for s in settings %}
      <div class="form-group" style="margin:0;padding:16px;background:var(--surface2);border-radius:var(--radius-sm);border:1px solid var(--border)">
        <label class="form-label" style="font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--text3)">{{ s.key.replace('_',' ') }}</label>
        {% if s.description %}<div class="text-xs text-muted mb-2">{{ s.description }}</div>{% endif %}
        {% if s.setting_type == 'bool' %}
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" name="{{ s.key }}" {{ 'checked' if s.value.lower() == 'true' }} style="width:16px;height:16px;accent-color:var(--accent)"/>
          <span class="text-sm">Enabled</span>
        </label>
        {% else %}
        <input type="{{ 'number' if s.setting_type in ('float','int') else 'text' }}" name="{{ s.key }}" value="{{ s.value }}" class="form-control" {{ 'step=0.01' if s.setting_type == 'float' }}/>
        {% endif %}
      </div>
      {% endfor %}
      </div>
      <button type="submit" class="btn btn-primary mt-4"><i class="fas fa-save"></i> Save All Settings</button>
    </form>
  </div>
</div>
{% endblock %}'''
    return render_template_string(tpl, settings=settings)


# ─── Error Handlers ───────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    tpl = BASE_HTML + '''{% block content %}
    <div class="empty-state" style="padding:80px">
      <i class="fas fa-shield-halved" style="color:var(--danger)"></i>
      <h2 style="margin-top:16px">Access Denied</h2>
      <p>You don't have permission to view this page.</p>
      <a href="{{ url_for('dashboard') }}" class="btn btn-primary mt-4">Go Home</a>
    </div>{% endblock %}'''
    return render_template_string(tpl), 403


@app.errorhandler(404)
def not_found(e):
    tpl = BASE_HTML + '''{% block content %}
    <div class="empty-state" style="padding:80px">
      <i class="fas fa-circle-question" style="color:var(--text3)"></i>
      <h2 style="margin-top:16px">Page Not Found</h2>
      <a href="{{ url_for('dashboard') }}" class="btn btn-primary mt-4">Go Home</a>
    </div>{% endblock %}'''
    return render_template_string(tpl), 404


# ─── Init DB & Run ────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    init_settings()
    # Create default admin if none exists
    if not User.query.filter_by(is_admin=True).first():
        admin = User(
            name="Admin",
            email=os.environ.get("ADMIN_EMAIL", "admin@taskhive.com"),
            is_admin=True,
            referral_code=generate_referral_code()
        )
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "admin123456"))
        db.session.add(admin)
        db.session.commit()
        logger.info("Default admin created: admin@taskhive.com / admin123456")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
