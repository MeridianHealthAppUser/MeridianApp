from django.core.management.base import BaseCommand

from care.billing import run_debits
from care.payment_providers import current_provider
from practices.tenancy import enabled_companies


class Command(BaseCommand):
    help = ('Collect the subscription debits that have come round, and retry failed ones on their retry days '
            '(care/billing.py). Run once a day, in the morning.')

    def handle(self, *args, **options):
        if current_provider() is None:
            self.stdout.write('No payment provider is connected, so no debits were collected.')
            return
        for company in enabled_companies():
            counts = run_debits(company)
            self.stdout.write(f'{company.name}: {counts["tried"]} debit{"" if counts["tried"] == 1 else "s"} tried, '
                              f'{counts["paid"]} paid, {counts["failed"]} failed.')
