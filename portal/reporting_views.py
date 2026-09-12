from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import DoctorActivityStatement
from care.reporting import (ACTIVITIES, approve_activity_statement, create_activity_statement,
                            explicit_rates, operational_metrics, period_bounds, refresh_activity_statement, reporting_today)
from care.services import record_audit
from practices.models import Company
from .operations_views import _csv_response
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class ReportPeriodForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    scope = forms.ChoiceField(choices=(('current', 'Current practice'), ('all', 'All my permitted practices')))

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end'):
            period_bounds(data['start'], data['end'])
        return data


class StatementCreateForm(forms.Form):
    doctor = forms.ModelChoiceField(queryset=get_user_model().objects.none())
    start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    initial = forms.DecimalField(label='Rate per completed initial consult (R)', min_value=0, max_value=100000, decimal_places=2)
    review = forms.DecimalField(label='Rate per completed review (R)', min_value=0, max_value=100000, decimal_places=2)
    follow_up = forms.DecimalField(label='Rate per completed follow-up / ad-hoc consult (R)', min_value=0, max_value=100000, decimal_places=2)
    messages = forms.DecimalField(label='Rate per message sent (R)', min_value=0, max_value=100000, decimal_places=2)
    confirm = forms.BooleanField(label='I have checked these explicit rates. This creates a local draft statement only.')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['doctor'].queryset = get_user_model().objects.filter(is_active=True, company_memberships__company=company, company_memberships__is_active=True, company_memberships__role='doctor').distinct().order_by('first_name', 'last_name')

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end'):
            period_bounds(data['start'], data['end'])
            if data['end'] >= reporting_today():
                raise ValidationError('Choose completed dates ending before today.')
        return data


class StatementActionForm(forms.Form):
    action = forms.ChoiceField(choices=(('approve', 'Approve snapshot'), ('refresh', 'Refresh draft activity')))
    confirm = forms.BooleanField(label='I have reviewed this statement. No payment will be made.')


@method_decorator(never_cache, name='dispatch')
class ReportingView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    nav_section = 'metrics'
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section, **kwargs)


class MetricsView(ReportingView):
    http_method_names = ('get', 'head', 'options')
    export = False

    def get(self, request):
        data = request.GET.copy()
        data.setdefault('start', reporting_today().replace(day=1))
        data.setdefault('end', reporting_today())
        data.setdefault('scope', 'current')
        form = ReportPeriodForm(data)
        results = {}
        if form.is_valid():
            results = operational_metrics(actor=request.user, company=self.company, **form.cleaned_data)
            if self.export:
                for practice in results['practices']:
                    if request.method != 'HEAD':
                        record_audit(company=practice, actor=request.user, action='metrics.exported', request=request)
                rows = [('Period start', results['start']), ('Period end', results['end']), ('Scope', ', '.join(practice.name for practice in results['practices'])), *results['metrics'],
                        ('Weight records with two dated measurements', results['weight_stats']['count']),
                        ('Mean recorded weight change (%)', results['weight_stats']['mean'])]
                rows.extend((f"Patient record cohort {item['month']:%Y-%m}", item['records']) for item in results['cohorts'])
                return _csv_response('meridian-operational-metrics.csv', ('Metric', 'Value'), rows)
            from .reporting_presentation import metrics_dashboard
            results['dashboard'] = metrics_dashboard(results)
        return render(request, 'portal/reporting_metrics.html', self.context(form=form, **results), status=400 if self.export else 200)


class StatementView(ReportingView):
    nav_section = 'statements'

    def statements(self):
        if self.membership.role not in ('doctor', 'super_admin'):
            raise PermissionDenied('Activity statements are available to the doctor and practice Super Admin.')
        queryset = DoctorActivityStatement.objects.for_company(self.company).select_related('doctor', 'prepared_by', 'approved_by').order_by('-period_end', 'doctor_id', '-pk')
        return queryset.filter(doctor=self.request.user) if self.membership.role == 'doctor' else queryset

    def require_editor(self):
        if self.membership.role != 'super_admin':
            raise PermissionDenied('Only the practice Super Admin can prepare or approve statements.')


class StatementListView(StatementView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        status = request.GET.get('status', 'all')
        queryset = self.statements()
        if status == 'draft':
            queryset = queryset.filter(approved_at__isnull=True)
        elif status == 'approved':
            queryset = queryset.filter(approved_at__isnull=False)
        elif status != 'all':
            queryset = queryset.none()
        page = Paginator(queryset, 20).get_page(request.GET.get('page'))
        return render(request, 'portal/reporting_statements.html', self.context(page_obj=page, status_filter=status,
            pagination_query='status=' + status if status in ('draft', 'approved') else '', can_edit=self.membership.role == 'super_admin'))


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
