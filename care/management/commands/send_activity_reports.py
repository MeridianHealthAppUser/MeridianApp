from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from care.activity_reports import due_period, period_label, send_activity_report
from care.reporting import reporting_today
from practices.tenancy import enabled_companies


class Command(BaseCommand):
    help = ('Email each practice’s monthly doctor activity report (care/activity_reports.py): from the 25th, the period '
            'from the 25th of last month to the 24th, once. Run once a day; a period already sent is never sent again.')

    def add_arguments(self, parser):
        parser.add_argument('--company', help='Only this practice (its slug)')
        parser.add_argument('--dry-run', action='store_true', help='Say what would be sent, without sending or saving it')

    def handle(self, *args, **options):
        companies = enabled_companies().order_by('name')
        if options['company']:
            companies = companies.filter(slug=options['company'])
            if not companies.exists():
                raise CommandError('Choose an active practice by its slug.')
        if not settings.EMAIL_ENABLED and not options['dry_run']:
            self.stdout.write('Email is not set up, so no activity reports were sent.')
            return
        today, failed = reporting_today(), 0
        for company in companies:
            period = due_period(company, today)
            if period is None:
                self.stdout.write(f'{company.name}: no activity report due.')
                continue
            label = period_label(*period)
            if options['dry_run']:
                self.stdout.write(f'{company.name}: would send the activity report for {label}.')
                continue
            try:
                report, sent = send_activity_report(company, *period)
            except (PermissionDenied, ValidationError) as error:
                failed += 1
                self.stderr.write(f'{company.name}: the activity report for {label} was not sent. {" ".join(getattr(error, "messages", [str(error)]))}')
                continue
            except Exception as error:  # The mail server refused it: nothing was saved, so tomorrow's run tries again.
                failed += 1
                self.stderr.write(f'{company.name}: the activity report for {label} could not be emailed ({error.__class__.__name__}). It will be tried again on the next run.')
                continue
            self.stdout.write(f'{company.name}: activity report for {label} sent to {report.sent_to}.' if sent
                              else f'{company.name}: activity report for {label} was already sent.')
        if failed:
            raise CommandError(f'{failed} activity report{"" if failed == 1 else "s"} not sent.')
