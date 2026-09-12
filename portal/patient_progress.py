"""An accessible chart of recorded weights, with no clinical interpretation."""

from datetime import date
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


def weight_chart(rows):
    rows = list(rows)
    if not rows:
        return None
    values = [row.weight_kg for row in rows]
    low, high = min(values), max(values)
    margin = max(Decimal('0.5'), (high - low) / Decimal('10'))
    low, high = max(Decimal('0'), low - margin), high + margin
    start, end = rows[0].recorded_on, rows[-1].recorded_on
    days = max(1, (end - start).days)
    points = []
    for row in rows:
        x = Decimal('62') + Decimal((row.recorded_on - start).days) / Decimal(days) * Decimal('630')
        y = Decimal('212') - (row.weight_kg - low) / (high - low) * Decimal('180')
        points.append({'x': f'{x:.2f}', 'y': f'{y:.2f}', 'date': row.recorded_on, 'weight': row.weight_kg})
    return dict(points=points, polyline=' '.join(f'{point["x"]},{point["y"]}' for point in points),
                ticks=[{'y': 32, 'label': f'{high:.1f}'}, {'y': 122, 'label': f'{(high + low) / 2:.1f}'}, {'y': 212, 'label': f'{low:.1f}'}],
                start=start, end=end, count=len(points), minimum=min(values), maximum=max(values))


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
