"""Explicitly connect an existing technical administrator to the enabled practice."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care.services import record_audit
from practices.models import Company, CompanyMembership


class Command(BaseCommand):
    help = (
        'Connect an existing active Django superuser to the configured single practice. '
        'New memberships use Super Admin; existing active roles are preserved. '
        'Creates no accounts or demo data.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True, help='Email of the existing active Django superuser.')

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.MULTI_PRACTICE_ENABLED:
            raise CommandError('This command is only available in single-practice mode.')

        slug = settings.SINGLE_PRACTICE_SLUG
        try:
            Company._meta.get_field('slug').clean(slug, None)
        except ValidationError as error:
            raise CommandError('SINGLE_PRACTICE_SLUG must be a valid, non-empty practice slug.') from error

        # Case-insensitive matching is convenient, but never choose between two
        # existing identities whose email addresses differ only in case.
        users = list(get_user_model().objects.filter(
            email__iexact=options['email'].strip(),
        )[:2])
        if len(users) != 1 or not users[0].is_active or not users[0].is_superuser or not users[0].is_staff:
            raise CommandError('Provide the email of one existing, active Django superuser.')
        user = users[0]

        company, company_created = Company.objects.select_for_update().get_or_create(
            slug=slug,
            defaults={'name': 'Meridian Health', 'is_active': True},
        )
        if not company.is_active:
            raise CommandError('The configured practice is inactive. No access was changed.')

        # Practice administration and role switching also lock company first,
        # then user. Recheck privileges after the lock, not a stale identity.
        user = get_user_model().objects.select_for_update().filter(
            pk=user.pk, email__iexact=options['email'].strip(),
        ).first()
        if user is None or not user.is_active or not user.is_superuser or not user.is_staff:
            raise CommandError('Provide the email of one existing, active Django superuser.')

        membership, membership_created = CompanyMembership.objects.get_or_create(
            company=company,
            user=user,
            defaults={'role': CompanyMembership.Role.SUPER_ADMIN, 'is_active': True},
        )
        if not membership.is_active or membership.role not in CompanyMembership.Role.values:
            raise CommandError(
                'This account already has an inactive or unsupported practice role. '
                'Review that membership explicitly; this command will not replace it.'
            )

        if company_created:
            record_audit(
                company=company, actor=user, action='practice.created', target=company,
                metadata={'source': 'bootstrap_practice_admin'},
            )
        if membership_created:
            record_audit(
                company=company, actor=user, action='staff.membership_linked', target=membership,
                metadata={'source': 'bootstrap_practice_admin', 'user_id': user.pk, 'role': membership.role},
            )

        result = 'Created' if membership_created else 'Already has'
        self.stdout.write(self.style.SUCCESS(
            f'{result} active {membership.get_role_display()} membership for {company.name} ({company.slug}). '
            'The existing account can now enter the practice workspace.'
        ))
