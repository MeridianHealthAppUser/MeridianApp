"""The monthly doctor activity report (Dr Czech, Nov 2026).

On the 25th, each doctor's activity from the 25th of the month before to the 24th, at the practice's rates, is emailed
to the address set in Insights > Activity statements > Monthly reports and kept there as a log (ActivityReport). For
each clinician: new consults, follow-up consults (follow-ups, six-month reviews and ad-hoc), messages answered in
patient conversations and blood results they marked reviewed, each at its rate, and their total payout; then the total
for everyone. What counts is care.reporting.doctor_activity: a consult counts when it takes place, unless cancelled or
missed, so a booking cancelled later never counts. How many of them were marked completed is shown beside it, for
information only. Demo patients never count. Nothing is paid here.

`manage.py send_activity_reports` runs every day and sends each period once, from the 25th. A Super Admin can also
send the latest period with Send now, and send it again only when they confirm that.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership
from practices.tenancy import require_enabled_company
from .billing import money
from .emails import build_email, site_link
from .models import ActivityReport, PracticeSettings
from .reporting import ACTIVITIES, CONSULT_TYPES, activity_amount, doctor_activity, practice_rates, reporting_today
from .services import record_audit

# Each period starts on the 25th and ends on the 24th of the next month.
PERIOD_DAY = 25


def period_ending(year, month):
    """The period that ends on the 24th of this month: from the 25th of the month before (of December, for January)."""
    start = date(year - 1, 12, PERIOD_DAY) if month == 1 else date(year, month - 1, PERIOD_DAY)
    return start, date(year, month, PERIOD_DAY - 1)


def latest_period(today=None):
    """The latest period that has ended, in the practice's time: from the 25th, the 25th of last month to the 24th of
    this one; before the 25th, the one before that."""
    today = today or reporting_today()
    if today.day < PERIOD_DAY:
        today = today.replace(day=1) - timedelta(days=1)
    return period_ending(today.year, today.month)


def current_period_start(today=None):
    """The first day of the period today is in: the 25th of this month, or of last month before the 25th."""
    return latest_period(today)[1] + timedelta(days=1)


def current_period(today=None):
    """The period today is in, which the daily job sends the day after it ends."""
    start = current_period_start(today)
    return period_ending(start.year + start.month // 12, start.month % 12 + 1)


def period_label(start, end):
    """25 October – 24 November 2026, with both years when the period crosses into a new one."""
    first = f'{start.day} {start:%B}' if start.year == end.year else f'{start.day} {start:%B %Y}'
    return f'{first} – {end.day} {end:%B %Y}'


def settings_for(company):
    return PracticeSettings.objects.for_company(company).first() or PracticeSettings(company=company)


def report_rows(company, start, end, rates):
    """A row for every active clinician here, those who did nothing too, and for anyone who was a clinician here with
    activity in the period, so a doctor who has since left is still paid for it. In name order."""
    memberships = CompanyMembership.objects.filter(company=company, clinician_type__in=CompanyMembership.CLINICIAN_TYPES).select_related('user').order_by(
        'user__first_name', 'user__last_name', 'user_id')
    rows = []
    for membership in memberships:
        doctor = membership.user
        counts, _ = doctor_activity(company=company, doctor=doctor, start=start, end=end)
        if not (membership.is_active and doctor.is_active) and not any(counts[key] for key, _ in ACTIVITIES):
            continue
        rows.append({'doctor_id': doctor.pk, 'doctor': doctor.titled_name, 'counts': counts,
                     'amounts': {key: str(Decimal(rates[key]) * counts[key]) for key, _ in ACTIVITIES},
                     'total': str(activity_amount(rates, counts))})
    return rows


def build_report(company, start, end):
    """(rows, rates, total) for the period, at the practice's rates now."""
    rates = practice_rates(company)
    rows = report_rows(company, start, end, rates)
    return rows, rates, sum((Decimal(row['total']) for row in rows), Decimal('0.00'))


def report_doctors(report):
    """The saved report for display: each doctor's lines (label, count, how many were marked completed for consults,
    rate, amount) and total, from its own saved rates. A ValidationError when the saved figures don't add up."""
    try:
        rates = {key: Decimal(str(report.rates[key])) for key, _ in ACTIVITIES}
        doctors = []
        for row in report.rows:
            lines = []
            for key, label in ACTIVITIES:
                count = row['counts'][key]
                completed = row['counts'].get(f'{key}_completed', 0) if key in CONSULT_TYPES else None
                if type(count) is not int or count < 0 or (completed is not None and (type(completed) is not int or not 0 <= completed <= count)):
                    raise ValueError
                amount = rates[key] * count
                if amount != Decimal(str(row['amounts'][key])):
                    raise ValueError
                lines.append({'key': key, 'label': label, 'count': count, 'completed': completed, 'rate': rates[key],
                              'amount': amount, 'rate_text': money(rates[key]), 'amount_text': money(amount)})
            total = sum((line['amount'] for line in lines), Decimal('0.00'))
            if total != Decimal(str(row['total'])):
                raise ValueError
            doctors.append({'doctor': str(row['doctor']), 'lines': lines, 'total': total, 'total_text': money(total)})
        if sum((doctor['total'] for doctor in doctors), Decimal('0.00')) != report.total:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise ValidationError('This report’s saved figures don’t add up.') from None
    return doctors


def report_totals(doctors):
    """Each activity added up over every doctor, as a doctor's lines are."""
    totals = []
    for index, (key, label) in enumerate(ACTIVITIES):
        lines = [doctor['lines'][index] for doctor in doctors]
        amount = sum((line['amount'] for line in lines), Decimal('0.00'))
        totals.append({'key': key, 'label': label, 'count': sum(line['count'] for line in lines), 'amount': amount, 'amount_text': money(amount),
                       'completed': sum(line['completed'] for line in lines) if key in CONSULT_TYPES else None})
    return totals


# Each rate as the pages say it: "R300.00 a new consult".
RATE_PHRASES = {'initial': 'a new consult', 'follow_up': 'a follow-up consult', 'messages': 'a message answered',
                'review': 'a blood result reviewed'}


def rate_phrases(rates):
    """["R300.00 a new consult", …] for these rates, in the order of ACTIVITIES."""
    return [f'{money(rates[key])} {RATE_PHRASES[key]}' for key, _ in ACTIVITIES]


def csv_filename(report):
    return f'activity-report-{report.period_start:%Y-%m-%d}-to-{report.period_end:%Y-%m-%d}.csv'


def csv_table(report):
    """One row per doctor: each activity's count (and how many consults were marked completed) and amount in Rand,
    then their total payout; then a row adding them all up."""
    headings = ['Doctor']
    for key, label in ACTIVITIES:
        headings += [label, *([f'{label} marked completed'] if key in CONSULT_TYPES else []), f'{label} (R)']
    headings.append('Total payout (R)')
    rows = []
    for doctor in report_doctors(report):
        row = [doctor['doctor']]
        for line in doctor['lines']:
            row += [line['count'], *([line['completed']] if line['completed'] is not None else []), line['amount']]
        rows.append(row + [doctor['total']])
    columns = list(zip(*(row[1:] for row in rows))) if rows else [[0]] * (len(headings) - 1)
    rows.append(['All doctors', *(sum(column, start=type(column[0])(0)) for column in columns)])
    return headings, rows


def _cell(value):
    """A spreadsheet cell that can't be read as a formula, as Excel downloads do (portal.operations_views)."""
    value = '' if value is None else str(value)
    if value.lstrip(' \t\r\n\v\f﻿').startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n', '\v', '\f', '﻿')):
        return "'" + value
    return value


def csv_text(report):
    """The report as a CSV file: the email's attachment and the page's download."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    headings, rows = csv_table(report)
    writer.writerow(headings)
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue()


def report_email(report, recipient):
    """The report's email: a table for each doctor, the total for everyone and the CSV attached. Email stays light."""
    label = period_label(report.period_start, report.period_end)
    message = build_email(to=recipient, subject=f'Doctor activity report, {label}', template='activity_report', context={
        'period': label, 'doctors': report_doctors(report), 'total': money(report.total),
        'url': site_link('portal:activity-report-detail', report.pk),
    })
    message.attach(csv_filename(report), csv_text(report), 'text/csv')
    return message


def require_report_sender(company, actor):
    """Only the practice's Super Admin sends a report on request; the daily job sends as nobody (actor None)."""
    require_enabled_company(company)
    if actor is None:
        return
    if not (actor.is_active and company.is_active and CompanyMembership.objects.filter(
            company=company, user=actor, user__is_active=True, is_active=True, role=CompanyMembership.Role.SUPER_ADMIN).exists()):
        raise PermissionDenied('Only the practice Super Admin can send the monthly activity report.')


def send_activity_report(company, start, end, *, actor=None, resend=False, request=None):
    """Save the period's report and email it to the practice's address: (report, sent).

    A period already emailed isn't sent again unless resend is asked for explicitly; then it is worked out afresh, at
    the rates now. The report is saved only once the email has gone, so a failed send leaves nothing behind and the
    next daily run tries again. The practice is locked meanwhile, so two runs at once never both send."""
    require_report_sender(company, actor)
    if (start, end) != period_ending(end.year, end.month):
        raise ValidationError('A report covers the 25th of one month to the 24th of the next.')
    if end >= reporting_today():
        raise ValidationError('A report can only be sent once its period has ended, from the 25th.')
    if not settings.EMAIL_ENABLED:
        raise ValidationError('Email isn’t set up, so the report can’t be sent.')
    with transaction.atomic():
        company = Company.objects.select_for_update().get(pk=company.pk)
        report = ActivityReport.objects.for_company(company).filter(period_start=start, period_end=end).first()
        if report is not None and report.sent_at is not None and not resend:
            return report, False
        recipient = settings_for(company).activity_report_email.strip()
        if not recipient:
            raise ValidationError('Add the email address to send the report to first.')
        resending = report is not None and report.sent_at is not None
        report = report or ActivityReport(company=company, period_start=start, period_end=end)
        report.rows, report.rates, report.total = build_report(company, start, end)
        report.created_by, report.sent_to, report.sent_at = actor, recipient, timezone.now()
        report.full_clean()
        report.save()
        report_email(report, recipient).send(fail_silently=False)
        record_audit(company=company, actor=actor, action='activity_report.resent' if resending else 'activity_report.sent',
                     target=report, request=request, metadata={'period_start': start.isoformat(), 'period_end': end.isoformat(),
                                                               'doctors': len(report.rows), 'total': str(report.total)})
    return report, True


def due_period(company, today=None):
    """The daily job's check for one practice: the period to send now, or None. From the 25th the period that ended on
    the 24th is due until it has been sent; before the 25th nothing is, so a late run never sends an old month."""
    today = today or reporting_today()
    if today.day < PERIOD_DAY or not settings_for(company).activity_report_email.strip():
        return None
    start, end = latest_period(today)
    if ActivityReport.objects.for_company(company).filter(period_start=start, period_end=end, sent_at__isnull=False).exists():
        return None
    return start, end
