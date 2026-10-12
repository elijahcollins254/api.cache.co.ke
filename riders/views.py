# riders/views.py
from decimal import Decimal, InvalidOperation
import hmac
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import RiderLocation, RiderProfile
from .serializers import RiderLocationSerializer, RiderProfileSerializer
from .models import RiderLedgerEntry, RiderWallet, WithdrawalRequest
from .payouts import (
    b2c_configuration_error,
    fail_withdrawal_and_refund,
    initiate_b2c,
    settle_b2c_callback,
)

logger = logging.getLogger(__name__)


class RiderLocationViewSet(viewsets.ModelViewSet):
    """Private viewset: riders push GPS updates. Auth required for create/update."""
    queryset = RiderLocation.objects.all().order_by('-id')
    serializer_class = RiderLocationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Riders see their own locations; staff can see all
        if self.request.user.is_staff:
            return super().get_queryset()
        return self.queryset.filter(rider=self.request.user)

    def perform_create(self, serializer):
        serializer.save(rider=self.request.user)


class PublicRiderLocationsView(APIView):
    """
    Public endpoint: GET /riders/ -> returns an array of latest RiderLocation entries,
    one per rider, newest first. No authentication required.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        qs = RiderLocation.objects.all().order_by("-recorded_at", "-created_at")
        latest_by_rider = {}
        for loc in qs:
            rider_id = getattr(loc, "rider_id", None)
            rider_key = f"anon-{loc.id}" if rider_id is None else str(rider_id)
            if rider_key not in latest_by_rider:
                latest_by_rider[rider_key] = loc

        latest_list = list(latest_by_rider.values())
        serializer = RiderLocationSerializer(latest_list, many=True, context={"request": request})
        return Response(serializer.data)


class RiderProfileViewSet(viewsets.ModelViewSet):
    """
    Read (public) access to rider profiles. Creation/updates/deletes restricted to admin.
    Routes:
      - GET /riders/profiles/         -> list
      - GET /riders/profiles/<pk>/    -> retrieve
      - POST/PUT/PATCH/DELETE         -> admin only
    """
    queryset = RiderProfile.objects.select_related("user", "user__service_location").all().order_by("-created_at")
    serializer_class = RiderProfileSerializer

    def get_permissions(self):
        # Public read access, admin required for write
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]


class RiderEarningsView(APIView):
    """GET the authenticated rider's balance/history; POST an M-Pesa withdrawal."""
    permission_classes = [permissions.IsAuthenticated]

    def _require_rider(self, request):
        if getattr(request.user, 'role', None) != 'rider':
            return Response({'detail': 'Rider access is required.'}, status=status.HTTP_403_FORBIDDEN)
        return None

    def get(self, request):
        denied = self._require_rider(request)
        if denied:
            return denied
        wallet, _ = RiderWallet.objects.get_or_create(rider=request.user)
        entries = RiderLedgerEntry.objects.filter(rider=request.user).select_related('order', 'withdrawal')[:100]
        withdrawals = WithdrawalRequest.objects.filter(rider=request.user)[:30]
        return Response({
            'balance': str(wallet.balance),
            'phone_number': request.user.phone or '',
            'earning_per_delivery': str(getattr(settings, 'RIDER_DELIVERY_EARNING_AMOUNT', '50.00')),
            'minimum_withdrawal': str(getattr(settings, 'RIDER_MIN_WITHDRAWAL_AMOUNT', '50.00')),
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
        denied = self._require_rider(request)
        if denied:
            return denied
        config_error = b2c_configuration_error()
        if config_error:
            return Response({'detail': config_error}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            amount = Decimal(str(request.data.get('amount', '')).strip())
        except (InvalidOperation, ValueError):
            return Response({'amount': 'Enter a valid amount.'}, status=status.HTTP_400_BAD_REQUEST)
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            return Response({'amount': 'Enter a positive whole KES amount.'}, status=status.HTTP_400_BAD_REQUEST)
        minimum = Decimal(str(getattr(settings, 'RIDER_MIN_WITHDRAWAL_AMOUNT', '50.00')))
        if amount < minimum:
            return Response({'amount': f'Minimum withdrawal is KES {minimum}.'}, status=status.HTTP_400_BAD_REQUEST)

        raw_phone = str(request.data.get('phone_number') or request.user.phone or '').strip()
        from users.models import format_phone_number
        phone = format_phone_number(raw_phone)
        if not phone or not (phone.startswith('+2547') or phone.startswith('+2541')) or len(phone) != 13:
            return Response({'phone_number': 'Enter a valid Kenyan M-Pesa number.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            wallet, _ = RiderWallet.objects.get_or_create(rider=request.user)
            wallet = RiderWallet.objects.select_for_update().get(pk=wallet.pk)
            if wallet.balance < amount:
                return Response({'amount': 'Insufficient available earnings.'}, status=status.HTTP_400_BAD_REQUEST)
            withdrawal = WithdrawalRequest.objects.create(
                rider=request.user,
                amount=amount,
                phone_number=phone,
                status=WithdrawalRequest.STATUS_PENDING,
            )
            RiderLedgerEntry.objects.create(
                rider=request.user,
                withdrawal=withdrawal,
                entry_type=RiderLedgerEntry.TYPE_WITHDRAWAL,
                amount=amount,
                description=f'M-Pesa payout #{withdrawal.pk}',
            )
            RiderWallet.objects.filter(pk=wallet.pk).update(balance=F('balance') - amount, updated_at=timezone.now())

        try:
            result = initiate_b2c(withdrawal)
        except Exception as exc:
            logger.exception('Could not submit rider withdrawal %s to Daraja', withdrawal.pk)
            withdrawal = fail_withdrawal_and_refund(withdrawal, str(exc))
            return Response({
                'detail': 'M-Pesa payout could not be started. The amount has been returned to your balance.',
                'withdrawal_id': withdrawal.pk,
                'status': withdrawal.status,
            }, status=status.HTTP_502_BAD_GATEWAY)

        withdrawal.status = WithdrawalRequest.STATUS_PROCESSING
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


class RiderB2CCallbackView(APIView):
    """Daraja callbacks are token-protected and correlated by conversation ID."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, callback_type):
        expected_token = str(getattr(settings, 'MPESA_B2C_CALLBACK_TOKEN', ''))
        supplied_token = str(request.query_params.get('token', ''))
        if not expected_token or not hmac.compare_digest(expected_token, supplied_token):
            return Response({'detail': 'Invalid callback token.'}, status=status.HTTP_403_FORBIDDEN)
        if callback_type not in ('result', 'timeout'):
            return Response({'detail': 'Unknown callback type.'}, status=status.HTTP_404_NOT_FOUND)
        settle_b2c_callback(request.data, timed_out=callback_type == 'timeout')
        return Response({'ResultCode': 0, 'ResultDesc': 'Accepted'})
