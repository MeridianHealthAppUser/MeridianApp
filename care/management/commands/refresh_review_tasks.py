from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from care.review_automation import refresh_review_tasks
from practices.models import Company


class Command(BaseCommand):
    help = 'Create local doctor review reminders for one practice. No emails, prescriptions or payments.'

    def add_arguments(self, parser):
        parser.add_argument('--company', required=True, help='Exact practice slug')
        parser.add_argument('--actor-email', required=True, help='An active Super Admin in this practice')
        parser.add_argument('--within-days', type=int, default=30)

    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['company'], is_active=True).first()
        actor = get_user_model().objects.filter(email__iexact=options['actor_email'], is_active=True).first()
        if company is None or actor is None:
            raise CommandError('Choose an active practice and an active Super Admin account.')
        try:
            stats = refresh_review_tasks(company=company, actor=actor, within_days=options['within_days'])
        except (PermissionDenied, ValidationError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS('Local review check complete: ' + ', '.join(f'{key}={value}' for key, value in stats.items())))
