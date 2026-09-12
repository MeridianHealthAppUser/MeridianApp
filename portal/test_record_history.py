"""Stable history cursors and typed-text Excel exports retain clinical scoping."""

from datetime import timedelta
from io import BytesIO
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zipfile import ZipFile

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, ClinicalEncounter, ClinicalNote, LabRequest, PatientEvent
from practices.models import CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from .record_history import HISTORY_PAGE_SIZE
from . import test_records


class RecordHistoryTests(TestCase):
    # Reuse only the synthetic clinical-record fixtures and helpers, not its
    # already covered test methods.
    setUpTestData = classmethod(test_records.ClinicalRecordTests.setUpTestData.__func__)
    event = classmethod(test_records.ClinicalRecordTests.event.__func__)
    login = test_records.ClinicalRecordTests.login
    url = test_records.ClinicalRecordTests.url
    page = test_records.ClinicalRecordTests.page

    def seed_history(self, count=45):
        moment = timezone.now() - timedelta(days=5)
        for index in range(count):
            self.event(self.patient, f'History entry {index}', occurred_at=moment - timedelta(minutes=index))

    def links(self, **filters):
        self.seed_history()
        self.login()
        response = self.page(**filters)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['timeline_next_url'])
        self.assertTrue(response.context['timeline_download_url'])
        return response

    def entry_keys(self, html):
        return re.findall(r'data-entry-key="([a-z_]+:\d+)"', html)

    def walk(self, next_url):
        keys, contents, urls = [], [], set()
        while next_url:
            self.assertNotIn(next_url, urls, 'A cursor must make forward progress.')
            urls.add(next_url)
            self.assertLess(len(urls), 15)
            response = self.client.get(next_url)
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()
            self.assertLessEqual(data['count'], HISTORY_PAGE_SIZE)
            keys.extend(self.entry_keys(data['html']))
            contents.append(data['html'])
            next_url = data['next_url']
        return keys, ''.join(contents)

    def xlsx(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response.headers['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        archive = ZipFile(BytesIO(response.content))
        xml = {name: archive.read(name) for name in archive.namelist()}
        for name, content in xml.items():
            if name.endswith(('.xml', '.rels')):
                ElementTree.fromstring(content)
        return response, xml

    def cells(self, xml):
        root = ElementTree.fromstring(xml)
        namespace = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        return [[cell.find('x:is/x:t', namespace).text or '' for cell in row.findall('x:c', namespace)]
                for row in root.findall('x:sheetData/x:row', namespace)]

    def test_cursor_pages_include_all_rows_once_and_add_no_view_audits(self):
        first = self.links()
        audit_count = AuditEvent.objects.count()
        keys, _ = self.walk(first.context['timeline_next_url'])
        initial = [f"{row['kind']}:{row['id']}" for row in first.context['timeline_entries']]
        self.assertEqual(len(initial), 20)
        self.assertEqual(len(initial + keys), len(set(initial + keys)))
        self.assertEqual(len(initial + keys), first.context['page_obj'].paginator.count)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_keyset_ties_order_by_id_then_source_and_new_events_do_not_shift_cursor(self):
        moment = timezone.now() - timedelta(days=2)
        for index in range(30):
            event = self.event(self.patient, f'Tied event {index}', occurred_at=moment)
            note = ClinicalNote.objects.create(company=self.alpha, patient=self.patient, author=self.doctor, body=f'Tied note {index}')
            PatientEvent.objects.filter(pk=event.pk).update(created_at=moment)
            ClinicalNote.objects.filter(pk=note.pk).update(created_at=moment)
        self.login()
        first = self.page(category='clinical')
        initial = [f"{row['kind']}:{row['id']}" for row in first.context['timeline_entries']]
        self.event(self.patient, 'NEW_AFTER_SNAPSHOT', occurred_at=moment - timedelta(days=10))
        keys, html = self.walk(first.context['timeline_next_url'])
        self.assertEqual(len(initial + keys), first.context['page_obj'].paginator.count)
        self.assertEqual(len(initial + keys), len(set(initial + keys)))
        self.assertNotIn('NEW_AFTER_SNAPSHOT', html)

    def test_later_signing_or_lab_review_does_not_enter_previous_snapshot(self):
        draft = ClinicalEncounter.objects.create(company=self.alpha, patient=self.patient, clinician=self.doctor,
            occurred_at=timezone.now() - timedelta(days=12), clinical_summary='LATE_SIGN_SECRET')
        lab = LabRequest.objects.create(company=self.alpha, patient=self.patient, requested_by=self.doctor, panel_name='Earlier request')
        first = self.links(category='clinical')
        ClinicalEncounter.objects.filter(pk=draft.pk).update(status='signed', signed_at=timezone.now(), signed_by=self.doctor)
        LabRequest.objects.filter(pk=lab.pk).update(status='reviewed', reviewed_at=timezone.now(), reviewed_by=self.doctor, result_summary='LATE_REVIEW_SECRET')
        _, html = self.walk(first.context['timeline_next_url'])
        _, workbook = self.xlsx(first.context['timeline_download_url'])
        for secret in ('LATE_SIGN_SECRET', 'LATE_REVIEW_SECRET'):
            self.assertNotIn(secret, html)
            self.assertNotIn(secret.encode(), workbook['xl/worksheets/sheet1.xml'])

    def test_cursor_preserves_filters_scope_care_purpose_and_snapshot(self):
        self.seed_history()
        for number in range(25):
            self.event(self.beta_patient, f'Beta older event {number}', occurred_at=timezone.now() - timedelta(days=7))
        self.login()
        first = self.page(scope='all', reason='covering_colleague', category='clinical',
                          date_from=timezone.localdate() - timedelta(days=8), date_to=timezone.localdate())
        _, html = self.walk(first.context['timeline_next_url'])
        self.assertIn('Beta older event', html)
        self.assertNotIn('System and consent', html)
        for forbidden in ('UNAUTHORIZED_PRACTICE_SECRET', 'INACTIVE_PRACTICE_SECRET', 'SAME_NAME_OTHER_PATIENT_SECRET'):
            self.assertNotIn(forbidden, html)
        _, workbook = self.xlsx(first.context['timeline_download_url'])
        summary = dict(self.cells(workbook['xl/worksheets/sheet2.xml'])[1:])
        self.assertEqual(summary['Care purpose'], 'covering_colleague')
        self.assertEqual(summary['Scope'], 'all')
        self.assertEqual(summary['Activity filter'], 'clinical')
        self.assertIn(self.beta.name, summary['Included practices'])

    def test_anonymous_patient_and_practice_admin_cannot_load_or_export(self):
        first = self.links()
        urls = (first.context['timeline_next_url'], first.context['timeline_download_url'])
        self.client.logout()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)
        for actor in (self.user, self.admin):
            self.login(actor)
            for url in urls:
                self.assertIn(self.client.get(url).status_code, (403, 409))

    def test_other_actor_or_practice_switch_cannot_reuse_a_signed_view(self):
        first = self.links()
        urls = (first.context['timeline_next_url'], first.context['timeline_download_url'])
        self.login(self.colleague)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 409)
        self.login()
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.beta.pk
        session.save()
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 409)
            self.assertNotContains(response, 'Alpha visible event', status_code=409)

    def test_changed_cross_practice_roles_or_patient_link_require_reload(self):
        first = self.links(scope='all', reason='direct_care')
        urls = (first.context['timeline_next_url'], first.context['timeline_download_url'])
        for change in ({'role': 'super_admin'}, {'role': 'doctor', 'is_active': False}):
            CompanyMembership.objects.filter(company=self.beta, user=self.doctor).update(**change)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 409)
        CompanyMembership.objects.filter(company=self.beta, user=self.doctor).update(role='doctor', is_active=True)
        Patient.objects.filter(pk=self.beta_patient.pk).update(user=None)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 409)

    def test_current_membership_revocation_or_inactive_patient_is_checked_fresh(self):
        first = self.links()
        urls = (first.context['timeline_next_url'], first.context['timeline_download_url'])
        CompanyMembership.objects.filter(company=self.alpha, user=self.doctor).update(is_active=False)
        for url in urls:
            self.assertIn(self.client.get(url).status_code, (403, 409))
        CompanyMembership.objects.filter(company=self.alpha, user=self.doctor).update(is_active=True)
        self.login()
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_missing_tampered_expired_cursor_and_changed_filters_do_not_leak_or_write(self):
        first = self.links()
        parts = urlsplit(first.context['timeline_next_url'])
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        before = AuditEvent.objects.count()
        for changes in ({'cursor': ''}, {'cursor': 'tampered'}, {'history_context': 'tampered'},
                        {'category': 'all'}, {'scope': 'all'}, {'snapshot': timezone.now().isoformat()}):
            response = self.client.get(parts.path, {**query, **changes})
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('html', response.json())
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 13 * 60 * 60):
            self.assertEqual(self.client.get(first.context['timeline_next_url']).status_code, 409)
            self.assertEqual(self.client.get(first.context['timeline_download_url']).status_code, 409)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_cursor_from_another_filtered_view_cannot_be_transplanted(self):
        first = self.links()
        other = self.page(category='clinical')
        old_query = parse_qs(urlsplit(first.context['timeline_next_url']).query)
        new_url = urlsplit(other.context['timeline_next_url'])
        new_query = {key: value[0] for key, value in parse_qs(new_url.query).items()}
        new_query['cursor'] = old_query['cursor'][0]
        self.assertEqual(self.client.get(new_url.path, new_query).status_code, 409)

    def test_cursor_context_is_bound_to_patient_route(self):
        first = self.links()
        for key, name in (('timeline_next_url', 'staff-patient-record-history'), ('timeline_download_url', 'staff-patient-record-excel')):
            query = urlsplit(first.context[key]).query
            wrong = reverse(f'portal:{name}', args=[self.beta_patient.pk])
            self.assertEqual(self.client.get(wrong + '?' + query).status_code, 409)

    def test_fragment_escapes_clinical_text_and_never_includes_private_sources(self):
        first = self.links(category='clinical')
        oldest = PatientEvent.objects.filter(patient=self.patient, title__startswith='History entry').order_by('occurred_at').first()
        PatientEvent.objects.filter(pk=oldest.pk).update(title='<img src=x onerror=alert(1)>')
        _, html = self.walk(first.context['timeline_next_url'])
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img src=x onerror', html)
        for secret in ('OTHER_PRIVATE_NOTE_SECRET', 'OWN_PRIVATE_NOTE_SECRET', 'OTHER_DRAFT_SECRET', 'OWN_DRAFT_SECRET',
                       'INTERNAL_EVENT_SECRET', 'BINARY_REPORT_SECRET', 'RAW_AUDIT_METADATA_SECRET'):
            self.assertNotIn(secret, html)

    def test_excel_is_real_workbook_full_filtered_history_without_private_sources(self):
        first = self.links(scope='all', reason='direct_care')
        response, workbook = self.xlsx(first.context['timeline_download_url'])
        rows = self.cells(workbook['xl/worksheets/sheet1.xml'])
        self.assertEqual(len(rows) - 1, first.context['page_obj'].paginator.count)
        self.assertEqual(rows[0][0], 'Date and time (SAST)')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn(f'patient-{self.patient.pk}-history.xlsx', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        content = b''.join(workbook.values())
        for secret in ('OTHER_PRIVATE_NOTE_SECRET', 'OWN_PRIVATE_NOTE_SECRET', 'OTHER_DRAFT_SECRET', 'OWN_DRAFT_SECRET',
                       'INTERNAL_EVENT_SECRET', 'BINARY_REPORT_SECRET', 'RAW_AUDIT_METADATA_SECRET', 'DRAFT_AUDIT_SECRET',
                       'UNAUTHORIZED_PRACTICE_SECRET', 'INACTIVE_PRACTICE_SECRET', 'SAME_NAME_OTHER_PATIENT_SECRET'):
            self.assertNotIn(secret.encode(), content)
        self.assertIn(b'Beta visible event', content)

    def test_excel_strings_do_not_execute_formulas_or_truncate_long_details(self):
        name = '=HYPERLINK("https://example.invalid", "not a formula")'
        long_text = 'A' * 35000 + '\x01' + 'B' * 17000
        self.event(self.patient, name, detail=long_text)
        self.login()
        first = self.page(category='clinical')
        _, workbook = self.xlsx(first.context['timeline_download_url'])
        sheet = workbook['xl/worksheets/sheet1.xml']
        self.assertNotIn(b'<f', sheet)
        root = ElementTree.fromstring(sheet)
        namespace = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        self.assertTrue(all(cell.attrib['t'] == 'inlineStr' for cell in root.findall('.//x:c', namespace)))
        rows = self.cells(sheet)
        formula_rows = [row for row in rows[1:] if row[3] == name or (row[4] and row[4][0] in ('A', 'B'))]
        self.assertIn(name, [row[3] for row in rows])
        self.assertEqual(''.join(row[4] for row in formula_rows), long_text.replace('\x01', '\ufffd'))
        self.assertTrue(all(len(cell) <= 15000 for row in rows for cell in row))

    def test_excel_export_is_audited_once_per_actual_practice_without_clinical_payload(self):
        first = self.links(scope='all', reason='covering_colleague')
        before = AuditEvent.objects.filter(action='patient.clinical_record_exported').count()
        self.xlsx(first.context['timeline_download_url'])
        audits = AuditEvent.objects.filter(action='patient.clinical_record_exported').order_by('-pk')[:2]
        self.assertEqual(AuditEvent.objects.filter(action='patient.clinical_record_exported').count(), before + 2)
        self.assertEqual({row.company_id for row in audits}, {self.alpha.pk, self.beta.pk})
        self.assertTrue(all(row.metadata['reason'] == 'covering_colleague' for row in audits))

    def test_empty_filtered_history_still_downloads_a_valid_header_only_workbook(self):
        self.login()
        first = self.page(date_from='2000-01-01', date_to='2000-01-02')
        self.assertFalse(first.context['timeline_next_url'])
        _, workbook = self.xlsx(first.context['timeline_download_url'])
        self.assertEqual(len(self.cells(workbook['xl/worksheets/sheet1.xml'])), 1)
        summary = dict(self.cells(workbook['xl/worksheets/sheet2.xml'])[1:])
        self.assertEqual(summary['From date'], '2000-01-01')
        self.assertEqual(summary['To date'], '2000-01-02')

    def test_excel_queries_do_not_load_report_bytes_or_arbitrary_audit_metadata(self):
        first = self.links()
        with CaptureQueriesContext(connection) as queries:
            self.xlsx(first.context['timeline_download_url'])
        for query in queries:
            sql = query['sql'].lower()
            if sql.lstrip().startswith('select'):
                self.assertNotIn('"care_labresult"."content"', sql)
                self.assertNotIn('"care_labresult"."sha256"', sql)
                self.assertNotIn('"care_auditevent"."metadata"', sql)
                self.assertNotIn('"care_auditevent"."ip_address"', sql)

    def test_super_admin_export_omits_doctor_only_profile_answers(self):
        self.seed_history()
        self.login(self.super_admin)
        first = self.page(scope='all', reason='direct_care')
        _, workbook = self.xlsx(first.context['timeline_download_url'])
        content = b''.join(workbook.values())
        self.assertNotIn(b'ALPHA_DOCTOR_ONLY_PROFILE', content)
        self.assertNotIn(b'BETA_DOCTOR_ONLY_PROFILE', content)
        self.assertIn(b'Signed consultation summary', content)

    def test_export_limit_is_explicit_error_not_partial_attachment_or_audit(self):
        first = self.links()
        before = AuditEvent.objects.count()
        with patch('portal.record_history.HISTORY_EXPORT_LIMIT', 1):
            response = self.client.get(first.context['timeline_download_url'])
        self.assertEqual(response.status_code, 413)
        self.assertIn('Nothing was truncated', response.json()['error'])
        self.assertNotIn('Content-Disposition', response.headers)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_head_does_not_record_view_or_download_and_post_is_rejected(self):
        first = self.links()
        before = AuditEvent.objects.count()
        for url in (first.context['timeline_next_url'], first.context['timeline_download_url']):
            response = self.client.head(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'')
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_invalid_record_filters_expose_no_download_or_history_link(self):
        self.login()
        response = self.page(scope='all')
        self.assertTrue(response.context['filter_form'].errors)
        self.assertFalse(response.context.get('timeline_download_url'))
        self.assertFalse(response.context.get('timeline_next_url'))
