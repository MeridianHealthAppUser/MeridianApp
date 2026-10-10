"""Patient files: PathCare forms and lab reports move out of their own tables into PatientFile.

The data step copies each existing PDF into a PatientFile row with the bytes kept in the database
(PatientFile.content), so this migration never needs object storage and loses nothing. Once
Spaces is configured, `python manage.py move_patient_files_to_spaces` moves the bytes there.
"""

import hashlib

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


PATHCARE_FILENAME = 'pathcare-blood-test-request.pdf'


def _new_file(PatientFile, row, *, kind, filename, uploaded_by_id):
    content = bytes(row.content or b'')
    file = PatientFile.objects.create(
        company_id=row.company_id, patient_id=row.patient_id, kind=kind, filename=filename[:160],
        content_type='application/pdf', size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        storage_key='', content=content, uploaded_by_id=uploaded_by_id,
    )
    # Keep the original dates rather than the date of this migration.
    PatientFile.objects.filter(pk=file.pk).update(created_at=row.created_at, updated_at=row.updated_at)
    return file


def copy_pdfs_to_patient_files(apps, schema_editor):
    PatientFile = apps.get_model('care', 'PatientFile')
    PathCareRequestForm = apps.get_model('care', 'PathCareRequestForm')
    LabResult = apps.get_model('care', 'LabResult')
    # One row at a time, so a large table never loads every PDF into memory together.
    for pk in PathCareRequestForm.objects.filter(file__isnull=True).order_by('pk').values_list('pk', flat=True):
        form = PathCareRequestForm.objects.select_related('lab_request').get(pk=pk)
        file = _new_file(PatientFile, form, kind='pathcare_form', filename=PATHCARE_FILENAME,
                         uploaded_by_id=form.lab_request.requested_by_id)
        PathCareRequestForm.objects.filter(pk=pk).update(file=file)
    for pk in LabResult.objects.filter(file__isnull=True).order_by('pk').values_list('pk', flat=True):
        result = LabResult.objects.get(pk=pk)
        file = _new_file(PatientFile, result, kind='lab_result', filename=result.filename,
                         uploaded_by_id=result.uploaded_by_id)
        LabResult.objects.filter(pk=pk).update(file=file)


def _file_bytes(file):
    if file.content is not None:
        return bytes(file.content)
    if not getattr(settings, 'PATIENT_FILES_STORAGE_ENABLED', False):
        raise RuntimeError(f'Patient file {file.pk} is in object storage, which is not configured here. '
                           'Configure it before reversing this migration.')
    from django.core.files.storage import storages

    with storages['patient_files'].open(file.storage_key, 'rb') as handle:
        return handle.read()


def copy_pdfs_back(apps, schema_editor):
    PatientFile = apps.get_model('care', 'PatientFile')
    for model_name in ('PathCareRequestForm', 'LabResult'):
        Model = apps.get_model('care', model_name)
        for pk, file_id in Model.objects.filter(file__isnull=False).order_by('pk').values_list('pk', 'file_id'):
            Model.objects.filter(pk=pk).update(content=_file_bytes(PatientFile.objects.get(pk=file_id)))


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0016_staff_read_state_and_pathcare_forms'),
        ('practices', '0004_patient_title'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PatientFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('pathcare_form', 'PathCare request form'),
                                                   ('lab_result', 'Lab result report')], max_length=32)),
                ('filename', models.CharField(max_length=160)),
                ('content_type', models.CharField(default='application/pdf', max_length=100)),
                ('size', models.PositiveIntegerField()),
                ('sha256', models.CharField(max_length=64)),
                ('storage_key', models.CharField(blank=True, max_length=512)),
                ('content', models.BinaryField(blank=True, null=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='%(app_label)s_%(class)s_records', to='practices.company')),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='%(app_label)s_%(class)s_records', to='practices.patient')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name='uploaded_patient_files', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_at', '-pk'),
                'indexes': [models.Index(fields=['company', 'patient', 'kind'], name='patient_file_lookup_idx')],
                'constraints': [
                    models.UniqueConstraint(condition=models.Q(('storage_key', ''), _negated=True),
                                            fields=('storage_key',), name='unique_patient_file_storage_key'),
                    models.CheckConstraint(condition=models.Q(('content__isnull', False),
                                                              models.Q(('storage_key', ''), _negated=True),
                                                              _connector='OR'),
                                           name='patient_file_has_bytes'),
                ],
            },
        ),
        migrations.AddField(
            model_name='pathcarerequestform',
            name='file',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT,
                                       related_name='pathcare_form', to='care.patientfile'),
        ),
        migrations.AddField(
            model_name='labresult',
            name='file',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT,
                                       related_name='lab_result', to='care.patientfile'),
        ),
        # Nullable before 0018 drops them, so reversing can add them back to tables that have rows
        # (the copy-back below fills them, then they are made required again).
        migrations.AlterField(
            model_name='pathcarerequestform',
            name='content',
            field=models.BinaryField(null=True),
        ),
        migrations.AlterField(
            model_name='labresult',
            name='content',
            field=models.BinaryField(null=True),
        ),
        migrations.RunPython(copy_pdfs_to_patient_files, copy_pdfs_back),
    ]
