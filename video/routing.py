from django.urls import re_path

from .consumers import NotificationConsumer, VideoRoomConsumer


websocket_urlpatterns = [
    re_path(r'^ws/video/appointments/(?P<appointment_id>[0-9]{1,19})/$', VideoRoomConsumer.as_asgi()),
    re_path(r'^ws/notifications/$', NotificationConsumer.as_asgi()),
]
