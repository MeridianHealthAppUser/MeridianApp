"""Insights and Cohorts, for the practice's Super Admin (care.insights; Dr Czech, Nov 2026). Insights has a chart for
each of his measures over all time or a chosen period; Cohorts has the retention views from his notes. Both carry the
practice's dated notes (InsightNote) as markers. Historical data holds the monthly figures from before the app."""

from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache

from care.insights import MEDICINES, SOURCES, add_months, cohorts, first_of, month_range, overview
from care.models import InsightFigure, InsightNote
from care.reporting import reporting_today
from care.services import record_audit
from care.table_export import table_workbook
from practices.models import CompanyMembership
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context

MEDICINE_LABELS = dict(MEDICINES)
FIGURE_COLUMNS = ((InsightFigure.Metric.NEW_BOOKINGS, 'New bookings'), (InsightFigure.Metric.GOOGLE, 'Google'),
                  (InsightFigure.Metric.AI_OVERVIEW, 'AI overview'), (InsightFigure.Metric.WORD_OF_MOUTH, 'Word of mouth'),
                  (InsightFigure.Metric.FACEBOOK, 'Facebook'), (InsightFigure.Metric.INSTAGRAM, 'Instagram'))


# The quick ranges on Insights (James, 20 Nov 2026). Each but Custom is worked out from today when the page opens, so a
# saved link to Last 90 days is always the last 90 days. Custom is the dates picked on the calendar.
RANGES = (('last-30', 'Last 30 days'), ('last-90', 'Last 90 days'), ('last-365', 'Last 365 days'),
          ('this-month', 'This month'), ('year-to-date', 'Year to date'), ('all', 'All time'), ('custom', 'Custom'))


def range_start(key, today):
    """The first day of a quick range; None for All time. Each runs to today."""
    return {'last-30': today - timedelta(days=29), 'last-90': today - timedelta(days=89),
            'last-365': today - timedelta(days=364), 'this-month': first_of(today),
            'year-to-date': date(today.year, 1, 1)}.get(key)


class InsightsPeriodForm(forms.Form):
    """A quick range, or Custom dates; All time when there's neither. Dates without a range are Custom."""

    range = forms.ChoiceField(choices=RANGES, required=False)
    start = forms.DateField(required=False)
    end = forms.DateField(required=False)

    def clean(self):
        data = super().clean()
        today = reporting_today()
        chosen = data.get('range') or ('custom' if data.get('start') or data.get('end') else 'all')
        if chosen != 'custom':
            return {'range': chosen, 'start': range_start(chosen, today), 'end': None}
        start, end = data.get('start'), data.get('end')
        if start and end and end < start:
            raise ValidationError('The end date must be on or after the start date.')
        if start and start > today:
            raise ValidationError('Choose a start date that isn’t in the future.')
        return {'range': 'custom' if start or end else 'all', 'start': start, 'end': end}


class InsightNoteForm(forms.ModelForm):
    class Meta:
        model = InsightNote
        fields = ('on', 'label')
        widgets = {'on': forms.DateInput(attrs={'type': 'date'}), 'label': forms.TextInput(attrs={'placeholder': 'Decreased price to R1000'})}


@method_decorator(never_cache, name='dispatch')
class InsightsBase(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    admin_only = True
    nav_section = 'metrics'
    http_method_names = ('get', 'head', 'options')

    def require_super_admin(self):
        # Insights is for Super Admins; practice administrators and doctors don't see it.
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Insights are available to the practice Super Admin.')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section, **kwargs)

    def notes_context(self, request):
        return dict(note_form=InsightNoteForm(initial={'on': reporting_today()}, auto_id='note_%s'),
                    note_context=make_workflow_context(request, self.company, 'insight-note'),
                    notes=InsightNote.objects.for_company(self.company).order_by('-on', '-pk'),
                    next_url=request.get_full_path())


def series(key, label, values, tone):
    return {'key': key, 'label': label, 'values': list(values), 'tone': tone}


def overview_charts(data):
    """What static/js/insights_charts.js draws, chart by chart. Semaglutide and Tirzepatide keep the same colours
    everywhere; an All line is the neutral ink."""
    x = data['keys']
    doctors = data['doctors']
    medicines = [series(key, label, data['signups'][key], position) for position, (key, label) in enumerate(MEDICINES, start=1)]
    weight = data['weight_by_month']
    return {
        'bookings': {'kind': 'line', 'x': x, 'format': 'int', 'notes': True, 'series': [
            series('all', 'All bookings', data['bookings']['all'], 'total'),
            series('new', 'New', data['bookings']['new'], 1), series('follow_up', 'Follow-up', data['bookings']['follow_up'], 2)]},
        'sources': {'kind': 'stacked', 'x': x, 'format': 'int', 'notes': True, 'series': [
            series(key, label, data['sources'][key], position) for position, (key, label) in enumerate(SOURCES, start=1)]},
        'signups': {'kind': 'line', 'x': x, 'format': 'int', 'notes': True, 'series': [
            series('all', 'All new subscriptions', data['signups']['all'], 'total'), *medicines]},
        'mrr': {'kind': 'line', 'x': x, 'format': 'rand', 'notes': True, 'series': [series('mrr', 'Monthly recurring revenue', data['mrr'], 1)]},
        'dropouts': {'kind': 'bars', 'x': x, 'format': 'int', 'notes': True, 'series': [series('dropouts', 'Dropouts', data['dropouts'], 1)]},
        'churn': {'kind': 'line', 'x': x, 'format': 'pct', 'notes': True, 'series': [series('churn', 'Churn', data['churn'], 1)]},
        'doctors': {'kind': 'line', 'x': x, 'format': 'int', 'notes': True, 'series': [
            series(f'doctor-{doctor["id"]}', doctor['name'], data['doctor_consults'][doctor['id']], position)
            for position, doctor in enumerate(doctors[:8], start=1)]},
        'weight': {'kind': 'line', 'x': [f'M{month}' for month in range(13)], 'axis': 'months', 'format': 'kg', 'series': [
            {**series(key, MEDICINE_LABELS[key], [value for value, _ in points], position),
             'counts': [count for _, count in points]} for position, (key, points) in enumerate(weight, start=1)]},
    }


class InsightsView(InsightsBase):
    """Insights: Dr Czech's measures month by month (care.insights.overview)."""

    export = False

    def get(self, request):
        self.require_super_admin()
        form = InsightsPeriodForm(request.GET)
        period = form.cleaned_data if form.is_valid() else {'range': 'all', 'start': None, 'end': None}
        data = overview(self.company, start=period['start'], end=period['end'])
        today = reporting_today()
        # The dates the range shows, from the first day to the last (today, unless an earlier end was picked).
        shown = (period['start'] or data['first'], min(period['end'], today) if period['end'] else today)
        if self.export:
            return self.xlsx(request, data, shown)
        first_month = add_months(first_of(reporting_today()), -23)
        known = InsightFigure.objects.for_company(self.company).order_by('month').values_list('month', flat=True).first()
        entered = {(figure.month, figure.metric): figure.value for figure in InsightFigure.objects.for_company(self.company)}
        figure_rows = [{'month': month, 'cells': [(metric, entered.get((month, metric), '')) for metric, _ in FIGURE_COLUMNS]}
                       for month in reversed(month_range(min(known or first_month, first_month), reporting_today()))]
        return render(request, 'portal/insights.html', self.context(
            form=form, period=period, ranges=RANGES, shown=shown, today=today,
            data=data, kpis=data['kpis'], charts=overview_charts(data),
            chart_notes=data['notes'], sources=SOURCES, medicines=MEDICINES,
            figure_columns=FIGURE_COLUMNS, figure_rows=figure_rows,
            figures_context=make_workflow_context(request, self.company, 'insight-figures'),
            export_query=request.GET.urlencode(), **self.notes_context(request)))

    def xlsx(self, request, data, shown):
        """The months on the page as an Excel sheet, one row a month."""
        headings = ('Month', 'All bookings', 'New bookings', 'Follow-up bookings', *[label for _, label in SOURCES],
                    'New subscriptions', *[f'New subscriptions: {label}' for _, label in MEDICINES],
                    'Monthly recurring revenue (R)', 'Dropouts', 'Subscriptions at the start of the month', 'Churn (%)')
        rows = [(key, data['bookings']['all'][position], data['bookings']['new'][position], data['bookings']['follow_up'][position],
                 *[data['sources'][source][position] for source, _ in SOURCES],
                 data['signups']['all'][position], *[data['signups'][medicine][position] for medicine, _ in MEDICINES],
                 data['mrr'][position], data['dropouts'][position], data['running'][position], data['churn'][position])
                for position, key in enumerate(data['keys'])]
        try:
            output, count = table_workbook('Insights', headings, rows)
        except ValidationError as error:
            return HttpResponse(' '.join(error.messages), status=400, content_type='text/plain')
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action='metrics.exported', request=request,
                             metadata={'from': shown[0].isoformat(), 'to': shown[1].isoformat(), 'rows': count})
            response = FileResponse(output, as_attachment=True, filename=f'insights-{shown[0]:%Y-%m-%d}-to-{shown[1]:%Y-%m-%d}.xlsx',
                                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        except Exception:
            output.close()
            raise


class CohortsView(InsightsBase):
    """Cohorts: retention by signup month, for all patients or one medicine (care.insights.cohorts)."""

    nav_section = 'cohorts'

    def get(self, request):
        self.require_super_admin()
        medicine = request.GET.get('medicine', '')
        medicine = medicine if medicine in MEDICINE_LABELS else ''
        grouping = 'quarter' if request.GET.get('group') == 'quarter' else 'month'
        data = cohorts(self.company, medicine=medicine or None)
        # The heatmap shows the last 12 months unless a year is chosen; by quarter it shows them all.
        years = sorted({row['month'].year for row in data['rows']}, reverse=True)
        year = request.GET.get('year', '')
        year = int(year) if year.isdigit() and int(year) in years else None
        rows = data['quarters'] if grouping == 'quarter' else data['rows']
        if grouping == 'month':
            rows = [row for row in rows if row['month'].year == year] if year else rows[-12:]
        # Columns as far as the shown cohorts reach (at least a year): later ones would all be empty.
        reached = max((k for row in rows for k, value in enumerate(row['cells']) if value is not None), default=0)
        shown = max(12, min(data['horizon'], reached))
        rows = [{**row, 'cells': [(value, heat_step(value)) for value in row['cells'][:shown + 1]]} for row in rows]
        horizon = data['horizon']
        x = [f'M{month}' for month in range(horizon + 1)]
        # The latest year darkest, older ones lighter (and dashed, then dotted).
        shades = ['seq-4', 'seq-3', 'seq-2', 'seq-1'] if len(data['years']) > 3 else ['seq-4', 'seq-2', 'seq-1']
        tones = {item['year']: shades[min(len(shades) - 1, len(data['years']) - 1 - position)] for position, item in enumerate(data['years'])}
        charts = {
            'years': {'kind': 'line', 'x': x, 'axis': 'months', 'format': 'pct', 'ends': True, 'max': 100, 'series': [
                {**series(str(item['year']), str(item['year']), item['values'], tones[item['year']]), 'size': item['size']}
                for item in data['years']]},
        }
        for milestone in data['milestones']:
            charts[f"m{milestone['months']}"] = {'kind': 'line', 'x': data.get('keys', []), 'format': 'pct', 'notes': True, 'max': 100,
                                                 'series': [series(f"m{milestone['months']}", f"Month {milestone['months']}", milestone['values'], 1)]}
        return render(request, 'portal/insights_cohorts.html', self.context(
            data=data, rows=rows, horizon_range=range(shown + 1), grouping=grouping, years=years, year=year,
            medicine=medicine, medicines=MEDICINES, medicine_label=MEDICINE_LABELS.get(medicine, 'All patients'),
            charts=charts, chart_notes=data['notes'], milestones=[(m['months'], f"m{m['months']}", f"Month {m['months']}") for m in data['milestones']],
            kpi_rows=[(months, *data['kpis'][months]) for months in (1, 3, 6, 12)] if data['kpis'] else [],
            **self.notes_context(request)))


def heat_step(value):
    """The heatmap's shade for a share: 1 (low) to 7 (high), as a step of one blue."""
    if value is None:
        return 0
    return max(1, min(7, int(value // 14.3) + 1))


def back_to(request):
    target = request.POST.get('next', '')
    if target.startswith(reverse('portal:metrics')) and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect('portal:metrics')


@method_decorator(never_cache, name='dispatch')
class InsightNoteView(InsightsBase):
    """Add a note (POST), or remove one (POST to its delete address)."""

    http_method_names = ('post',)

    def post(self, request, pk=None):
        self.require_super_admin()
        try:
            validate_workflow_context(request, self.company, 'insight-note')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return back_to(request)
        if pk is not None:
            note = get_object_or_404(InsightNote.objects.for_company(self.company), pk=pk)
            record_audit(company=self.company, actor=request.user, action='insight_note.deleted', target=note, request=request,
                         metadata={'on': note.on.isoformat(), 'label': note.label})
            note.delete()
            messages.success(request, 'Note removed.')
            return back_to(request)
        form = InsightNoteForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Add a date and a note of up to 160 characters.')
            return back_to(request)
        note = form.save(commit=False)
        note.company, note.created_by = self.company, request.user
        note.full_clean()
        note.save()
        record_audit(company=self.company, actor=request.user, action='insight_note.created', target=note, request=request)
        messages.success(request, f'Note added for {note.on:%-d %b %Y}.')
        return back_to(request)


@method_decorator(never_cache, name='dispatch')
class InsightFiguresView(InsightsBase):
    """Save Historical data: each month's numbers from before the app. An emptied cell removes that figure, so the
    app's own count is used again."""

    http_method_names = ('post',)

    def post(self, request):
        self.require_super_admin()
        try:
            validate_workflow_context(request, self.company, 'insight-figures')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return back_to(request)
        metrics = {metric for metric, _ in FIGURE_COLUMNS}
        changes, problems = [], []
        for name, raw in request.POST.items():
            if not name.startswith('figure-'):
                continue
            try:
                _, month_text, metric = name.split('-', 2)
                year, month = (int(part) for part in month_text.split('_'))
                month = date(year, month, 1)
            except ValueError:
                continue
            if metric not in metrics or month > reporting_today():
                continue
            value = raw.strip()
            if value and not (value.isdigit() and int(value) <= 100000):
                problems.append(f'{month:%b %Y}')
                continue
            changes.append((month, metric, int(value) if value else None))
        if problems:
            messages.error(request, f'Use whole numbers only: check {", ".join(problems[:4])}.')
            return back_to(request)
        saved = removed = 0
        for month, metric, value in changes:
            existing = InsightFigure.objects.for_company(self.company).filter(month=month, metric=metric).first()
            if value is None:
                if existing:
                    existing.delete()
                    removed += 1
            elif existing is None or existing.value != value:
                InsightFigure.objects.update_or_create(company=self.company, month=month, metric=metric,
                                                       defaults={'value': value, 'updated_by': request.user})
                saved += 1
        if saved or removed:
            record_audit(company=self.company, actor=request.user, action='insight_figures.updated', request=request,
                         metadata={'saved': saved, 'removed': removed})
        messages.success(request, 'Historical data saved.' if saved or removed else 'No figures changed.')
        return back_to(request)
