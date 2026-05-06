"""
DTIP — Admin Routes
"""
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify

from extensions import db, cache
from models import (User, Wallet, Payment, TaskCompletion, Alert,
                    AuditLog, PlatformSettings)
from utils.security import require_admin, require_moderator, sanitize
from utils.helpers import notify, audit, broadcast_settings_update

logger   = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)

_SETTING_KEYS = [
    'activation_fee', 'referral_bonus', 'premium_fee', 'withdrawal_fee_pct',
    'free_daily_limit', 'premium_daily_limit', 'share_price', 'base_url',
]


# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/settings', methods=['GET', 'POST'])
@require_admin
def platform_settings(admin):
    from flask import current_app
    cfg = current_app.config
    defaults = dict(
        activation_fee=cfg['DEFAULT_ACTIVATION_FEE'],
        referral_bonus=cfg['DEFAULT_REFERRAL_BONUS'],
        premium_fee=cfg['DEFAULT_PREMIUM_FEE'],
        withdrawal_fee_pct=cfg['DEFAULT_WITHDRAWAL_FEE_PCT'],
        free_daily_limit=cfg['FREE_DAILY_LIMIT'],
        premium_daily_limit=cfg['PREMIUM_DAILY_LIMIT'],
        share_price='100.0',
        base_url=cfg['BASE_URL'],
    )
    if request.method == 'GET':
        return jsonify({k: PlatformSettings.get(k, defaults.get(k, ''))
                        for k in _SETTING_KEYS})

    d = request.get_json() or {}
    for k, v in d.items():
        if k in _SETTING_KEYS:
            PlatformSettings.set(k, sanitize(str(v), 500))
    db.session.commit()
    cache.clear()
    broadcast_settings_update()
    audit(admin.id, 'settings_updated', details=str(d)[:500])
    return jsonify(ok=True)


# ─────────────────────────────────────────
# PUBLIC SETTINGS (non-sensitive)
# ─────────────────────────────────────────

@admin_bp.route('/api/settings/public')
def public_settings():
    from flask import current_app
    cfg = current_app.config
    return jsonify(
        activation_fee=PlatformSettings.get('activation_fee', cfg['DEFAULT_ACTIVATION_FEE']),
        referral_bonus=PlatformSettings.get('referral_bonus', cfg['DEFAULT_REFERRAL_BONUS']),
        premium_fee=PlatformSettings.get('premium_fee', cfg['DEFAULT_PREMIUM_FEE']),
        free_daily_limit=PlatformSettings.get('free_daily_limit', cfg['FREE_DAILY_LIMIT']),
        premium_daily_limit=PlatformSettings.get('premium_daily_limit', cfg['PREMIUM_DAILY_LIMIT']),
        share_price=PlatformSettings.get('share_price', '100.0'),
    )


# ─────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/users')
@require_admin
def list_users(admin):
    page = request.args.get('page', 1, int)
    q    = request.args.get('q', '')
    qry  = User.query
    if q:
        qry = qry.filter(
            User.email.ilike(f'%{q}%') | User.username.ilike(f'%{q}%')
        )
    users = qry.order_by(User.created_at.desc()).paginate(page=page, per_page=50)
    return jsonify(users=[u.to_dict() for u in users.items],
                   total=users.total, pages=users.pages)


@admin_bp.route('/api/admin/users/<int:uid>/suspend', methods=['POST'])
@require_admin
def suspend_user(admin, uid):
    user = User.query.get_or_404(uid)
    if user.role == 'admin':
        return jsonify(error='Cannot suspend another admin'), 403
    d = request.get_json() or {}
    user.is_suspended     = True
    user.suspension_reason = sanitize(d.get('reason', 'Policy violation'), 500)
    db.session.commit()
    audit(admin.id, 'user_suspended', f'user:{uid}', user.suspension_reason)
    notify(user.id, '🚫 Account Suspended',
           f'Your account has been suspended: {user.suspension_reason}', 'error')
    return jsonify(ok=True)


@admin_bp.route('/api/admin/users/<int:uid>/unsuspend', methods=['POST'])
@require_admin
def unsuspend_user(admin, uid):
    user = User.query.get_or_404(uid)
    user.is_suspended      = False
    user.suspension_reason = None
    db.session.commit()
    audit(admin.id, 'user_unsuspended', f'user:{uid}')
    notify(user.id, '✅ Account Restored', 'Your account has been reactivated.', 'success')
    return jsonify(ok=True)


@admin_bp.route('/api/admin/users/<int:uid>/role', methods=['POST'])
@require_admin
def change_role(admin, uid):
    user = User.query.get_or_404(uid)
    d    = request.get_json() or {}
    role = d.get('role')
    if role not in ('admin', 'moderator', 'member'):
        return jsonify(error='Invalid role'), 400
    old_role   = user.role
    user.role  = role
    db.session.commit()
    audit(admin.id, 'role_changed', f'user:{uid}', f'{old_role} → {role}')
    return jsonify(ok=True, user=user.to_dict())


# ─────────────────────────────────────────
# BROADCAST
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/broadcast', methods=['POST'])
@require_admin
def broadcast(admin):
    from models import Message
    d   = request.get_json() or {}
    msg = sanitize(d.get('message', ''), 2000)
    if not msg:
        return jsonify(error='Message required'), 400

    m = Message(sender_id=admin.id, message=msg, is_broadcast=True)
    db.session.add(m)
    db.session.commit()
    from extensions import socketio
    socketio.emit('broadcast', m.to_dict(), to=None)
    return jsonify(ok=True)


# ─────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/alerts', methods=['GET', 'POST'])
@require_admin
def manage_alerts(admin):
    if request.method == 'GET':
        alerts = Alert.query.order_by(Alert.created_at.desc()).limit(50).all()
        return jsonify(alerts=[a.to_dict() for a in alerts])

    d     = request.get_json() or {}
    title = sanitize(d.get('title', ''), 200)
    msg   = sanitize(d.get('message', ''), 2000)
    atype = d.get('type', 'info')
    if atype not in ('info', 'warning', 'danger', 'success'):
        atype = 'info'
    if not title or not msg:
        return jsonify(error='title and message required'), 400

    expires_at = None
    if d.get('expires_at'):
        try:
            expires_at = datetime.fromisoformat(d['expires_at'])
        except ValueError:
            pass

    alert = Alert(title=title, message=msg, type=atype,
                  created_by=admin.id, expires_at=expires_at)
    db.session.add(alert)
    db.session.commit()
    audit(admin.id, 'alert_created', f'alert:{alert.id}', title)
    from extensions import socketio
    socketio.emit('alert', alert.to_dict(), to=None)
    return jsonify(alert=alert.to_dict()), 201


@admin_bp.route('/api/admin/alerts/<int:aid>', methods=['DELETE'])
@require_admin
def delete_alert(admin, aid):
    alert = Alert.query.get_or_404(aid)
    alert.is_active = False
    db.session.commit()
    return jsonify(ok=True)


@admin_bp.route('/api/alerts/active')
def active_alerts():
    now    = datetime.utcnow()
    alerts = Alert.query.filter(
        Alert.is_active.is_(True),
        (Alert.expires_at.is_(None)) | (Alert.expires_at > now),
    ).order_by(Alert.created_at.desc()).all()
    return jsonify(alerts=[a.to_dict() for a in alerts])


# ─────────────────────────────────────────
# PAYMENT APPROVAL (admin)
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/payments')
@require_admin
def list_payments(admin):
    page = request.args.get('page', 1, int)
    status = request.args.get('status', '')
    qry  = Payment.query
    if status:
        qry = qry.filter_by(status=status)
    pays = qry.order_by(Payment.created_at.desc()).paginate(page=page, per_page=50)
    return jsonify(payments=[p.to_dict() for p in pays.items],
                   total=pays.total, pages=pays.pages)


# ─────────────────────────────────────────
# AUDIT LOG (admin)
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/audit')
@require_admin
def audit_log(admin):
    page  = request.args.get('page', 1, int)
    logs  = (AuditLog.query
             .order_by(AuditLog.created_at.desc())
             .paginate(page=page, per_page=100))
    def _log(l):
        return dict(
            id=l.id,
            actor=l.actor.username if l.actor else 'system',
            action=l.action, target=l.target, details=l.details,
            ip_address=l.ip_address,
            created_at=l.created_at.isoformat(),
        )
    return jsonify(logs=[_log(l) for l in logs.items],
                   total=logs.total, pages=logs.pages)


# ─────────────────────────────────────────
# DASHBOARD STATS
# ─────────────────────────────────────────

@admin_bp.route('/api/admin/stats')
@require_admin
@cache.cached(timeout=60, key_prefix='admin_stats')
def admin_stats(admin):
    from sqlalchemy import func
    from models import Task, Share

    total_users   = User.query.count()
    active_users  = User.query.filter_by(is_active=True, is_activated=True).count()
    premium_users = User.query.filter_by(tier='premium').count()
    pending_tasks = TaskCompletion.query.filter_by(status='pending').count()
    total_payments = Payment.query.filter_by(status='completed').count()
    total_revenue  = db.session.query(
        func.sum(Payment.amount)
    ).filter_by(status='completed', type='activation').scalar() or 0

    return jsonify(
        total_users=total_users,
        active_users=active_users,
        premium_users=premium_users,
        pending_tasks=pending_tasks,
        total_payments=total_payments,
        total_revenue=float(total_revenue),
    )
