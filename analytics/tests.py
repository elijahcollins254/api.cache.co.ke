from rest_framework.test import APITestCase

from users.models import User

from .models import FriendConnection


class ContactSyncTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='contact-owner',
            password='test-password',
            phone='0712345678',
        )
        self.friend = User.objects.create_user(
            username='contact-friend',
            password='test-password',
            phone='0723456789',
            first_name='Cache',
            last_name='Friend',
        )
        self.other_friend = User.objects.create_user(
            username='other-contact-friend',
            password='test-password',
            phone='0734567890',
        )
        self.client.force_authenticate(user=self.user)
        self.sync_url = '/analytics/contacts/sync/'
        self.friends_url = '/analytics/friends/'
        self.contacts_url = '/analytics/contacts/'

    def test_sync_replaces_friend_map_and_returns_minimal_profiles(self):
        response = self.client.post(
            self.sync_url,
            {'phone_numbers': ['0723456789', '+254 723 456 789', '0745678901']},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['matched_count'], 1)
        self.assertEqual(response.data['friends'], [{
            'id': self.friend.pk,
            'username': 'contact-friend',
            'first_name': 'Cache',
            'last_name': 'Friend',
        }])
        self.assertNotIn('phone', response.data['friends'][0])
        self.assertTrue(
            FriendConnection.objects.filter(owner=self.user, friend=self.friend).exists()
        )
        self.assertFalse(
            FriendConnection.objects.filter(owner=self.user, friend=self.user).exists()
        )
        self.assertFalse(
            FriendConnection.objects.filter(owner=self.user, friend=self.other_friend).exists()
        )

    def test_empty_sync_clears_existing_map_without_saving_unmatched_numbers(self):
        FriendConnection.objects.create(owner=self.user, friend=self.friend)

        response = self.client.post(
            self.sync_url,
            {'phone_numbers': []},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['matched_count'], 0)
        self.assertFalse(FriendConnection.objects.filter(owner=self.user).exists())

    def test_rejects_invalid_phone_numbers(self):
        response = self.client.post(
            self.sync_url,
            {'phone_numbers': ['not a phone number']},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(FriendConnection.objects.filter(owner=self.user).exists())

    def test_friend_list_is_scoped_to_authenticated_user(self):
        FriendConnection.objects.create(owner=self.user, friend=self.friend)
        FriendConnection.objects.create(owner=self.other_friend, friend=self.user)

        response = self.client.get(self.friends_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['matched_count'], 1)
        self.assertEqual(response.data['friends'][0]['id'], self.friend.pk)

    def test_delete_clears_only_current_users_friend_map(self):
        FriendConnection.objects.create(owner=self.user, friend=self.friend)
        FriendConnection.objects.create(owner=self.other_friend, friend=self.user)

        response = self.client.delete(self.contacts_url)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(FriendConnection.objects.filter(owner=self.user).exists())
        self.assertTrue(FriendConnection.objects.filter(owner=self.other_friend).exists())

    def test_contact_sync_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.post(
            self.sync_url,
            {'phone_numbers': ['0723456789']},
            format='json',
        )

        self.assertEqual(response.status_code, 401)

# Create your tests here.
