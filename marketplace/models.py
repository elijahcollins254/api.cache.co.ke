from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify


class MerchantProfile(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending review'),
        ('verified', 'Verified'),
        ('suspended', 'Suspended'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='merchant_profile')
    merchant_username = models.SlugField(max_length=100, unique=True)
    business_name = models.CharField(max_length=160)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.merchant_username = slugify(self.merchant_username or self.business_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.business_name


class ShopCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Shop category'
        verbose_name_plural = 'Shop categories'

    def save(self, *args, **kwargs):
        self.slug = slugify(self.slug or self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ProductCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Product category'
        verbose_name_plural = 'Product categories'

    def save(self, *args, **kwargs):
        self.slug = slugify(self.slug or self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Shop(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('pending', 'Pending review'),
        ('suspended', 'Suspended'),
    ]

    # A null owner is reserved for the platform-managed legacy services shop.
    owner = models.ForeignKey(MerchantProfile, on_delete=models.CASCADE, related_name='shops', null=True, blank=True)
    category = models.ForeignKey(
        ShopCategory,
        on_delete=models.PROTECT,
        related_name='shops',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    description = models.TextField(blank=True)
    logo = models.ImageField(upload_to='shops/logos/', blank=True, null=True)
    banner = models.ImageField(upload_to='shops/banners/', blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.slug = slugify(self.slug or self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Product(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='products')
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name='products',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0.01)])
    stock_quantity = models.PositiveIntegerField(default=0)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['shop', 'slug'], name='unique_product_slug_per_shop'),
        ]

    def save(self, *args, **kwargs):
        self.slug = slugify(self.slug or self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.shop.name})'


class MerchantOrderFulfillment(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_PREPARED = 'prepared'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending acceptance'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_PREPARED, 'Prepared for pickup'),
    ]

    merchant = models.ForeignKey(MerchantProfile, on_delete=models.CASCADE, related_name='order_fulfillments')
    order = models.ForeignKey('orders.Order', on_delete=models.CASCADE, related_name='merchant_fulfillments')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['merchant', 'order'], name='unique_merchant_order_fulfillment'),
        ]


class MerchantWallet(models.Model):
    merchant = models.OneToOneField(MerchantProfile, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class MerchantWithdrawal(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_PAID = 'paid'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_PAID, 'Paid'),
        (STATUS_FAILED, 'Failed'),
    ]

    merchant = models.ForeignKey(MerchantProfile, on_delete=models.CASCADE, related_name='withdrawals')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    conversation_id = models.CharField(max_length=128, blank=True, db_index=True)
    originator_conversation_id = models.CharField(max_length=128, blank=True)
    transaction_reference = models.CharField(max_length=128, blank=True)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class MerchantLedgerEntry(models.Model):
    TYPE_EARNING = 'earning'
    TYPE_WITHDRAWAL = 'withdrawal'
    TYPE_REFUND = 'refund'
    TYPE_CHOICES = [
        (TYPE_EARNING, 'Product sale'),
        (TYPE_WITHDRAWAL, 'Withdrawal'),
        (TYPE_REFUND, 'Withdrawal refund'),
    ]

    merchant = models.ForeignKey(MerchantProfile, on_delete=models.CASCADE, related_name='ledger_entries')
    order = models.ForeignKey(
        'orders.Order', null=True, blank=True, on_delete=models.PROTECT, related_name='merchant_earnings'
    )
    withdrawal = models.ForeignKey(
        MerchantWithdrawal, null=True, blank=True, on_delete=models.PROTECT, related_name='ledger_entries'
    )
    entry_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['merchant', 'order'], condition=models.Q(entry_type='earning'),
                name='unique_merchant_order_earning',
            ),
            models.UniqueConstraint(
                fields=['merchant', 'withdrawal'], condition=models.Q(entry_type='withdrawal'),
                name='unique_merchant_withdrawal_debit',
            ),
            models.UniqueConstraint(
                fields=['merchant', 'withdrawal'], condition=models.Q(entry_type='refund'),
                name='unique_merchant_withdrawal_refund',
            ),
        ]