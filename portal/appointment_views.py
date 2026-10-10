"""Appointment changes requested and agreed inside a secure conversation."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.availability import SAST, bookable_start_times
from care.forms import AppointmentProposalForm
from care.models import Appointment, MessageThread
from practices.models import CompanyMembership

from .views import (
    PatientPortalRequiredMixin,
    StaffCompanyRequiredMixin,
    _invalid_patient_form,
    _invalid_staff_form,
    _patient_record_context,
)
from .inbox import staff_inbox_redirect
from .record_messages import render_record_messages


class AppointmentProposalActionMixin:
    http_method_names = ('post',)
    actor_role = None

    def action_company(self):
        return self.patient_company if self.actor_role == 'patient' else self.company

    def is_staff_inbox(self):
        # Staff answer appointment suggestions in the record's messages block ("inbox" is its older name).
        return self.actor_role == 'doctor' and self.request.POST.get('return_to') in ('record', 'inbox')

    def require_doctor(self):
        if self.actor_role == 'doctor' and not self.membership.is_clinician:
            raise PermissionDenied('Only the appointment clinician or patient can suggest or agree a change.')

    def scoped_threads(self):
        queryset = MessageThread.objects.for_company(self.action_company()).filter(
            patient__is_active=True, patient__company=self.action_company(),
        )
        if self.actor_role == 'patient':
            queryset = queryset.filter(patient=self.patient)
        else:
            queryset = queryset.filter(participants=self.request.user)
        return queryset.select_related('patient')

    def from_patient_messages(self):
        return self.actor_role == 'doctor' and self.request.POST.get('return_to') == 'patient_messages'

    def invalid_proposal_form(self, thread, form):
        if self.actor_role == 'patient':
            return _invalid_patient_form(
                self.request, self.patient_company, self.patient, 'proposal_form', form,
                reply_thread_id=thread.pk,
            )
        if self.from_patient_messages():
            from .inbox import render_staff_inbox

            return render_staff_inbox(self.request, self.company, self.membership,
                                      proposal_form=form, failed_form='proposal_form', reply_thread_id=thread.pk)
        if self.is_staff_inbox():
            return render_record_messages(self.request, self.company, self.membership, thread.patient,
                                          proposal_form=form, failed_form='proposal_form', reply_thread_id=thread.pk)
        return _invalid_staff_form(
            self.request, self.company, self.membership, thread.patient, 'proposal_form', form,
            reply_thread_id=thread.pk,
        )

    def success_redirect(self, thread):
        if self.actor_role == 'patient':
            from .patient_views import patient_messages_redirect

            if self.request.POST.get('return_to') == 'appointments':
                return redirect('portal:patient-appointments')

            return patient_messages_redirect(thread)
        if self.from_patient_messages():
            from .inbox import staff_inbox_url
            return redirect(staff_inbox_url(thread))
        if self.is_staff_inbox():
            return staff_inbox_redirect(thread)
        from .patient_workspace import workspace_url
        return redirect(workspace_url(thread.patient, 'messages', thread=thread.pk))


class ProposeAppointmentMixin(AppointmentProposalActionMixin):
    def post(self, request, pk):
        from care.scheduling import propose_appointment_time

        thread = get_object_or_404(self.scoped_threads(), pk=pk, is_closed=False)
        self.require_doctor()
        form = AppointmentProposalForm(
            request.POST, company=self.action_company(), patient=thread.patient,
            thread=thread, actor=request.user, actor_role=self.actor_role,
        )
        if form.is_valid():
            try:
                propose_appointment_time(
                    appointment=form.cleaned_data['appointment'], thread=thread,
                    actor=request.user, actor_role=self.actor_role,
                    proposed_starts_at=form.cleaned_data['proposed_starts_at'],
                    note=form.cleaned_data['note'], request=request,
                )
            except ValidationError as error:
                form.add_error(None, ' '.join(error.messages))
            else:
                messages.success(request, 'Suggested time sent. The appointment will change only after acceptance.')
                return self.success_redirect(thread)
        return self.invalid_proposal_form(thread, form)


class StaffAppointmentProposeView(LoginRequiredMixin, StaffCompanyRequiredMixin, ProposeAppointmentMixin, View):
    actor_role = 'doctor'


class PatientAppointmentProposeView(LoginRequiredMixin, PatientPortalRequiredMixin, ProposeAppointmentMixin, View):
    actor_role = 'patient'


class ProposalTimesMixin(AppointmentProposalActionMixin):
    """The times the suggest-a-new-time form's Time list offers for one of its appointments on a date: the
    appointment's clinician's free quarter hours that day (their working hours or special day, not on leave, clear
    of their other bookings and the patient's), for the appointment's length. Sending the suggestion checks again."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        thread = get_object_or_404(self.scoped_threads(), pk=pk, is_closed=False)
        self.require_doctor()
        # The appointments this person may suggest a time for, as the form offers them.
        appointments = AppointmentProposalForm(
            company=self.action_company(), patient=thread.patient, thread=thread, actor=request.user, actor_role=self.actor_role,
        ).fields['appointment'].queryset
        query = ProposalTimesQuery(request.GET)
        appointment = appointments.filter(pk=query.cleaned_data['appointment']).first() if query.is_valid() else None
        if appointment is None:
            return JsonResponse({'times': []}, status=400)
        times = bookable_start_times(
            company=appointment.company, clinician=appointment.clinician, day=query.cleaned_data['date'],
            duration_minutes=appointment.duration_minutes, patient=appointment.patient, exclude_appointment=appointment,
        )
        if appointment.status == Appointment.Status.BOOKED:  # Its current time isn't a different one.
            current = timezone.localtime(appointment.starts_at, SAST)
            if current.date() == query.cleaned_data['date']:
                times = [at for at in times if at != f'{current:%H:%M}']
        return JsonResponse({'times': times})


class ProposalTimesQuery(forms.Form):
    appointment = forms.IntegerField(min_value=1)
    date = forms.DateField()


@method_decorator(never_cache, name='dispatch')
class StaffProposalTimesView(LoginRequiredMixin, StaffCompanyRequiredMixin, ProposalTimesMixin, View):
    actor_role = 'doctor'


@method_decorator(never_cache, name='dispatch')
class PatientProposalTimesView(LoginRequiredMixin, PatientPortalRequiredMixin, ProposalTimesMixin, View):
    actor_role = 'patient'


class RespondToAppointmentMixin(AppointmentProposalActionMixin):
    def post(self, request, pk):
        from care.models import AppointmentProposal
        from care.scheduling import respond_to_appointment_proposal

        queryset = AppointmentProposal.objects.for_company(self.action_company()).filter(
            patient__is_active=True, patient__company=self.action_company(),
        )
        if self.actor_role == 'patient':
            queryset = queryset.filter(patient=self.patient)
        else:
            queryset = queryset.filter(thread__participants=request.user)
        proposal = get_object_or_404(queryset.select_related('patient', 'thread'), pk=pk)
        self.require_doctor()
        decision = request.POST.get('decision', '')
        try:
            result = respond_to_appointment_proposal(
                proposal=proposal, actor=request.user, actor_role=self.actor_role,
                decision=decision, request=request,
            )
        except ValidationError as error:
            extra = {
                'proposal_error': ' '.join(error.messages),
                'failed_proposal_id': proposal.pk,
                'reply_thread_id': proposal.thread_id,
            }
            if self.actor_role == 'patient':
                from .patient_views import patient_messages_context

                context = patient_messages_context(
                    request, self.patient_company, self.patient, selected_thread_id=proposal.thread_id, **extra,
                )
                template = 'portal/patient_messages.html'
            elif self.from_patient_messages():
                from .inbox import render_staff_inbox

                return render_staff_inbox(request, self.company, self.membership, **extra)
            elif self.is_staff_inbox():
                return render_record_messages(request, self.company, self.membership, proposal.patient, **extra)
            else:
                context = _patient_record_context(request, self.company, self.membership, proposal.patient, **extra)
                template = 'portal/patient_detail.html'
            return render(request, template, context)
        if result.status == AppointmentProposal.Status.ACCEPTED:
            messages.success(request, 'The new appointment time is confirmed.')
        elif result.status == AppointmentProposal.Status.DECLINED:
            messages.success(request, 'Suggestion declined. The appointment is unchanged.')
        elif result.status == AppointmentProposal.Status.WITHDRAWN:
            messages.success(request, 'Suggestion withdrawn. The appointment is unchanged.')
        return self.success_redirect(proposal.thread)


class StaffAppointmentRespondView(LoginRequiredMixin, StaffCompanyRequiredMixin, RespondToAppointmentMixin, View):
    actor_role = 'doctor'


class PatientAppointmentRespondView(LoginRequiredMixin, PatientPortalRequiredMixin, RespondToAppointmentMixin, View):
    actor_role = 'patient'
