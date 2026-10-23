import logging
from itertools import islice

import requests
from django.conf import settings

from .models import Offer, OfferNotificationSubscription, OfferPushDevice

logger = logging.getLogger(__name__)


def send_offer_sms(phone_number, message):
    """Send an offer-related SMS and return the provider result."""
    from services.sms_service import AfricasTalkingSMSService

    if not phone_number:
        return {
            'status': 'error',
            'message': 'No phone number is available for this notification.',
        }

    try:
        return AfricasTalkingSMSService().send_sms(phone_number, message)
    except Exception as exc:
        logger.exception('Offer SMS failed for %s', phone_number)
        return {
            'status': 'error',
            'message': str(exc),
            'error': str(exc),
        }


def send_offer_claim_confirmation(user, offer: Offer):
    """Confirm an offer claim by SMS."""
    discount = (
        f'{offer.discount_percent}% off'
        if offer.discount_percent > 0
        else f'KSh {offer.discount_amount} off'
    )
    message = (
        f'CACHE INDUSTRIES: You claimed "{offer.title}" ({discount}). '
        f'Use code {offer.code} when placing your order.'
    )
    return send_offer_sms(user.phone, message)


def notify_subscribers_of_new_offer(offer: Offer):
    """Notify every active subscriber when an offer is created in admin."""
    if not getattr(settings, 'AFRICAS_TALKING_API_KEY', ''):
        logger.warning('Skipping offer SMS notifications because Africa\'s Talking is not configured.')
        return {'sent': 0, 'failed': 0, 'skipped': True}

    message = (
        f"CACHE INDUSTRIES new offer: {offer.title}. "
        f"{offer.discount_percent}% off" if offer.discount_percent > 0
        else f"CACHE INDUSTRIES new offer: {offer.title}. KSh {offer.discount_amount} off"
    )
    message = f"{message}. Code: {offer.code}. Visit cache.co.ke/offers"

    sent = 0
    failed = 0
    subscriptions = OfferNotificationSubscription.objects.filter(
        is_active=True,
    ).exclude(phone_number__isnull=True).exclude(phone_number='')

    for subscription in subscriptions.iterator():
        result = send_offer_sms(subscription.phone_number, message)
        if result.get('status') == 'success':
            sent += 1
        else:
            failed += 1
            logger.warning(
                'Failed to notify offer subscriber %s: %s',
                subscription.phone_number,
                result.get('message'),
            )

    return {'sent': sent, 'failed': failed, 'skipped': False}


def notify_push_devices_of_new_offer(offer: Offer):
    """Send a new-offer push to every registered Expo device."""
    discount = (
        f'{offer.discount_percent}% off'
        if offer.discount_percent > 0
        else f'KSh {offer.discount_amount} off'
    )
    sent = 0
    failed = 0
    devices = OfferPushDevice.objects.values_list('pk', 'token').iterator(chunk_size=100)

    while device_batch := list(islice(devices, 100)):
        messages = [
            {
                'to': token,
                'title': f'New offer: {offer.title}',
                'body': f'{discount}. Use code {offer.code}.',
                'sound': 'default',
                'channelId': 'default',
                'data': {'screen': '/offers', 'offerId': offer.pk},
            }
            for _, token in device_batch
        ]
        try:
            response = requests.post(
                'https://exp.host/--/api/v2/push/send',
                json=messages,
                headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                timeout=(3.05, 10),
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            failed += len(messages)
            logger.exception('Failed to send new-offer push notification batch: %s', exc)
            continue

        tickets = result.get('data') if isinstance(result, dict) else None
        if not isinstance(tickets, list):
            failed += len(messages)
            logger.error('Expo returned an invalid push ticket response.')
            continue

        for (device_id, token), ticket in zip(device_batch, tickets):
            if not isinstance(ticket, dict):
                failed += 1
                logger.error('Expo returned an invalid push ticket.')
                continue
            if ticket.get('status') == 'ok':
                sent += 1
                continue

            failed += 1
            details = ticket.get('details') or {}
            error = details.get('error', 'unknown') if isinstance(details, dict) else 'unknown'
            if error == 'DeviceNotRegistered':
                OfferPushDevice.objects.filter(
                    pk=device_id,
                    token=token,
                ).delete()
            logger.warning(
                'Expo rejected offer push for device %s: %s',
                device_id,
                error,
            )

        if len(tickets) < len(messages):
            missing_tickets = len(messages) - len(tickets)
            failed += missing_tickets
            logger.warning('Expo returned %s fewer push tickets than messages.', missing_tickets)

    return {'sent': sent, 'failed': failed}
