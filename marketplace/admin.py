from django.contrib import admin
from django.db.models import Count

from .models import MerchantProfile, Product, ProductCategory, Shop, ShopCategory


@admin.action(description='Activate selected products')
def activate_products(modeladmin, request, queryset):
    queryset.update(is_active=True)


@admin.action(description='Deactivate selected products')
def deactivate_products(modeladmin, request, queryset):
    queryset.update(is_active=False)


@admin.action(description='Deactivate selected product categories')
def deactivate_product_categories(modeladmin, request, queryset):
    queryset.update(is_active=False)


@admin.register(MerchantProfile)
class MerchantProfileAdmin(admin.ModelAdmin):
    list_display = ('business_name', 'merchant_username', 'user', 'status', 'is_verified')
    list_filter = ('status', 'is_verified')
    search_fields = ('business_name', 'merchant_username', 'user__email')


@admin.register(ShopCategory)
class ShopCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'product_count', 'is_active', 'featured_on_homepage', 'created_at')
    list_filter = ('is_active', 'featured_on_homepage')
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}
    actions = [deactivate_product_categories]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(product_count=Count('products'))

    @admin.display(description='Products', ordering='product_count')
    def product_count(self, obj):
        return obj.product_count

    def save_model(self, request, obj, form, change):
        if obj.featured_on_homepage:
            from services.models import ServiceCategory

            ProductCategory.objects.exclude(pk=obj.pk).update(featured_on_homepage=False)
            ServiceCategory.objects.update(featured_on_homepage=False)
        super().save_model(request, obj, form, change)


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'owner', 'status', 'is_verified', 'created_at')
    list_filter = ('category', 'status', 'is_verified')
    search_fields = ('name', 'slug', 'owner__business_name')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'shop', 'price', 'stock_quantity', 'is_active')
    list_filter = ('category', 'shop', 'is_active')
    search_fields = ('name', 'description', 'category__name', 'shop__name')
    autocomplete_fields = ('shop', 'category')
    prepopulated_fields = {'slug': ('name',)}
    actions = [activate_products, deactivate_products]