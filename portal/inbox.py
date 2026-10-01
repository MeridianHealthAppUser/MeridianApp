"""Practice-scoped staff inbox presentation and safe return destinations."""

from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, Count, Exists, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from care.forms import PatientMessageForm
from care.models import MessageThread, PatientMessage
from care.services import record_audit
from practices.models import CompanyMembership


def staff_inbox_redirect(thread):
    return redirect(f'{reverse("portal:staff-inbox")}?thread={thread.pk}#inbox-conversation')


def _inbox_threads(company):
    latest = PatientMessage.objects.for_company(company).filter(thread_id=OuterRef('pk')).order_by('-created_at', '-pk')
    return MessageThread.objects.for_company(company).filter(
        patient__company=company, patient__is_active=True,
    ).select_related('patient__user', 'patient__assigned_doctor').annotate(
        latest_sender_id=Subquery(latest.values('sender_id')[:1]),
        latest_body=Subquery(latest.values('body')[:1]),
        has_message=Exists(latest),
        last_activity=Coalesce('last_message_at', 'created_at'),
        unread_count=Count('messages', filter=Q(
            messages__company=company,
            messages__sender_id=F('patient__user_id'),
            messages__read_at__isnull=True,
        )),
    ).annotate(
        is_awaiting_reply=Case(
            When(latest_sender_id=F('patient__user_id'), then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        ),
    ).order_by('-last_activity', '-pk')


def staff_inbox_context(request, company, membership, *, selected_thread_id=None, **overrides):
    """Render a conversation only once it is chosen; listing threads never marks them read."""
    from .views import _attach_appointment_proposals

    all_threads = _inbox_threads(company)
    metrics = {
        'total': all_threads.count(),
        'awaiting': all_threads.filter(is_awaiting_reply=True).count(),
        'answered': all_threads.filter(has_message=True, is_awaiting_reply=False).count(),
    }
    filter_status = request.GET.get('status', 'all')
    filtered = all_threads
    if filter_status == 'awaiting':
        filtered = filtered.filter(is_awaiting_reply=True)
    elif filter_status == 'answered':
        filtered = filtered.filter(has_message=True, is_awaiting_reply=False)
    else:
        filter_status = 'all'
    paginator = Paginator(filtered, 20)
    page = paginator.get_page(request.GET.get('page'))
    threads = list(page.object_list)

    requested = selected_thread_id if selected_thread_id is not None else request.GET.get('thread')
    if requested is not None:
        raw_id = str(requested)
        if not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 19:
            raise Http404('Conversation not found.')
        thread_id = int(raw_id)
        if not 0 < thread_id <= 9223372036854775807:
            raise Http404('Conversation not found.')
        selected = get_object_or_404(all_threads, pk=thread_id)
    else:
        # Nothing opens by itself, so new patient messages stay unread until chosen.
        selected = None

    context = {
        'company': company,
        'active_membership': membership,
        'is_staff_inbox': True,
        'threads': threads,
        'paginator': paginator,
        'page_obj': page,
        'is_paginated': page.has_other_pages(),
        'filter_status': filter_status,
        'metrics': metrics,
        'selected_thread': selected,
        'message_threads': [selected] if selected else [],
        'message_form': PatientMessageForm(auto_id='inbox_%s'),
        'can_propose_appointments': False,
    }
    context.update(overrides)
    if selected is None:
        return context

    selected.conversation_messages = list(PatientMessage.objects.for_company(company).filter(
        thread=selected,
    ).select_related('sender').order_by('created_at', 'pk'))
    unread_ids = []
    for message in selected.conversation_messages:
        message.is_from_patient = message.sender_id is not None and message.sender_id == selected.patient.user_id
        if message.is_from_patient and message.read_at is None:
            unread_ids.append(message.pk)
    if unread_ids and request.method == 'GET':
        PatientMessage.objects.for_company(company).filter(
            pk__in=unread_ids, read_at__isnull=True,
        ).update(read_at=timezone.now())
    selected.unread_count = 0
    for thread in threads:
        if thread.pk == selected.pk:
            thread.unread_count = 0
    if request.method == 'GET':
        record_audit(
            company=company, actor=request.user, patient=selected.patient,
            action='message.thread_viewed', target=selected, request=request,
        )
    return _attach_appointment_proposals(
        context, request, company, selected.patient, actor_role='doctor',
        allowed=membership.role == CompanyMembership.Role.DOCTOR,
    )
