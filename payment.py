"""
DTIP — Payment Service (IntaSend LIVE)
No demo mode. No sandbox. Real money only.
"""
import logging
import uuid
from datetime import datetime, timedelta

import requests as http_req
from flask import current_app

from extensions import db
from models import User, Wallet, Payment
from utils.helpers import ledger, notify, gen_code, get_setting, audit

logger = logging.getLogger(__name__)

SUSPICIOUS_KEYWORDS = [
    'porn', 'xxx', 'drugs', 'weapon', 'hack',
    'phishing', 'scam', 'casino', 'bet', 'gambling',
]


# ─────────────────────────────────────────
# STK PUSH (C2B)
# ─────────────────────────────────────────

def intasend_stk_push(phone: str, amount: float, ref: str, currency: str = 'KES') -> dict:
    """Initiate M-Pesa STK Push via IntaSend LIVE."""
    base = current_app.config['INTASEND_BASE']
    key  = current_app.config['INTASEND_API_KEY']
    try:
        resp = http_req.post(
            f'{base}/api/v1/payment/mpesa-stk-push/',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json=dict(amount=amount, phone_number=phone,
                      api_ref=ref, currency=currency),
            timeout=15,
        )
        data = resp.json()
        if resp.status_code in (200, 201):
            return {'status': 'pending', 'id': data.get('id', ''), 'data': data}
        logger.error(f'IntaSend STK error {resp.status_code}: {data}')
        return {'status': 'error', 'message': data.get('detail', 'Payment failed')}
    except Exception as exc:
        logger.exception('IntaSend STK push exception')
        return {'status': 'error', 'message': str(exc)}


# ─────────────────────────────────────────
# B2C PAYOUT
# ─────────────────────────────────────────

def intasend_b2c(phone: str, amount: float, ref: str) -> dict:
    """Send money to user via IntaSend B2C LIVE."""
    base = current_app.config['INTASEND_BASE']
    key  = current_app.config['INTASEND_API_KEY']
    try:
        resp = http_req.post(
            f'{base}/api/v1/send-money/mpesa/',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json=dict(
                currency='KES',
                transactions=[{'name': 'User', 'account': phone, 'amount': amount}],
                api_ref=ref,
            ),
            timeout=15,
        )
        data = resp.json()
        ok   = resp.status_code in (200, 201)
        return {'status': 'pending' if ok else 'error', 'data': data}
    except Exception as exc:
        logger.exception('IntaSend B2C exception')
        return {'status': 'error', 'message': str(exc)}


# ─────────────────────────────────────────
# PROCESS COMPLETED WEBHOOK PAYMENT
# ─────────────────────────────────────────

def process_completed_payment(pay: Payment):
    """
    Called inside an active DB transaction.
    Handles activation, premium, deposit, withdrawal.
    """
    user = User.query.with_for_update().get(pay.user_id)
    if not user:
        return

    if pay.type == 'activation' and not user.is_activated:
        user.is_activated = True
        user.activation_paid_at = datetime.utcnow()
        _pay_referral_bonus(user, float(pay.amount))
        notify(user.id, '🎉 Account Activated!',
               f'Your account is live! KES {float(pay.amount):.0f} paid.', 'success')
        audit(None, 'account_activated', f'user:{user.id}',
              f'KES {float(pay.amount)} activation fee')

    elif pay.type == 'premium' and not user.is_premium_active:
        user.tier = 'premium'
        user.premium_expires = datetime.utcnow() + timedelta(days=30)
        notify(user.id, '⭐ Premium Activated!',
               '30 days of premium access unlocked!', 'success')
        audit(None, 'premium_activated', f'user:{user.id}')

    elif pay.type == 'deposit':
        w = Wallet.query.with_for_update().filter_by(user_id=user.id).first()
        if w:
            w.balance      = float(w.balance) + float(pay.amount)
            w.total_earned = float(w.total_earned) + float(pay.amount)
            ledger(w, 'deposit', float(pay.amount), 'M-Pesa deposit', pay.reference)
            notify(user.id, '💳 Deposit Received',
                   f'KES {float(pay.amount):.0f} added to wallet', 'success')


def _pay_referral_bonus(user: User, activation_fee: float):
    if not user.referred_by:
        return
    ref_user = User.query.get(user.referred_by)
    if not ref_user:
        return
    bonus  = get_setting('referral_bonus', current_app.config['DEFAULT_REFERRAL_BONUS'])
    wallet = Wallet.query.with_for_update().filter_by(user_id=ref_user.id).first()
    if not wallet:
        return
    wallet.balance      = float(wallet.balance) + bonus
    wallet.total_earned = float(wallet.total_earned) + bonus
    ledger(wallet, 'referral', bonus,
           f'Referral bonus — {user.username} activated')
    notify(ref_user.id, '💰 Referral Bonus!',
           f'{user.username} activated! You earned KES {bonus:.0f}', 'success')
