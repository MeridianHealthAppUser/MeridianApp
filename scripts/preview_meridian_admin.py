"""Render synthetic admin pages in an isolated in-memory database for browser checks.

Pipe stdout to scripts/test_meridian_admin.cjs. Never touches the working database.
"""

import json
import os
import sys
from datetime import timedelta
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    DJANGO_SETTINGS_MODULE='config.settings', DATABASE_URL='sqlite:///:memory:',
    DJANGO_DEBUG='true', DJANGO_ALLOWED_HOSTS='testserver',
    MULTI_PRACTICE_ENABLED='false', SINGLE_PRACTICE_SLUG='meridian-health',
    VIDEO_ENABLED='false',
)

import django

django.setup()

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, ClinicalTask, Invoice, PatientSubscription
from practices.models import Company, CompanyMembership, Patient

call_command('migrate', verbosity=0, stdout=StringIO())
user = get_user_model().objects.create_superuser('preview@example.test', 'Preview-only!2026')
user.first_name, user.last_name = 'Alex', 'Morgan'
user.save()
client = Client()
pages = []


def capture(name, url):
    response = client.get(url)
    assert response.status_code == 200, (name, response.status_code)
    pages.append({'name': name, 'path': url, 'html': response.content.decode()})


capture('login', reverse('admin:login'))
client.force_login(user)
capture('empty', reverse('admin:index'))
company = Company.objects.create(name='Meridian Health', slug='meridian-health')
CompanyMembership.objects.create(company=company, user=user, role=CompanyMembership.Role.SUPER_ADMIN)
now = timezone.now()
for week, count in enumerate((3, 7, 5, 11, 14)):
    for index in range(count):
        day = now - timedelta(days=28 - week * 6, hours=index)
        patient = Patient.objects.create(company=company, first_name=f'Example {week}', last_name=f'Patient {index}')
        Patient.objects.filter(pk=patient.pk).update(created_at=day)
        Appointment.objects.create(company=company, patient=patient, clinician=user, starts_at=day,
                                   status=('booked', 'completed', 'completed', 'cancelled', 'no_show')[index % 5])
        if index % 2:
            PatientSubscription.objects.create(company=company, patient=patient, plan_name='Example programme', monthly_amount='1250.00')
        if index == 0:
            Invoice.objects.create(company=company, patient=patient, invoice_number=f'PREVIEW-{week}',
                                   subtotal='1250.00', total='1250.00', due_on=now.date(), status='issued')
            ClinicalTask.objects.create(company=company, patient=patient, title='Example follow-up',
                                        due_at=now - timedelta(days=1), assigned_to=user)
capture('overview', reverse('admin:index'))
capture('patients', reverse('admin:practices_patient_changelist'))
capture('user', reverse('admin:accounts_user_change', args=[user.pk]))
capture('workspace', reverse('portal:desktop-dashboard'))
print(json.dumps(pages))
