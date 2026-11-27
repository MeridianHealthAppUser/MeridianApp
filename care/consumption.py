"""Actual dispatched units, grouped into local calendar weeks.

The report deliberately does not use the editable purchasing starting AWC, stock
receipts, planned parcels or reservations. Every weekly total can be reconciled
to the same eligible shipment lines returned by ``weekly_items``.
"""

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Min, Q, Sum, F, Value, DurationField, DateTimeField, ExpressionWrapper
from django.db.models.functions import TruncMonth, TruncWeek
from django.utils import timezone

from .models import MedicationProduct, Shipment, ShipmentItem


WINDOW_CHOICES = (4, 8, 12)
AVERAGE_WEEKS = 8


def three_months_ago(day):
    """The same calendar day three months earlier, clamped to that month's end."""
    month_index = day.year * 12 + day.month - 1 - 3
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _cutoff(as_of=None):
    """Return an exclusive aware cutoff; a supplied date includes that whole day."""
    if as_of is None:
        return timezone.now()
    if isinstance(as_of, datetime):
        return timezone.make_aware(as_of, timezone.get_current_timezone()) if timezone.is_naive(as_of) else as_of
    if isinstance(as_of, date):
        return _midnight(as_of + timedelta(days=1))
    raise ValidationError('Choose a valid report date.')


def _midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def week_start(day):
    if isinstance(day, datetime):
        day = timezone.localtime(day).date() if timezone.is_aware(day) else day.date()
    if not isinstance(day, date):
        raise ValidationError('Choose a valid week.')
    return day - timedelta(days=(day.weekday() - 1) % 7)


def consumption_items(company, *, as_of=None):
    """The only shipment lines eligible for any consumption total or drill-down."""
    return ShipmentItem.objects.filter(
        company=company,
        product__company=company,
        shipment__company=company,
        shipment__patient__company=company,
        shipment__status__in=(Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED),
        shipment__dispatched_at__isnull=False,
        shipment__dispatched_at__lt=_cutoff(as_of),
    )


def period_items(company, start, end, *, as_of=None):
    """Eligible lines in an inclusive local date range, with deterministic ordering."""
    return consumption_items(company, as_of=as_of).filter(
        shipment__dispatched_at__gte=_midnight(start),
        shipment__dispatched_at__lt=_midnight(end + timedelta(days=1)),
    ).order_by('-shipment__dispatched_at', '-shipment_id', '-pk')


def weekly_items(company, product, start, *, as_of=None):
    start = week_start(start)
    return period_items(company, start, start + timedelta(days=6), as_of=as_of).filter(
        product_id=getattr(product, 'pk', product),
    )


def consumption_graph_report(company, *, date_from=None, date_to=None, grouping='weeks',
                             product_ids=None, as_of=None):
    """Zero-filled product series for an exact, inclusive local date range.

    Weekly buckets use the same shipping weeks as the consumption matrix. The
    first and last buckets only count dates inside the requested range.
    """
    cutoff = _cutoff(as_of)
    today = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else timezone.localtime(cutoff).date()
    date_from = date_from if date_from is not None else three_months_ago(today)
    date_to = date_to if date_to is not None else today
    if grouping not in ('weeks', 'months'):
        raise ValidationError('Choose weeks or months.')
    if any(type(day) is not date for day in (date_from, date_to)):
        raise ValidationError('Choose valid from and to dates.')
    if date_from < date(1900, 1, 1) or date_to < date_from or date_to > today:
        raise ValidationError('Choose a date range from January 1900 through today, with the from date first.')

    def bucket_start(day):
        return week_start(day) if grouping == 'weeks' else day.replace(day=1)

    def next_bucket(day):
        if grouping == 'weeks':
            return day + timedelta(days=7)
        return date(day.year + day.month // 12, day.month % 12 + 1, 1)

    periods = []
    start = bucket_start(date_from)
    while start <= date_to:
        following = next_bucket(start)
        first, last = max(start, date_from), min(following - timedelta(days=1), date_to)
        label = (f"{first.strftime('%d %b %Y').lstrip('0')} – {last.strftime('%d %b %Y').lstrip('0')}"
                 if grouping == 'weeks' else start.strftime('%b %Y'))
        periods.append({'start': start.isoformat(), 'end': last.isoformat(), 'label': label,
                        'date_from': first.isoformat(), 'date_to': last.isoformat()})
        start = following

    products = MedicationProduct.objects.filter(company=company)
    if product_ids is not None:
        products = products.filter(pk__in=product_ids)
    products = list(products.order_by('name', 'strength', 'pk'))
    grid = {}
    if products:
        dispatch_date = F('shipment__dispatched_at')
        if grouping == 'weeks':
            dispatch_date = ExpressionWrapper(
                dispatch_date - Value(timedelta(days=1), output_field=DurationField()),
                output_field=DateTimeField())
        truncate = TruncWeek if grouping == 'weeks' else TruncMonth
        values = period_items(company, date_from, date_to, as_of=cutoff).filter(
            product_id__in=[product.pk for product in products],
        ).order_by().annotate(
            graph_period=truncate(dispatch_date, tzinfo=timezone.get_current_timezone()),
        ).values('product_id', 'graph_period').annotate(units=Sum('quantity'))
        for value in values:
            day = value['graph_period'].date() + (timedelta(days=1) if grouping == 'weeks' else timedelta())
            grid[value['product_id'], day.isoformat()] = int(value['units'])

    series = []
    for product in products:
        values = [grid.get((product.pk, period['start']), 0) for period in periods]
        series.append({'id': product.pk, 'name': str(product), 'values': values, 'total': sum(values)})
    return {'grouping': grouping, 'date_from': date_from, 'date_to': date_to,
            'periods': periods, 'series': series, 'total_units': sum(row['total'] for row in series)}


def consumption_report(company, *, end_week=None, weeks=8, query='', categories=(), as_of=None):
    """Build a weekly matrix and current historical averages in three SQL reads.

    Eight-week averages include all eight completed weeks, even zero weeks.
    The current-year average covers 1 January through the last completed Sunday;
    its denominator is elapsed calendar days / 7, including the initial partial
    week. All-time averages start at each product's first recorded dispatch week
    and include every completed week since then. The current week never enters
    these averages, but can be selected in the matrix as a partial week.
    """
    if type(weeks) is not int or weeks not in WINDOW_CHOICES:
        raise ValidationError('Choose 4, 8 or 12 weeks.')
    cutoff = _cutoff(as_of)
    # A date cutoff denotes the end of that date; datetimes denote an instant.
    today = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else timezone.localtime(cutoff).date()
    current_week = week_start(today)
    selected_end = week_start(end_week) if end_week is not None else current_week - timedelta(days=7)
    if selected_end > current_week:
        raise ValidationError('Choose this week or an earlier week.')
    try:
        selected_start = selected_end - timedelta(weeks=weeks - 1)
        average_start = current_week - timedelta(weeks=AVERAGE_WEEKS)
        selected_last = selected_end + timedelta(days=6)
    except OverflowError:
        raise ValidationError('Choose a valid report week.') from None
    year_start = date(today.year, 1, 1)
    average_end = current_week - timedelta(days=1)
    year_days = max(0, (current_week - year_start).days)
    year_weeks = Decimal(year_days) / Decimal(7)

    products = MedicationProduct.objects.filter(company=company)
    if categories:
        products = products.filter(category__in=categories)
    products = list(products.order_by('name', 'strength', 'pk'))
    words = str(query).casefold().split()
    if words:
        products = [product for product in products if all(word in ' '.join((
            product.name, product.strength, str(product.get_category_display()),
        )).casefold() for word in words)]
    ids = [product.pk for product in products]

    grid = {}
    aggregates = {}
    if ids:
        items = consumption_items(company, as_of=cutoff).filter(product_id__in=ids)
        weekly = items.filter(
            shipment__dispatched_at__gte=_midnight(selected_start),
            shipment__dispatched_at__lt=_midnight(selected_last + timedelta(days=1)),
        ).order_by().annotate(
            # Shift Tuesday back to Monday for the database's calendar-week truncation.
            dispatch_week=TruncWeek(
                ExpressionWrapper(F('shipment__dispatched_at') - Value(timedelta(days=1), output_field=DurationField()),
                                  output_field=DateTimeField()),
                tzinfo=timezone.get_current_timezone()),
        ).values('product_id', 'dispatch_week').annotate(units=Sum('quantity'))
        grid = {(value['product_id'], value['dispatch_week'].date() + timedelta(days=1)): value['units'] for value in weekly}

        completed = items.filter(shipment__dispatched_at__lt=_midnight(current_week))
        aggregates = {value['product_id']: value for value in completed.order_by().values('product_id').annotate(
            units_8=Sum('quantity', filter=Q(shipment__dispatched_at__gte=_midnight(average_start))),
            units_year=Sum('quantity', filter=Q(shipment__dispatched_at__gte=_midnight(year_start))),
            units_all=Sum('quantity'),
            first_dispatch=Min('shipment__dispatched_at'),
        )}

    columns = []
    for offset in range(weeks):
        start = selected_start + timedelta(weeks=offset)
        columns.append({'start': start, 'end': start + timedelta(days=6),
                        'is_partial': start == current_week, 'iso_week': start.isocalendar().week})
    rows = []
    totals = [0] * weeks
    for product in products:
        values = aggregates.get(product.pk, {})
        first = values.get('first_dispatch')
        first_week = week_start(first) if first else None
        all_weeks = Decimal((current_week - first_week).days // 7) if first_week else Decimal(0)
        units_8, units_year, units_all = (int(values.get(key) or 0) for key in ('units_8', 'units_year', 'units_all'))
        cells = []
        for index, column in enumerate(columns):
            quantity = grid.get((product.pk, column['start']), 0)
            cells.append(dict(column, quantity=quantity))
            totals[index] += quantity
        rows.append({
            'product': product, 'cells': cells, 'total': sum(cell['quantity'] for cell in cells),
            'avg_8': Decimal(units_8) / Decimal(AVERAGE_WEEKS),
            'avg_year': Decimal(units_year) / year_weeks if year_weeks else None,
            'avg_all': Decimal(units_all) / all_weeks if all_weeks else None,
            'units_8': units_8, 'units_year': units_year, 'units_all': units_all,
            'weeks_8': Decimal(AVERAGE_WEEKS), 'weeks_year': year_weeks, 'weeks_all': all_weeks,
            'first_week': first_week,
        })

    return {
        'rows': rows, 'weeks': columns, 'period_start': selected_start, 'period_end': selected_last,
        'end_week': selected_end, 'current_week_start': current_week, 'as_of': today, 'as_of_at': cutoff,
        'average_8_start': average_start, 'year_start': year_start, 'average_end': average_end,
        'totals_by_week': [dict(column, quantity=total) for column, total in zip(columns, totals)],
        'total_units': sum(totals), 'product_count': len(products),
        'products_with_consumption': sum(row['total'] > 0 for row in rows),
    }
