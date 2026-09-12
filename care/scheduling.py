"""Appointment changes require agreement from both the clinician and patient."""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, Value
from django.db.models.functions import Cast
from django.utils import timezone

from practices.models import CompanyMembership

from .models import Appointment, AppointmentProposal, AvailabilitySlot, MessageThread, PatientEvent
from .services import post_patient_message, record_audit


def ensure_clinician_available(*, clinician, starts_at, duration_minutes, exclude_appointment=None, company=None):
    """Check actual occupied intervals across practices without disclosing them.

    Callers that write an appointment must hold the clinician User row lock for
    the duration of their transaction. Pending proposals do not occupy slots.
    """
    from .availability import ensure_working_time

    ensure_working_time(
        company=company or getattr(exclude_appointment, 'company', None),
        clinician=clinician, starts_at=starts_at, duration_minutes=duration_minutes,
    )
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    duration = ExpressionWrapper(
        Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)),
        output_field=DurationField(),
    )
    occupied = Appointment.objects.filter(clinician=clinician, starts_at__lt=ends_at).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    )
    if exclude_appointment is not None:
        occupied = occupied.exclude(pk=getattr(exclude_appointment, 'pk', exclude_appointment))
    occupied = occupied.annotate(
        ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField()),
    ).filter(ends_at__gt=starts_at)
    if occupied.exists():
        raise ValidationError(
            'The clinician is no longer available at this time. Please propose another time.',
            code='appointment_overlap',
        )


def ensure_patient_available(*, patient, starts_at, duration_minutes, exclude_appointment=None):
    """An identity cannot be booked with two doctors at once, across practices."""
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    duration = ExpressionWrapper(Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField())
    occupied = Appointment.objects.filter(starts_at__lt=ends_at).exclude(status__in=('cancelled', 'no_show'))
    occupied = occupied.filter(patient__user_id=patient.user_id) if patient.user_id else occupied.filter(patient=patient)
    if exclude_appointment is not None:
        occupied = occupied.exclude(pk=getattr(exclude_appointment, 'pk', exclude_appointment))
    if occupied.annotate(ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField())).filter(ends_at__gt=starts_at).exists():
        raise ValidationError('The patient already has an appointment that overlaps this time. Choose another time.')


def _lock_appointment(appointment_id, company_id):
    # Read the lock key, acquire User before Appointment, then check that the key
    # did not change while waiting. Never acquire a second clinician out of order.
    clinician_id, patient_id, patient_user_id = Appointment.objects.values_list('clinician_id', 'patient_id', 'patient__user_id').get(
        pk=appointment_id, company_id=company_id,
    )
    list(get_user_model().objects.select_for_update().filter(pk__in={clinician_id, patient_user_id} - {None}).order_by('pk'))
    appointment = Appointment.objects.select_for_update().get(pk=appointment_id, company_id=company_id)
    if appointment.clinician_id != clinician_id:
        raise ValidationError('The appointment clinician changed. Reload the conversation and try again.')
    if appointment.patient_id != patient_id or appointment.patient.user_id != patient_user_id:
        raise ValidationError('The appointment patient changed. Reload the conversation and try again.')
    return appointment


def _validate_actor(appointment, actor, actor_role):
    if not getattr(actor, 'is_active', False) or actor_role not in ('doctor', 'patient'):
        raise PermissionDenied('Only this appointment’s clinician and patient can agree to a change.')
    if not appointment.company.is_active or not appointment.patient.is_active:
        raise ValidationError('This appointment is not available because its practice or patient record is inactive.')
    if appointment.patient.company_id != appointment.company_id:
        raise ValidationError('The appointment does not belong to the patient’s practice.')
    if actor_role == 'doctor':
        if actor.pk != appointment.clinician_id or not CompanyMembership.objects.filter(
            user=actor, company_id=appointment.company_id, is_active=True, role=CompanyMembership.Role.DOCTOR,
        ).exists():
            raise PermissionDenied('Only the clinician booked for this appointment can respond as the doctor.')
    elif actor.pk != appointment.patient.user_id:
        raise PermissionDenied('Only the patient who owns this appointment can respond through the patient portal.')


def _validate_participants(appointment):
    if not appointment.patient.user_id or not appointment.patient.user.is_active:
        raise ValidationError('The patient needs an active portal account to agree to a time.')
    if not appointment.clinician.is_active or not CompanyMembership.objects.filter(
        user_id=appointment.clinician_id, company_id=appointment.company_id,
        role=CompanyMembership.Role.DOCTOR, is_active=True,
    ).exists():
        raise ValidationError('The appointment clinician is no longer active in this practice.')


def _validate_future_time(starts_at):
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise ValidationError('Choose a valid appointment date and time with a timezone.')
    if starts_at <= timezone.now():
        raise ValidationError('Choose a future appointment time.')


def _proposal_kind(appointment):
    if appointment.status == Appointment.Status.BOOKED:
        if appointment.starts_at <= timezone.now():
            raise ValidationError('A past appointment cannot be rescheduled. Ask the practice to update its status first.')
        return AppointmentProposal.Kind.RESCHEDULE
    if appointment.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW):
        if appointment.proposals.filter(kind=AppointmentProposal.Kind.REBOOK, status=AppointmentProposal.Status.ACCEPTED).exists():
            raise ValidationError('This appointment has already been rebooked. Use the new appointment for any further changes.')
        return AppointmentProposal.Kind.REBOOK
    raise ValidationError('A completed appointment cannot be rescheduled or rebooked.')


def _validate_thread(thread, appointment):
    if thread.company_id != appointment.company_id or thread.patient_id != appointment.patient_id:
        raise ValidationError('Use a conversation belonging to this appointment’s patient and practice.')
    if thread.is_closed:
        raise ValidationError('This conversation is closed. Start a new conversation to propose a time.')


def _time_label(starts_at):
    return timezone.localtime(starts_at).strftime('%d %B %Y at %H:%M %Z')


def _record_event(proposal, actor, action, title, detail, request):
    event = PatientEvent.objects.create(
        company_id=proposal.company_id,
        patient_id=proposal.patient_id,
        category=PatientEvent.Category.APPOINTMENT,
        title=title,
        detail=detail,
        source_type=proposal._meta.label_lower,
        source_id=str(proposal.pk),
        is_patient_visible=True,
    )
    record_audit(
        company=proposal.company, actor=actor, patient=proposal.patient,
        action=action, target=proposal, request=request,
        metadata={'appointment_id': proposal.appointment_id, 'thread_id': proposal.thread_id, 'event_id': event.pk},
    )


@transaction.atomic
def propose_appointment_time(*, appointment, thread, actor, actor_role, proposed_starts_at, note='', request=None):
    appointment = _lock_appointment(appointment.pk, appointment.company_id)
    _validate_actor(appointment, actor, actor_role)
    _validate_participants(appointment)
    kind = _proposal_kind(appointment)
    _validate_future_time(proposed_starts_at)
    if kind == AppointmentProposal.Kind.RESCHEDULE and proposed_starts_at == appointment.starts_at:
        raise ValidationError('Choose a different time from the current appointment.')
    if not isinstance(note, str) or len(note.strip()) > 2000:
        raise ValidationError('Keep the proposal note to 2,000 characters or fewer.')
    ensure_clinician_available(
        company=appointment.company,
        clinician=appointment.clinician, starts_at=proposed_starts_at,
        duration_minutes=appointment.duration_minutes, exclude_appointment=appointment,
    )
    ensure_patient_available(patient=appointment.patient, starts_at=proposed_starts_at,
                             duration_minutes=appointment.duration_minutes, exclude_appointment=appointment)
    recipient = appointment.patient.user if actor_role == 'doctor' else appointment.clinician
    if actor.pk == recipient.pk:
        raise PermissionDenied('You cannot propose an appointment change to yourself.')
    pending = list(AppointmentProposal.objects.select_for_update().filter(
        appointment=appointment, status=AppointmentProposal.Status.PENDING,
    ).order_by('pk'))
    thread = MessageThread.objects.select_for_update().get(pk=thread.pk)
    _validate_thread(thread, appointment)
    for previous in pending:
        previous.status = AppointmentProposal.Status.SUPERSEDED
        previous.responded_by = actor
        previous.responded_at = timezone.now()
        previous.save(update_fields=('status', 'responded_by', 'responded_at', 'updated_at'))
        _record_event(
            previous, actor, 'appointment.proposal.superseded', 'Appointment proposal replaced',
            'A newer proposed time replaced this proposal. The appointment has not changed.', request,
        )
    proposal = AppointmentProposal(
        company=appointment.company, patient=appointment.patient,
        appointment=appointment, thread=thread, proposed_by=actor, recipient=recipient,
        proposer_role=actor_role, kind=kind,
        original_starts_at=appointment.starts_at, original_status=appointment.status,
        original_clinician=appointment.clinician, original_duration_minutes=appointment.duration_minutes,
        proposed_starts_at=proposed_starts_at, note=note.strip(),
    )
    proposal.full_clean()
    proposal.save()
    body = (
        f'Proposed appointment {"rebooking" if kind == AppointmentProposal.Kind.REBOOK else "reschedule"}: '
        f'{_time_label(proposed_starts_at)} ({appointment.duration_minutes} minutes). '
        f'{recipient.full_name} needs to accept this time before the appointment changes. '
        'This proposal does not reserve a time slot.'
    )
    if pending:
        body += ' This replaces the previous pending proposal.'
    if proposal.note:
        body += f'\n\n{proposal.note}'
    post_patient_message(thread=thread, sender=actor, body=body, request=request)
    _record_event(
        proposal, actor, 'appointment.proposal.created', 'New appointment time proposed',
        f'{_time_label(proposed_starts_at)} is awaiting agreement. The appointment has not changed.', request,
    )
    return proposal


def _validate_response_actor(proposal, appointment, actor, actor_role, decision):
    _validate_actor(appointment, actor, actor_role)
    if decision == 'withdraw':
        if actor.pk != proposal.proposed_by_id or actor_role != proposal.proposer_role:
            raise PermissionDenied('Only the proposer can withdraw this proposal.')
    elif (
        actor.pk == proposal.proposed_by_id
        or actor.pk != proposal.recipient_id
        or actor_role == proposal.proposer_role
    ):
        raise PermissionDenied('Only the other person can accept or decline this proposal.')


def _validate_snapshot(proposal, appointment):
    if (
        proposal.original_starts_at != appointment.starts_at
        or proposal.original_status != appointment.status
        or proposal.original_clinician_id != appointment.clinician_id
        or proposal.original_duration_minutes != appointment.duration_minutes
    ):
        raise ValidationError('The appointment has changed since this time was proposed. Please send a new proposal.')


@transaction.atomic
def respond_to_appointment_proposal(*, proposal, actor, actor_role, decision, request=None):
    status_for_decision = {
        'accept': AppointmentProposal.Status.ACCEPTED,
        'decline': AppointmentProposal.Status.DECLINED,
        'withdraw': AppointmentProposal.Status.WITHDRAWN,
    }
    if decision not in status_for_decision:
        raise ValidationError('Choose accept, decline, or withdraw.')
    pointer = AppointmentProposal.objects.values('appointment_id', 'company_id').get(
        pk=proposal.pk, company_id=proposal.company_id,
    )
    appointment = _lock_appointment(pointer['appointment_id'], pointer['company_id'])
    proposal = AppointmentProposal.objects.select_for_update().get(pk=proposal.pk, company_id=pointer['company_id'])
    _validate_response_actor(proposal, appointment, actor, actor_role, decision)
    target_status = status_for_decision[decision]
    if proposal.status != AppointmentProposal.Status.PENDING:
        if proposal.status == target_status and proposal.responded_by_id == actor.pk:
            return proposal
        raise ValidationError('This proposal has already been answered or replaced. Reload the conversation.')
    thread = MessageThread.objects.select_for_update().get(pk=proposal.thread_id)
    _validate_thread(thread, appointment)
    if decision == 'accept':
        _validate_participants(appointment)
        _validate_snapshot(proposal, appointment)
        _proposal_kind(appointment)
        _validate_future_time(proposal.proposed_starts_at)
        ensure_clinician_available(
            company=appointment.company,
            clinician=appointment.clinician,
            starts_at=proposal.proposed_starts_at,
            duration_minutes=proposal.original_duration_minutes,
            exclude_appointment=appointment,
        )
        ensure_patient_available(patient=appointment.patient, starts_at=proposal.proposed_starts_at,
                                 duration_minutes=appointment.duration_minutes, exclude_appointment=appointment)
        if proposal.kind == AppointmentProposal.Kind.RESCHEDULE:
            appointment.starts_at = proposal.proposed_starts_at
            appointment.save(update_fields=('starts_at', 'updated_at'))
            # The old availability interval no longer has a booking. Updating
            # both values together avoids leaving a reserved slot at the old time.
            AvailabilitySlot.objects.filter(appointment=appointment).update(
                appointment=None, is_booked=False, updated_at=timezone.now(),
            )
            title = 'Appointment rescheduled'
            detail = f'Both participants agreed. The appointment is now {_time_label(proposal.proposed_starts_at)}.'
        else:
            replacement = Appointment(
                company=appointment.company, patient=appointment.patient, clinician=appointment.clinician,
                appointment_type=appointment.appointment_type,
                starts_at=proposal.proposed_starts_at, duration_minutes=proposal.original_duration_minutes,
                status=Appointment.Status.BOOKED,
            )
            replacement.full_clean()
            replacement.save()
            proposal.resulting_appointment = replacement
            title = 'Appointment rebooked'
            detail = (
                f'Both participants agreed to a new appointment on {_time_label(proposal.proposed_starts_at)}. '
                'The original cancelled or missed appointment remains in the history.'
            )
    elif decision == 'decline':
        title = 'Appointment proposal declined'
        detail = f'The proposed time of {_time_label(proposal.proposed_starts_at)} was declined. The appointment has not changed.'
    else:
        title = 'Appointment proposal withdrawn'
        detail = f'The proposed time of {_time_label(proposal.proposed_starts_at)} was withdrawn. The appointment has not changed.'
    proposal.status = target_status
    proposal.responded_by = actor
    proposal.responded_at = timezone.now()
    proposal.full_clean()
    proposal.save(update_fields=('status', 'responded_by', 'responded_at', 'resulting_appointment', 'updated_at'))
    post_patient_message(thread=thread, sender=actor, body=f'{title}. {detail}', request=request)
    _record_event(proposal, actor, f'appointment.proposal.{target_status}', title, detail, request)
    return proposal
