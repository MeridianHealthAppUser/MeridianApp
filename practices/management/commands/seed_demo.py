from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care.demo import seed_demo_care
from care.notifications import paused as notifications_paused
from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import multi_practice_enabled


DEMO_PASSWORD = 'MeridianDemo!2026'

# More of Sam's patients, each with their own portal login, so lists, bookings and messages have more than
# one person in them. Only their basic details are seeded; Nadia keeps the full clinical history.
DEMO_PATIENTS = (
    ('ayesha.p@example.co.za', 'Ayesha', 'Patel', 'MH-DEMO-002', date(1985, 3, 14), '+27 82 555 0102', 'Johannesburg'),
    ('pieter.vdm@example.co.za', 'Pieter', 'van der Merwe', 'MH-DEMO-003', date(1974, 8, 2), '+27 82 555 0103', 'Durban'),
    ('zanele.n@example.co.za', 'Zanele', 'Ndlovu', 'MH-DEMO-004', date(1997, 11, 23), '+27 82 555 0104', 'Pretoria'),
)


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
        if not multi_practice_enabled() and settings.SINGLE_PRACTICE_SLUG != 'meridian-health':
            raise CommandError('The demo command is only for Meridian Health. No records were changed.')
        orion = None
        if multi_practice_enabled():
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
            *({'email': email, 'first_name': first_name, 'last_name': last_name, 'is_staff': False, 'is_superuser': False}
              for email, first_name, last_name, *_ in DEMO_PATIENTS),
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
            if company is None:
                continue
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

        for email, first_name, last_name, record_number, born, phone, city in DEMO_PATIENTS:
            Patient.objects.update_or_create(
                company=meridian,
                user=users[email],
                defaults={
                    'first_name': first_name,
                    'last_name': last_name,
                    'medical_record_number': record_number,
                    'date_of_birth': born,
                    'phone': phone,
                    'city': city,
                    'assigned_doctor': users['sam.marchant@meridianhealth.co.za'],
                    'is_active': True,
                },
            )

        with notifications_paused():  # Demo messages email nobody.
            seed_demo_care(meridian=meridian, orion=orion, users=users, nadia=nadia)

        self.stdout.write(self.style.SUCCESS(
            f'Demo data is ready: {2 if orion else 1} practices, {len(people)} accounts, '
            f'{5 if orion else 3} staff memberships, patient portals, and clinical demo records. '
            f'New demo accounts use: {DEMO_PASSWORD}. Existing passwords are preserved unless --reset-passwords is supplied.'
        ))
