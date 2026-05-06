"""
DTIP — General Helpers
"""
import secrets
import logging

from flask import current_app

logger = logging.getLogger(__name__)


def gen_code(length: int = 8) -> str:
    """Generate a readable referral / reference code."""
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_setting(key: str, default):
    """Fetch a PlatformSettings value with type coercion."""
    from models import PlatformSettings
    v = PlatformSettings.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return v


def notify(user_id: int, title: str, body: str, ntype: str = 'info'):
    """Create an in-app notification and push via SocketIO."""
    from models import Notification
    from extensions import db, socketio

    n = Notification(user_id=user_id, title=title, body=body, type=ntype)
    db.session.add(n)
    try:
        socketio.emit('notification', n.to_dict(), room=f'user_{user_id}')
    except Exception:
        pass


def ledger(wallet, tx_type: str, amount, desc: str, ref: str = None):
    """Append a WalletLedger entry. Call BEFORE committing the session."""
    from models import WalletLedger
    from extensions import db
    from datetime import datetime

    wallet.updated_at = datetime.utcnow()
    db.session.add(WalletLedger(
        wallet_id=wallet.id,
        type=tx_type,
        amount=amount,
        balance_after=wallet.balance,
        description=desc,
        reference=ref,
    ))


def audit(actor_id, action: str, target: str = None, details: str = None, ip: str = None):
    """Write an audit log entry."""
    from models import AuditLog
    from extensions import db

    db.session.add(AuditLog(
        actor_id=actor_id,
        action=action,
        target=target,
        details=details,
        ip_address=ip,
    ))


def broadcast_settings_update():
    """Push updated platform settings to all connected clients."""
    from models import PlatformSettings
    from extensions import socketio

    try:
        keys = ['activation_fee', 'referral_bonus', 'premium_fee',
                'withdrawal_fee_pct', 'free_daily_limit', 'premium_daily_limit',
                'share_price', 'base_url']
        cfg = current_app.config
        defaults = {
            'activation_fee':      cfg['DEFAULT_ACTIVATION_FEE'],
            'referral_bonus':      cfg['DEFAULT_REFERRAL_BONUS'],
            'premium_fee':         cfg['DEFAULT_PREMIUM_FEE'],
            'withdrawal_fee_pct':  cfg['DEFAULT_WITHDRAWAL_FEE_PCT'],
            'free_daily_limit':    cfg['FREE_DAILY_LIMIT'],
            'premium_daily_limit': cfg['PREMIUM_DAILY_LIMIT'],
            'share_price':         '100.0',
            'base_url':            cfg['BASE_URL'],
        }
        settings = {k: PlatformSettings.get(k, defaults.get(k, '')) for k in keys}
        socketio.emit('settings_update', settings, to=None)
    except Exception as exc:
        logger.warning(f'broadcast_settings_update failed: {exc}')
