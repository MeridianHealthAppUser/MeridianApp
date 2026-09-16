"""A read-only clinical record; every source retains its real practice boundary.

The timeline index is a database UNION, so pagination does not load the full
history. Rendered rows are fetched in source batches. PDF content and arbitrary
audit metadata are never selected. Private notes and unsigned encounters are
deliberately absent, including from the export.
"""

from datetime import datetime, time, timedelta
from itertools import islice
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Case, CharField, F, Q, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from care.models import (
    AuditEvent, ClinicalEncounter, ClinicalNote, ConsentRecord, LabRequest, LabResult,
    MessageThread, PatientEvent, PatientMedicalProfile, PatientMedicalProfileRevision,
    PatientSubscription, TreatmentAuthorization, WeightEntry,
)
from care.services import record_audit
from practices.models import CompanyMembership, Patient
from practices.tenancy import scope_queryset
from .patient_care_forms import PROFILE_FIELDS
from .record_forms import ClinicalRecordFilterForm, RECORD_CATEGORIES, RECORD_PURPOSES, ScopedPatientDirectoryFilterForm
from .views import StaffCompanyRequiredMixin


CLINICAL_ROLES = (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.SUPER_ADMIN)
RECORD_TIMEZONE = ZoneInfo('Africa/Johannesburg')
AUDIT_LABELS = {
    'patient.record_viewed': 'Patient record viewed',
    'patient.clinical_record_viewed': 'Clinical record viewed',
    'patient.clinical_record_exported': 'Clinical record exported',
    'patient.contact_updated': 'Contact details updated',
    'weight_entry.created': 'Weight entry recorded',
    'appointment.created': 'Appointment created',
    'appointment.patient_booked': 'Appointment booked by patient',
    'consultation.signed': 'Consultation signed',
    'lab_request.created': 'Blood test requested',
    'lab_result.uploaded': 'Blood test report uploaded',
    'lab_request.reviewed': 'Blood test report reviewed',
    'lab_result.downloaded': 'Blood test report downloaded',
    'treatment.authorized': 'Treatment authorization recorded',
    'subscription.enrolled_local': 'Local subscription recorded',
    'message.sent': 'Secure message sent',
    'message.thread_viewed': 'Secure conversation viewed',
    'shipment.created': 'Shipment created',
    'shipment.locked': 'Shipment contents locked',
    'shipment.dispatched': 'Shipment dispatched',
    'pharmacy_order.submitted': 'Pharmacy order requested',
    'pharmacy_order.accepted': 'Pharmacy order accepted',
    'pharmacy_order.cancelled': 'Pharmacy order cancelled',
}


def staff_memberships(actor, *, clinical=False):
    queryset = CompanyMembership.objects.filter(
        user=actor, user__is_active=True, is_active=True, company__is_active=True,
        role__in=CLINICAL_ROLES if clinical else CompanyMembership.Role.values,
    ).select_related('company')
    return {membership.company_id: membership for membership in scope_queryset(queryset)}


def scoped_source(model, patients):
    """Checking both IDs also rejects malformed imported cross-tenant rows."""
    return scope_queryset(model.objects.filter(patient_id__in=[patient.pk for patient in patients],
                                                company_id=F('patient__company_id')))


def safe_profile_answers(answers):
    if not isinstance(answers, dict):
        return []
    return [{'key': key, 'label': label, 'answer': answers[key]} for key, label, _ in PROFILE_FIELDS
            if isinstance(answers.get(key), str) and answers[key].strip()]


def patient_directory_context(view):
    """A broader directory never turns a foreign practice row into a local URL."""
    memberships = staff_memberships(view.request.user)
    data = view.request.GET.copy()
    data.setdefault('scope', 'current')
    scope_all = data['scope'] == 'all'
    companies = [member.company for member in memberships.values()] if scope_all else [view.company]
    form = ScopedPatientDirectoryFilterForm(data, company=view.company, companies=companies)
    queryset = Patient.objects.filter(company__in=companies, is_active=True).select_related(
        'user', 'assigned_doctor', 'company',
    ).order_by('last_name', 'first_name', 'company__name', 'pk')
    patient_count = queryset.count()
    filters = {}
    if form.is_valid():
        query, clinician = form.cleaned_data['q'], form.cleaned_data['clinician']
        for term in query.split():
            queryset = queryset.filter(
                Q(first_name__icontains=term) | Q(last_name__icontains=term)
                | Q(id_number__icontains=term) | Q(user__email__icontains=term)
                | Q(medical_record_number__icontains=term),
            )
        if clinician:
            queryset = queryset.filter(assigned_doctor=clinician)
        filters = {'q': query, 'clinician': clinician.pk if clinician else '', 'scope': form.cleaned_data['scope']}
    else:
        queryset = queryset.none()
    context = view.paginate(queryset, **filters)
    patients = list(context['page_obj'].object_list)
    authorizations, subscriptions = {}, {}
    for authorization in scoped_source(TreatmentAuthorization, patients).filter(
        product__company_id=F('company_id'),
    ).select_related('product').order_by('-starts_on', '-created_at', '-pk'):
        authorizations.setdefault(authorization.patient_id, authorization)
    for subscription in scoped_source(PatientSubscription, patients).order_by('-created_at', '-pk'):
        subscriptions.setdefault(subscription.patient_id, subscription)
    for patient in patients:
        patient.record_authorization = authorizations.get(patient.pk)
        patient.record_subscription = subscriptions.get(patient.pk)
        patient.in_current_practice = patient.company_id == view.company.pk
        patient.can_open_clinical_record = patient.in_current_practice and memberships[patient.company_id].role in CLINICAL_ROLES
    context.update(filter_form=form, patients=patients, patient_count=patient_count,
                   directory_all_practices=scope_all, directory_company_count=len(companies))
    return context


class RecordTimeline:
    def __init__(self, *, patients, memberships, filters, current_company):
        self.patients = patients
        self.records = {patient.pk: patient for patient in patients}
        self.companies = {patient.company_id: patient.company for patient in patients}
        self.current_company = current_company
        self.filters = filters
        doctor_companies = [company_id for company_id, member in memberships.items()
                            if member.role == CompanyMembership.Role.DOCTOR]
        event_category = Case(
            When(category='clinical', then=Value('clinical')),
            When(category__in=('medication', 'delivery'), then=Value('supply')),
            When(category='appointment', then=Value('appointments')),
            When(category='message', then=Value('messages')),
            default=Value('system'), output_field=CharField(),
        )
        # Only explicitly shared events enter this record. Typed sources below
        # add the clinical content; opaque internal event text is not a backdoor
        # to another doctor's drafts, private notes or task descriptions.
        events = scoped_source(PatientEvent, patients).filter(is_patient_visible=True)
        notes = scoped_source(ClinicalNote, patients).filter(is_private=False, signed_encounter__isnull=True)
        encounters = scoped_source(ClinicalEncounter, patients).filter(status=ClinicalEncounter.Status.SIGNED)
        labs = scoped_source(LabRequest, patients)
        results = scoped_source(LabResult, patients).filter(
            lab_request__patient_id=F('patient_id'), lab_request__company_id=F('company_id'),
        ).defer('content', 'sha256')
        revisions = scoped_source(PatientMedicalProfileRevision, patients).filter(
            company_id__in=doctor_companies, profile__patient_id=F('patient_id'),
            profile__company_id=F('company_id'),
        )
        self.sources = {
            'event': (events, 'occurred_at', event_category),
            'note': (notes.select_related('author'), 'created_at', Value('clinical')),
            'consultation': (encounters.select_related('clinician', 'signed_by'), 'occurred_at', Value('clinical')),
            'lab_request': (labs.select_related('requested_by'), 'created_at', Value('clinical')),
            'lab_review': (labs.filter(status=LabRequest.Status.REVIEWED, reviewed_at__isnull=False).select_related('reviewed_by'), 'reviewed_at', Value('clinical')),
            'lab_result': (results.select_related('uploaded_by'), 'created_at', Value('clinical')),
            'profile': (revisions.select_related('saved_by'), 'created_at', Value('clinical')),
            'consent': (scoped_source(ConsentRecord, patients), 'created_at', Value('system')),
            'authorization': (scoped_source(TreatmentAuthorization, patients).filter(
                product__company_id=F('company_id'),
            ).select_related('product', 'prescribed_by'), 'created_at', Value('supply')),
            'weight': (scoped_source(WeightEntry, patients).select_related('recorded_by'), 'created_at', Value('clinical')),
            'audit': (scoped_source(AuditEvent, patients).filter(action__in=AUDIT_LABELS).select_related('actor').defer('metadata', 'ip_address'), 'created_at', Value('system')),
        }

    def index(self, *, before=None):
        querysets = []
        for kind, (queryset, date_field, category) in self.sources.items():
            # Viewing a record itself adds an audit entry. Retaining this cutoff
            # on pagination/export prevents that new entry shifting every page.
            queryset = queryset.filter(created_at__lte=self.filters['snapshot']).order_by().annotate(
                timeline_at=F(date_field), timeline_kind=Value(kind, output_field=CharField()),
                timeline_id=F('pk'), timeline_category=category,
            )
            # Rows created earlier can become visible only after a later sign or
            # review. Do not let those later transitions enter an older snapshot.
            if kind == 'consultation':
                queryset = queryset.filter(Q(signed_at__lte=self.filters['snapshot']) | Q(signed_at__isnull=True))
            elif kind == 'lab_review':
                queryset = queryset.filter(reviewed_at__lte=self.filters['snapshot'])
            if before is not None:
                older = Q(**{f'{date_field}__lt': before['at']}) | Q(**{date_field: before['at'], 'pk__lt': before['id']})
                if kind > before['kind']:
                    older |= Q(**{date_field: before['at'], 'pk': before['id']})
                queryset = queryset.filter(older)
            if self.filters['category'] != 'all':
                queryset = queryset.filter(timeline_category=self.filters['category'])
            if self.filters.get('date_from'):
                start = timezone.make_aware(datetime.combine(self.filters['date_from'], time.min), RECORD_TIMEZONE)
                queryset = queryset.filter(**{f'{date_field}__gte': start})
            if self.filters.get('date_to'):
                end = timezone.make_aware(datetime.combine(self.filters['date_to'] + timedelta(days=1), time.min), RECORD_TIMEZONE)
                queryset = queryset.filter(**{f'{date_field}__lt': end})
            querysets.append(queryset.values('timeline_at', 'timeline_kind', 'timeline_id', 'timeline_category'))
        return querysets[0].union(*querysets[1:], all=True).order_by('-timeline_at', '-timeline_id', 'timeline_kind')

    def render_rows(self, index_rows):
        rows = list(index_rows)
        ids = {}
        for row in rows:
            ids.setdefault(row['timeline_kind'], []).append(row['timeline_id'])
        objects = {kind: {obj.pk: obj for obj in self.sources[kind][0].filter(pk__in=keys)}
                   for kind, keys in ids.items()}
        result = []
        for row in rows:
            obj = objects[row['timeline_kind']].get(row['timeline_id'])
            if obj is not None:
                result.append(self.render_row(row, obj))
        return result

    def render_row(self, row, obj):
        kind = row['timeline_kind']
        entry = {
            'id': obj.pk, 'kind': kind, 'at': row['timeline_at'],
            'category': row['timeline_category'], 'category_label': dict(RECORD_CATEGORIES)[row['timeline_category']],
            'company_id': obj.company_id, 'company_name': self.companies[obj.company_id].name,
            'patient_id': obj.patient_id, 'title': '', 'detail': '', 'actor': '', 'url': '', 'answers': [],
        }
        current = obj.company_id == self.current_company.pk
        if kind == 'event':
            entry.update(title=obj.title, detail=obj.detail)
        elif kind == 'note':
            entry.update(title=obj.get_note_type_display(), detail=obj.body,
                         actor=obj.author.full_name if obj.author else 'Recorded clinician')
        elif kind == 'consultation':
            entry.update(title='Signed consultation', detail=obj.clinical_summary,
                         actor=obj.signed_by.full_name if obj.signed_by else obj.clinician.full_name,
                         signed_at=obj.signed_at)
            if current:
                entry['url'] = reverse('portal:clinical-consultation-detail', args=[obj.pk])
        elif kind == 'lab_request':
            entry.update(title=f'Blood test requested: {obj.panel_name}', actor=obj.requested_by.full_name,
                         detail=f'Requested {obj.requested_on.isoformat()}.' + (f' Due {obj.due_on.isoformat()}.' if obj.due_on else ''))
            if current:
                entry['url'] = reverse('portal:clinical-lab-detail', args=[obj.pk])
        elif kind == 'lab_review':
            entry.update(title=f'Blood test reviewed: {obj.panel_name}', detail=obj.result_summary,
                         actor=obj.reviewed_by.full_name if obj.reviewed_by else 'Recorded clinician')
            if current:
                entry['url'] = reverse('portal:clinical-lab-detail', args=[obj.pk])
        elif kind == 'lab_result':
            entry.update(title='Blood test report uploaded', detail=f'{obj.filename} · {obj.size} bytes',
                         actor=obj.uploaded_by.full_name)
            if current:
                entry['url'] = reverse('portal:clinical-lab-detail', args=[obj.lab_request_id])
        elif kind == 'profile':
            entry.update(title=f'Self-reported medical profile · version {obj.revision}',
                         detail='Patient-provided history, not a clinical assessment.',
                         actor=obj.saved_by.full_name if obj.saved_by else 'Patient',
                         answers=safe_profile_answers(obj.answers))
        elif kind == 'consent':
            entry.update(title=obj.get_consent_type_display(), detail=(
                f'{"Accepted" if obj.accepted else "Not accepted"} · version {obj.document_version}'
            ), accepted_at=obj.accepted_at)
        elif kind == 'authorization':
            entry.update(title=f'Treatment authorization: {obj.product}', actor=obj.prescribed_by.full_name,
                         detail=f'{obj.max_dose} · {obj.quantity_per_cycle} per cycle · {obj.starts_on.isoformat()} to {obj.expires_on.isoformat()} · {obj.get_status_display()}')
        elif kind == 'weight':
            entry.update(title=f'Weight recorded: {obj.weight_kg} kg',
                         detail=f'Measured {obj.recorded_on.isoformat()}.',
                         actor=obj.recorded_by.full_name if obj.recorded_by else 'Recorded user')
        elif kind == 'audit':
            entry.update(title=AUDIT_LABELS[obj.action], actor=obj.actor.full_name if obj.actor else 'System',
                         detail='Access or activity recorded in this practice’s audit trail.')
        return entry


class ClinicalRecordAccessMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    http_method_names = ('get', 'head', 'options')

    def resolve_record(self, filter_data=None):
        if self.membership.role not in CLINICAL_ROLES:
            raise PermissionDenied('Clinical records are available to doctors and Super Admins only.')
        self.patient = get_object_or_404(
            Patient.objects.select_related('user', 'company', 'assigned_doctor'),
            pk=self.kwargs['pk'], company=self.company, is_active=True,
        )
        data = self.request.GET.copy() if filter_data is None else filter_data.copy()
        data.setdefault('scope', 'current')
        data.setdefault('category', 'all')
        self.filter_form = ClinicalRecordFilterForm(data)
        self.memberships = staff_memberships(self.request.user, clinical=True)
        self.records = [self.patient]
        self.valid_filters = self.filter_form.is_valid()
        if self.valid_filters and self.filter_form.cleaned_data['scope'] == 'all' and self.patient.user_id:
            self.records = list(Patient.objects.filter(
                user_id=self.patient.user_id, company_id__in=self.memberships, is_active=True,
            ).select_related('company', 'assigned_doctor', 'user').order_by('company__name', 'pk'))
        self.filters = self.filter_form.cleaned_data if self.valid_filters else {}
        self.timeline = RecordTimeline(patients=self.records, memberships=self.memberships,
                                       filters=self.filters, current_company=self.company) if self.valid_filters else None

    def audit_access(self, action):
        if self.request.method != 'GET' or not self.valid_filters:
            return
        for patient in self.records:
            record_audit(company=patient.company, actor=self.request.user, patient=patient, target=patient,
                         action=action, request=self.request, metadata={
                             'source_patient_id': self.patient.pk, 'scope': self.filters['scope'],
                             'reason': self.filters.get('reason', '') if self.filters['scope'] == 'all' else '',
                         })

    def query_string(self):
        return urlencode({key: value.isoformat() if hasattr(value, 'isoformat') else value
                          for key, value in self.filters.items() if value})

    def sidebar(self):
        patient = self.patient
        authorization = scoped_source(TreatmentAuthorization, [patient]).filter(
            product__company_id=F('company_id'),
        ).select_related('product', 'prescribed_by').order_by('-starts_on', '-created_at', '-pk').first()
        if authorization:
            authorization.is_current = (authorization.status == TreatmentAuthorization.Status.ACTIVE
                                        and authorization.starts_on <= timezone.localdate() <= authorization.expires_on)
        consents = []
        seen = set()
        for consent in scoped_source(ConsentRecord, [patient]).order_by('-created_at', '-pk'):
            if consent.consent_type not in seen:
                consents.append(consent)
                seen.add(consent.consent_type)
        weights = list(scoped_source(WeightEntry, [patient]).order_by('-recorded_on', '-pk')[:5])
        profile = None
        if self.membership.role == CompanyMembership.Role.DOCTOR:
            profile = scoped_source(PatientMedicalProfile, [patient]).first()
        return {'authorization': authorization, 'record_consents': consents, 'record_weights': weights,
                'medical_profile': profile, 'profile_answers': safe_profile_answers(profile.answers) if profile else []}


@method_decorator(never_cache, name='dispatch')
class ClinicalRecordView(ClinicalRecordAccessMixin, TemplateView):
    template_name = 'portal/record_detail.html'

    def get_context_data(self, **kwargs):
        self.resolve_record()
        context = super().get_context_data(**kwargs)
        page = Paginator(self.timeline.index() if self.timeline else [], 20).get_page(self.request.GET.get('page'))
        thread = MessageThread.objects.filter(company=self.company, patient=self.patient).order_by('-last_message_at', '-pk').first()
        query = self.query_string()
        context.update(company=self.company, active_membership=self.membership, nav_section='record',
                       page_title='Clinical record', patient=self.patient, filter_form=self.filter_form,
                       records=self.records, cross_practice=len(self.records) > 1,
                       page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query,
                       timeline_entries=self.timeline.render_rows(page.object_list) if self.timeline else [],
                       export_url=f'{reverse("portal:staff-patient-record-export", args=[self.patient.pk])}?{query}' if self.valid_filters else '',
                       care_purpose=dict(RECORD_PURPOSES).get(self.filters.get('reason', ''), ''),
                       messages_url=f'{reverse("portal:staff-inbox")}?{urlencode({"thread": thread.pk})}' if thread else reverse('portal:staff-inbox'),
                       is_doctor=self.membership.role == CompanyMembership.Role.DOCTOR)
        if self.valid_filters:
            from .record_history import timeline_links
            context.update(timeline_links(self, list(page.object_list), has_next=page.has_next()))
            context.update(self.sidebar())
        from .patient_workspace import patient_workspace_context
        context.update(patient_workspace_context(self.request, self.company, self.membership, self.patient, 'history'))
        self.audit_access('patient.clinical_record_viewed')
        return context


@method_decorator(never_cache, name='dispatch')
class ClinicalRecordExportView(ClinicalRecordAccessMixin, View):
    def get(self, request, *args, **kwargs):
        self.resolve_record()
        if not self.valid_filters:
            return JsonResponse({'errors': self.filter_form.errors.get_json_data()}, status=400)
        # Batch source loading avoids N+1 and never fetches lab attachment bytes.
        index = self.timeline.index().iterator(chunk_size=200)
        entries = []
        while batch := list(islice(index, 200)):
            entries.extend(self.timeline.render_rows(batch))
        for entry in entries:
            entry.pop('url', None)
        sidebar = self.sidebar()
        authorization = sidebar['authorization']
        payload = {
            'schema_version': 1, 'exported_at': timezone.now(), 'scope': self.filters['scope'],
            'care_purpose': self.filters.get('reason', ''), 'filters': self.filters,
            'source_patient_id': self.patient.pk,
            'records': [{'patient_id': patient.pk, 'company_id': patient.company_id, 'company': patient.company.name,
                         'first_name': patient.first_name, 'last_name': patient.last_name,
                         'medical_record_number': patient.medical_record_number} for patient in self.records],
            'timeline': entries,
            'current_practice_summary': {
                'company_id': self.company.pk,
                'authorization': ({'id': authorization.pk, 'product': str(authorization.product),
                                   'max_dose': authorization.max_dose, 'quantity_per_cycle': authorization.quantity_per_cycle,
                                   'starts_on': authorization.starts_on, 'expires_on': authorization.expires_on,
                                   'status': authorization.status, 'is_current': authorization.is_current} if authorization else None),
                'recent_weights': [{'id': weight.pk, 'recorded_on': weight.recorded_on, 'weight_kg': weight.weight_kg}
                                   for weight in sidebar['record_weights']],
                'consents': [{'id': consent.pk, 'type': consent.consent_type, 'version': consent.document_version,
                              'accepted': consent.accepted, 'accepted_at': consent.accepted_at}
                             for consent in sidebar['record_consents']],
                'self_reported_profile': sidebar['profile_answers'],
            },
            'exclusions': ['Private notes', 'Unsigned consultations', 'Report attachment bytes',
                           'Internal event text', 'Arbitrary audit metadata and IP addresses'],
        }
        response = JsonResponse(payload, json_dumps_params={'indent': 2})
        response['Content-Disposition'] = f'attachment; filename="patient-{self.patient.pk}-clinical-record.json"'
        response['X-Content-Type-Options'] = 'nosniff'
        self.audit_access('patient.clinical_record_exported')
        return response
