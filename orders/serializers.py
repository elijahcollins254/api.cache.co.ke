# orders/serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import transaction
from .models import Order, OrderItem, OrderReview
from services.models import Service
from users.models import Location
from offers.models import UserOffer
from marketplace.models import Product
from decimal import Decimal
from django.utils import timezone

User = get_user_model()


class OrderReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderReview
        fields = ['rating', 'feedback', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError('Rating must be between 1 and 5.')
        return value


class OrderListLightSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for order lists - only essential fields for performance
    Used for staff dashboards and mobile apps where we need fast response times
    Reduces response size by 70-80% compared to full OrderListSerializer
    """
    status = serializers.CharField()
    service_name = serializers.CharField(source="service.name", read_only=True, allow_null=True)
    user_name = serializers.SerializerMethodField()
    rider_name = serializers.SerializerMethodField()
    delivery_code = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    review_rating = serializers.SerializerMethodField()
    
    def get_user_name(self, obj):
        if not obj.user:
            return obj.customer_name or "Unknown"
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username
    
    def get_rider_name(self, obj):
        rider = obj.delivery_rider or obj.pickup_rider or obj.rider
        if not rider:
            return "Unassigned"
        return f"{rider.first_name} {rider.last_name}".strip() or rider.username

    def get_delivery_code(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if (
            not user
            or not user.is_authenticated
            or obj.user_id != user.id
            or obj.status in ['delivered', 'cancelled']
        ):
            return None
        return obj.delivery_code

    def get_timeline(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated or obj.user_id != user.id:
            return []

        status_labels = {
            'pending_payment': 'Payment pending',
            'partially_paid': 'Partially paid',
            'requested': 'Order placed',
            'pending_assignment': 'Finding a rider',
            'assigned_pickup': 'Rider assigned',
            'accepted_delivery': 'Rider confirmed',
            'picked': 'Picked up',
            'at_gate': 'At the gate',
            'delivered': 'Delivered',
            'cancelled': 'Cancelled',
        }
        timeline = [{
            'key': f'created-{obj.pk}',
            'label': 'Order placed',
            'timestamp': obj.created_at.isoformat() if obj.created_at else None,
        }]
        latest_status = None

        for event in obj.events.filter(
            event_type__in=['status_changed', 'rider_assigned'],
        ).order_by('created_at'):
            if event.event_type == 'status_changed':
                new_status = (event.data or {}).get('new')
                if not new_status:
                    continue
                label = status_labels.get(new_status, new_status.replace('_', ' ').capitalize())
                latest_status = new_status
                timeline.append({
                    'key': f'event-{event.pk}',
                    'label': label,
                    'timestamp': event.created_at.isoformat() if event.created_at else None,
                })
            else:
                rider_name = (event.data or {}).get('new')
                timeline.append({
                    'key': f'event-{event.pk}',
                    'label': 'Rider assigned',
                    'subtitle': f'Assigned to {rider_name}' if rider_name else None,
                    'timestamp': event.created_at.isoformat() if event.created_at else None,
                })

        if (
            obj.status != latest_status
            and status_labels.get(obj.status) != 'Order placed'
        ):
            status_timestamp = {
                'accepted_delivery': obj.delivery_code_sent_at,
                'picked': obj.picked_at,
                'at_gate': obj.gate_notified_at,
                'delivered': obj.delivered_at,
            }.get(obj.status)
            timeline.append({
                'key': f'current-{obj.pk}',
                'label': status_labels.get(obj.status, obj.status.replace('_', ' ').capitalize()),
                'timestamp': status_timestamp.isoformat() if status_timestamp else None,
            })

        return timeline

    def get_review_rating(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated or obj.user_id != user.id:
            return None
        try:
            review = obj.review
        except OrderReview.DoesNotExist:
            return None
        return review.rating if review else None
    
    class Meta:
        model = Order
        fields = [
            "id", "code", "status", "created_at", "customer_name", "service_name",
            "user_name", "rider_name", "price", "actual_price", "is_paid", "order_type",
            "delivery_code", "timeline", "review_rating",
        ]


class OrderCreateSerializer(serializers.ModelSerializer):
    # Accept multiple services via 'services' field
    services = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.all(),
        many=True,
        write_only=True,
        required=False
    )
    # Keep 'service' for backward compatibility (single service)
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.all(),
        required=False
    )
    service_location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects.filter(is_active=True),
        required=False
    )
    # Accept service quantities as a list of {service_id, quantity} objects
    service_quantities = serializers.ListField(
        child=serializers.DictField(child=serializers.IntegerField()),
        write_only=True,
        required=False
    )
    products = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.filter(is_active=True),
        many=True,
        write_only=True,
        required=False,
    )
    product_quantities = serializers.ListField(
        child=serializers.DictField(child=serializers.IntegerField()),
        write_only=True,
        required=False,
    )
    total_price = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "code",
            "service",
            "services",
            "service_location",
            "description",
            "pickup_address",
            "dropoff_address",
            "requested_pickup_at",
            "urgency",
            "items",
            "package",
            "weight_kg",
            "price",
            "total_price",
            "estimated_delivery",
            "service_quantities",
            "products",
            "product_quantities",
            "payment_method",
            # Manual order fields
            "order_type",
            "drop_off_type",
            "customer_name",
            "customer_phone",
        ]
        read_only_fields = ["id", "code", "total_price"]

    def validate(self, data):
        # basic sanity defaults / checks
        if data.get("items") is None:
            data["items"] = 1
        if data.get("items", 0) < 1:
            raise serializers.ValidationError({"items": "Must be at least 1"})
        
        # Check if this is a manual order
        order_type = data.get("order_type", "online")
        
        if order_type == "manual":
            # Manual orders require customer details instead of services
            if not data.get("customer_name"):
                raise serializers.ValidationError({"customer_name": "Customer name is required for manual orders"})
            if not data.get("customer_phone"):
                raise serializers.ValidationError({"customer_phone": "Customer phone is required for manual orders"})
        else:
            # Online orders require at least one service or marketplace product.
            services = data.get("services", [])
            service = data.get("service")
            products = data.get("products", [])
            if not services and not service and not products:
                raise serializers.ValidationError(
                    {"services": "Add at least one service or product"}
                )

            products_by_id = {product.id: product for product in products}
            product_quantities = data.get("product_quantities", [])
            quantities_by_product = {}
            for item in product_quantities:
                product_id = item.get("product_id")
                quantity = item.get("quantity", 1)
                if product_id not in products_by_id:
                    raise serializers.ValidationError({"product_quantities": "Product quantity refers to an item not in the order."})
                if quantity < 1:
                    raise serializers.ValidationError({"product_quantities": "Product quantities must be at least 1."})
                quantities_by_product[product_id] = quantity

            for product in products:
                if product.shop.status != "active" or not product.shop.is_verified:
                    raise serializers.ValidationError({"products": f"{product.name} is not currently available."})
                requested_quantity = quantities_by_product.get(product.id, 1)
                if requested_quantity > product.stock_quantity:
                    raise serializers.ValidationError({"products": f"Only {product.stock_quantity} of {product.name} are available."})
        
        # allow weight_kg and price to be null for backend calculation
        return data

    @transaction.atomic
    def create(self, validated_data):
        """
        Attach order to:
         - request.user if authenticated (for manual orders by staff), else
         - a single guest user (created if missing) for online orders.
        Automatically set service_location from user's service_location.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None)
        order_type = validated_data.get("order_type", "online")

        # For manual orders, track which staff member created it
        created_by_user = None
        if order_type == "manual" and user and getattr(user, "is_authenticated", False):
            created_by_user = user
            # Manual orders don't necessarily need a user linked
            if 'user' not in validated_data or validated_data['user'] is None:
                # Create or get guest user for manual orders
                guest_username = "guest_orders"
                guest_email = "guest@cache.local"
                guest_user, created = User.objects.get_or_create(
                    username=guest_username,
                    defaults={"email": guest_email, "is_active": False},
                )
                if created:
                    guest_user.set_unusable_password()
                    guest_user.save()
                validated_data['user'] = guest_user
        else:
            # For online orders, use normal user handling
            if not (user and getattr(user, "is_authenticated", False)):
                guest_username = "guest_orders"
                guest_email = "guest@cache.local"
                guest_user, created = User.objects.get_or_create(
                    username=guest_username,
                    defaults={"email": guest_email, "is_active": False},
                )
                if created:
                    guest_user.set_unusable_password()
                    guest_user.save()
                user = guest_user

            if 'user' not in validated_data:
                validated_data['user'] = user
        
        # Auto-set service_location from staff's service_location for manual orders
        if 'service_location' not in validated_data or not validated_data['service_location']:
            if order_type == "manual" and created_by_user and created_by_user.service_location:
                # Manual orders inherit staff member's location
                validated_data['service_location'] = created_by_user.service_location
            elif user and user.service_location:
                # For online orders, use user's service_location
                validated_data['service_location'] = user.service_location
            elif user and user.location:
                # Try to infer from user's location field
                user_location = user.location.lower().strip()
                from users.models import Location
                inferred_location = Location.objects.filter(
                    name__icontains=user_location,
                    is_active=True
                ).first()
                if inferred_location:
                    validated_data['service_location'] = inferred_location
            else:
                # Try to infer from pickup_address
                pickup_address = validated_data.get('pickup_address', '').lower()
                from users.models import Location
                locations = Location.objects.filter(is_active=True)
                for loc in locations:
                    if loc.name.lower() in pickup_address:
                        validated_data['service_location'] = loc
                        break
        
        # Extract services list for M2M
        services = validated_data.pop('services', [])
        products = validated_data.pop('products', [])
        # Extract service_quantities if provided
        service_quantities = validated_data.pop('service_quantities', [])
        product_quantities = validated_data.pop('product_quantities', [])

        if products:
            # Lock and re-fetch inventory inside the transaction so simultaneous
            # checkouts cannot oversell the same stock.
            requested_product_ids = [product.id for product in products]
            locked_products = list(
                Product.objects.select_for_update().filter(
                    pk__in=requested_product_ids,
                    is_active=True,
                    shop__status="active",
                    shop__is_verified=True,
                )
            )
            if len(locked_products) != len(set(requested_product_ids)):
                raise serializers.ValidationError({"products": "One or more products are no longer available."})
            products_by_id = {product.id: product for product in locked_products}
            products = [products_by_id[product_id] for product_id in requested_product_ids]
        
        # If no services in list but single service provided, use that
        if not services and 'service' in validated_data and order_type != "manual":
            services = [validated_data['service']]
        
        # Track created_by for manual orders
        if created_by_user:
            validated_data['created_by'] = created_by_user
        
        # For manual orders, set status to pending_assignment (don't auto-assign rider)
        if order_type == "manual":
            validated_data['status'] = 'pending_assignment'
        else:
            # Online orders stay out of staff queues until payment succeeds.
            validated_data['status'] = 'pending_payment'
        
        # Online catalog orders are paid before fulfillment. Calculate the
        # amount from stored prices instead of trusting the client payload.
        free_delivery_offer = None
        if order_type == "online" and (services or products):
            quantities_by_service = {
                item.get("service_id"): item.get("quantity", 1)
                for item in service_quantities
            }
            service_total = sum(
                service.price * quantities_by_service.get(service.id, 1)
                for service in services
            )
            quantities_by_product = {
                item.get("product_id"): item.get("quantity", 1)
                for item in product_quantities
            }
            product_total = sum(
                product.price * quantities_by_product.get(product.id, 1)
                for product in products
            )
            free_delivery_offer = UserOffer.objects.filter(
                user=validated_data["user"],
                offer__benefit_type="free_delivery",
                is_used=False,
            ).select_related("offer").order_by("claimed_at").first()
            delivery_cost = Decimal("0.00") if free_delivery_offer else Decimal("50.00")
            order_total = service_total + product_total + delivery_cost
            validated_data["price"] = order_total
            if free_delivery_offer:
                validated_data["applied_offer"] = free_delivery_offer.offer
                validated_data["free_delivery"] = True

        # Create the order
        order = Order.objects.create(**validated_data)

        if free_delivery_offer:
            free_delivery_offer.is_used = True
            free_delivery_offer.used_at = timezone.now()
            free_delivery_offer.save(update_fields=["is_used", "used_at"])
        
        # Add services to the order (skip for manual orders)
        if services:
            order.services.set(services)
            # Set the primary service to the first one for backward compatibility
            if not order.service:
                order.service = services[0]
                order.save(update_fields=['service'])
            
            # Create OrderItem entries with quantities
            if service_quantities:
                for sq in service_quantities:
                    service_id = sq.get('service_id')
                    quantity = sq.get('quantity', 1)
                    if service_id and quantity > 0:
                        try:
                            service = Service.objects.get(pk=service_id)
                            OrderItem.objects.update_or_create(
                                order=order,
                                service=service,
                                defaults={'quantity': quantity}
                            )
                        except Service.DoesNotExist:
                            pass
            else:
                # If no quantities are provided, default each service to one.
                for service in services:
                    OrderItem.objects.update_or_create(
                        order=order,
                        service=service,
                        defaults={'quantity': 1}
                    )

        if products:
            from .models import ProductOrderItem

            quantities_by_product = {
                item.get("product_id"): item.get("quantity", 1)
                for item in product_quantities
            }
            for product in products:
                ProductOrderItem.objects.update_or_create(
                    order=order,
                    product=product,
                    defaults={
                        'product_name': product.name,
                        'unit_price': product.price,
                        'quantity': quantities_by_product.get(product.id, 1),
                    },
                )
                product.stock_quantity -= quantities_by_product.get(product.id, 1)
                product.save(update_fields=['stock_quantity'])
        
        if not order.code:
            order.save(update_fields=["code"])
        return order

    def get_total_price(self, obj):
        """Calculate the catalog subtotal across services and products."""
        service_total = sum(service.price for service in obj.services.all())
        product_total = sum(
            item.unit_price * item.quantity
            for item in obj.product_order_items.all()
        )
        total = service_total + product_total
        return float(total) if total else 0


class OrderItemSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source='service.name', read_only=True)
    service_price = serializers.SerializerMethodField()
    
    class Meta:
        model = OrderItem
        fields = ['id', 'service', 'service_name', 'service_price', 'quantity']
        read_only_fields = ['id']
    
    def get_service_price(self, obj):
        return float(obj.service.price) if obj.service.price else 0


class OrderListSerializer(serializers.ModelSerializer):
    package = serializers.SerializerMethodField()
    price_display = serializers.SerializerMethodField()
    total_price = serializers.SerializerMethodField()
    actual_price = serializers.SerializerMethodField()
    status = serializers.CharField()
    service_name = serializers.CharField(source="service.name", read_only=True, allow_null=True)
    services_list = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()
    service_location = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()
    delivery_code = serializers.SerializerMethodField()
    review_rating = serializers.SerializerMethodField()
    pickup_rider = serializers.SerializerMethodField()
    delivery_rider = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    pickup_location = serializers.SerializerMethodField()
    dropoff_location = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    order_items = serializers.SerializerMethodField()
    is_paid = serializers.SerializerMethodField()
    paid_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    washer_name = serializers.SerializerMethodField()
    folder_name = serializers.SerializerMethodField()
    fumigator_name = serializers.SerializerMethodField()
    rider_name = serializers.SerializerMethodField()
    staff_input_details = serializers.SerializerMethodField()
    
    def get_is_paid(self, obj):
        """Return true only when the full payable amount has been received."""
        _total, _paid, remaining = self.get_order_payment_progress(obj)
        return remaining is not None and remaining <= Decimal('0.01')

    def get_order_payment_progress(self, obj):
        if not hasattr(self, '_payment_progress_cache'):
            self._payment_progress_cache = {}
        if obj.pk not in self._payment_progress_cache:
            from payments.views import get_order_payment_progress
            self._payment_progress_cache[obj.pk] = get_order_payment_progress(obj)
        return self._payment_progress_cache[obj.pk]

    def get_delivery_code(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if (
            not user
            or not user.is_authenticated
            or obj.user_id != user.id
            or obj.status in ['delivered', 'cancelled']
        ):
            return None
        return obj.delivery_code

    def get_review_rating(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None

        is_customer = obj.user_id == user.id
        is_assigned_rider = (
            obj.status == 'delivered'
            and user.id in (obj.rider_id, obj.pickup_rider_id, obj.delivery_rider_id)
        )
        if not is_customer and not is_assigned_rider:
            return None
        try:
            review = obj.review
        except OrderReview.DoesNotExist:
            return None
        return review.rating if review else None

    def get_paid_amount(self, obj):
        _total, paid, _remaining = self.get_order_payment_progress(obj)
        return paid

    def get_remaining_amount(self, obj):
        _total, _paid, remaining = self.get_order_payment_progress(obj)
        return remaining
    
    def get_user(self, obj):
        if not obj.user:
            return None
        return {
            'username': obj.user.username,
            'phone': obj.user.phone,
            'location': obj.user.location,
            'first_name': obj.user.first_name,
            'last_name': obj.user.last_name,
        }

    def get_service_location(self, obj):
        if not obj.service_location:
            return None
        return {
            'id': obj.service_location.id,
            'name': obj.service_location.name,
        }

    def get_rider(self, obj):
        """Return rider info (backward compatibility - checks both pickup and delivery)"""
        # Prefer delivery_rider if available, otherwise return pickup_rider
        rider = obj.delivery_rider or obj.pickup_rider or obj.rider
        if not rider:
            return None
        return {
            'username': rider.username,
            'first_name': rider.first_name,
            'last_name': rider.last_name,
            'phone': rider.phone,
            'service_location': {
                'name': rider.service_location.name if rider.service_location else None
            }
        }

    def get_pickup_rider(self, obj):
        """Return pickup rider info"""
        if not obj.pickup_rider:
            return None
        return {
            'id': obj.pickup_rider.id,
            'username': obj.pickup_rider.username,
            'first_name': obj.pickup_rider.first_name,
            'last_name': obj.pickup_rider.last_name,
            'service_location': {
                'name': obj.pickup_rider.service_location.name if obj.pickup_rider.service_location else None
            }
        }

    def get_delivery_rider(self, obj):
        """Return delivery rider info"""
        if not obj.delivery_rider:
            return None
        return {
            'id': obj.delivery_rider.id,
            'username': obj.delivery_rider.username,
            'first_name': obj.delivery_rider.first_name,
            'last_name': obj.delivery_rider.last_name,
            'service_location': {
                'name': obj.delivery_rider.service_location.name if obj.delivery_rider.service_location else None
            }
        }

    def get_created_by(self, obj):
        """Return staff member who created this manual order"""
        if not obj.created_by:
            return None
        return {
            'username': obj.created_by.username,
            'first_name': obj.created_by.first_name,
            'last_name': obj.created_by.last_name,
            'service_location': {
                'name': obj.created_by.service_location.name if obj.created_by.service_location else None
            }
        }

    def get_services_list(self, obj):
        """Return list of services with their details"""
        services = obj.services.all().values('id', 'name', 'price')
        return list(services)

    def get_order_items(self, obj):
        """Return order items with quantities"""
        items = obj.order_items.all()
        serialized_items = list(OrderItemSerializer(items, many=True).data)
        serialized_items.extend({
            'id': item.id,
            'service': None,
            'service_name': item.product_name,
            'service_price': float(item.unit_price),
            'quantity': item.quantity,
            'item_type': 'product',
            'product': item.product_id,
        } for item in obj.product_order_items.all())
        return serialized_items

    def get_timeline(self, obj):
        """Return the order events/timeline for admin users or an empty list for others.

        The context must provide the request; if the request user is staff or superuser, return full timeline.
        Otherwise return a limited timeline (e.g. status changes only) or empty list.
        """
        request = self.context.get('request')
        # If no request context, be conservative and return the stored timeline if present
        if not request:
            events = getattr(obj, 'events', []).all() if hasattr(obj, 'events') else []
        else:
            user = getattr(request, 'user', None)
            # Admins (staff or superusers) get full timeline
            if user and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
                events = obj.events.all()
            else:
                # For non-admins, expose only generic status entries if any
                events = obj.events.filter(event_type__icontains='status')

        out = []
        for ev in events:
            out.append({
                'id': ev.id,
                'event_type': ev.event_type,
                'actor': ev.actor.username if ev.actor else None,
                'data': ev.data,
                'created_at': ev.created_at.isoformat() if ev.created_at else None,
            })
        return out

    def get_total_price(self, obj):
        """Calculate the catalog subtotal across services and products."""
        service_total = sum(service.price for service in obj.services.all())
        product_total = sum(
            item.unit_price * item.quantity
            for item in obj.product_order_items.all()
        )
        total = service_total + product_total
        return float(total) if total else None

    def get_package(self, obj):
        if obj.service:
            return obj.service.name
        first_product_item = obj.product_order_items.first()
        return first_product_item.product_name if first_product_item else f"Package {obj.package}"

    def get_price_display(self, obj):
        # Use total price from services if available
        total = sum(service.price for service in obj.services.all())
        if total:
            try:
                p = Decimal(str(total))
                if p == p.to_integral():
                    return f"KSh {int(p):,}"
                return f"KSh {p:,}"
            except Exception:
                return str(total)
        elif obj.price is not None:
            try:
                p = Decimal(obj.price)
                if p == p.to_integral():
                    return f"KSh {int(p):,}"
                return f"KSh {p:,}"
            except Exception:
                return str(obj.price)
        return ""

    def get_actual_price(self, obj):
        """Convert Decimal actual_price to float for JSON serialization"""
        if obj.actual_price is not None:
            try:
                return float(obj.actual_price)
            except (ValueError, TypeError):
                return None
        return None

    def get_pickup_location(self, obj):
        """Return pickup location coordinates if available from service_location"""
        if obj.service_location and hasattr(obj.service_location, 'latitude') and hasattr(obj.service_location, 'longitude'):
            return {
                'lat': float(obj.service_location.latitude) if obj.service_location.latitude else None,
                'lng': float(obj.service_location.longitude) if obj.service_location.longitude else None
            }
        return None

    def get_dropoff_location(self, obj):
        """Return dropoff location - could be user's location or another location"""
        if obj.user and hasattr(obj.user, 'latitude') and hasattr(obj.user, 'longitude'):
            return {
                'lat': float(obj.user.latitude) if obj.user.latitude else None,
                'lng': float(obj.user.longitude) if obj.user.longitude else None
            }
        return None

    def get_washer_name(self, obj):
        """Return washer's full name"""
        if obj.washer:
            return f"{obj.washer.first_name} {obj.washer.last_name}".strip() or obj.washer.username
        return None

    def get_folder_name(self, obj):
        """Return folder's full name"""
        if obj.folder:
            return f"{obj.folder.first_name} {obj.folder.last_name}".strip() or obj.folder.username
        return None

    def get_fumigator_name(self, obj):
        """Return fumigator's full name"""
        if hasattr(obj, 'fumigator') and obj.fumigator:
            return f"{obj.fumigator.first_name} {obj.fumigator.last_name}".strip() or obj.fumigator.username
        return None

    def get_rider_name(self, obj):
        """Return rider's full name"""
        if obj.rider:
            return f"{obj.rider.first_name} {obj.rider.last_name}".strip() or obj.rider.username
        return None

    def get_staff_input_details(self, obj):
        """Return all staff input details for this order"""
        details = []
        
        # Washer details
        if (obj.washer_items is not None or obj.washer_weight or obj.washer_notes or obj.washer_price):
            details.append({
                'staff_member': self.get_washer_name(obj) or 'Washer',
                'staff_role': 'Washer',
                'items': obj.washer_items,
                'weight_kg': float(obj.washer_weight) if obj.washer_weight else None,
                'description': obj.washer_notes,
                'actual_price': float(obj.washer_price) if obj.washer_price else None,
                'status_recorded': 'washed',
                'recorded_at': obj.washed_at.isoformat() if obj.washed_at else None,
            })
        
        # Folder details
        if (obj.folder_items is not None or obj.folder_weight or obj.folder_notes or obj.folder_price):
            details.append({
                'staff_member': self.get_folder_name(obj) or 'Folder',
                'staff_role': 'Folder',
                'items': obj.folder_items,
                'weight_kg': float(obj.folder_weight) if obj.folder_weight else None,
                'description': obj.folder_notes,
                'actual_price': float(obj.folder_price) if obj.folder_price else None,
                'status_recorded': 'ready',
                'recorded_at': obj.folded_at.isoformat() if obj.folded_at else None,
            })
        
        # Fumigator details
        if (hasattr(obj, 'fumigator_items') and (obj.fumigator_items is not None or obj.fumigator_weight or obj.fumigator_notes or obj.fumigator_price)):
            details.append({
                'staff_member': self.get_fumigator_name(obj) or 'Fumigator',
                'staff_role': 'Fumigator',
                'items': getattr(obj, 'fumigator_items', None),
                'weight_kg': float(obj.fumigator_weight) if getattr(obj, 'fumigator_weight', None) else None,
                'description': getattr(obj, 'fumigator_notes', None),
                'actual_price': float(obj.fumigator_price) if getattr(obj, 'fumigator_price', None) else None,
                'status_recorded': 'fumigated',
                'recorded_at': obj.fumigated_at.isoformat() if getattr(obj, 'fumigated_at', None) else None,
            })
        
        # Rider details
        if (obj.rider_items is not None or obj.rider_weight or obj.rider_notes or obj.rider_price):
            details.append({
                'staff_member': self.get_rider_name(obj) or 'Rider',
                'staff_role': 'Rider',
                'items': obj.rider_items,
                'weight_kg': float(obj.rider_weight) if obj.rider_weight else None,
                'description': obj.rider_notes or obj.description,
                'actual_price': float(obj.rider_price) if obj.rider_price else None,
                'status_recorded': 'delivered',
                'recorded_at': obj.delivered_at.isoformat() if obj.delivered_at else None,
            })
        
        return details

    class Meta:
        model = Order
        fields = [
            "id",
            "code",
            "created_at",
            "user",
            "service_name",
            "services_list",
            "service_location",
            "pickup_address",
            "dropoff_address",
            "requested_pickup_at",
            "pickup_location",
            "dropoff_location",
            "urgency",
            "items",
            "weight_kg",
            "quantity",
            "description",
            "package",
            "price",
            "actual_price",
            "total_price",
            "price_display",
            "status",
            "estimated_delivery",
            "delivered_at",
            "gate_notified_at",
            "pickup_confirmed_at",
            "picked_at",
            "ready_at",
            "rider",
            "delivery_code",
            "review_rating",
            "pickup_rider",
            "delivery_rider",
            "created_by",
            "timeline",
            "order_items",
            "is_paid",
            "paid_amount",
            "remaining_amount",
            "payment_method",
            "applied_offer",
            "offer_discount",
            "free_delivery",
            # Manual order fields
            "order_type",
            "drop_off_type",
            "customer_name",
            "customer_phone",
            # Staff detail tracking fields
            "washer_name",
            "folder_name",
            "fumigator_name",
            "rider_name",
            "staff_input_details",
            "washer_items",
            "washer_weight",
            "washer_notes",
            "washer_price",
            "folder_items",
            "folder_weight",
            "folder_notes",
            "folder_price",
            "fumigator_items",
            "fumigator_weight",
            "fumigator_notes",
            "fumigator_price",
            "rider_items",
            "rider_weight",
            "rider_notes",
            "rider_price",
            "washed_at",
            "folded_at",
            "fumigated_at",
        ]
