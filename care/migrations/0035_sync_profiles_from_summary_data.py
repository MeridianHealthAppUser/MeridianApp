from django.db import migrations


SHARED = {
    'medications': 'chronic_medication',
    'allergies': 'allergies',
    'medical_history': 'medical_history',
    'surgical_history': 'surgical_history',
}


def sync_profiles(apps, schema_editor):
    """Summary data already started owns the answers it shares with the patient's medical profile: write them into
    the profile as a new saved version, by whoever last edited the summary, keeping the earlier versions."""
    PatientClinicalSummary = apps.get_model('care', 'PatientClinicalSummary')
    PatientMedicalProfile = apps.get_model('care', 'PatientMedicalProfile')
    PatientMedicalProfileRevision = apps.get_model('care', 'PatientMedicalProfileRevision')
    PatientSummaryEdit = apps.get_model('care', 'PatientSummaryEdit')
    for summary in PatientClinicalSummary.objects.all():
        profile = PatientMedicalProfile.objects.filter(company_id=summary.company_id, patient_id=summary.patient_id).first()
        answers = dict(profile.answers) if profile and isinstance(profile.answers, dict) else {}
        changed = False
        for key, field in SHARED.items():
            value = getattr(summary, field) or ''
            if (answers.get(key) or '') != value:
                answers[key] = value
                changed = True
        if not changed:
            continue
        last_edit = PatientSummaryEdit.objects.filter(summary_id=summary.pk).order_by('-created_at', '-pk').first()
        editor_id = last_edit.edited_by_id if last_edit else None
        if profile is None:
            profile = PatientMedicalProfile(company_id=summary.company_id, patient_id=summary.patient_id)
        profile.answers = answers
        profile.revision += 1
        profile.saved_by_id = editor_id
        profile.save()
        PatientMedicalProfileRevision.objects.create(
            company_id=summary.company_id, patient_id=summary.patient_id, profile=profile, revision=profile.revision,
            answers=dict(answers), saved_by_id=editor_id,
        )


class Migration(migrations.Migration):

    dependencies = [("care", "0034_data_request_task")]

    operations = [migrations.RunPython(sync_profiles, migrations.RunPython.noop)]
