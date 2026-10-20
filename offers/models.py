from django.db import models
from users.models import User
from django.utils import timezone

class Offer(models.Model):
    BENEFIT_TYPES = (
        ('cash_discount', 'Cash Discount'),
        ('free_delivery', 'Free Delivery'),
        ('other', 'Other'),
    )

    title = models.CharField(max_length=200)
    description = models.TextField()
    discount_percent = models.IntegerField(default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    benefit_type = models.CharField(max_length=30, choices=BENEFIT_TYPES, default='cash_discount')
    code = models.CharField(max_length=50, unique=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    max_uses = models.IntegerField(null=True, blank=True)
    current_uses = models.IntegerField(default=0)
    
    def __str__(self):
        return self.title

class UserOffer(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='claimed_offers')
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name='user_claims')
    claimed_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        unique_together = ('user', 'offer')
        ordering = ['-claimed_at']

    def __str__(self):
        return f"{self.user.username} - {self.offer.title}"


class OfferNotificationSubscription(models.Model):
    """
    Model to track users who have subscribed to offer notifications via SMS.
    Can be linked to a User or tracked by phone number for non-registered users.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='offer_subscription'
    )
    phone_number = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        help_text="Phone number for SMS notifications (if user not registered)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['phone_number']),
        ]

    def __str__(self):
        if self.user:
            return f"{self.user.username} - Offer Notifications"
        return f"{self.phone_number} - Offer Notifications"


class OfferPushDevice(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='offer_push_devices',
    )
    token = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.user.username} - Offer Push Device"