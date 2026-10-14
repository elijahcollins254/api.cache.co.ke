from django.utils.text import slugify
from django.db.models import Count, Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MerchantProfile, Product, ProductCategory, Shop, ShopCategory
from .serializers import (
    MerchantProfileSerializer,
    ProductCategorySerializer,
    ProductSerializer,
    ShopCategorySerializer,
    ShopSerializer,
)


class MerchantProfileViewSet(viewsets.ModelViewSet):
    queryset = MerchantProfile.objects.all().select_related('user')
    serializer_class = MerchantProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['get'])
    def me(self, request):
        merchant, _ = MerchantProfile.objects.get_or_create(
            user=request.user,
            defaults={
                'merchant_username': request.user.username.lower(),
                'business_name': request.user.get_full_name() or request.user.username,
                'phone': request.user.phone or '',
            }
        )
        serializer = self.get_serializer(merchant)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def register(self, request):
        merchant_username = (request.data.get('merchant_username') or '').strip()
        business_name = (request.data.get('business_name') or '').strip()
        phone = (request.data.get('phone') or '').strip()

        if not merchant_username or not business_name or not phone:
            return Response(
                {'detail': 'merchant_username, business_name and phone are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if MerchantProfile.objects.filter(merchant_username__iexact=merchant_username).exists():
            return Response(
                {'detail': 'This merchant username is already taken.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        merchant = MerchantProfile.objects.create(
            user=user,
            merchant_username=merchant_username,
            business_name=business_name,
            phone=phone,
            status='pending',
        )
        serializer = self.get_serializer(merchant)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ShopCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ShopCategory.objects.filter(is_active=True).order_by('name')
    serializer_class = ShopCategorySerializer
    permission_classes = [permissions.AllowAny]


class ProductCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProductCategory.objects.filter(is_active=True).order_by('name')
    serializer_class = ProductCategorySerializer
    permission_classes = [permissions.AllowAny]


class ShopViewSet(viewsets.ModelViewSet):
    queryset = Shop.objects.all().select_related('owner', 'owner__user', 'category').annotate(
        catalog_count=(
            Count('products', filter=Q(products__is_active=True), distinct=True)
            + Count('services', filter=Q(services__is_active=True), distinct=True)
        )
    )
    serializer_class = ShopSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_authenticated:
            return qs
        return qs.filter(status='active', is_verified=True)

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'products']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

        merchant = MerchantProfile.objects.filter(user=request.user).first()
        if merchant is None:
            return Response({'detail': 'You must register as a merchant before creating a shop.'}, status=status.HTTP_400_BAD_REQUEST)

        payload = dict(request.data)
        payload['owner'] = merchant.id
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        shop = serializer.save(owner=merchant)
        return Response(ShopSerializer(shop).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def products(self, request, slug=None):
        shop = self.get_object()
        products = Product.objects.filter(shop=shop, is_active=True)
        serializer = ProductSerializer(products, many=True, context={'request': request})
        return Response(serializer.data)


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.filter(is_active=True).select_related(
        'category', 'shop', 'shop__category', 'shop__owner', 'shop__owner__user'
    )
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()
        shop_slug = self.request.query_params.get('shop')
        if shop_slug:
            qs = qs.filter(shop__slug=shop_slug)
        if self.action in ['list', 'retrieve']:
            qs = qs.filter(shop__is_verified=True, shop__status='active')
        if self.request.user.is_authenticated:
            merchant = MerchantProfile.objects.filter(user=self.request.user).first()
            if merchant:
                qs = qs | Product.objects.filter(shop__owner=merchant)
        return qs.distinct()

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

        merchant = MerchantProfile.objects.filter(user=request.user).first()
        if merchant is None:
            return Response({'detail': 'You must register as a merchant before creating products.'}, status=status.HTTP_400_BAD_REQUEST)

        shop_id = request.data.get('shop') or request.data.get('shop_id')
        if not shop_id:
            return Response({'detail': 'shop is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            shop = Shop.objects.get(id=shop_id, owner=merchant)
        except Shop.DoesNotExist:
            return Response({'detail': 'You can only create products for your own shop.'}, status=status.HTTP_400_BAD_REQUEST)

        payload = dict(request.data)
        payload['shop_id'] = shop.id
        if not payload.get('slug'):
            payload['slug'] = slugify(payload.get('name', ''))
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        product = serializer.save(shop=shop)
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        merchant = MerchantProfile.objects.filter(user=self.request.user).first()
        if not merchant or serializer.instance.shop.owner_id != merchant.id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('You can only update products in your own shop.')
        serializer.save()

    def perform_destroy(self, instance):
        merchant = MerchantProfile.objects.filter(user=self.request.user).first()
        if not merchant or instance.shop.owner_id != merchant.id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('You can only delete products in your own shop.')
        instance.delete()
