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
from video.routing import websocket_urlpatterns
from video.security import SameOriginWebSocketMiddleware

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': SameOriginWebSocketMiddleware(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
})
