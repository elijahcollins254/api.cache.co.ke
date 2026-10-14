from django.contrib import admin

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
    list_display = ('name', 'slug', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}
    actions = [deactivate_product_categories]


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
    prepopulated_fields = {'slug': ('name',)}
    actions = [activate_products, deactivate_products]