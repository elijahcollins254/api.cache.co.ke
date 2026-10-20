from django.urls import path

from .views import ContactSyncView, FriendListView

urlpatterns = [
    path('contacts/sync/', ContactSyncView.as_view(), name='contact-sync'),
    path('contacts/', ContactSyncView.as_view(), name='contact-clear'),
    path('friends/', FriendListView.as_view(), name='friend-list'),
]
