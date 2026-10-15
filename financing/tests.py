from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import ShareInvestment


User = get_user_model()


class ShareInvestmentApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.investor = User.objects.create_user(
            username='investor',
            password='test-password',
            phone='0712345678',
        )

    def test_anyone_can_create_an_investment_without_a_minimum_plan(self):
        response = self.client.post(
            '/loans/share-investments/',
            {'amount': 1, 'phone': '0712345678'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        investment = ShareInvestment.objects.get(pk=response.data['id'])
        self.assertEqual(investment.amount, Decimal('1.00'))
        self.assertEqual(investment.phone_number, '254712345678')
        self.assertIsNone(investment.user)
        self.assertEqual(response.data['shares_percentage'], 0.00001)

    def test_invalid_phone_is_rejected(self):
        response = self.client.post(
            '/loans/share-investments/',
            {'amount': 100, 'phone': '12345'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ShareInvestment.objects.exists())

    def test_investor_can_only_list_their_own_contributions(self):
        own_investment = ShareInvestment.objects.create(
            user=self.investor,
            amount=Decimal('250'),
            phone_number='254712345678',
        )
        ShareInvestment.objects.create(amount=Decimal('500'), phone_number='254722222222')
        self.client.force_authenticate(user=self.investor)

        response = self.client.get('/loans/share-investments/mine/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data], [str(own_investment.id)])

    def test_public_overview_counts_only_successful_share_payments(self):
        ShareInvestment.objects.create(
            amount=Decimal('100000'),
            phone_number='254712345678',
            status='success',
            completed_at=timezone.now(),
        )
        ShareInvestment.objects.create(
            amount=Decimal('50000'),
            phone_number='254722222222',
            status='pending',
        )

        response = self.client.get('/loans/share-investments/overview/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['fundraising']['total_raised'], 100000)
        self.assertEqual(response.data['fundraising']['investor_count'], 1)
        self.assertEqual(response.data['fundraising']['shares_issued_percentage'], 1)
        self.assertEqual(len(response.data['business']['monthly_sales']), 6)
