"""Signed, keyset-paginated record history and bounded spreadsheet downloads."""

from datetime import datetime
from hashlib import sha256
from itertools import islice
from urllib.parse import urlencode

from django.core import signing
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from .record_views import ClinicalRecordAccessMixin, RECORD_TIMEZONE
from .record_xlsx import build_history_workbook


HISTORY_PAGE_SIZE = 20
HISTORY_EXPORT_LIMIT = 10_000
HISTORY_MAX_AGE = 12 * 60 * 60
CONTEXT_SALT = 'portal.record-history.context.v1'
CURSOR_SALT = 'portal.record-history.cursor.v1'
STALE_MESSAGE = 'This history view has expired or your practice access has changed. Reload the patient record before continuing.'


def _filter_payload(filters):
    return {key: value.isoformat() if hasattr(value, 'isoformat') else value
            for key, value in filters.items() if value}


def _record_scope(view):
    return sorted([[patient.pk, patient.company_id, patient.user_id, view.memberships[patient.company_id].role]
                   for patient in view.records])


def _make_context(view):
    return signing.dumps({'actor': view.request.user.pk, 'company': view.company.pk,
        'patient': view.patient.pk, 'records': _record_scope(view), 'filters': _filter_payload(view.filters)},
        salt=CONTEXT_SALT, compress=True)


def _cursor(context, row):
    return signing.dumps({'context': sha256(context.encode()).hexdigest(), 'at': row['timeline_at'].isoformat(),
                          'id': row['timeline_id'], 'kind': row['timeline_kind']}, salt=CURSOR_SALT, compress=True)


def _next_url(patient_id, context, row):
    return reverse('portal:staff-patient-record-history', args=[patient_id]) + '?' + urlencode({
        'history_context': context, 'cursor': _cursor(context, row),
    })


def timeline_links(view, index_rows, *, has_next):
    """Integration hook for an already resolved clinical-record page or tab."""
    context = _make_context(view)
    return {
        'timeline_next_url': _next_url(view.patient.pk, context, index_rows[-1]) if has_next and index_rows else '',
        'timeline_download_url': reverse('portal:staff-patient-record-excel', args=[view.patient.pk]) + '?' + urlencode({'history_context': context}),
        'timeline_export_limit': HISTORY_EXPORT_LIMIT,
    }


class InvalidHistoryContext(Exception):
    pass


class SignedHistoryMixin(ClinicalRecordAccessMixin):
    def resolve_history(self):
        context = self.request.GET.get('history_context', '')
        try:
            payload = signing.loads(context, salt=CONTEXT_SALT, max_age=HISTORY_MAX_AGE)
            if (not isinstance(payload, dict) or payload.get('actor') != self.request.user.pk or
                    payload.get('company') != self.company.pk or payload.get('patient') != self.kwargs['pk'] or
                    not isinstance(payload.get('filters'), dict)):
                raise InvalidHistoryContext
            # Extra filter parameters cannot change the scope of a signed view.
            if any(key in self.request.GET for key in ('scope', 'reason', 'category', 'date_from', 'date_to', 'snapshot', 'page')):
                raise InvalidHistoryContext
            self.resolve_record(filter_data=payload['filters'])
            if (not self.valid_filters or _filter_payload(self.filters) != payload['filters'] or
                    _record_scope(self) != payload.get('records')):
                raise InvalidHistoryContext
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise InvalidHistoryContext from None
        return context

    def decode_cursor(self, context):
        token = self.request.GET.get('cursor', '')
        try:
            payload = signing.loads(token, salt=CURSOR_SALT, max_age=HISTORY_MAX_AGE)
            if (not isinstance(payload, dict) or payload.get('context') != sha256(context.encode()).hexdigest() or
                    type(payload.get('id')) is not int or payload['id'] < 1 or
                    payload.get('kind') not in self.timeline.sources or not isinstance(payload.get('at'), str)):
                raise InvalidHistoryContext
            payload['at'] = datetime.fromisoformat(payload['at'])
            if timezone.is_naive(payload['at']) or not 1900 <= payload['at'].year <= 2100:
                raise InvalidHistoryContext
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise InvalidHistoryContext from None
        return payload


@method_decorator(never_cache, name='dispatch')
class ClinicalRecordHistoryView(SignedHistoryMixin, View):
    def get(self, request, *args, **kwargs):
        try:
            context = self.resolve_history()
            cursor = self.decode_cursor(context)
        except InvalidHistoryContext:
            return JsonResponse({'error': STALE_MESSAGE}, status=409)
        # Keyset pagination: no OFFSET and no new viewed audit on background loads.
        index_rows = list(self.timeline.index(before=cursor)[:HISTORY_PAGE_SIZE + 1])
        page_rows = index_rows[:HISTORY_PAGE_SIZE]
        entries = self.timeline.render_rows(page_rows)
        response = JsonResponse({
            'html': render_to_string('portal/includes/record_timeline_entries.html', {'timeline_entries': entries}),
            'next_url': _next_url(self.patient.pk, context, page_rows[-1]) if len(index_rows) > HISTORY_PAGE_SIZE else '',
            'count': len(entries),
        })
        response['X-Content-Type-Options'] = 'nosniff'
        return response


@method_decorator(never_cache, name='dispatch')
class ClinicalRecordExcelView(SignedHistoryMixin, View):
    def get(self, request, *args, **kwargs):
        try:
            self.resolve_history()
            if 'cursor' in request.GET:
                raise InvalidHistoryContext
        except InvalidHistoryContext:
            return JsonResponse({'error': STALE_MESSAGE}, status=409)
        if self.timeline.index().count() > HISTORY_EXPORT_LIMIT:
            return JsonResponse({'error': f'This export exceeds {HISTORY_EXPORT_LIMIT:,} history entries. Narrow the activity or date filters and download again. Nothing was truncated.'}, status=413)

        def entries():
            index = self.timeline.index().iterator(chunk_size=200)
            while batch := list(islice(index, 200)):
                yield from self.timeline.render_rows(batch)

        summary = [
            ('Patient record', str(self.patient.pk)),
            ('Patient name', f'{self.patient.first_name} {self.patient.last_name}'),
            ('Current practice', self.company.name),
            ('Included practices', ', '.join(patient.company.name for patient in self.records)),
            ('Scope', self.filters['scope']), ('Care purpose', self.filters.get('reason', '')),
            ('Activity filter', self.filters['category']),
            ('From date', self.filters.get('date_from') or ''), ('To date', self.filters.get('date_to') or ''),
            ('History snapshot (SAST)', timezone.localtime(self.filters['snapshot'], RECORD_TIMEZONE).isoformat()),
            ('Downloaded (SAST)', timezone.localtime(timezone.now(), RECORD_TIMEZONE).isoformat()),
            ('Excluded', 'Private notes; unsigned consultations; attachment bytes; internal events; arbitrary audit metadata and IP addresses.'),
            ('Format', 'All cells are text. Very long entries continue in additional rows. Unsupported XML control characters are replaced.'),
        ]
        content = build_history_workbook(entries(), summary=summary)
        response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="patient-{self.patient.pk}-history.xlsx"'
        response['X-Content-Type-Options'] = 'nosniff'
        self.audit_access('patient.clinical_record_exported')
        return response
