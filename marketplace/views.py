from django.utils.text import slugify
from django.db import transaction
from django.db.models import Count, Q
from django.db.models import F
from django.utils import timezone
from django.conf import settings
from decimal import Decimal, InvalidOperation
import logging
import re
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    MerchantLedgerEntry,
    MerchantOrderFulfillment,
    MerchantProfile,
    MerchantWallet,
    MerchantWithdrawal,
    Product,
    ProductCategory,
    Shop,
    ShopCategory,
)
from .merchant_payouts import fail_withdrawal_and_refund
from riders.payouts import b2c_configuration_error, initiate_b2c
from .serializers import (
    MerchantProfileSerializer,
    ProductCategorySerializer,
    ProductSerializer,
    ShopCategorySerializer,
    ShopSerializer,
)

logger = logging.getLogger(__name__)


def _merchant_for_request(request):
    return MerchantProfile.objects.filter(user=request.user).first()


def _is_admin(request):
    return request.user.is_staff or request.user.is_superuser or getattr(request.user, 'role', None) == 'admin'


def _dashboard_merchant(request):
    if _is_admin(request):
        merchant_id = request.query_params.get('merchant_id')
        if not merchant_id:
            return None, Response({'detail': 'Select a merchant to view.'}, status=status.HTTP_400_BAD_REQUEST)
        merchant = MerchantProfile.objects.filter(pk=merchant_id).first()
        if merchant is None:
            return None, Response({'detail': 'Merchant profile not found.'}, status=status.HTTP_404_NOT_FOUND)
    else:
        merchant = _merchant_for_request(request)
        if merchant is None:
            return None, Response({'detail': 'Merchant profile not found.'}, status=status.HTTP_403_FORBIDDEN)
    return merchant, None


class AdminMerchantDirectoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not _is_admin(request):
            return Response({'detail': 'Administrator access required.'}, status=status.HTTP_403_FORBIDDEN)
        merchants = MerchantProfile.objects.order_by('business_name').values(
            'id', 'business_name', 'merchant_username', 'status'
        )
        return Response({'merchants': list(merchants)})


class MerchantOrdersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        merchant, denied = _dashboard_merchant(request)
        if denied:
            return denied

        from orders.models import Order, ProductOrderItem

        order_ids = ProductOrderItem.objects.filter(
            product__shop__owner=merchant,
        ).values_list('order_id', flat=True).distinct()
        orders = []
        for order in Order.objects.filter(
            id__in=order_ids,
        ).prefetch_related('product_order_items__product__shop').order_by('-created_at')[:100]:
            fulfillment = MerchantOrderFulfillment.objects.filter(merchant=merchant, order=order).first()
            items = [
                {
                    'name': line.product_name,
                    'quantity': line.quantity,
                    'unit_price': str(line.unit_price),
                    'total': str(line.unit_price * line.quantity),
                    'shop': line.product.shop.name,
                }
                for line in order.product_order_items.all()
                if line.product.shop.owner_id == merchant.id
            ]
            orders.append({
                'id': order.id,
                'code': order.code,
                'created_at': order.created_at,
                'status': order.status,
                'is_paid': order.is_paid(),
                'fulfillment_status': fulfillment.status if fulfillment else MerchantOrderFulfillment.STATUS_PENDING,
                'pickup_address': order.pickup_address,
                'items': items,
                'total': str(sum((Decimal(item['total']) for item in items), Decimal('0'))),
            })
        return Response({'merchant_id': merchant.id, 'merchant': merchant.business_name, 'orders': orders})


class MerchantOrderFulfillmentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, order_id):
        if _is_admin(request):
            return Response({'detail': 'Administrators can view merchant orders but cannot fulfill them.'}, status=status.HTTP_403_FORBIDDEN)
        merchant = _merchant_for_request(request)
        if merchant is None:
            return Response({'detail': 'Merchant profile not found.'}, status=status.HTTP_403_FORBIDDEN)
        from orders.models import Order

        order = Order.objects.filter(
            pk=order_id,
            product_order_items__product__shop__owner=merchant,
        ).distinct().first()
        if order is None:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not order.is_paid():
            return Response({'detail': 'This order must be paid before fulfillment.'}, status=status.HTTP_400_BAD_REQUEST)
        if order.status.lower() in ('cancelled', 'delivered'):
            return Response({'detail': 'This order can no longer be prepared.'}, status=status.HTTP_400_BAD_REQUEST)

        fulfillment, _ = MerchantOrderFulfillment.objects.get_or_create(merchant=merchant, order=order)
        action = request.data.get('action')
        transitions = {
            MerchantOrderFulfillment.STATUS_PENDING: ('accept', MerchantOrderFulfillment.STATUS_ACCEPTED),
            MerchantOrderFulfillment.STATUS_ACCEPTED: ('prepare', MerchantOrderFulfillment.STATUS_PREPARED),
        }
        expected = transitions.get(fulfillment.status)
        if not expected or action != expected[0]:
            return Response({'detail': 'Accept the order first, then mark it prepared.'}, status=status.HTTP_400_BAD_REQUEST)
        fulfillment.status = expected[1]
        fulfillment.save(update_fields=['status', 'updated_at'])
        return Response({'fulfillment_status': fulfillment.status})


class MerchantEarningsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _merchant(self, request):
        return _dashboard_merchant(request)

    def get(self, request):
        merchant, denied = self._merchant(request)
        if denied:
            return denied
        wallet = MerchantWallet.objects.filter(merchant=merchant).first()
        entries = MerchantLedgerEntry.objects.filter(merchant=merchant).select_related('order', 'withdrawal')[:100]
        withdrawals = MerchantWithdrawal.objects.filter(merchant=merchant)[:30]
        return Response({
            'balance': str(wallet.balance if wallet else 0),
            'phone_number': merchant.phone or (request.user.phone if merchant.user_id == request.user.id else ''),
            'minimum_withdrawal': str(getattr(settings, 'MERCHANT_MIN_WITHDRAWAL_AMOUNT', '50.00')),
            'entries': [{
                'id': entry.id,
                'type': entry.entry_type,
                'amount': str(entry.amount),
                'description': entry.description,
                'order_code': entry.order.code if entry.order_id else None,
                'withdrawal_id': entry.withdrawal_id,
                'created_at': entry.created_at,
            } for entry in entries],
            'withdrawals': [{
                'id': item.id,
                'amount': str(item.amount),
                'phone_number': item.phone_number,
                'status': item.status,
                'transaction_reference': item.transaction_reference,
                'failure_reason': item.failure_reason,
                'created_at': item.created_at,
            } for item in withdrawals],
        })

    def post(self, request):
        if _is_admin(request):
            return Response({'detail': 'Administrators can view merchant earnings but cannot request withdrawals.'}, status=status.HTTP_403_FORBIDDEN)
        merchant, denied = self._merchant(request)
        if denied:
            return denied
        config_error = b2c_configuration_error()
        if config_error:
            return Response({'detail': 'M-Pesa payouts are not configured yet.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            amount = Decimal(str(request.data.get('amount', '')).strip())
        except (InvalidOperation, ValueError):
            return Response({'amount': 'Enter a valid amount.'}, status=status.HTTP_400_BAD_REQUEST)
        minimum = Decimal(str(getattr(settings, 'MERCHANT_MIN_WITHDRAWAL_AMOUNT', '50.00')))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value() or amount < minimum:
            return Response({'amount': f'Enter a whole KES amount of at least {minimum}.'}, status=status.HTTP_400_BAD_REQUEST)
        from users.models import format_phone_number
        phone = format_phone_number(str(request.data.get('phone_number') or merchant.phone or '').strip())
        if not phone or not re.fullmatch(r'\+254(?:7|1)\d{8}', phone):
            return Response({'phone_number': 'Enter a valid Kenyan M-Pesa number.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            wallet, _ = MerchantWallet.objects.get_or_create(merchant=merchant)
            wallet = MerchantWallet.objects.select_for_update().get(pk=wallet.pk)
            if wallet.balance < amount:
                return Response({'amount': 'Insufficient available earnings.'}, status=status.HTTP_400_BAD_REQUEST)
            withdrawal = MerchantWithdrawal.objects.create(
                merchant=merchant,
                amount=amount,
                phone_number=phone,
                status=MerchantWithdrawal.STATUS_PENDING,
            )
            MerchantLedgerEntry.objects.create(
                merchant=merchant,
                withdrawal=withdrawal,
                entry_type=MerchantLedgerEntry.TYPE_WITHDRAWAL,
                amount=amount,
                description=f'M-Pesa payout #{withdrawal.pk}',
            )
            MerchantWallet.objects.filter(pk=wallet.pk).update(
                balance=F('balance') - amount,
                updated_at=timezone.now(),
            )
        try:
            result = initiate_b2c(withdrawal, recipient_type='Merchant')
        except Exception as exc:
            logger.exception('Could not submit merchant withdrawal %s to Daraja', withdrawal.pk)
            withdrawal = fail_withdrawal_and_refund(withdrawal, str(exc))
            return Response({
                'detail': 'M-Pesa payout could not be started. The amount has been returned to your balance.',
                'withdrawal_id': withdrawal.pk,
                'status': withdrawal.status,
            }, status=status.HTTP_502_BAD_GATEWAY)
        withdrawal.status = MerchantWithdrawal.STATUS_PROCESSING
        withdrawal.conversation_id = str(result.get('ConversationID') or '')
        withdrawal.originator_conversation_id = str(result.get('OriginatorConversationID') or '')
        withdrawal.save(update_fields=['status', 'conversation_id', 'originator_conversation_id', 'updated_at'])
        return Response({
            'id': withdrawal.pk,
            'amount': str(withdrawal.amount),
            'phone_number': withdrawal.phone_number,
            'status': withdrawal.status,
            'message': 'Payout sent to M-Pesa. Its status will update when Safaricom confirms it.',
        }, status=status.HTTP_201_CREATED)


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
