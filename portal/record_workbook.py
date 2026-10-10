"""Export record: a patient's record at this practice as an Excel workbook, one sheet per record tab.

Each sheet holds what its tab shows, with the same limits as the record's other exports: signed
consultations only, no private notes, only conversations the person exporting takes part in, and
no attachment bytes, payment-provider details or audit metadata.
"""

from itertools import islice

from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.clinical_summary import get_summary
from care.models import (
    Appointment, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest, MessageThread, Payment, PatientMessage,
    RecordTag, Shipment, TreatmentAuthorization, WeightEntry,
)
from care.prescriptions import current_prescription

from .patient_workspace import scoped
from .record_overview import clinical_log, dispensed, summary_context
from .record_views import RECORD_TIMEZONE, ClinicalRecordAccessMixin
from .record_xlsx import build_workbook


EXCLUDED = ('Private notes; unsigned consultations; conversations you are not part of; attachment bytes; '
            'payment-provider details; internal events, audit metadata and IP addresses.')
LOG_TYPES = {'consult': 'Consult note', 'blood': 'Blood form', 'prescription': 'Prescription'}


def _when(value):
    return timezone.localtime(value, RECORD_TIMEZONE).strftime('%Y-%m-%d %H:%M') if value else ''


def _day(value):
    return value.isoformat() if value else ''


def _person(user, fallback=''):
    return user.full_name if user else fallback


def _table(title, headers, rows, empty):
    rows = list(rows)
    yield 'title', (title,)
    yield 'header', headers
    if not rows:
        yield '', (empty,)
    for row in rows:
        yield '', row
    yield '', ()


def overview_rows(company, patient):
    """The Overview tab: the clinical and dispensed history, current prescription, weight and summary data."""
    log = clinical_log(company, patient)
    yield from _table('Clinical history', ('Date (SAST)', 'Who', 'Type', 'Entry'), (
        (_when(entry['at']), entry['by'] or ('Automatic' if entry.get('automatic') else ''), LOG_TYPES[entry['kind']], entry['text'])
        for entry in log), 'No clinical entries yet.')
    yield from _table('Dispensed history', ('Date', 'Medicine', 'Month', 'Detail'), (
        (_day(row['date']), row['title'], row['tag'], row['detail']) for row in dispensed(company, patient)),
        'Nothing has been dispensed yet.')
    prescription = current_prescription(company, patient)
    yield from _table('Current prescription', ('Field', 'Value'), (
        ('Medicine', prescription.product.name), ('Dose', prescription.max_dose),
        ('Authorised', _day(prescription.starts_on)), ('Expires', _day(prescription.expires_on)),
        ('By', _person(prescription.prescribed_by)),
    ) if prescription else (), 'No current prescription.')
    weights = WeightEntry.objects.for_company(company).filter(patient=patient).order_by('recorded_on', 'pk')
    yield from _table('Weight over time', ('Date', 'Weight (kg)'), (
        (_day(entry.recorded_on), entry.weight_kg) for entry in weights), 'No weights recorded yet.')
    summary = summary_context(company, patient, get_summary(company, patient))
    weight, bmi = summary['summary_weight'], summary['summary_bmi']
    yield from _table('Summary data', ('Field', 'Value'), (
        ('Weight start → current → goal (kg)', f"{weight['start']} → {weight['current']} → {weight['goal'] or 'Not set'}"),
        ('BMI start → current', f"{bmi['start'] or '—'} → {bmi['current'] or '—'}"),
        *((row['label'], row['value'] or 'Not recorded') for row in summary['summary_texts']),
        *((flag['label'], flag['display']) for flag in summary['summary_flags']),
    ), '')


def history_rows(timeline):
    def entries():
        index = timeline.index().iterator(chunk_size=200)
        while batch := list(islice(index, 200)):
            yield from timeline.render_rows(batch)

    yield 'header', ('Date (SAST)', 'Category', 'Event', 'Detail', 'Recorded by')
    for entry in entries():
        yield '', (_when(entry['at']), entry['category_label'], entry['title'], entry['detail'], entry['actor'])


def _sheet(headers, rows):
    yield 'header', headers
    for row in rows:
        yield '', row


def workbook_sheets(request, company, patient, timeline):
    tags = Prefetch('tags', queryset=RecordTag.objects.for_company(company))
    appointments = scoped(Appointment, company, patient).select_related('clinician').order_by('-starts_at', '-pk')
    consultations = scoped(ClinicalEncounter, company, patient).filter(status=ClinicalEncounter.Status.SIGNED).select_related(
        'clinician', 'signed_by').order_by('-occurred_at', '-pk')
    labs = scoped(LabRequest, company, patient).select_related('requested_by').defer('result_summary').order_by('-requested_on', '-pk')
    notes = scoped(ClinicalNote, company, patient).filter(is_private=False, signed_encounter__isnull=True).select_related(
        'author').prefetch_related(tags).order_by('-created_at', '-pk')
    weights = scoped(WeightEntry, company, patient).select_related('recorded_by').order_by('-recorded_on', '-pk')
    threads = scoped(MessageThread, company, patient).filter(participants=request.user).distinct()
    messages = PatientMessage.objects.filter(company=company, thread__in=threads).select_related('thread', 'sender').order_by(
        '-thread__last_message_at', 'thread_id', 'created_at', 'pk')
    tasks = scoped(ClinicalTask, company, patient).select_related('assigned_to').prefetch_related(tags).order_by('-created_at', '-pk')
    payments = scoped(Payment, company, patient).only('due_on', 'amount', 'status', 'paid_at').order_by('-due_on', '-pk')
    treatment = scoped(TreatmentAuthorization, company, patient).filter(product__company=company).select_related(
        'product', 'prescribed_by').order_by('-created_at', '-pk')
    deliveries = scoped(Shipment, company, patient).defer('dispatch_snapshot', 'hold_reason').order_by('-scheduled_for', '-pk')

    def sender(message):
        if message.sender_id and message.sender_id == patient.user_id:
            return str(patient)
        return _person(message.sender, 'Care team')

    return [
        ('Overview', overview_rows(company, patient), (18, 26, 22, 70), False),
        ('History', history_rows(timeline), (18, 18, 40, 60, 24), True),
        ('Appointments', _sheet(('Date and time (SAST)', 'Appointment', 'Duration (minutes)', 'Doctor', 'Status'), (
            (_when(row.starts_at), row.get_appointment_type_display(), row.duration_minutes, _person(row.clinician), row.get_status_display())
            for row in appointments)), (20, 22, 12, 24, 14), True),
        ('Consultations', _sheet(('Date (SAST)', 'Doctor', 'Status', 'Signed by', 'Consult note'), (
            (_when(row.occurred_at), _person(row.clinician), row.get_status_display(), _person(row.signed_by), row.clinical_summary)
            for row in consultations)), (18, 24, 12, 24, 80), True),
        ('Blood tests', _sheet(('Requested', 'Panel', 'Doctor', 'Status'), (
            (_day(row.requested_on), row.panel_name, _person(row.requested_by, 'Automatic'), row.get_status_display())
            for row in labs)), (14, 40, 24, 16), True),
        ('Notes', _sheet(('Date (SAST)', 'Type', 'Author', 'Note', 'Tags'), (
            (_when(row.created_at), row.get_note_type_display(), _person(row.author), row.body, ', '.join(tag.name for tag in row.tags.all()))
            for row in notes)), (18, 16, 24, 80, 24), True),
        ('Weight', _sheet(('Date', 'Weight (kg)', 'Note', 'Recorded by'), (
            (_day(row.recorded_on), row.weight_kg, row.note, _person(row.recorded_by)) for row in weights)), (14, 12, 40, 24), True),
        ('Messages', _sheet(('Conversation', 'Status', 'Date (SAST)', 'From', 'Message'), (
            (row.thread.subject, 'Closed' if row.thread.is_closed else 'Open', _when(row.created_at), sender(row), row.body)
            for row in messages)), (30, 10, 18, 24, 80), True),
        ('Tasks', _sheet(('Task', 'Description', 'Assigned to', 'Due (SAST)', 'Status', 'Tags'), (
            (row.title, row.description, _person(row.assigned_to, 'Unassigned'), _when(row.due_at) or 'No due date',
             row.get_status_display(), ', '.join(tag.name for tag in row.tags.all())) for row in tasks)), (32, 50, 24, 18, 14, 24), True),
        ('Payments', _sheet(('Due date', 'Amount (R)', 'Stored status', 'Recorded paid date (SAST)'), (
            (_day(row.due_on), row.amount, row.get_status_display(), _when(row.paid_at) or 'Not recorded') for row in payments)),
            (14, 12, 16, 22), True),
        ('Treatment', _sheet(('Medication', 'Dose', 'Doctor', 'Valid from', 'Valid to', 'Status'), (
            (row.product.name, row.max_dose, _person(row.prescribed_by), _day(row.starts_on), _day(row.expires_on), row.get_status_display())
            for row in treatment)), (26, 18, 24, 14, 14, 14), True),
        ('Deliveries', _sheet(('Scheduled', 'Cycle', 'Status', 'Tracking', 'Dispatched (SAST)'), (
            (_day(row.scheduled_for), row.cycle_number, row.get_status_display(), row.tracking_number or 'Not dispatched', _when(row.dispatched_at))
            for row in deliveries)), (14, 8, 16, 24, 20), True),
        ('Export details', _sheet(('Field', 'Value'), (
            ('Patient', str(patient)), ('Record number', patient.medical_record_number or 'Not recorded'),
            ('Practice', company.name), ('Exported by', request.user.full_name),
            ('Exported (SAST)', _when(timezone.now())), ('Excluded', EXCLUDED),
        )), (22, 90), True),
    ]


@method_decorator(never_cache, name='dispatch')
class ClinicalRecordWorkbookView(ClinicalRecordAccessMixin, View):
    """The Export record button: this practice's record for the patient, as an Excel workbook."""

    def get(self, request, *args, **kwargs):
        self.resolve_record({'scope': 'current', 'category': 'all'})
        if not self.valid_filters:
            return JsonResponse({'errors': self.filter_form.errors.get_json_data()}, status=400)
        content = build_workbook(workbook_sheets(request, self.company, self.patient, self.timeline))
        response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="patient-{self.patient.pk}-record.xlsx"'
        response['X-Content-Type-Options'] = 'nosniff'
        self.audit_access('patient.clinical_record_exported')
        return response
