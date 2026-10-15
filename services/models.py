from django.db import models

class ServiceCategory(models.Model):
    """Service categories that can be managed through the admin interface."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, help_text='URL-friendly name (auto-generated)')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, help_text='Inactive categories will be hidden from the website')
    featured_on_homepage = models.BooleanField(
        default=False,
        help_text='Show this category first on the home page. Selecting it clears the previous featured category.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Service Category'
        verbose_name_plural = 'Service Categories'

    def __str__(self):
        return self.name


class Service(models.Model):
    """Individual services organized by category."""
    name = models.CharField(max_length=100)
    shop = models.ForeignKey(
        'marketplace.Shop',
        on_delete=models.PROTECT,
        related_name='services',
        null=True,
        blank=True,
    )
    category = models.ForeignKey(ServiceCategory, on_delete=models.PROTECT, related_name='services', null=True, blank=True)
    category_name = models.CharField(max_length=30, null=True, blank=True, help_text='Legacy: kept for migration purposes')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='services/', null=True, blank=True, help_text='Service image')
    image_url = models.URLField(blank=True, null=True, help_text='Remote image URL (preferred over uploaded image)')
    is_active = models.BooleanField(default=True, help_text='Inactive services will be hidden from the website')
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        if self.category:
            return f"{self.name} ({self.category.name})"
        elif self.category_name:
            return f"{self.name} ({self.category_name})"
        return self.name
