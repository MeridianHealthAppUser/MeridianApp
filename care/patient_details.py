"""A patient's details as the practice holds them, corrected by a Super Admin from Edit profile on the record.

Documents already issued keep the details they were issued with: an invoice its billed_to, a parcel the address it
was booked (booked_address) and dispatched (dispatch_snapshot) with, a PathCare form its stored PDF and a compounding
record its snapshot. The delivery address is kept separately (care.addresses). Each change is audited with the fields
that changed, never their values. The Prescribing doctor is set here too, through care.patient_assignment, which keeps
its own audit and clinician history.
"""

from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import CompanyMembership, Patient

from .addresses import clean_phone
from .models import PatientEvent
from .patient_assignment import assign_doctor
from .services import record_audit

FIELDS = ('title', 'first_name', 'last_name', 'date_of_birth', 'id_number', 'phone', 'city')
LABELS = {'title': 'title', 'first_name': 'first name', 'last_name': 'last name', 'date_of_birth': 'date of birth',
          'id_number': 'ID or passport number', 'phone': 'phone number', 'city': 'city', 'assigned_doctor': 'Prescribing doctor'}
ONLY_SUPER_ADMIN = 'Only a Super Admin can edit a patient’s profile.'


def can_edit_details(membership):
    return membership is not None and membership.is_active and membership.role == CompanyMembership.Role.SUPER_ADMIN


def _clean(company, patient, data):
    values = {name: ' '.join(str(data.get(name) or '').split()) for name in FIELDS if name != 'date_of_birth'}
    values['date_of_birth'] = data.get('date_of_birth') or None
    errors = {}
    if values['title'] not in ('', *Patient.Title.values):
        errors['title'] = 'Choose one of the listed titles.'
    for name in ('first_name', 'last_name'):
        if not values[name]:
            errors[name] = f'Enter the patient’s {LABELS[name]}.'
    born = values['date_of_birth']
    if born is not None and not date(1900, 1, 1) <= born <= timezone.localdate():
        errors['date_of_birth'] = 'Enter a date of birth between 1900 and today.'
    if values['id_number'] and Patient.objects.filter(company=company, id_number=values['id_number']).exclude(pk=patient.pk).exists():
        errors['id_number'] = 'Another patient in this practice already has this ID or passport number.'
    if values['phone']:
        try:
            values['phone'] = clean_phone(values['phone'])
        except ValidationError:
            errors['phone'] = 'Enter a phone number, such as 082 555 0114.'
    for name in FIELDS:
        limit = Patient._meta.get_field(name).max_length
        if limit and len(values[name] or '') > limit:
            errors.setdefault(name, f'Keep the {LABELS[name]} to {limit} characters.')
    if errors:
        raise ValidationError(errors)
    return values


def changed_text(changed):
    """The changed fields in words ("name and phone number"), first and last name together as name."""
    names = ['name' if name in ('first_name', 'last_name') else LABELS[name] for name in changed]
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else f'{", ".join(names[:-1])} and {names[-1]}'


@transaction.atomic
def update_patient_details(*, company, patient, actor, data, request=None):
    """Save the patient's changed details and, when data has assigned_doctor, their Prescribing doctor. Returns the
    names of the fields that changed, or [] when none did."""
    membership = CompanyMembership.objects.filter(company=company, user=actor, is_active=True).first()
    if not (actor.is_active and company.is_active and can_edit_details(membership)):
        raise PermissionDenied(ONLY_SUPER_ADMIN)
    doctor = data.get('assigned_doctor')
    reassigned = 'assigned_doctor' in data and getattr(doctor, 'pk', None) != patient.assigned_doctor_id
    if reassigned:
        # Before the patient is locked below: assign_doctor locks the practice, then the patient.
        assign_doctor(patient=patient, actor=actor, doctor=doctor, expected_updated=patient.updated_at.isoformat(),
                      request=request)
    locked = Patient.objects.select_for_update().filter(pk=patient.pk, company=company, is_active=True).first()
    if locked is None:
        raise PermissionDenied('This patient isn’t available.')
    values = _clean(company, locked, data)
    changed = [name for name in FIELDS if getattr(locked, name) != values[name]]
    if not changed:
        return ['assigned_doctor'] if reassigned else []
    for name in changed:
        setattr(locked, name, values[name])
    locked.save(update_fields=(*changed, 'updated_at'))
    # A login that is only this patient's carries their name too; a staff member's name is kept in Users and roles.
    user = locked.user
    if user is not None and {'first_name', 'last_name'} & set(changed) and not CompanyMembership.objects.filter(user=user).exists():
        user.first_name, user.last_name = locked.first_name, locked.last_name
        user.save(update_fields=('first_name', 'last_name'))
    record_audit(company=company, actor=actor, patient=locked, action='patient.details_updated', target=locked,
                 request=request, metadata={'changed_fields': changed})
    PatientEvent.objects.create(
        company=company, patient=locked, category=PatientEvent.Category.ADMIN, title='Your details were updated',
        detail=f'Your practice updated your {changed_text(changed)}. Check them on My profile.',
        source_type='practices.patient', source_id=str(locked.pk))
    return [*changed, 'assigned_doctor'] if reassigned else changed
