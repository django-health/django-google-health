from django.urls import path

from . import views

app_name = "googlehealth"

urlpatterns = [
    path("connect/", views.connect, name="connect"),
    path("callback/", views.callback, name="callback"),
    path("mobile/callback/", views.mobile_callback, name="mobile_callback"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("notifications/", views.notification_receiver, name="notifications"),
]
