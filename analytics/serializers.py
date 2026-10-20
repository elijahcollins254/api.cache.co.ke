import re

from rest_framework import serializers

from .models import FriendConnection


def normalize_kenyan_phone_number(value):
    value = value.strip()
    if not re.fullmatch(r'\+?[0-9().\s-]+', value):
        raise serializers.ValidationError('Enter a valid Kenyan phone number.')

    digits = re.sub(r'\D', '', value)
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('254') and len(digits) == 12:
        return f'+{digits}'
    if digits.startswith('0') and len(digits) == 10:
        return f'+254{digits[1:]}'
    if len(digits) == 9:
        return f'+254{digits}'
    raise serializers.ValidationError('Enter a valid Kenyan phone number.')


class ContactSyncSerializer(serializers.Serializer):
    phone_numbers = serializers.ListField(
        child=serializers.CharField(max_length=32, trim_whitespace=True),
        allow_empty=True,
        max_length=1000,
    )

    def validate_phone_numbers(self, values):
        normalized = []
        for value in values:
            phone_number = normalize_kenyan_phone_number(value)
            if phone_number not in normalized:
                normalized.append(phone_number)
        return normalized


class FriendProfileSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='friend_id', read_only=True)
    username = serializers.CharField(source='friend.username', read_only=True)
    first_name = serializers.CharField(source='friend.first_name', read_only=True)
    last_name = serializers.CharField(source='friend.last_name', read_only=True)

    class Meta:
        model = FriendConnection
        fields = ['id', 'username', 'first_name', 'last_name']
