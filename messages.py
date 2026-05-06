"""
DTIP — Messages & Shares Routes
"""
import logging
from flask import Blueprint, request, jsonify

from extensions import db, limiter, socketio
from models import Message, Share, Wallet
from utils.security import require_auth, require_admin, sanitize
from utils.helpers import ledger, get_setting, notify, audit

logger      = logging.getLogger(__name__)
messages_bp = Blueprint('messages', __name__)
shares_bp   = Blueprint('shares', __name__)


# ─────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────

@messages_bp.route('/api/messages')
@require_auth
def get_messages(user):
    page = request.args.get('page', 1, int)
    msgs = (Message.query
            .filter(
                (Message.receiver_id == user.id) |
                (Message.is_broadcast.is_(True))
            )
            .order_by(Message.created_at.desc())
            .paginate(page=page, per_page=30))
    return jsonify(messages=[m.to_dict() for m in msgs.items],
                   total=msgs.total, pages=msgs.pages)


@messages_bp.route('/api/messages', methods=['POST'])
@require_auth
@limiter.limit("30 per hour")
def send_message(user):
    d    = request.get_json() or {}
    text = sanitize(d.get('message', ''), 2000)
    recv = d.get('receiver_id')
    if not text:
        return jsonify(error='Message is required'), 400

    msg = Message(sender_id=user.id, receiver_id=recv, message=text)
    db.session.add(msg)
    db.session.commit()

    if recv:
        socketio.emit('message', msg.to_dict(), room=f'user_{recv}')
    return jsonify(message=msg.to_dict()), 201


@messages_bp.route('/api/messages/<int:mid>/read', methods=['POST'])
@require_auth
def mark_read(user, mid):
    msg = Message.query.get_or_404(mid)
    if msg.receiver_id != user.id and not msg.is_broadcast:
        return jsonify(error='Forbidden'), 403
    msg.is_read = True
    db.session.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────

@messages_bp.route('/api/notifications')
@require_auth
def get_notifications(user):
    from models import Notification
    page = request.args.get('page', 1, int)
    notifs = (Notification.query
              .filter_by(user_id=user.id)
              .order_by(Notification.created_at.desc())
              .paginate(page=page, per_page=30))
    return jsonify(
        notifications=[n.to_dict() for n in notifs.items],
        unread=Notification.query.filter_by(user_id=user.id, is_read=False).count(),
        total=notifs.total, pages=notifs.pages,
    )


@messages_bp.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def read_all_notifications(user):
    from models import Notification
    Notification.query.filter_by(user_id=user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────
# SHARES
# ─────────────────────────────────────────

@shares_bp.route('/api/shares')
@require_auth
def my_shares(user):
    shares = Share.query.filter_by(user_id=user.id).all()
    price  = get_setting('share_price', 100.0)
    total_qty = sum(s.quantity for s in shares)
    return jsonify(
        shares=[s.to_dict() for s in shares],
        total_shares=total_qty,
        share_price=price,
        portfolio_value=round(total_qty * float(price), 2),
    )


@shares_bp.route('/api/shares/buy', methods=['POST'])
@require_auth
@limiter.limit("10 per hour")
def buy_shares(user):
    if not user.is_activated:
        return jsonify(error='Activate your account first'), 400

    d   = request.get_json() or {}
    qty = int(d.get('quantity', 0))
    if qty < 1:
        return jsonify(error='Quantity must be at least 1'), 400

    price = float(get_setting('share_price', 100.0))
    total = round(qty * price, 2)

    wallet = Wallet.query.with_for_update().filter_by(user_id=user.id).first()
    if not wallet or float(wallet.balance) < total:
        return jsonify(error='Insufficient wallet balance'), 400

    wallet.balance     = float(wallet.balance) - total
    wallet.total_spent = float(wallet.total_spent) + total
    ledger(wallet, 'share_purchase', -total,
           f'Bought {qty} shares @ KES {price:.0f}')

    share = Share(user_id=user.id, quantity=qty,
                  price_each=price, total_paid=total)
    db.session.add(share)
    db.session.commit()
    audit(user.id, 'shares_purchased', f'share:{share.id}',
          f'{qty} @ KES {price}')
    return jsonify(share=share.to_dict(), wallet=wallet.to_dict()), 201


@shares_bp.route('/api/shares/history')
def shares_history():
    """Public aggregate share data for graphs."""
    from sqlalchemy import func
    rows = (db.session.query(
                func.date(Share.purchased_at).label('date'),
                func.sum(Share.quantity).label('qty'),
                func.sum(Share.total_paid).label('revenue'),
            )
            .filter_by(status='active')
            .group_by(func.date(Share.purchased_at))
            .order_by('date')
            .all())
    data       = [{'date': str(r.date), 'qty': int(r.qty or 0),
                   'revenue': float(r.revenue or 0)} for r in rows]
    cumulative = 0
    for row in data:
        cumulative     += row['qty']
        row['cumulative'] = cumulative
    return jsonify(history=data)
