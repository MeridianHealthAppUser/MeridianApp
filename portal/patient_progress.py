"""Accessible recorded weights, optionally alongside a labelled illustrative reference."""

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.core.validators import MinValueValidator, MaxValueValidator


class WeightHistoryFilterForm(forms.Form):
    date_from = forms.DateField(label='From date', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                                validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])
    date_to = forms.DateField(label='To date', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                              validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])

    def clean(self):
        data = super().clean()
        if data.get('date_from') and data.get('date_to') and data['date_from'] > data['date_to']:
            self.add_error('date_to', 'The end date must be on or after the start date.')
        return data


REFERENCE_WEEKS = 12


def weight_reference(baseline, course=None):
    """A finite illustration for a known course, independent of the displayed-row limit."""
    if baseline is None or course is None:
        return None
    started = course['started_on']
    starter_end = started + timedelta(weeks=4)
    starts_on = max(starter_end, baseline.recorded_on)
    ends_on = min(course['ends_on'], starter_end + timedelta(weeks=REFERENCE_WEEKS))
    # Do not infer an earlier weight from a late first entry, or extend a short
    # prescription. Invalid imported low weights cannot create a nonphysical band.
    if starts_on >= ends_on or baseline.weight_kg <= Decimal((ends_on - starts_on).days) / 7:
        return None
    return {
        'treatment_start': started,
        'baseline_date': baseline.recorded_on,
        'baseline_weight': baseline.weight_kg,
        'starts_on': starts_on,
        'ends_on': ends_on,
        'weeks_limit': REFERENCE_WEEKS,
    }


def weight_chart(rows, *, reference=None):
    rows = list(rows)
    if not rows:
        return None
    values = [row.weight_kg for row in rows]
    start, end = rows[0].recorded_on, rows[-1].recorded_on
    reference_rows = []
    if reference:
        reference = dict(reference)
        # A first check-in can show a short illustration within its prescribed
        # course. The actual series still includes all displayed recorded dates.
        end = max(end, min(reference['ends_on'], reference['starts_on'] + timedelta(weeks=4)))
        band_start = max(start, reference['starts_on'])
        band_end = min(end, reference['ends_on'])
        if band_start < band_end:
            for day in (band_start, band_end):
                weeks = Decimal((day - reference['starts_on']).days) / 7
                reference_rows.append({
                    'date': day,
                    'lower': reference['baseline_weight'] - weeks,
                    'upper': reference['baseline_weight'] - weeks * Decimal('0.5'),
                })
    axis_values = values + [row[key] for row in reference_rows for key in ('lower', 'upper')]
    low, high = min(axis_values), max(axis_values)
    margin = max(Decimal('0.5'), (high - low) / Decimal('10'))
    low, high = max(Decimal('0'), low - margin), high + margin
    days = max(1, (end - start).days)

    def x(day):
        return (Decimal('377') if start == end else
                Decimal('62') + Decimal((day - start).days) / days * Decimal('630'))

    def y(weight):
        return Decimal('212') - (weight - low) / (high - low) * Decimal('180')

    points = []
    for row in rows:
        points.append({'x': f'{x(row.recorded_on):.2f}', 'y': f'{y(row.weight_kg):.2f}',
                       'date': row.recorded_on, 'weight': row.weight_kg})
    if reference:
        upper = [f'{x(row["date"]):.2f},{y(row["upper"]):.2f}' for row in reference_rows]
        lower = [f'{x(row["date"]):.2f},{y(row["lower"]):.2f}' for row in reference_rows]
        reference.update(polygon=' '.join(upper + list(reversed(lower))),
                         upper_line=' '.join(upper), lower_line=' '.join(lower), rows=reference_rows)
        if start <= reference['starts_on'] <= end:
            reference['starter_x'] = f'{x(reference["starts_on"]):.2f}'
    return dict(points=points, polyline=' '.join(f'{point["x"]},{point["y"]}' for point in points),
                ticks=[{'y': 32, 'label': f'{high:.1f}'}, {'y': 122, 'label': f'{(high + low) / 2:.1f}'}, {'y': 212, 'label': f'{low:.1f}'}],
                start=start, end=end, recorded_end=rows[-1].recorded_on, reference=reference,
                count=len(points), minimum=min(values), maximum=max(values))


def weight_history_context(request, weights):
    form = WeightHistoryFilterForm(request.GET, auto_id='weight_filter_%s')
    query = {}
    if form.is_valid():
        if form.cleaned_data['date_from']:
            weights = weights.filter(recorded_on__gte=form.cleaned_data['date_from'])
        if form.cleaned_data['date_to']:
            weights = weights.filter(recorded_on__lte=form.cleaned_data['date_to'])
        query = {key: value for key, value in form.cleaned_data.items() if value is not None}
    else:
        weights = weights.none()
    page = Paginator(weights, 20).get_page(request.GET.get('page'))
    # Keep page cost bounded while every entry remains reachable in the table.
    chart_rows = list(reversed(list(weights[:300])))
    return dict(page_obj=page, is_paginated=page.has_other_pages(), pagination_query=urlencode(query),
                weight_filter_form=form, weight_chart=weight_chart(chart_rows),
                chart_is_limited=page.paginator.count > 300)
