from django.test import TestCase
from django.contrib.auth import get_user_model
from decimal import Decimal

from orders.models import Order
from orders.serializers import OrderListLightSerializer
from services.models import Service
from users.models import Location

User = get_user_model()


class OrderDeliveryVerificationTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name='Test Location')
        self.customer = User.objects.create_user(
            username='customer_user',
            email='customer@example.com',
            password='secret123',
            role='customer',
            phone='+254712345678',
        )
        self.rider = User.objects.create_user(
            username='delivery_rider',
            email='rider@example.com',
            password='secret123',
            role='rider',
            phone='+254700123456',
        )
        self.service = Service.objects.create(
            name='Laundry Delivery',
            description='Test service',
            price=Decimal('1000.00'),
        )

    def test_delivery_code_is_generated_and_verified(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='accepted_delivery',
            delivery_rider=self.rider,
            price=Decimal('2500.00'),
            service_location=self.location,
        )
        order.services.add(self.service)

        code = order.generate_delivery_code()

        self.assertTrue(code.isdigit())
        self.assertEqual(len(code), 4)
        self.assertTrue(order.verify_delivery_code(code))
        self.assertFalse(order.verify_delivery_code('0000'))
        order.refresh_from_db()
        self.assertEqual(order.delivery_code, code)

    def test_delivery_code_is_only_included_for_own_active_order(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='accepted_delivery',
            delivery_rider=self.rider,
            delivery_code='1234',
            price=Decimal('2500.00'),
            service_location=self.location,
        )

        class Request:
            user = self.customer

        self.assertEqual(
            OrderListLightSerializer(order, context={'request': Request()}).data['delivery_code'],
            '1234',
        )

        Request.user = self.rider
        self.assertIsNone(
            OrderListLightSerializer(order, context={'request': Request()}).data['delivery_code'],
        )

        Request.user = self.customer
        order.status = 'delivered'
        self.assertIsNone(
            OrderListLightSerializer(order, context={'request': Request()}).data['delivery_code'],
        )

    def test_delivery_code_is_not_reused_after_new_generation(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='accepted_delivery',
            delivery_rider=self.rider,
            price=Decimal('2500.00'),
            service_location=self.location,
        )
        order.services.add(self.service)

        first_code = order.generate_delivery_code()
        second_code = order.generate_delivery_code()

        self.assertNotEqual(first_code, second_code)
        self.assertTrue(order.verify_delivery_code(second_code))
        self.assertFalse(order.verify_delivery_code(first_code))
