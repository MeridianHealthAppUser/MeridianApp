from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care.demo import seed_demo_care
from practices.models import Company, CompanyMembership, Patient


DEMO_PASSWORD = 'MeridianDemo!2026'


class Command(BaseCommand):
    help = 'Create or update the local Meridian demo companies, users, and memberships.'

    def add_arguments(self, parser):
        parser.add_argument('--reset-passwords', action='store_true', help='Explicitly reset existing local demo accounts to the documented demo password.')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('Demo seeding is disabled outside development. It resets known local passwords and must never run against production data.')
        meridian, _ = Company.objects.update_or_create(
            slug='meridian-health',
            defaults={'name': 'Meridian Health', 'is_active': True},
        )
        orion, _ = Company.objects.update_or_create(
            slug='orion-mens-health',
            defaults={'name': "Orion Men’s Health", 'is_active': True},
        )

        user_model = get_user_model()
        people = (
            {
                'email': 'joshua.czech@meridianhealth.co.za',
                'first_name': 'Joshua',
                'last_name': 'Czech',
                'is_staff': True,
                # A Super Admin membership belongs to a practice. Site-wide
                # technical administration requires a separate account.
                'is_superuser': False,
            },
            {
                'email': 'sam.marchant@meridianhealth.co.za',
                'first_name': 'Sam',
                'last_name': 'Marchant',
                'is_staff': True,
                'is_superuser': False,
            },
            {
                'email': 'lindiwe.mahlangu@meridianhealth.co.za',
                'first_name': 'Lindiwe',
                'last_name': 'Mahlangu',
                'is_staff': True,
                'is_superuser': False,
            },
            {
                'email': 'nadia.m@example.co.za',
                'first_name': 'Nadia',
                'last_name': 'Mokoena',
                'is_staff': False,
                'is_superuser': False,
            },
        )

        users = {}
        for person in people:
            user, created = user_model.objects.update_or_create(
                email=person['email'],
                defaults={
                    'first_name': person['first_name'],
                    'last_name': person['last_name'],
                    'is_active': True,
                    'is_staff': person['is_staff'],
                    'is_superuser': person['is_superuser'],
                },
            )
            if created or options.get('reset_passwords'):
                user.set_password(DEMO_PASSWORD)
                user.save(update_fields=['password'])
            users[person['email']] = user

        memberships = (
            (users['joshua.czech@meridianhealth.co.za'], meridian, CompanyMembership.Role.SUPER_ADMIN),
            (users['joshua.czech@meridianhealth.co.za'], orion, CompanyMembership.Role.SUPER_ADMIN),
            (users['sam.marchant@meridianhealth.co.za'], meridian, CompanyMembership.Role.DOCTOR),
            (users['sam.marchant@meridianhealth.co.za'], orion, CompanyMembership.Role.DOCTOR),
            (users['lindiwe.mahlangu@meridianhealth.co.za'], meridian, CompanyMembership.Role.PRACTICE_ADMIN),
        )
        for user, company, role in memberships:
            CompanyMembership.objects.update_or_create(
                user=user,
                company=company,
                defaults={'role': role, 'is_active': True},
            )

        nadia, _ = Patient.objects.update_or_create(
            company=meridian,
            user=users['nadia.m@example.co.za'],
            defaults={
                'first_name': 'Nadia',
                'last_name': 'Mokoena',
                'medical_record_number': 'MH-DEMO-001',
                'is_active': True,
            },
        )

        seed_demo_care(meridian=meridian, orion=orion, users=users, nadia=nadia)

        self.stdout.write(self.style.SUCCESS(
            'Demo data is ready: 2 practices, 4 accounts, 5 staff memberships, patient portals, and clinical demo records. '
            f'New demo accounts use: {DEMO_PASSWORD}. Existing passwords are preserved unless --reset-passwords is supplied.'
        ))
