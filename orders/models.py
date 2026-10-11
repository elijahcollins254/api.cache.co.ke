# orders/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone
from services.models import Service
from users.models import Location
from decimal import Decimal
import hashlib
import random
import secrets

class Order(models.Model):
    STATUS_CHOICES = [
        ('pending_payment', 'Pending Payment'),
        ('partially_paid', 'Partially Paid'),
        ('requested', 'Order Placed'),
        ('pending_assignment', 'Finding a Rider'),
        ('assigned_pickup', 'Rider Assigned'),
        ('accepted_delivery', 'Rider Confirmed'),
        ('picked', 'Picked Up'),
        ('at_gate', 'At the Gate'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,            # allow null
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )

    # Support multiple services per order
    services = models.ManyToManyField(Service, related_name="orders")
    
    # Keep service field for backward compatibility (will be the first service or None)
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_orders")
    service_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        help_text="The location where this order is being processed"
    )
    code = models.CharField(max_length=32, unique=True, blank=True)  # e.g. "WW-12345"
    pickup_address = models.TextField()
    dropoff_address = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested')
    urgency = models.IntegerField(default=1)  # e.g. 1–5 urgency level
    items = models.IntegerField(default=1)    # number of items
    package = models.IntegerField(default=1, blank=True)  # you already had this
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # store numeric
    # Staff-entered actual price paid (may differ from estimate)
    actual_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual price paid for the order recorded by staff"
    )
    applied_offer = models.ForeignKey(
        'offers.Offer',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='orders_with_offer',
        help_text="Offer claimed and applied during checkout"
    )
    offer_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Cash discount deducted from the order price"
    )
    free_delivery = models.BooleanField(default=False, help_text="Whether delivery is free for this order")
    # Payment method used for this order
    payment_method = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        choices=[
            ('mpesa', 'M-Pesa'),
            ('card', 'Card'),
            ('bnpl', 'Buy Now Pay Later'),
            ('cash', 'Cash'),
        ],
        help_text="Payment method used for this order"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    estimated_delivery = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)  # helpful to record real delivery time
    delivery_code = models.CharField(max_length=4, null=True, blank=True)
    delivery_code_hash = models.CharField(max_length=128, null=True, blank=True)
    delivery_code_sent_at = models.DateTimeField(null=True, blank=True)
    gate_notified_at = models.DateTimeField(null=True, blank=True)
    pickup_confirmed_at = models.DateTimeField(null=True, blank=True)
    
    # --- RIDER TRACKING (SEPARATE PICKUP & DELIVERY) ---
    # Original rider field kept for backward compatibility
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_orders",
        help_text="[DEPRECATED] Use pickup_rider or delivery_rider instead"
    )
    
    # Pickup rider - who collects from customer
    pickup_rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pickup_orders",
        help_text="Rider who picks up from customer"
    )
    picked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when order was picked up"
    )
    
    # Delivery rider - who delivers to customer
    delivery_rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="delivery_orders",
        help_text="Rider who delivers to customer"
    )
    ready_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when order was ready for delivery"
    )
    # Rider-added details during pickup
    quantity = models.IntegerField(
        null=True,
        blank=True,
        help_text="Quantity of items picked up by rider"
    )
    description = models.TextField(
        null=True,
        blank=True,
        help_text="Rider's notes about the order"
    )
    # Optional requested pickup datetime set by customer when scheduling
    requested_pickup_at = models.DateTimeField(null=True, blank=True)
    
    # Fields for staff-created manual orders
    order_type = models.CharField(
        max_length=20,
        choices=[
            ('online', 'Online Order'),
            ('manual', 'Staff-Created Order'),
        ],
        default='online',
        help_text="Whether order was created online or manually by staff"
    )
    drop_off_type = models.CharField(
        max_length=20,
        choices=[
            ('delivery', 'Customer Delivery'),
            ('walk_in', 'Walk-in Customer'),
            ('phone', 'Phone Order'),
        ],
        default='delivery',
        help_text="How the order was dropped off/created"
    )
    # Customer details for manual orders (when user is None or guest)
    customer_name = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Customer name for manual/walk-in orders"
    )
    customer_phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Customer phone for manual/walk-in orders"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_orders",
        help_text="Staff member who created this manual order"
    )
    # Track delivery request status
    delivery_requested = models.BooleanField(
        default=False,
        help_text="Whether customer has requested delivery"
    )
    delivery_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when customer requested delivery"
    )
    # Track washing and folding workflow
    washer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="washed_orders",
        help_text="Staff member who washed the order"
    )
    washed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when order was washed"
    )
    folder = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="folded_orders",
        help_text="Staff member who folded the order"
    )
    folded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when order was folded"
    )
    # Washer input details
    washer_items = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of items recorded by washer"
    )
    washer_weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Weight (kg) recorded by washer"
    )
    washer_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes/description recorded by washer"
    )
    washer_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual price recorded by washer"
    )
    
    # Folder input details
    folder_items = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of items recorded by folder"
    )
    folder_weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Weight (kg) recorded by folder"
    )
    folder_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes/description recorded by folder"
    )
    folder_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual price recorded by folder"
    )
    
    # Fumigator workflow
    fumigator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fumigated_orders",
        help_text="Staff member who fumigated the order"
    )
    fumigated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when order was fumigated"
    )
    # Fumigator input details
    fumigator_items = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of items recorded by fumigator"
    )
    fumigator_weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Weight (kg) recorded by fumigator"
    )
    fumigator_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes/description recorded by fumigator"
    )
    fumigator_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual price recorded by fumigator"
    )
    
    # Rider input details
    rider_items = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of items recorded by rider"
    )
    rider_weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Weight (kg) recorded by rider"
    )
    rider_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes/description recorded by rider during delivery"
    )
    rider_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual price recorded by rider"
    )


    def save(self, *args, **kwargs):
        if not self.code:
            alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
            parts = [''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
            self.code = f"CM-{'-'.join(parts)}"
        # Set the first service as primary service for backward compatibility
        if not self.service and self.pk:
            first_service = self.services.first()
            if first_service:
                self.service = first_service
        super().save(*args, **kwargs)

    def get_total_price(self):
        """Calculate total price from all services"""
        return sum(service.price for service in self.services.all())

    def get_latest_staff_price(self):
        """
        Get the latest actual price recorded by any staff member.
        Returns the most recently recorded price from washer, folder, fumigator, or rider.
        Returns None if no staff member has recorded a price yet.
        """
        prices_with_timestamps = []
        
        if self.washer_price and self.washed_at:
            prices_with_timestamps.append((self.washer_price, self.washed_at))
        
        if self.folder_price and self.folded_at:
            prices_with_timestamps.append((self.folder_price, self.folded_at))
        
        if self.fumigator_price and self.fumigated_at:
            prices_with_timestamps.append((self.fumigator_price, self.fumigated_at))
        
        if self.rider_price and self.delivered_at:
            prices_with_timestamps.append((self.rider_price, self.delivered_at))
        
        if not prices_with_timestamps:
            return None
        
        # Return the price with the most recent timestamp
        return max(prices_with_timestamps, key=lambda x: x[1])[0]

    def get_actual_status_display(self):
        """
        Get the actual status display.
        If status is 'pending_assignment' but a rider is assigned, return a more accurate status.
        """
        if self.status == 'pending_assignment':
            if self.rider:
                # If rider is assigned but status is still pending_assignment, show as 'Rider Assigned'
                return 'Rider Assigned'
            return self.get_status_display()
        return self.get_status_display()

    # Valid status transitions
    VALID_TRANSITIONS = {
        'pending_payment': ['partially_paid', 'requested', 'cancelled'],
        'partially_paid': ['requested', 'cancelled'],
        'requested': ['pending_assignment', 'assigned_pickup', 'cancelled'],
        'pending_assignment': ['assigned_pickup', 'cancelled'],
        'assigned_pickup': ['accepted_delivery', 'cancelled'],
        'accepted_delivery': ['picked', 'cancelled'],
        'picked': ['at_gate', 'cancelled'],
        'at_gate': ['delivered', 'cancelled'],
        'delivered': ['cancelled'],
        'cancelled': [],
    }

    def can_transition_to(self, new_status):
        """Check if this order can transition from current status to new status"""
        current = self.status.lower()
        new = new_status.lower()
        
        if current not in self.VALID_TRANSITIONS:
            return False
        
        return new in self.VALID_TRANSITIONS[current]

    def is_assigned_to_pickup_rider(self, user):
        """Check if user is the assigned pickup rider"""
        return self.pickup_rider == user or (
            # Fallback to old rider field for backward compatibility
            not self.pickup_rider and self.rider == user and 
            self.status in ['assigned_pickup', 'accepted_delivery', 'picked', 'at_gate']
        )

    def is_assigned_to_delivery_rider(self, user):
        """Check if user is the assigned delivery rider"""
        return self.delivery_rider == user

    def is_rider_for_any_stage(self, user):
        """Check if user is assigned as either pickup or delivery rider"""
        return self.is_assigned_to_pickup_rider(user) or self.is_assigned_to_delivery_rider(user)

    def is_paid(self):
        """Check if this order has a successful payment"""
        from payments.models import Payment
        return Payment.objects.filter(
            order_id=self.id,
            status=Payment.STATUS_SUCCESS
        ).exists()

    def _hash_delivery_code(self, raw_code):
        return hashlib.sha256(str(raw_code).strip().encode('utf-8')).hexdigest()

    def generate_delivery_code(self):
        """Generate a 4-digit customer delivery code and store its hash."""
        code = f"{random.randint(1000, 9999):04d}"
        self.delivery_code = code
        self.delivery_code_hash = self._hash_delivery_code(code)
        self.delivery_code_sent_at = timezone.now()
        self.save(update_fields=['delivery_code', 'delivery_code_hash', 'delivery_code_sent_at', 'updated_at'])
        return code

    def verify_delivery_code(self, raw_code):
        if not raw_code or not self.delivery_code_hash:
            return False
        return self._hash_delivery_code(raw_code) == self.delivery_code_hash

    def __str__(self):
        return f"Order #{self.id} - {self.user.username if self.user else 'Guest'}"

    class Meta:
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['rider', 'status']),
            models.Index(fields=['status']),
            models.Index(fields=['code']),
        ]


class OrderReview(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='review')
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='order_reviews')
    rating = models.PositiveSmallIntegerField()
    feedback = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.order.code} review ({self.rating}/5)'


class OrderItem(models.Model):
    """Track individual service items and their quantities in an order"""
    order = models.ForeignKey(Order, related_name='order_items', on_delete=models.CASCADE)
    service = models.ForeignKey(Service, related_name='order_items', on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1, help_text="Quantity of this service in the order")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['order', 'service']]
        ordering = ['created_at']

    def __str__(self):
        return f"{self.order.code} - {self.service.name} x{self.quantity}"


class ProductOrderItem(models.Model):
    """Snapshot of a marketplace product included in a delivery order."""
    order = models.ForeignKey(Order, related_name='product_order_items', on_delete=models.CASCADE)
    product = models.ForeignKey('marketplace.Product', related_name='order_items', on_delete=models.PROTECT)
    product_name = models.CharField(max_length=160)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['order', 'product']]
        ordering = ['created_at']

    def __str__(self):
        return f"{self.order.code} - {self.product_name} x{self.quantity}"


class OrderEvent(models.Model):
    """A lightweight audit/event record for actions taken on an Order.

    Examples: order_created, status_changed, pickup_details_recorded, assigned_rider
    """
    order = models.ForeignKey(Order, related_name='events', on_delete=models.CASCADE)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='order_events',
    )
    event_type = models.CharField(max_length=64)
    data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"OrderEvent({self.order.code}): {self.event_type} @ {self.created_at}"
