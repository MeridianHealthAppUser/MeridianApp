"""Patient files: private storage for PathCare forms and lab reports, with a database fallback."""

import hashlib
import io
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .clinical import create_lab_request, submit_lab_result
from .models import LabResult, PatientFile
from .patient_files import (
    PatientFileIntegrityError, PatientFileUnavailable, build_key, delete_patient_file, get_storage, move_to_storage,
    patient_files, read_patient_file, store_patient_file,
)


PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF'
OTHER_PDF = b'%PDF-1.4\n2 0 obj << /Type /Pages >> endobj\n%%EOF'
KEY = re.compile(r'^meridian-health/patients/(\d+)/(\d+)/(\w+)/([0-9a-f-]{36})\.pdf$')


class PatientFileTestData:
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Files Practice', slug='files-practice')
        cls.other_company = Company.objects.create(name='Other Files Practice', slug='other-files-practice')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='files-doctor@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor', clinician_type='doctor')
        cls.patient_user = users.create_user(email='files-patient@example.test')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='File', last_name='Patient')
        cls.other_patient = Patient.objects.create(company=cls.company, first_name='Other', last_name='Patient')
        cls.foreign_patient = Patient.objects.create(company=cls.other_company, first_name='Foreign', last_name='Patient')

    def store(self, content=PDF, **values):
        options = {'company': self.company, 'patient': self.patient, 'kind': PatientFile.Kind.LAB_RESULT,
                   'filename': 'results.pdf', 'content': content, 'uploaded_by': self.patient_user}
        options.update(values)
        return store_patient_file(**options)

    def stored(self, **values):
        """Store a file and let its transaction commit, so it is moved to storage."""
        with self.captureOnCommitCallbacks(execute=True):
            file = self.store(**values)
        file.refresh_from_db()
        return file


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientFileServiceTests(PatientFileTestData, TestCase):
    def test_a_file_is_moved_to_private_storage_under_a_scoped_unguessable_key_once_committed(self):
        with self.captureOnCommitCallbacks(execute=True):
            file = self.store()
            # Until the transaction commits the bytes are only in the database.
            self.assertEqual((file.storage_key, bytes(file.content)), ('', PDF))
        file.refresh_from_db()
        self.assertIsNone(file.content)
        match = KEY.match(file.storage_key)
        self.assertIsNotNone(match, file.storage_key)
        self.assertEqual(match.groups()[:3], (str(self.company.pk), str(self.patient.pk), 'lab_result'))
        self.assertNotIn('results', file.storage_key)
        self.assertEqual((file.size, file.sha256, file.filename, file.content_type, file.uploaded_by_id),
                         (len(PDF), hashlib.sha256(PDF).hexdigest(), 'results.pdf', 'application/pdf', self.patient_user.pk))
        with get_storage().open(file.storage_key, 'rb') as handle:
            self.assertEqual(handle.read(), PDF)
        self.assertEqual(read_patient_file(file), PDF)
        second = self.stored()
        self.assertNotEqual(second.storage_key, file.storage_key)

    def test_a_rolled_back_file_leaves_nothing_behind(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.store()
                raise RuntimeError('the record could not be saved')
        self.assertEqual(callbacks, [])  # Nothing is ever written to storage.
        self.assertFalse(PatientFile.objects.exists())

    def test_files_are_scoped_to_the_practice_and_patient(self):
        mine = self.stored()
        form = self.stored(kind=PatientFile.Kind.PATHCARE_FORM, content=OTHER_PDF)
        self.stored(patient=self.other_patient)
        with self.assertRaises(ValidationError):
            self.store(patient=self.foreign_patient)
        with self.assertRaises(ValidationError):
            self.store(kind='passport_scan')
        with self.assertRaises(ValidationError):
            self.store(content=b'')
        self.assertEqual(set(patient_files(self.company, self.patient)), {mine, form})
        self.assertEqual(list(patient_files(self.company, self.patient, PatientFile.Kind.PATHCARE_FORM)), [form])
        self.assertEqual(list(patient_files(self.other_company, self.patient)), [])
        self.assertIn('content', patient_files(self.company, self.patient).first().get_deferred_fields())

    def test_reading_checks_the_sha256(self):
        in_db = self.store()
        PatientFile.objects.filter(pk=in_db.pk).update(content=OTHER_PDF)
        in_db.refresh_from_db()
        with self.assertRaises(PatientFileIntegrityError), self.assertLogs('care.patient_files', 'ERROR') as logs:
            read_patient_file(in_db)
        self.assertNotIn('results.pdf', ''.join(logs.output))
        in_storage = self.stored()
        storage = get_storage()
        storage.delete(in_storage.storage_key)
        storage.save(in_storage.storage_key, ContentFile(OTHER_PDF))
        with self.assertRaises(PatientFileIntegrityError), self.assertLogs('care.patient_files', 'ERROR'):
            read_patient_file(in_storage)
        storage.delete(in_storage.storage_key)
        with self.assertRaises(PatientFileUnavailable):
            read_patient_file(in_storage)

    def test_a_row_loaded_before_the_move_is_still_readable(self):
        file = self.store()
        loaded = PatientFile.objects.defer('content').get(pk=file.pk)
        self.assertTrue(move_to_storage(file.pk))
        self.assertFalse(move_to_storage(file.pk))  # Nothing left to move.
        self.assertEqual(read_patient_file(loaded), PDF)

    def test_a_failed_move_keeps_the_file_in_the_database(self):
        with patch.object(type(get_storage()), 'save', side_effect=OSError('Space unreachable')), \
                self.assertLogs('care.patient_files', 'ERROR') as logs:
            file = self.stored()
        self.assertEqual((file.storage_key, bytes(file.content)), ('', PDF))
        self.assertIn(f'Patient file {file.pk} could not be moved', logs.output[0])
        self.assertEqual(read_patient_file(file), PDF)

    def test_delete_removes_the_object_and_the_row_but_never_a_file_a_record_uses(self):
        file = self.stored()
        key = file.storage_key
        with self.captureOnCommitCallbacks(execute=True):
            delete_patient_file(file)
        self.assertFalse(PatientFile.objects.filter(pk=file.pk).exists())
        self.assertFalse(get_storage().exists(key))
        lab = create_lab_request(company=self.company, patient=self.patient, actor=self.doctor, panel_name='FBC')
        with self.captureOnCommitCallbacks(execute=True):
            result = submit_lab_result(lab_request=lab, actor=self.patient_user, filename='results.pdf', content=PDF)
        result.file.refresh_from_db()
        self.assertTrue(result.file.storage_key)
        with self.assertRaises(ProtectedError):
            delete_patient_file(result.file)
        self.assertTrue(get_storage().exists(result.file.storage_key))
        self.assertEqual(read_patient_file(LabResult.objects.get(pk=result.pk).file), PDF)

    def test_a_record_cannot_use_another_patients_file(self):
        lab = create_lab_request(company=self.company, patient=self.patient, actor=self.doctor, panel_name='FBC')
        other = self.store(patient=self.other_patient)
        result = LabResult(company=self.company, patient=self.patient, lab_request=lab, uploaded_by=self.patient_user,
                           filename='results.pdf', file=other, size=other.size, sha256=other.sha256)
        with self.assertRaisesMessage(ValidationError, 'same patient and practice'):
            result.full_clean()


@override_settings(MULTI_PRACTICE_ENABLED=True, PATIENT_FILES_STORAGE_ENABLED=False)
class DatabaseFallbackTests(PatientFileTestData, TestCase):
    def test_without_storage_files_stay_in_the_database(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            file = self.store()
        self.assertEqual(callbacks, [])
        file.refresh_from_db()
        self.assertEqual((file.storage_key, bytes(file.content)), ('', PDF))
        self.assertIsNone(get_storage())
        self.assertFalse(move_to_storage(file.pk))
        self.assertEqual(read_patient_file(file), PDF)
        with self.captureOnCommitCallbacks(execute=True):
            delete_patient_file(file)
        self.assertFalse(PatientFile.objects.exists())

    def test_a_file_in_storage_cant_be_read_where_storage_isnt_configured(self):
        file = self.store()
        PatientFile.objects.filter(pk=file.pk).update(storage_key='meridian-health/patients/1/1/lab_result/x.pdf', content=None)
        file.refresh_from_db()
        with self.assertRaises(PatientFileUnavailable):
            read_patient_file(file)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class MovePatientFilesCommandTests(PatientFileTestData, TestCase):
    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command('move_patient_files_to_spaces', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_it_refuses_to_run_without_spaces(self):
        self.store()
        with self.assertRaisesMessage(CommandError, 'Spaces is not configured'):
            self.run_command()
        with override_settings(SPACES_CONFIGURED=True, PATIENT_FILES_STORAGE_ENABLED=False), \
                self.assertRaisesMessage(CommandError, 'Spaces is not configured'):
            self.run_command()
        self.assertEqual(PatientFile.objects.filter(storage_key='').count(), 1)

    @override_settings(SPACES_CONFIGURED=True, SPACES_BUCKET='meridian-test-files')
    def test_it_moves_every_database_file_in_batches_and_can_run_again(self):
        files = [self.store(content=PDF + str(number).encode()) for number in range(5)]
        already = self.stored()
        out, _ = self.run_command('--dry-run')
        self.assertIn('5 file(s)', out)
        self.assertIn('Nothing was changed', out)
        self.assertEqual(PatientFile.objects.filter(storage_key='').count(), 5)
        out, _ = self.run_command('--batch-size', '2')
        self.assertIn('Done: 5 moved, 0 already moved, 0 failed.', out)
        for file in files:
            file.refresh_from_db()
            self.assertIsNone(file.content)
            self.assertRegex(file.storage_key, KEY)
            self.assertEqual(len(read_patient_file(file)), file.size)
        self.assertEqual(read_patient_file(files[3]), PDF + b'3')
        already_key = already.storage_key
        already.refresh_from_db()
        self.assertEqual(already.storage_key, already_key)
        out, _ = self.run_command()
        self.assertIn('Done: 0 moved', out)
        self.assertNotIn('results.pdf', out)

    @override_settings(SPACES_CONFIGURED=True, SPACES_BUCKET='meridian-test-files')
    def test_a_file_that_fails_stays_in_the_database_and_the_rest_are_moved(self):
        good, broken = self.store(), self.store(content=OTHER_PDF)
        PatientFile.objects.filter(pk=broken.pk).update(sha256='0' * 64)
        with self.assertRaisesMessage(CommandError, '1 file(s) could not be moved'):
            self.run_command()
        good.refresh_from_db()
        broken.refresh_from_db()
        self.assertTrue(good.storage_key)
        self.assertEqual((broken.storage_key, bytes(broken.content)), ('', OTHER_PDF))


class PatientFileMigrationTests(TransactionTestCase):
    """The data migration copies existing inline PDFs into PatientFile rows, keeps them in the database, and reverses."""

    before = [('care', '0016_staff_read_state_and_pathcare_forms')]
    after = [('care', '0018_patient_file_required')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        # The models as they are at the target, with every other app at its latest migration.
        others = [node for node in executor.loader.graph.leaf_nodes() if node[0] != 'care']
        return executor.loader.project_state(targets + others).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_pdfs_survive_the_migration_and_its_reversal(self):
        old = self.migrate(self.before)
        users = old.get_model('accounts', 'User')
        doctor = users.objects.create(email='migrate-doctor@example.test', password='!')
        patient_user = users.objects.create(email='migrate-patient@example.test', password='!')
        company = old.get_model('practices', 'Company').objects.create(name='Migrate', slug='migrate-files')
        patient = old.get_model('practices', 'Patient').objects.create(company=company, user=patient_user,
                                                                       first_name='M', last_name='P')
        LabRequest = old.get_model('care', 'LabRequest')
        form_lab = LabRequest.objects.create(company=company, patient=patient, requested_by=doctor, panel_name='FBC')
        result_lab = LabRequest.objects.create(company=company, patient=patient, requested_by=doctor, panel_name='TSH',
                                               status='uploaded')
        created = timezone.now().replace(microsecond=0) - timedelta(days=30)
        form = old.get_model('care', 'PathCareRequestForm').objects.create(
            company=company, patient=patient, lab_request=form_lab, tests='FBC', content=OTHER_PDF,
            size=len(OTHER_PDF), sha256=hashlib.sha256(OTHER_PDF).hexdigest(), email_to='p@example.test')
        result = old.get_model('care', 'LabResult').objects.create(
            company=company, patient=patient, lab_request=result_lab, uploaded_by=patient_user,
            filename='my results.pdf', content=PDF, size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest())
        old.get_model('care', 'LabResult').objects.filter(pk=result.pk).update(created_at=created)

        new = self.migrate(self.after)
        form = new.get_model('care', 'PathCareRequestForm').objects.select_related('file').get(pk=form.pk)
        result = new.get_model('care', 'LabResult').objects.select_related('file').get(pk=result.pk)
        self.assertEqual(new.get_model('care', 'PatientFile').objects.count(), 2)
        for record, kind, filename, content, uploader in (
            (form, 'pathcare_form', 'pathcare-blood-test-request.pdf', OTHER_PDF, doctor.pk),
            (result, 'lab_result', 'my results.pdf', PDF, patient_user.pk),
        ):
            file = record.file
            self.assertEqual((file.company_id, file.patient_id, file.kind, file.filename, file.uploaded_by_id),
                             (company.pk, patient.pk, kind, filename, uploader))
            self.assertEqual((bytes(file.content), file.storage_key, file.size, file.sha256),
                             (content, '', len(content), hashlib.sha256(content).hexdigest()))
            self.assertEqual(file.created_at, record.created_at)
        self.assertEqual(result.file.created_at, created)
        self.assertEqual(read_patient_file(PatientFile.objects.get(pk=result.file_id)), PDF)

        # The form's bytes are moved to storage before reversing, so the reversal reads them back from there.
        self.assertTrue(move_to_storage(form.file_id))
        old = self.migrate(self.before)
        self.assertEqual(bytes(old.get_model('care', 'PathCareRequestForm').objects.get(pk=form.pk).content), OTHER_PDF)
        self.assertEqual(bytes(old.get_model('care', 'LabResult').objects.get(pk=result.pk).content), PDF)


class PatientFilesStorageSettingTests(TestCase):
    def test_the_test_suite_never_uses_a_real_space_and_keys_are_namespaced(self):
        from django.core.files.storage import InMemoryStorage

        self.assertIsInstance(storages['patient_files'], InMemoryStorage)
        self.assertRegex(build_key(company_id=1, patient_id=2, kind='lab_result'),
                         r'^meridian-health/patients/1/2/lab_result/[0-9a-f-]{36}\.pdf$')
        with override_settings(PATIENT_FILES_PREFIX=''):
            self.assertTrue(build_key(company_id=1, patient_id=2, kind='pathcare_form').startswith('patients/1/2/pathcare_form/'))
