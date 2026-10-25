from django.conf import settings
from django.core.management.base import BaseCommand

from care.journeys import send_due
from practices.tenancy import enabled_companies


class Command(BaseCommand):
    help = 'Send the email journeys’ emails that are due today (care/journeys.py). Run once a day.'

    def handle(self, *args, **options):
        if not settings.EMAIL_ENABLED:
            self.stdout.write('Email is not set up, so no journey emails were sent.')
            return
        for company in enabled_companies():
            sent = send_due(company)
            self.stdout.write(f'{company.name}: {sent} journey email{"" if sent == 1 else "s"} sent.')
