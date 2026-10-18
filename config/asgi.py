"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_application = get_asgi_application()

# Initialise Django before imports that can touch models.
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from care.routing import websocket_urlpatterns as message_websocket_urlpatterns
from video.routing import websocket_urlpatterns as video_websocket_urlpatterns
from video.security import SameOriginWebSocketMiddleware

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': SameOriginWebSocketMiddleware(AuthMiddlewareStack(URLRouter(
        message_websocket_urlpatterns + video_websocket_urlpatterns,
    ))),
})
