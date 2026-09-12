from django.conf import settings


def call_notifications(request):
    return {'video_notifications_enabled': bool(
        getattr(getattr(request, 'user', None), 'is_authenticated', False)
        and settings.VIDEO_ENABLED
        and (settings.DEBUG or settings.VIDEO_REDIS_URL)
    )}
