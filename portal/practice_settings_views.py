"""Practice-wide settings that only a Super Admin can change."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import PROVINCES, PracticeSettings
from care.purchasing import person_name, po_doctors
from care.services import record_audit
from care.suppliers import normalise_vat_number
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
        ('Contact details updated', True), ('Prescribing doctor changed', True),
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
        ('Delivery addresses added or changed', True), ('Waybills booked with The Courier Guy', False),
    )),
    ('Billing', (
        ('Debit orders set up, approved, declined or needing a new amount', True),
        ('Debits collected, failed and retried', True), ('Payments recorded by the practice', False),
        ('Next debit dates moved', False),
    )),
    ('Messaging', (
        ('Messages sent to patients and colleagues', False), ('Conversations closed or reopened', False),
        ('Clinicians and colleagues added to conversations', False),
    )),
    ('Operations', (
        ('Catalogue changes', False),
        ('Review rules changed', False),
    )),
    ('Administration', (
        ('Staff accounts, roles and clinician types changed', False), ('Practice details and settings changed', False),
        ('Policy versions published', False), ('Privacy preferences, requests and responses', False),
        ('Passwords changed or reset, other devices signed out, and profiles or staff details updated', False), ('Activity statements created, refreshed or approved', False),
        ('Follow-ups recorded and enquiries received', False),
        ('Monthly activity reports sent and their settings changed', False),
        ('Insights notes and historical data changed, and Insights exported', False),
    )),
)


class PracticeSettingsForm(forms.ModelForm):
    class Meta:
        model = PracticeSettings
        fields = ('store_view_log',)


class ShippingSettingsForm(forms.ModelForm):
    class Meta:
        model = PracticeSettings
        fields = ('shipping_weekday', 'doctors_ship')


class BillingSettingsForm(forms.ModelForm):
    """What a first consultation costs, whether parcels wait for payment, and when a failed debit is tried again."""

    class Meta:
        model = PracticeSettings
        fields = ('initial_consult_fee', 'require_payment', 'payment_retry_days', 'new_debit_order')
        labels = {'initial_consult_fee': 'Initial consultation fee (R)'}

    def clean_payment_retry_days(self):
        value = self.cleaned_data['payment_retry_days']
        parts = [part.strip() for part in value.split(',') if part.strip()]
        if not parts or any(not part.isdigit() or not 0 < int(part) <= 60 for part in parts):
            raise forms.ValidationError('Give days after the debit date between 1 and 60, separated by commas, such as 3,7.')
        return ','.join(str(day) for day in sorted({int(part) for part in parts}))


class CourierSettingsForm(forms.ModelForm):
    """Where The Courier Guy collects parcels, and the parcel the practice usually sends."""

    class Meta:
        model = PracticeSettings
        fields = ('collection_business', 'collection_street', 'collection_suburb', 'collection_city', 'collection_province',
                  'collection_postal_code', 'collection_contact', 'collection_phone', 'collection_email',
                  'courier_service_level', 'parcel_length_cm', 'parcel_width_cm', 'parcel_height_cm', 'parcel_weight_kg')


class PurchaseOrderSettingsForm(forms.ModelForm):
    """Bill to on every purchase order, and the doctor whose name and HPCSA number it carries."""

    class Meta:
        model = PracticeSettings
        fields = ('po_bill_to_name', 'po_billing_street', 'po_billing_suburb', 'po_billing_city', 'po_billing_province',
                  'po_billing_postal_code', 'po_vat_number', 'po_pharmacy_number', 'po_accounts_email', 'po_doctor',
                  'po_delivery_instructions')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['po_doctor']
        field.queryset = po_doctors(company)
        field.empty_label = 'Choose a doctor'
        self.fields['po_billing_province'].choices = [('', 'Choose a province'), *PROVINCES]
        field.label_from_instance = lambda user: f'{person_name(user)} · ' + (
            f'HPCSA {user.hpcsa_number}' if user.hpcsa_number else 'HPCSA not on profile')

    def clean_po_vat_number(self):
        return normalise_vat_number(self.cleaned_data['po_vat_number'])


def _json_value(value):
    """A setting as the audit log keeps it: amounts (Decimal) as text, a person by their id, everything else as it is."""
    if isinstance(value, models.Model):
        return value.pk
    return value if value is None or isinstance(value, (bool, int, str)) else str(value)


SECTION_FORMS = {'shipping': ShippingSettingsForm, 'billing': BillingSettingsForm, 'courier': CourierSettingsForm,
                 'purchasing': PurchaseOrderSettingsForm}


def _section_form(form_class, company, *args, **kwargs):
    """Only the purchase order section needs the practice, to offer its own doctors."""
    if form_class is PurchaseOrderSettingsForm:
        kwargs['company'] = company
    return form_class(*args, **kwargs)


@method_decorator(never_cache, name='dispatch')
class PracticeSettingsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Each section of the page saves its own settings: Shipping, or the optional event log."""

    http_method_names = ('get', 'post', 'head', 'options')

    def settings_row(self):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can change practice settings.')
        return PracticeSettings.objects.get_or_create(company=self.company)[0]

    def display(self, request, row, status=200, **forms):
        from care.courier import courier_enabled
        from care.payment_providers import current_provider

        context = dict(
            company=self.company, active_membership=self.membership, nav_section='practice_settings',
            page_title='Practice settings', event_log_sections=EVENT_LOG_SECTIONS, courier_enabled=courier_enabled(),
            payment_provider=current_provider(), form=forms.get('form') or PracticeSettingsForm(instance=row))
        for section, form_class in SECTION_FORMS.items():
            context[f'{section}_form'] = forms.get(f'{section}_form') or _section_form(form_class, self.company, instance=row)
        return render(request, 'portal/practice_settings.html', context, status=status)

    def get(self, request):
        return self.display(request, self.settings_row())

    def post(self, request):
        row = self.settings_row()
        section = request.POST.get('section')
        form = _section_form(SECTION_FORMS.get(section, PracticeSettingsForm), self.company, request.POST, instance=row)
        if not form.is_valid():
            row.refresh_from_db()
            return self.display(request, row, status=400, **{f'{section}_form' if section in SECTION_FORMS else 'form': form})
        with transaction.atomic():
            changed = list(form.changed_data)
            saved = form.save()
            if changed:
                record_audit(company=self.company, actor=request.user, action='practice.settings_updated', target=saved,
                             request=request, metadata={field: _json_value(getattr(saved, field)) for field in changed})
        messages.success(request, 'Practice settings saved.')
        return redirect('portal:practice-settings')
