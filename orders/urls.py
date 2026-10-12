# orders/urls.py

from django.urls import path
from .views import (
    AdminDashboardSummaryView,
    OrderListCreateView, 
    OrderUpdateView, 
    RiderOrderListView,
    RequestedOrdersListView,
    StaffCreateOrderView,
    OrderPaymentStatusView,
    OrderContributionStatusView,
    PublicOrderSummaryView,
    RequestDeliveryView,
    ApplyOfferView,
    OrderReviewView,
    RiderRatingSummaryView,
)

urlpatterns = [
    path('admin-summary/', AdminDashboardSummaryView.as_view(), name='admin-dashboard-summary'),
    path('', OrderListCreateView.as_view(), name='order-list'),
    path('update/', OrderUpdateView.as_view(), name='order-update'),
    path('rider/', RiderOrderListView.as_view(), name='rider-order-list'),
    path('rider/rating-summary/', RiderRatingSummaryView.as_view(), name='rider-rating-summary'),
    path('requested/', RequestedOrdersListView.as_view(), name='requested-orders-list'),
    path('create/', StaffCreateOrderView.as_view(), name='staff-create-order'),
    path('<str:code>/payment-status/', OrderPaymentStatusView.as_view(), name='order-payment-status'),
    path('<str:code>/contribution-status/', OrderContributionStatusView.as_view(), name='order-contribution-status'),
    path('<str:code>/public-summary/', PublicOrderSummaryView.as_view(), name='public-order-summary'),
    path('<str:code>/request-delivery/', RequestDeliveryView.as_view(), name='request-delivery'),
    path('<str:code>/apply-offer/', ApplyOfferView.as_view(), name='apply-offer'),
    path('<str:code>/review/', OrderReviewView.as_view(), name='order-review'),
]
