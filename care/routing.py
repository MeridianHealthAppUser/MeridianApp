from django.urls import path

from .consumers import MessageNotificationConsumer


websocket_urlpatterns = [
    path('ws/messages/', MessageNotificationConsumer.as_asgi()),
]
