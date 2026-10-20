import requests
import base64
import logging
import os
from urllib.parse import quote
from datetime import datetime
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import views, viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from .models import Payment, TradeIn
from bnpl.models import BNPLAccount as BNPLUser
from bnpl.services import (
    notify_boost_limit_increased,
    notify_boost_opted_in,
    notify_boost_repayment,
    notify_boost_transaction,
    refresh_credit_limit,
)
from .serializers import BNPLUserSerializer, TradeInSerializer
from orders.models import Order

from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

BOOST_CHECKOUT_TIMEOUT_MINUTES = 15


def create_paid_guest_account(order, payment_phone):
    """Attach a guest order to a real account and return its setup URL, if new."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator
    from services.sms_service import format_phone_number

    User = get_user_model()
    phone = format_phone_number(payment_phone)
    if not phone or not order.user or order.user.username != 'guest_orders':
        return None

    user = User.objects.filter(phone=phone, is_active=True).first()
    if user:
        order.user = user
        order.save(update_fields=['user'])
        return None

    username_base = f'customer_{phone.lstrip("+")}'
    username = username_base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f'{username_base}_{suffix}'

    user = User.objects.create(
        username=username,
        email=f'{phone.lstrip("+")}@cache.local',
        phone=phone,
        role='customer',
        is_active=True,
    )
    user.set_unusable_password()
    user.save(update_fields=['password'])
    order.user = user
    order.save(update_fields=['user'])

    frontend_url = os.getenv('FRONTEND_URL', 'https://www.cache.co.ke').rstrip('/')
    token = default_token_generator.make_token(user)
    return f'{frontend_url}/reset-password?uid={user.pk}&token={quote(token)}'


def get_order_payment_progress(order):
    """Calculate payment progress against the actual payable amount.

    The payable total for an order is the offer-adjusted amount when an offer is
    applied. Using the raw order price here creates a stale remaining-balance
    calculation and allows double-crediting or overpayment against a discounted
    order.
    """
    total = order.actual_price if order.actual_price is not None else order.price
    paid = Payment.objects.filter(
        Q(order_id=order.id) | Q(raw_payload__order_reference=order.code),
        status=Payment.STATUS_SUCCESS,
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total = Decimal(str(total)) if total is not None else None
    remaining = max(total - paid, Decimal('0')) if total is not None else None
    return total, paid, remaining


def release_expired_boost_checkout(payment, now=None):
    """Release BOOST reserved for an STK checkout that never completed."""
    if payment.provider != 'mpesa' or payment.status not in (
        Payment.STATUS_PENDING,
        Payment.STATUS_INITIATED,
    ):
        return False

    payload = payment.raw_payload or {}
    if not payload.get('is_boost_checkout') or not payment.initiated_at:
        return False

    now = now or timezone.now()
    age_seconds = (now - payment.initiated_at).total_seconds()
    if age_seconds < BOOST_CHECKOUT_TIMEOUT_MINUTES * 60:
        return False

    boost_payment_id = payload.get('boost_payment_id')
    boost_amount = Decimal(str(payload.get('boost_amount', '0')))
    if not boost_payment_id or boost_amount <= 0:
        payment.mark_failed(note='Expired BOOST checkout without reserved credit.')
        return True

    account = BNPLUser.objects.select_for_update().get(user_id=payment.user_id)
    boost_payment = Payment.objects.select_for_update().get(id=boost_payment_id)
    account.current_balance = max(account.current_balance - boost_amount, Decimal('0.00'))
    account.save(update_fields=['current_balance', 'updated_at'])
    boost_payment.mark_failed(note='BOOST credit released after expired M-Pesa checkout.')
    payment.mark_failed(note='M-Pesa checkout expired; reserved BOOST credit released.')
    logger.info('Released expired BOOST checkout %s: %s KES', payment.id, boost_amount)
    return True


def notify_partially_paid_order(order, remaining):
    """Tell the customer and admins that fulfillment is blocked by a balance."""
    message = (
        f"Order {order.code} is partially paid. KES {remaining:,.2f} remains "
        "before your order can be placed and delivery can start."
    )
    try:
        from notifications.models import Notification
        from django.contrib.auth import get_user_model

        if order.user:
            Notification.objects.create(user=order.user, order=order, message=message, notification_type='order_update')
        User = get_user_model()
        for admin in User.objects.filter(is_staff=True, is_active=True):
            Notification.objects.create(
                user=admin,
                order=order,
                message=f"{message} Customer: {order.user.username if order.user else 'Guest'}.",
                notification_type='order_update',
            )
    except Exception:
        logger.exception("Failed to create partial-payment notifications for %s", order.code)

    try:
        from django.conf import settings
        from services.sms_service import AfricasTalkingSMSService, format_phone_number
        sms_service = AfricasTalkingSMSService()
        customer_phone = order.user.phone if order.user and order.user.phone else order.customer_phone
        if customer_phone:
            sms_service.send_sms(format_phone_number(customer_phone), f"CACHE INDUSTRIES\n{message}")
        if settings.ADMIN_PHONE_NUMBER:
            sms_service.send_sms(settings.ADMIN_PHONE_NUMBER, f"CACHE INDUSTRIES\n{message}")
    except Exception:
        logger.exception("Failed to send partial-payment SMS for %s", order.code)


def activate_paid_order(order, payment_method, payment_phone=None):
    """Activate only fully paid orders; otherwise keep them partially paid."""
    _total, _paid, remaining = get_order_payment_progress(order)
    if remaining is None or remaining > Decimal('0.01'):
        order.payment_method = payment_method
        if order.status in ('pending_payment', 'partially_paid'):
            order.status = 'partially_paid'
        order.save(update_fields=['payment_method', 'status'])
        notify_partially_paid_order(order, remaining or Decimal('0'))
        return False

    order.payment_method = payment_method
    if order.status in ('pending_payment', 'partially_paid'):
        order.status = 'requested'
    order.save(update_fields=['payment_method', 'status'])

    account_setup_url = None
    try:
        account_setup_url = create_paid_guest_account(order, payment_phone or order.customer_phone)
    except Exception:
        logger.exception("Failed to create paid guest account for %s", order.code)

    try:
        from notifications.models import Notification
        from django.contrib.auth import get_user_model

        if order.user:
            Notification.objects.create(
                user=order.user,
                order=order,
                message=f"Payment received for order {order.code}. Your order has been placed.",
                notification_type='new_order'
            )

        User = get_user_model()
        for admin in User.objects.filter(is_staff=True, is_active=True):
            Notification.objects.create(
                user=admin,
                order=order,
                message=f"Paid online order {order.code} is ready for processing.",
                notification_type='new_order'
            )
    except Exception:
        logger.exception("Failed to create paid-order notifications for %s", order.code)

    try:
        from django.conf import settings
        from services.sms_service import AfricasTalkingSMSService, format_phone_number

        services = ', '.join(service.name for service in order.services.all()) or 'N/A'
        customer_phone = order.user.phone if order.user and order.user.phone else order.customer_phone
        payable_amount = order.price
        message = (
            f"CACHE INDUSTRIES\n"
            f"Payment Confirmed!\n"
            f"Order #: {order.code}\n"
            f"Services: {services}\n"
            f"Amount: KES {payable_amount}\n"
            f"View: https://www.cache.co.ke/orders/{order.code}"
        )
        if account_setup_url and customer_phone:
            message += f"\nAccount created. Set your password: {account_setup_url}"
        sms_service = AfricasTalkingSMSService()
        if customer_phone:
            sms_service.send_sms(format_phone_number(customer_phone), message)
        if settings.ADMIN_PHONE_NUMBER:
            sms_service.send_sms(settings.ADMIN_PHONE_NUMBER, message.replace('Payment Confirmed!', 'PAID ONLINE ORDER!'))
    except Exception:
        logger.exception("Failed to send paid-order SMS for %s", order.code)
    return True


class ZeroPaymentCompletionView(views.APIView):
    """Complete an order whose applied offer reduces its payable amount to zero."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, code):
        try:
            order = Order.objects.select_for_update().get(code=code, user=request.user)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        existing_payment = Payment.objects.filter(
            order_id=order.id,
            provider='offer',
            status=Payment.STATUS_SUCCESS,
        ).first()
        if existing_payment:
            return Response({
                'status': 'success',
                'message': 'Order already completed.',
                'order_id': order.code,
            })

        if order.status != 'pending_payment':
            return Response(
                {'detail': 'This order is no longer awaiting payment.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not order.applied_offer_id:
            return Response(
                {'detail': 'Apply an offer before completing a zero-payment order.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payable_amount = order.price
        if payable_amount is None or payable_amount > 0:
            return Response(
                {'detail': 'This order still has a balance to pay.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payment = Payment.objects.create(
            user=request.user,
            order_id=order.id,
            provider='offer',
            provider_reference=f'offer-{order.code}',
            amount=0,
            phone_number=str(request.user.phone or 'N/A'),
            status=Payment.STATUS_SUCCESS,
            initiated_at=timezone.now(),
            completed_at=timezone.now(),
            raw_payload={'offer_id': order.applied_offer_id, 'zero_payment': True},
            notes='Completed with applied offer.',
        )
        activate_paid_order(order, 'offer')

        return Response({
            'status': payment.status,
            'message': 'Payment completed successfully. Your order is being processed.',
            'order_id': order.code,
        })

class BNPLViewSet(viewsets.GenericViewSet):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = BNPLUserSerializer

    def get_queryset(self):
        return BNPLUser.objects.filter(user=self.request.user)

    @action(detail=False, methods=['get'])
    def status(self, request):
        """Get the user's BNPL status and check for pending payment confirmations."""
        try:
            bnpl_user = BNPLUser.objects.get(user=request.user)
            
            # Check if there are any pending BNPL balance payment
            pending_payment = Payment.objects.filter(
                user=request.user,
                status='initiated',
                raw_payload__is_bnpl_balance_payment=True
            ).order_by('-created_at').first()
            
            if pending_payment and pending_payment.provider_reference:
                # Query M-Pesa to check if payment has been completed
                mpesa_view = MpesaSTKPushView()
                try:
                    result = mpesa_view._query_transaction_status(pending_payment.provider_reference)
                    
                    # If M-Pesa reports success (result_code == 0), update BNPL balance
                    if result.get('result_code') == '0':
                        logger.info(f"M-Pesa query confirms payment success for user {request.user}")
                        repayment_amount = Decimal(str(pending_payment.amount))
                        previous_limit = bnpl_user.credit_limit
                        # Clear BNPL balance
                        bnpl_user.current_balance = Decimal('0')
                        bnpl_user.save()
                        # Mark payment as success
                        pending_payment.mark_success(payload=result)
                        bnpl_user = refresh_credit_limit(bnpl_user)
                        notify_boost_repayment(bnpl_user, repayment_amount)
                        if bnpl_user.credit_limit > previous_limit:
                            notify_boost_limit_increased(bnpl_user, previous_limit)
                    elif result.get('result_code') != '1':  # 1 = still processing, other codes = failed
                        # Payment failed or error
                        pending_payment.mark_failed(payload=result, note=f"M-Pesa query result code: {result.get('result_code')}")
                except Exception as e:
                    logger.warning(f"Error querying M-Pesa payment status: {str(e)}")
                    # Continue without failing - callback might come later
            
            serializer = self.get_serializer(bnpl_user)
            return Response(serializer.data)
        except BNPLUser.DoesNotExist:
            return Response({
                'is_enrolled': False,
                'credit_limit': 0,
                'current_balance': 0
            })

    @action(detail=False, methods=['post'])
    def refresh_status(self, request):
        """Refresh and return the user's current BNPL status."""
        try:
            bnpl_user = BNPLUser.objects.get(user=request.user)
            serializer = self.get_serializer(bnpl_user)
            return Response({
                'status': 'success',
                'data': serializer.data,
                'message': 'BOOST status refreshed'
            })
        except BNPLUser.DoesNotExist:
            return Response({
                'status': 'error',
                'is_enrolled': False,
                'credit_limit': 0,
                'current_balance': 0,
                'message': 'BOOST is not active'
            })

    @action(detail=False, methods=['get'])
    def check_pending_payment(self, request):
        """Check status of pending BNPL balance payment with M-Pesa query."""
        try:
            bnpl_user = BNPLUser.objects.get(user=request.user)
            
            # Find the most recent BNPL balance payment (regardless of status)
            # This way we can return the final result (success/failed) after payment completes
            recent_payment = Payment.objects.filter(
                user=request.user,
                raw_payload__is_bnpl_balance_payment=True
            ).order_by('-created_at').first()
            
            if not recent_payment:
                    return Response({
                        'has_pending_payment': False,
                        'message': 'No pending BOOST repayment'
                    })
            
            # If payment is already success, return success response
            if recent_payment.status == 'success':
                logger.info(f"Recent BNPL payment already marked success: {recent_payment.provider_reference}")
                payment_amount = Decimal(str(recent_payment.amount))
                return Response({
                    'has_pending_payment': False,
                    'payment_status': 'success',
                    'message': 'Payment completed successfully',
                    'current_balance': float(bnpl_user.current_balance),
                    'credit_limit': float(bnpl_user.credit_limit),
                    'payment_amount': float(payment_amount)
                })
            
            # If payment already failed, return failed response
            if recent_payment.status == 'failed':
                logger.info(f"Recent BNPL payment already marked failed: {recent_payment.provider_reference}")
                return Response({
                    'has_pending_payment': False,
                    'payment_status': 'failed',
                    'message': recent_payment.notes or 'Payment failed. Please try again.',
                    'current_balance': float(bnpl_user.current_balance),
                    'credit_limit': float(bnpl_user.credit_limit)
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # If payment is still pending/initiated, query M-Pesa
            if recent_payment.status in ['pending', 'initiated']:
                mpesa_view = MpesaSTKPushView()
                try:
                    result = mpesa_view._query_transaction_status(recent_payment.provider_reference)
                    result_code = result.get('result_code')
                    result_desc = result.get('result_desc', '').lower()
                    
                    logger.info(f"Pending payment status check for user {request.user}: ResultCode={result_code}, ResultDesc={result_desc}, result={result}")
                    
                    # M-Pesa Result Codes:
                    # 0 = Success
                    # 1 = Request still processing
                    # Other codes may vary, check result_desc for context
                    
                    # Normalize result code comparison
                    is_success = result_code in ['0', 0]
                    is_processing = result_code in ['1', 1] or 'processing' in result_desc or 'still being' in result_desc
                    
                    # Update payment status based on M-Pesa response
                    if is_success:
                        # Payment successful - reduce balance by payment amount
                        payment_amount = Decimal(str(recent_payment.amount))
                        logger.info(f"Payment confirmed successful for user {request.user}, reducing balance by {payment_amount}")
                        
                        previous_limit = bnpl_user.credit_limit
                        bnpl_user.current_balance -= payment_amount
                        bnpl_user.current_balance = max(bnpl_user.current_balance, Decimal('0'))  # Ensure no negative
                        bnpl_user.save()
                        recent_payment.mark_success(payload=result)
                        bnpl_user = refresh_credit_limit(bnpl_user)
                        notify_boost_repayment(bnpl_user, payment_amount)
                        if bnpl_user.credit_limit > previous_limit:
                            notify_boost_limit_increased(bnpl_user, previous_limit)
                        
                        return Response({
                            'has_pending_payment': False,
                            'payment_status': 'success',
                            'message': 'Payment completed successfully',
                            'current_balance': float(bnpl_user.current_balance),
                            'credit_limit': float(bnpl_user.credit_limit),
                            'payment_amount': float(payment_amount)
                        })
                    
                    elif is_processing:
                        # Payment still processing
                        logger.info(f"Payment still processing for user {request.user}")
                        return Response({
                            'has_pending_payment': True,
                            'payment_status': 'processing',
                            'message': result_desc or 'Payment is still being processed. Please wait.',
                            'result_desc': result.get('result_desc', ''),
                            'current_balance': float(bnpl_user.current_balance)
                        })
                    
                    else:
                        # Payment failed or cancelled - only mark as failed if definitely not processing
                        logger.warning(f"Payment failed for user {request.user}: ResultCode={result_code}, ResultDesc={result_desc}")
                        recent_payment.mark_failed(payload=result, note=f"Query result code: {result_code}, desc: {result_desc}")
                        
                        return Response({
                            'has_pending_payment': False,
                            'payment_status': 'failed',
                            'message': result.get('result_desc', 'Payment failed. Please try again.'),
                            'current_balance': float(bnpl_user.current_balance),
                            'credit_limit': float(bnpl_user.credit_limit)
                        }, status=status.HTTP_400_BAD_REQUEST)
                        
                except Exception as e:
                    logger.warning(f"Error querying M-Pesa for pending payment: {str(e)}", exc_info=True)
                    # If query fails, return pending status and let callback handle it
                    return Response({
                        'has_pending_payment': True,
                        'payment_status': 'processing',
                        'message': 'Payment status check in progress. Please wait.',
                        'current_balance': float(bnpl_user.current_balance)
                    })
            
            # Fallback: unknown status
            return Response({
                'has_pending_payment': False,
                'payment_status': recent_payment.status,
                'message': f'Payment status: {recent_payment.status}',
                'current_balance': float(bnpl_user.current_balance)
            })
        
        except BNPLUser.DoesNotExist:
            return Response(
                {'detail': 'You are not enrolled in BOOST'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error checking pending payment: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error checking payment status: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


    @action(detail=False, methods=['post'])
    def opt_in(self, request):
        """Opt in to BNPL service."""
        phone_number = request.data.get('phone_number')
        if not phone_number:
            return Response(
                {'detail': 'Phone number is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if user is already enrolled
        bnpl_user, created = BNPLUser.objects.get_or_create(
            user=request.user,
            defaults={
                'phone_number': phone_number,
                'is_active': True
            }
        )

        if not created:
            if not bnpl_user.is_active:
                bnpl_user.is_active = True
                bnpl_user.phone_number = phone_number
                bnpl_user.save()
                notify_boost_opted_in(bnpl_user)
                serializer = self.get_serializer(bnpl_user)
                return Response(serializer.data)
            return Response(
                {'detail': 'You are already active on BOOST'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(bnpl_user)
        notify_boost_opted_in(bnpl_user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def opt_out(self, request):
        """Opt out of BNPL service."""
        try:
            bnpl_user = BNPLUser.objects.get(user=request.user)
            if bnpl_user.current_balance > 0:
                return Response(
                    {'detail': 'Cannot opt out while you have an outstanding balance'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            bnpl_user.is_active = False
            bnpl_user.save()
            return Response({'detail': 'Successfully opted out of BOOST'})
        except BNPLUser.DoesNotExist:
            return Response(
                {'detail': 'You are not enrolled in BOOST'},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['post'])
    def pay_balance(self, request):
        """Initiate M-Pesa payment for BNPL balance using STK Push.
        
        Optionally accepts custom amount, otherwise pays full balance.
        """
        try:
            bnpl_user = BNPLUser.objects.get(user=request.user)
            
            if bnpl_user.current_balance <= 0:
                    return Response(
                        {'detail': 'No outstanding BOOST balance to repay'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Get optional custom amount from request, default to full balance
            requested_amount = request.data.get('amount')
            
            if requested_amount:
                try:
                    amount_to_pay = Decimal(str(requested_amount))
                    if amount_to_pay <= 0:
                        return Response(
                            {'detail': 'Amount must be greater than 0'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    if amount_to_pay > bnpl_user.current_balance:
                        return Response(
                            {
                                'detail': f'Cannot pay more than balance',
                                'current_balance': float(bnpl_user.current_balance),
                                'requested_amount': float(amount_to_pay)
                            },
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {'detail': 'Invalid amount format'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                amount_to_pay = bnpl_user.current_balance
            
            # Get phone number - can be provided in request or use user's BNPL phone
            phone = request.data.get('phone_number') or bnpl_user.phone_number
            
            if not phone:
                return Response(
                    {'detail': 'Phone number is required for M-Pesa payment'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Format phone number - remove any special characters and plus sign
            # M-Pesa requires format: 254XXXXXXXXX (no + sign, exactly 12 digits)
            phone_str = str(phone).strip()
            # Remove plus sign and any non-digit characters
            phone_str = ''.join(c for c in phone_str if c.isdigit())
            
            # Ensure it starts with 254 (Kenya country code)
            if phone_str.startswith('0'):
                phone_str = '254' + phone_str[1:]
            elif not phone_str.startswith('254'):
                phone_str = '254' + phone_str
            
            # Validate phone length (should be exactly 12 digits: 254 + 9 digits)
            if len(phone_str) != 12:
                return Response(
                    {'detail': f'Invalid phone number format. Please provide a valid Kenyan phone number.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            phone = phone_str
            
            # Get M-Pesa access token
            mpesa_view = MpesaSTKPushView()
            access_token = mpesa_view._get_access_token()
            
            # Convert amount to float for M-Pesa
            amount = float(amount_to_pay)
            
            # Create special order reference for BNPL balance payment
            account_reference = f'BNPL_BALANCE_{request.user.id}'
            
            # Initiate STK Push
            stk_response = mpesa_view._initiate_stk_push(
                access_token, amount, phone, account_reference
            )
            
            # Create Payment record for BNPL balance payment
            checkout_request_id = stk_response.get('CheckoutRequestID', '')
            payment = Payment.objects.create(
                user=request.user,
                order_id=None,  # No specific order, it's for balance
                amount=amount_to_pay,
                phone_number=phone,
                provider='mpesa',
                provider_reference=checkout_request_id,
                status='pending',
                raw_payload={
                    'order_reference': account_reference,
                    'is_bnpl_balance_payment': True,
                    'bnpl_payment_amount': float(amount_to_pay),
                    'bnpl_balance_before': float(bnpl_user.current_balance)
                }
            )
            payment.mark_initiated(provider_reference=checkout_request_id)
            
            logger.info(f"BNPL balance payment initiated for user {request.user}: {checkout_request_id}, amount: {amount}")
            return Response({
                'status': 'success',
                'message': 'M-Pesa prompt sent to your phone to repay your BOOST balance',
                'checkout_request_id': stk_response.get('CheckoutRequestID'),
                'amount': amount,
                'balance': float(bnpl_user.current_balance)
            }, status=status.HTTP_200_OK)
            
        except BNPLUser.DoesNotExist:
            return Response(
                {'detail': 'You are not enrolled in BOOST'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error initiating BNPL balance payment: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error initiating payment: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'])
    def process(self, request):
        """Process a BNPL payment for an order."""
        try:
            order_id = request.data.get('order_id')
            amount = request.data.get('amount')

            if not all([order_id, amount]):
                return Response(
                    {'detail': 'order_id and amount are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            try:
                amount = float(amount)
                if amount <= 0:
                    return Response(
                        {'detail': 'Amount must be greater than 0'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except (ValueError, TypeError):
                return Response(
                    {'detail': 'Invalid amount format'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # SECURITY: Validate that the amount matches the actual order price
            validation_error = self._validate_bnpl_order_amount(order_id, amount)
            if validation_error:
                return validation_error

            # Extract numeric part from order_id (e.g., 'WW-00225' -> 225)
            # For BNPL or non-numeric references, use a smaller hash
            order_id_numeric = None
            if isinstance(order_id, str):
                import re
                # Try to find regular order IDs first (WW-00225 format)
                numeric_matches = re.findall(r'\d+', order_id)
                if numeric_matches:
                    try:
                        # Use the first number (usually the order number)
                        first_num = int(numeric_matches[0])
                        # Ensure it fits in PositiveIntegerField (max 2147483647)
                        if first_num <= 2147483647:
                            order_id_numeric = first_num
                        else:
                            # If too large, use modulo
                            order_id_numeric = first_num % 1000000
                    except (ValueError, TypeError):
                        pass
                
                # If we couldn't extract a number, use hash of the string
                if order_id_numeric is None:
                    order_id_numeric = abs(hash(order_id)) % 1000000
            else:
                try:
                    order_id_numeric = int(order_id)
                    if order_id_numeric > 2147483647:
                        order_id_numeric = order_id_numeric % 1000000
                except (ValueError, TypeError):
                    order_id_numeric = None

            # Get or create BNPL user
            bnpl_user = BNPLUser.objects.get(user=request.user)

            if not bnpl_user.is_active:
                    return Response(
                        {'detail': 'Your BOOST account is inactive'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Calculate available credit (convert to Decimal for proper calculation)
            from decimal import Decimal
            amount_decimal = Decimal(str(amount))
            available_credit = bnpl_user.credit_limit - bnpl_user.current_balance

            # Check if order amount exceeds available credit
            if amount_decimal > available_credit:
                return Response(
                    {
                        'detail': f'Order amount exceeds available credit',
                        'required_amount': amount,
                        'available_credit': float(available_credit),
                        'credit_limit': float(bnpl_user.credit_limit),
                        'current_balance': float(bnpl_user.current_balance)
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Update BNPL balance
            bnpl_user.current_balance += amount_decimal
            bnpl_user.save()
            refresh_credit_limit(bnpl_user)

            # Create Payment record
            payment = Payment.objects.create(
                user=request.user,
                order_id=order_id_numeric,
                amount=amount_decimal,
                phone_number=bnpl_user.phone_number,
                provider='bnpl',
                status='success',
                raw_payload={
                    'order_reference': order_id,
                    'credit_limit': str(bnpl_user.credit_limit),
                    'new_balance': str(bnpl_user.current_balance)
                }
            )
            payment.mark_success()
            notify_boost_transaction(bnpl_user, amount_decimal, order_id)

            # Update the order's payment_method to reflect BNPL
            try:
                order = Order.objects.get(code=order_id)
                activate_paid_order(order, 'bnpl')
            except Order.DoesNotExist:
                logger.warning(f"Order with code {order_id} not found when processing BNPL payment")

            serializer = self.get_serializer(bnpl_user)
            return Response(
                {
                    'detail': 'BOOST purchase credit approved successfully',
                    'bnpl_status': serializer.data,
                    'payment_id': payment.id
                },
                status=status.HTTP_201_CREATED
            )

        except BNPLUser.DoesNotExist:
            return Response(
                {'detail': 'You are not active on BOOST. Please activate it first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error processing BNPL payment: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error processing BNPL payment: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'])
    @transaction.atomic
    def checkout(self, request):
        """Use BOOST credit first and collect the remaining amount by M-Pesa."""
        try:
            order_id = request.data.get('order_id')
            requested_total = Decimal(str(request.data.get('amount')))
            phone = request.data.get('phone_number') or getattr(request.user, 'phone', None)

            if not order_id or requested_total <= 0 or not phone:
                return Response(
                    {'detail': 'order_id, amount, and phone_number are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            amount_error = self._validate_bnpl_order_amount(order_id, requested_total)
            if amount_error:
                return amount_error

            account = BNPLUser.objects.select_for_update().get(user=request.user)
            if not account.is_active:
                return Response(
                    {'detail': 'Your BOOST account is inactive'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            available_credit = max(account.credit_limit - account.current_balance, Decimal('0.00'))
            boost_amount = min(requested_total, available_credit)
            mpesa_amount = requested_total - boost_amount

            account.current_balance += boost_amount
            account.save(update_fields=['current_balance', 'updated_at'])

            boost_payment = Payment.objects.create(
                user=request.user,
                order_id=None,
                amount=boost_amount,
                phone_number=phone,
                provider='bnpl',
                status=Payment.STATUS_SUCCESS,
                raw_payload={
                    'order_reference': order_id,
                    'is_boost_credit': True,
                    'boost_amount': float(boost_amount),
                    'order_total': float(requested_total),
                }
            )
            boost_payment.mark_success()
            if boost_amount > 0:
                notify_boost_transaction(account, boost_amount, order_id)

            if mpesa_amount <= 0:
                order = Order.objects.get(code=order_id)
                activate_paid_order(order, 'bnpl')
                return Response({
                    'status': 'success',
                    'payment_method': 'boost',
                    'boost_amount': float(boost_amount),
                    'mpesa_amount': 0,
                    'message': 'BOOST covered the full order amount',
                }, status=status.HTTP_201_CREATED)

            phone_str = ''.join(character for character in str(phone).strip() if character.isdigit())
            if phone_str.startswith('0'):
                phone_str = '254' + phone_str[1:]
            elif not phone_str.startswith('254'):
                phone_str = '254' + phone_str
            if len(phone_str) != 12:
                raise ValueError('Invalid phone number format. Please provide a valid Kenyan phone number.')

            mpesa_view = MpesaSTKPushView()
            access_token = mpesa_view._get_access_token()
            account_reference = order_id
            stk_response = mpesa_view._initiate_stk_push(
                access_token, float(mpesa_amount), phone_str, account_reference
            )
            checkout_request_id = stk_response.get('CheckoutRequestID', '')
            payment = Payment.objects.create(
                user=request.user,
                order_id=None,
                amount=mpesa_amount,
                phone_number=phone_str,
                provider='mpesa',
                provider_reference=checkout_request_id,
                status=Payment.STATUS_PENDING,
                raw_payload={
                    'order_reference': order_id,
                    'is_boost_checkout': True,
                    'boost_payment_id': boost_payment.id,
                    'boost_amount': float(boost_amount),
                    'order_total': float(requested_total),
                }
            )
            payment.mark_initiated(provider_reference=checkout_request_id)

            return Response({
                'status': 'success',
                'payment_method': 'boost_mpesa',
                'boost_amount': float(boost_amount),
                'mpesa_amount': float(mpesa_amount),
                'checkout_request_id': checkout_request_id,
                'message': f'BOOST applied KES {boost_amount}; M-Pesa payment requested for KES {mpesa_amount}',
            }, status=status.HTTP_200_OK)
        except BNPLUser.DoesNotExist:
            return Response(
                {'detail': 'You are not enrolled in BOOST'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Order.DoesNotExist:
            return Response(
                {'detail': f'Order not found: {request.data.get("order_id")}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as error:
            transaction.set_rollback(True)
            logger.error(f'Error processing BOOST checkout: {error}', exc_info=True)
            return Response(
                {'detail': str(error)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _validate_bnpl_order_amount(self, order_id, amount):
        """Validate that the provided amount matches the actual order price.
        
        SECURITY: This prevents users from modifying the amount in the URL
        to pay less than the server-calculated order total.
        
        Args:
            order_id: The order code (e.g., 'WW-00225')
            amount: The amount being paid
            
        Returns:
            None if valid, or a Response object with error details if invalid
        """
        try:
            from decimal import Decimal
            
            # Game wallet or non-order payments don't need validation
            if order_id == 'GAME_WALLET_TOPUP' or order_id is None:
                return None

            # Try to find the order by its code
            try:
                order = Order.objects.get(code=order_id)
            except Order.DoesNotExist:
                logger.warning(f"[SECURITY] BNPL: Order not found with code: {order_id}")
                return Response(
                    {'detail': f'Order not found: {order_id}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # The server-calculated payable total includes any offer discount.
            order_price = order.actual_price if order.actual_price is not None else order.price
            
            if order_price is None:
                logger.error(f"[SECURITY] BNPL: Order {order_id} has no payable amount.")
                return Response(
                    {'detail': 'Order cannot be checked out because it has no price.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Convert to Decimal for accurate comparison
            order_price_decimal = Decimal(str(order_price))
            amount_decimal = Decimal(str(amount))
            
            # Check if amounts match (allow 0.01 tolerance for rounding)
            if abs(order_price_decimal - amount_decimal) > Decimal('0.01'):
                logger.warning(
                    f"[SECURITY] FRAUD ALERT - BNPL Amount mismatch for order {order_id}: "
                    f"requested={amount}, actual={order_price}"
                )
                return Response(
                    {
                        'detail': f'Payment amount does not match order total',
                        'expected_amount': float(order_price),
                        'provided_amount': amount,
                        'order_id': order_id
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"[SECURITY] BNPL order amount validated for {order_id}: {amount} KES")
            return None
            
        except Exception as e:
            logger.error(f"[SECURITY] Error validating BNPL order amount: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error validating order: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def users(self, request):
        """Get all BNPL users (for admin)."""
        # Allow only staff/admin to view all users
        if not request.user.is_staff:
            return Response(
                {'detail': 'Permission denied. Admin access required.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        page_size = request.query_params.get('page_size', 100)
        bnpl_users = BNPLUser.objects.all().order_by('-created_at')
        
        try:
            page_size = int(page_size)
            bnpl_users = bnpl_users[:page_size]
        except (ValueError, TypeError):
            pass
        
        serializer = self.get_serializer(bnpl_users, many=True)
        return Response(serializer.data)


class MpesaSTKPushView(views.APIView):
    permission_classes = []  # Allow unauthenticated access
    authentication_classes = []

    def get(self, request):
        """Get user's phone number for checkout (if authenticated)."""
        if request.user.is_authenticated:
            phone = None
            if hasattr(request.user, 'phone_number'):
                phone = request.user.phone_number
            elif hasattr(request.user, 'profile') and hasattr(request.user.profile, 'phone_number'):
                phone = request.user.profile.phone_number
            
            if phone:
                return Response({'phone_number': phone})
        
        return Response({'phone_number': None})

    def post(self, request):
        """Initiate M-Pesa STK Push payment."""
        amount = request.data.get('amount')
        phone = request.data.get('phone')
        order_id = request.data.get('order_id')  # Can be null for game wallet top-ups
        share_investment_id = request.data.get('share_investment_id')
        share_investment = None

        if share_investment_id:
            from financing.models import ShareInvestment

            try:
                share_investment = ShareInvestment.objects.get(pk=share_investment_id)
            except (ShareInvestment.DoesNotExist, ValueError):
                return Response(
                    {'detail': 'Share investment not found.'},
                    status=status.HTTP_404_NOT_FOUND
                )

            if share_investment.status != 'pending':
                return Response(
                    {'detail': 'This share investment already has a payment outcome.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if share_investment.checkout_request_id:
                return Response(
                    {'detail': 'An M-Pesa prompt has already been sent for this investment.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        logger.info(f"STK Push request: amount={amount}, phone={phone}, order_id={order_id}")
        
        # If no phone provided and user is authenticated, try to get from user profile
        if not phone and request.user.is_authenticated:
            # Try to get phone from user profile
            if hasattr(request.user, 'phone_number'):
                phone = request.user.phone_number
                logger.info(f"Using phone from user profile: {phone}")
            elif hasattr(request.user, 'profile') and hasattr(request.user.profile, 'phone_number'):
                phone = request.user.profile.phone_number
                logger.info(f"Using phone from user.profile: {phone}")
        
        # Validate input - order_id is optional (null for game wallet top-ups)
        if not all([amount, phone]):
            error_msg = f'Missing required fields: amount={bool(amount)}, phone={bool(phone)}'
            logger.error(error_msg)
            return Response(
                {'detail': 'amount and phone are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Convert amount to numeric
        try:
            amount_float = float(amount)
            amount = int(amount_float)
        except (ValueError, TypeError):
            return Response(
                {'detail': 'Invalid amount format'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if amount <= 0:
            return Response(
                {'detail': 'Amount must be greater than zero.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if share_investment and amount_float != float(share_investment.amount):
            return Response(
                {'detail': 'Payment amount must match the share investment amount.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # SECURITY: Validate that the contribution does not exceed the unpaid balance.
        validation_error = self._validate_mpesa_order_amount(order_id, amount_float)
        if validation_error:
            return validation_error
        
        order_id_numeric = None
        if order_id is not None and order_id != 'GAME_WALLET_TOPUP':
            try:
                order_id_numeric = Order.objects.only('id').get(code=order_id).id
            except Order.DoesNotExist:
                return Response(
                    {'detail': f'Order not found: {order_id}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Validate phone number format (Kenyan format)
        if not self._validate_phone(phone):
            return Response(
                {'detail': 'Invalid phone number. Use format: 254712345678 or 0712345678'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if share_investment:
            normalized_phone = ''.join(character for character in phone if character.isdigit())
            if normalized_phone.startswith('0'):
                normalized_phone = f'254{normalized_phone[1:]}'
            if normalized_phone != share_investment.phone_number:
                return Response(
                    {'detail': 'Payment phone must match the share investment phone.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Clean up phone number - remove + prefix if present
        if phone.startswith('+'):
            phone = phone[1:]
            logger.info(f"Cleaned phone number from request: {phone}")
        if share_investment:
            phone = share_investment.phone_number
        
        # Log the phone being used
        logger.info(f"Using phone number for STK Push: {phone}")
        
        try:
            # Verify credentials are set
            if not settings.MPESA_CONSUMER_KEY or not settings.MPESA_CONSUMER_SECRET:
                error_msg = 'M-Pesa credentials not configured. Check your .env file for MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET'
                logger.error(error_msg)
                return Response(
                    {'detail': error_msg},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Get access token from Daraja API
            access_token = self._get_access_token()
            
            # Determine if this is a game wallet top-up (order_id is null)
            is_game_wallet_topup = order_id is None and share_investment is None
            account_reference = (
                'CACHEINVEST'
                if share_investment
                else order_id if order_id else 'GAME_WALLET_TOPUP'
            )
            
            # Initiate STK Push
            stk_response = self._initiate_stk_push(
                access_token, amount, phone, account_reference
            )
            
            # Create Payment record
            checkout_request_id = stk_response.get('CheckoutRequestID', '')
            if not checkout_request_id:
                raise ValueError('M-Pesa did not return a checkout request ID.')

            payment = Payment.objects.create(
                user=request.user if request.user.is_authenticated else None,
                order_id=order_id_numeric,
                amount=amount,
                phone_number=phone,
                provider='mpesa',
                provider_reference=checkout_request_id,
                status='pending',
                raw_payload={
                    'order_reference': account_reference,
                    'is_game_wallet': is_game_wallet_topup,
                    'is_share_investment': share_investment is not None,
                    'share_investment_id': str(share_investment.id) if share_investment else None,
                }
            )
            payment.mark_initiated(provider_reference=checkout_request_id)
            if share_investment:
                share_investment.checkout_request_id = checkout_request_id
                share_investment.save(update_fields=['checkout_request_id'])
            
            logger.info(f"Payment initiated successfully: {checkout_request_id}, is_game_wallet: {is_game_wallet_topup}")
            return Response({
                'status': 'success',
                'message': 'STK push sent to your phone',
                'checkout_request_id': stk_response.get('CheckoutRequestID'),
                'order_id': order_id or 'GAME_WALLET_TOPUP',
                'amount': amount
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            if share_investment:
                share_investment.status = 'failed'
                share_investment.completed_at = timezone.now()
                share_investment.save(update_fields=['status', 'completed_at'])
            logger.error(f"Error initiating payment: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error initiating payment: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _validate_mpesa_order_amount(self, order_id, amount):
        """Validate that the provided amount matches the actual order price.
        
        SECURITY: This prevents users from modifying the amount in the URL
        to pay less than the server-calculated order total.
        
        Args:
            order_id: The order code (e.g., 'WW-00225') or None for game wallet top-ups
            amount: The amount being paid
            
        Returns:
            None if valid, or a Response object with error details if invalid
        """
        try:
            # Game wallet top-ups don't need order validation
            if order_id is None or order_id == 'GAME_WALLET_TOPUP':
                return None

            # Try to find the order by its code
            try:
                order = Order.objects.get(code=order_id)
            except Order.DoesNotExist:
                logger.warning(f"[SECURITY] Order not found with code: {order_id}")
                return Response(
                    {'detail': f'Order not found: {order_id}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # The stored payable total is the offer-adjusted amount when present.
            from decimal import Decimal
            order_price = order.actual_price if order.actual_price is not None else order.price
            
            if order_price is None:
                logger.error(f"[SECURITY] Order {order_id} has no payable amount.")
                return Response(
                    {'detail': 'Order cannot be checked out because it has no price.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Convert to Decimal for accurate comparison
            order_price_decimal = Decimal(str(order_price))
            amount_decimal = Decimal(str(amount))
            
            from django.db.models import Q, Sum
            paid = Payment.objects.filter(
                Q(order_id=order.id) | Q(raw_payload__order_reference=order.code),
                status=Payment.STATUS_SUCCESS,
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            remaining = order_price_decimal - paid

            if amount_decimal <= 0 or amount_decimal - remaining > Decimal('0.01'):
                logger.warning(
                    f"[SECURITY] Contribution exceeds unpaid balance for order {order_id}: "
                    f"requested={amount}, remaining={remaining}"
                )
                return Response(
                    {
                        'detail': 'Payment amount exceeds the remaining order balance',
                        'remaining_amount': float(max(remaining, Decimal('0'))),
                        'provided_amount': amount,
                        'order_id': order_id
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"[SECURITY] Order amount validated successfully for {order_id}: {amount} KES")
            return None
            
        except Exception as e:
            logger.error(f"[SECURITY] Error validating M-Pesa order amount: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error validating order: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _validate_phone(self, phone):
        """Validate Kenyan phone number."""
        # Remove + prefix if present
        clean_phone = phone.lstrip('+')
        
        # Accept formats: 254712345678 or 0712345678 or +254712345678
        if clean_phone.startswith('0') and len(clean_phone) == 10:
            return True
        if clean_phone.startswith('254') and len(clean_phone) == 12:
            return True
        return False

    def _get_mpesa_config(self):
        """Get M-Pesa configuration based on environment setting."""
        environment = os.getenv('MPESA_ENVIRONMENT', 'production').lower()
        
        if environment == 'sandbox':
            logger.info("Using SANDBOX M-Pesa environment")
            return {
                'consumer_key': os.getenv('MPESA_SANDBOX_CONSUMER_KEY'),
                'consumer_secret': os.getenv('MPESA_SANDBOX_CONSUMER_SECRET'),
                'business_shortcode': os.getenv('MPESA_SANDBOX_BUSINESS_SHORTCODE', '174379'),
                'passkey': os.getenv('MPESA_SANDBOX_PASSKEY'),
                'oauth_url': 'https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials',
                'stk_push_url': 'https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest'
            }
        else:
            logger.info("Using PRODUCTION M-Pesa environment")
            return {
                'consumer_key': settings.MPESA_CONSUMER_KEY,
                'consumer_secret': settings.MPESA_CONSUMER_SECRET,
                'business_shortcode': settings.MPESA_BUSINESS_SHORTCODE,
                'passkey': settings.MPESA_PASSKEY,
                'oauth_url': 'https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials',
                'stk_push_url': 'https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest'
            }

    def _get_access_token(self):
        """Get access token from Safaricom Daraja API."""
        config = self._get_mpesa_config()
        url = config['oauth_url']
        
        logger.info(f"Requesting access token from {url}")
        logger.info(f"Consumer Key (first 20 chars): {config['consumer_key'][:20] if config['consumer_key'] else 'NOT SET'}")
        
        try:
            response = requests.get(
                url,
                auth=(config['consumer_key'], config['consumer_secret']),
                timeout=10
            )
            logger.info(f"OAuth Response status: {response.status_code}")
            logger.info(f"OAuth Response body: {response.text}")
            
            response.raise_for_status()
            token = response.json()['access_token']
            logger.info(f"✓ Access token obtained successfully (length: {len(token)})")
            logger.info(f"Access token (first 20 chars): {token[:20]}...")
            return token
        except requests.exceptions.RequestException as e:
            # Log detailed response information
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Failed to get access token: HTTP {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
                logger.error(f"Response headers: {dict(e.response.headers)}")
            else:
                logger.error(f"Failed to get access token: {str(e)}")
            logger.error(f"Credentials issue check:")
            logger.error(f"  - Consumer Key: {repr(config['consumer_key'][:50] if config['consumer_key'] else 'NOT SET')}...")
            logger.error(f"  - Consumer Secret: {repr(config['consumer_secret'][:20] if config['consumer_secret'] else 'NOT SET')}...")
            raise Exception(f'Failed to get access token: {str(e)}')

    def _initiate_stk_push(self, access_token, amount, phone, order_id):
        """Send STK Push request to Daraja API."""
        config = self._get_mpesa_config()
        url = config['stk_push_url']
        
        # Generate timestamp and password
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = self._encode_password(
            config['business_shortcode'],
            config['passkey'],
            timestamp
        )
        
        # Format phone number to 254 format
        formatted_phone = phone.replace('0', '254', 1) if phone.startswith('0') else phone
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "BusinessShortCode": config['business_shortcode'],
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": formatted_phone,
            "PartyB": config['business_shortcode'],
            "PhoneNumber": formatted_phone,
            "CallBackURL": settings.MPESA_CALLBACK_URL,
            "AccountReference": order_id,
            "TransactionDesc": f"cache Order {order_id}"
        }
        
        logger.info(f"Initiating STK Push to {formatted_phone} for amount {amount} KES")
        logger.info(f"STK Push URL: {url}")
        logger.info(f"STK Push Authorization header: Bearer {access_token[:20]}...")
        logger.info(f"STK Push payload: {payload}")
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            logger.info(f"STK Push response status: {response.status_code}")
            logger.info(f"STK Push response body: {response.text}")
            logger.info(f"STK Push response headers: {dict(response.headers)}")
            
            if response.status_code != 200:
                logger.error(f"STK Push returned non-200 status: {response.status_code}")
                logger.error(f"Full response: {response.text}")
            
            response.raise_for_status()
            result = response.json()
            logger.info(f"STK Push successful. CheckoutRequestID: {result.get('CheckoutRequestID')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"STK push request exception: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response text: {e.response.text}")
                logger.error(f"Response content type: {e.response.headers.get('content-type')}")
            raise Exception(f'STK push failed: {str(e)}')

    @staticmethod
    def _encode_password(shortcode, passkey, timestamp):
        """Encode password for M-Pesa authentication."""
        password_string = f"{shortcode}{passkey}{timestamp}"
        return base64.b64encode(password_string.encode()).decode()

    def _query_transaction_status(self, checkout_request_id):
        """Query M-Pesa for STK Push transaction status.
        
        This allows synchronous polling instead of waiting for async callbacks.
        
        Args:
            checkout_request_id: The CheckoutRequestID returned from STK Push
            
        Returns:
            dict with result_code and details, or raises exception on network error
        """
        try:
            config = self._get_mpesa_config()
            access_token = self._get_access_token()
            
            url = 'https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query' if config['business_shortcode'] != '174379' else 'https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query'
            
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password = self._encode_password(
                config['business_shortcode'],
                config['passkey'],
                timestamp
            )
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "BusinessShortCode": config['business_shortcode'],
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id
            }
            
            logger.info(f"Querying M-Pesa for STK Push status: {checkout_request_id}")
            logger.info(f"Query URL: {url}")
            
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                result_code = result.get('ResultCode', result.get('resultCode'))
                logger.info(f"M-Pesa query response for {checkout_request_id}: ResultCode={result_code}")
                logger.info(f"Full response: {result}")
                
                return {
                    'result_code': str(result_code),
                    'result_desc': result.get('ResultDesc', result.get('resultDesc', '')),
                    'checkout_request_id': checkout_request_id,
                    'raw_response': result
                }
            else:
                logger.warning(f"M-Pesa query returned status {response.status_code}: {response.text}")
                return {
                    'result_code': str(response.status_code),
                    'result_desc': f'HTTP {response.status_code}',
                    'checkout_request_id': checkout_request_id
                }
        except Exception as e:
            logger.error(f"Error querying M-Pesa transaction status: {str(e)}", exc_info=True)
            raise Exception(f'Failed to query transaction status: {str(e)}')



class MpesaCallbackView(views.APIView):
    """Handle M-Pesa callback notifications."""
    permission_classes = []  # Allow unauthenticated access for M-Pesa callbacks
    authentication_classes = []
    
    @transaction.atomic
    def post(self, request):
        """Process M-Pesa callback."""
        try:
            data = request.data
            checkout_request_id = data.get('Body', {}).get('stkCallback', {}).get('CheckoutRequestID')
            result_code = data.get('Body', {}).get('stkCallback', {}).get('ResultCode')
            
            logger.info(f"M-Pesa callback received: CheckoutRequestID={checkout_request_id}, ResultCode={result_code}")
            
            # Update payment status
            if checkout_request_id:
                try:
                    payment = Payment.objects.get(provider_reference=checkout_request_id)
                except Payment.DoesNotExist:
                    logger.warning(f"Payment not found for CheckoutRequestID: {checkout_request_id}")
                    return Response({'status': 'success'})
                
                if result_code == 0 and payment.status == Payment.STATUS_SUCCESS:
                    return Response({'status': 'success'})

                if result_code == 0:
                    payment.mark_success(payload=data)
                    logger.info(f"Payment marked as success: {checkout_request_id}")

                    is_share_investment = (payment.raw_payload or {}).get('is_share_investment', False)
                    if is_share_investment:
                        from financing.models import ShareInvestment

                        share_investment_id = (payment.raw_payload or {}).get('share_investment_id')
                        share_investment = ShareInvestment.objects.select_for_update().get(
                            pk=share_investment_id
                        )
                        share_investment.status = 'success'
                        share_investment.completed_at = timezone.now()
                        share_investment.save(update_fields=['status', 'completed_at'])
                    
                    # Check if this is a BNPL balance payment
                    is_bnpl_balance = (payment.raw_payload or {}).get('is_bnpl_balance_payment', False)
                    
                    if is_bnpl_balance and payment.user and not is_share_investment:
                        try:
                            bnpl_user = BNPLUser.objects.get(user=payment.user)
                            # Reduce BNPL balance by payment amount
                            payment_amount = Decimal(str(payment.amount))
                            previous_limit = bnpl_user.credit_limit
                            bnpl_user.current_balance -= payment_amount
                            bnpl_user.current_balance = max(bnpl_user.current_balance, Decimal('0'))  # Ensure no negative
                            bnpl_user.save()
                            bnpl_user = refresh_credit_limit(bnpl_user)
                            notify_boost_repayment(bnpl_user, payment_amount)
                            if bnpl_user.credit_limit > previous_limit:
                                notify_boost_limit_increased(bnpl_user, previous_limit)
                            logger.info(f"BNPL balance reduced for user {payment.user}: -{payment_amount}, new balance: {bnpl_user.current_balance}")
                        except BNPLUser.DoesNotExist:
                            logger.warning(f"BNPL user not found for payment user {payment.user}")
                        except Exception as e:
                            logger.error(f"Error reducing BNPL balance: {str(e)}", exc_info=True)
                    
                    # Check if this is a game wallet top-up
                    elif (
                        (payment.raw_payload or {}).get('is_game_wallet', False)
                        and payment.user
                        and not is_share_investment
                    ):
                        # Credit the game wallet
                        try:
                            from casino.models import GameWallet
                            
                            wallet, _ = GameWallet.objects.get_or_create(user=payment.user)
                            amount_decimal = Decimal(str(payment.amount))
                            wallet.add_funds(
                                amount_decimal,
                                source='mpesa',
                                payment_id=payment.pk,
                                notes=f'M-Pesa top-up via STK Push'
                            )
                            logger.info(f"Updated game wallet for user {payment.user}: added KES {amount_decimal}, new balance: {wallet.balance}")
                        except Exception as e:
                            logger.error(f"Error updating game wallet: {str(e)}", exc_info=True)
                    
                    elif not is_share_investment:
                        # Update order's payment_method to reflect M-Pesa
                        order_reference = (payment.raw_payload or {}).get('order_reference')
                        if order_reference and order_reference != 'GAME_WALLET_TOPUP' and not order_reference.startswith('BNPL_BALANCE'):
                            try:
                                order = Order.objects.get(code=order_reference)
                                if activate_paid_order(order, 'mpesa', payment.phone_number):
                                    logger.info(f"Updated order {order_reference} payment_method to mpesa")
                            except Order.DoesNotExist:
                                logger.warning(f"Order with code {order_reference} not found when processing M-Pesa callback")
                            except Exception as e:
                                logger.error(f"Error updating order payment_method: {str(e)}", exc_info=True)
                else:
                    payment.mark_failed(payload=data, note=f'Result Code: {result_code}')
                    payload = payment.raw_payload or {}
                    if payload.get('is_share_investment'):
                        from financing.models import ShareInvestment

                        share_investment_id = payload.get('share_investment_id')
                        try:
                            share_investment = ShareInvestment.objects.select_for_update().get(
                                pk=share_investment_id
                            )
                        except ShareInvestment.DoesNotExist:
                            logger.error(
                                "Share investment %s missing for payment %s",
                                share_investment_id,
                                payment.pk,
                            )
                        else:
                            share_investment.status = 'failed'
                            share_investment.completed_at = timezone.now()
                            share_investment.save(update_fields=['status', 'completed_at'])
                    if payload.get('is_boost_checkout'):
                        try:
                            boost_payment = Payment.objects.get(id=payload['boost_payment_id'])
                            bnpl_user = BNPLUser.objects.select_for_update().get(user=payment.user)
                            boost_amount = Decimal(str(payload.get('boost_amount', boost_payment.amount)))
                            bnpl_user.current_balance = max(
                                bnpl_user.current_balance - boost_amount,
                                Decimal('0.00')
                            )
                            bnpl_user.save(update_fields=['current_balance', 'updated_at'])
                            boost_payment.mark_failed(
                                payload=data,
                                note=f'BOOST credit released after M-Pesa failure: {result_code}'
                            )
                            logger.info(
                                f'Released BOOST credit for failed checkout {checkout_request_id}: '
                                f'{boost_amount} KES'
                            )
                        except (BNPLUser.DoesNotExist, Payment.DoesNotExist, KeyError) as error:
                            logger.error(f'Unable to release BOOST credit: {error}', exc_info=True)
            
            return Response({'status': 'success'})
        except Exception as e:
            logger.error(f"Callback processing error: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Callback processing error: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentStatusView(views.APIView):
    """Check payment status by checkout request ID."""
    permission_classes = []
    authentication_classes = []
    
    def get(self, request):
        """Get payment status."""
        checkout_request_id = request.query_params.get('checkout_request_id')
        
        if not checkout_request_id:
            return Response(
                {'detail': 'checkout_request_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            payment = Payment.objects.get(provider_reference=checkout_request_id)
            if payment.status in (Payment.STATUS_PENDING, Payment.STATUS_INITIATED):
                with transaction.atomic():
                    payment = Payment.objects.select_for_update().get(pk=payment.pk)
                    release_expired_boost_checkout(payment)
                    payment.refresh_from_db()
            
            return Response({
                'status': payment.status,
                'amount': float(payment.amount),
                'phone': payment.phone_number,
                'initiated_at': payment.initiated_at,
                'completed_at': payment.completed_at,
                'error_message': payment.notes if payment.status == 'failed' else None
            }, status=status.HTTP_200_OK)
        except Payment.DoesNotExist:
            return Response(
                {'detail': 'Payment not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error fetching payment status: {str(e)}", exc_info=True)
            return Response(
                {'detail': f'Error fetching payment status: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TradeInView(views.APIView):
    """Accept trade-in submissions from users and retrieve all trade-ins."""
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get all trade-ins for the authenticated user."""
        try:
            tradeins = TradeIn.objects.filter(user=request.user).order_by('-created_at')
            serializer = TradeInSerializer(tradeins, many=True)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error fetching trade-ins: {str(e)}", exc_info=True)
            return Response({'detail': f'Error fetching trade-ins: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            description = request.data.get('description')
            estimated_price = request.data.get('estimated_price')
            contact_phone = request.data.get('contact_phone')

            if not description or estimated_price is None:
                return Response({'detail': 'description and estimated_price are required'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                from decimal import Decimal
                estimated_price_dec = Decimal(str(estimated_price))
            except Exception:
                return Response({'detail': 'Invalid estimated_price'}, status=status.HTTP_400_BAD_REQUEST)

            tradein = TradeIn.objects.create(
                user=request.user,
                description=description,
                estimated_price=estimated_price_dec,
                contact_phone=contact_phone or ''
            )

            serializer = TradeInSerializer(tradein)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Error creating trade-in: {str(e)}", exc_info=True)
            return Response({'detail': f'Error creating trade-in: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)