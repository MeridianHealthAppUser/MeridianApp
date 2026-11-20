"""Load the monthly figures from before the app (Dr Czech's spreadsheet) into Insights: new patient bookings and where
they came from. A blank cell is left alone; a number, zero included, is the month's figure. Running it again updates
the same months. They can also be changed on Insights (Historical data)."""

import csv
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care.models import InsightFigure
from practices.models import Company

COLUMNS = {'new_bookings': InsightFigure.Metric.NEW_BOOKINGS, 'google': InsightFigure.Metric.GOOGLE,
           'ai_overview': InsightFigure.Metric.AI_OVERVIEW, 'word_of_mouth': InsightFigure.Metric.WORD_OF_MOUTH,
           'facebook': InsightFigure.Metric.FACEBOOK, 'instagram': InsightFigure.Metric.INSTAGRAM}
DEFAULT_FILE = Path(__file__).resolve().parents[2] / 'fixtures' / 'insight_figures_meridian.csv'


class Command(BaseCommand):
    help = 'Load monthly Insights figures from before the app (month,new_bookings,google,ai_overview,word_of_mouth,facebook,instagram).'

    def add_arguments(self, parser):
        parser.add_argument('--company', required=True, help='Exact practice slug')
        parser.add_argument('--file', default=str(DEFAULT_FILE), help='CSV file (defaults to the figures Dr Czech sent, Nov 2026)')

    @transaction.atomic
    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['company']).first()
        if company is None:
            raise CommandError('Choose an existing practice slug.')
        saved = 0
        with open(options['file'], newline='') as source:
            for line, row in enumerate(csv.DictReader(source), start=2):
                try:
                    year, month = (int(part) for part in row['month'].strip().split('-'))
                    first = date(year, month, 1)
                except (KeyError, ValueError):
                    raise CommandError(f'Line {line}: write the month as YYYY-MM.') from None
                for column, metric in COLUMNS.items():
                    value = (row.get(column) or '').strip()
                    if not value:
                        continue
                    if not value.isdigit():
                        raise CommandError(f'Line {line}: {column} must be a whole number.')
                    InsightFigure.objects.update_or_create(company=company, month=first, metric=metric, defaults={'value': int(value)})
                    saved += 1
        self.stdout.write(self.style.SUCCESS(f'{saved} monthly figures saved for {company.name}.'))
