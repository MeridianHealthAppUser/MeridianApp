"""Compact note icons share one accessible editor without leaking or duplicating parcel notes."""
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from care.models import Shipment, ShipmentItem
from care.operations import set_shipment_note
from .test_operations import OperationsPortalFixture
from .workflow_context import make_workflow_context


class NoteMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.icons = []
        self.text = []
        self.skipped = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if 'data-shipment-note' in attrs:
            self.icons.append(attrs)
        if tag in ('script', 'style', 'template'):
            self.skipped += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'template'):
            self.skipped = max(0, self.skipped - 1)

    def handle_data(self, data):
        if not self.skipped:
            self.text.append(data)


class ShippingNoteTests(OperationsPortalFixture):
    def shipping(self, **params):
        defaults = dict(week=self.today, shipping_step='all')
        defaults.update(params)
        return self.page('ops-shipping', **defaults)

    def markup(self, response):
        parser = NoteMarkup()
        parser.feed(response.content.decode())
        return parser

    def note_token(self, parcel):
        request = RequestFactory().get('/')
        request.user = self.admin
        return make_workflow_context(request, self.company, 'shipment-note', parcel)

    def test_icons_replace_inline_note_text_and_use_one_dialog_in_each_view(self):
        text = 'Call reception before delivery <script>alert("note")</script>'
        parcel = self.shipment(quantity=1, note=text)
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=self.open_product, quantity=1, dose='')
        self.shipment(quantity=1)
        for mode in ('customers', 'medication'):
            with self.subTest(view=mode):
                page = self.shipping(view=mode)
                markup = self.markup(page)
                self.assertEqual(markup.ids.count('shipment-note-dialog'), 1)
                self.assertEqual([key for key, count in Counter(markup.ids).items() if count > 1], [])
                self.assertTrue(markup.icons)
                noted = [icon for icon in markup.icons if icon.get('data-note') == text]
                self.assertTrue(noted)
                self.assertTrue(all('has-note' in icon.get('class', '') for icon in noted))
                self.assertNotIn(text, ''.join(markup.text))
                self.assertNotContains(page, '<script>alert("note")</script>')

    def test_fallback_opens_the_visible_parcels_note_and_preserves_current_filters(self):
        parcel = self.shipment(quantity=1, note='Call reception')
        page = self.shipping(note=parcel.pk, view='medication', product=self.product.pk, payment='paid')
        self.assertTrue(page.context['note_open'])
        self.assertEqual(page.context['note_record'].pk, parcel.pk)
        self.assertFalse(page.context['note_read_only'])
        self.assertContains(page, 'Call reception')
        query = parse_qs(page.context['list_query'])
        self.assertEqual(query['view'], ['medication'])
        self.assertEqual(query['product'], [str(self.product.pk)])
        self.assertNotIn('note', query)

    def test_fallback_does_not_load_another_practice_or_filtered_out_parcel(self):
        self.shipment(quantity=1)
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product,
                                subscription=None, note='Private beta note')
        ready = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(), note='Hidden confirmed note')
        for target in (foreign, ready):
            with self.subTest(target=target.pk):
                page = self.shipping(note=target.pk, shipping_step='pending')
                self.assertFalse(page.context['note_open'])
                self.assertIsNone(page.context['note_record'])
                self.assertNotContains(page, target.note)

    def test_delivered_note_is_viewable_but_cannot_be_edited(self):
        parcel = self.shipment(quantity=1, status=Shipment.Status.DELIVERED, note='Historic parcel note')
        page = self.shipping(note=parcel.pk)
        self.assertTrue(page.context['note_open'])
        self.assertTrue(page.context['note_read_only'])
        self.assertFalse(getattr(page.context['note_record'], 'note_context', None))
        response = self.client.post(reverse('portal:ops-shipment-note', args=[parcel.pk]),
                                   {'workflow_context': self.note_token(parcel), 'note': 'Changed'})
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.note, 'Historic parcel note')

    def test_signed_save_and_clear_return_to_the_filtered_stage(self):
        parcel = self.shipment(quantity=1)
        query = dict(week=self.today.isoformat(), shipping_step='ready', view='medication',
                     product=[self.product.pk, self.open_product.pk], doctor=self.doctor.pk, payment='paid')
        address = reverse('portal:ops-shipment-note', args=[parcel.pk])
        for text in ('Delivery instructions', ''):
            parcel.refresh_from_db()
            response = self.client.post(address, {'workflow_context': self.note_token(parcel), 'note': text,
                'list_query': urlencode(query, doseq=True)})
            self.assertEqual(response.status_code, 302)
            parcel.refresh_from_db()
            self.assertEqual(parcel.note, text)
            kept = parse_qs(urlsplit(response['Location']).query)
            self.assertEqual(kept['shipping_step'], ['ready'])
            self.assertEqual(kept['view'], ['medication'])
            self.assertEqual(kept['product'], [str(self.product.pk), str(self.open_product.pk)])

    def test_unsigned_note_save_is_rejected_without_changing_the_note(self):
        parcel = self.shipment(quantity=1, note='Keep this note')
        response = self.client.post(reverse('portal:ops-shipment-note', args=[parcel.pk]), {'note': 'Replace it'})
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.note, 'Keep this note')

    def test_service_rechecks_closed_status_after_locking_the_current_parcel(self):
        parcel = self.shipment(quantity=1, note='Historic note')
        for status in (Shipment.Status.DELIVERED, Shipment.Status.CANCELLED):
            Shipment.objects.filter(pk=parcel.pk).update(status=status)
            # Deliberately retain the stale Draft object to simulate delivery after the UI was rendered.
            with self.assertRaises(ValidationError):
                set_shipment_note(shipment=parcel, actor=self.admin, note='Changed after closure')
            self.assertEqual(Shipment.objects.get(pk=parcel.pk).note, 'Historic note')
