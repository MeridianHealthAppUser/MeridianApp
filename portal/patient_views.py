"""Separate patient pages, always scoped to the sign-in's own practice record."""

from datetime import timedelta
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from care.forms import AppointmentProposalForm, PatientMessageForm, PatientThreadForm, WeightEntryForm
from care.models import (
    Appointment, ConsentRecord, MessageThread, PatientEvent, PatientMessage,
    PatientSubscription, WeightEntry,
)
from care.services import record_audit
from practices.models import Patient
from video.access import attach_video_join

from .patient_context import make_patient_context, validate_patient_context
from .video_links import safe_video_link
from .views import PatientPortalRequiredMixin, PatientThreadCreateView, _attach_appointment_proposals


class AppointmentFilterForm(forms.Form):
    status = forms.ChoiceField(choices=(
        ('upcoming', 'Upcoming'), ('history', 'Past and cancelled'), ('all', 'All appointments'),
    ))


class MessageFilterForm(forms.Form):
    status = forms.ChoiceField(choices=(
        ('all', 'All conversations'), ('open', 'Open'), ('closed', 'Closed'),
    ))


class PatientContactForm(forms.Form):
    # Deliberately not a broad profile ModelForm: identity, access and care-team
    # fields are not editable through a patient's contact-details form.
    phone = forms.CharField(label='Contact number', max_length=32, required=False,
                            widget=forms.TextInput(attrs={'type': 'tel', 'autocomplete': 'tel'}))
    city = forms.CharField(max_length=120, required=False,
                           widget=forms.TextInput(attrs={'autocomplete': 'address-level2'}))


def patient_page_context(request, company, patient, section, title):
    return {
        'is_patient_portal': True, 'patient_company': company, 'patient': patient,
        'patient_section': section, 'page_title': title,
        'patient_context': make_patient_context(request, company, patient),
    }


def patient_overview_context(request, company, patient, **overrides):
    context = patient_page_context(request, company, patient, 'overview', 'Your care overview')
    context.update(
        active_subscription=PatientSubscription.objects.for_company(company).filter(
            patient=patient, status=PatientSubscription.Status.ACTIVE,
        ).first(),
        upcoming_appointments=_with_appointment_end(Appointment.objects.for_company(company)).filter(
            patient=patient, scheduled_ends_at__gte=timezone.now(), status=Appointment.Status.BOOKED,
        ).select_related('clinician').order_by('starts_at', 'pk')[:3],
        recent_weights=WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk')[:3],
        unread_messages=PatientMessage.objects.for_company(company).filter(
            thread__company=company, thread__patient=patient, read_at__isnull=True,
        ).exclude(sender=request.user).count(),
        patient_events=PatientEvent.objects.for_company(company).filter(
            patient=patient, is_patient_visible=True,
        ).order_by('-occurred_at', '-pk')[:5],
    )
    context['upcoming_appointments'] = attach_video_join(
        context['upcoming_appointments'], request.user.pk, allowed_role='patient',
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


def patient_appointments_context(request, company, patient):
    context = patient_page_context(request, company, patient, 'appointments', 'Your appointments')
    form, status = _filter(request, AppointmentFilterForm, 'upcoming')
    now = timezone.now()
    upcoming = Q(scheduled_ends_at__gte=now, status=Appointment.Status.BOOKED)
    appointments = _with_appointment_end(Appointment.objects.for_company(company).filter(patient=patient).select_related('clinician'))
    if status == 'upcoming':
        appointments = appointments.filter(upcoming)
    elif status == 'history':
        appointments = appointments.exclude(upcoming)
    elif status is None:
        appointments = appointments.none()
    appointments = appointments.order_by('-starts_at', '-pk') if status == 'history' else appointments.order_by('starts_at', 'pk')
    context.update(_paginate(request, appointments), filter_form=form)
    context['appointments'] = list(context['page_obj'].object_list)
    attach_video_join(context['appointments'], request.user.pk, allowed_role='patient', now=now)
    thread = MessageThread.objects.for_company(company).filter(patient=patient, is_closed=False).first()
    eligible_ids = set()
    if thread:
        proposal_form = AppointmentProposalForm(
            company=company, patient=patient, thread=thread, actor=request.user, actor_role='patient',
        )
        eligible_ids = set(proposal_form.fields['appointment'].queryset.filter(
            pk__in=[appointment.pk for appointment in context['appointments']],
        ).values_list('pk', flat=True))
    for appointment in context['appointments']:
        # Never turn an untrusted imported link into an executable URL.
        appointment.video_link = safe_video_link(appointment.video_link)
        can_reschedule = appointment.status == Appointment.Status.BOOKED and appointment.starts_at > now
        can_rebook = appointment.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)
        if can_reschedule or can_rebook:
            if thread and appointment.pk in eligible_ids:
                appointment.proposal_url = (
                    f'{reverse("portal:patient-messages")}?thread={thread.pk}&appointment={appointment.pk}'
                    f'#appointment-proposals-{thread.pk}'
                )
                appointment.proposal_label = 'Suggest a new time' if can_reschedule else 'Ask to rebook'
            elif not thread:
                appointment.proposal_url = f'{reverse("portal:patient-messages")}#new-thread'
                appointment.proposal_label = 'Message your care team'
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
        last_activity=Coalesce('last_message_at', 'created_at'),
        unread_count=Count('messages', filter=(
            Q(messages__company=company, messages__read_at__isnull=True) & ~Q(messages__sender=request.user)
        )),
    ).order_by('-last_activity', '-pk')
    form, status = _filter(request, MessageFilterForm, 'all')
    filtered = all_threads
    if status in ('open', 'closed'):
        filtered = filtered.filter(is_closed=status == 'closed')
    elif status is None:
        filtered = filtered.none()
    context.update(_paginate(request, filtered))
    threads = list(context['page_obj'].object_list)
    requested = selected_thread_id if selected_thread_id is not None else request.GET.get('thread')
    selected = get_object_or_404(all_threads, pk=_thread_id(requested)) if requested is not None else (threads[0] if threads else None)
    context.update(
        threads=threads, filter_form=form, selected_thread=selected,
        message_threads=[selected] if selected else [],
        message_form=PatientMessageForm(auto_id='patient_reply_%s'),
        thread_form=PatientThreadForm(auto_id='thread_%s'),
    )
    context.update(overrides)
    if selected is None:
        return context
    # Page one is the latest 50 messages; reverse just that page for natural
    # reading order. Other threads and older pages remain unread until opened.
    history = PatientMessage.objects.for_company(company).filter(thread=selected).select_related('sender').order_by('-created_at', '-pk')
    message_page = Paginator(history, 50).get_page(request.GET.get('message_page', 1))
    selected.conversation_messages = list(reversed(list(message_page.object_list)))
    read_ids = [message.pk for message in selected.conversation_messages if message.sender_id != request.user.pk and message.read_at is None]
    # HEAD has no displayed body, and an invalid POST must not cause incidental
    # writes while returning a preserved form draft. Successful POSTs redirect
    # to a GET, which records the actual conversation view.
    if read_ids and request.method == 'GET':
        PatientMessage.objects.for_company(company).filter(thread=selected, pk__in=read_ids, read_at__isnull=True).update(read_at=timezone.now())
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
    if request.method == 'GET':
        record_audit(company=company, actor=request.user, patient=patient, action='message.thread_viewed', target=selected, request=request)
    return _attach_appointment_proposals(context, request, company, patient, actor_role='patient', allowed=True)


def patient_account_context(request, company, patient, **overrides):
    context = patient_page_context(request, company, patient, 'account', 'Your account')
    context.update(
        contact_form=PatientContactForm(initial={'phone': patient.phone, 'city': patient.city}),
        consents=ConsentRecord.objects.for_company(company).filter(patient=patient).order_by('-created_at', '-pk'),
    )
    context.update(overrides)
    return context


class PatientAppointmentsView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return render(request, 'portal/patient_appointments.html', patient_appointments_context(request, self.patient_company, self.patient))


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


class PatientAccountView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'head', 'post', 'options')

    def get(self, request):
        return render(request, 'portal/patient_account.html', patient_account_context(request, self.patient_company, self.patient))

    def post(self, request):
        form = PatientContactForm(request.POST)
        try:
            validate_patient_context(request, self.patient_company, self.patient)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return render(request, 'portal/patient_account.html', patient_account_context(
                request, self.patient_company, self.patient, contact_form=form,
            ))
        with transaction.atomic():
            patient = get_object_or_404(
                Patient.objects.select_for_update(), pk=self.patient.pk, user=request.user,
                company=self.patient_company, company__is_active=True, is_active=True,
            )
            changed = [name for name in ('phone', 'city') if getattr(patient, name) != form.cleaned_data[name]]
            if changed:
                for name in changed:
                    setattr(patient, name, form.cleaned_data[name])
                patient.save(update_fields=(*changed, 'updated_at'))
                record_audit(
                    company=self.patient_company, actor=request.user, patient=patient,
                    action='patient.contact_updated', target=patient, request=request,
                    metadata={'changed_fields': changed},
                )
        messages.success(request, 'Your contact details have been saved for this practice.')
        return redirect('portal:patient-account')
