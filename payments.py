"""
DTIP — Payment Routes (IntaSend LIVE, no demo mode)
"""
import hmac
import logging
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from extensions import db, limiter
from models import Payment, User, Wallet
from utils.security import require_auth, sanitize
from utils.helpers import ledger, notify, get_setting, audit
from services.payment import intasend_stk_push, intasend_b2c, process_completed_payment

logger      = logging.getLogger(__name__)
payments_bp = Blueprint('payments', __name__)


def _phone(raw: str) -> str:
    """Normalise Kenyan phone numbers to 2547XXXXXXXX format."""
    p = ''.join(filter(str.isdigit, raw or ''))
    if p.startswith('0') and len(p) == 10:
        p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9:
        p = '254' + p
    return p


# ─────────────────────────────────────────
# ACTIVATION
# ─────────────────────────────────────────

@payments_bp.route('/api/activate', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def activate(user):
    if user.is_activated:
        return jsonify(error='Account already activated'), 400

    d     = request.get_json() or {}
    phone = _phone(d.get('phone', ''))
    if not phone:
        return jsonify(error='Phone number is required'), 400

    fee = get_setting('activation_fee', current_app.config['DEFAULT_ACTIVATION_FEE'])
    ref = f'ACT-{uuid.uuid4().hex[:10].upper()}'

    result = intasend_stk_push(phone, fee, ref)
    if result['status'] == 'error':
        return jsonify(error=result.get('message', 'Payment initiation failed')), 502

    pay = Payment(
        user_id=user.id, type='activation', amount=fee, fee=0,
        net_amount=fee, phone=phone, reference=ref,
        intasend_id=result.get('id', ''), status='pending',
    )
    db.session.add(pay)
    db.session.commit()
    audit(user.id, 'activation_payment_initiated', f'payment:{pay.id}',
          f'KES {fee}', request.remote_addr)
    return jsonify(ok=True, payment=pay.to_dict(),
                   message='Check your phone for the M-Pesa prompt')


# ─────────────────────────────────────────
# PREMIUM UPGRADE
# ─────────────────────────────────────────

@payments_bp.route('/api/upgrade-premium', methods=['POST'])
@require_auth
@limiter.limit("5 per hour")
def upgrade_premium(user):
    if not user.is_activated:
        return jsonify(error='Activate your account first'), 400

    d     = request.get_json() or {}
    phone = _phone(d.get('phone', ''))
    if not phone:
        return jsonify(error='Phone number is required'), 400

    fee = get_setting('premium_fee', current_app.config['DEFAULT_PREMIUM_FEE'])
    ref = f'PREM-{uuid.uuid4().hex[:10].upper()}'

    result = intasend_stk_push(phone, fee, ref)
    if result['status'] == 'error':
        return jsonify(error=result.get('message', 'Payment initiation failed')), 502

    pay = Payment(
        user_id=user.id, type='premium', amount=fee, fee=0,
        net_amount=fee, phone=phone, reference=ref,
        intasend_id=result.get('id', ''), status='pending',
    )
    db.session.add(pay)
    db.session.commit()
    return jsonify(ok=True, payment=pay.to_dict(),
                   message='Check your phone for the M-Pesa prompt')


# ─────────────────────────────────────────
# WITHDRAWAL
# ─────────────────────────────────────────

@payments_bp.route('/api/withdraw', methods=['POST'])
@require_auth
@limiter.limit("3 per hour")
def withdraw(user):
    if not user.is_activated:
        return jsonify(error='Activate your account first'), 400

    d      = request.get_json() or {}
    amount = float(d.get('amount', 0))
    phone  = _phone(d.get('phone', ''))

    if amount < 100:
        return jsonify(error='Minimum withdrawal is KES 100'), 400
    if not phone:
        return jsonify(error='Phone number is required'), 400

    fee_pct = get_setting('withdrawal_fee_pct',
                          current_app.config['DEFAULT_WITHDRAWAL_FEE_PCT'])
    fee        = round(amount * fee_pct / 100, 2)
    net_amount = round(amount - fee, 2)

    # ── Atomic balance deduction ──────────────────────────────────────
    wallet = Wallet.query.with_for_update().filter_by(user_id=user.id).first()
    if not wallet or float(wallet.balance) < amount:
        return jsonify(error='Insufficient balance'), 400

    ref = f'WD-{uuid.uuid4().hex[:10].upper()}'
    wallet.balance    = float(wallet.balance) - amount
    wallet.total_spent = float(wallet.total_spent) + amount
    ledger(wallet, 'withdrawal', -amount, f'Withdrawal to {phone}', ref)

    pay = Payment(
        user_id=user.id, type='withdrawal', amount=amount, fee=fee,
        net_amount=net_amount, phone=phone, reference=ref, status='processing',
    )
    db.session.add(pay)
    db.session.flush()

    # ── Initiate B2C ──────────────────────────────────────────────────
    result = intasend_b2c(phone, net_amount, ref)
    if result['status'] == 'error':
        # Rollback balance deduction
        db.session.rollback()
        return jsonify(error='Payout failed — try again later'), 502

    db.session.commit()
    audit(user.id, 'withdrawal_initiated', f'payment:{pay.id}',
          f'KES {amount} fee={fee}', request.remote_addr)
    notify(user.id, '💸 Withdrawal Initiated',
           f'KES {net_amount:.0f} is on its way to {phone}', 'info')
    return jsonify(ok=True, payment=pay.to_dict())


# ─────────────────────────────────────────
# INTASEND WEBHOOK
# ─────────────────────────────────────────

@payments_bp.route('/webhook/intasend', methods=['POST'])
def intasend_webhook():
    """
    Idempotent: skips already-processed payments.
    Signature-verified using HMAC-SHA256.
    """
    raw_body  = request.get_data()
    payload   = request.get_json(silent=True) or {}
    logger.info(f'IntaSend webhook received: {payload}')

    # ── Signature verification ────────────────────────────────────────
    secret = current_app.config.get('INTASEND_SECRET', '')
    if secret:
        sig = request.headers.get('X-IntaSend-Signature', '')
        import hashlib as _hl
        expected = hmac.new(
            secret.encode(), raw_body, _hl.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning('IntaSend webhook: invalid signature')
            return jsonify(error='Invalid signature'), 403

    invoice_id = payload.get('invoice_id') or payload.get('id') or ''
    state      = payload.get('state', '').upper()
    api_ref    = payload.get('api_ref', '')

    # ── Locate payment ────────────────────────────────────────────────
    pay = (Payment.query.filter_by(reference=api_ref).first()
           or Payment.query.filter_by(intasend_id=invoice_id).first())

    if pay:
        # ── Idempotency — skip if already processed ───────────────────
        if pay.webhook_received and pay.status in ('completed', 'failed'):
            logger.info(f'Duplicate webhook for payment {pay.id}, skipping')
            return jsonify(status='ok'), 200

        if state == 'COMPLETE':
            pay.status          = 'completed'
            pay.webhook_received = True
            process_completed_payment(pay)

        elif state in ('FAILED', 'CANCELLED'):
            pay.status          = 'failed'
            pay.webhook_received = True
            notify(pay.user_id, '❌ Payment Failed',
                   f'Your {pay.type} payment of KES {float(pay.amount):.0f} failed.',
                   'error')

        db.session.commit()
        audit(None, f'webhook_{state.lower()}',
              f'payment:{pay.id}', api_ref)
    else:
        logger.warning(f'Webhook: payment not found ref={api_ref} id={invoice_id}')

    return jsonify(status='received'), 200


# ─────────────────────────────────────────
# PAYMENT HISTORY
# ─────────────────────────────────────────

@payments_bp.route('/api/my/payments')
@require_auth
def my_payments(user):
    page  = request.args.get('page', 1, int)
    pays  = (Payment.query
             .filter_by(user_id=user.id)
             .order_by(Payment.created_at.desc())
             .paginate(page=page, per_page=20))
    return jsonify(payments=[p.to_dict() for p in pays.items],
                   total=pays.total, pages=pays.pages)


# ─────────────────────────────────────────
# WALLET / LEDGER
# ─────────────────────────────────────────

@payments_bp.route('/api/my/wallet')
@require_auth
def my_wallet(user):
    from models import WalletLedger
    page  = request.args.get('page', 1, int)
    txs   = (WalletLedger.query
             .filter_by(wallet_id=user.wallet.id)
             .order_by(WalletLedger.created_at.desc())
             .paginate(page=page, per_page=20))
    return jsonify(
        wallet=user.wallet.to_dict(),
        ledger=[t.to_dict() for t in txs.items],
        total=txs.total, pages=txs.pages,
    )
