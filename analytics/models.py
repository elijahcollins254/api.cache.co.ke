from django.conf import settings
from django.db import models
from django.db.models import F, Q


class FriendConnection(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='friend_connections',
    )
    friend = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='matched_by_contacts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['friend__first_name', 'friend__last_name', 'friend__username']
        constraints = [
            models.UniqueConstraint(
                fields=['owner', 'friend'],
                name='analytics_unique_friend_connection',
            ),
            models.CheckConstraint(
                check=~Q(owner=F('friend')),
                name='analytics_no_self_friend_connection',
            ),
        ]

    def __str__(self):
        return f'{self.owner_id} -> {self.friend_id}'
