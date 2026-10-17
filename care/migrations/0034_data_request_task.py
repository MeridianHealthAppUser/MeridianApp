import django.db.models.deletion
from django.db import migrations, models


TITLES = {
    'access': 'Data request: copy of information',
    'correction': 'Data request: correction',
    'deletion': 'Data request: deletion review',
    'question': 'Data request: privacy question',
}


def note(kind, description):
    lines = ["The patient's request:", description, '']
    if kind == 'correction':
        lines.append("Height, allergies and medical history are changed in Summary data on the record's Overview.")
    lines.append('Completing this task marks the request Resolved and tells the patient you have marked it complete. '
                 'To give a fuller answer, or to decline, an administrator replies from Data requests instead, '
                 'which also completes this task.')
    return '\n'.join(lines)


def raise_tasks_for_waiting_requests(apps, schema_editor):
    """Requests already waiting on the practice get the task a new request is raised with."""
    PatientDataRequest = apps.get_model('care', 'PatientDataRequest')
    ClinicalTask = apps.get_model('care', 'ClinicalTask')
    CompanyMembership = apps.get_model('practices', 'CompanyMembership')
    waiting = PatientDataRequest.objects.filter(task__isnull=True, status__in=('open', 'in_review')).select_related('patient')
    for record in waiting:
        doctor_id = record.patient.assigned_doctor_id
        if doctor_id and not CompanyMembership.objects.filter(
            company_id=record.company_id, user_id=doctor_id, user__is_active=True, is_active=True,
        ).exclude(clinician_type='').exists():
            doctor_id = None
        task = ClinicalTask.objects.create(
            company_id=record.company_id, patient_id=record.patient_id, title=TITLES[record.kind],
            description=note(record.kind, record.description), assigned_to_id=doctor_id,
        )
        PatientDataRequest.objects.filter(pk=record.pk).update(task=task)


class Migration(migrations.Migration):

    dependencies = [("care", "0033_practice_shipping_weekday"), ("practices", "0004_patient_title")]

    operations = [
        migrations.AddField(
            model_name="patientdatarequest",
            name="task",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="data_request",
                to="care.clinicaltask",
            ),
        ),
        migrations.RunPython(raise_tasks_for_waiting_requests, migrations.RunPython.noop),
    ]
