from django.contrib import admin
from django.utils.text import slugify
from .models import ServiceCategory, Service


@admin.action(description="Activate selected services")
def activate_services(modeladmin, request, queryset):
    queryset.update(is_active=True)


@admin.action(description="Deactivate selected services")
def deactivate_services(modeladmin, request, queryset):
    queryset.update(is_active=False)


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "featured_on_homepage", "service_count", "created_at")
    list_filter = ("is_active", "featured_on_homepage", "created_at")
    search_fields = ("name", "description")
    ordering = ("name",)
    readonly_fields = ("slug", "created_at", "updated_at")
    
    def save_model(self, request, obj, form, change):
        if not obj.slug:
            obj.slug = slugify(obj.name)
        if obj.featured_on_homepage:
            from marketplace.models import ProductCategory

            ServiceCategory.objects.exclude(pk=obj.pk).update(featured_on_homepage=False)
            ProductCategory.objects.update(featured_on_homepage=False)
        super().save_model(request, obj, form, change)
    
    def service_count(self, obj):
        """Display number of services in this category."""
        count = obj.services.filter(is_active=True).count()
        return count
    service_count.short_description = "Active Services"


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "shop", "category", "price", "is_active", "created_at")
    list_filter = ("shop", "category", "is_active", "created_at")
    search_fields = ("name", "description", "category__name", "shop__name")
    ordering = ("category", "name")
    readonly_fields = ("created_at", "updated_at")
    actions = [activate_services, deactivate_services]

    fieldsets = (
        ("Service Information", {
            'fields': ('name', 'shop', 'category', 'price')
        }),
        ("Details", {
            'fields': ('description', 'image_url', 'image')
        }),
        ("Status", {
            'fields': ('is_active',)
        }),
        ("Timestamps", {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
