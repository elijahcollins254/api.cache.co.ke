# financing/views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from django.utils import timezone
from django.db.models import Q, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import TruncMonth
from datetime import timedelta
from payments.models import Payment
from orders.models import Order
from .models import (
    LoanApplication,
    LoanCollateral,
    LoanGuarantor,
    LoanRepayment,
    Investment,
    ShareInvestment,
    SHARE_OFFERING_TARGET_RAISE,
    SHARE_OFFERING_TARGET_PERCENTAGE,
)
from .serializers import (
    LoanApplicationDetailSerializer,
    LoanApplicationListSerializer,
    CreateLoanApplicationSerializer,
    LoanCollateralSerializer,
    LoanGuarantorSerializer,
    LoanRepaymentSerializer,
    InvestmentListSerializer,
    InvestmentDetailSerializer,
    CreateInvestmentSerializer,
    ShareInvestmentSerializer,
    CreateShareInvestmentSerializer,
)


class LoanApplicationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing loan applications.
    
    - Users can view their own applications
    - Users can create new loan applications
    - Admins can review and approve/reject applications
    """
    queryset = LoanApplication.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user__username', 'user__email', 'id']
    ordering_fields = ['created_at', 'loan_amount', 'status']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter based on user role"""
        user = self.request.user
        
        # Admins see all applications
        if user.is_staff or user.is_superuser:
            return LoanApplication.objects.all()
        
        # Regular users see only their own
        return LoanApplication.objects.filter(user=user)
    
    def get_serializer_class(self):
        """Use different serializers based on action"""
        if self.action == 'create':
            return CreateLoanApplicationSerializer
        elif self.action == 'pending_review':
            return LoanApplicationListSerializer
        # Return detailed serializer for list and retrieve to get all nested data
        return LoanApplicationDetailSerializer
    
    def create(self, request, *args, **kwargs):
        """Create a new loan application"""
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        loan_app = serializer.save()
        
        # Return detailed serializer
        return Response(
            LoanApplicationDetailSerializer(loan_app).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def pending_review(self, request):
        """Get all loan applications pending review"""
        pending_apps = self.get_queryset().filter(status='pending')
        serializer = self.get_serializer(pending_apps, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def approve(self, request, pk=None):
        """Approve a loan application"""
        loan_app = self.get_object()
        
        if loan_app.status != 'pending':
            return Response(
                {'error': f'Cannot approve application with status {loan_app.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        loan_app.status = 'approved'
        loan_app.approved_at = timezone.now()
        loan_app.reviewed_at = timezone.now()
        loan_app.reviewed_by = request.user
        loan_app.reviewer_notes = request.data.get('notes', '')
        loan_app.save()
        
        return Response(
            LoanApplicationDetailSerializer(loan_app).data,
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def reject(self, request, pk=None):
        """Reject a loan application"""
        loan_app = self.get_object()
        
        if loan_app.status != 'pending':
            return Response(
                {'error': f'Cannot reject application with status {loan_app.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        loan_app.status = 'rejected'
        loan_app.reviewed_at = timezone.now()
        loan_app.reviewed_by = request.user
        loan_app.reviewer_notes = request.data.get('notes', '')
        loan_app.save()
        
        return Response(
            LoanApplicationDetailSerializer(loan_app).data,
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def fund(self, request, pk=None):
        """Mark a loan application as funded"""
        loan_app = self.get_object()
        
        if loan_app.status != 'approved':
            return Response(
                {'error': 'Only approved applications can be funded'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        loan_app.status = 'active'
        loan_app.funded_at = timezone.now()
        loan_app.due_date = timezone.now() + timezone.timedelta(days=loan_app.duration_days)
        loan_app.save()
        
        return Response(
            LoanApplicationDetailSerializer(loan_app).data,
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def repay(self, request, pk=None):
        """Record a repayment for a loan"""
        loan_app = self.get_object()
        
        # Check if user owns this loan
        if loan_app.user != request.user and not request.user.is_staff:
            return Response(
                {'error': 'You do not have permission to repay this loan'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        if loan_app.status not in ['active', 'approved']:
            return Response(
                {'error': f'Cannot repay loan with status {loan_app.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        amount = request.data.get('amount')
        payment_method = request.data.get('payment_method', 'mpesa')
        
        if not amount:
            return Response(
                {'error': 'amount is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            return Response(
                {'error': 'amount must be a valid number'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create repayment record
        repayment = LoanRepayment.objects.create(
            loan_application=loan_app,
            amount=amount,
            payment_method=payment_method,
            status='completed'  # In production, integrate with payment gateway
        )
        
        # Update loan
        loan_app.amount_repaid += amount
        
        # Check if fully repaid
        if loan_app.amount_repaid >= loan_app.total_repayment:
            loan_app.status = 'repaid'
        
        loan_app.save()
        
        return Response(
            LoanRepaymentSerializer(repayment).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated])
    def summary(self, request, pk=None):
        """Get a summary of loan application with key metrics"""
        loan_app = self.get_object()
        
        # Check permissions
        if loan_app.user != request.user and not request.user.is_staff:
            return Response(
                {'error': 'You do not have permission to view this loan'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        remaining = max(0, (loan_app.total_repayment or 0) - loan_app.amount_repaid)
        
        return Response({
            'id': str(loan_app.id),
            'status': loan_app.status,
            'loan_amount': str(loan_app.loan_amount),
            'total_repayment': str(loan_app.total_repayment or 0),
            'amount_repaid': str(loan_app.amount_repaid),
            'remaining_balance': str(remaining),
            'due_date': loan_app.due_date,
            'days_remaining': (loan_app.due_date - timezone.now()).days if loan_app.due_date else None,
        })


# Standalone function-based view for creating loan requests
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from orders.models import Order
from decimal import Decimal


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_loan_request(request):
    """
    Create a new loan request from the frontend.
    
    Accepts:
    - loan_type: 'order_collateral' or 'collateral_only'
    - loan_amount: decimal
    - duration_days: integer
    - purpose: string
    - order_id: integer (for order_collateral)
    - collateral: dict with type and items
    - guarantors: list of dicts with name, phone_number, email, relationship
    """
    try:
        data = request.data
        user = request.user
        
        # Validate required fields
        loan_type = data.get('loan_type')
        loan_amount = data.get('loan_amount')
        duration_days = data.get('duration_days')
        purpose = data.get('purpose')
        collateral_data = data.get('collateral', {})
        guarantors_data = data.get('guarantors', [])
        
        # Validation
        if not loan_type or loan_type not in ['order_collateral', 'collateral_only']:
            return Response(
                {'error': 'loan_type must be "order_collateral" or "collateral_only"'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not loan_amount:
            return Response(
                {'error': 'loan_amount is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not duration_days:
            return Response(
                {'error': 'duration_days is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not purpose:
            return Response(
                {'error': 'purpose is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not guarantors_data or len(guarantors_data) == 0:
            return Response(
                {'error': 'At least one guarantor is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Handle order collateral
        order = None
        order_value = None
        
        if loan_type == 'order_collateral':
            order_id = data.get('order_id')
            if not order_id:
                return Response(
                    {'error': 'order_id is required for order_collateral loans'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                order = Order.objects.get(id=order_id)
                order_value = Decimal(str(order.actual_price or order.price or 0))
            except Order.DoesNotExist:
                return Response(
                    {'error': f'Order with id {order_id} not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Create loan application
        loan_app = LoanApplication.objects.create(
            user=user,
            loan_type=loan_type,
            loan_amount=Decimal(str(loan_amount)),
            duration_days=int(duration_days),
            purpose=purpose,
            order=order,
            order_value=order_value,
            status='pending'
        )
        
        # Calculate interest
        loan_app.calculate_total_interest()
        loan_app.save()
        
        # Add collateral items for collateral_only type
        if loan_type == 'collateral_only':
            collateral_items = collateral_data.get('items', [])
            for item in collateral_items:
                LoanCollateral.objects.create(
                    loan_application=loan_app,
                    collateral_type=item.get('type', 'other'),
                    description=item.get('description', ''),
                    estimated_value=Decimal(str(item.get('estimated_value', 0)))
                )
        
        # Add guarantors
        for guarantor in guarantors_data:
            LoanGuarantor.objects.create(
                loan_application=loan_app,
                name=guarantor.get('name', ''),
                phone_number=guarantor.get('phone_number', ''),
                email=guarantor.get('email', ''),
                relationship=guarantor.get('relationship', 'friend')
            )
        
        # Return created loan application
        serializer = LoanApplicationDetailSerializer(loan_app)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


class InvestmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing investments.
    
    - Users can view their own investments
    - Users can create new investments
    - Admins can view all investments
    """
    queryset = Investment.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user__username', 'user__email', 'id', 'plan_type']
    ordering_fields = ['created_at', 'amount', 'status', 'maturity_date']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter based on user role"""
        user = self.request.user
        
        # Admins see all investments
        if user.is_staff or user.is_superuser:
            return Investment.objects.all()
        
        # Regular users see only their own
        return Investment.objects.filter(user=user)
    
    def get_serializer_class(self):
        """Use different serializers based on action"""
        if self.action == 'create':
            return CreateInvestmentSerializer
        elif self.action == 'list':
            return InvestmentListSerializer
        return InvestmentDetailSerializer
    
    def create(self, request, *args, **kwargs):
        """Create a new investment"""
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        investment = serializer.save()
        
        # Return detailed serializer
        return Response(
            InvestmentDetailSerializer(investment).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=False, methods=['get'])
    def my_investments(self, request):
        """Get all investments for the current user"""
        investments = self.get_queryset()
        serializer = self.get_serializer(investments, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get investment summary for the current user"""
        user = request.user
        investments = Investment.objects.filter(user=user)
        
        total_invested = sum(int(inv.amount) for inv in investments)
        active_investments = investments.filter(status='active').count()
        total_expected_returns = sum(float(inv.expected_annual_return) for inv in investments)
        total_received_returns = sum(float(inv.total_received_returns) for inv in investments)
        
        return Response({
            'total_invested': total_invested,
            'active_investments': active_investments,
            'total_investments': investments.count(),
            'total_expected_returns': round(total_expected_returns, 2),
            'total_received_returns': round(total_received_returns, 2),
            'pending_investments': investments.filter(status='pending').count(),
            'completed_investments': investments.filter(status='completed').count(),
        })
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def activate(self, request, pk=None):
        """Activate a pending investment"""
        investment = self.get_object()
        
        if investment.status != 'pending':
            return Response(
                {'error': f'Cannot activate investment with status {investment.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        investment.status = 'active'
        investment.payment_confirmed_at = timezone.now()
        investment.save()
        
        return Response(
            InvestmentDetailSerializer(investment).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def confirm_payment(self, request, pk=None):
        """Confirm payment for an investment"""
        investment = self.get_object()
        
        # Check if user owns this investment
        if investment.user != request.user and not request.user.is_staff:
            return Response(
                {'error': 'You do not have permission to update this investment'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        if investment.status != 'pending':
            return Response(
                {'error': f'Cannot confirm payment for investment with status {investment.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        investment.status = 'active'
        investment.payment_confirmed_at = timezone.now()
        investment.payment_method = request.data.get('payment_method', 'mpesa')
        investment.transaction_id = request.data.get('transaction_id', '')
        investment.save()
        
        return Response(
            InvestmentDetailSerializer(investment).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def complete(self, request, pk=None):
        """Complete an investment at maturity"""
        investment = self.get_object()

        if investment.status != 'active':
            return Response(
                {'error': f'Cannot complete investment with status {investment.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        investment.status = 'completed'
        investment.total_received_returns = investment.expected_annual_return
        investment.save()

        return Response(
            InvestmentDetailSerializer(investment).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def cancel(self, request, pk=None):
        """Cancel an investment"""
        investment = self.get_object()

        if investment.status in ['completed', 'cancelled']:
            return Response(
                {'error': f'Cannot cancel investment with status {investment.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        investment.status = 'cancelled'
        investment.save()

        return Response(
            InvestmentDetailSerializer(investment).data,
            status=status.HTTP_200_OK
        )


class ShareInvestmentViewSet(viewsets.GenericViewSet):
    queryset = ShareInvestment.objects.all()
    serializer_class = ShareInvestmentSerializer

    def get_permissions(self):
        if self.action in ('create', 'overview'):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if self.request.user.is_authenticated:
            return ShareInvestment.objects.filter(user=self.request.user)
        return ShareInvestment.objects.none()

    def create(self, request, *args, **kwargs):
        serializer = CreateShareInvestmentSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        investment = serializer.save()
        return Response(
            ShareInvestmentSerializer(investment).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['get'])
    def mine(self, request):
        investments = self.get_queryset()
        return Response(ShareInvestmentSerializer(investments, many=True).data)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def overview(self, request):
        now = timezone.now()
        six_months_ago = now - timedelta(days=183)

        paid_investments = ShareInvestment.objects.filter(status='success')
        total_raised = paid_investments.aggregate(total=Sum('amount'))['total'] or 0
        investor_count = paid_investments.values('phone_number').distinct().count()
        monthly_raise = (
            paid_investments.filter(completed_at__gte=six_months_ago)
            .annotate(month=TruncMonth('completed_at'))
            .values('month')
            .annotate(total=Sum('amount'))
            .order_by('month')
        )
        raise_by_month = {
            row['month'].strftime('%Y-%m'): float(row['total'])
            for row in monthly_raise
            if row['month']
        }

        orders = Order.objects.exclude(status='cancelled')
        paid_order_payments = Payment.objects.filter(
            status=Payment.STATUS_SUCCESS,
            provider='mpesa',
        ).annotate(
            order_reference=KeyTextTransform('order_reference', 'raw_payload'),
        ).filter(
            Q(order_id__in=orders.values('id'))
            | Q(order_reference__in=orders.values('code'))
        )
        gross_revenue = paid_order_payments.aggregate(total=Sum('amount'))['total'] or 0
        paid_order_count = orders.filter(
            Q(id__in=paid_order_payments.values('order_id'))
            | Q(code__in=paid_order_payments.values('order_reference'))
        ).distinct().count()
        delivered_orders = orders.filter(status='delivered').count()
        order_count = orders.count()
        monthly_sales = (
            paid_order_payments.filter(completed_at__gte=six_months_ago)
            .annotate(month=TruncMonth('completed_at'))
            .values('month')
            .annotate(total=Sum('amount'))
            .order_by('month')
        )
        sales_by_month = {
            row['month'].strftime('%Y-%m'): float(row['total'])
            for row in monthly_sales
            if row['month']
        }

        months = []
        month_cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for offset in range(5, -1, -1):
            month_number = month_cursor.month - offset
            year = month_cursor.year + (month_number - 1) // 12
            month = (month_number - 1) % 12 + 1
            month_key = f'{year:04d}-{month:02d}'
            month_date = month_cursor.replace(year=year, month=month)
            months.append({
                'month': month_key,
                'label': month_date.strftime('%b %Y'),
                'raised': raise_by_month.get(month_key, 0),
                'sales': sales_by_month.get(month_key, 0),
            })

        return Response({
            'fundraising': {
                'total_raised': float(total_raised),
                'investor_count': investor_count,
                'shares_issued_percentage': float(
                    total_raised * SHARE_OFFERING_TARGET_PERCENTAGE / SHARE_OFFERING_TARGET_RAISE
                ),
                'target_raise': float(SHARE_OFFERING_TARGET_RAISE),
                'target_shares_percentage': float(SHARE_OFFERING_TARGET_PERCENTAGE),
                'monthly_raise': months,
            },
            'business': {
                'gross_revenue': float(gross_revenue),
                'order_count': order_count,
                'delivered_order_count': delivered_orders,
                'active_order_count': max(0, order_count - delivered_orders),
                'average_paid_order_value': (
                    float(gross_revenue) / paid_order_count if paid_order_count else 0
                ),
                'monthly_sales': months,
            },
        })
