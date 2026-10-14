from django.utils.text import slugify
from rest_framework import serializers

from .models import MerchantProfile, Shop, ShopCategory, Product, ProductCategory


class MerchantProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerchantProfile
        fields = [
            'id',
            'merchant_username',
            'business_name',
            'phone',
            'email',
            'description',
            'status',
            'is_verified',
            'created_at',
        ]
        read_only_fields = ['id', 'status', 'is_verified', 'created_at']


class ShopCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ShopCategory
        fields = ['id', 'name', 'slug', 'description']


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['id', 'name', 'slug', 'description']


class ShopSerializer(serializers.ModelSerializer):
    owner = MerchantProfileSerializer(read_only=True, allow_null=True)
    merchant_username = serializers.SerializerMethodField()
    catalog_count = serializers.IntegerField(read_only=True, required=False)
    category = ShopCategorySerializer(read_only=True, allow_null=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source='category',
        queryset=ShopCategory.objects.filter(is_active=True),
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Shop
        fields = [
            'id',
            'owner',
            'merchant_username',
            'catalog_count',
            'category',
            'category_id',
            'name',
            'slug',
            'description',
            'logo',
            'banner',
            'is_verified',
            'status',
            'created_at',
        ]
        read_only_fields = ['id', 'owner', 'merchant_username', 'created_at']

    def get_merchant_username(self, obj):
        return obj.owner.merchant_username if obj.owner else None


class ProductSerializer(serializers.ModelSerializer):
    shop = ShopSerializer(read_only=True)
    category = ProductCategorySerializer(read_only=True, allow_null=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source='category',
        queryset=ProductCategory.objects.filter(is_active=True),
        write_only=True,
        required=False,
        allow_null=True,
    )
    shop_id = serializers.PrimaryKeyRelatedField(
        source='shop',
        queryset=Shop.objects.all(),
        write_only=True,
    )
    slug = serializers.SlugField(required=False, allow_blank=True)

    class Meta:
        model = Product
        fields = [
            'id',
            'shop',
            'shop_id',
            'category',
            'category_id',
            'name',
            'slug',
            'description',
            'price',
            'stock_quantity',
            'image',
            'image_url',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'shop']

    def validate(self, attrs):
        attrs = dict(attrs)
        attrs['slug'] = attrs.get('slug') or slugify(attrs.get('name', ''))
        if not attrs['slug']:
            raise serializers.ValidationError({'slug': 'A valid product slug is required.'})
        return attrs

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if not representation.get('image_url') and instance.image:
            request = self.context.get('request')
            representation['image_url'] = (
                request.build_absolute_uri(instance.image.url) if request else instance.image.url
            )
        return representation
