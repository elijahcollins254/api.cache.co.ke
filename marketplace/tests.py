from django.test import TestCase
from django.test import RequestFactory
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from .models import MerchantProfile, MerchantLedgerEntry, MerchantOrderFulfillment, MerchantWallet, Shop, ShopCategory, Product, ProductCategory
from services.models import Service, ServiceCategory
from marketplace.admin import ProductAdmin, ProductCategoryAdmin, activate_products, deactivate_products
from services.admin import ServiceCategoryAdmin
from orders.models import Order, ProductOrderItem
from payments.models import Payment

User = get_user_model()


class MarketplaceModelsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='merchant1',
            email='merchant1@example.com',
            password='SecurePass123',
            phone='+254712345678',
            role='merchant',
        )
        self.merchant = MerchantProfile.objects.create(
            user=self.user,
            business_name='Fresh Basket',
            merchant_username='freshbasket',
            phone='+254712345678',
            status='verified',
        )

    def test_shop_slug_and_username_are_unique(self):
        shop = Shop.objects.create(
            owner=self.merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            description='Groceries and home goods',
            is_verified=True,
        )
        self.assertEqual(shop.owner.merchant_username, 'freshbasket')

    def test_product_requires_shop_and_price(self):
        with self.assertRaises(ValidationError):
            product = Product(
                shop=None,
                name='Tomatoes',
                slug='tomatoes',
                price='0.00',
            )
            product.full_clean()

    def test_product_slug_is_generated_from_name_when_missing(self):
        shop = Shop.objects.create(
            owner=self.merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            description='Groceries and home goods',
            is_verified=True,
        )
        product = Product.objects.create(
            shop=shop,
            name='Fresh Avocado',
            price='120.00',
        )
        self.assertEqual(product.slug, 'fresh-avocado')

    def test_product_admin_bulk_actions_activate_and_deactivate_products(self):
        shop = Shop.objects.create(
            owner=self.merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
        )
        product = Product.objects.create(
            shop=shop,
            name='Fresh Avocado',
            slug='fresh-avocado',
            price='120.00',
            is_active=False,
        )

        activate_products(None, None, Product.objects.filter(pk=product.pk))
        product.refresh_from_db()
        self.assertTrue(product.is_active)

        deactivate_products(None, None, Product.objects.filter(pk=product.pk))
        product.refresh_from_db()
        self.assertFalse(product.is_active)

    def test_product_admin_registers_bulk_status_actions(self):
        self.assertIn(activate_products, ProductAdmin.actions)
        self.assertIn(deactivate_products, ProductAdmin.actions)

    def test_shop_category_slug_is_generated_from_name(self):
        category = ShopCategory.objects.create(name='Home & Living')
        self.assertEqual(category.slug, 'home-living')

    def test_service_migration_moves_every_service_to_cache_liqour(self):
        from importlib import import_module
        from django.apps import apps

        legacy_shop = Shop.objects.create(
            owner=self.merchant,
            name='Legacy Service Shop',
            slug='legacy-service-shop',
            status='suspended',
            is_verified=False,
        )
        assigned_service = Service.objects.create(
            shop=legacy_shop,
            name='Legacy Assigned Service',
            price='100.00',
        )
        unassigned_service = Service.objects.create(
            name='Legacy Unassigned Service',
            price='150.00',
        )

        migration = import_module('services.migrations.0011_move_services_to_cache_liqour')
        migration.move_services_to_cache_liqour(apps, None)

        cache_liqour = Shop.objects.get(slug='cache-liqour')
        assigned_service.refresh_from_db()
        unassigned_service.refresh_from_db()
        self.assertEqual(assigned_service.shop_id, cache_liqour.id)
        self.assertEqual(unassigned_service.shop_id, cache_liqour.id)
        self.assertEqual(cache_liqour.status, 'active')
        self.assertTrue(cache_liqour.is_verified)

    def test_legacy_services_are_imported_as_products_idempotently(self):
        from importlib import import_module
        from django.apps import apps

        service_category, _ = ServiceCategory.objects.get_or_create(
            name='Cane Spirit',
            defaults={'slug': 'cane-spirit'},
        )
        service = Service.objects.create(
            name='Kenyan Cane Pineapple 750 ml',
            shop=None,
            category=service_category,
            price='850.00',
            description='Legacy product description',
            image_url='https://example.com/cane.jpg',
            is_active=False,
        )
        cache_liqour, _ = Shop.objects.get_or_create(
            slug='cache-liqour',
            defaults={
                'name': 'Cache Liqour',
                'status': 'active',
                'is_verified': True,
            },
        )

        migration = import_module('marketplace.migrations.0004_import_legacy_services_as_products')
        migration.import_legacy_services_as_products(apps, None)
        migration.import_legacy_services_as_products(apps, None)

        product = Product.objects.get(shop=cache_liqour, name=service.name)
        self.assertEqual(product.slug, 'kenyan-cane-pineapple-750-ml')
        self.assertEqual(product.stock_quantity, 20)
        self.assertEqual(product.category.name, 'Cane Spirit')
        self.assertEqual(str(product.price), str(service.price))
        self.assertEqual(product.image_url, service.image_url)
        self.assertFalse(product.is_active)
        self.assertEqual(Product.objects.filter(shop=cache_liqour, name=service.name).count(), 1)
        self.assertTrue(Service.objects.filter(pk=service.pk).exists())


class MarketplaceAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='merchant_api_user',
            email='merchant_api@example.com',
            password='SecurePass123',
            phone='+254712345678',
            role='merchant',
        )

    def test_merchant_registration_endpoint_creates_profile(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            '/marketplace/merchants/register/',
            {
                'merchant_username': 'freshbasket',
                'business_name': 'Fresh Basket',
                'phone': '+254712345678',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(MerchantProfile.objects.filter(merchant_username='freshbasket').exists())

    def test_authenticated_user_can_create_shop(self):
        self.client.force_authenticate(user=self.user)
        MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
            status='verified',
        )

        response = self.client.post(
            '/marketplace/shops/',
            {
                'name': 'Fresh Basket Shop',
                'slug': 'fresh-basket-shop',
                'description': 'Groceries and home goods',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Shop.objects.filter(slug='fresh-basket-shop').exists())

    def test_shop_categories_are_loaded_from_api_and_attached_to_shops(self):
        category = ShopCategory.objects.create(name='Groceries', slug='groceries')
        self.client.force_authenticate(user=self.user)
        MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
            status='verified',
        )

        create_response = self.client.post(
            '/marketplace/shops/',
            {
                'name': 'Fresh Basket Shop',
                'slug': 'fresh-basket-shop',
                'category_id': category.id,
            },
            format='json',
        )
        categories_response = self.client.get('/marketplace/categories/')

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data['category']['slug'], 'groceries')
        self.assertEqual(categories_response.status_code, 200)
        categories = categories_response.data
        if isinstance(categories, dict):
            categories = categories.get('results', [])
        self.assertTrue(any(item['slug'] == 'groceries' for item in categories))

    def test_authenticated_merchant_can_create_product(self):
        self.client.force_authenticate(user=self.user)
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
            status='verified',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            description='Groceries and home goods',
            is_verified=True,
            status='active',
        )

        response = self.client.post(
            '/marketplace/products/',
            {
                'shop': shop.id,
                'name': 'Fresh Avocado',
                'price': '120.00',
                'description': 'Farm fresh avocados',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Product.objects.filter(slug='fresh-avocado').exists())

    def test_public_products_endpoint_includes_shop_identity(self):
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
            status='verified',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            description='Groceries and home goods',
            is_verified=True,
            status='active',
        )
        Product.objects.create(
            shop=shop,
            name='Fresh Avocado',
            slug='fresh-avocado',
            price='120.00',
            description='Farm fresh avocados',
            is_active=True,
        )

        response = self.client.get('/marketplace/products/')

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data), 0)
        self.assertIsInstance(response.data[0]['shop'], dict)
        self.assertEqual(response.data[0]['shop']['merchant_username'], 'freshbasket')

    def test_catalog_endpoint_returns_services_and_products(self):
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
            status='verified',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            description='Groceries and laundry',
            is_verified=True,
            status='active',
        )
        category, _ = ServiceCategory.objects.get_or_create(
            slug='laundry',
            defaults={'name': 'Laundry', 'is_active': True},
        )
        Service.objects.create(
            shop=shop,
            category=category,
            name='Express Wash',
            price='250.00',
            description='Fast wash and fold service',
            is_active=True,
        )
        Product.objects.create(
            shop=shop,
            name='Fresh Avocado',
            slug='fresh-avocado',
            price='120.00',
            description='Farm fresh avocados',
            is_active=True,
        )

        response = self.client.get('/services/catalog/')

        self.assertEqual(response.status_code, 200)
        names = [item['name'] for item in response.data]
        self.assertIn('Express Wash', names)
        self.assertIn('Fresh Avocado', names)
        self.assertTrue(any(item['type'] == 'service' and item['name'] == 'Express Wash' for item in response.data))
        self.assertTrue(any(item['type'] == 'product' and item['name'] == 'Fresh Avocado' for item in response.data))

    def test_catalog_endpoint_paginates_and_searches_on_request(self):
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            is_verified=True,
            status='active',
        )
        Product.objects.create(shop=shop, name='Fresh Avocado', price='120.00')
        Product.objects.create(shop=shop, name='Red Apple', price='80.00')

        response = self.client.get('/services/catalog/?page_size=1')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 1)
        self.assertIsNotNone(response.data['next'])

        search_response = self.client.get('/services/catalog/?page_size=24&search=apple')

        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.data['count'], 1)
        self.assertEqual(search_response.data['results'][0]['name'], 'Red Apple')

    def test_catalog_endpoint_filters_paginated_items_by_shop(self):
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            is_verified=True,
            status='active',
        )
        other_shop = Shop.objects.create(
            owner=merchant,
            name='Other Shop',
            slug='other-shop',
            is_verified=True,
            status='active',
        )
        Product.objects.create(shop=shop, name='Fresh Avocado', price='120.00')
        Product.objects.create(shop=shop, name='Red Apple', price='80.00')
        Product.objects.create(shop=other_shop, name='Other Product', price='50.00')

        response = self.client.get('/services/catalog/?shop=fresh-basket-shop&page_size=1')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['shop']['slug'], 'fresh-basket-shop')
        self.assertIsNotNone(response.data['next'])

    def test_catalog_popularity_uses_paid_non_cancelled_sales(self):
        merchant = MerchantProfile.objects.create(
            user=self.user,
            merchant_username='freshbasket',
            business_name='Fresh Basket',
            phone='+254712345678',
        )
        shop = Shop.objects.create(
            owner=merchant,
            name='Fresh Basket Shop',
            slug='fresh-basket-shop',
            is_verified=True,
            status='active',
        )
        groceries = ProductCategory.objects.create(
            name='Groceries',
            slug='groceries',
            featured_on_homepage=True,
        )
        liquor = ProductCategory.objects.create(name='Liquor', slug='liquor')
        popular_product = Product.objects.create(
            shop=shop,
            category=groceries,
            name='Fresh Avocado',
            price='120.00',
            stock_quantity=10,
        )
        other_product = Product.objects.create(
            shop=shop,
            category=liquor,
            name='Red Wine',
            price='800.00',
            stock_quantity=10,
        )

        paid_order = Order.objects.create(
            user=self.user,
            pickup_address='Shop',
            dropoff_address='Customer',
        )
        ProductOrderItem.objects.create(
            order=paid_order,
            product=popular_product,
            product_name=popular_product.name,
            unit_price=popular_product.price,
            quantity=4,
        )
        ProductOrderItem.objects.create(
            order=paid_order,
            product=other_product,
            product_name=other_product.name,
            unit_price=other_product.price,
            quantity=2,
        )
        Payment.objects.create(
            user=self.user,
            order_id=paid_order.id,
            amount='2080.00',
            phone_number='+254712345678',
            status=Payment.STATUS_SUCCESS,
        )

        unpaid_order = Order.objects.create(
            user=self.user,
            pickup_address='Shop',
            dropoff_address='Customer',
        )
        ProductOrderItem.objects.create(
            order=unpaid_order,
            product=other_product,
            product_name=other_product.name,
            unit_price=other_product.price,
            quantity=10,
        )
        Payment.objects.create(
            user=self.user,
            order_id=unpaid_order.id,
            amount='8000.00',
            phone_number='+254712345678',
            status=Payment.STATUS_FAILED,
        )

        cancelled_order = Order.objects.create(
            user=self.user,
            pickup_address='Shop',
            dropoff_address='Customer',
            status='cancelled',
        )
        ProductOrderItem.objects.create(
            order=cancelled_order,
            product=other_product,
            product_name=other_product.name,
            unit_price=other_product.price,
            quantity=20,
        )
        Payment.objects.create(
            user=self.user,
            order_id=cancelled_order.id,
            amount='16000.00',
            phone_number='+254712345678',
            status=Payment.STATUS_SUCCESS,
        )

        popularity_response = self.client.get('/services/catalog/popularity/')
        catalog_response = self.client.get('/services/catalog/?page_size=24&ordering=popular')

        self.assertEqual(popularity_response.status_code, 200)
        self.assertEqual(
            popularity_response.data['featured_category'],
            {'type': 'product', 'slug': 'groceries', 'name': 'Groceries'},
        )
        category_sales = {
            (category['type'], category['slug']): category['units_sold']
            for category in popularity_response.data['categories']
        }
        self.assertEqual(category_sales[('product', 'groceries')], 4)
        self.assertEqual(category_sales[('product', 'liquor')], 2)
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(catalog_response.data['results'][0]['id'], popular_product.id)
        self.assertEqual(catalog_response.data['results'][0]['units_sold'], 4)

    def test_admin_featured_category_selection_is_exclusive(self):
        product_category = ProductCategory.objects.create(
            name='Groceries',
            slug='groceries',
            featured_on_homepage=True,
        )
        service_category = ServiceCategory.objects.create(name='Laundry', slug='laundry')
        request = RequestFactory().post('/admin/')

        service_category.featured_on_homepage = True
        ServiceCategoryAdmin(ServiceCategory, admin.site).save_model(
            request,
            service_category,
            form=None,
            change=True,
        )
        product_category.refresh_from_db()
        self.assertFalse(product_category.featured_on_homepage)

        product_category.featured_on_homepage = True
        ProductCategoryAdmin(ProductCategory, admin.site).save_model(
            request,
            product_category,
            form=None,
            change=True,
        )
        service_category.refresh_from_db()
        self.assertFalse(service_category.featured_on_homepage)


class MerchantDashboardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.merchant_user = User.objects.create_user(
            username='dashboard_merchant',
            password='SecurePass123',
            phone='+254712345678',
            role='merchant',
        )
        self.merchant = MerchantProfile.objects.create(
            user=self.merchant_user,
            merchant_username='dashboard-merchant',
            business_name='Dashboard Merchant',
            phone='+254712345678',
        )
        self.customer = User.objects.create_user(username='merchant_customer', password='secret123')
        self.rider = User.objects.create_user(username='merchant_rider', password='secret123', role='rider')
        self.admin = User.objects.create_user(username='merchant_admin', password='secret123', role='admin')
        self.shop = Shop.objects.create(
            owner=self.merchant,
            name='Merchant Shop',
            slug='merchant-shop',
        )
        self.product = Product.objects.create(
            shop=self.shop,
            name='Fresh Produce Box',
            price='120.00',
        )
        self.order = Order.objects.create(
            user=self.customer,
            pickup_address='Merchant pickup point',
            dropoff_address='Customer address',
            status='requested',
        )
        ProductOrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price='120.00',
            quantity=1,
        )
        Payment.objects.create(
            user=self.customer,
            order_id=self.order.id,
            amount='120.00',
            phone_number='+254712345678',
            status=Payment.STATUS_SUCCESS,
        )

    def test_merchant_sees_its_paid_order_and_can_prepare_it(self):
        self.client.force_authenticate(user=self.merchant_user)

        response = self.client.get('/marketplace/merchant/orders/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['orders']), 1)
        self.assertEqual(response.data['orders'][0]['items'][0]['name'], 'Fresh Produce Box')
        self.assertTrue(response.data['orders'][0]['is_paid'])

        accepted = self.client.post(
            f'/marketplace/merchant/orders/{self.order.id}/fulfillment/',
            {'action': 'accept'},
            format='json',
        )
        prepared = self.client.post(
            f'/marketplace/merchant/orders/{self.order.id}/fulfillment/',
            {'action': 'prepare'},
            format='json',
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(prepared.status_code, 200)
        self.assertEqual(prepared.data['fulfillment_status'], MerchantOrderFulfillment.STATUS_PREPARED)

    def test_admin_can_select_merchants_and_view_read_only_dashboard(self):
        other_user = User.objects.create_user(username='other_dashboard_merchant', password='secret123', role='merchant')
        other_merchant = MerchantProfile.objects.create(
            user=other_user,
            merchant_username='other-dashboard-merchant',
            business_name='Other Dashboard Merchant',
            phone='+254700000001',
        )
        self.client.force_authenticate(user=self.admin)

        directory = self.client.get('/marketplace/merchant/admin/')
        orders = self.client.get(f'/marketplace/merchant/orders/?merchant_id={self.merchant.id}')
        earnings = self.client.get(f'/marketplace/merchant/earnings/?merchant_id={self.merchant.id}')
        missing_selection = self.client.get('/marketplace/merchant/orders/')
        fulfillment = self.client.post(
            f'/marketplace/merchant/orders/{self.order.id}/fulfillment/',
            {'action': 'accept'},
            format='json',
        )
        withdrawal = self.client.post(
            '/marketplace/merchant/earnings/',
            {'amount': '50', 'phone_number': '+254712345678'},
            format='json',
        )

        self.assertEqual(directory.status_code, 200)
        self.assertEqual({item['id'] for item in directory.data['merchants']}, {self.merchant.id, other_merchant.id})
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(orders.data['merchant_id'], self.merchant.id)
        self.assertEqual(len(orders.data['orders']), 1)
        self.assertEqual(earnings.status_code, 200)
        self.assertEqual(fulfillment.status_code, 403)
        self.assertEqual(withdrawal.status_code, 403)
        self.assertEqual(missing_selection.status_code, 400)

    def test_merchant_cannot_select_another_merchants_dashboard(self):
        other_user = User.objects.create_user(username='other_shop_owner', password='secret123', role='merchant')
        other_merchant = MerchantProfile.objects.create(
            user=other_user,
            merchant_username='other-shop-owner',
            business_name='Other Shop Owner',
            phone='+254700000002',
        )
        self.client.force_authenticate(user=self.merchant_user)

        response = self.client.get(f'/marketplace/merchant/orders/?merchant_id={other_merchant.id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['merchant_id'], self.merchant.id)
        self.assertEqual(len(response.data['orders']), 1)

    def test_non_admin_cannot_list_merchants(self):
        self.client.force_authenticate(user=self.merchant_user)

        response = self.client.get('/marketplace/merchant/admin/')

        self.assertEqual(response.status_code, 403)

    def test_rider_cannot_accept_order_until_merchant_prepares_it(self):
        self.client.force_authenticate(user=self.rider)

        response = self.client.post('/orders/rider/', {'order_id': self.order.id, 'action': 'accept'}, format='json')

        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.order.rider_id)

    def test_delivery_credits_product_sales_only_once(self):
        MerchantOrderFulfillment.objects.create(
            merchant=self.merchant,
            order=self.order,
            status=MerchantOrderFulfillment.STATUS_PREPARED,
        )
        self.order.status = 'delivered'
        self.order.save(update_fields=['status', 'updated_at'])

        from .merchant_payouts import credit_completed_order
        credit_completed_order(self.order)
        credit_completed_order(self.order)

        wallet = MerchantWallet.objects.get(merchant=self.merchant)
        self.assertEqual(wallet.balance, 120)
        self.assertEqual(
            MerchantLedgerEntry.objects.filter(
                merchant=self.merchant,
                order=self.order,
                entry_type=MerchantLedgerEntry.TYPE_EARNING,
            ).count(),
            1,
        )
