# financing/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone
from decimal import Decimal
import uuid

class LoanApplication(models.Model):
    """
    Stores loan applications from users waiting for review and approval.
    """
    
    STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('active', 'Active Loan'),
        ('repaid', 'Fully Repaid'),
        ('defaulted', 'Defaulted'),
        ('cancelled', 'Cancelled'),
    ]

    LOAN_TYPE_CHOICES = [
        ('order_collateral', 'Order Collateral'),
        ('collateral_only', 'Collateral Only'),
    ]

    # Primary identifiers
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='loan_applications'
    )
    
    # Loan details
    loan_type = models.CharField(max_length=20, choices=LOAN_TYPE_CHOICES)
    loan_amount = models.DecimalField(max_digits=12, decimal_places=2)
    duration_days = models.IntegerField()
    purpose = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Interest calculation
    daily_interest_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.0200'))  # 2%
    total_interest = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_repayment = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    
    # Order collateral (if applicable)
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loan_applications'
    )
    order_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    funded_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateTimeField(null=True, blank=True)
    
    # Admin review notes
    reviewer_notes = models.TextField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_loan_applications'
    )
    
    # Amount paid back
    amount_repaid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    def __str__(self):
        return f"LoanApplication {self.id} - {self.user.username} - {self.status}"
    
    def calculate_total_interest(self):
        """Calculate total interest based on loan amount and duration"""
        if self.loan_amount and self.duration_days:
            interest = self.loan_amount * self.daily_interest_rate * Decimal(str(self.duration_days))
            self.total_interest = interest
            self.total_repayment = self.loan_amount + interest
            return self.total_interest
        return Decimal('0.00')
    
    def save(self, *args, **kwargs):
        # Calculate interest if not already set
        if not self.total_interest:
            self.calculate_total_interest()
        super().save(*args, **kwargs)


class LoanCollateral(models.Model):
    """
    Stores collateral items for loan applications (property, vehicle, equipment, etc.)
    """
    
    COLLATERAL_TYPE_CHOICES = [
        ('property', 'Property'),
        ('vehicle', 'Vehicle'),
        ('equipment', 'Equipment'),
        ('jewelry', 'Jewelry/Gold'),
        ('electronics', 'Electronics'),
        ('order', 'Order'),
        ('other', 'Other'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan_application = models.ForeignKey(
        LoanApplication,
        on_delete=models.CASCADE,
        related_name='collateral_items'
    )
    
    collateral_type = models.CharField(max_length=20, choices=COLLATERAL_TYPE_CHOICES)
    description = models.TextField()
    estimated_value = models.DecimalField(max_digits=12, decimal_places=2)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.collateral_type} - {self.description[:50]}"


class LoanGuarantor(models.Model):
    """
    Stores guarantor information for loan applications
    """
    
    RELATIONSHIP_CHOICES = [
        ('friend', 'Friend'),
        ('family', 'Family Member'),
        ('employer', 'Employer'),
        ('colleague', 'Colleague'),
        ('business_partner', 'Business Partner'),
        ('other', 'Other'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan_application = models.ForeignKey(
        LoanApplication,
        on_delete=models.CASCADE,
        related_name='guarantors'
    )
    
    name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20)
    email = models.EmailField()
    relationship = models.CharField(max_length=20, choices=RELATIONSHIP_CHOICES)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} - {self.relationship}"


class LoanRepayment(models.Model):
    """
    Tracks repayment transactions for loans
    """
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan_application = models.ForeignKey(
        LoanApplication,
        on_delete=models.CASCADE,
        related_name='repayments'
    )
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    payment_method = models.CharField(max_length=50, null=True, blank=True)  # mpesa, bank_transfer, etc.
    transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"Repayment {self.id} - {self.amount} - {self.status}"


class Investment(models.Model):
    """
    Stores user investments and tracks expected returns
    """
    
    PLAN_TYPE_CHOICES = [
        ('starter', 'Starter'),
        ('professional', 'Professional'),
        ('enterprise', 'Enterprise'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending Payment'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='investments'
    )
    
    # Investment details
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Return calculations
    annual_return_percentage = models.DecimalField(max_digits=5, decimal_places=2)  # e.g., 15.00 for 15%
    expected_annual_return = models.DecimalField(max_digits=12, decimal_places=2)
    expected_monthly_return = models.DecimalField(max_digits=12, decimal_places=2)
    total_received_returns = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    
    # Duration details
    lockup_period_months = models.IntegerField()  # 12, 18, 24, or 36 months
    investment_date = models.DateTimeField(auto_now_add=True)
    maturity_date = models.DateTimeField()
    
    # Payment tracking
    payment_method = models.CharField(max_length=50, null=True, blank=True)  # mpesa, bank_transfer, etc.
    transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    
    # Return payouts
    next_payout_date = models.DateTimeField(null=True, blank=True)
    last_payout_date = models.DateTimeField(null=True, blank=True)
    payout_frequency = models.CharField(max_length=20, default='monthly')  # monthly, bi-weekly, weekly
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def calculate_maturity_date(self):
        """Calculate maturity date based on investment date and lockup period"""
        from datetime import timedelta
        return timezone.now() + timedelta(days=self.lockup_period_months * 30)
    
    def calculate_expected_returns(self):
        """Calculate expected annual and monthly returns"""
        annual = (self.amount * self.annual_return_percentage) / Decimal('100')
        monthly = annual / Decimal('12')
        self.expected_annual_return = annual
        self.expected_monthly_return = monthly
        return annual, monthly
    
    def save(self, *args, **kwargs):
        if not self.maturity_date:  # Only on creation
            self.maturity_date = self.calculate_maturity_date()
            self.calculate_expected_returns()
            # Set next payout date based on plan
            if self.plan_type == 'starter':
                freq_days = 30
                self.payout_frequency = 'monthly'
            elif self.plan_type == 'professional':
                freq_days = 14
                self.payout_frequency = 'bi-weekly'
            else:  # enterprise
                freq_days = 7
                self.payout_frequency = 'weekly'
            
            from datetime import timedelta
            self.next_payout_date = timezone.now() + timedelta(days=freq_days)
        
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"Investment {self.id} - {self.user.username} - {self.plan_type} - KSh {self.amount}"


class ShareInvestment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Payment'),
        ('success', 'Paid'),
        ('failed', 'Payment Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='share_investments',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=32)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    checkout_request_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Share investment {self.id} - KSh {self.amount} - {self.status}"
