from decimal import Decimal

from django.db import transaction

from .models import BNPLAccount


BASE_LIMIT = Decimal('50.00')
LIMIT_STEP = Decimal('10.00')
SPEND_STEP = Decimal('500.00')
ORDER_BONUS = Decimal('5.00')
REPAYMENT_BONUS = Decimal('10.00')
MAX_LIMIT = Decimal('1000.00')
BOOST_REPAYMENT_DAYS = 30
BOOST_LATE_INTEREST_RATE = Decimal('0.01')


def _send_sms(phone_number, message):
    """Send a BOOST SMS without making enrollment or checkout fail if SMS is unavailable."""
    try:
        from services.sms_service import AfricasTalkingSMSService

        AfricasTalkingSMSService().send_sms(phone_number, message)
    except Exception:
        import logging

        logging.getLogger(__name__).exception('Unable to send BOOST SMS')


def notify_boost_opted_in(account):
    """Tell a customer that BOOST has been activated for their account."""
    _send_sms(
        account.phone_number,
        (
            f'BOOST activated. Your starting purchase credit is KES '
            f'{account.credit_limit:,.2f}. Repay within {BOOST_REPAYMENT_DAYS} days. '
            f'Late repayment attracts {BOOST_LATE_INTEREST_RATE:.0%} interest per day '
            f'on the outstanding balance. '
            f'Use BOOST at checkout.'
        ),
    )


def notify_boost_transaction(account, boost_amount, order_reference=None):
    """Tell a customer how much BOOST credit was used and when it must be repaid."""
    reference = f' for order {order_reference}' if order_reference else ''
    _send_sms(
        account.phone_number,
        (
            f'BOOST used{reference}: KES {boost_amount:,.2f} purchase credit applied. '
            f'Repay within {BOOST_REPAYMENT_DAYS} days. Late repayment attracts '
            f'{BOOST_LATE_INTEREST_RATE:.0%} interest per day on the outstanding balance. '
            f'Current BOOST balance: KES {account.current_balance:,.2f}.'
        ),
    )


def notify_boost_repayment(account, repayment_amount):
    """Confirm a successful BOOST repayment by SMS."""
    _send_sms(
        account.phone_number,
        (
            f'BOOST repayment received: KES {repayment_amount:,.2f}. '
            f'Your outstanding BOOST balance is KES {account.current_balance:,.2f}. '
            f'Your approved BOOST limit is KES {account.credit_limit:,.2f}.'
        ),
    )


def notify_boost_limit_increased(account, previous_limit):
    """Tell a customer when their approved BOOST limit has increased."""
    _send_sms(
        account.phone_number,
        (
            f'BOOST limit increased. Your limit has grown from KES '
            f'{previous_limit:,.2f} to KES {account.credit_limit:,.2f}. '
            f'Keep making successful purchases and repayments to build your limit.'
        ),
    )


def calculate_credit_limit(user):
    """Calculate a transparent limit from completed purchases and repayments.

    Only delivered orders count as purchases. BNPL borrowing itself does not
    increase the limit; successful repayments do.
    """
    from orders.models import Order
    from payments.models import Payment

    completed_orders = Order.objects.filter(user=user, status='delivered')
    total_spend = sum(
        (order.actual_price or order.price or Decimal('0.00') for order in completed_orders),
        Decimal('0.00'),
    )
    purchase_steps = int(total_spend // SPEND_STEP)
    order_bonus = min(ORDER_BONUS * completed_orders.count(), Decimal('100.00'))
    repayment_count = Payment.objects.filter(
        user=user,
        provider='mpesa',
        status=Payment.STATUS_SUCCESS,
        raw_payload__is_bnpl_balance_payment=True,
    ).count()
    repayment_bonus = min(REPAYMENT_BONUS * repayment_count, Decimal('100.00'))

    calculated = BASE_LIMIT + (LIMIT_STEP * purchase_steps) + order_bonus + repayment_bonus
    return min(calculated, MAX_LIMIT)


@transaction.atomic
def refresh_credit_limit(account):
    """Persist an earned limit without reducing an already approved limit."""
    account = BNPLAccount.objects.select_for_update().get(pk=account.pk)
    earned_limit = calculate_credit_limit(account.user)
    if earned_limit > account.credit_limit:
        account.credit_limit = earned_limit
        account.save(update_fields=['credit_limit', 'updated_at'])
    return account
