"""Rider delivery earnings and Safaricom Daraja B2C payout helpers."""

import base64
import logging
from decimal import Decimal
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.db.models import Q
from django.utils import timezone

from .models import RiderLedgerEntry, RiderWallet, WithdrawalRequest

logger = logging.getLogger(__name__)


def credit_delivery_earning(order, rider):
    """Credit the configured fixed earning once for the order's delivery rider."""
    if not rider or rider.role != 'rider':
        return None
    amount = Decimal(str(getattr(settings, 'RIDER_DELIVERY_EARNING_AMOUNT', '50.00')))
    if amount <= 0:
        raise ValueError('RIDER_DELIVERY_EARNING_AMOUNT must be greater than zero.')

    with transaction.atomic():
        wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
        wallet = RiderWallet.objects.select_for_update().get(pk=wallet.pk)
        existing = RiderLedgerEntry.objects.filter(
            rider=rider, order=order, entry_type=RiderLedgerEntry.TYPE_EARNING
        ).first()
        if existing:
            return existing
        entry = RiderLedgerEntry.objects.create(
            rider=rider,
            order=order,
            entry_type=RiderLedgerEntry.TYPE_EARNING,
            amount=amount,
            description=f'Delivery completed: {order.code}',
        )
        RiderWallet.objects.filter(pk=wallet.pk).update(balance=F('balance') + amount, updated_at=timezone.now())
        return entry


def _daraja_config():
    sandbox = str(getattr(settings, 'MPESA_ENVIRONMENT', 'production')).lower() == 'sandbox'
    prefix = 'https://sandbox.safaricom.co.ke' if sandbox else 'https://api.safaricom.co.ke'
    consumer_key = getattr(settings, 'MPESA_SANDBOX_CONSUMER_KEY', '') if sandbox else ''
    consumer_secret = getattr(settings, 'MPESA_SANDBOX_CONSUMER_SECRET', '') if sandbox else ''
    consumer_key = consumer_key or getattr(settings, 'MPESA_CONSUMER_KEY', '')
    consumer_secret = consumer_secret or getattr(settings, 'MPESA_CONSUMER_SECRET', '')
    shortcode = getattr(settings, 'MPESA_B2C_SHORTCODE', '') or getattr(settings, 'MPESA_BUSINESS_SHORTCODE', '')
    return {
        'base_url': prefix,
        'consumer_key': consumer_key,
        'consumer_secret': consumer_secret,
        'shortcode': shortcode,
        'initiator': getattr(settings, 'MPESA_B2C_INITIATOR_NAME', ''),
        'security_credential': getattr(settings, 'MPESA_B2C_SECURITY_CREDENTIAL', ''),
        'command_id': getattr(settings, 'MPESA_B2C_COMMAND_ID', 'BusinessPayment'),
        'result_url': getattr(settings, 'MPESA_B2C_RESULT_URL', ''),
        'timeout_url': getattr(settings, 'MPESA_B2C_QUEUE_TIMEOUT_URL', ''),
        'callback_token': getattr(settings, 'MPESA_B2C_CALLBACK_TOKEN', ''),
    }


def b2c_configuration_error():
    config = _daraja_config()
    required = ('consumer_key', 'consumer_secret', 'shortcode', 'initiator', 'security_credential',
                'result_url', 'timeout_url', 'callback_token')
    missing = [key for key in required if not config[key]]
    return f"Missing B2C configuration: {', '.join(missing)}" if missing else None


def initiate_b2c(withdrawal):
    """Submit an M-Pesa B2C payout; the callback is authoritative for final status."""
    config = _daraja_config()
    error = b2c_configuration_error()
    if error:
        raise RuntimeError(error)
    if withdrawal.amount != withdrawal.amount.to_integral_value():
        raise ValueError('B2C payouts must be whole KES amounts.')

    oauth = requests.get(
        f"{config['base_url']}/oauth/v1/generate?grant_type=client_credentials",
        auth=(config['consumer_key'], config['consumer_secret']),
        timeout=20,
    )
    oauth.raise_for_status()
    access_token = oauth.json().get('access_token')
    if not access_token:
        raise RuntimeError('Safaricom did not return an access token.')

    callback_query = urlencode({'token': config['callback_token']})
    result_url = f"{config['result_url']}{'&' if '?' in config['result_url'] else '?'}{callback_query}"
    timeout_url = f"{config['timeout_url']}{'&' if '?' in config['timeout_url'] else '?'}{callback_query}"
    phone = withdrawal.phone_number.lstrip('+')
    payload = {
        'InitiatorName': config['initiator'],
        'SecurityCredential': config['security_credential'],
        'CommandID': config['command_id'],
        'Amount': int(withdrawal.amount),
        'PartyA': config['shortcode'],
        'PartyB': phone,
        'Remarks': f'Rider earnings payout {withdrawal.id}',
        'QueueTimeOutURL': timeout_url,
        'ResultURL': result_url,
        'Occasion': f'RIDER-{withdrawal.id}',
    }
    response = requests.post(
        f"{config['base_url']}/mpesa/b2c/v1/paymentrequest",
        json=payload,
        headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
        timeout=25,
    )
    response.raise_for_status()
    body = response.json()
    if str(body.get('ResponseCode', '')) != '0':
        raise RuntimeError(body.get('ResponseDescription') or body.get('errorMessage') or 'B2C payout request was rejected.')
    return body


def fail_withdrawal_and_refund(withdrawal, reason):
    """Mark a failed payout and return reserved funds exactly once."""
    with transaction.atomic():
        locked = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal.pk)
        if locked.status in (WithdrawalRequest.STATUS_FAILED, WithdrawalRequest.STATUS_PAID):
            return locked
        locked.status = WithdrawalRequest.STATUS_FAILED
        locked.failure_reason = str(reason)[:2000]
        locked.save(update_fields=['status', 'failure_reason', 'updated_at'])
        wallet = RiderWallet.objects.select_for_update().get(rider=locked.rider)
        refund_exists = RiderLedgerEntry.objects.filter(
            rider=locked.rider, withdrawal=locked, entry_type=RiderLedgerEntry.TYPE_REFUND
        ).exists()
        if not refund_exists:
            RiderLedgerEntry.objects.create(
                rider=locked.rider,
                withdrawal=locked,
                entry_type=RiderLedgerEntry.TYPE_REFUND,
                amount=locked.amount,
                description=f'Withdrawal refund #{locked.pk}',
            )
            RiderWallet.objects.filter(pk=wallet.pk).update(
                balance=F('balance') + locked.amount, updated_at=timezone.now()
            )
        return locked


def settle_b2c_callback(payload, timed_out=False):
    """Match the Daraja callback to its withdrawal and settle/refund it idempotently."""
    result = payload.get('Result', {}) if isinstance(payload, dict) else {}
    conversation_id = result.get('ConversationID') or result.get('OriginatorConversationID')
    if not conversation_id:
        return None
    try:
        withdrawal = WithdrawalRequest.objects.get(
            Q(conversation_id=conversation_id) | Q(originator_conversation_id=conversation_id)
        )
    except WithdrawalRequest.DoesNotExist:
        logger.warning('B2C callback for unknown conversation ID %s', conversation_id)
        return None

    if timed_out:
        return fail_withdrawal_and_refund(withdrawal, result.get('ResultDesc') or 'Safaricom payout queue timed out.')
    if str(result.get('ResultCode', '')) == '0':
        with transaction.atomic():
            locked = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal.pk)
            if locked.status not in (WithdrawalRequest.STATUS_PAID, WithdrawalRequest.STATUS_FAILED):
                parameters = result.get('ResultParameters', {}).get('ResultParameter', []) or []
                values = {str(item.get('Key')): item.get('Value') for item in parameters if isinstance(item, dict)}
                locked.status = WithdrawalRequest.STATUS_PAID
                locked.transaction_reference = str(values.get('TransactionReceipt', ''))
                locked.save(update_fields=['status', 'transaction_reference', 'updated_at'])
            return locked
    return fail_withdrawal_and_refund(withdrawal, result.get('ResultDesc') or 'Safaricom payout could not be started. The amount has been returned to your balance.')
