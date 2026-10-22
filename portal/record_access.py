"""Which patient records a clinician may open, and what they may do there.

Clinicians (the Clinician role) are limited by their link to the patient:

- CHANGE: the patient's current clinician reads and changes the whole record.
- COVER: a clinician booked with the patient as cover (an appointment that isn't cancelled, from when it's
  booked until COVER_DAYS after it) reads the record and writes consultation and general notes, nothing else.
- READ: a clinician assigned to the patient before, or whose cover has ended, reads the record only.
- None: anyone else can't open it.

Practice administrators don't open patient records at all (22 Oct 2026): they work from Calendar & Bookings,
Messages, Operations and Administration. Super Admins are not limited. Conversations stay open to the people in
them either way: a past clinician can still read and reply in a conversation they are part of.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, ClinicianAssignment
from practices.models import CompanyMembership

CHANGE, COVER, READ = 'change', 'cover', 'read'
COVER_DAYS = 14
NOT_ASSIGNED = 'You can only open the records of patients who are, or were, assigned to you.'
NO_RECORDS = 'Patient records aren’t available to practice administrators.'


class RecordAccessDenied(PermissionDenied):
    """Shown as an error toast on the page the person is sent to (see RecordAccessMiddleware), not a 403 page."""

    def __init__(self, message, url):
        super().__init__(message)
        self.message, self.url = message, url


def limited_by_assignment(membership):
    return membership is not None and membership.role == CompanyMembership.Role.DOCTOR


def _booked(user):
    return Appointment.objects.filter(clinician=user).exclude(status=Appointment.Status.CANCELLED)


def cover_since():
    """A cover booking lets its clinician write notes until COVER_DAYS after the appointment."""
    return timezone.now() - timedelta(days=COVER_DAYS)


def cover_appointment(patient, user):
    """The appointment a clinician is covering for this patient: the next one, else the latest still in its window."""
    booked = _booked(user).filter(company_id=patient.company_id, patient_id=patient.pk, starts_at__gte=cover_since())
    return booked.filter(starts_at__gte=timezone.now()).order_by('starts_at').first() or booked.order_by('-starts_at').first()


def no_records(membership):
    return membership is not None and membership.role == CompanyMembership.Role.PRACTICE_ADMIN


def record_access(membership, user, patient):
    if no_records(membership):
        return None
    if not limited_by_assignment(membership) or patient.assigned_doctor_id == user.pk:
        return CHANGE
    if cover_appointment(patient, user) is not None:
        return COVER
    past = ClinicianAssignment.objects.filter(company_id=patient.company_id, patient_id=patient.pk, clinician=user)
    if past.exists() or _booked(user).filter(company_id=patient.company_id, patient_id=patient.pk).exists():
        return READ
    return None


def can_view_record(membership, user, patient):
    return record_access(membership, user, patient) is not None


def can_write_notes(membership, user, patient):
    return record_access(membership, user, patient) in (CHANGE, COVER)


def can_change_record(membership, user, patient):
    return record_access(membership, user, patient) == CHANGE


def openable_patients(patients, user):
    """For the Clinician role: the patients they're assigned to now or were before, or are or were booked with."""
    return patients.filter(
        Q(assigned_doctor=user) | Q(pk__in=ClinicianAssignment.objects.filter(clinician=user).values('patient_id'))
        | Q(pk__in=_booked(user).values('patient_id')),
    )


def label_access(patients, user):
    """Mark the patients on a clinician's list who aren't theirs now: "Covering" or "Past patient"."""
    others = [patient.pk for patient in patients if patient.assigned_doctor_id != user.pk]
    covering = set(_booked(user).filter(patient_id__in=others, starts_at__gte=cover_since()).values_list('patient_id', flat=True))
    for patient in patients:
        patient.record_access_label = (
            '' if patient.assigned_doctor_id == user.pk else 'Covering' if patient.pk in covering else 'Past patient')
    return patients


def read_only_message(patient):
    return f'Only {patient.first_name}’s current clinician can change this record. You can still read it.'


def cover_message(patient):
    clinician = patient.assigned_doctor.full_name if patient.assigned_doctor_id else 'their clinician'
    return f'As cover for {patient.first_name}’s appointment you can write notes; only {clinician} changes the rest of the record.'


def _denied(membership):
    """Never assigned or booked: back to All patients with an error toast. A practice administrator: to Overview."""
    if no_records(membership):
        return RecordAccessDenied(NO_RECORDS, reverse('portal:desktop-dashboard'))
    return RecordAccessDenied(NOT_ASSIGNED, reverse('portal:patient-list'))


def require_record_view(membership, user, patient):
    if not can_view_record(membership, user, patient):
        raise _denied(membership)


def _refuse(membership, user, patient, allowed):
    access = record_access(membership, user, patient)
    if access is None:
        raise _denied(membership)
    if access not in allowed:
        from .patient_workspace import workspace_url
        raise RecordAccessDenied(cover_message(patient) if access == COVER else read_only_message(patient),
                                 workspace_url(patient))


def require_record_notes(membership, user, patient):
    """Writing consultation or general notes: the current clinician, or one covering an appointment."""
    _refuse(membership, user, patient, (CHANGE, COVER))


def require_record_change(membership, user, patient):
    """Anything else that changes the record: the current clinician only."""
    _refuse(membership, user, patient, (CHANGE,))


class RecordAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not isinstance(exception, RecordAccessDenied):
            return None
        from django.contrib import messages
        from django.shortcuts import redirect

        messages.error(request, exception.message)
        return redirect(exception.url)
