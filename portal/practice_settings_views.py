"""Practice-wide settings that only a Super Admin can change."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import PracticeSettings
from care.services import record_audit
from practices.models import CompanyMembership
from .views import StaffCompanyRequiredMixin


# What the event log always stores, for display only. True marks events that also appear in a patient's History tab.
# Keep in step with the actions recorded in the services and the History sources in record_views.
EVENT_LOG_SECTIONS = (
    ('Clinical record', (
        ('Signed consultations', True), ('Shared clinical notes', True), ('Consultation drafts saved', False),
        ('Blood tests requested, reports uploaded and reviews recorded', True), ('Medical profile updates', True),
        ('Weight entries', True), ('Consents accepted or declined', True), ('Tasks created, updated and completed', False),
        ('Record tags changed', False), ('Compounding records drafted, reviewed, submitted or cancelled', False),
    )),
    ('Patient record', (
        ('Patient record and login created', True), ('Enquiry converted to patient', True),
        ('Contact details updated', True), ('Assigned clinician changed', True),
    )),
    ('Appointments', (
        ('Appointments booked by staff or patients', True), ('Appointments cancelled, completed or missed', True),
        ('New times suggested, accepted, declined or withdrawn', True), ('Video calls joined and left', False),
        ('Working hours and time off changed', False),
    )),
    ('Treatment and care plans', (
        ('Treatment authorised, renewed, paused or revoked', True), ('Care plans enrolled, paused, resumed or cancelled', True),
        ('Review reminders created and shipments held by review checks', False),
    )),
    ('Supplies and deliveries', (
        ('Supply requests submitted, accepted or cancelled', True), ('Shipments created, locked, dispatched or delivered', True),
        ('Shipments prepared, held or cancelled', False), ('Patient basket changes', False),
    )),
    ('Messaging', (
        ('Messages sent to patients and colleagues', False), ('Conversations closed or reopened', False),
        ('Clinicians and colleagues added to conversations', False),
    )),
    ('Operations', (
        ('Stock received, adjusted, quarantined, released or written off', False), ('Catalogue changes', False),
        ('Review rules changed', False),
    )),
    ('Administration', (
        ('Staff accounts, roles and clinician types changed', False), ('Practice details and settings changed', False),
        ('Policy versions published', False), ('Privacy preferences, requests and responses', False),
        ('Passwords and profiles changed', False), ('Activity statements created, refreshed or approved', False),
        ('Follow-ups recorded and enquiries received', False),
    )),
)


class PracticeSettingsForm(forms.ModelForm):
    class Meta:
        model = PracticeSettings
        fields = ('store_view_log',)


@method_decorator(never_cache, name='dispatch')
class PracticeSettingsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def settings_row(self):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can change practice settings.')
        return PracticeSettings.objects.get_or_create(company=self.company)[0]

    def display(self, request, form, status=200):
        return render(request, 'portal/practice_settings.html', dict(
            company=self.company, active_membership=self.membership, nav_section='practice_settings',
            page_title='Practice settings', form=form, event_log_sections=EVENT_LOG_SECTIONS), status=status)

    def get(self, request):
        return self.display(request, PracticeSettingsForm(instance=self.settings_row()))

    def post(self, request):
        row = self.settings_row()
        before = row.store_view_log
        form = PracticeSettingsForm(request.POST, instance=row)
        if not form.is_valid():
            return self.display(request, form, status=400)
        with transaction.atomic():
            saved = form.save()
            if saved.store_view_log != before:
                record_audit(company=self.company, actor=request.user, action='practice.settings_updated', target=saved,
                             request=request, metadata={'store_view_log': saved.store_view_log})
        messages.success(request, 'Practice settings saved.')
        return redirect('portal:practice-settings')
