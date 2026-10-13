"""Practice administrators see appointment status changes in the patient's History."""

from datetime import timedelta

from django.test import override_settings
from django.urls import reverse

from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AdminHistoryTests(AppointmentLifecycleFixture):
    def history_labels(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'history'})
        return [row.workspace_label for row in response.context['rows']]

    def test_cancelled_completed_and_missed_appointments_are_shown(self):
        self.change(self.appointment(), status='cancelled')
        for status, offset in (('completed', -2), ('no_show', -3)):
            past = self.appointment(starts_at=self.starts_at + timedelta(days=offset - 3))
            self.change(past, status=status, actor=self.doctor)
        labels = self.history_labels()
        for label in ('Appointment cancelled', 'Appointment completion recorded', 'Appointment non-attendance recorded'):
            self.assertIn(label, labels)
