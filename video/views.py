"""Appointment-owned room preflight and short-lived ICE configuration."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.template.loader import get_template
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST, require_safe

from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .access import VideoAccessDenied, resolve_room_access


def _access(request, appointment_id):
    try:
        return resolve_room_access(request.user.pk, appointment_id)
    except VideoAccessDenied:
        raise Http404('This consultation room is not available.') from None


def _service_ready():
    # HTTP and signalling must not promise a multi-worker room without the
    # configured shared channel/presence infrastructure in production.
    return settings.DEBUG or bool(getattr(settings, 'VIDEO_REDIS_URL', ''))


def _return_url(request, access):
    if access.role == 'doctor':
        if request.session.get(ACTIVE_COMPANY_SESSION_KEY) == access.company_id:
            return reverse('portal:appointment-detail', args=[access.appointment_id])
        return reverse('portal:staff-schedule')
    if request.session.get(ACTIVE_PATIENT_COMPANY_SESSION_KEY) == access.company_id:
        return reverse('portal:patient-appointment-detail', args=[access.appointment_id])
    return reverse('portal:patient-appointments')


@never_cache
@login_required
@require_safe
def room(request, appointment_id):
    access = _access(request, appointment_id)
    if not _service_ready():
        return HttpResponse('Video consultations are temporarily unavailable. Please contact the practice.', status=503)
    # This standalone page deliberately avoids workspace context processors:
    # they choose fallback practices and can mutate a session on a plain GET.
    context = {
        'room_access': access,
        'csrf_token': get_token(request),
        'video_config': {
            'appointmentId': access.appointment_id, 'myUserId': access.user_id,
            'peerName': access.peer_name, 'companyName': access.company_name,
            'signalPath': f'/ws/video/appointments/{access.appointment_id}/',
            'iceConfigUrl': reverse('video:ice-config', args=[access.appointment_id]),
            'returnUrl': _return_url(request, access),
            'startsAt': access.starts_at.isoformat(), 'endsAt': access.ends_at.isoformat(),
            'joinClosesAt': access.join_closes_at.isoformat(),
        },
    }
    response = HttpResponse(get_template('video/room.html').render(context))
    response['Referrer-Policy'] = 'no-referrer'
    response['Permissions-Policy'] = 'camera=(self), microphone=(self), display-capture=(self)'
    return response


@never_cache
@login_required
@require_POST
@csrf_protect
def ice_config(request, appointment_id):
    access = _access(request, appointment_id)
    if not _service_ready():
        return JsonResponse({'error': 'Video consultations are temporarily unavailable.'}, status=503)
    from .ice import build_ice_config
    try:
        configuration = build_ice_config(access)
    except ImproperlyConfigured:
        return JsonResponse({'error': 'Video consultations are temporarily unavailable.'}, status=503)
    return JsonResponse(configuration)


def appointment_video_context(request, appointment, *, allowed_role):
    """Called only after the containing detail page's own tenant/role lookup."""
    from .access import attach_video_join
    from .models import CallSession

    attach_video_join([appointment], request.user.pk, allowed_role=allowed_role)
    sessions = CallSession.objects.filter(
        company_id=appointment.company_id, appointment_id=appointment.pk,
        patient_id=appointment.patient_id,
    ).select_related('doctor').order_by('-started_at', '-pk')
    page = Paginator(sessions, 20).get_page(request.GET.get('call_page'))
    return {'call_history_page': page, 'call_sessions': page.object_list}
