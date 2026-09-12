"""Restrict cookie-authenticated WebSockets to the exact trusted page origin."""
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import split_domain_port, validate_host


class SameOriginWebSocketMiddleware:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        headers = scope.get('headers', [])
        hosts = [value for name, value in headers if name.lower() == b'host']
        origins = [value for name, value in headers if name.lower() == b'origin']
        valid = False
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
                valid = bool(domain and validate_host(domain, settings.ALLOWED_HOSTS)
                    and origin.scheme == scheme and (settings.DEBUG or secure)
                    and origin.hostname == target.hostname
                    and (origin.port or (443 if secure else 80)) == (target.port or (443 if secure else 80))
                    and origin.username is None and origin.password is None
                    and not origin.path and not origin.query and not origin.fragment)
        except (ValueError, UnicodeError):
            pass
        if not valid:
            await send({'type': 'websocket.close', 'code': 4403})
            return
        return await self.application(scope, receive, send)
