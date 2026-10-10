from django.conf import settings
from django.core.management.base import BaseCommand

from care.notifications import send_due_reminders


class Command(BaseCommand):
    help = (f'Email patients and clinicians a reminder of booked appointments starting in the next '
            f'{settings.APPOINTMENT_REMINDER_HOURS} hours. Each appointment is reminded once; run every 15 minutes.')

    def handle(self, *args, **options):
        if not settings.EMAIL_ENABLED:
            self.stdout.write('Email is not set up, so no reminders were sent.')
            return
        reminded = send_due_reminders()
        self.stdout.write(f'Sent reminders for {reminded} appointment{"" if reminded == 1 else "s"}.')
