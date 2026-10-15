# financing/serializers.py
from rest_framework import serializers
from decimal import Decimal
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
from orders.models import Order
import re


class LoanCollateralSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanCollateral
        fields = ['id', 'collateral_type', 'description', 'estimated_value', 'created_at']
        read_only_fields = ['id', 'created_at']


class LoanGuarantorSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanGuarantor
        fields = ['id', 'name', 'phone_number', 'email', 'relationship', 'created_at']
        read_only_fields = ['id', 'created_at']


class LoanRepaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanRepayment
        fields = ['id', 'amount', 'status', 'payment_method', 'transaction_id', 'created_at', 'completed_at']
        read_only_fields = ['id', 'created_at', 'completed_at']


class InvestmentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for investment list views"""
    user_username = serializers.CharField(source='user.username', read_only=True)
    user_name = serializers.SerializerMethodField()
    days_until_maturity = serializers.SerializerMethodField()
    progress_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = Investment
        fields = [
            'id', 'plan_type', 'amount', 'status', 'annual_return_percentage',
            'expected_annual_return', 'expected_monthly_return', 'total_received_returns',
            'investment_date', 'maturity_date', 'days_until_maturity', 'progress_percentage',
            'user_username', 'user_name', 'created_at'
        ]
        read_only_fields = [
            'id', 'expected_annual_return', 'expected_monthly_return', 'total_received_returns',
            'investment_date', 'created_at'
        ]
    
    def get_user_name(self, obj):
        """Get full name or username"""
        if obj.user.first_name and obj.user.last_name:
            return f"{obj.user.first_name} {obj.user.last_name}"
        return obj.user.username
    
    def get_days_until_maturity(self, obj):
        """Calculate days until maturity"""
        from django.utils import timezone
        if obj.maturity_date:
            delta = obj.maturity_date - timezone.now()
            return max(0, delta.days)
        return 0
    
    def get_progress_percentage(self, obj):
        """Calculate investment progress percentage"""
        from django.utils import timezone
        if obj.investment_date and obj.maturity_date:
            total_days = (obj.maturity_date - obj.investment_date).days
            elapsed_days = (timezone.now() - obj.investment_date).days
            if total_days > 0:
                return min(100, int((elapsed_days / total_days) * 100))
        return 0


class InvestmentDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for investment details"""
    user_username = serializers.CharField(source='user.username', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_phone = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    days_until_maturity = serializers.SerializerMethodField()
    progress_percentage = serializers.SerializerMethodField()
    total_value_at_maturity = serializers.SerializerMethodField()
    
    class Meta:
        model = Investment
        fields = [
            'id', 'plan_type', 'amount', 'status', 'annual_return_percentage',
            'expected_annual_return', 'expected_monthly_return', 'total_received_returns',
            'lockup_period_months', 'investment_date', 'maturity_date', 'payout_frequency',
            'next_payout_date', 'last_payout_date', 'days_until_maturity', 'progress_percentage',
            'total_value_at_maturity', 'payment_method', 'payment_confirmed_at',
            'user_username', 'user_email', 'user_phone', 'user_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'expected_annual_return', 'expected_monthly_return', 'total_received_returns',
            'investment_date', 'maturity_date', 'created_at', 'updated_at', 'payment_confirmed_at'
        ]
    
    def get_user_phone(self, obj):
        """Get user phone number with fallback"""
        return obj.user.phone if hasattr(obj.user, 'phone') and obj.user.phone else (
            obj.user.phone_number if hasattr(obj.user, 'phone_number') else None
        )
    
    def get_user_name(self, obj):
        """Get full name or username"""
        if obj.user.first_name and obj.user.last_name:
            return f"{obj.user.first_name} {obj.user.last_name}"
        return obj.user.username
    
    def get_days_until_maturity(self, obj):
        """Calculate days until maturity"""
        from django.utils import timezone
        if obj.maturity_date:
            delta = obj.maturity_date - timezone.now()
            return max(0, delta.days)
        return 0
    
    def get_progress_percentage(self, obj):
        """Calculate investment progress percentage"""
        from django.utils import timezone
        if obj.investment_date and obj.maturity_date:
            total_days = (obj.maturity_date - obj.investment_date).days
            elapsed_days = (timezone.now() - obj.investment_date).days
            if total_days > 0:
                return min(100, int((elapsed_days / total_days) * 100))
        return 0
    
    def get_total_value_at_maturity(self, obj):
        """Calculate total value at maturity (principal + returns)"""
        return float(obj.amount + obj.expected_annual_return)


class CreateInvestmentSerializer(serializers.Serializer):
    """Serializer for creating investments"""
    plan_type = serializers.ChoiceField(choices=['starter', 'professional', 'enterprise'])
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('1'))
    
    def validate_amount(self, value):
        """Validate minimum investment amounts"""
        plan_type = self.initial_data.get('plan_type')
        
        min_amounts = {
            'starter': 5000,
            'professional': 25000,
            'enterprise': 100000,
        }
        
        min_amount = min_amounts.get(plan_type, 0)
        if value < min_amount:
            raise serializers.ValidationError(f"Minimum investment for this plan is {min_amount}")
        
        return value
    
    def create(self, validated_data):
        user = self.context['request'].user
        plan_type = validated_data['plan_type']
        
        # Return percentages by plan
        return_percentages = {
            'starter': 15,
            'professional': 18,
            'enterprise': 22,
        }
        
        # Lockup periods by plan
        lockup_periods = {
            'starter': 12,
            'professional': 18,
            'enterprise': 24,
        }
        
        investment = Investment.objects.create(
            user=user,
            plan_type=plan_type,
            amount=validated_data['amount'],
            annual_return_percentage=return_percentages[plan_type],
            lockup_period_months=lockup_periods[plan_type],
            status='pending'
        )
        
        return investment


class ShareInvestmentSerializer(serializers.ModelSerializer):
    shares_percentage = serializers.SerializerMethodField()

    class Meta:
        model = ShareInvestment
        fields = ['id', 'amount', 'status', 'shares_percentage', 'created_at', 'completed_at']
        read_only_fields = fields

    def get_shares_percentage(self, obj):
        return float(obj.amount * SHARE_OFFERING_TARGET_PERCENTAGE / SHARE_OFFERING_TARGET_RAISE)


class CreateShareInvestmentSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    phone = serializers.CharField(max_length=32)

    def validate_phone(self, value):
        digits = re.sub(r'\D', '', value)
        if len(digits) == 10 and digits.startswith('0'):
            digits = f'254{digits[1:]}'
        elif len(digits) == 9:
            digits = f'254{digits}'

        if len(digits) != 12 or not digits.startswith('254') or digits[3] not in ('1', '7'):
            raise serializers.ValidationError('Enter a valid Kenyan mobile number.')

        return digits

    def create(self, validated_data):
        request = self.context['request']
        return ShareInvestment.objects.create(
            user=request.user if request.user.is_authenticated else None,
            amount=validated_data['amount'],
            phone_number=validated_data['phone'],
        )


class LoanApplicationDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for loan applications with nested relationships"""
    collateral_items = LoanCollateralSerializer(many=True, read_only=True)
    guarantors = LoanGuarantorSerializer(many=True, read_only=True)
    repayments = LoanRepaymentSerializer(many=True, read_only=True)
    order_code = serializers.CharField(source='order.code', read_only=True)
    user_username = serializers.CharField(source='user.username', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_phone = serializers.SerializerMethodField()
    user_id = serializers.CharField(source='user.id', read_only=True)
    user_name = serializers.SerializerMethodField()
    
    class Meta:
        model = LoanApplication
        fields = [
            'id', 'user', 'user_id', 'user_name', 'user_username', 'user_email', 'user_phone',
            'loan_type', 'loan_amount', 'duration_days',
            'purpose', 'status', 'daily_interest_rate', 'total_interest', 'total_repayment',
            'order', 'order_code', 'order_value', 'created_at', 'updated_at', 'reviewed_at',
            'approved_at', 'funded_at', 'due_date', 'reviewer_notes', 'amount_repaid',
            'collateral_items', 'guarantors', 'repayments'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'reviewed_at', 'approved_at', 'funded_at',
            'total_interest', 'total_repayment'
        ]
    
    def get_user_phone(self, obj):
        """Get user phone number with fallback"""
        return obj.user.phone if hasattr(obj.user, 'phone') and obj.user.phone else (
            obj.user.phone_number if hasattr(obj.user, 'phone_number') else None
        )
    
    def get_user_name(self, obj):
        """Get full name or username"""
        if obj.user.first_name and obj.user.last_name:
            return f"{obj.user.first_name} {obj.user.last_name}"
        return obj.user.username


class LoanApplicationListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views"""
    order_code = serializers.CharField(source='order.code', read_only=True)
    user_username = serializers.CharField(source='user.username', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_phone = serializers.SerializerMethodField()
    user_id = serializers.CharField(source='user.id', read_only=True)
    user_name = serializers.SerializerMethodField()
    collateral_count = serializers.SerializerMethodField()
    guarantor_count = serializers.SerializerMethodField()
    
    class Meta:
        model = LoanApplication
        fields = [
            'id', 'user', 'user_id', 'user_name', 'user_username', 'user_email', 'user_phone',
            'loan_type', 'loan_amount', 'status',
            'order_code', 'created_at', 'collateral_count', 'guarantor_count'
        ]
    
    def get_user_phone(self, obj):
        """Get user phone number with fallback"""
        return obj.user.phone if hasattr(obj.user, 'phone') and obj.user.phone else (
            obj.user.phone_number if hasattr(obj.user, 'phone_number') else None
        )
    
    def get_user_name(self, obj):
        """Get full name or username"""
        if obj.user.first_name and obj.user.last_name:
            return f"{obj.user.first_name} {obj.user.last_name}"
        return obj.user.username
    
    def get_collateral_count(self, obj):
        return obj.collateral_items.count()
    
    def get_guarantor_count(self, obj):
        return obj.guarantors.count()


class CreateLoanApplicationSerializer(serializers.Serializer):
    """Serializer for creating loan applications"""
    loan_type = serializers.ChoiceField(choices=['order_collateral', 'collateral_only'])
    loan_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    duration_days = serializers.IntegerField(min_value=1, max_value=365)
    purpose = serializers.CharField(max_length=1000)
    order_id = serializers.IntegerField(required=False, allow_null=True)
    
    # Collateral items (for collateral_only type)
    collateral_items = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True
    )
    
    # Guarantors
    guarantors = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True
    )
    
    def validate(self, data):
        loan_type = data.get('loan_type')
        
        # For order_collateral, order_id is required
        if loan_type == 'order_collateral':
            if not data.get('order_id'):
                raise serializers.ValidationError("order_id is required for order_collateral loans")
        
        # At least one guarantor is required
        guarantors = data.get('guarantors', [])
        if not guarantors:
            raise serializers.ValidationError("At least one guarantor is required")
        
        return data
    
    def create(self, validated_data):
        user = self.context['request'].user
        
        # Extract nested data
        collateral_items = validated_data.pop('collateral_items', [])
        guarantors = validated_data.pop('guarantors', [])
        order_id = validated_data.pop('order_id', None)
        
        # Handle order if provided
        order = None
        order_value = None
        if order_id:
            try:
                order = Order.objects.get(id=order_id)
                order_value = order.actual_price or order.price
            except Order.DoesNotExist:
                raise serializers.ValidationError("Order not found")
        
        # Create loan application
        loan_app = LoanApplication.objects.create(
            user=user,
            order=order,
            order_value=order_value,
            **validated_data
        )
        
        # Create collateral items
        for collateral in collateral_items:
            LoanCollateral.objects.create(
                loan_application=loan_app,
                collateral_type=collateral.get('type', 'other'),
                description=collateral.get('description', ''),
                estimated_value=collateral.get('estimated_value', 0)
            )
        
        # Create guarantors
        for guarantor in guarantors:
            LoanGuarantor.objects.create(
                loan_application=loan_app,
                name=guarantor.get('name', ''),
                phone_number=guarantor.get('phone_number', ''),
                email=guarantor.get('email', ''),
                relationship=guarantor.get('relationship', 'other')
            )
        
        return loan_app
