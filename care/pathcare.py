"""PathCare blood-test request forms: fill the blank form, flatten it, store it and email it to the patient.

Only the fields listed in FIELDS are filled, and every one of them is written on every fill (blank
when there is nothing to put there). Everything else on the form is left for the doctor, the
patient or the collection point to complete by hand. The medical aid number is never stored or
filled. The stored file is what "View form" and "Resend" serve.
"""

import io
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from .clinical import _lock_context, _require_doctor, create_lab_request
from .models import Lead, PathCareRequestForm, PatientFile, PharmacyOrder
from .patient_files import read_patient_file, store_patient_file
from .services import record_audit


BLANK_FORM = Path(__file__).resolve().parent / 'pathcare' / 'clinical_request_blank.pdf'
FIELDS = ('Ref doctor', 'Ref doctor Code', 'PID', 'Patient Surname', 'Patient First Name', 'Patient Title',
          'Postal Address 1', 'Postal Address 2', 'Postal Address 3', 'Medical Aid No', 'CDD')
MULTILINE = {'CDD'}
FONT = '/Arial,Bold'
FONT_SIZES = (9, 8, 7, 6)
TESTS_MAX = 300
DOCTOR_DETAILS_MISSING = ('Your PathCare name and code are not on file, so the form can’t be filled. '
                          'Ask the practice to add them in Users and roles.')
FILENAME = 'pathcare-blood-test-request.pdf'
EMAIL_NOT_SET_UP = ('Email isn’t set up yet, so the blood test form wasn’t emailed. It is in the clinical log, '
                    'where you can view it, and resend it once email is set up.')

logger = logging.getLogger(__name__)

# Helvetica-Bold advance widths (per 1000 units), which Arial Bold matches closely.
_WIDTHS = dict(zip(
    ' !"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~',
    (278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
     556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722,
     611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556,
     611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778,
     556, 556, 500, 389, 280, 389, 584),
))


def text_width(text, size):
    return sum(_WIDTHS.get(char, 611) for char in text) * size / 1000


def _wrap(text, width, size):
    lines = []
    for paragraph in text.split('\n'):
        line = ''
        for word in paragraph.split(' '):
            candidate = f'{line} {word}' if line else word
            if text_width(candidate, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # A word wider than the box is broken where it reaches the edge.
            while text_width(word, size) > width:
                cut = max(1, next((i for i in range(len(word), 0, -1) if text_width(word[:i], size) <= width), 1))
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def fit_text(text, width, height, *, multiline):
    """The largest font size (9pt down to 6pt) at which the text fits the box, and the text as laid out."""
    for size in FONT_SIZES:
        if multiline:
            lines = _wrap(text, width, size)
            if len(lines) * size * 1.16 <= height:
                return size, '\n'.join(lines)
        elif text_width(text, size) <= width:
            return size, text
    return None


def _one_line(value):
    return ' '.join(str(value or '').split())


def patient_address(company, patient):
    """The delivery address from the patient's latest supply request: street; suburb, city; postal code."""
    order = PharmacyOrder.objects.for_company(company).filter(patient=patient).exclude(delivery_address={}).exclude(
        submitted_at__isnull=True,
    ).order_by('-submitted_at', '-pk').first()
    address = order.delivery_address if order and isinstance(order.delivery_address, dict) else {}
    second = ', '.join(part for part in (_one_line(address.get('line2')), _one_line(address.get('city'))) if part)
    return _one_line(address.get('line1')), second, _one_line(address.get('postal_code'))


def patient_email(company, patient):
    if patient.user_id and patient.user.email:
        return patient.user.email
    lead = Lead.objects.for_company(company).filter(converted_patient=patient).first()
    return lead.email if lead else ''


def _patient_id(value):
    value = ''.join(str(value or '').split()).upper()
    digits = value.replace('-', '')
    return digits if digits.isdigit() else value


def form_values(*, company, patient, doctor, tests=''):
    line1, line2, line3 = patient_address(company, patient)
    return {
        'Ref doctor': _one_line(doctor.pathcare_name).upper(),
        'Ref doctor Code': _one_line(doctor.pathcare_code).upper(),
        'PID': _patient_id(patient.id_number),
        'Patient Surname': _one_line(patient.last_name).upper(),
        'Patient First Name': _one_line(patient.first_name).upper(),
        'Patient Title': patient.title.upper(),
        'Postal Address 1': line1, 'Postal Address 2': line2, 'Postal Address 3': line3,
        # Not stored or filled: the doctor can write it in by hand.
        'Medical Aid No': '',
        'CDD': (tests or '').strip(),
    }


SUMMARY_ROWS = (
    ('Referring doctor', ('Ref doctor',)), ('PathCare code', ('Ref doctor Code',)), ('Patient ID', ('PID',)),
    ('Surname', ('Patient Surname',)), ('First name(s)', ('Patient First Name',)), ('Title', ('Patient Title',)),
    ('Postal address', ('Postal Address 1', 'Postal Address 2', 'Postal Address 3')),
)


def form_summary(values):
    """Every filled value for the preview, with missing ones marked."""
    rows = []
    for label, names in SUMMARY_ROWS:
        value = ', '.join(values[name] for name in names if values[name])
        rows.append({'label': label, 'value': value, 'missing': not value,
                     'blocking': names[0] in ('Ref doctor', 'Ref doctor Code')})
    return rows


def _boxes(page):
    boxes = {}
    for annotation in page.get('/Annots', []):
        widget = annotation.get_object()
        name = widget.get('/T') or (widget.get('/Parent') and widget['/Parent'].get_object().get('/T'))
        if name in FIELDS:
            left, bottom, right, top = (float(value) for value in widget['/Rect'])
            boxes[name] = (widget, right - left - 4, top - bottom - 2)
    return boxes


def fill_form(values):
    """The filled, flattened form as PDF bytes. Raises ValidationError if a value can't fit its box."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, TextStringObject

    writer = PdfWriter(clone_from=str(BLANK_FORM))
    page = writer.pages[0]
    filled = {}
    for name, (widget, width, height) in _boxes(page).items():
        text = values.get(name, '') or ''
        fitted = fit_text(text, width, height, multiline=name in MULTILINE)
        if fitted is None:
            raise ValidationError('The tests and clinical data are too long for the form’s box. Shorten them.'
                                  if name == 'CDD' else f'The {name.lower()} is too long for the form.')
        size, laid_out = fitted
        widget[NameObject('/DA')] = TextStringObject(f'{FONT} {size} Tf 0 g')
        filled[name] = laid_out
    writer.update_page_form_field_values(page, filled, auto_regenerate=False, flatten=True)
    # Flatten fully: the emailed form can't be edited, and no other box keeps a fillable field.
    writer.remove_annotations(subtypes='/Widget')
    # Named destinations in the blank form still point at the old fields; a one-page form doesn't need them.
    for key in ('/AcroForm', '/Names', '/Dests'):
        if key in writer._root_object:
            del writer._root_object[key]
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def clean_tests(tests):
    if not isinstance(tests, str) or not tests.strip():
        raise ValidationError('Enter the tests and any relevant clinical data.')
    tests = '\n'.join(' '.join(line.split()) for line in tests.strip().splitlines())
    if len(tests) > TESTS_MAX:
        raise ValidationError(f'Keep the tests and clinical data to {TESTS_MAX} characters or fewer.')
    return tests


def doctor_name(doctor):
    return f'Dr {doctor.last_name}' if doctor.last_name else doctor.full_name


@transaction.atomic
def request_blood_tests(*, company, patient, actor, tests, submission_key=None, request=None):
    """Create the blood-test request with its filled form, add it to the clinical log and queue the email."""
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    tests = clean_tests(tests)
    if not actor.pathcare_name.strip() or not actor.pathcare_code.strip():
        raise ValidationError(DOCTOR_DETAILS_MISSING)
    email = patient_email(company, patient)
    if not email:
        raise ValidationError('This patient has no email address on file, so the form can’t be emailed.')
    content = fill_form(form_values(company=company, patient=patient, doctor=actor, tests=tests))
    lab = create_lab_request(company=company, patient=patient, actor=actor, panel_name=_one_line(tests)[:255],
                             submission_key=submission_key, request=request)
    existing = PathCareRequestForm.objects.filter(lab_request=lab).first()
    if existing is not None:
        return existing  # A repeated submission (a double click) sends nothing twice.
    file = store_patient_file(company=company, patient=patient, kind=PatientFile.Kind.PATHCARE_FORM, filename=FILENAME,
                              content=content, uploaded_by=actor)
    form = PathCareRequestForm(company=company, patient=patient, lab_request=lab, tests=tests, file=file,
                               size=file.size, sha256=file.sha256, email_to=email)
    form.full_clean()
    form.save()
    record_audit(company=company, actor=actor, patient=patient, action='pathcare_form.created', target=form,
                 request=request, metadata={'lab_request_id': lab.pk})
    _queue(form)
    return form


@transaction.atomic
def resend_pathcare_form(*, form, actor, request=None):
    company, patient = _lock_context(form.company, form.patient, actor)
    _require_doctor(company, actor)
    form = PathCareRequestForm.objects.select_for_update().get(pk=form.pk, company=company, patient=patient)
    email = patient_email(company, patient)
    if not email:
        raise ValidationError('This patient has no email address on file, so the form can’t be emailed.')
    form.email_to, form.email_status = email, PathCareRequestForm.EmailStatus.QUEUED
    form.send_count += 1
    form.save(update_fields=('email_to', 'email_status', 'send_count', 'updated_at'))
    record_audit(company=company, actor=actor, patient=patient, action='pathcare_form.resent', target=form,
                 request=request, metadata={'send_count': form.send_count})
    _queue(form)
    return form


def _queue(form):
    from .tasks import send_pathcare_form_email

    def send():
        if not settings.EMAIL_ENABLED:  # No mail server yet: keep the form, marked not emailed, to resend later.
            logger.warning('Email is not set up, so PathCare form %s was not emailed.', form.pk)
            mark_failed(form.pk)
            return
        try:
            send_pathcare_form_email.delay(form.pk)
        except Exception:  # The task queue can't be reached: the form is kept, marked failed and can be resent.
            logger.exception('Could not queue PathCare form %s for emailing.', form.pk)
            mark_failed(form.pk)

    transaction.on_commit(send)


def deliver(form_id):
    """Send one queued form. Errors propagate so the task can retry."""
    form = PathCareRequestForm.objects.select_related('company', 'patient', 'lab_request__requested_by').get(pk=form_id)
    PathCareRequestForm.objects.filter(pk=form.pk).update(email_attempts=form.email_attempts + 1)
    doctor = form.lab_request.requested_by
    message = EmailMessage(
        subject=f'Your blood test request from {form.company.name}',
        body=(f'Hello {form.patient.first_name},\n\n'
              f'{doctor_name(doctor)} has requested blood tests for you. The PathCare request form is attached.\n\n'
              'Take the form, printed or on your phone, to any PathCare collection point.\n\n'
              + ('You can also open it in the Meridian app under Blood tests until you upload your results.\n\n'
                 if form.patient.user_id else '')
              + f'{form.company.name}\n'),
        from_email=settings.DEFAULT_FROM_EMAIL, to=[form.email_to],
    )
    message.attach(FILENAME, read_patient_file(form.file), 'application/pdf')
    message.send(fail_silently=False)
    PathCareRequestForm.objects.filter(pk=form.pk).update(
        email_status=PathCareRequestForm.EmailStatus.SENT, emailed_at=timezone.now(), updated_at=timezone.now(),
    )
    record_audit(company=form.company, actor=None, patient=form.patient, action='pathcare_form.emailed', target=form)


def mark_failed(form_id):
    form = PathCareRequestForm.objects.select_related('company', 'patient').get(pk=form_id)
    PathCareRequestForm.objects.filter(pk=form.pk).update(
        email_status=PathCareRequestForm.EmailStatus.FAILED, updated_at=timezone.now(),
    )
    record_audit(company=form.company, actor=None, patient=form.patient, action='pathcare_form.email_failed', target=form)
