# financing/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import LoanApplicationViewSet, InvestmentViewSet, ShareInvestmentViewSet, create_loan_request

router = DefaultRouter()
router.register(r'loans', LoanApplicationViewSet, basename='loan-application')
router.register(r'investments', InvestmentViewSet, basename='investment')
router.register(r'share-investments', ShareInvestmentViewSet, basename='share-investment')

urlpatterns = [
    path('request/', create_loan_request, name='create-loan-request'),
    path('', include(router.urls)),
]
