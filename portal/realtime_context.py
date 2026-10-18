"""Request context shared by message page rendering and live updates."""

from django.http import JsonResponse


def is_realtime_snapshot(request):
    return request.headers.get('X-Meridian-Realtime') == 'snapshot'


def records_conversation_view(request):
    """Background snapshots render the same authorized page without marking it seen."""
    return request.method == 'GET' and not is_realtime_snapshot(request)


def staff_scope(company, user):
    return f'staff:{company.pk}:{user.pk}'


def patient_scope(company, patient, user):
    return f'patient:{company.pk}:{patient.pk}:{user.pk}'


def changed_scope_response(request):
    expected = request.headers.get('X-Meridian-Scope')
    if expected and expected != request.realtime_scope:
        return JsonResponse({'error': 'Your active practice or account has changed.', 'scope': request.realtime_scope}, status=409)
    return None
