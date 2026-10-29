"""Separate patient pages, always scoped to the sign-in's own practice record."""

from datetime import timedelta
from urllib.parse import urlencode

from django import forms
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from care.forms import PatientMessageForm, PatientThreadForm, WeightEntryForm
from care.messaging import mark_patient_messages_read
from care.models import (
    Appointment, MessageThread, PatientMessage, PracticeSettings, WeightEntry,
)
from care.services import record_audit
from video.access import attach_video_join

from .inbox import latest_from, search_text, search_threads
from .patient_context import make_patient_context
from .people_tags import in_join_order
from .realtime_context import records_conversation_view
from .video_links import safe_video_link
from .views import PatientPortalRequiredMixin, PatientThreadCreateView


APPOINTMENT_STATUS_CHOICES = (('upcoming', 'Upcoming'), ('history', 'Past and cancelled'), ('all', 'All appointments'))
APPOINTMENT_WHEN_CHOICES = (('', 'Any time'), ('today', 'Today'), ('week', 'This week'), ('month', 'This month'))
# The appointment just booked, shown once at the top of My Appointments (portal/patient_care_views.py).
JUST_BOOKED = 'patient_just_booked'


class AppointmentFilterForm(forms.Form):
    status = forms.ChoiceField(choices=APPOINTMENT_STATUS_CHOICES)


def _when_bounds(when):
    """Today, this week (Monday to Sunday) or this calendar month, in SAST."""
    from care.availability import day_bounds

    today = timezone.localdate()
    if when == 'today':
        first, last = today, today
    elif when == 'week':
        first = today - timedelta(days=today.weekday())
        last = first + timedelta(days=6)
    else:
        first = today.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return day_bounds(first)[0], day_bounds(last)[1]


class MessageFilterForm(forms.Form):
    status = forms.ChoiceField(choices=(
        ('all', 'All conversations'), ('open', 'Open'), ('closed', 'Closed'),
    ))


# Five sidebar destinations. Pages without their own entry light up the one
# they belong to; account and privacy pages live in the account menu instead.
PATIENT_NAV_GROUPS = {
    'overview': 'home', 'progress': 'home', 'updates': 'home',
    'treatment': 'treatment', 'subscription': 'treatment', 'labs': 'treatment',
    'appointments': 'appointments', 'messages': 'messages', 'medical_profile': 'medical_info',
    'pharmacy': 'treatment', 'orders': 'treatment',
    'billing': 'treatment',
}


def unread_patient_messages(request, company, patient):
    return PatientMessage.objects.for_company(company).filter(
        thread__company=company, thread__patient=patient, read_at__isnull=True,
    ).exclude(sender=request.user).count()


def patient_page_context(request, company, patient, section, title):
    return {
        'is_patient_portal': True, 'patient_company': company, 'patient': patient,
        'patient_section': section, 'patient_nav': PATIENT_NAV_GROUPS.get(section, ''), 'page_title': title,
        'patient_context': make_patient_context(request, company, patient),
        'patient_unread_messages': unread_patient_messages(request, company, patient),
    }


def patient_overview_context(request, company, patient, **overrides):
    from .patient_progress import weight_chart, weight_reference
    from .patient_summary import current_authorization, current_plan, latest_authorization, next_consult, next_delivery, weight_reference_course

    context = patient_page_context(request, company, patient, 'overview', 'Home')
    plan = current_plan(company, patient)
    authorization = current_authorization(company, patient, plan)
    weights = WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk')
    latest, first = weights.first(), weights.last()
    last = None if authorization else latest_authorization(company, patient)
    change = latest.weight_kg - first.weight_kg if latest and first and latest.pk != first.pk else None
    today = timezone.localdate()
    reference_authorization = authorization or last
    course = weight_reference_course(company, patient, reference_authorization) if first else None
    baseline = first
    if course:
        # Prefer a recorded starting weight, otherwise the first later check-in.
        # The anchor is queried outside the latest-300 slice so it never resets.
        baseline = weights.filter(recorded_on__lte=course['started_on']).first() or first
    chart_rows = list(weights[:301])
    context.update(
        current_authorization=authorization, last_authorization=last,
        last_authorization_state=('' if last is None else 'upcoming' if last.starts_on > today and last.status == 'active'
                                  else 'paused' if last.status == 'paused' else 'ended'),
        next_consult=next_consult(request, company, patient),
        next_delivery=next_delivery(company, patient),
        care_doctor=patient.assigned_doctor or (authorization.prescribed_by if authorization else None),
        latest_weight=latest, first_weight=first,
        weight_change=change, weight_change_abs=abs(change) if change is not None else None,
        weight_chart=weight_chart(reversed(chart_rows[:300]), reference=weight_reference(baseline, course)),
        chart_is_limited=len(chart_rows) > 300,
        weight_form=WeightEntryForm(company=company, patient=patient, recorded_by=request.user,
                                    initial={'recorded_on': timezone.localdate()}),
    )
    context.update(overrides)
    return context


def _filter(request, form_class, default):
    form = form_class({'status': request.GET.get('status', default)}, auto_id='filter_%s')
    # Invalid filter values never broaden the result set.
    return form, form.cleaned_data['status'] if form.is_valid() else None


def _paginate(request, queryset, per_page=20):
    page = Paginator(queryset, per_page).get_page(request.GET.get('page'))
    return {
        'page_obj': page, 'is_paginated': page.has_other_pages(),
        'pagination_query': urlencode({'status': request.GET['status']}) if 'status' in request.GET else '',
    }


def _with_appointment_end(queryset):
    # Ongoing booked consultations remain on Upcoming until the scheduled end.
    duration = ExpressionWrapper(Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField())
    return queryset.annotate(scheduled_ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField()))


def _appointment_list(request, company, patient, now):
    """My Appointments' table and its Filters: Status (Upcoming to start with), Date, Clinician and Appointment type."""
    from django.contrib.auth import get_user_model
    from .filter_menus import choice_section, filter_menu, ticked_ids, ticked_values

    form, status = _filter(request, AppointmentFilterForm, 'upcoming')
    when = request.GET.get('when', '') if request.GET.get('when', '') in dict(APPOINTMENT_WHEN_CHOICES) else ''
    clinicians = ticked_ids(request.GET.getlist('clinician'))
    types = [value for value in ticked_values(request.GET.getlist('type')) if value in Appointment.Type.values]
    yours = Appointment.objects.for_company(company).filter(patient=patient)
    upcoming = Q(scheduled_ends_at__gte=now, status=Appointment.Status.BOOKED)
    appointments = _with_appointment_end(yours.select_related('clinician'))
    if status == 'upcoming':
        appointments = appointments.filter(upcoming)
    elif status == 'history':
        appointments = appointments.exclude(upcoming)
    elif status is None:
        appointments = appointments.none()
    if when:
        start, end = _when_bounds(when)
        appointments = appointments.filter(starts_at__gte=start, starts_at__lt=end)
    if clinicians:
        appointments = appointments.filter(clinician_id__in=clinicians)
    if types:
        appointments = appointments.filter(appointment_type__in=types)
    appointments = appointments.order_by('-starts_at', '-pk') if status == 'history' else appointments.order_by('starts_at', 'pk')
    context = _paginate(request, appointments)
    kept = {key: value for key, value in {'status': request.GET.get('status', ''), 'when': when, 'clinician': clinicians,
                                         'type': types}.items() if value}
    context['pagination_query'] = urlencode(kept, doseq=True)
    # Only the clinicians they've had appointments with.
    people = get_user_model().objects.filter(pk__in=yours.values('clinician_id')).order_by('first_name', 'last_name', 'pk')
    action = reverse('portal:patient-appointments')
    context.update(filter_form=form, filter_menu=filter_menu(
        action=action, clear_url=action, label='Filter appointments', form=form, sections=[
            choice_section('status', 'Status', APPOINTMENT_STATUS_CHOICES, [request.GET.get('status', 'upcoming')], pick=True,
                           default='upcoming', errors=form.errors.get('status', ())),
            choice_section('when', 'Date', APPOINTMENT_WHEN_CHOICES, [when], icon='date', pick=True, default=''),
            choice_section('clinician', 'Clinician', [(person.pk, person.full_name) for person in people], clinicians, icon='person'),
            choice_section('type', 'Appointment type', Appointment.Type.choices, types, icon='type'),
        ]))
    context['appointments'] = list(context['page_obj'].object_list)
    context['video_server_time'] = now
    attach_video_join(context['appointments'], request.user.pk, allowed_role='patient', now=now)
    return context


def patient_appointments_context(request, company, patient):
    from .patient_summary import RENEWAL_WINDOW_DAYS, current_authorization, current_plan, next_consult

    context = patient_page_context(request, company, patient, 'appointments', 'My Appointments')
    consult = next_consult(request, company, patient)
    authorization = current_authorization(company, patient, current_plan(company, patient))
    renewal_due = None
    if authorization is not None and consult is None and (authorization.expires_on - timezone.localdate()).days <= RENEWAL_WINDOW_DAYS:
        renewal_due = authorization.expires_on
    context.update(next_consult=consult, renewal_due=renewal_due)
    now = timezone.now()
    # The appointment just booked, at the top of the page this once.
    just_booked = Appointment.objects.for_company(company).filter(
        pk=request.session.pop(JUST_BOOKED, None), patient=patient, status=Appointment.Status.BOOKED,
    ).select_related('clinician').first()
    if just_booked is not None:
        attach_video_join([just_booked], request.user.pk, allowed_role='patient', now=now)
    context['just_booked'] = just_booked
    context.update(_appointment_list(request, company, patient, now))
    for appointment in context['appointments']:
        # Never turn an untrusted imported link into an executable URL.
        appointment.video_link = safe_video_link(appointment.video_link)
    return context


def patient_progress_context(request, company, patient, **overrides):
    from .patient_progress import weight_history_context

    context = patient_page_context(request, company, patient, 'progress', 'Your progress')
    weights = WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk')
    latest, first = weights.first(), weights.last()
    context.update(weight_history_context(request, weights))
    context.update(
        weights=context['page_obj'].object_list, latest_weight=latest, first_weight=first,
        weight_change=latest.weight_kg - first.weight_kg if latest and first else None,
        weight_form=WeightEntryForm(company=company, patient=patient, recorded_by=request.user),
    )
    context.update(overrides)
    return context


def patient_messages_redirect(thread):
    return redirect(f'{reverse("portal:patient-messages")}?thread={thread.pk}#patient-conversation')


def _thread_id(raw):
    value = str(raw)
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise Http404('Conversation not found.')
    number = int(value)
    if not 0 < number <= 9223372036854775807:
        raise Http404('Conversation not found.')
    return number


def patient_messages_context(request, company, patient, *, selected_thread_id=None, **overrides):
    context = patient_page_context(request, company, patient, 'messages', 'Your messages')
    latest = PatientMessage.objects.for_company(company).filter(thread_id=OuterRef('pk')).order_by('-created_at', '-pk')
    all_threads = MessageThread.objects.for_company(company).filter(patient=patient).annotate(
        latest_body=Subquery(latest.values('body')[:1]),
        latest_sender_id=Subquery(latest.values('sender_id')[:1]),
        latest_sender_first_name=Subquery(latest.values('sender__first_name')[:1]),
        last_activity=Coalesce('last_message_at', 'created_at'),
        unread_count=Count('messages', filter=(
            Q(messages__company=company, messages__read_at__isnull=True) & ~Q(messages__sender=request.user)
        )),
    ).prefetch_related(in_join_order('participants')).order_by('-last_activity', '-pk')
    form, status = _filter(request, MessageFilterForm, 'all')
    filtered = all_threads
    if status in ('open', 'closed'):
        filtered = filtered.filter(is_closed=status == 'closed')
    elif status is None:
        filtered = filtered.none()
    # A search narrows the list; the open conversation stays open even when it doesn't match.
    search = search_text(request.GET.get('q'))
    if search:
        filtered = search_threads(filtered, company, search)
    context.update(_paginate(request, filtered))
    threads = list(context['page_obj'].object_list)
    for thread in threads:
        people = list(thread.participants.all())
        thread.people = ', '.join(person.full_name for person in people)
        # A clinician is named before their message only when more than one is in the conversation.
        thread.latest_from = latest_from(thread, request.user, unnamed={people[0].pk} if len(people) == 1 else ())
    requested = selected_thread_id if selected_thread_id is not None else request.GET.get('thread')
    # A new message opens in the conversation pane. Links elsewhere in the
    # portal can suggest a subject, which the patient can change before sending.
    composing = request.GET.get('compose') == '1' or overrides.get('failed_form') == 'thread_form'
    suggested_subject = ' '.join(request.GET.get('subject', '').split())[:120] if composing else ''
    suggested_to = request.GET.get('to', '') if composing and request.GET.get('to', '').isdecimal() else ''
    initial = {key: value for key, value in (('subject', suggested_subject), ('recipient', suggested_to)) if value}
    # Nothing opens by itself: replies stay unread until the patient chooses a conversation.
    selected = get_object_or_404(all_threads, pk=_thread_id(requested)) if requested is not None else None
    settings_row = PracticeSettings.objects.for_company(company).first()
    context.update(
        threads=threads, filter_form=form, selected_thread=selected,
        search=search, search_hidden={'status': status or 'all'}, search_action=reverse('portal:patient-messages'),
        message_threads=[selected] if selected else [],
        message_form=PatientMessageForm(auto_id='patient_reply_%s'),
        thread_form=PatientThreadForm(auto_id='thread_%s', patient=patient, initial=initial or None),
        composing=composing or (selected is None and not all_threads.exists()),
        subject_suggestions=('Side effects', 'My dose', 'My delivery', 'My appointment', 'Something else'),
        support_email=settings_row.support_email if settings_row else '',
    )
    context.update(overrides)
    if selected is None:
        return context
    # Page one is the latest 50 messages; reverse just that page for natural
    # reading order. Other threads and older pages remain unread until opened.
    history = PatientMessage.objects.for_company(company).filter(thread=selected).select_related('sender').order_by('-created_at', '-pk')
    message_page = Paginator(history, 50).get_page(request.GET.get('message_page', 1))
    selected.conversation_messages = list(reversed(list(message_page.object_list)))
    selected.participant_rows = list(selected.participant_links.select_related('user', 'added_by'))
    read_ids = [message.pk for message in selected.conversation_messages if message.sender_id != request.user.pk and message.read_at is None]
    # HEAD has no displayed body, and an invalid POST must not cause incidental
    # writes while returning a preserved form draft. Successful POSTs redirect
    # to a GET, which records the actual conversation view.
    if read_ids and records_conversation_view(request):
        mark_patient_messages_read(selected, request.user, read_ids)
        context['patient_unread_messages'] = unread_patient_messages(request, company, patient)
    selected.unread_count = history.filter(read_at__isnull=True).exclude(sender=request.user).count()
    for thread in threads:
        if thread.pk == selected.pk:
            thread.unread_count = selected.unread_count

    def history_url(number):
        return reverse('portal:patient-messages') + '?' + urlencode({
            'thread': selected.pk, 'status': status or 'all', 'page': context['page_obj'].number,
            'message_page': number,
        })

    context.update(
        message_page_obj=message_page,
        older_messages_url=history_url(message_page.next_page_number()) if message_page.has_next() else None,
        newer_messages_url=history_url(message_page.previous_page_number()) if message_page.has_previous() else None,
    )
    if records_conversation_view(request):
        record_audit(company=company, actor=request.user, patient=patient, action='message.thread_viewed', target=selected, request=request)
    return context


class PatientAppointmentsView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from .patient_care_views import booking_context

        context = patient_appointments_context(request, self.patient_company, self.patient)
        context.update(booking_context(request, self.patient_company, self.patient))
        return render(request, 'portal/patient_appointments.html', context)


class PatientAppointmentVideoStatusView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """Refresh only a bounded visible page, with the same fresh policy as room access."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        raw = request.GET.getlist('id')
        if not raw or len(raw) > 50 or any(
            not value.isascii() or not value.isdecimal() or len(value) > 19
            or not 0 < int(value) < 2 ** 63 for value in raw
        ):
            return JsonResponse({'error': 'Supply between 1 and 50 valid appointment IDs.'}, status=400)
        now = timezone.now()
        appointments = Appointment.objects.for_company(self.patient_company).filter(
            patient=self.patient, pk__in={int(value) for value in raw},
        )
        rows = attach_video_join(appointments, request.user.pk, allowed_role='patient', now=now)
        return JsonResponse({
            'scope': request.realtime_scope,
            'serverTime': now.isoformat(),
            'appointments': [{
                'id': item.pk,
                'joinUrl': item.video_room_url,
                'opensAt': item.video_join_opens_at.isoformat() if item.video_join_opens_at else None,
                'closesAt': item.video_join_closes_at.isoformat() if item.video_join_closes_at else None,
                'message': item.video_join_message,
            } for item in rows],
        })


class PatientProgressView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return render(request, 'portal/patient_progress.html', patient_progress_context(request, self.patient_company, self.patient))


class PatientMessagesView(PatientThreadCreateView):
    # Preserve legacy POST /patient/messages/ while giving the inbox its own GET
    # page. New templates use the unambiguous /patient/messages/new/ endpoint.
    http_method_names = ('get', 'head', 'post', 'options')

    def get(self, request):
        return render(request, 'portal/patient_messages.html', patient_messages_context(request, self.patient_company, self.patient))
