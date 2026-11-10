"""Requested labels and the simplified doctor workspace."""
from datetime import date
from django.urls import reverse
from care.forms import AppointmentForm
from portal.operations_forms import ShipmentActionForm
from portal.operations_views import chosen_week
from django.test import RequestFactory
from .test_operations import OperationsPortalFixture


class FeedbackEditsTests(OperationsPortalFixture):
    def test_doctor_navigation_and_patient_messages_are_simplified(self):
        self.login(self.doctor)
        directory = self.page('patient-list')
        self.assertNotContains(directory, 'Scroll the table sideways')
        self.assertNotContains(directory, 'console-nav__text">Weekly shipping')
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages'})
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Team conversations about this patient')
        self.assertNotContains(page, 'Message a colleague about this patient')

    def test_tuesday_week_boundaries_and_action_checkbox(self):
        factory = RequestFactory()
        for day, expected in (('2026-11-10', date(2026, 11, 10)),
                              ('2026-11-16', date(2026, 11, 10)),
                              ('2026-11-17', date(2026, 11, 17)),
                              ('2027-01-01', date(2026, 12, 29))):
            self.assertEqual(chosen_week(factory.get('/', {'week': day})), expected)
        self.assertNotIn('confirm', ShipmentActionForm(shipment=self.locked(self.shipment())).fields)

    def test_appointment_lengths_offer_only_fifteen_and_thirty_minutes(self):
        form = AppointmentForm(company=self.company, initial={'appointment_type': 'initial'})
        self.assertEqual([value for value, _ in form.fields['duration_minutes'].choices], [15, 30])
        self.assertEqual(form['duration_minutes'].value(), 30)
        follow_up = AppointmentForm(company=self.company, initial={'appointment_type': 'follow_up'})
        self.assertEqual(follow_up['duration_minutes'].value(), 15)
