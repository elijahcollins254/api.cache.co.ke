from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_204_NO_CONTENT
from rest_framework.views import APIView

from .models import FriendConnection
from .serializers import ContactSyncSerializer, FriendProfileSerializer

User = get_user_model()


class ContactSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ContactSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone_numbers = serializer.validated_data['phone_numbers']
        friends = User.objects.filter(
            phone__in=phone_numbers,
            is_active=True,
            role='customer',
        ).exclude(pk=request.user.pk).order_by('pk')

        with transaction.atomic():
            FriendConnection.objects.filter(owner=request.user).delete()
            FriendConnection.objects.bulk_create(
                FriendConnection(owner=request.user, friend=friend)
                for friend in friends
            )

        connections = list(
            FriendConnection.objects.filter(owner=request.user).select_related('friend')
        )
        return Response({
            'matched_count': len(connections),
            'friends': FriendProfileSerializer(connections, many=True).data,
        }, status=HTTP_200_OK)

    def delete(self, request):
        FriendConnection.objects.filter(owner=request.user).delete()
        return Response(status=HTTP_204_NO_CONTENT)


class FriendListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        connections = FriendConnection.objects.filter(
            owner=request.user,
        ).select_related('friend')
        friends = FriendProfileSerializer(connections, many=True).data
        return Response({'matched_count': len(friends), 'friends': friends})
