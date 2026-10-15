from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
	AdminMerchantDirectoryView,
	MerchantEarningsView,
	MerchantOrderFulfillmentView,
	MerchantOrdersView,
	MerchantProfileViewSet,
	ProductCategoryViewSet,
	ProductViewSet,
	ShopCategoryViewSet,
	ShopViewSet,
)


router = DefaultRouter()
router.register('merchants', MerchantProfileViewSet, basename='merchant')
router.register('categories', ShopCategoryViewSet, basename='shop-category')
router.register('product-categories', ProductCategoryViewSet, basename='product-category')
router.register('shops', ShopViewSet, basename='shop')
router.register('products', ProductViewSet, basename='product')

urlpatterns = [
	path('merchant/admin/', AdminMerchantDirectoryView.as_view(), name='admin-merchant-directory'),
	path('merchant/orders/', MerchantOrdersView.as_view(), name='merchant-orders'),
	path('merchant/orders/<int:order_id>/fulfillment/', MerchantOrderFulfillmentView.as_view(), name='merchant-order-fulfillment'),
	path('merchant/earnings/', MerchantEarningsView.as_view(), name='merchant-earnings'),
	path('', include(router.urls)),
]