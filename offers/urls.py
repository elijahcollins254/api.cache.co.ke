from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import OfferViewSet, UserOfferViewSet, OfferSubscriptionViewSet, OfferPushDeviceView

router = DefaultRouter(trailing_slash=False)
router.register(r'user-offers', UserOfferViewSet, basename='user-offers')
router.register(r'subscriptions', OfferSubscriptionViewSet, basename='subscription')
router.register(r'', OfferViewSet, basename='offer')

urlpatterns = [
    path('push-devices/', OfferPushDeviceView.as_view(), name='offer-push-devices'),
    path('', include(router.urls)),
]