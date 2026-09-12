"""Short-lived coturn REST credentials, issued only by an authorised POST view."""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone


def _urls(values, schemes):
    if not isinstance(values, (list, tuple)) or len(values) > 8:
        raise ImproperlyConfigured('Invalid video relay configuration.')
    result = []
    for value in values:
        if not isinstance(value, str) or len(value) > 512 or re.search(r'\s', value):
            raise ImproperlyConfigured('Invalid video relay configuration.')
        scheme, separator, address = value.partition(':')
        if not separator or scheme not in schemes or address.startswith('//'):
            raise ImproperlyConfigured('Invalid video relay configuration.')
        try:
            parsed = urlsplit('//'+address)
            valid = (parsed.hostname and parsed.username is None and parsed.password is None
                     and not parsed.path and not parsed.fragment
                     and parsed.query in ('', 'transport=udp', 'transport=tcp')
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            raise ImproperlyConfigured('Invalid video relay configuration.')
        result.append(value)
    return result


def build_ice_config(access):
    """Never include account/patient IDs or the shared TURN secret in the result.

    Access must have been freshly authorised by video.access immediately before
    calling. Credentials are intentionally not persisted or logged.
    """
    stun = _urls(settings.VIDEO_STUN_URLS, ('stun', 'stuns'))
    turn = _urls(settings.VIDEO_TURN_URLS, ('turn', 'turns'))
    secret = settings.VIDEO_TURN_SECRET
    policy = settings.VIDEO_ICE_TRANSPORT_POLICY
    ttl = settings.VIDEO_TURN_CREDENTIAL_TTL
    if (policy not in ('all', 'relay') or type(ttl) is not int or not 60 <= ttl <= 14400
            or bool(turn) != bool(secret) or (policy == 'relay' and not turn)):
        raise ImproperlyConfigured('Video relay is not fully configured.')
    if secret and (not isinstance(secret, str) or len(secret) < 32):
        raise ImproperlyConfigured('Video relay is not fully configured.')
    expires = timezone.now() + timedelta(seconds=ttl)
    servers = [{'urls': stun}] if stun and policy != 'relay' else []
    if turn:
        username = f'{int(expires.timestamp())}:{secrets.token_urlsafe(18)}'
        credential = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers.append({'urls': turn, 'username': username, 'credential': credential, 'credentialType': 'password'})
    return {'iceServers': servers, 'expiresAt': expires.isoformat(),
            'relayConfigured': bool(turn), 'iceTransportPolicy': policy}
