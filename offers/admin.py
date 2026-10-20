from django.contrib import admin
from .models import Offer, UserOffer, OfferNotificationSubscription
from .services import notify_push_devices_of_new_offer, notify_subscribers_of_new_offer

@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('title', 'code', 'discount_percent', 'discount_amount', 'is_active', 'current_uses', 'max_uses')
    list_filter = ('is_active', 'valid_from', 'valid_until')
    search_fields = ('title', 'code', 'description')
    readonly_fields = ('current_uses',)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change and obj.is_active:
            notify_subscribers_of_new_offer(obj)
            notify_push_devices_of_new_offer(obj)

@admin.register(UserOffer)
class UserOfferAdmin(admin.ModelAdmin):
    list_display = ('user', 'offer', 'claimed_at', 'used_at', 'is_used')
    list_filter = ('is_used', 'claimed_at', 'used_at')
    search_fields = ('user__username', 'user__email', 'offer__title', 'offer__code')
    readonly_fields = ('claimed_at',)

@admin.register(OfferNotificationSubscription)
class OfferNotificationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('get_user_or_phone', 'is_active', 'created_at', 'updated_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('user__username', 'user__email', 'phone_number')
    readonly_fields = ('created_at', 'updated_at')

    def get_user_or_phone(self, obj):
        return obj.user.username if obj.user else obj.phone_number
    get_user_or_phone.short_description = 'User / Phone'