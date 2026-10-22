"""The administrative History (typed labels only, nothing clinical) shows appointment status changes. It's the
History for staff without clinical access; practice administrators, the only such staff, no longer open records
(22 Oct 2026), so it's built directly here."""

from datetime import timedelta

from django.test import RequestFactory, override_settings

from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from practices.models import CompanyMembership
from .patient_workspace import workspace_tab_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AdminHistoryTests(AppointmentLifecycleFixture):
    def history_labels(self):
        request = RequestFactory().get('/', {'tab': 'history'})
        request.user = self.admin
        membership = CompanyMembership.objects.get(company=self.company, user=self.admin)
        context = workspace_tab_context(request, self.company, membership, self.patient, 'history')
        return [row.workspace_label for row in context['rows']]

    def test_cancelled_completed_and_missed_appointments_are_shown(self):
        self.change(self.appointment(), status='cancelled')
        for status, offset in (('completed', -2), ('no_show', -3)):
            past = self.appointment(starts_at=self.starts_at + timedelta(days=offset - 3))
            self.change(past, status=status, actor=self.doctor)
        labels = self.history_labels()
        for label in ('Appointment cancelled', 'Appointment completion recorded', 'Appointment non-attendance recorded'):
            self.assertIn(label, labels)
