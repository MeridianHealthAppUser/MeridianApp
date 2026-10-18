"""Restrict cookie-authenticated WebSockets to the exact trusted page origin."""
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import split_domain_port, validate_host

logger = logging.getLogger(__name__)


class SameOriginWebSocketMiddleware:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        headers = scope.get('headers', [])
        hosts = [value for name, value in headers if name.lower() == b'host']
        origins = [value for name, value in headers if name.lower() == b'origin']
        rejection = 'missing_or_duplicate_headers'
        try:
            if len(hosts) == len(origins) == 1:
                host = hosts[0].decode('ascii')
                origin = urlsplit(origins[0].decode('ascii'))
                domain, _ = split_domain_port(host.lower())
                secure = scope.get('scheme') in ('wss', 'https')
                proxy = getattr(settings, 'SECURE_PROXY_SSL_HEADER', None)
                if proxy:
                    header_name = proxy[0].removeprefix('HTTP_').lower().replace('_', '-').encode()
                    forwarded = [value.decode('ascii').split(',')[0].strip() for name, value in headers if name.lower() == header_name]
                    secure = secure or (len(forwarded) == 1 and forwarded[0] == proxy[1])
                scheme = 'https' if secure else 'http'
                target = urlsplit(scheme+'://'+host)
                checks = (
                    ('untrusted_host', domain and validate_host(domain, settings.ALLOWED_HOSTS)),
                    ('scheme_mismatch', origin.scheme == scheme),
                    ('insecure_transport', settings.DEBUG or secure),
                    ('host_mismatch', origin.hostname == target.hostname),
                    ('port_mismatch', (origin.port or (443 if secure else 80)) == (target.port or (443 if secure else 80))),
                    ('origin_credentials', origin.username is None and origin.password is None),
                    ('origin_path', not origin.path and not origin.query and not origin.fragment),
                )
                rejection = next((reason for reason, passed in checks if not passed), None)
        except (ValueError, UnicodeError):
            rejection = 'malformed_headers'
        if rejection:
            # A fixed reason identifies proxy configuration errors without
            # recording cookies, URLs, patient identifiers or header values.
            logger.warning('WebSocket origin rejected: %s', rejection)
            await send({'type': 'websocket.close', 'code': 4403})
            return
        return await self.application(scope, receive, send)
