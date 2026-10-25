"""Patients' delivery addresses: where their parcels go and who the courier calls (DeliveryAddress).

The patient keeps their own on My profile and gives it when they start their subscription. Practice administrators
and Super Admins can change it for them, from the Weekly shipping list, when a patient asks. Each change is audited
with the fields that changed, never their values.
"""

import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from practices.models import Company, CompanyMembership, Patient

from .models import PROVINCES, DeliveryAddress, PatientEvent
from .services import record_audit

FIELDS = ('recipient', 'business', 'street', 'suburb', 'city', 'province', 'postal_code', 'phone', 'instructions')
REQUIRED = ('street', 'suburb', 'city', 'province', 'postal_code', 'phone')
STAFF_ROLES = (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)


def address_for(patient):
    return DeliveryAddress.objects.filter(company_id=patient.company_id, patient=patient).first()


def clean_phone(value):
    """A South African or international number: digits, spaces, brackets, dashes and a leading +, 9 to 15 digits."""
    value = ' '.join(str(value or '').split())
    digits = re.sub(r'\D', '', value)
    if not re.fullmatch(r'\+?[\d\s()-]+', value) or not 9 <= len(digits) <= 15:
        raise ValidationError('Enter a contact number the courier can call, such as 082 555 0114.')
    return value


def _clean(data):
    values = {name: ' '.join(str(data.get(name) or '').split()) for name in FIELDS}
    missing = [name for name in REQUIRED if not values[name]]
    if missing:
        labels = [str(DeliveryAddress._meta.get_field(name).verbose_name).lower() for name in missing]
        raise ValidationError(f'Add the {", ".join(labels)}.')
    if values['province'] not in dict(PROVINCES):
        raise ValidationError('Choose a province.')
    if not re.fullmatch(r'\d{4}', values['postal_code']):
        raise ValidationError('Enter the four-digit postal code.')
    values['phone'] = clean_phone(values['phone'])
    for name in FIELDS:
        limit = DeliveryAddress._meta.get_field(name).max_length
        if len(values[name]) > limit:
            raise ValidationError(f'Keep the {DeliveryAddress._meta.get_field(name).verbose_name.lower()} to {limit} characters.')
    return values


def can_manage(company, actor, patient):
    """The patient themself, or a practice administrator or Super Admin of the practice, or one of its doctors when
    doctors ship parcels (Practice settings)."""
    from .operations import doctors_ship

    if not getattr(actor, 'is_active', False):
        return False
    if patient.user_id == actor.pk:
        return True
    roles = STAFF_ROLES + ((CompanyMembership.Role.DOCTOR,) if doctors_ship(company) else ())
    return CompanyMembership.objects.filter(company=company, user=actor, is_active=True, role__in=roles).exists()


@transaction.atomic
def save_delivery_address(*, company, patient, actor, data, request=None):
    """Create or change the patient's delivery address. Returns (address, changed field names)."""
    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().filter(pk=patient.pk, company=company, is_active=True).first()
    if patient is None or not company.is_active or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists():
        raise PermissionDenied('This patient isn’t available.')
    if not can_manage(company, actor, patient):
        raise PermissionDenied('Only the patient or the practice team can change this delivery address.')
    values = _clean(data)
    address = DeliveryAddress.objects.select_for_update().filter(company=company, patient=patient).first()
    created = address is None
    address = address or DeliveryAddress(company=company, patient=patient)
    changed = [name for name in FIELDS if getattr(address, name) != values[name]]
    if not changed and not created:
        return address, []
    for name, value in values.items():
        setattr(address, name, value)
    address.updated_by = actor
    address.full_clean()
    address.save()
    by_patient = patient.user_id == actor.pk
    record_audit(company=company, actor=actor, patient=patient, action='delivery_address.saved', target=address,
                 request=request, metadata={'changed_fields': changed, 'created': created, 'by_patient': by_patient})
    PatientEvent.objects.create(
        company=company, patient=patient, category=PatientEvent.Category.DELIVERY,
        title='Delivery address added' if created else 'Delivery address changed',
        detail='' if by_patient else 'Your practice updated it for you. Check it on My profile.',
        source_type='care.deliveryaddress', source_id=str(address.pk))
    return address, changed
