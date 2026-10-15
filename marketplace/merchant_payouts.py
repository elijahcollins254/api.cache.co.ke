import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import MerchantLedgerEntry, MerchantWallet, MerchantWithdrawal

logger = logging.getLogger(__name__)


def merchants_prepared_for_pickup(order):
    from orders.models import ProductOrderItem
    from .models import MerchantOrderFulfillment

    merchant_ids = set(ProductOrderItem.objects.filter(
        order=order,
        product__shop__owner__isnull=False,
    ).values_list('product__shop__owner_id', flat=True).distinct())
    prepared_merchant_ids = set(MerchantOrderFulfillment.objects.filter(
        order=order,
        merchant_id__in=merchant_ids,
        status=MerchantOrderFulfillment.STATUS_PREPARED,
    ).values_list('merchant_id', flat=True))
    return merchant_ids.issubset(prepared_merchant_ids)


def credit_completed_order(order):
    if not order.is_paid() or order.status.lower() != 'delivered':
        return

    with transaction.atomic():
        lines = order.product_order_items.filter(
            product__shop__owner__isnull=False,
            product__shop__owner__order_fulfillments__status='prepared',
            product__shop__owner__order_fulfillments__order=order,
        ).select_related('product__shop__owner')
        totals = {}
        merchants = {}
        for line in lines:
            merchant = line.product.shop.owner
            totals[merchant.pk] = totals.get(merchant.pk, Decimal('0')) + line.unit_price * line.quantity
            merchants[merchant.pk] = merchant

        for merchant_id, amount in totals.items():
            if amount <= 0:
                continue
            entry, created = MerchantLedgerEntry.objects.get_or_create(
                merchant_id=merchant_id,
                order=order,
                entry_type=MerchantLedgerEntry.TYPE_EARNING,
                defaults={
                    'amount': amount,
                    'description': f'Paid product sales: {order.code}',
                },
            )
            if not created:
                continue
            wallet, _ = MerchantWallet.objects.get_or_create(merchant=merchants[merchant_id])
            MerchantWallet.objects.filter(pk=wallet.pk).update(
                balance=F('balance') + amount,
                updated_at=timezone.now(),
            )


def fail_withdrawal_and_refund(withdrawal, reason):
    with transaction.atomic():
        locked = MerchantWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        if locked.status in (MerchantWithdrawal.STATUS_FAILED, MerchantWithdrawal.STATUS_PAID):
            return locked
        locked.status = MerchantWithdrawal.STATUS_FAILED
        locked.failure_reason = str(reason)[:2000]
        locked.save(update_fields=['status', 'failure_reason', 'updated_at'])
        wallet = MerchantWallet.objects.select_for_update().get(merchant=locked.merchant)
        if not MerchantLedgerEntry.objects.filter(
            merchant=locked.merchant,
            withdrawal=locked,
            entry_type=MerchantLedgerEntry.TYPE_REFUND,
        ).exists():
            MerchantLedgerEntry.objects.create(
                merchant=locked.merchant,
                withdrawal=locked,
                entry_type=MerchantLedgerEntry.TYPE_REFUND,
                amount=locked.amount,
                description=f'Withdrawal refund #{locked.pk}',
            )
            MerchantWallet.objects.filter(pk=wallet.pk).update(
                balance=F('balance') + locked.amount,
                updated_at=timezone.now(),
            )
        return locked


def settle_b2c_callback(payload, timed_out=False):
    result = payload.get('Result', {}) if isinstance(payload, dict) else {}
    conversation_id = result.get('ConversationID') or result.get('OriginatorConversationID')
    if not conversation_id:
        return None
    try:
        withdrawal = MerchantWithdrawal.objects.get(
            Q(conversation_id=conversation_id) | Q(originator_conversation_id=conversation_id)
        )
    except MerchantWithdrawal.DoesNotExist:
        return None

    if timed_out:
        return fail_withdrawal_and_refund(
            withdrawal,
            result.get('ResultDesc') or 'Safaricom payout queue timed out.',
        )
    if str(result.get('ResultCode', '')) == '0':
        with transaction.atomic():
            locked = MerchantWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
            if locked.status not in (MerchantWithdrawal.STATUS_PAID, MerchantWithdrawal.STATUS_FAILED):
                parameters = result.get('ResultParameters', {}).get('ResultParameter', []) or []
                values = {str(item.get('Key')): item.get('Value') for item in parameters if isinstance(item, dict)}
                locked.status = MerchantWithdrawal.STATUS_PAID
                locked.transaction_reference = str(values.get('TransactionReceipt', ''))
                locked.save(update_fields=['status', 'transaction_reference', 'updated_at'])
            return locked
    return fail_withdrawal_and_refund(
        withdrawal,
        result.get('ResultDesc') or 'Safaricom payout failed. The amount has been returned to your balance.',
    )