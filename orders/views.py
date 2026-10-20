# orders/views.py
from django.db import models, transaction
from django.db.models import Prefetch
from django.utils import timezone
from decimal import Decimal
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.db.models import Avg, Count, Q, Sum
from rest_framework.views import APIView
from .models import Order, OrderReview
from .serializers import OrderListSerializer, OrderCreateSerializer, OrderReviewSerializer
from users.permissions import LocationBasedPermission
from users.models import Location, User
from offers.models import UserOffer


class AdminDashboardSummaryView(APIView):
    """Return dashboard totals without loading paginated order records."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        if not (user.is_superuser or getattr(user, 'role', None) == 'admin'):
            return Response(
                {'detail': 'Only administrators can view dashboard statistics.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        order_stats = Order.objects.aggregate(
            total_orders=Count('pk'),
            completed=Count('pk', filter=Q(status__iexact='delivered')),
            total_revenue=Sum('price'),
        )
        active_riders = User.objects.filter(
            Q(assigned_orders__isnull=False)
            | Q(pickup_orders__isnull=False)
            | Q(delivery_orders__isnull=False)
        ).distinct().count()

        total_orders = order_stats['total_orders']
        completed = order_stats['completed']
        return Response({
            'total_orders': total_orders,
            'in_progress': total_orders - completed,
            'completed': completed,
            'revenue': float(order_stats['total_revenue'] or 0),
            'active_riders': active_riders,
            'total_users': User.objects.count(),
        })


class RiderRatingSummaryView(APIView):
    """Return the authenticated rider's rating across their delivered orders."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rider = request.user
        if getattr(rider, 'role', None) != 'rider':
            return Response(
                {'detail': 'Only riders can view rider rating statistics.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        assigned_orders = Order.objects.filter(
            Q(rider=rider) | Q(pickup_rider=rider) | Q(delivery_rider=rider),
            status='delivered',
        )
        review_stats = OrderReview.objects.filter(order__in=assigned_orders).aggregate(
            average_rating=Avg('rating'),
            rating_count=Count('pk'),
        )

        return Response({
            'average_rating': (
                round(review_stats['average_rating'], 1)
                if review_stats['average_rating'] is not None
                else None
            ),
            'rating_count': review_stats['rating_count'],
            'delivered_order_count': assigned_orders.count(),
        })


def send_rider_assignment_sms(order, rider):
    """Notify an assigned rider by SMS without blocking the order update."""
    rider_phone = getattr(rider, 'phone', None)
    if not rider_phone or not str(rider_phone).strip():
        print(f"⚠ No phone number found for rider {rider.username}")
        return

    try:
        from django.utils import timezone
        from services.sms_service import AfricasTalkingSMSService

        services = ', '.join([service.name for service in order.services.all()]) if order.services.exists() else 'N/A'
        user_name = order.user.get_full_name() or order.user.username if order.user else order.customer_name or 'Customer'
        user_phone = (order.user.phone if order.user and order.user.phone else None) or order.customer_phone or None
        clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
        if order.estimated_delivery:
            hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
            estimated_delivery = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
        else:
            estimated_delivery = 'TBD'

        message = (
            f"CACHE INDUSTRIES\n"
            f"New Order Assigned!\n"
            f"Order #: {order.code}\n"
            f"Customer: {user_name}\n"
            f"Phone: {user_phone or 'N/A'}\n"
            f"Pickup: {clean_pickup}\n"
            f"Dropoff: {order.dropoff_address}\n"
            f"Products: {services}\n"
            f"Items: {order.items}\n"
            f"Price: KES {order.price or 'TBD'}\n"
            f"Est. Delivery: {estimated_delivery}\n"
            f"Accept: https://www.cache.co.ke/rider/order/{order.code}"
        )
        result = AfricasTalkingSMSService().send_sms(rider_phone, message)

        if result and result.get('status') == 'success':
            print(f"✓ Rider SMS sent to {rider.username} ({rider_phone}) for order {order.code}")
        else:
            error_msg = result.get('message', 'Unknown error') if result else 'No response'
            print(f"⚠ Failed to send rider SMS to {rider.username}: {error_msg}")
    except Exception as sms_error:
        print(f"⚠ Error sending rider SMS: {str(sms_error)}")


def send_customer_delivery_code_sms(order, delivery_code):
    """Send the 4-digit customer validation code during the delivery phase."""
    if not order.user or not getattr(order.user, 'phone', None):
        return {'status': 'skipped', 'message': 'No customer phone to send code to.'}

    try:
        from services.sms_service import AfricasTalkingSMSService
        message = (
            f"CACHE INDUSTRIES\n"
            f"Delivery Code\n"
            f"Order #: {order.code}\n"
            f"Your delivery code is {delivery_code}. Please give this to the rider when they arrive."
        )
        result = AfricasTalkingSMSService().send_sms(order.user.phone, message)
        if result and result.get('status') == 'success':
            print(f"✓ Delivery code SMS sent to customer for order {order.code}")
            return result
        return result if result else {'status': 'error', 'message': 'No response from SMS service'}
    except Exception as sms_error:
        print(f"⚠ Error sending delivery code SMS: {str(sms_error)}")
        return {'status': 'error', 'message': str(sms_error)}


def send_gate_arrival_sms(order):
    """Notify the customer that the rider is at the gate and remind them to share the delivery code."""
    if not order.user or not getattr(order.user, 'phone', None):
        return {'status': 'skipped', 'message': 'No customer phone to notify.'}

    try:
        from services.sms_service import AfricasTalkingSMSService
        message = (
            f"CACHE INDUSTRIES\n"
            f"Your rider has reached the gate for order {order.code}.\n"
            f"Please share the delivery code you received from us with the rider to confirm delivery."
        )
        result = AfricasTalkingSMSService().send_sms(order.user.phone, message)
        if result and result.get('status') == 'success':
            order.gate_notified_at = timezone.now()
            order.save(update_fields=['gate_notified_at', 'updated_at'])
            print(f"✓ Gate SMS sent to customer for order {order.code}")
            return result
        return result if result else {'status': 'error', 'message': 'No response from SMS service'}
    except Exception as sms_error:
        print(f"⚠ Error sending gate SMS: {str(sms_error)}")
        return {'status': 'error', 'message': str(sms_error)}


class ApplyOfferView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, code):
        try:
            order = Order.objects.get(code=code, user=request.user)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        if order.status != 'pending_payment':
            return Response({'detail': 'Offers can only be applied before payment.'}, status=status.HTTP_400_BAD_REQUEST)

        requested_offer_id = request.data.get('offer_id')
        if order.applied_offer_id and str(order.applied_offer_id) == str(requested_offer_id):
            offer = order.applied_offer
            original_total = Decimal(str(order.price or order.actual_price or 0))
            return Response({
                'offer': {'id': offer.id, 'title': offer.title, 'benefit_type': offer.benefit_type},
                'original_amount': original_total,
                'discount': order.offer_discount,
                'payable_amount': order.actual_price,
                'free_delivery': order.free_delivery,
            })

        try:
            user_offer = UserOffer.objects.select_related('offer').get(
                user=request.user,
                offer_id=requested_offer_id,
                is_used=False,
            )
        except UserOffer.DoesNotExist:
            return Response({'detail': 'This offer is not available to you.'}, status=status.HTTP_400_BAD_REQUEST)

        offer = user_offer.offer
        if order.applied_offer_id and order.applied_offer_id != offer.id:
            return Response({'detail': 'Another offer is already applied to this order.'}, status=status.HTTP_400_BAD_REQUEST)

        original_total = Decimal(str(order.price or order.actual_price or 0))
        discount = Decimal('0')
        if offer.benefit_type == 'cash_discount':
            discount = (
                original_total * Decimal(offer.discount_percent) / Decimal('100')
                if offer.discount_percent > 0
                else Decimal(str(offer.discount_amount))
            )
            discount = min(discount, original_total)

        order.applied_offer = offer
        order.offer_discount = discount
        order.free_delivery = offer.benefit_type == 'free_delivery'
        order.actual_price = original_total - discount
        order.save(update_fields=['applied_offer', 'offer_discount', 'free_delivery', 'actual_price', 'updated_at'])
        user_offer.is_used = True
        user_offer.used_at = timezone.now()
        user_offer.save(update_fields=['is_used', 'used_at'])

        return Response({
            'offer': {'id': offer.id, 'title': offer.title, 'benefit_type': offer.benefit_type},
            'original_amount': original_total,
            'discount': discount,
            'payable_amount': order.actual_price,
            'free_delivery': order.free_delivery,
        })
class StaffCreateOrderView(APIView):
    """
    POST -> Create a manual order for a customer (by staff)
    Only accessible by staff members
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        # Check if user is staff
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {'error': 'Only staff members can create manual orders'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Use the OrderCreateSerializer to handle order creation
        serializer = OrderCreateSerializer(
            data=request.data,
            context={'request': request}
        )

        if serializer.is_valid():
            try:
                order = serializer.save()
                
                # Create in-app notifications for admin and customer
                try:
                    from notifications.models import Notification
                    from django.contrib.auth import get_user_model
                    User = get_user_model()
                    
                    # 1. Notify customer (user who dropped off items)
                    if order.user:
                        Notification.objects.create(
                            user=order.user,
                            order=order,
                            message=f"Your order {order.code} has been created by staff.",
                            notification_type='new_order'
                        )
                    
                    # 2. Notify all admins
                    admin_users = User.objects.filter(is_superuser=True, is_active=True)
                    services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
                    
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            order=order,
                            message=f"Manual order {order.code} created by {request.user.username}",
                            notification_type='new_order'
                        )
                
                except Exception as notif_error:
                    print(f"⚠ Error creating notifications: {str(notif_error)}")
                
                # Send SMS to admin and customer (no rider assigned yet for manual orders)
                try:
                    from django.conf import settings
                    from services.sms_service import AfricasTalkingSMSService
                    
                    admin_phone = settings.ADMIN_PHONE_NUMBER
                    
                    # Format services list
                    services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
                    user_name = order.user.get_full_name() or order.user.username if order.user else order.customer_name or 'Customer'
                    # Get customer phone from user.phone OR customer_phone field (for walk-in orders)
                    user_phone = (order.user.phone if order.user and order.user.phone else None) or \
                                 (order.customer_phone if order.customer_phone else None) or None
                    
                    # 1. Send SMS to CUSTOMER (user who dropped off items)
                    if user_phone and str(user_phone).strip():
                        try:
                            from services.sms_service import format_phone_number
                            from django.utils import timezone
                            sms_service = AfricasTalkingSMSService()
                            # Format phone number to international format
                            formatted_phone = format_phone_number(user_phone)
                            order_url = f"https://www.cache.co.ke/orders/{order.code}"
                            
                            # Extract clean pickup address and calculate hours for estimated delivery
                            clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                            if order.estimated_delivery:
                                hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
                                est_time = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
                            else:
                                est_time = 'TBD'
                            
                            customer_message = (
                                f"CACHE INDUSTRIES\n"
                                f"Order Created!\n"
                                f"Order #: {order.code}\n"
                                f"Services: {services}\n"
                                f"Pickup: {clean_pickup}\n"
                                f"Price: KES {order.price or 'TBD'}\n"
                                f"Est. Delivery: {est_time}\n"
                                f"View: {order_url}\n"
                                f"We'll update you when it's ready!"
                            )
                            
                            result = sms_service.send_sms(formatted_phone, customer_message)
                            
                            if result and result.get('status') == 'success':
                                print(f"✓ Customer SMS sent to {formatted_phone} for order {order.code}")
                            else:
                                error_msg = result.get('message', 'Unknown error') if result else 'No response'
                                print(f"⚠ Failed to send customer SMS: {error_msg}")
                        
                        except Exception as sms_error:
                            print(f"⚠ Error sending customer SMS: {str(sms_error)}")
                            import traceback
                            traceback.print_exc()
                    
                    # 2. Send SMS to ADMIN
                    if admin_phone:
                        try:
                            from django.utils import timezone
                            sms_service = AfricasTalkingSMSService()
                            order_url = f"https://www.cache.co.ke/orders/{order.code}"
                            
                            # Extract clean pickup address and calculate hours for estimated delivery
                            clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                            if order.estimated_delivery:
                                hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
                                est_time = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
                            else:
                                est_time = 'TBD'
                            
                            admin_message = (
                                f"MANUAL ORDER CREATED!\n"
                                f"Order #: {order.code}\n"
                                f"Customer: {user_name}\n"
                                f"Phone: {user_phone or 'N/A'}\n"
                                f"Pickup: {clean_pickup}\n"
                                f"Dropoff: {order.dropoff_address}\n"
                                f"Services: {services}\n"
                                f"Items: {order.items}\n"
                                f"Price: KES {order.price or 'TBD'}\n"
                                f"Est. Delivery: {est_time}\n"
                                f"Created By: {request.user.username}\n"
                                f"Status: {order.get_actual_status_display()}\n"
                                f"Manage: {order_url}"
                            )
                            
                            result = sms_service.send_sms(admin_phone, admin_message)
                            
                            if result and result.get('status') == 'success':
                                print(f"✓ Admin SMS sent to {admin_phone} for order {order.code}")
                            else:
                                error_msg = result.get('message', 'Unknown error') if result else 'No response'
                                print(f"⚠ Failed to send admin SMS: {error_msg}")
                        
                        except Exception as sms_error:
                            print(f"⚠ Error sending admin SMS: {str(sms_error)}")
                            import traceback
                            traceback.print_exc()
                
                except Exception as e:
                    print(f"⚠ Error in SMS notification block: {str(e)}")
                    import traceback
                    traceback.print_exc()
                
                return Response(
                    OrderListSerializer(order, context={'request': request}).data,
                    status=status.HTTP_201_CREATED
                )
            except Exception as e:
                return Response(
                    {'error': f'Failed to create order: {str(e)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )


class RequestedOrdersListView(generics.ListAPIView):
    """
    GET -> List all unassigned orders with status 'requested'
    Only for admin/staff to see pending orders
    """
    serializer_class = OrderListSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """
        Get all unassigned requested orders
        """
        return Order.objects.filter(
            status='requested',
            rider__isnull=True
        ).select_related('user', 'service', 'service_location').prefetch_related('services').order_by('-created_at')

class RiderOrderListView(generics.ListAPIView):
    """
    GET -> List orders assigned to the authenticated rider
    For riders: Show delivery orders through the pickup and delivery lifecycle
    Excludes orders with: Cleaning, fumigation, cctv installation, shower installation
    """
    serializer_class = OrderListSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """
        Get orders based on user's staff type
        """
        user = self.request.user
        excluded_services = ['Cleaning', 'fumigation', 'cctv installation', 'shower installation']
        
        # Riders own the delivery lifecycle; legacy staff roles no longer add
        # washing/folding states to the order workflow.
        # Check both old 'rider' field and new 'pickup_rider'/'delivery_rider' fields
        from django.db.models import Q
        queryset = Order.objects.filter(
            Q(rider=user) |  # Old field for backward compatibility
            Q(pickup_rider=user) |  # New pickup rider field
            Q(delivery_rider=user),  # New delivery rider field
            status__in=['requested', 'pending_assignment', 'assigned_pickup', 'accepted_delivery', 'picked', 'at_gate', 'delivered']
        ).exclude(
            services__name__in=excluded_services
        ).distinct().select_related('user', 'service', 'rider', 'pickup_rider', 'delivery_rider', 'service_location').prefetch_related('services').order_by('-created_at')

        print(f"\n[DEBUG RiderOrders] Rider {user.username} (ID: {user.id}) querying orders")
        print(f"[DEBUG] Total orders assigned to this rider: {queryset.count()}")
        print(f"[DEBUG] Checking rider={user}, pickup_rider={user}, delivery_rider={user}")
        
        return queryset

    def post(self, request, *args, **kwargs):
        """Accept an order"""
        order_id = request.data.get('order_id')
        action = request.data.get('action', 'accept')  # 'accept' or 'reject'

        try:
            order = Order.objects.get(
                id=order_id,
                rider__isnull=True,
                status__iexact='requested'  # case-insensitive match
            )
            if action == 'accept':
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response(
                        {'error': 'The merchant is still preparing this order.'},
                        status=status.HTTP_409_CONFLICT,
                    )
                order.rider = request.user
                order.status = 'accepted_delivery'
                order.save()
                return Response({'message': 'Order accepted successfully', 'order': OrderListSerializer(order).data})
            else:
                return Response({'message': 'Order rejected'})
        except Order.DoesNotExist:
            return Response(
                {'error': 'Order not found or already assigned'},
                status=status.HTTP_404_NOT_FOUND
            )

class OrderUpdateView(APIView):
    """
    PATCH -> Update order status, quantity, weight, and description
    Sends notification to rider when status changes to 'ready' (ready for delivery)
    """
    permission_classes = [permissions.IsAuthenticated, LocationBasedPermission]

    def patch(self, request, *args, **kwargs):
        try:
            order_id = request.query_params.get('id')
            if not order_id:
                return Response({'error': 'Order ID is required'}, status=status.HTTP_400_BAD_REQUEST)

            order = Order.objects.get(id=order_id)

            action = request.data.get('action')
            if action == 'confirm_pickup':
                if order.delivery_rider_id != request.user.id and order.pickup_rider_id != request.user.id and order.rider_id != request.user.id:
                    return Response({'error': 'This order is not assigned to you.'}, status=status.HTTP_403_FORBIDDEN)
                if order.status != 'accepted_delivery':
                    return Response({'error': 'Accept the delivery before confirming pickup.'}, status=status.HTTP_400_BAD_REQUEST)
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response({'error': 'The merchant is still preparing this order.'}, status=status.HTTP_409_CONFLICT)
                order.pickup_confirmed_at = timezone.now()
                order.status = 'picked'
                order.picked_at = order.picked_at or timezone.now()
                order.save(update_fields=['pickup_confirmed_at', 'picked_at', 'status', 'updated_at'])
                return Response({'message': 'Pickup confirmed successfully.'})

            if action == 'notify_gate':
                if order.delivery_rider_id != request.user.id and order.pickup_rider_id != request.user.id and order.rider_id != request.user.id:
                    return Response({'error': 'This delivery is not assigned to you.'}, status=status.HTTP_403_FORBIDDEN)
                if not order.pickup_confirmed_at:
                    return Response({'error': 'Confirm pickup and check all products before notifying the customer.'}, status=status.HTTP_400_BAD_REQUEST)

                if order.status != 'picked':
                    return Response({'error': 'The order must be marked as picked up before notifying the customer.'}, status=status.HTTP_400_BAD_REQUEST)

                if order.gate_notified_at:
                    return Response({
                        'message': 'The customer was already notified that the rider is at the gate.',
                        'already_notified': True,
                    })

                if not order.delivery_code:
                    delivery_code = order.generate_delivery_code()
                    send_customer_delivery_code_sms(order, delivery_code)
                order.status = 'at_gate'
                order.save(update_fields=['status', 'updated_at'])
                send_gate_arrival_sms(order)
                return Response({
                    'message': 'Customer has been notified that the rider is at the gate.',
                    'already_notified': False,
                })

            # A rider may accept only one delivery at a time.
            if request.data.get('status') == 'accepted_delivery':
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response({'error': 'The merchant is still preparing this order.'}, status=status.HTTP_409_CONFLICT)
                has_active_delivery = Order.objects.filter(
                    delivery_rider=request.user,
                    status='accepted_delivery',
                ).exclude(id=order.id).exists()
                if has_active_delivery:
                    return Response(
                        {'error': 'You already have an active delivery. Mark it as delivered before accepting another.'},
                        status=status.HTTP_409_CONFLICT,
                    )

                is_assigned_rider = (
                    order.delivery_rider_id == request.user.id or
                    order.pickup_rider_id == request.user.id or
                    order.rider_id == request.user.id
                )
                if not is_assigned_rider:
                    return Response(
                        {'error': 'This delivery is not assigned to you.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )

                order.delivery_rider = request.user
                if not order.delivery_code:
                    delivery_code = order.generate_delivery_code()
                    send_customer_delivery_code_sms(order, delivery_code)
            
            # Check if the staff member has permission for this location
            # Allow: superusers, or staff with matching service_location, or any staff with washer/folder role
            if request.user.is_staff and not request.user.is_superuser:
                has_staff_type = hasattr(request.user, 'staff_type') and request.user.staff_type in ['washer', 'folder']
                has_location_match = request.user.service_location and order.service_location == request.user.service_location
                
                if not has_staff_type and not has_location_match:
                    return Response({'error': 'You do not have permission to update this order'}, 
                                 status=status.HTTP_403_FORBIDDEN)

            # Determine incoming values and capture old values for audit BEFORE mutating
            new_status = request.data.get('status')
            old_status = order.status

            if new_status and new_status.lower() == 'picked':
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response({'error': 'The merchant is still preparing this order.'}, status=status.HTTP_409_CONFLICT)
            
            # Validate status transition
            if new_status and not order.can_transition_to(new_status):
                return Response(
                    {'error': f'Cannot transition from {old_status} to {new_status}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if new_status and new_status.lower() == 'delivered':
                if getattr(request.user, 'role', None) == 'rider' and request.user.id not in {
                    order.delivery_rider_id, order.rider_id
                }:
                    return Response(
                        {'error': 'Only the assigned delivery rider can complete this delivery.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                delivered_code = str(request.data.get('delivery_code') or '').strip()
                if not order.gate_notified_at:
                    return Response(
                        {'error': 'The customer must be notified at the gate before delivery can be completed.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not order.verify_delivery_code(delivered_code):
                    return Response(
                        {'error': 'A valid customer delivery code is required before marking this order as delivered.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            
            status_changed_to_picked = new_status and new_status.lower() == 'picked' and old_status.lower() != 'picked'
            status_changed_to_delivered = new_status and new_status.lower() == 'delivered' and old_status.lower() != 'delivered'
            # Legacy washer/folder notification branches remain below for old
            # records, but the delivery-only flow never enters them.
            status_changed_to_ready = False
            status_changed_to_washed = False

            # Capture old values before update
            old_values = {
                'status': old_status,
                'pickup_rider': order.pickup_rider,
                'delivery_rider': order.delivery_rider,
                'quantity': getattr(order, 'quantity', None),
                'weight_kg': getattr(order, 'weight_kg', None),
                'description': getattr(order, 'description', None),
                'actual_price': getattr(order, 'actual_price', None),
                'delivered_at': getattr(order, 'delivered_at', None),
            }

            print(f"\n[DEBUG OrderUpdate] Order {order.code}")
            print(f"[DEBUG] Old status: {old_status}, New status: {new_status}")
            print(f"[DEBUG] Status changed to picked: {status_changed_to_picked}")
            print(f"[DEBUG] Pickup rider: {order.pickup_rider.username if order.pickup_rider else 'None'}")
            print(f"[DEBUG] Delivery rider: {order.delivery_rider.username if order.delivery_rider else 'None'}")

            # Update status if provided
            if new_status:
                order.status = new_status
                
                # Set timestamps based on status
                if status_changed_to_picked:
                    order.picked_at = timezone.now()
                elif status_changed_to_delivered:
                    order.delivered_at = timezone.now()

            # Handle pickup rider assignment
            pickup_rider_id = request.data.get('pickup_rider')
            if pickup_rider_id is not None:
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response({'error': 'The merchant is still preparing this order.'}, status=status.HTTP_409_CONFLICT)
                try:
                    from users.models import User
                    rider = User.objects.get(id=pickup_rider_id, role='rider', is_active=True)
                    order.pickup_rider = rider
                    # Also update old rider field for backward compatibility
                    order.rider = rider
                    # If status is pending_assignment or requested, move to assigned_pickup
                    if order.status in ['pending_assignment', 'requested']:
                        order.status = 'assigned_pickup'
                    print(f"[DEBUG] Pickup rider assigned: {rider.username}")

                    # Send notification to the newly assigned rider
                    from notifications.models import Notification
                    message = f"Order {order.code} assigned for pickup. Pickup: {order.pickup_address[:50]}..."
                    Notification.objects.create(
                        user=rider,
                        order=order,
                        message=message,
                        notification_type='new_order'
                    )
                    print(f"✓ Notification sent to pickup rider {rider.username} for order {order.code}")
                    send_rider_assignment_sms(order, rider)

                except Exception as e:
                    print(f"⚠ Error assigning pickup rider: {str(e)}")

            # Handle delivery rider assignment
            delivery_rider_id = request.data.get('delivery_rider')
            if delivery_rider_id is not None:
                from marketplace.merchant_payouts import merchants_prepared_for_pickup
                if not merchants_prepared_for_pickup(order):
                    return Response({'error': 'The merchant is still preparing this order.'}, status=status.HTTP_409_CONFLICT)
                try:
                    from users.models import User
                    rider = User.objects.get(id=delivery_rider_id, role='rider', is_active=True)
                    order.delivery_rider = rider
                    if order.status in ['requested', 'pending_assignment']:
                        order.status = 'assigned_pickup'
                    print(f"[DEBUG] Delivery rider assigned: {rider.username}")

                    # Send notification to the newly assigned rider
                    from notifications.models import Notification
                    message = f"Order {order.code} assigned for delivery. Dropoff: {order.dropoff_address[:50]}..."
                    Notification.objects.create(
                        user=rider,
                        order=order,
                        message=message,
                        notification_type='new_order'
                    )
                    print(f"✓ Notification sent to delivery rider {rider.username} for order {order.code}")
                    send_rider_assignment_sms(order, rider)

                except Exception as e:
                    print(f"⚠ Error assigning delivery rider: {str(e)}")

            # Backward compatibility: if old 'rider' field is used and it's a pickup stage, assign to pickup_rider
            rider_id = request.data.get('rider')
            if rider_id is not None and not pickup_rider_id and not delivery_rider_id:
                try:
                    from users.models import User
                    rider = User.objects.get(id=rider_id, role='rider', is_active=True)
                    old_rider = order.rider
                    
                    # Determine if this is pickup or delivery based on current status
                    if order.status in ['requested', 'pending_assignment', 'assigned_pickup']:
                        order.pickup_rider = rider
                        order.rider = rider
                        if order.status in ['pending_assignment', 'requested']:
                            order.status = 'assigned_pickup'
                    elif order.status in ['accepted_delivery', 'picked', 'at_gate']:
                        order.delivery_rider = rider
                    
                    print(f"[DEBUG] Rider assigned (legacy): {rider.username} (was: {old_rider.username if old_rider else 'None'})")

                    # Send notification to the newly assigned rider
                    from notifications.models import Notification
                    message = f"Order {order.code} assigned to you."
                    Notification.objects.create(
                        user=rider,
                        order=order,
                        message=message,
                        notification_type='new_order'
                    )
                    print(f"✓ Notification sent to rider {rider.username} for order {order.code}")
                    send_rider_assignment_sms(order, rider)

                except Exception as e:
                    print(f"⚠ Error assigning rider: {str(e)}")

                except User.DoesNotExist:
                    return Response({'error': 'Invalid rider selected'}, status=status.HTTP_400_BAD_REQUEST)

            # Update rider-provided details
            quantity = request.data.get('quantity')
            if quantity is not None:
                order.quantity = quantity

            weight_kg = request.data.get('weight_kg')
            if weight_kg is not None:
                order.weight_kg = weight_kg

            description = request.data.get('description')
            if description is not None:
                order.description = description

            # Update role-specific fields (washer, folder, fumigator)
            staff_role = request.data.get('staff_role')
            
            # Map role names to field prefixes
            role_field_map = {
                'washer': 'washer',
                'folder': 'folder',
                'fumigator': 'fumigator',
                'staff': 'staff'
            }
            
            if staff_role and staff_role in role_field_map:
                prefix = role_field_map[staff_role]
                
                # Update role-specific items
                role_items_key = f'{prefix}_items'
                role_items = request.data.get(role_items_key)
                if role_items is not None:
                    try:
                        setattr(order, role_items_key, int(role_items))
                    except (ValueError, TypeError):
                        pass
                
                # Update role-specific weight
                role_weight_key = f'{prefix}_weight'
                role_weight = request.data.get(role_weight_key)
                if role_weight is not None:
                    try:
                        from decimal import Decimal
                        setattr(order, role_weight_key, Decimal(str(role_weight)))
                    except (ValueError, TypeError, Exception):
                        pass
                
                # Update role-specific notes
                role_notes_key = f'{prefix}_notes'
                role_notes = request.data.get(role_notes_key)
                if role_notes is not None:
                    setattr(order, role_notes_key, role_notes)
                
                # Update role-specific price
                role_price_key = f'{prefix}_price'
                role_price = request.data.get(role_price_key)
                if role_price is not None:
                    try:
                        from decimal import Decimal
                        setattr(order, role_price_key, Decimal(str(role_price)))
                    except (ValueError, TypeError, Exception):
                        pass
                
                # Track which role/staff member made the update
                if prefix == 'washer' and request.user:
                    order.washer = request.user
                    order.washed_at = timezone.now()
                elif prefix == 'folder' and request.user:
                    order.folder = request.user
                    order.folded_at = timezone.now()
                elif prefix == 'fumigator' and request.user:
                    order.fumigator = request.user
                    order.fumigated_at = timezone.now()

            with transaction.atomic():
                order.save()
                if status_changed_to_delivered:
                    from riders.payouts import credit_delivery_earning
                    delivery_rider = order.delivery_rider or order.rider
                    if delivery_rider:
                        credit_delivery_earning(order, delivery_rider)
                    from marketplace.merchant_payouts import credit_completed_order
                    credit_completed_order(order)

            # Accept delivered_at from request (rider marking delivery)
            delivered_at = request.data.get('delivered_at')
            if delivered_at is not None:
                try:
                    # Parse and set delivered_at; allow ISO strings
                    from django.utils.dateparse import parse_datetime
                    parsed = parse_datetime(str(delivered_at))
                    if parsed:
                        order.delivered_at = parsed
                        order.save(update_fields=['delivered_at'])
                except Exception:
                    # ignore parse errors
                    pass

            # Record events for changes
            from .models import OrderEvent
            actor = request.user if request.user.is_authenticated else None

            # status change
            if new_status and (old_status != new_status):
                OrderEvent.objects.create(
                    order=order,
                    actor=actor,
                    event_type='status_changed',
                    data={'old': old_status, 'new': new_status}
                )

            # rider change
            if rider_id is not None and old_values.get('rider') != order.rider:
                OrderEvent.objects.create(
                    order=order,
                    actor=actor,
                    event_type='rider_assigned',
                    data={'old': old_values.get('rider').username if old_values.get('rider') else None, 'new': order.rider.username}
                )

            # details changed (quantity, weight, description)
            changed_details = {}
            if quantity is not None and quantity != old_values.get('quantity'):
                changed_details['quantity'] = {'old': old_values.get('quantity'), 'new': quantity}
            if weight_kg is not None and weight_kg != old_values.get('weight_kg'):
                changed_details['weight_kg'] = {'old': old_values.get('weight_kg'), 'new': weight_kg}
            if description is not None and description != old_values.get('description'):
                changed_details['description'] = {'old': old_values.get('description'), 'new': description}
            # delivered_at changed (rider marking delivered)
            delivered_at_req = request.data.get('delivered_at')
            if delivered_at_req is not None:
                old_da = old_values.get('delivered_at')
                # compare as ISO string where possible
                if str(old_da) != str(delivered_at_req):
                    changed_details['delivered_at'] = {'old': old_da, 'new': delivered_at_req}

            if changed_details:
                OrderEvent.objects.create(
                    order=order,
                    actor=actor,
                    event_type='details_updated',
                    data=changed_details
                )
            print(f"[DEBUG] Order saved with status: {order.status}")

            # Handle status change to 'washed' - washer marks as washed
            if status_changed_to_washed:
                # Track who washed the order
                if request.user.is_staff or (hasattr(request.user, 'staff_type') and request.user.staff_type == 'washer'):
                    order.washer = request.user
                    order.washed_at = timezone.now()
                    order.save(update_fields=['washer', 'washed_at'])
                    print(f"✓ Order {order.code} marked as washed by {request.user.username}")
                
                # Create notification for folder staff at the same location
                try:
                    from notifications.models import Notification
                    from django.contrib.auth import get_user_model
                    User = get_user_model()
                    
                    # Notify all folder staff at this location
                    folder_staff = User.objects.filter(
                        service_location=order.service_location,
                        staff_type='folder',
                        is_active=True
                    )
                    
                    services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
                    
                    for folder in folder_staff:
                        Notification.objects.create(
                            user=folder,
                            order=order,
                            message=f"Order {order.code} ({services}) is ready for folding!",
                            notification_type='order_update'
                        )
                        print(f"✓ Notification sent to folder {folder.username} for order {order.code}")
                    
                    # Send SMS to folder staff
                    if folder_staff.exists():
                        try:
                            from services.sms_service import AfricasTalkingSMSService
                            
                            sms_service = AfricasTalkingSMSService()
                            customer_name = order.user.get_full_name() or order.user.username if order.user else 'Customer'
                            
                            sms_message = (
                                f"Order Ready for Folding!\n"
                                f"Order #: {order.code}\n"
                                f"Customer: {customer_name}\n"
                                f"Service: {services}\n"
                                f"Items: {order.quantity or order.items}\n"
                                f"Weight: {order.weight_kg}kg\n"
                                f"Please proceed with folding. Thank you!"
                            )
                            
                            for folder in folder_staff:
                                if hasattr(folder, 'phone') and folder.phone:
                                    sms_result = sms_service.send_sms(folder.phone, sms_message)
                                    if sms_result and sms_result.get('status') == 'success':
                                        print(f"✓ SMS sent to folder {folder.username}")
                        except Exception as e:
                            print(f"⚠ SMS service error notifying folder: {str(e)}")
                
                except Exception as e:
                    print(f"⚠ Error notifying folder staff: {str(e)}")
            
            # Handle status change to 'ready'
            if status_changed_to_ready:
                # Track who folded the order (for ready status set by folder)
                if hasattr(request.user, 'staff_type') and request.user.staff_type == 'folder':
                    order.folder = request.user
                    order.folded_at = timezone.now()
                    order.save(update_fields=['folder', 'folded_at'])
                    print(f"✓ Order {order.code} marked as ready by folder {request.user.username}")
                
                from notifications.models import Notification
                from django.contrib.auth import get_user_model
                User = get_user_model()
                
                assigned_rider = None
                
                # DISABLED: Auto-assignment of riders - now manual by admin
                # For manual orders created by staff, only auto-assign if delivery address was provided
                # if order.order_type == 'manual':
                #     print(f"[DEBUG] Order {order.code} is a manual order")
                #     print(f"[DEBUG] Order created by staff: {order.created_by.username if order.created_by else 'Unknown'}")
                #
                #     # Check if delivery address was provided (not the default "To be assigned")
                #     has_delivery_address = (
                #         order.dropoff_address and
                #         order.dropoff_address.lower() != 'to be assigned' and
                #         order.dropoff_address.strip() != ''
                #     )
                #
                #     if has_delivery_address:
                #         print(f"[DEBUG] Delivery address provided: {order.dropoff_address}")
                #         print(f"[DEBUG] Assigning to available rider...")
                #         # Only auto-assign if delivery address was provided
                #         if not order.rider and order.service_location:
                #             available_riders = User.objects.filter(
                #                 role='rider',
                #                 service_location=order.service_location,
                #                 is_active=True
                #             ).select_related('rider_profile').order_by('rider_profile__completed_jobs')
                #
                #             if available_riders.exists():
                #                 assigned_rider = available_riders.first()
                #                 order.rider = assigned_rider
                #                 # Update status from pending_assignment to requested if it was pending
                #                 if order.status == 'pending_assignment':
                #                     order.status = 'requested'
                #                 order.save(update_fields=['rider', 'status'])
                #                 print(f"✓ Order {order.code} assigned to rider {assigned_rider.username}")
                #             else:
                #                 print(f"⚠ No available riders in {order.service_location.name}")
                #     else:
                #         print(f"[DEBUG] No delivery address - order stays with staff creator")
                #
                # # If order doesn't have a rider assigned, assign it to an available rider in the same location
                # elif not order.rider and order.service_location:
                #     print(f"[DEBUG] No rider assigned yet, finding available rider...")
                #     available_riders = User.objects.filter(
                #         role='rider',
                #         service_location=order.service_location,
                #         is_active=True
                #     ).select_related('rider_profile').order_by('rider_profile__completed_jobs')
                #
                #     print(f"[DEBUG] Available riders in {order.service_location.name}: {available_riders.count()}")
                #
                #     if available_riders.exists():
                #         assigned_rider = available_riders.first()
                #         order.rider = assigned_rider
                #         # Update status from pending_assignment to requested if it was pending
                #         if order.status == 'pending_assignment':
                #             order.status = 'requested'
                #         order.save(update_fields=['rider', 'status'])
                #         print(f"✓ Order {order.code} assigned to rider {assigned_rider.username}")
                #     else:
                #         print(f"⚠ No available riders in {order.service_location.name} for order {order.code}")
                # else:
                #     # Rider already assigned, use existing rider
                #     assigned_rider = order.rider
                #     if assigned_rider:
                #         print(f"[DEBUG] Rider already assigned: {assigned_rider.username}")
                #
                # # Send notification to rider if assigned
                # if assigned_rider:
                #     message = f"Order {order.code} is ready for delivery! Pickup from: {order.pickup_address}"
                #     notification = Notification.objects.create(
                #         user=assigned_rider,
                #         order=order,
                #         message=message,
                #         notification_type='order_update'
                #     )
                #     print(f"✓ Notification (ID: {notification.id}) sent to rider {assigned_rider.username} for order {order.code}")
                #
                #     # Send SMS to rider if phone number exists
                #     print(f"[DEBUG] Checking rider phone: {assigned_rider.phone if hasattr(assigned_rider, 'phone') else 'No phone attribute'}")
                #     if assigned_rider.phone:  # type: ignore
                #         try:
                #             from services.sms_service import AfricasTalkingSMSService
                #
                #             print(f"[DEBUG] Initializing SMS service for rider...")
                #             sms_service = AfricasTalkingSMSService()
                #             print(f"[DEBUG] Sending order ready notification to {assigned_rider.phone}...")
                #
                #             sms_result = sms_service.send_order_ready_notification(
                #                 assigned_rider.phone,  # type: ignore
                #                 order,
                #                 assigned_rider.get_full_name() or assigned_rider.username
                #             )
                #
                #             print(f"[DEBUG] SMS result: {sms_result}")
                #
                #             if sms_result and sms_result.get('status') == 'success':
                #                 print(f"✓ SMS sent to rider {assigned_rider.username} ({assigned_rider.phone})")  # type: ignore
                #             else:
                #                 error_msg = sms_result.get('message', 'Unknown error') if sms_result else 'No response'
                #                 print(f"⚠ Failed to send SMS to rider: {error_msg}")
                #         except Exception as e:
                #             print(f"⚠ SMS service error for rider {assigned_rider.username}: {str(e)}")
                #             import traceback
                #             traceback.print_exc()
                #     else:
                #         print(f"⚠ Rider {assigned_rider.username} has no phone number registered")
                # else:
                #     print(f"⚠ No rider to send notification to for order {order.code}")

                # record assignment event
                if assigned_rider:
                    from .models import OrderEvent
                    OrderEvent.objects.create(
                        order=order,
                        actor=request.user if request.user.is_authenticated else None,
                        event_type='assigned_rider',
                        data={'rider_id': assigned_rider.id, 'rider_username': assigned_rider.username}  # type: ignore
                    )
                
                # Send SMS to customer notifying that order is ready with invoice
                if order.user and order.user.phone:  # type: ignore
                    try:
                        from services.sms_service import AfricasTalkingSMSService
                        
                        sms_service = AfricasTalkingSMSService()
                        sms_result = sms_service.send_order_ready_for_customer(
                            order.user.phone,  # type: ignore
                            order
                        )
                        
                        if sms_result and sms_result.get('status') == 'success':
                            print(f"✓ Order ready notification with invoice sent to customer {order.user.username}")
                        else:
                            error_msg = sms_result.get('message', 'Unknown error') if sms_result else 'No response'
                            print(f"⚠ Failed to send order ready SMS to customer: {error_msg}")
                    except Exception as e:
                        print(f"⚠ SMS service error for order ready notification: {str(e)}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"⚠ Customer {order.user.username if order.user else 'Unknown'} has no phone number registered")
            
            # Handle status change to 'delivered' - send SMS to customer
            status_changed_to_delivered = new_status and new_status.lower() == 'delivered' and old_status.lower() != 'delivered'
            if status_changed_to_delivered and order.user and order.user.phone:  # type: ignore
                try:
                    from services.sms_service import AfricasTalkingSMSService
                    
                    sms_service = AfricasTalkingSMSService()
                    sms_result = sms_service.send_delivery_confirmation(
                        order.user.phone,  # type: ignore
                        order
                    )
                    
                    if sms_result and sms_result.get('status') == 'success':
                        print(f"✓ Delivery confirmation SMS sent to customer {order.user.username}")
                    else:
                        error_msg = sms_result.get('message', 'Unknown error') if sms_result else 'No response'
                        print(f"⚠ Failed to send delivery SMS: {error_msg}")
                except Exception as e:
                    print(f"⚠ SMS service error for delivery confirmation: {str(e)}")
                    import traceback
                    traceback.print_exc()

            serializer = OrderListSerializer(order)
            return Response(serializer.data)

        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            print(f"[ERROR] Exception in OrderUpdateView: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class OrderReviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_order(self, request, code):
        return Order.objects.get(code=code, user=request.user)

    def get(self, request, code):
        try:
            order = self.get_order(request, code)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
        if order.status != 'delivered':
            return Response({'detail': 'You can review an order after it is delivered.'}, status=status.HTTP_400_BAD_REQUEST)
        review = OrderReview.objects.filter(order=order).first()
        return Response(OrderReviewSerializer(review).data if review else None)

    def post(self, request, code):
        try:
            order = self.get_order(request, code)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
        if order.status != 'delivered':
            return Response({'detail': 'You can review an order after it is delivered.'}, status=status.HTTP_400_BAD_REQUEST)

        review = OrderReview.objects.filter(order=order).first()
        serializer = OrderReviewSerializer(
            instance=review,
            data={'rating': request.data.get('rating'), 'feedback': request.data.get('feedback', '').strip()},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(order=order, customer=request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)


class OrderListCreateView(generics.ListCreateAPIView):
    """
    GET  -> list orders (filtered by location for staff)
    POST -> create order (anonymous allowed)
    """
    queryset = Order.objects.all().order_by("-created_at")
    # We'll enforce authentication for GET (listing) but still allow anonymous POST (create)
    permission_classes = [LocationBasedPermission]
    
    # Explicitly configure pagination for faster responses
    from rest_framework.pagination import PageNumberPagination
    class OrderPagination(PageNumberPagination):
        page_size = 20  # Reduced from default 50 for faster response
        page_size_query_param = 'page_size'
        max_page_size = 100
    
    pagination_class = OrderPagination
    
    def get_serializer(self, *args, **kwargs):
        """
        Support ?fields= query parameter to request only specific fields
        Example: /orders/?fields=id,code,status,price
        This reduces response payload size and speeds up serialization
        """
        serializer_class = self.get_serializer_class()
        kwargs.setdefault('context', self.get_serializer_context())
        
        # Get fields parameter if provided
        fields_param = self.request.query_params.get('fields')
        if fields_param and self.request.method == 'GET':
            # Parse the fields list
            requested_fields = set(f.strip() for f in fields_param.split(',') if f.strip())
            
            # Create a dynamic serializer with only requested fields
            if requested_fields:
                class DynamicFieldsSerializer(serializer_class):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        
                        # Get all field names from the serializer
                        allowed_fields = set(self.fields.keys())
                        
                        # Calculate which fields to drop
                        fields_to_drop = allowed_fields - requested_fields
                        
                        # Remove fields not in the request
                        for field_name in fields_to_drop:
                            self.fields.pop(field_name, None)
                
                return DynamicFieldsSerializer(*args, **kwargs)
        
        return serializer_class(*args, **kwargs)

    def get_permissions(self):
        """
        Use stricter permissions for GET requests (require authentication) so
        anonymous users cannot list all orders. Allow anonymous POST to create orders.
        """
        if self.request.method == 'GET':
            return [permissions.IsAuthenticated(), LocationBasedPermission()]
        if self.request.method == 'POST':
            return [permissions.AllowAny(), LocationBasedPermission()]
        return [permissions.IsAuthenticated(), LocationBasedPermission()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        is_admin = user.is_superuser or getattr(user, 'role', None) == 'admin'
        
        # Optimize queries with selective loading
        # Defer heavy text fields that aren't needed for list view
        queryset = queryset.defer(
            'description', 'washer_notes', 'folder_notes', 'fumigator_notes'
        ).select_related(
            'user', 'service', 'rider', 'service_location', 
            'pickup_rider', 'delivery_rider', 'created_by', 'review'
        )
        
        # Riders should see only orders assigned through any rider field.
        # Keep this before the staff location filter because riders may also
        # have staff privileges, but must not receive every order at a location.
        is_rider = user.is_authenticated and (
            getattr(user, 'role', None) == 'rider' or
            getattr(user, 'staff_type', None) == 'rider'
        )
        if is_rider:
            queryset = queryset.filter(
                models.Q(rider=user) |
                models.Q(pickup_rider=user) |
                models.Q(delivery_rider=user)
            ).distinct()
        # For staff users, filter by their service location
        elif user.is_authenticated and user.is_staff and not is_admin:
            print(f"\n[DEBUG Orders] Staff user: {user.username} (ID: {user.id})")
            print(f"[DEBUG Orders] Staff service_location: {user.service_location} (ID: {user.service_location.id if user.service_location else 'None'})")
            
            if user.service_location:
                # Filter by service location only - no complex Q() queries for performance
                queryset = queryset.filter(
                    service_location=user.service_location
                )
                print(f"[DEBUG Orders] Applied location filter for: {user.service_location}")
            else:
                print(f"[DEBUG Orders] ⚠️ Staff has no service_location assigned, returning no orders")
                return Order.objects.none()
        # For regular users, show only their orders
        elif user.is_authenticated and not user.is_staff and not is_admin:
            queryset = queryset.filter(user=user)

        # Accept the order's database ID as a fallback for older clients that
        # did not receive the public order code in the list response.
        code = self.request.query_params.get("code")
        if code:
            code_value = code.strip()
            code_filter = models.Q(code__iexact=code_value)
            if code_value.isdecimal():
                order_id = int(code_value)
                if order_id <= 2147483647:
                    code_filter |= models.Q(pk=order_id)
            return queryset.filter(code_filter)
        
        # === BACKEND FILTERING SUPPORT (for performance optimization) ===
        
        # Filter by status if provided
        status_filter = self.request.query_params.get("status")
        if status_filter:
            print(f"[DEBUG Orders] Filtering by status: {status_filter}")
            queryset = queryset.filter(status__iexact=status_filter.strip())
        
        # Filter by rider if provided (check both rider, pickup_rider, delivery_rider, and created_by for manual orders)
        rider_filter = self.request.query_params.get("rider")
        if rider_filter:
            print(f"[DEBUG Orders] Filtering by rider/staff: {rider_filter}")
            rider_query = models.Q(
                rider__username__icontains=rider_filter
            ) | models.Q(
                pickup_rider__username__icontains=rider_filter
            ) | models.Q(
                delivery_rider__username__icontains=rider_filter
            ) | models.Q(
                created_by__username__icontains=rider_filter
            )
            queryset = queryset.filter(rider_query)
        
        # Search by code or customer name if provided
        search_query = self.request.query_params.get("search")
        if search_query:
            print(f"[DEBUG Orders] Searching for: {search_query}")
            search_term = search_query.strip()
            queryset = queryset.filter(
                models.Q(code__icontains=search_term) |
                models.Q(customer_name__icontains=search_term) |
                models.Q(user__username__icontains=search_term) |
                models.Q(user__first_name__icontains=search_term) |
                models.Q(user__last_name__icontains=search_term)
            )
        
        return queryset

    def perform_create(self, serializer):
        # Automatically set the service_location based on the pickup address
        # You might want to implement a more sophisticated location assignment logic
        if self.request.user.is_authenticated:
            order = serializer.save(user=self.request.user)
        else:
            order = serializer.save()
        
        print(f"\n[DEBUG perform_create] Order {order.code} created")
        print(f"[DEBUG] Initial rider: {order.rider.username if order.rider else 'None'}")
        
        # AUTO-ASSIGN RIDER FOR ALL ORDERS WITHOUT A RIDER BEFORE SENDING SMS
        # This ensures the rider is assigned before we try to send SMS
        # Applies to both manual (staff-created) and online orders
        if not order.rider:
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                
                print(f"[DEBUG] Attempting to auto-assign rider for order {order.code}")
                print(f"[DEBUG] Order type: {order.order_type}, Has rider: {bool(order.rider)}")
                
                service_location = order.service_location
                print(f"[DEBUG] Initial service_location: {service_location}")
                
                # If no service_location, try to infer from user's location or pickup address
                if not service_location:
                    # Try to match from user's location field
                    if order.user and order.user.location:
                        user_location = order.user.location.lower().strip()
                        print(f"[DEBUG] Trying to find location matching user location: {user_location}")
                        service_location = Location.objects.filter(
                            name__icontains=user_location,
                            is_active=True
                        ).first()
                        print(f"[DEBUG] Found location from user: {service_location}")
                    
                    # If still no location, try to extract from pickup_address
                    if not service_location:
                        print(f"[DEBUG] Trying to find location from pickup_address: {order.pickup_address}")
                        locations = Location.objects.filter(is_active=True)
                        print(f"[DEBUG] Available locations: {[loc.name for loc in locations]}")
                        for loc in locations:
                            if loc.name.lower() in order.pickup_address.lower():
                                service_location = loc
                                print(f"[DEBUG] Found location from address: {service_location}")
                                break
                    
                    # If still no match, assign to the first active location
                    if not service_location:
                        print(f"[DEBUG] No location found, using first active location")
                        service_location = Location.objects.filter(is_active=True).first()
                        print(f"[DEBUG] First active location: {service_location}")
                
                # DISABLED: Auto-assignment of riders - now manual by admin
                # Get all riders assigned to this location, sorted by completed_jobs
                # if service_location:
                #     print(f"[DEBUG] Looking for riders in location: {service_location.name}")
                #     riders = User.objects.filter(
                #         role='rider',
                #         service_location=service_location,
                #         is_active=True
                #     ).select_related('rider_profile').order_by('rider_profile__completed_jobs')
                #
                #     print(f"[DEBUG] Found {riders.count()} riders in {service_location.name}")
                #     for rider in riders:
                #         print(f"[DEBUG]   - {rider.username} (completed_jobs: {rider.rider_profile.completed_jobs if hasattr(rider, 'rider_profile') else 'N/A'})")
                #
                #     if riders.exists():
                #         # Auto-assign to the first available rider (least busy)
                #         assigned_rider = riders.first()
                #         order.rider = assigned_rider
                #         order.service_location = service_location
                #         # Update status from pending_assignment to requested if it was pending
                #         if order.status == 'pending_assignment':
                #             order.status = 'requested'
                #         order.save(update_fields=['rider', 'service_location', 'status'])
                #         print(f"[ASSIGNED] Order {order.code} auto-assigned to rider {assigned_rider.username}")
                #     else:
                #         print(f"⚠ No active riders found in {service_location.name}")
                # else:
                #     print(f"⚠ No service_location found, cannot assign rider")
            
            except Exception as e:
                print(f"⚠ Error auto-assigning rider: {str(e)}")
                import traceback
                traceback.print_exc()
        
        print(f"[DEBUG] Rider after assignment: {order.rider.username if order.rider else 'None'}")
        print(f"[DEBUG] Rider phone: {order.rider.phone if order.rider and hasattr(order.rider, 'phone') else 'N/A'}")

        # Online orders remain provisional until checkout succeeds. Alert the
        # admin and customer about pending payment without marking the order placed.
        if order.order_type == 'online' and not order.is_paid():
            try:
                from django.conf import settings
                from services.sms_service import AfricasTalkingSMSService

                admin_phone = settings.ADMIN_PHONE_NUMBER
                if admin_phone:
                    services = ', '.join(
                        service.name for service in order.services.all()
                    ) or 'N/A'
                    customer_name = (
                        order.user.get_full_name() or order.user.username
                        if order.user
                        else order.customer_name or 'Customer'
                    )
                    customer_phone = (
                        (order.user.phone if order.user and order.user.phone else None)
                        or order.customer_phone
                        or 'N/A'
                    )
                    clean_pickup = (
                        order.pickup_address.split('(contact:')[0].strip()
                        if order.pickup_address
                        else 'N/A'
                    )
                    admin_message = (
                        f"CACHE INDUSTRIES\n"
                        f"NEW ORDER - PAYMENT PENDING\n"
                        f"Order #: {order.code}\n"
                        f"Customer: {customer_name}\n"
                        f"Phone: {customer_phone}\n"
                        f"Pickup: {clean_pickup}\n"
                        f"Dropoff: {order.dropoff_address}\n"
                        f"Services: {services}\n"
                        f"Items: {order.items}\n"
                        f"Amount: KES {order.actual_price or order.price or 'TBD'}\n"
                        f"Payment: NOT YET PAID\n"
                        f"View: https://www.cache.co.ke/orders/{order.code}"
                    )
                    result = AfricasTalkingSMSService().send_sms(admin_phone, admin_message)
                    if result and result.get('status') == 'success':
                        print(f"✓ Unpaid order SMS sent to admin for order {order.code}")
                    else:
                        error_msg = result.get('message', 'Unknown error') if result else 'No response'
                        print(f"⚠ Failed to send unpaid order SMS to admin: {error_msg}")
            except Exception as sms_error:
                # Admin SMS delivery must not undo or fail an already-created order.
                print(f"⚠ Error sending unpaid order SMS for order {order.code}: {sms_error}")

            customer_phone = (
                (order.user.phone if order.user and order.user.phone else None)
                or order.customer_phone
            )
            if customer_phone and str(customer_phone).strip():
                try:
                    from services.sms_service import AfricasTalkingSMSService

                    sms_result = AfricasTalkingSMSService().send_order_payment_reminder(
                        customer_phone,
                        order,
                    )
                    if sms_result and sms_result.get('status') == 'success':
                        print(f"✓ Payment reminder SMS sent for order {order.code}")
                    else:
                        print(
                            f"⚠ Payment reminder SMS failed for order {order.code}: "
                            f"{sms_result.get('message', 'Unknown error') if sms_result else 'No response'}"
                        )
                except Exception as sms_error:
                    # SMS delivery must not undo or fail an already-created order.
                    print(f"⚠ Error sending payment reminder for order {order.code}: {sms_error}")
            return
        
        # Create in-app notifications for all three parties
        try:
            from notifications.models import Notification
            from django.contrib.auth import get_user_model
            User = get_user_model()
            
            # 1. Notify customer
            if order.user:
                Notification.objects.create(
                    user=order.user,
                    order=order,
                    message=f"Your order {order.code} has been placed successfully!",
                    notification_type='new_order'
                )
            
            # 2. Notify all admins
            admin_users = User.objects.filter(is_superuser=True, is_active=True)
            services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
            
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    order=order,
                    message=f"📦 New online order {order.code} from {order.user.username if order.user else 'Guest'}",
                    notification_type='new_order'
                )
            
            # 3. Notify rider (if assigned)
            if order.rider:
                Notification.objects.create(
                    user=order.rider,
                    order=order,
                    message=f"Order {order.code} assigned to you!",
                    notification_type='order_assigned'
                )
        
        except Exception as e:
            print(f"⚠ Error creating order notifications: {str(e)}")
        
        # Send SMS to all three parties (admin, customer, and rider if assigned)
        try:
            from django.conf import settings
            from services.sms_service import AfricasTalkingSMSService
            from users.models import Location
            
            admin_phone = settings.ADMIN_PHONE_NUMBER
            
            # Format services list
            services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
            user_name = order.user.get_full_name() or order.user.username if order.user else order.customer_name or 'Customer'
            # Get customer phone from user.phone OR customer_phone field (for walk-in orders)
            user_phone = (order.user.phone if order.user and order.user.phone else None) or \
                         (order.customer_phone if order.customer_phone else None) or None
            
            # 1. Send SMS to CUSTOMER
            if user_phone and str(user_phone).strip():
                try:
                    from services.sms_service import format_phone_number
                    sms_service = AfricasTalkingSMSService()
                    # Format phone number to international format
                    formatted_phone = format_phone_number(user_phone)
                    order_url = f"https://www.cache.co.ke/orders/{order.code}"
                    
                    # Extract clean pickup address and calculate hours for estimated delivery
                    clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                    if order.estimated_delivery:
                        hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
                        est_time = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
                    else:
                        est_time = 'TBD'
                    
                    customer_message = (
                        f"CACHE INDUSTRIES\n"
                        f"Order Confirmed!\n"
                        f"Order #: {order.code}\n"
                        f"Services: {services}\n"
                        f"Pickup: {clean_pickup}\n"
                        f"Price: KES {order.price or 'TBD'}\n"
                        f"Est. Delivery: {est_time}\n"
                        f"View: {order_url}\n"
                        f"We'll notify you when your order is ready!"
                    )
                    
                    result = sms_service.send_sms(formatted_phone, customer_message)
                    
                    if result and result.get('status') == 'success':
                        print(f"✓ Customer SMS sent to {formatted_phone} for order {order.code}")
                    else:
                        error_msg = result.get('message', 'Unknown error') if result else 'No response'
                        print(f"⚠ Failed to send customer SMS: {error_msg}")
                
                except Exception as sms_error:
                    print(f"⚠ Error sending customer SMS: {str(sms_error)}")
                    import traceback
                    traceback.print_exc()
            
            # 2. Send SMS to ADMIN
            if admin_phone:
                try:
                    sms_service = AfricasTalkingSMSService()
                    admin_url = f"https://www.cache.co.ke/orders/{order.code}"
                    
                    # Extract clean pickup address (remove contact info)
                    clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                    
                    # Calculate hours for estimated delivery
                    if order.estimated_delivery:
                        hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
                        est_time = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
                    else:
                        est_time = 'TBD'
                    
                    admin_message = (
                        f"CACHE INDUSTRIES\n"
                        f"New Online Order Assigned!\n"
                        f"Order #: {order.code}\n"
                        f"Customer: {user_name}\n"
                        f"Phone: {user_phone or 'N/A'}\n"
                        f"Pickup: {clean_pickup}\n"
                        f"Dropoff: {order.dropoff_address}\n"
                        f"Services: {services}\n"
                        f"Items: {order.items}\n"
                        f"Price: KES {order.price or 'TBD'}\n"
                        f"Est. Delivery: {est_time}\n"
                        f"Status: {order.get_actual_status_display()}\n"
                        f"Manage: {admin_url}"
                    )
                    
                    result = sms_service.send_sms(admin_phone, admin_message)
                    
                    if result and result.get('status') == 'success':
                        print(f"✓ Admin SMS sent to {admin_phone} for order {order.code}")
                    else:
                        error_msg = result.get('message', 'Unknown error') if result else 'No response'
                        print(f"⚠ Failed to send admin SMS: {error_msg}")
                
                except Exception as sms_error:
                    print(f"⚠ Error sending admin SMS: {str(sms_error)}")
                    import traceback
                    traceback.print_exc()
            
            # 3. Send SMS to ASSIGNED RIDER (if order has rider assigned)
            print(f"[DEBUG] Checking rider for SMS: {order.rider}")
            if order.rider:
                print(f"[DEBUG] Rider exists: {order.rider.username}")
                print(f"[DEBUG] Rider phone attribute: {hasattr(order.rider, 'phone')}")
                print(f"[DEBUG] Rider phone value: {order.rider.phone if hasattr(order.rider, 'phone') else 'NO PHONE ATTR'}")
                
                # Properly convert empty string to None
                rider_phone = order.rider.phone if hasattr(order.rider, 'phone') and order.rider.phone else None  # type: ignore
                rider_phone = None if rider_phone and not str(rider_phone).strip() else rider_phone  # type: ignore
                print(f"[DEBUG] Final rider_phone: {rider_phone}")
                
                if rider_phone and str(rider_phone).strip():
                    try:
                        print(f"[DEBUG] Attempting to send SMS to rider {order.rider.username} at {rider_phone}")
                        sms_service = AfricasTalkingSMSService()
                        rider_url = f"https://www.cache.co.ke/rider/order/{order.code}"
                        
                        # Extract clean pickup address and calculate hours for estimated delivery
                        clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                        if order.estimated_delivery:
                            hours_diff = (order.estimated_delivery - timezone.now()).total_seconds() / 3600
                            est_time = f"{int(hours_diff)}hrs" if hours_diff > 0 else 'TBD'
                        else:
                            est_time = 'TBD'
                        
                        rider_name = order.rider.get_full_name() or order.rider.username if order.rider else 'Rider'
                        rider_message = (
                            f"CACHE INDUSTRIES\n"
                            f"New Order Assigned!\n"
                            f"Order #: {order.code}\n"
                            f"Customer: {user_name}\n"
                            f"Phone: {user_phone or 'N/A'}\n"
                            f"Pickup: {clean_pickup}\n"
                            f"Dropoff: {order.dropoff_address}\n"
                            f"Products: {services}\n"
                            f"Items: {order.items}\n"
                            # f"Price: KES {order.price or 'TBD'}\n"
                            f"Est. Delivery: {est_time}\n"
                            f"Accept: {rider_url}"
                        )
                        
                        result = sms_service.send_sms(rider_phone, rider_message)
                        
                        if result and result.get('status') == 'success':
                            print(f"✓ Rider SMS sent to {order.rider.username} ({rider_phone}) for order {order.code}")
                        else:
                            error_msg = result.get('message', 'Unknown error') if result else 'No response'
                            print(f"⚠ Failed to send rider SMS to {order.rider.username}: {error_msg}")
                    
                    except Exception as sms_error:
                        print(f"⚠ Error sending rider SMS to {order.rider.username}: {str(sms_error)}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"[DEBUG] Rider {order.rider.username} has no phone number")
            else:
                print(f"[DEBUG] No rider assigned to order {order.code}")
        
        except Exception as e:
            print(f"⚠ Error in SMS notification block: {str(e)}")
            import traceback
            traceback.print_exc()

    def get_serializer_class(self):
        if self.request.method == "POST":
            return OrderCreateSerializer
        return OrderListSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx


class OrderPaymentStatusView(APIView):
    """
    GET -> Get payment status for an order
    Retrieves the payment information associated with an order
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, code, *args, **kwargs):
        try:
            # Try to get the order by code (order reference/id)
            order = Order.objects.get(code=code)
            
            # Check if user has permission to view this order
            if order.user != request.user and not request.user.is_staff:
                return Response(
                    {'detail': 'You do not have permission to view this order'},
                    status=status.HTTP_403_FORBIDDEN
                )

            rider = order.delivery_rider or order.pickup_rider or order.rider
            rider_info = {
                'name': rider.get_full_name() or rider.username,
                'phone': rider.phone,
            } if rider else None
            
            # Get the latest payment for this order
            from payments.models import Payment
            from django.db.models import Q

            payments = Payment.objects.filter(
                Q(order_id=order.id)
                | Q(raw_payload__order_reference=order.code)
            ).order_by('-created_at')

            if payments.exists():
                payment = payments.first()
                total = order.price
                paid = payments.filter(status=Payment.STATUS_SUCCESS).aggregate(total=Sum('amount'))['total'] or Decimal('0')
                remaining = max(Decimal(str(total)) - paid, Decimal('0')) if total is not None else Decimal('0')
                mpesa_payments = payments.filter(provider='mpesa')
                payment_statuses = set(payments.values_list('status', flat=True))
                mpesa_statuses = set(mpesa_payments.values_list('status', flat=True))

                # Keep an unresolved M-Pesa leg pending even when BOOST has
                # already reserved part of the order total.
                if mpesa_payments.exists() and 'success' not in mpesa_statuses and (
                    'initiated' in mpesa_statuses or 'pending' in mpesa_statuses
                ):
                    payment_status = 'initiated'
                elif remaining > Decimal('0.01') and 'success' in payment_statuses:
                    payment_status = 'partially_paid'
                elif mpesa_payments.exists() and 'success' in mpesa_statuses:
                    payment_status = 'success'
                elif mpesa_payments.exists():
                    payment_status = 'failed'
                elif 'success' in payment_statuses:
                    payment_status = 'success'
                elif 'initiated' in payment_statuses or 'pending' in payment_statuses:
                    payment_status = 'initiated'
                else:
                    payment_status = 'failed'

                return Response({
                    'status': payment_status,
                    'message': (
                        f'Partially paid. KES {remaining:,.2f} remains before your order '
                        'can be placed and delivery can start.'
                        if payment_status == 'partially_paid'
                        else f'Payment is {payment_status}'
                    ),
                    'checkout_request_id': payment.provider_reference,
                    'order_id': order.code,
                    'amount': float(order.price if order.price is not None else payment.amount),
                    'total_amount': float(total) if total is not None else 0,
                    'paid_amount': float(paid),
                    'remaining_amount': float(remaining),
                    'can_start_delivery': remaining <= Decimal('0.01'),
                    'rider_assigned': rider is not None,
                    'rider': rider_info,
                    'delivery_requested': order.delivery_requested,
                })

            else:
                return Response({
                    'status': 'pending',
                    'message': 'No payment found for this order',
                    'checkout_request_id': '',
                    'order_id': order.code,
                    'amount': 0,
                    'rider_assigned': rider is not None,
                    'rider': rider_info,
                    'delivery_requested': order.delivery_requested,
                }, status=status.HTTP_404_NOT_FOUND)
                
        except Order.DoesNotExist:
            return Response(
                {'detail': 'Order not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class OrderContributionStatusView(APIView):
    """Return public payment progress for a shareable checkout link."""
    permission_classes = []
    authentication_classes = []

    def get(self, request, code, *args, **kwargs):
        try:
            order = Order.objects.get(code=code)
            total = order.actual_price if order.actual_price is not None else order.price
            if total is None:
                return Response({'detail': 'This order does not have a payable amount yet.'}, status=status.HTTP_400_BAD_REQUEST)

            from payments.models import Payment
            payments = Payment.objects.filter(
                Q(order_id=order.id) | Q(raw_payload__order_reference=order.code),
                status=Payment.STATUS_SUCCESS,
            )
            paid = payments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total = Decimal(str(total))
            remaining = max(total - paid, Decimal('0'))
            percent = min((paid / total * Decimal('100')) if total else Decimal('0'), Decimal('100'))

            return Response({
                'order_id': order.code,
                'total_amount': float(total),
                'paid_amount': float(paid),
                'remaining_amount': float(remaining),
                'progress_percent': float(percent.quantize(Decimal('0.01'))),
                'is_fully_paid': remaining <= Decimal('0.01'),
            })
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)


class PublicOrderSummaryView(APIView):
    """Return payment-safe order details for a shareable checkout link."""
    permission_classes = []
    authentication_classes = []

    def get(self, request, code, *args, **kwargs):
        try:
            order = Order.objects.select_related('service').prefetch_related(
                'order_items__service', 'product_order_items'
            ).get(code=code)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        items = [
            {
                'name': item.service.name,
                'quantity': item.quantity,
                'price': float(item.service.price or 0),
            }
            for item in order.order_items.all()
        ]
        items.extend(
            {
                'name': item.product_name,
                'quantity': item.quantity,
                'price': float(item.unit_price),
                'item_type': 'product',
            }
            for item in order.product_order_items.all()
        )

        return Response({
            'code': order.code,
            'package': getattr(order.service, 'name', None) or (items[0]['name'] if items else 'Delivery order'),
            'items': order.items,
            'total_amount': float(order.price or 0),
            'delivery_cost': 0.0 if order.free_delivery else 50.0,
            'dropoff_address': order.dropoff_address,
            'order_items': items,
        })


class RequestDeliveryView(APIView):
    """
    POST -> Request delivery for a paid order
    Notifies the assigned rider via SMS and in-app notification
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, code, *args, **kwargs):
        try:
            # Try to get the order by code
            order = Order.objects.get(code=code)
            
            # Check if user has permission to request delivery for this order
            if order.user != request.user and not request.user.is_staff:
                return Response(
                    {'detail': 'You do not have permission to request delivery for this order'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Check if delivery has already been requested
            if order.delivery_requested:
                return Response(
                    {'detail': 'Delivery has already been requested for this order'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if order has a payment
            from payments.models import Payment
            try:
                payment = Payment.objects.filter(order_id=order.id).latest('created_at')
                if payment.status != 'success':
                    return Response(
                        {'detail': 'Payment must be successful before requesting delivery'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except Payment.DoesNotExist:
                return Response(
                    {'detail': 'No payment found for this order'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if order has an assigned rider
            if not order.rider:
                return Response(
                    {'detail': 'No rider assigned to this order yet'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Send SMS to rider requesting delivery
            rider = order.rider
            rider_phone = rider.phone if hasattr(rider, 'phone') else None  # type: ignore
            
            if rider_phone:
                try:
                    from services.sms_service import AfricasTalkingSMSService
                    
                    sms_service = AfricasTalkingSMSService()
                    
                    services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
                    customer_name = order.user.get_full_name() or order.user.username if order.user else 'Customer'
                    customer_phone = order.user.phone if order.user and order.user.phone else 'N/A'  # type: ignore
                    
                    # Extract clean pickup address
                    clean_pickup = order.pickup_address.split('(contact:')[0].strip() if order.pickup_address else 'N/A'
                    
                    rider_url = f"https://www.cache.co.ke/rider/order/{order.code}"
                    rider_message = (
                        f"Delivery Request!\n"
                        f"Order #: {order.code}\n"
                        f"Customer: {customer_name}\n"
                        f"Phone: {customer_phone}\n"
                        f"Pickup: {clean_pickup}\n"
                        f"Dropoff: {order.dropoff_address}\n"
                        f"Service: {services}\n"
                        f"Items: {order.quantity or order.items}\n"
                        f"Amount: KES {order.actual_price or order.price}\n"
                        f"Payment: ✓ Confirmed\n"
                        f"View: {rider_url}\n"
                        f"Please proceed with delivery. Thank you!"
                    )
                    
                    sms_result = sms_service.send_sms(rider_phone, rider_message)
                    
                    if not (sms_result and sms_result.get('status') == 'success'):
                        return Response(
                            {'detail': f'Failed to notify rider: {sms_result.get("message", "Unknown error") if sms_result else "No response"}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )
                    
                    print(f"✓ Delivery request SMS sent to rider {rider.username} ({rider_phone}) for order {order.code}")
                
                except Exception as e:
                    print(f"⚠ SMS service error for delivery request: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    return Response(
                        {'detail': f'Error sending notification to rider: {str(e)}'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            else:
                return Response(
                    {'detail': 'Rider has no phone number registered'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create in-app notification for rider
            try:
                from notifications.models import Notification
                Notification.objects.create(
                    user=rider,
                    order=order,
                    message=f"Customer {order.user.username if order.user else 'Customer'} requested delivery for order {order.code}",
                    notification_type='delivery_request'
                )
                print(f"✓ Delivery request notification sent to rider {rider.username} for order {order.code}")
            except Exception as e:
                print(f"⚠ Error creating notification: {str(e)}")
            
            # Send SMS to admin about delivery request
            try:
                from django.conf import settings
                from services.sms_service import AfricasTalkingSMSService
                
                admin_phone = settings.ADMIN_PHONE_NUMBER
                
                if admin_phone:
                    sms_service = AfricasTalkingSMSService()
                    
                    services = ', '.join([s.name for s in order.services.all()]) if order.services.exists() else 'N/A'
                    customer_name = order.user.get_full_name() or order.user.username if order.user else 'Customer'
                    rider_name = rider.get_full_name() or rider.username
                    
                    admin_message = (
                        f"📦 DELIVERY REQUEST!\n"
                        f"Order #: {order.code}\n"
                        f"Customer: {customer_name}\n"
                        f"Rider: {rider_name}\n"
                        f"Service: {services}\n"
                        f"Pickup: {order.pickup_address}\n"
                        f"Dropoff: {order.dropoff_address}\n"
                        f"Amount: KES {order.actual_price or order.price}\n"
                        f"Payment: ✓ Confirmed\n"
                        f"Status: Ready for pickup/delivery"
                    )
                    
                    admin_sms_result = sms_service.send_sms(admin_phone, admin_message)
                    
                    if admin_sms_result and admin_sms_result.get('status') == 'success':
                        print(f"✓ Delivery request SMS sent to admin ({admin_phone}) for order {order.code}")
                    else:
                        error_msg = admin_sms_result.get('message', 'Unknown error') if admin_sms_result else 'No response'
                        print(f"⚠ Failed to send admin SMS: {error_msg}")
                else:
                    print(f"⚠ Admin phone number not configured")
            
            except Exception as e:
                print(f"⚠ SMS service error sending admin notification: {str(e)}")
                import traceback
                traceback.print_exc()
            
            # Mark delivery as requested
            from django.utils import timezone
            order.delivery_requested = True
            order.delivery_requested_at = timezone.now()
            order.save(update_fields=['delivery_requested', 'delivery_requested_at'])
            
            print(f"✓ Delivery request marked as complete for order {order.code}")
            
            return Response({
                'status': 'success',
                'message': f'Delivery request sent to rider {rider.get_full_name() or rider.username}',
                'rider_name': rider.get_full_name() or rider.username,
                'rider_phone': rider_phone,
            })
        
        except Order.DoesNotExist:
            return Response(
                {'detail': 'Order not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            print(f"[ERROR] Exception in RequestDeliveryView: {str(e)}")
            return Response(
                {'detail': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )