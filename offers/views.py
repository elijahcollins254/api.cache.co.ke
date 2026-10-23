from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils import timezone
from django.db import models
from django.http import JsonResponse
from .models import Offer, UserOffer, OfferNotificationSubscription, OfferPushDevice
from .serializers import OfferSerializer, UserOfferSerializer, OfferNotificationSubscriptionSerializer
from .services import send_offer_claim_confirmation

class OfferViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Offer.objects.filter(is_active=True)
    serializer_class = OfferSerializer
    permission_classes = [AllowAny]

    def get_permissions(self):
        """
        Allow anyone to list/retrieve offers, but require authentication for claiming
        """
        if self.action == 'claim':
            return [IsAuthenticated()]
        return [AllowAny()]

    def get_queryset(self):
        now = timezone.now()
        return Offer.objects.filter(
            is_active=True,
            valid_from__lte=now,
        ).filter(
            models.Q(valid_until__isnull=True) | 
            models.Q(valid_until__gt=now)
        ).filter(
            models.Q(max_uses__isnull=True) |
            models.Q(current_uses__lt=models.F('max_uses'))
        )

    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        offer = self.get_object()
        user = request.user

        # Check if user already claimed this offer
        if UserOffer.objects.filter(user=user, offer=offer).exists():
            return Response(
                {'detail': 'You have already claimed this offer.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if offer is still available
        if offer.max_uses and offer.current_uses >= offer.max_uses:
            return Response(
                {'detail': 'This offer has reached its maximum usage limit.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create user offer and increment usage counter
        user_offer = UserOffer.objects.create(user=user, offer=offer)
        offer.current_uses += 1
        offer.save()

        sms_result = send_offer_claim_confirmation(user, offer)
        if sms_result.get('status') != 'success':
            return Response(
                {
                    **UserOfferSerializer(user_offer).data,
                    'sms_notification': 'Offer claimed, but the confirmation SMS could not be sent.',
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            UserOfferSerializer(user_offer).data,
            status=status.HTTP_201_CREATED
        )

class UserOfferViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = UserOfferSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return UserOffer.objects.filter(user=self.request.user)


class OfferSubscriptionViewSet(viewsets.ViewSet):
    """
    Endpoint to manage offer notification subscriptions.
    Users can subscribe/unsubscribe to receive SMS notifications about new offers.
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get', 'post'], permission_classes=[IsAuthenticated])
    def my_subscription(self, request):
        """
        GET: Get the current user's subscription status
        POST: Create or update the current user's subscription
        """
        if request.method == 'GET':
            subscription = OfferNotificationSubscription.objects.filter(user=request.user).first()
            if subscription:
                serializer = OfferNotificationSubscriptionSerializer(subscription)
                return Response(serializer.data)
            return Response({'is_subscribed': False})

        elif request.method == 'POST':
            if not request.user.phone:
                return Response(
                    {'detail': 'Add a phone number to your profile before subscribing to SMS notifications.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            subscription, created = OfferNotificationSubscription.objects.get_or_create(
                user=request.user,
                defaults={'phone_number': request.user.phone}
            )
            subscription.is_active = True
            subscription.phone_number = request.user.phone or subscription.phone_number
            subscription.save()

            serializer = OfferNotificationSubscriptionSerializer(subscription)
            return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated])
    def unsubscribe(self, request):
        """
        Unsubscribe from offer notifications
        """
        subscription = OfferNotificationSubscription.objects.filter(user=request.user).first()
        if subscription:
            subscription.is_active = False
            subscription.save()
            return Response({'detail': 'Unsubscribed from offer notifications'}, status=status.HTTP_200_OK)
        return Response({'detail': 'Not subscribed'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def subscribed_numbers(self, request):
        """
        Get all active subscribed phone numbers for SMS sending.
        This endpoint is used by external services to fetch the list of numbers to send SMS to.
        CORS enabled for cross-origin requests.
        """
        subscriptions = OfferNotificationSubscription.objects.filter(is_active=True).exclude(
            phone_number__isnull=True
        ).exclude(phone_number='')

        phone_numbers = [sub.phone_number for sub in subscriptions]

        response = Response({
            'count': len(phone_numbers),
            'phone_numbers': phone_numbers,
            'timestamp': timezone.now()
        })

        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type'

        return response


class OfferPushDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get('token')
        if (
            not isinstance(token, str)
            or not token.startswith(('ExponentPushToken[', 'ExpoPushToken['))
            or not token.endswith(']')
        ):
            return Response(
                {'detail': 'A valid Expo push token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        device, created = OfferPushDevice.objects.update_or_create(
            token=token,
            defaults={'user': request.user},
        )
        return Response(
            {'registered': True, 'id': device.pk},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        token = request.data.get('token')
        if not isinstance(token, str) or not token:
            return Response(
                {'detail': 'A push token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        OfferPushDevice.objects.filter(user=request.user, token=token).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)