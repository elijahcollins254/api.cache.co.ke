from unittest.mock import Mock, patch

from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from users.models import User

from offers.admin import OfferAdmin
from offers.models import Offer, OfferNotificationSubscription, OfferPushDevice, UserOffer
from offers.services import notify_push_devices_of_new_offer


class OfferClaimNotificationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='offer-customer',
            password='test-password',
            phone='0712345678',
        )
        self.offer = Offer.objects.create(
            title='Weekend Wash',
            description='Save on weekend laundry.',
            discount_percent=20,
            code='WEEKEND20',
            valid_from=timezone.now(),
        )
        self.url = f'/offers/{self.offer.pk}/claim'
        self.client.force_authenticate(user=self.user)

    @patch('offers.views.send_offer_claim_confirmation')
    def test_claim_sends_confirmation_sms(self, send_confirmation):
        send_confirmation.return_value = {'status': 'success'}

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(UserOffer.objects.filter(user=self.user, offer=self.offer).exists())
        send_confirmation.assert_called_once_with(self.user, self.offer)
        self.assertNotIn('sms_notification', response.data)

    @patch('offers.views.send_offer_claim_confirmation')
    def test_claim_is_kept_when_confirmation_sms_fails(self, send_confirmation):
        send_confirmation.return_value = {'status': 'error', 'message': 'provider unavailable'}

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(UserOffer.objects.filter(user=self.user, offer=self.offer).exists())
        self.assertEqual(response.data['sms_notification'], 'Offer claimed, but the confirmation SMS could not be sent.')


class OfferSubscriptionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='subscription-customer',
            password='test-password',
            phone='0712345678',
        )
        self.client.force_authenticate(user=self.user)

    @patch('offers.services.send_offer_sms')
    def test_subscribing_does_not_send_sms(self, send_sms):
        response = self.client.post('/offers/subscriptions/my_subscription')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            OfferNotificationSubscription.objects.get(user=self.user).is_active
        )
        send_sms.assert_not_called()

    @patch('offers.services.send_offer_sms')
    def test_unsubscribing_does_not_send_sms(self, send_sms):
        subscription = OfferNotificationSubscription.objects.create(
            user=self.user,
            phone_number=self.user.phone,
            is_active=True,
        )

        response = self.client.post('/offers/subscriptions/unsubscribe')

        self.assertEqual(response.status_code, 200)
        subscription.refresh_from_db()
        self.assertFalse(subscription.is_active)
        send_sms.assert_not_called()


class OfferPushDeviceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='push-customer',
            password='test-password',
            phone='0712345678',
        )
        self.client.force_authenticate(user=self.user)
        self.url = '/offers/push-devices/'

    def test_registers_push_token_for_authenticated_user(self):
        response = self.client.post(
            self.url,
            {'token': 'ExponentPushToken[device-token]'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            OfferPushDevice.objects.filter(
                user=self.user,
                token='ExponentPushToken[device-token]',
            ).exists()
        )

    def test_rejects_invalid_push_token(self):
        response = self.client.post(self.url, {'token': 'not-an-expo-token'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(OfferPushDevice.objects.count(), 0)

    def test_unregister_only_removes_current_users_device(self):
        other_user = User.objects.create_user(username='other-push-user', password='test-password')
        device = OfferPushDevice.objects.create(
            user=other_user,
            token='ExponentPushToken[other-device]',
        )

        response = self.client.delete(
            self.url,
            {'token': device.token},
            format='json',
        )

        self.assertEqual(response.status_code, 204)
        self.assertTrue(OfferPushDevice.objects.filter(pk=device.pk).exists())


class OfferPushDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='offer-push-user', password='test-password')
        self.offer = Offer.objects.create(
            title='Weekend Wash',
            description='Save on weekend laundry.',
            discount_percent=20,
            code='WEEKEND20',
            valid_from=timezone.now(),
        )

    @patch('offers.services.requests.post')
    def test_sends_offer_push_to_registered_device(self, post):
        OfferPushDevice.objects.create(
            user=self.user,
            token='ExponentPushToken[device-token]',
        )
        response = Mock()
        response.json.return_value = {'data': [{'status': 'ok'}]}
        post.return_value = response

        result = notify_push_devices_of_new_offer(self.offer)

        self.assertEqual(result, {'sent': 1, 'failed': 0})
        post.assert_called_once()
        message = post.call_args.kwargs['json'][0]
        self.assertEqual(message['to'], 'ExponentPushToken[device-token]')
        self.assertEqual(message['title'], 'New offer: Weekend Wash')
        self.assertEqual(message['data'], {'screen': '/offers', 'offerId': self.offer.pk})

    @patch('offers.admin.notify_subscribers_of_new_offer')
    @patch('offers.admin.notify_push_devices_of_new_offer')
    def test_admin_creation_sends_push_for_active_offer(self, notify_push, notify_sms):
        admin_user = User.objects.create_superuser(
            username='offer-admin',
            password='test-password',
            phone='0711111111',
        )
        request = RequestFactory().post('/admin/offers/offer/add/')
        request.user = admin_user
        offer = Offer(
            title='New Offer',
            description='A new discount.',
            discount_percent=10,
            code='NEWOFFER',
            valid_from=timezone.now(),
        )

        OfferAdmin(Offer, admin.site).save_model(request, offer, form=None, change=False)

        notify_push.assert_called_once_with(offer)
        notify_sms.assert_called_once_with(offer)
