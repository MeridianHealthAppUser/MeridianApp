from django.core.management.base import BaseCommand

from care.blood_tests import run_checks
from practices.tenancy import enabled_companies


class Command(BaseCommand):
    help = ('Email patients whose blood tests are due today and not done, and hold the parcels of patients whose '
            'blood tests weren’t reviewed a week after their Required by date (care/blood_tests.py). Run once a day.')

    def handle(self, *args, **options):
        for company in enabled_companies():
            told, held = run_checks(company)
            self.stdout.write(f'{company.name}: {told} patient{"" if told == 1 else "s"} told their blood tests are due, '
                              f'{held} parcel{"" if held == 1 else "s"} held.')
