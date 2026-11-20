import logging
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core.validators import MaxValueValidator
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.activity_reports import (PERIOD_DAY, csv_filename, csv_text, current_period, latest_period, period_label,
                                   rate_phrases, report_doctors, report_totals, send_activity_report, settings_for)
from care.billing import money
from care.models import ActivityReport, DoctorActivityStatement, PracticeSettings
from care.reporting import (ACTIVITIES, approve_activity_statement, create_activity_statement, explicit_rates,
                            period_bounds, practice_rates, refresh_activity_statement, reporting_today)
from care.services import record_audit, view_log_enabled
from practices.models import Company, CompanyMembership
from .operations_views import _csv_response
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context

logger = logging.getLogger(__name__)
MONTHLY_REPORTS = 'monthly-reports'


class StatementCreateForm(forms.Form):
    doctor = forms.ModelChoiceField(queryset=get_user_model().objects.none())
    start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    initial = forms.DecimalField(label='Per new consult (R)', min_value=0, max_value=100000, decimal_places=2)
    follow_up = forms.DecimalField(label='Per follow-up consult (R)', min_value=0, max_value=100000, decimal_places=2)
    messages = forms.DecimalField(label='Per message answered (R)', min_value=0, max_value=100000, decimal_places=2)
    review = forms.DecimalField(label='Per blood result reviewed (R)', min_value=0, max_value=100000, decimal_places=2)
    confirm = forms.BooleanField(label='I have checked these explicit rates. This creates a local draft statement only.')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['doctor'].queryset = get_user_model().objects.filter(is_active=True, company_memberships__company=company, company_memberships__is_active=True, company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES).distinct().order_by('first_name', 'last_name')
        # The practice's rates (Monthly reports) to start from; each can be changed for this statement.
        try:
            for key, rate in practice_rates(company).items():
                self.fields[key].initial = rate
        except ValidationError:
            pass

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end'):
            period_bounds(data['start'], data['end'])
            if data['end'] >= reporting_today():
                raise ValidationError('Choose completed dates ending before today.')
        return data


class ActivityReportSettingsForm(forms.ModelForm):
    """Monthly reports' settings: who the report goes to and the rate for each activity."""

    class Meta:
        model = PracticeSettings
        fields = ('activity_report_email', 'activity_rate_initial', 'activity_rate_follow_up', 'activity_rate_messages', 'activity_rate_review')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['activity_report_email'].label = 'Send the report to'
        self.fields['activity_report_email'].help_text = 'Emailed on the 25th of each month. Leave blank to stop it.'
        for name in self.Meta.fields[1:]:
            # As statements take them (care.reporting.explicit_rates): whole Rand and cents, up to R100 000.
            self.fields[name].validators.append(MaxValueValidator(Decimal('100000')))
            self.fields[name].widget.attrs.update(min='0', max='100000', step='0.01')


class StatementActionForm(forms.Form):
    action = forms.ChoiceField(choices=(('approve', 'Approve snapshot'), ('refresh', 'Refresh draft activity')))
    confirm = forms.BooleanField(label='I have reviewed this statement. No payment will be made.')


@method_decorator(never_cache, name='dispatch')
class ReportingView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    admin_only = True
    nav_section = 'metrics'
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section, **kwargs)


class StatementView(ReportingView):
    nav_section = 'statements'

    def statements(self):
        if not (self.membership.is_clinician or self.membership.role == 'super_admin'):
            raise PermissionDenied('Activity statements are available to the clinician and practice Super Admin.')
        queryset = DoctorActivityStatement.objects.for_company(self.company).select_related('doctor', 'prepared_by', 'approved_by').order_by('-period_end', 'doctor_id', '-pk')
        return queryset if self.membership.role == 'super_admin' else queryset.filter(doctor=self.request.user)

    def require_editor(self):
        if self.membership.role != 'super_admin':
            raise PermissionDenied('Only the practice Super Admin can prepare or approve statements.')


class StatementListView(StatementView):
    """Activity statements, and for the Super Admin the monthly reports above them (care.activity_reports)."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return self.display(request)

    def display(self, request, settings_form=None, status=200):
        status_filter = request.GET.get('status', 'all')
        queryset = self.statements()
        if status_filter == 'draft':
            queryset = queryset.filter(approved_at__isnull=True)
        elif status_filter == 'approved':
            queryset = queryset.filter(approved_at__isnull=False)
        elif status_filter != 'all':
            queryset = queryset.none()
        page = Paginator(queryset, 20).get_page(request.GET.get('page'))
        is_super_admin = self.membership.role == 'super_admin'
        return render(request, 'portal/reporting_statements.html', self.context(page_obj=page, status_filter=status_filter,
            pagination_query='status=' + status_filter if status_filter in ('draft', 'approved') else '', can_edit=is_super_admin,
            monthly=self.monthly(request, settings_form) if is_super_admin else None), status=status)

    def monthly(self, request, settings_form=None):
        """Monthly reports: the recipient and rates, Send now for the latest period, and the log of reports sent."""
        row = settings_for(self.company)
        start, end = latest_period()
        upcoming = current_period()
        reports = ActivityReport.objects.for_company(self.company).select_related('created_by')
        page = Paginator(reports, 12).get_page(request.GET.get('reports_page'))
        for report in page:
            report.total_text = money(report.total)
        try:
            rates = rate_phrases(practice_rates(self.company))
        except ValidationError:
            rates = []
        latest_report = reports.filter(period_start=start, period_end=end, sent_at__isnull=False).first()
        posted = request.method == 'POST' and settings_form is not None
        return {
            'settings': row, 'rates': rates,
            'settings_form': settings_form if settings_form is not None else ActivityReportSettingsForm(instance=row),
            'settings_context': request.POST.get('workflow_context', '') if posted else make_workflow_context(request, self.company, 'activity-report-settings'),
            'send_context': make_workflow_context(request, self.company, 'activity-report-send'),
            # Not yet sent although its 25th has come: the daily job hasn't run or couldn't send it.
            'latest': {'start': start, 'end': end, 'label': period_label(start, end), 'report': latest_report,
                       'due': latest_report is None and reporting_today().day >= PERIOD_DAY},
            'upcoming': {'label': period_label(*upcoming), 'sends_on': upcoming[1] + timedelta(days=1)},
            'reports': page,
            'reports_query': 'status=' + request.GET['status'] if request.GET.get('status') in ('draft', 'approved') else '',
        }


class ActivityReportSettingsView(StatementListView):
    """Save Monthly reports' recipient and rates (a dialog on Activity statements)."""

    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request):
        self.require_editor()
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        row = settings_for(self.company)
        fields = ActivityReportSettingsForm.Meta.fields
        before = {name: getattr(row, name) for name in fields}
        form = ActivityReportSettingsForm(request.POST, instance=row)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'activity-report-settings')
            if valid:
                row = form.save()
                changed = [name for name in fields if getattr(row, name) != before[name]]
                record_audit(company=self.company, actor=request.user, action='activity_report.settings_changed', target=row,
                             request=request, metadata={'fields': changed})
                messages.success(request, 'Monthly report settings saved.' if changed else 'Nothing changed in the monthly report settings.')
                return redirect(f"{reverse('portal:activity-statements')}#{MONTHLY_REPORTS}")
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, form, 400)


class ActivityReportSendView(StatementView):
    """Send now: email the latest period's report, or send it again when confirmed in the dialog."""

    http_method_names = ('post',)

    def post(self, request):
        self.require_editor()
        start, end = latest_period()
        back = redirect(f"{reverse('portal:activity-statements')}#{MONTHLY_REPORTS}")
        try:
            validate_workflow_context(request, self.company, 'activity-report-send')
            if request.POST.get('period_end') != end.isoformat():
                raise ValidationError('A newer period has ended since this page opened. Reload it and send that one.')
            report, sent = send_activity_report(self.company, start, end, actor=request.user, resend=request.POST.get('resend') == '1', request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return back
        except PermissionDenied:
            raise
        except Exception:  # The mail server refused it; nothing was saved.
            logger.exception('Could not email the activity report for %s.', self.company.pk)
            messages.error(request, 'The report couldn’t be emailed. Nothing was saved, so try again in a few minutes.')
            return back
        label = period_label(start, end)
        if sent:
            messages.success(request, f'The activity report for {label} was sent to {report.sent_to}.')
        else:
            sent_on = timezone.localtime(report.sent_at)
            messages.warning(request, f'The activity report for {label} was already sent on {sent_on.day} {sent_on:%B}, so it wasn’t sent again.')
        return redirect('portal:activity-report-detail', pk=report.pk)


class ActivityReportDetailView(StatementView):
    """One monthly report as it was sent: each doctor's activity, amounts and total payout. Super Admin only, as it
    lists every doctor's pay. Its CSV is the one the email attached."""

    http_method_names = ('get', 'head', 'options')
    export = False

    def get(self, request, pk):
        self.require_editor()
        report = get_object_or_404(ActivityReport.objects.for_company(self.company).select_related('created_by'), pk=pk)
        try:
            doctors = report_doctors(report)
        except ValidationError:
            return HttpResponseBadRequest('This report’s saved figures don’t add up, so it can’t be shown or downloaded. Ask your practice Super Admin to review it.')
        if request.method != 'HEAD' and view_log_enabled(self.company):
            record_audit(company=self.company, actor=request.user, action='activity_report.exported' if self.export else 'activity_report.viewed',
                         target=report, request=request)
        if self.export:
            response = HttpResponse(csv_text(report), content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="{csv_filename(report)}"'
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        return render(request, 'portal/reporting_statement_report.html', self.context(
            report=report, doctors=doctors, totals=report_totals(doctors), rates=rate_phrases(report.rates),
            period=period_label(report.period_start, report.period_end), total=money(report.total)))


class StatementCreateView(StatementView):
    def display(self, request, form, status=200):
        return render(request, 'portal/reporting_statement_form.html', self.context(form=form,
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'statement-create')), status=status)

    def get(self, request):
        self.require_editor()
        return self.display(request, StatementCreateForm(company=self.company))

    @transaction.atomic
    def post(self, request):
        self.require_editor()
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        form = StatementCreateForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'statement-create')
            if valid:
                data = form.cleaned_data
                statement = create_activity_statement(company=self.company, actor=request.user, doctor=data['doctor'], start=data['start'], end=data['end'], rates={key: data[key] for key, _ in ACTIVITIES}, request=request)
                messages.success(request, 'Draft activity statement prepared. No payment was made.')
                return redirect('portal:activity-statement-detail', pk=statement.pk)
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, form, 400)


class StatementDetailView(StatementView):
    export = False

    def rows(self, record):
        rates = explicit_rates(record.rates)
        if not isinstance(record.counts, dict) or any(type(record.counts.get(key)) is not int or record.counts[key] < 0 for key, _ in ACTIVITIES):
            raise ValidationError('Invalid saved activity counts.')
        rows = [(label, record.counts[key], Decimal(rates[key]), Decimal(rates[key]) * record.counts[key]) for key, label in ACTIVITIES]
        if sum((row[3] for row in rows), Decimal('0.00')) != record.amount:
            raise ValidationError('The saved total does not match its activity and rates.')
        return rows

    def invalid_snapshot(self):
        return HttpResponseBadRequest('This statement contains inconsistent saved activity or rates and cannot be displayed or exported. Ask your practice Super Admin to review the source record.')

    def display(self, request, record, form=None, status=200):
        try:
            rows = self.rows(record)
        except ValidationError:
            return self.invalid_snapshot()
        return render(request, 'portal/reporting_statement_detail.html', self.context(statement=record, rows=rows,
            form=form if form is not None else StatementActionForm(), can_edit=self.membership.role == 'super_admin' and not record.approved_at,
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'statement', record)), status=status)

    def get(self, request, pk):
        record = get_object_or_404(self.statements(), pk=pk)
        try:
            rows = self.rows(record)
        except ValidationError:
            return self.invalid_snapshot()
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='activity_statement.exported' if self.export else 'activity_statement.viewed', target=record, request=request)
        if self.export:
            return _csv_response(f'activity-statement-{record.pk}.csv', ('Activity', 'Count', 'Rate (ZAR)', 'Reference amount (ZAR)'),
                [(f'{self.company.name} | {record.doctor.full_name} | {record.period_start} to {record.period_end}', '', '', ''),
                 ('Approved snapshot — no payment made' if record.approved_at else 'Draft — no payment made', '', '', ''), *rows, ('Total', '', '', record.amount)])
        return self.display(request, record)

    @transaction.atomic
    def post(self, request, pk):
        if self.export:
            raise PermissionDenied('Exports are read-only.')
        self.require_editor()
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        record = get_object_or_404(self.statements(), pk=pk)
        form = StatementActionForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'statement', record)
            if valid:
                if form.cleaned_data['action'] == 'approve':
                    approve_activity_statement(statement=record, actor=request.user, expected_updated=token['updated'], confirm=form.cleaned_data['confirm'], request=request)
                    messages.success(request, 'Statement approved as a fixed local snapshot. No payment was made.')
                else:
                    refresh_activity_statement(statement=record, actor=request.user, expected_updated=token['updated'], request=request)
                    messages.success(request, 'Draft activity refreshed using its existing explicit rates.')
                return redirect('portal:activity-statement-detail', pk=pk)
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, record, form, 400)
