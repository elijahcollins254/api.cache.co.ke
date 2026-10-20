from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from bnpl.models import BNPLAccount
from orders.models import Order
from payments.models import Payment
from payments.views import get_order_payment_progress, release_expired_boost_checkout
from rest_framework.test import APIClient
from services.models import Service
from users.models import Location

User = get_user_model()


class PaymentProgressTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name='Test Location')
        self.customer = User.objects.create_user(
            username='customer_user',
            email='customer@example.com',
            password='secret123',
            role='customer',
            phone='+254712345678',
        )
        self.service = Service.objects.create(
            name='Laundry Delivery',
            description='Test service',
            price=Decimal('1000.00'),
        )

    def test_payment_progress_uses_discounted_total_when_offer_applied(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='pending_payment',
            price=Decimal('1000.00'),
            actual_price=Decimal('500.00'),
            service_location=self.location,
        )
        order.services.add(self.service)

        Payment.objects.create(
            user=self.customer,
            order_id=order.id,
            amount=Decimal('200.00'),
            phone_number='+254712345678',
            provider='mpesa',
            status='success',
        )

        total, paid, remaining = get_order_payment_progress(order)

        self.assertEqual(total, Decimal('500.00'))
        self.assertEqual(paid, Decimal('200.00'))
        self.assertEqual(remaining, Decimal('300.00'))

    def test_expired_boost_checkout_releases_reserved_credit_once(self):
        account = BNPLAccount.objects.create(
            user=self.customer,
            phone_number='+254712345678',
            credit_limit=Decimal('500.00'),
            current_balance=Decimal('100.00'),
        )
        boost_payment = Payment.objects.create(
            user=self.customer,
            amount=Decimal('100.00'),
            phone_number='+254712345678',
            provider='bnpl',
            status=Payment.STATUS_SUCCESS,
        )
        stk_payment = Payment.objects.create(
            user=self.customer,
            amount=Decimal('50.00'),
            phone_number='+254712345678',
            provider='mpesa',
            provider_reference='ws_CO_expired',
            status=Payment.STATUS_INITIATED,
            initiated_at=timezone.now() - timedelta(minutes=16),
            raw_payload={
                'is_boost_checkout': True,
                'boost_payment_id': boost_payment.id,
                'boost_amount': 100.0,
            },
        )

        self.assertTrue(release_expired_boost_checkout(stk_payment))
        account.refresh_from_db()
        boost_payment.refresh_from_db()
        stk_payment.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('0.00'))
        self.assertEqual(boost_payment.status, Payment.STATUS_FAILED)
        self.assertEqual(stk_payment.status, Payment.STATUS_FAILED)
        self.assertFalse(release_expired_boost_checkout(stk_payment))


class PaymentEndpointTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='payment_customer',
            password='secret123',
            role='customer',
            phone='0712345678',
        )
        self.order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            price=Decimal('1000.00'),
        )
        self.client = APIClient()

    def test_mpesa_payment_links_to_order_primary_key(self):
        with (
            self.settings(MPESA_CONSUMER_KEY='test-key', MPESA_CONSUMER_SECRET='test-secret'),
            patch('payments.views.MpesaSTKPushView._get_access_token', return_value='test-token'),
            patch(
                'payments.views.MpesaSTKPushView._initiate_stk_push',
                return_value={'CheckoutRequestID': 'ws_CO_test'},
            ),
        ):
            response = self.client.post(
                '/payments/mpesa/stk-push/',
                {
                    'amount': 1000,
                    'phone': '0712345678',
                    'order_id': self.order.code,
                },
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        payment = Payment.objects.get(provider_reference='ws_CO_test')
        self.assertEqual(payment.order_id, self.order.id)
        self.assertEqual(payment.raw_payload['order_reference'], self.order.code)

    def test_boost_opt_in_returns_active_account(self):
        self.client.force_authenticate(user=self.customer)

        with patch('payments.views.notify_boost_opted_in'):
            response = self.client.post(
                '/payments/bnpl/opt_in/',
                {'phone_number': self.customer.phone},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['is_enrolled'])
        self.assertTrue(BNPLAccount.objects.get(user=self.customer).is_active)
