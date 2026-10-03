"""Who can see and write in patient conversations.

A conversation is private to the patient and the staff members taking part in
it. Nobody else in the practice, including administrators and Super Admins,
can list, read or reply to it. Clinicians join only when a participant adds them.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import CompanyMembership
from .models import Appointment, ClinicianAssignment, MessageThread, MessageThreadParticipant
from .patient_assignment import active_doctors
from .services import post_patient_message, record_audit


def staff_threads(company, user):
    """Conversations in this practice that the staff member takes part in."""
    return MessageThread.objects.for_company(company).filter(
        participants=user, patient__company=company, patient__is_active=True,
    )


def is_participant(thread, user):
    return MessageThreadParticipant.objects.filter(thread_id=thread.pk, user_id=user.pk).exists()


def add_participant(thread, user, added_by=None):
    link, _ = MessageThreadParticipant.objects.get_or_create(
        company_id=thread.company_id, thread=thread, user=user, defaults={'added_by': added_by},
    )
    return link


def recipient_choices(patient):
    """The assigned clinician first, then past clinicians and anyone the patient is booked with."""
    past = ClinicianAssignment.objects.for_company(patient.company).filter(patient=patient).values_list('clinician_id', flat=True)
    booked = Appointment.objects.for_company(patient.company).filter(
        patient=patient, status=Appointment.Status.BOOKED, starts_at__gte=timezone.now(),
    ).values_list('clinician_id', flat=True)
    ids = {*past, *booked, patient.assigned_doctor_id} - {None}
    clinicians = list(active_doctors(patient.company).filter(pk__in=ids))
    clinicians.sort(key=lambda user: user.pk != patient.assigned_doctor_id)
    return clinicians


@transaction.atomic
def start_conversation(*, patient, opened_by, subject, body, recipient=None, request=None):
    """Staff open a conversation with themselves in it; a patient addresses one of their clinicians."""
    if opened_by.pk == patient.user_id:
        if recipient is None or recipient.pk not in {clinician.pk for clinician in recipient_choices(patient)}:
            raise ValidationError('Choose one of your clinicians to message.')
        participant = recipient
    else:
        participant = opened_by
    thread = MessageThread(company=patient.company, patient=patient, subject=subject, opened_by=opened_by)
    thread.full_clean()
    thread.save()
    add_participant(thread, participant)
    post_patient_message(thread=thread, sender=opened_by, body=body, request=request)
    return thread


@transaction.atomic
def hand_over(*, thread, actor, clinician, request=None):
    """A participant adds a clinician, who can then read the whole conversation and reply."""
    thread = MessageThread.objects.select_for_update().select_related('patient').get(pk=thread.pk)
    if not is_participant(thread, actor):
        raise PermissionDenied('Only people in this conversation can add someone to it.')
    if thread.is_closed:
        raise ValidationError('Reopen the conversation before adding a clinician.')
    if clinician is None or not active_doctors(thread.company).filter(pk=clinician.pk).exists():
        raise ValidationError('Choose an active clinician in this practice.')
    if is_participant(thread, clinician):
        raise ValidationError(f'{clinician.full_name} is already in this conversation.')
    link = add_participant(thread, clinician, added_by=actor)
    record_audit(company=thread.company, actor=actor, patient=thread.patient, action='message.participant_added',
                 target=thread, request=request, metadata={'user_id': clinician.pk})
    return link


def adopt_unattended_threads(patient, clinician):
    """Conversations nobody is in yet (from before participants existed) go to the new clinician."""
    for thread in MessageThread.objects.for_company(patient.company).filter(patient=patient, participants__isnull=True):
        add_participant(thread, clinician)


def require_participant(thread, user):
    if not (CompanyMembership.objects.filter(company_id=thread.company_id, user_id=user.pk, is_active=True).exists()
            and is_participant(thread, user)):
        raise PermissionDenied('You are not part of this conversation.')
