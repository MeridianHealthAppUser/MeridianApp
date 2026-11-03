"""Dispatch history is one scoped, month-filtered list with an equivalent Excel download."""
from datetime import date, datetime, timezone as datetime_timezone
from html.parser import HTMLParser
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import AuditEvent, Payment, PracticeSettings, Shipment, ShipmentItem, StockMovement
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture


class HistoryLinks(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.links = []
        self._link = None
        self.headers, self._heading = [], None
        self.current_pages, self._current_page = [], None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.append(attrs)
            self._link = attrs
            self._link['parts'] = []
        if tag == 'th':
            self._heading = []
        if tag == 'span' and attrs.get('aria-current') == 'page':
            self._current_page = []

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)
        if self._current_page is not None:
            self._current_page.append(data)
        if self._link is not None:
            self._link['parts'].append(data)

    def handle_endtag(self, tag):
        if tag == 'th' and self._heading is not None:
            self.headers.append(' '.join(''.join(self._heading).split()))
            self._heading = None
        if tag == 'span' and self._current_page is not None:
            self.current_pages.append(' '.join(''.join(self._current_page).split()))
            self._current_page = None
        if tag == 'a' and self._link is not None:
            self._link['text'] = ' '.join(''.join(self._link.pop('parts')).split())
            self._link = None


class DispatchHistoryTests(OperationsPortalFixture):
    def history(self, **params):
        return self.client.get(reverse('portal:ops-history'), params)

    def sent(self, timestamp=None, *, company=None, patient=None, product=None, **extra):
        company = company or self.company
        patient = patient or self.patient
        product = product or self.product
        timestamp = timestamp or timezone.now()
        snapshot = dict(patient_name=str(patient), patient_id_number=patient.id_number,
                        company_name=company.name, items=[dict(product=str(product), dose='Saved dose',
                                                            quantity=2, batch_note='Saved batch')])
        return self.shipment(company=company, patient=patient, product=product,
                             subscription=self.plan if company == self.company else None,
                             quantity=2, scheduled_for=timezone.localdate(timestamp),
                             status=extra.pop('status', Shipment.Status.DISPATCHED),
                             dispatched_at=timestamp, dispatch_snapshot=extra.pop('dispatch_snapshot', snapshot),
                             **extra)

    def ids(self, response):
        return [shipment.pk for shipment in response.context['shipments']]

    def workbook(self, **params):
        response = self.client.get(reverse('portal:ops-history-export'), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('.xlsx', response['Content-Disposition'])
        content = b''.join(response.streaming_content) if response.streaming else response.content
        response.close()
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        return response, workbook

    def records(self, workbook):
        rows = list(workbook['Dispatch history'].iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def export_links(self, response):
        return [link for link in HistoryLinks(response).links
                if urlsplit(link.get('href', '')).path == reverse('portal:ops-history-export')]

    def test_default_is_all_dispatch_history_without_week_or_list_switches(self):
        self.assertEqual(reverse('portal:ops-history-export'), '/dispatch-history/export.xlsx')
        old = self.sent(datetime(2020, 1, 2, tzinfo=datetime_timezone.utc))
        recent = self.sent(status=Shipment.Status.DELIVERED)
        self.shipment(status=Shipment.Status.DRAFT)
        self.shipment(status=Shipment.Status.READY)
        self.shipment(status=Shipment.Status.CANCELLED)
        foreign = self.sent(company=self.beta, patient=self.beta_patient, product=self.beta_product)
        response = self.history()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), [recent.pk, old.pk])
        self.assertEqual(response.context['page_obj'].paginator.per_page, 25)
        self.assertNotContains(response, 'Dispatch history view')
        self.assertNotContains(response, 'Previous week')
        self.assertNotContains(response, self.beta_product.name)
        self.assertNotIn(foreign.pk, self.ids(response))
        self.assertContains(response, 'Download Excel')
        form = response.context['filter_form']
        for field, label in (('start_month', 'From month'), ('end_month', 'To month')):
            self.assertEqual(form.fields[field].label, label)
            self.assertEqual(form.fields[field].widget.input_type, 'month')
            self.assertFalse(form.fields[field].required)
        self.assertNotIn('start', form.fields)
        self.assertNotIn('end', form.fields)
        for legacy in ({'view': 'week', 'week': '1990-01-01'},
                       {'view': 'list', 'week': 'invalid', 'start': 'bad', 'end': 'bad'}):
            with self.subTest(legacy=legacy):
                self.assertEqual(self.ids(self.history(**legacy)), [recent.pk, old.pk])

    def test_month_bounds_include_leap_day_and_use_sast_even_under_utc_request_timezone(self):
        before = self.sent(datetime(2024, 1, 31, 21, 59, 59, tzinfo=datetime_timezone.utc))
        first = self.sent(datetime(2024, 1, 31, 22, tzinfo=datetime_timezone.utc))
        leap_last = self.sent(datetime(2024, 2, 29, 21, 59, 59, tzinfo=datetime_timezone.utc))
        after = self.sent(datetime(2024, 2, 29, 22, tzinfo=datetime_timezone.utc))
        with timezone.override('UTC'):
            response = self.history(start_month='2024-02', end_month='2024-02')
            export, workbook = self.workbook(start_month='2024-02', end_month='2024-02')
        self.assertIn('dispatch-history-2024-02-to-2024-02.xlsx', export['Content-Disposition'])
        self.assertEqual(self.ids(response), [leap_last.pk, first.pk])
        self.assertEqual([row['Shipment'] for row in self.records(workbook)], [leap_last.pk, first.pk])
        self.assertNotIn(before.pk, self.ids(response))
        self.assertNotIn(after.pk, self.ids(response))

    def test_optional_month_bounds_and_december_rollover(self):
        november = self.sent(datetime(2026, 11, 30, 21, 59, 59, tzinfo=datetime_timezone.utc))
        december = self.sent(datetime(2026, 11, 30, 22, tzinfo=datetime_timezone.utc))
        last = self.sent(datetime(2026, 12, 31, 21, 59, 59, tzinfo=datetime_timezone.utc))
        january = self.sent(datetime(2026, 12, 31, 22, tzinfo=datetime_timezone.utc))
        self.assertEqual(self.ids(self.history(start_month='2026-12', end_month='2026-12')),
                         [last.pk, december.pk])
        self.assertEqual(self.ids(self.history(start_month='2026-12')), [january.pk, last.pk, december.pk])
        self.assertEqual(self.ids(self.history(end_month='2026-12')), [last.pk, december.pk, november.pk])

    def test_invalid_and_reversed_months_show_errors_and_cannot_be_exported(self):
        self.sent()
        PracticeSettings.objects.filter(company=self.company).update(store_view_log=True)
        for params in ({'start_month': 'bad'}, {'start_month': '2026-2'}, {'end_month': '2026-13'},
                       {'start_month': '2026-02-01'}, {'end_month': '0000-01'},
                       {'start_month': '2026-12', 'end_month': '2026-11'}):
            with self.subTest(params=params):
                response = self.history(**params)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['filter_form'].errors)
                self.assertEqual(self.ids(response), [])
                self.assertFalse(self.export_links(response))
                with patch('care.dispatch_export.dispatch_history_workbook') as writer:
                    export = self.client.get(reverse('portal:ops-history-export'), params)
                writer.assert_not_called()
                self.assertEqual(export.status_code, 400)
                self.assertNotIn('Content-Disposition', export)
        self.assertFalse(AuditEvent.objects.filter(action='dispatch.history_exported').exists())

    def test_pagination_search_and_download_share_the_same_order_and_filters(self):
        timestamp = datetime(2026, 5, 15, 12, tzinfo=datetime_timezone.utc)
        matching = [self.sent(timestamp, tracking_number=f'FIND-ME-{index:02d}') for index in range(31)]
        self.sent(timestamp, tracking_number='SOMETHING-ELSE')
        self.sent(datetime(2026, 6, 1, 12, tzinfo=datetime_timezone.utc), tracking_number='FIND-ME-LATER')
        params = dict(start_month='2026-05', end_month='2026-05', q='FIND-ME')
        legacy = dict(view='week', week='1990-01-01')
        first, second = self.history(**params, **legacy), self.history(**params, **legacy, page=2)
        expected = [shipment.pk for shipment in reversed(matching)]
        self.assertEqual(self.ids(first), expected[:25])
        self.assertEqual(self.ids(second), expected[25:])
        self.assertEqual(len(self.ids(second)), 6)
        self.assertEqual(second.context['page_obj'].paginator.count, 31)
        self.assertEqual(parse_qs(second.context['pagination_query']),
                         {key: [value] for key, value in params.items()})
        for response, current, destination in ((first, 1, 2), (second, 2, 1)):
            with self.subTest(page=current):
                markup = HistoryLinks(response)
                numbered = [link for link in markup.links if link.get('aria-label') == f'Page {destination}']
                self.assertEqual(len(numbered), 1)
                self.assertEqual(urlsplit(numbered[0]['href']).path, reverse('portal:ops-history'))
                self.assertEqual(parse_qs(urlsplit(numbered[0]['href']).query),
                                 {**{key: [value] for key, value in params.items()}, 'page': [str(destination)]})
                self.assertIn(str(current), markup.current_pages)
                direction = 'Next' if current == 1 else 'Previous'
                opposite = 'Previous' if current == 1 else 'Next'
                navigation = [link for link in markup.links if link.get('text') == direction]
                self.assertEqual(len(navigation), 1)
                self.assertEqual(navigation[0]['href'], numbered[0]['href'])
                self.assertFalse([link for link in markup.links if link.get('text') == opposite])
        export = self.export_links(second)
        self.assertEqual(len(export), 1)
        self.assertContains(second, 'data-search-region="table-export"', count=1)
        self.assertEqual(parse_qs(urlsplit(export[0]['href']).query),
                         {key: [value] for key, value in params.items()})
        self.assertEqual(parse_qs(urlsplit(second.context['search_clear_url']).query),
                         {'start_month': ['2026-05'], 'end_month': ['2026-05']})
        _, workbook = self.workbook(**params, page=2)
        self.assertEqual([row['Shipment'] for row in self.records(workbook)], expected)

    def test_snapshot_patient_product_and_address_survive_later_changes(self):
        snapshot = dict(patient_name='Original patient at dispatch', patient_id_number='ORIGINAL-ID',
                        company_name='Original practice', items=[
                            dict(product='Original medicine', dose='Original dose', quantity=3, batch_note='ORIGINAL-BATCH'),
                            dict(product='Original second medicine', dose='Second dose', quantity=1,
                                 batch='LEGACY-BATCH', expires_on='2027-05-01')],
                        delivery_address={'lines': ['Original address', 'Original city'], 'phone': 'ORIGINAL-PHONE',
                                          'recipient': 'Original recipient'},
                        waybill='ORIGINAL-WAYBILL')
        shipment = self.sent(dispatch_snapshot=snapshot,
                             booked_address={'lines': ['Older booked address'], 'phone': 'BOOKED-PHONE'})
        self.patient.first_name = 'Changed patient'
        self.patient.id_number = 'CHANGED-ID'
        self.patient.save()
        self.product.name = 'Changed medicine'
        self.product.save()
        self.address.street = 'Changed address'
        self.address.phone = 'CHANGED-PHONE'
        self.address.recipient = 'Changed recipient'
        self.address.save()
        response = self.history()
        self.assertEqual(HistoryLinks(response).headers,
                         ['Patient', 'Doctor', 'Delivered to', 'Parcel', 'Dispatched', 'Status', 'Note', 'Actions'])
        self.assertNotContains(response, '<th scope="col">Patient at dispatch</th>', html=True)
        self.assertContains(response, 'Original patient at dispatch')
        self.assertContains(response, 'Original medicine')
        self.assertContains(response, 'Original address, Original city')
        self.assertContains(response, 'ORIGINAL-PHONE')
        self.assertContains(response, 'Original recipient')
        self.assertNotContains(response, 'Changed patient')
        self.assertNotContains(response, 'Changed medicine')
        for changed in ('Changed address', 'CHANGED-PHONE', 'Changed recipient', 'Older booked address', 'BOOKED-PHONE'):
            self.assertNotContains(response, changed)
        _, workbook = self.workbook()
        rows = self.records(workbook)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['Shipment'] for row in rows], [shipment.pk, shipment.pk])
        self.assertEqual([row['Product'] for row in rows], ['Original medicine', 'Original second medicine'])
        self.assertEqual([row['Quantity'] for row in rows], [3, 1])
        self.assertEqual(rows[0]['Patient'], snapshot['patient_name'])
        self.assertEqual(rows[0]['ID / passport'], 'ORIGINAL-ID')
        self.assertEqual(rows[0]['Practice'], 'Original practice')
        self.assertEqual(rows[0]['Deliver to'], 'Original address, Original city')
        self.assertEqual(rows[0]['Contact number'], 'ORIGINAL-PHONE')
        self.assertEqual(rows[0]['Waybill'], 'ORIGINAL-WAYBILL')
        self.assertEqual(rows[0]['Batch and expiry'], 'ORIGINAL-BATCH')
        self.assertEqual(rows[1]['Batch and expiry'], 'LEGACY-BATCH, expiry 2027-05-01')

    def test_legacy_dispatch_without_snapshot_uses_its_saved_item_lines(self):
        shipment = self.sent(dispatch_snapshot={})
        shipment.items.update(dose='Legacy item dose', batch_note='Legacy item batch')
        response = self.history()
        self.assertContains(response, 'Legacy record; no saved dispatch snapshot')
        _, workbook = self.workbook()
        row = self.records(workbook)[0]
        self.assertEqual((row['Shipment'], row['Patient'], row['Product']),
                         (shipment.pk, str(self.patient), str(self.product)))
        self.assertEqual((row['Dose'], row['Quantity'], row['Batch and expiry']),
                         ('Legacy item dose', 2, 'Legacy item batch'))
        self.assertContains(response, ', '.join(self.address.lines()))
        self.assertContains(response, self.address.phone)
        self.assertEqual((row['Deliver to'], row['Contact number']),
                         (', '.join(self.address.lines()), self.address.phone))

    def test_legacy_delivery_prefers_the_courier_booking_over_a_changed_current_address(self):
        self.sent(dispatch_snapshot={}, booked_address={
            'lines': ['Booked street', 'Booked city'], 'phone': 'BOOKED-PHONE', 'recipient': 'Booked recipient'})
        self.address.street = 'Changed current address'
        self.address.phone = 'CHANGED-PHONE'
        self.address.save()
        response = self.history()
        self.assertContains(response, 'Booked street, Booked city')
        self.assertContains(response, 'BOOKED-PHONE')
        self.assertContains(response, 'Booked recipient')
        self.assertNotContains(response, 'Changed current address')
        self.assertNotContains(response, 'CHANGED-PHONE')
        _, workbook = self.workbook()
        row = self.records(workbook)[0]
        self.assertEqual((row['Deliver to'], row['Contact number']),
                         ('Booked street, Booked city', 'BOOKED-PHONE'))

    def test_excel_contains_typed_dates_numbers_and_literal_untrusted_text(self):
        timestamp = datetime(2026, 11, 2, 22, 45, tzinfo=datetime_timezone.utc)
        snapshot = dict(patient_name='=HYPERLINK("https://example.test")', patient_id_number='+00123',
                        company_name='#N/A', items=[dict(product='=1+1', dose='@SUM(A1)', quantity=3,
                                                        batch_note='-1+1')],
                        delivery_address={'lines': ['=ADDRESS'], 'phone': '=PHONE'}, waybill='=WAYBILL')
        shipment = self.sent(timestamp, dispatch_snapshot=snapshot, tracking_number='=TRACK',
                             hold_reason='=HOLD', note='=NOTE')
        with timezone.override('UTC'):
            _, workbook = self.workbook()
        sheet = workbook['Dispatch history']
        headers = [cell.value for cell in sheet[1]]
        cells = dict(zip(headers, sheet[2]))
        self.assertEqual((cells['Shipment'].value, cells['Shipment'].data_type), (shipment.pk, 'n'))
        self.assertEqual((cells['Quantity'].value, cells['Quantity'].data_type), (3, 'n'))
        self.assertEqual((cells['Planned date'].value, cells['Planned date'].data_type), (date(2026, 11, 3), 'd'))
        self.assertEqual((cells['Dispatched (SAST)'].value, cells['Dispatched (SAST)'].data_type),
                         (datetime(2026, 11, 3, 0, 45), 'd'))
        expected = {'Patient': snapshot['patient_name'], 'ID / passport': '+00123', 'Practice': '#N/A',
                    'Product': '=1+1', 'Dose': '@SUM(A1)', 'Batch and expiry': '-1+1', 'Tracking': '=TRACK',
                    'Hold reason': '=HOLD', 'Note': '=NOTE', 'Deliver to': '=ADDRESS',
                    'Contact number': '=PHONE', 'Waybill': '=WAYBILL'}
        for label, value in expected.items():
            with self.subTest(column=label):
                self.assertEqual((cells[label].value, cells[label].data_type), (value, 's'))

    def test_empty_history_still_downloads_a_valid_header_only_workbook(self):
        response = self.history()
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        export, workbook = self.workbook()
        self.assertIn('dispatch-history.xlsx', export['Content-Disposition'])
        self.assertEqual(workbook['Dispatch history'].max_row, 1)
        self.assertEqual(self.records(workbook), [])

    def test_history_and_excel_require_login_and_shipping_access(self):
        self.sent()
        for route in ('ops-history', 'ops-history-export'):
            url = reverse(f'portal:{route}')
            self.client.logout()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('login', response['Location'])
            for actor in (self.patient_user, self.doctor):
                with self.subTest(route=route, actor=actor.pk):
                    self.login(actor)
                    self.assertEqual(self.client.get(url).status_code, 403)
            self.login(self.super_admin)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()
            PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
            self.login(self.doctor)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()
            PracticeSettings.objects.filter(company=self.company).update(doctors_ship=False)
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.history().status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:ops-history-export')).status_code, 403)

    def test_current_company_scopes_both_the_list_and_workbook(self):
        alpha = self.sent(tracking_number='ALPHA-TRACK')
        beta = self.sent(company=self.beta, patient=self.beta_patient, product=self.beta_product,
                         tracking_number='PRIVATE-BETA-TRACK')
        malformed = self.sent(patient=self.beta_patient, tracking_number='WRONG-PATIENT-COMPANY')
        self.assertEqual(self.ids(self.history(company=self.beta.pk)), [alpha.pk])
        _, workbook = self.workbook(company=self.beta.pk)
        self.assertEqual([row['Shipment'] for row in self.records(workbook)], [alpha.pk])
        self.login(self.beta_admin, self.beta)
        self.assertEqual(self.ids(self.history()), [beta.pk])
        _, workbook = self.workbook()
        self.assertEqual([row['Shipment'] for row in self.records(workbook)], [beta.pk])
        self.assertNotEqual(malformed.pk, beta.pk)

    def test_get_and_head_preserve_dispatches_and_only_get_download_is_audited(self):
        self.sent()
        PracticeSettings.objects.filter(company=self.company).update(store_view_log=True)
        models = (Shipment, ShipmentItem, StockMovement, Payment)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        for route in ('ops-history', 'ops-history-export'):
            response = self.client.head(reverse(f'portal:{route}'))
            self.assertEqual(response.status_code, 200)
            content = b''.join(response.streaming_content) if response.streaming else response.content
            self.assertEqual(content, b'')
            response.close()
        self.assertFalse(AuditEvent.objects.filter(action='dispatch.history_exported').exists())
        response = self.history()
        exported, _ = self.workbook()
        for page in (response, exported):
            self.assertIn('private', page['Cache-Control'])
            self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(exported['X-Content-Type-Options'], 'nosniff')
        audit = AuditEvent.objects.get(action='dispatch.history_exported')
        self.assertEqual((audit.company, audit.actor), (self.company, self.admin))
        self.assertEqual(audit.metadata['rows'], 1)
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)

    def test_export_refuses_more_than_ten_thousand_matching_shipments(self):
        self.sent()
        PracticeSettings.objects.filter(company=self.company).update(store_view_log=True)
        with patch('django.db.models.query.QuerySet.count', return_value=10001):
            response = self.client.get(reverse('portal:ops-history-export'))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, '10,000', status_code=400)
        self.assertNotIn('Content-Disposition', response)
        self.assertFalse(AuditEvent.objects.filter(action='dispatch.history_exported').exists())
