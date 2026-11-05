"""Blood tests a doctor asks for by a date: the Required by date on the PathCare request (Dr Czech, Nov 2026).

The patient has the tests and marks them Done on My Tasks (or uploads the lab's report), which gives the requesting
doctor a task to review them; the doctor marks them Reviewed. On the Required by date, a patient who hasn't marked
them Done is emailed that their subscription will be paused until they are. A week after the date, if the doctor
hasn't marked them Reviewed, the patient's subscription and shipping pause: no debits are collected and no parcel is
prepared, locked or dispatched (care.billing, care.operations, care.treatment). Reviewing lifts the pause: billing
carries on from that day and the parcels it held go back to drafts. A request without a date never pauses anything.

run_checks, once a day (manage.py run_blood_test_checks), sends the emails and holds the parcels already planned.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.formats import date_format

from practices.models import Patient

from .models import LabRequest, PatientEvent, PatientSubscription, Shipment

GRACE = timedelta(weeks=1)
# Every reason given for the pause starts with this, so the parcels it held can be found again when it lifts.
PAUSED = 'Paused until the blood tests'


def pauses_on(lab):
    """The day blood tests that aren't reviewed by then pause the subscription and shipping, or None without a date."""
    return lab.due_on + GRACE if lab.due_on else None


def pausing_lab(company_id, patient_id, on_date=None):
    """The blood tests that pause this patient's subscription and shipping on the day, or None."""
    day = on_date or timezone.localdate()
    return LabRequest.objects.filter(
        company_id=company_id, patient_id=patient_id, due_on__isnull=False, due_on__lte=day - GRACE,
    ).exclude(status=LabRequest.Status.REVIEWED).select_related('requested_by').order_by('due_on', 'pk').first()


def pause_reason(company_id, patient_id, on_date=None):
    """Why the patient's subscription and shipping are paused on the day, for staff and patient alike; '' when not."""
    lab = pausing_lab(company_id, patient_id, on_date)
    if lab is None:
        return ''
    return f'{PAUSED} {lab.requested_by.titled_name} asked for by {date_format(lab.due_on, "j F")} are done and reviewed.'


def hold_parcels(company, patient, reason):
    """Hold the patient's parcels that haven't gone out, with the reason shown on Weekly shipping."""
    return Shipment.objects.filter(
        company=company, patient=patient, status__in=(Shipment.Status.DRAFT, Shipment.Status.READY),
    ).update(status=Shipment.Status.HELD, hold_reason=reason[:500], updated_at=timezone.now())


def lift_pause(lab):
    """After blood tests are reviewed, when nothing else pauses the patient: the parcels the pause held go back to
    drafts, to be checked and locked again, and billing carries on from today rather than catching up on the weeks
    it was paused."""
    from .billing import BILLABLE, resume_debit_date

    today = timezone.localdate()
    if not lab.due_on or pauses_on(lab) > today or pausing_lab(lab.company_id, lab.patient_id, today) is not None:
        return
    Shipment.objects.filter(
        company_id=lab.company_id, patient_id=lab.patient_id, status=Shipment.Status.HELD, hold_reason__startswith=PAUSED,
    ).update(status=Shipment.Status.DRAFT, hold_reason='', locked_at=None, updated_at=timezone.now())
    for plan in PatientSubscription.objects.select_for_update().filter(
        company_id=lab.company_id, patient_id=lab.patient_id, status__in=BILLABLE, next_debit_on__lt=today,
    ):
        resumes = resume_debit_date(plan)
        if resumes is not None:
            plan.next_debit_on = resumes
            plan.save(update_fields=('next_debit_on', 'updated_at'))


def run_checks(company, *, today=None):
    """The daily round. Tell each patient whose blood tests are due and not done that their subscription will be
    paused until they are, and hold the parcels of patients whose pause has begun. Returns (told, parcels held)."""
    from .notifications import queue as notify

    today = today or timezone.localdate()
    told = 0
    due = LabRequest.objects.filter(
        company=company, due_on__isnull=False, due_on__lte=today, due_notice_sent_at__isnull=True,
        status=LabRequest.Status.REQUESTED, patient__is_active=True,
    ).select_related('company', 'patient')
    for lab in due:
        lab.due_notice_sent_at = timezone.now()
        lab.save(update_fields=('due_notice_sent_at', 'updated_at'))
        PatientEvent.objects.create(
            company=lab.company, patient=lab.patient, category=PatientEvent.Category.CLINICAL,
            title='Your blood tests are due',
            detail=f'If they aren’t done and reviewed by your doctor by {date_format(pauses_on(lab), "j F")}, your '
                   'subscription and deliveries are paused until they are. Mark them Done on My Tasks once you’ve had them.',
            source_type=lab._meta.label_lower, source_id=str(lab.pk), is_patient_visible=True,
        )
        notify('blood_tests_due', lab.pk)
        told += 1
    held = 0
    paused = LabRequest.objects.filter(company=company, due_on__lte=today - GRACE).exclude(
        status=LabRequest.Status.REVIEWED).values('patient_id')
    for patient in Patient.objects.filter(company=company, pk__in=paused):
        held += hold_parcels(company, patient, pause_reason(company.pk, patient.pk, today))
    return told, held
