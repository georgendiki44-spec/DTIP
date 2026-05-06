"""
DTIP — Database Models
All SQLAlchemy models live here.
AI detection fields removed per spec.
"""
import secrets
import re
from datetime import datetime, date, timedelta

from flask import current_app
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


# ─────────────────────────────────────────
# PLATFORM SETTINGS
# ─────────────────────────────────────────

class PlatformSettings(db.Model):
    __tablename__ = 'platform_settings'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(80), unique=True, nullable=False)
    value      = db.Column(db.String(1000), nullable=False)
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


# ─────────────────────────────────────────
# USER
# ─────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'users'

    id                = db.Column(db.Integer, primary_key=True)
    email             = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username          = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash     = db.Column(db.String(255), nullable=True)
    google_id         = db.Column(db.String(120), unique=True, nullable=True)
    avatar_url        = db.Column(db.String(500), nullable=True)
    role              = db.Column(db.String(20), default='member')   # admin|moderator|member
    tier              = db.Column(db.String(20), default='free')
    is_active         = db.Column(db.Boolean, default=True)
    is_suspended      = db.Column(db.Boolean, default=False)
    suspension_reason = db.Column(db.String(500), nullable=True)
    is_verified       = db.Column(db.Boolean, default=False)
    is_activated      = db.Column(db.Boolean, default=False)
    activation_paid_at = db.Column(db.DateTime, nullable=True)
    referral_code     = db.Column(db.String(20), unique=True, nullable=True)
    referred_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    tasks_done_today  = db.Column(db.Integer, default=0)
    last_task_date    = db.Column(db.Date, nullable=True)
    premium_expires   = db.Column(db.DateTime, nullable=True)
    premium_suspended = db.Column(db.Boolean, default=False)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    last_login        = db.Column(db.DateTime, nullable=True)

    wallet            = db.relationship('Wallet', backref='user', uselist=False, lazy='joined')

    # ── Password (werkzeug) ───────────────────────────────────────────────

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, pw)

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def referral_link(self) -> str:
        base = PlatformSettings.get('base_url', current_app.config['BASE_URL'])
        return f"{base}/?ref={self.referral_code}"

    @property
    def is_premium_active(self) -> bool:
        if self.premium_suspended:
            return False
        if self.tier == 'premium':
            if self.premium_expires and self.premium_expires > datetime.utcnow():
                return True
            elif not self.premium_expires:
                return True
        return False

    def get_daily_tasks_done(self) -> int:
        if self.last_task_date != date.today():
            return 0
        return self.tasks_done_today or 0

    def increment_task_count(self):
        today = date.today()
        if self.last_task_date != today:
            self.tasks_done_today = 1
            self.last_task_date = today
        else:
            self.tasks_done_today = (self.tasks_done_today or 0) + 1

    def daily_limit(self) -> int:
        if self.is_premium_active:
            return int(PlatformSettings.get(
                'premium_daily_limit', current_app.config['PREMIUM_DAILY_LIMIT']))
        return int(PlatformSettings.get(
            'free_daily_limit', current_app.config['FREE_DAILY_LIMIT']))

    def to_dict(self) -> dict:
        return dict(
            id=self.id, email=self.email, username=self.username,
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
            last_login=self.last_login.isoformat() if self.last_login else None,
        )


# ─────────────────────────────────────────
# WALLET
# ─────────────────────────────────────────

class Wallet(db.Model):
    __tablename__ = 'wallets'

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    balance      = db.Column(db.Numeric(12, 2), default=0)
    escrow       = db.Column(db.Numeric(12, 2), default=0)
    total_earned = db.Column(db.Numeric(12, 2), default=0)
    total_spent  = db.Column(db.Numeric(12, 2), default=0)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return dict(
            balance=float(self.balance),
            escrow=float(self.escrow),
            total_earned=float(self.total_earned),
            total_spent=float(self.total_spent),
        )


class WalletLedger(db.Model):
    __tablename__ = 'wallet_ledger'

    id            = db.Column(db.Integer, primary_key=True)
    wallet_id     = db.Column(db.Integer, db.ForeignKey('wallets.id'), nullable=False, index=True)
    type          = db.Column(db.String(30), nullable=False)
    amount        = db.Column(db.Numeric(12, 2), nullable=False)
    balance_after = db.Column(db.Numeric(12, 2), nullable=False)
    description   = db.Column(db.String(255))
    reference     = db.Column(db.String(100))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return dict(
            id=self.id, type=self.type,
            amount=float(self.amount),
            balance_after=float(self.balance_after),
            description=self.description,
            reference=self.reference,
            created_at=self.created_at.isoformat(),
        )


# ─────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────

class Task(db.Model):
    __tablename__ = 'tasks'

    id           = db.Column(db.Integer, primary_key=True)
    title        = db.Column(db.String(200), nullable=False)
    description  = db.Column(db.Text, nullable=False)
    instructions = db.Column(db.Text, nullable=True)
    category     = db.Column(db.String(80), nullable=False)
    reward       = db.Column(db.Numeric(10, 2), nullable=False)
    requires_pdf = db.Column(db.Boolean, default=True)
    is_active    = db.Column(db.Boolean, default=True)
    is_flagged   = db.Column(db.Boolean, default=False)
    flag_reason  = db.Column(db.String(255))
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    deadline     = db.Column(db.DateTime, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    creator     = db.relationship('User', foreign_keys=[created_by])
    completions = db.relationship('TaskCompletion', backref='task',
                                  lazy='dynamic', cascade='all,delete')

    def to_dict(self) -> dict:
        return dict(
            id=self.id, title=self.title, description=self.description,
            instructions=self.instructions, category=self.category,
            reward=float(self.reward), requires_pdf=self.requires_pdf,
            is_active=self.is_active, is_flagged=self.is_flagged,
            flag_reason=self.flag_reason,
            deadline=self.deadline.isoformat() if self.deadline else None,
            completion_count=self.completions.count(),
            created_by=self.created_by,
            created_at=self.created_at.isoformat(),
        )


class TaskCompletion(db.Model):
    __tablename__ = 'task_completions'

    id               = db.Column(db.Integer, primary_key=True)
    task_id          = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False, index=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    proof_text       = db.Column(db.Text, nullable=True)
    pdf_filename     = db.Column(db.String(255), nullable=True)
    pdf_original     = db.Column(db.String(255), nullable=True)
    pdf_hash         = db.Column(db.String(64), nullable=True)   # SHA-256 for duplicate detection
    status           = db.Column(db.String(20), default='pending')  # pending|approved|rejected
    rejection_reason = db.Column(db.Text, nullable=True)
    reviewed_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at      = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    user     = db.relationship('User', foreign_keys=[user_id], backref='completions')
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])

    def to_dict(self) -> dict:
        return dict(
            id=self.id, task_id=self.task_id, user_id=self.user_id,
            username=self.user.username if self.user else None,
            proof_text=self.proof_text,
            pdf_filename=self.pdf_filename, pdf_original=self.pdf_original,
            pdf_url=f'/uploads/{self.pdf_filename}' if self.pdf_filename else None,
            status=self.status, rejection_reason=self.rejection_reason,
            reviewed_by=self.reviewed_by,
            reviewer_name=self.reviewer.username if self.reviewer else None,
            reviewed_at=self.reviewed_at.isoformat() if self.reviewed_at else None,
            created_at=self.created_at.isoformat(),
        )


# ─────────────────────────────────────────
# SHARES
# ─────────────────────────────────────────

class Share(db.Model):
    __tablename__ = 'shares'

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    quantity     = db.Column(db.Integer, nullable=False)
    price_each   = db.Column(db.Numeric(10, 2), nullable=False)
    total_paid   = db.Column(db.Numeric(10, 2), nullable=False)
    status       = db.Column(db.String(20), default='active')
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='shares')

    def to_dict(self) -> dict:
        return dict(
            id=self.id, user_id=self.user_id, quantity=self.quantity,
            price_each=float(self.price_each), total_paid=float(self.total_paid),
            status=self.status, purchased_at=self.purchased_at.isoformat(),
        )


# ─────────────────────────────────────────
# PAYMENTS
# ─────────────────────────────────────────

class Payment(db.Model):
    __tablename__ = 'payments'

    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    type             = db.Column(db.String(30), nullable=False)
    amount           = db.Column(db.Numeric(10, 2), nullable=False)
    fee              = db.Column(db.Numeric(10, 2), default=0)
    net_amount       = db.Column(db.Numeric(10, 2), nullable=False)
    phone            = db.Column(db.String(20))
    reference        = db.Column(db.String(100), unique=True, index=True)
    intasend_id      = db.Column(db.String(100), nullable=True, index=True)
    status           = db.Column(db.String(20), default='pending')
    webhook_received = db.Column(db.Boolean, default=False)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='payments')

    def to_dict(self) -> dict:
        return dict(
            id=self.id, type=self.type,
            amount=float(self.amount), fee=float(self.fee),
            net_amount=float(self.net_amount), phone=self.phone,
            reference=self.reference, status=self.status,
            created_at=self.created_at.isoformat(),
        )


# ─────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────

class Message(db.Model):
    __tablename__ = 'messages'

    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    message      = db.Column(db.Text, nullable=False)
    is_read      = db.Column(db.Boolean, default=False)
    is_broadcast = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    sender   = db.relationship('User', foreign_keys=[sender_id], backref='sent_msgs')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='recv_msgs')

    def to_dict(self) -> dict:
        return dict(
            id=self.id, sender_id=self.sender_id,
            sender_name=self.sender.username if self.sender else 'System',
            sender_avatar=self.sender.avatar_url if self.sender else None,
            receiver_id=self.receiver_id, message=self.message,
            is_read=self.is_read, is_broadcast=self.is_broadcast,
            created_at=self.created_at.isoformat(),
        )


# ─────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────

class Notification(db.Model):
    __tablename__ = 'notifications'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title      = db.Column(db.String(200), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(20), default='info')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return dict(
            id=self.id, title=self.title, body=self.body,
            type=self.type, is_read=self.is_read,
            created_at=self.created_at.isoformat(),
        )


# ─────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────

class Alert(db.Model):
    __tablename__ = 'alerts'

    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(20), default='info')   # info|warning|danger|success
    is_active  = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

    creator = db.relationship('User')

    def to_dict(self) -> dict:
        return dict(
            id=self.id, title=self.title, message=self.message,
            type=self.type, is_active=self.is_active,
            created_at=self.created_at.isoformat(),
            expires_at=self.expires_at.isoformat() if self.expires_at else None,
        )


# ─────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id         = db.Column(db.Integer, primary_key=True)
    actor_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action     = db.Column(db.String(100), nullable=False)
    target     = db.Column(db.String(200), nullable=True)   # e.g. "user:42" or "payment:7"
    details    = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship('User', foreign_keys=[actor_id])
