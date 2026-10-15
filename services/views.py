from datetime import timedelta

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Case, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Lower
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import viewsets, permissions, filters
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from marketplace.models import Product
from marketplace.serializers import ShopSerializer
from orders.models import OrderItem, ProductOrderItem
from payments.models import Payment
from .models import Service, ServiceCategory
from .serializers import ServiceCategorySerializer, ServiceSerializer


class CatalogPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = 'page_size'
    max_page_size = 48


class ServiceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Expose active service categories for catalogue filters."""
    queryset = ServiceCategory.objects.filter(is_active=True).order_by('name')
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.AllowAny]


class ServiceViewSet(viewsets.ModelViewSet):
    """List and manage service catalogue (laundry, duvet, carpet, fumigation, etc.)"""
    queryset = Service.objects.select_related(
        'category', 'shop', 'shop__category', 'shop__owner'
    ).order_by('category', 'name')
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'category', 'description']
    ordering_fields = ['price', 'name']

    def get_queryset(self):
        queryset = super().get_queryset()
        shop_slug = self.request.query_params.get('shop')
        if shop_slug:
            queryset = queryset.filter(shop__slug=shop_slug)
        if self.action in ['list', 'retrieve']:
            queryset = queryset.filter(is_active=True)
            queryset = queryset.exclude(shop__isnull=True)
            queryset = queryset.filter(shop__status='active')
            if not self.request.user.is_authenticated:
                queryset = queryset.filter(shop__is_verified=True)
        return queryset

    @action(detail=False, methods=['get'], url_path='catalog')
    def catalog(self, request):
        services = self.get_queryset().filter(is_active=True)
        products = Product.objects.filter(is_active=True).select_related(
            'shop', 'shop__category', 'shop__owner', 'shop__owner__user'
        )
        shop_slug = request.query_params.get('shop')
        if shop_slug:
            services = services.filter(shop__slug=shop_slug)
            products = products.filter(shop__slug=shop_slug)
        if not request.user.is_authenticated:
            products = products.filter(shop__is_verified=True, shop__status='active')

        search = request.query_params.get('search', '').strip()
        if search:
            services = services.filter(
                Q(name__icontains=search)
                | Q(description__icontains=search)
                | Q(category__name__icontains=search)
                | Q(category_name__icontains=search)
                | Q(shop__name__icontains=search)
            )
            products = products.filter(
                Q(name__icontains=search)
                | Q(description__icontains=search)
                | Q(category__name__icontains=search)
                | Q(shop__name__icontains=search)
            )

        paginated = 'page' in request.query_params or 'page_size' in request.query_params
        popular = request.query_params.get('ordering') == 'popular' and paginated
        page_size = None
        page_number = None
        total_count = None
        if paginated:
            paginator = CatalogPagination()
            page_size = paginator.get_page_size(request)
            try:
                page_number = int(request.query_params.get('page', '1'))
            except ValueError as exc:
                raise ValidationError({'page': 'Enter a valid page number.'}) from exc
            if page_number < 1:
                raise ValidationError({'page': 'Page numbers must be greater than zero.'})

            total_count = services.count() + products.count()
            fetch_limit = page_number * page_size
            if popular:
                since = timezone.now() - timedelta(days=90)
                paid_order_ids = Payment.objects.filter(
                    status=Payment.STATUS_SUCCESS,
                    order_id__isnull=False,
                ).values('order_id')
                service_sales = OrderItem.objects.filter(
                    service_id=OuterRef('pk'),
                    order_id__in=paid_order_ids,
                    order__created_at__gte=since,
                ).exclude(order__status='cancelled').values('service_id').annotate(
                    units=Sum('quantity')
                ).values('units')[:1]
                product_sales = ProductOrderItem.objects.filter(
                    product_id=OuterRef('pk'),
                    order_id__in=paid_order_ids,
                    order__created_at__gte=since,
                ).exclude(order__status='cancelled').values('product_id').annotate(
                    units=Sum('quantity')
                ).values('units')[:1]
                services = services.annotate(
                    units_sold=Coalesce(Subquery(service_sales, output_field=IntegerField()), Value(0))
                ).order_by('-units_sold', Lower('name'), 'pk')[:fetch_limit]
                products = products.annotate(
                    units_sold=Case(
                        When(
                            stock_quantity__gt=0,
                            then=Coalesce(Subquery(product_sales, output_field=IntegerField()), Value(0)),
                        ),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ).order_by('-units_sold', Lower('name'), 'pk')[:fetch_limit]
            else:
                services = services.order_by(Lower('name'), 'pk')[:fetch_limit]
                products = products.order_by(Lower('name'), 'pk')[:fetch_limit]

        catalog = []

        for service in services:
            item = {
                'id': service.id,
                'type': 'service',
                'name': service.name,
                'category': service.category.slug if service.category else (service.category_name or 'Services'),
                'description': service.description,
                'price': float(service.price),
                'image_url': service.image_url or (request.build_absolute_uri(service.image.url) if service.image else None),
                'shop': ShopSerializer(service.shop, context={'request': request}).data if service.shop else None,
            }
            if popular:
                item['units_sold'] = service.units_sold
            catalog.append(item)

        for product in products:
            item = {
                'id': product.id,
                'type': 'product',
                'name': product.name,
                'category': product.category.slug if product.category else 'Products',
                'category_name': product.category.name if product.category else 'Products',
                'shop_category': product.shop.category.slug if product.shop and product.shop.category else None,
                'description': product.description,
                'price': float(product.price),
                'stock_quantity': product.stock_quantity,
                'image_url': product.image_url or (request.build_absolute_uri(product.image.url) if product.image else None),
                'shop': ShopSerializer(product.shop, context={'request': request}).data if product.shop else None,
            }
            if popular:
                item['units_sold'] = product.units_sold
            catalog.append(item)

        if popular:
            catalog.sort(key=lambda item: (-item['units_sold'], (item['name'] or '').lower(), item['type'], item['id']))
        else:
            catalog.sort(key=lambda item: ((item['name'] or '').lower(), item['type'], item['id']))
        if paginated:
            start = (page_number - 1) * page_size
            page_items = catalog[start:start + page_size]
            page = Paginator(range(total_count), page_size)
            try:
                paginator.page = page.page(page_number)
            except EmptyPage as exc:
                raise NotFound('Invalid page.') from exc
            paginator.request = request
            return paginator.get_paginated_response(page_items)
        return Response(catalog)

    @action(detail=False, methods=['get'], url_path='catalog/popularity')
    def catalog_popularity(self, request):
        since = timezone.now() - timedelta(days=90)
        paid_order_ids = Payment.objects.filter(
            status=Payment.STATUS_SUCCESS,
            order_id__isnull=False,
        ).values('order_id')
        eligible_orders = Q(order__created_at__gte=since) & ~Q(order__status='cancelled')

        product_sales = ProductOrderItem.objects.filter(
            eligible_orders,
            order_id__in=paid_order_ids,
            product__is_active=True,
            product__stock_quantity__gt=0,
            product__shop__status='active',
            product__shop__is_verified=True,
        ).annotate(
            category_slug=Coalesce('product__category__slug', Value('products'))
        ).values('category_slug').annotate(units_sold=Sum('quantity')).order_by()

        service_sales = OrderItem.objects.filter(
            eligible_orders,
            order_id__in=paid_order_ids,
            service__is_active=True,
            service__shop__status='active',
            service__shop__is_verified=True,
        ).annotate(
            category_slug=Coalesce(
                'service__category__slug',
                'service__category_name',
                Value('services'),
            )
        ).values('category_slug').annotate(units_sold=Sum('quantity')).order_by()

        categories = []
        for row in product_sales:
            categories.append({
                'type': 'product',
                'slug': slugify(row['category_slug']) or 'products',
                'units_sold': row['units_sold'],
            })
        for row in service_sales:
            categories.append({
                'type': 'service',
                'slug': slugify(row['category_slug']) or 'services',
                'units_sold': row['units_sold'],
            })
        return Response({'categories': categories})

    # public read access, authenticated required to create/update/delete
    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'catalog', 'catalog_popularity']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), permissions.IsAdminUser()]