from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from orders.models import Order
from payments.models import Payment

from .models import BNPLAccount
from .services import calculate_credit_limit, notify_boost_transaction


class CreditLimitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bnpl-test-user',
            password='test-password',
        )

    def test_new_account_starts_at_fifty(self):
        account = BNPLAccount.objects.create(user=self.user, phone_number='0712345678')
        self.assertEqual(account.credit_limit, Decimal('50.00'))

    def test_delivered_spend_and_repayment_increase_limit(self):
        Order.objects.create(
            user=self.user,
            pickup_address='Pickup',
            dropoff_address='Dropoff',
            status='delivered',
            price=Decimal('500.00'),
        )
        Payment.objects.create(
            user=self.user,
            amount=Decimal('20.00'),
            phone_number='254712345678',
            provider='mpesa',
            status=Payment.STATUS_SUCCESS,
            raw_payload={'is_bnpl_balance_payment': True},
        )

        self.assertEqual(calculate_credit_limit(self.user), Decimal('75.00'))

    @patch('bnpl.services._send_sms')
    def test_boost_transaction_message_uses_thirty_day_repayment_period(self, send_sms):
        account = BNPLAccount.objects.create(user=self.user, phone_number='0712345678')

        notify_boost_transaction(account, Decimal('50.00'))

        self.assertIn('Repay within 30 days.', send_sms.call_args.args[1])
