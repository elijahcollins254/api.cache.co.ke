from django.test import TestCase
from django.contrib.auth import get_user_model
from decimal import Decimal
from types import SimpleNamespace

from rest_framework.test import APIClient

from orders.models import Order, OrderEvent
from orders.serializers import OrderListLightSerializer, OrderListSerializer
from orders.views import OrderListCreateView
from services.models import Service
from users.models import Location

User = get_user_model()


class AdminDashboardSummaryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='dashboard_admin',
            password='secret123',
            role='admin',
        )
        self.customer = User.objects.create_user(
            username='dashboard_customer',
            password='secret123',
            role='customer',
        )
        self.rider = User.objects.create_user(
            username='dashboard_rider',
            password='secret123',
            role='rider',
        )
        self.client = APIClient()

    def test_summary_aggregates_all_orders_and_user_counts(self):
        Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='delivered',
            delivery_rider=self.rider,
            price=Decimal('2500.00'),
        )
        Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='requested',
            pickup_rider=self.rider,
            price=Decimal('1200.00'),
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get('/orders/admin-summary/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            'total_orders': 2,
            'in_progress': 1,
            'completed': 1,
            'revenue': 3700.0,
            'active_riders': 1,
            'total_users': 3,
        })

    def test_summary_is_restricted_to_administrators(self):
        self.client.force_authenticate(user=self.customer)

        response = self.client.get('/orders/admin-summary/')

        self.assertEqual(response.status_code, 403)


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

    def test_code_filtered_orders_use_the_full_serializer(self):
        view = OrderListCreateView()
        view.request = SimpleNamespace(
            method='GET',
            query_params={'code': 'ORDER-CODE'},
        )
        self.assertIs(view.get_serializer_class(), OrderListSerializer)

        list_view = OrderListCreateView()
        list_view.request = SimpleNamespace(method='GET', query_params={})
        self.assertIs(list_view.get_serializer_class(), OrderListSerializer)

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

    def test_timeline_includes_order_creation_and_status_events_for_customer(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='at_gate',
            delivery_rider=self.rider,
            price=Decimal('2500.00'),
            service_location=self.location,
        )
        OrderEvent.objects.create(
            order=order,
            actor=self.rider,
            event_type='status_changed',
            data={'old': 'picked', 'new': 'at_gate'},
        )

        class Request:
            user = self.customer

        timeline = OrderListLightSerializer(order, context={'request': Request()}).data['timeline']
        self.assertEqual([event['label'] for event in timeline], ['Order placed', 'At the gate'])
        self.assertTrue(timeline[0]['timestamp'])
        self.assertTrue(timeline[1]['timestamp'])

        Request.user = self.rider
        self.assertEqual(
            OrderListLightSerializer(order, context={'request': Request()}).data['timeline'],
            [],
        )

    def test_full_order_serializer_includes_customer_timeline_and_delivery_code(self):
        order = Order.objects.create(
            user=self.customer,
            pickup_address='Main Road',
            dropoff_address='Gate B',
            status='at_gate',
            delivery_rider=self.rider,
            delivery_code='4321',
            price=Decimal('2500.00'),
            service_location=self.location,
        )
        OrderEvent.objects.create(
            order=order,
            actor=self.rider,
            event_type='status_changed',
            data={'old': 'picked', 'new': 'at_gate'},
        )

        class Request:
            user = self.customer

        serialized = OrderListSerializer(order, context={'request': Request()}).data
        self.assertEqual(serialized['delivery_code'], '4321')
        self.assertEqual(serialized['timeline'][0]['event_type'], 'status_changed')

        Request.user = self.rider
        serialized_for_rider = OrderListSerializer(order, context={'request': Request()}).data
        self.assertIsNone(serialized_for_rider['delivery_code'])
        self.assertTrue(all(
            event['event_type'] == 'status_changed'
            for event in serialized_for_rider['timeline']
        ))

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
