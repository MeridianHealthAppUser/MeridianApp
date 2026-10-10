"""Patient files: store, read and delete a patient's private files (PathCare forms, lab reports).

Every file is a PatientFile row scoped to one practice and one patient. Its bytes go to the
`patient_files` storage (a private DigitalOcean Space in production) under a key nobody can guess:
<prefix>/patients/<company id>/<patient id>/<kind>/<uuid4>.pdf. Nothing has a public URL; views
read the bytes here after their own access checks and serve them.

A new file is written to the database first and moved to storage once its transaction commits, so a
rolled-back request never leaves an object behind and an unreachable Space never loses a file: it
stays in the database until `manage.py move_patient_files_to_spaces` moves it. Without storage
(production before a Space is set up) files simply stay in the database.

Logs name files by id only, never by patient or file name.
"""

import hashlib
import logging
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone

from .models import PatientFile


STORAGE_ALIAS = 'patient_files'
EXTENSIONS = {'application/pdf': '.pdf'}

logger = logging.getLogger(__name__)


class PatientFileError(Exception):
    """A patient file can't be read."""


class PatientFileUnavailable(PatientFileError):
    """The bytes can't be reached: storage isn't configured here, or the object is missing."""


class PatientFileIntegrityError(PatientFileError):
    """The bytes don't match the SHA-256 recorded when the file was stored."""


def storage_enabled():
    return bool(getattr(settings, 'PATIENT_FILES_STORAGE_ENABLED', False))


def get_storage():
    """The configured storage, or None when files are kept in the database. Resolved on every call."""
    return storages[STORAGE_ALIAS] if storage_enabled() else None


def build_key(*, company_id, patient_id, kind, content_type='application/pdf'):
    prefix = (getattr(settings, 'PATIENT_FILES_PREFIX', '') or '').strip('/')
    key = f'patients/{company_id}/{patient_id}/{kind}/{uuid.uuid4()}{EXTENSIONS.get(content_type, ".bin")}'
    return f'{prefix}/{key}' if prefix else key


def patient_files(company, patient, kind=None):
    """A patient's files in one practice, without their bytes."""
    files = PatientFile.objects.for_company(company).filter(patient=patient).defer('content')
    return files.filter(kind=kind) if kind else files


def store_patient_file(*, company, patient, kind, filename, content, content_type='application/pdf', uploaded_by=None):
    """Keep a file for a patient and return its PatientFile. Use inside the transaction that links it to its record."""
    if patient.company_id != company.pk:
        raise ValidationError('The file must belong to a patient of this practice.')
    if kind not in PatientFile.Kind.values:
        raise ValidationError('Unknown kind of patient file.')
    if not isinstance(content, (bytes, bytearray, memoryview)) or not content:
        raise ValidationError('The file is empty.')
    content = bytes(content)
    file = PatientFile(
        company=company, patient=patient, kind=kind, filename=(filename or '')[:160] or f'{kind}.pdf',
        content_type=content_type, size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        content=content, uploaded_by=uploaded_by,
    )
    file.full_clean(exclude=('content',))  # Never send the bytes to the database just to validate them.
    file.save()
    if storage_enabled():
        transaction.on_commit(lambda: _move_after_commit(file.pk))
    return file


def _move_after_commit(file_id):
    try:
        move_to_storage(file_id)
    except Exception:  # The file is safe in the database; the move command can try again later.
        logger.exception('Patient file %s could not be moved to storage, so it stays in the database.', file_id)


def move_to_storage(file_id):
    """Move one database-held file to storage. True if it was moved; False if there was nothing to move."""
    storage = get_storage()
    if storage is None:
        return False
    file = PatientFile.objects.filter(pk=file_id, storage_key='', content__isnull=False).first()
    if file is None:
        return False
    content = bytes(file.content)
    if hashlib.sha256(content).hexdigest() != file.sha256:
        raise PatientFileIntegrityError(f'Patient file {file.pk} does not match its SHA-256; it was not moved.')
    upload = ContentFile(content)
    upload.content_type = file.content_type
    key = storage.save(build_key(company_id=file.company_id, patient_id=file.patient_id, kind=file.kind,
                                 content_type=file.content_type), upload)
    try:
        if storage.size(key) != file.size:
            raise PatientFileIntegrityError(f'Patient file {file.pk} was not stored in full; it was not moved.')
        moved = PatientFile.objects.filter(pk=file.pk, storage_key='').update(
            storage_key=key, content=None, updated_at=timezone.now(),
        )
    except Exception:
        _delete_object(storage, key)
        raise
    if not moved:  # Moved (or deleted) at the same time by someone else: keep theirs.
        _delete_object(storage, key)
    return bool(moved)


def read_patient_file(file):
    """The file's bytes, checked against the SHA-256 recorded when it was stored."""
    content = _read(file)
    if content is None:
        # Moved to storage (or deleted) since this row was loaded: look again once.
        file.refresh_from_db(fields=('storage_key', 'content'))
        content = _read(file)
    if content is None:
        raise PatientFileUnavailable(f'Patient file {file.pk} has no stored bytes.')
    if hashlib.sha256(content).hexdigest() != file.sha256:
        logger.error('Patient file %s does not match its SHA-256.', file.pk)
        raise PatientFileIntegrityError(f'Patient file {file.pk} does not match its SHA-256.')
    return content


def _read(file):
    if file.storage_key:
        storage = get_storage()
        if storage is None:
            raise PatientFileUnavailable(f'Patient file {file.pk} is in storage, which is not configured here.')
        try:
            with storage.open(file.storage_key, 'rb') as handle:
                return handle.read()
        except FileNotFoundError as error:
            raise PatientFileUnavailable(f'Patient file {file.pk} is missing from storage.') from error
    return bytes(file.content) if file.content is not None else None


@transaction.atomic
def delete_patient_file(file):
    """Delete the file's row and, once that commits, its stored object. A file still used by a record is protected."""
    key = PatientFile.objects.filter(pk=file.pk).values_list('storage_key', flat=True).first()
    file.delete()
    if key:
        transaction.on_commit(lambda: _delete_after_commit(key, file.pk))


def _delete_after_commit(key, file_id):
    storage = get_storage()
    if storage is None:
        logger.error('Patient file %s was deleted, but its stored object could not be: storage is not configured.', file_id)
        return
    try:
        storage.delete(key)
    except Exception:
        logger.exception('Patient file %s was deleted, but its stored object could not be.', file_id)


def _delete_object(storage, key):
    try:
        storage.delete(key)
    except Exception:
        logger.exception('An unused patient file object could not be removed from storage.')
