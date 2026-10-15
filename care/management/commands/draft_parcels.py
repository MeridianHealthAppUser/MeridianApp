from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from care.models import TreatmentAuthorization
from care.parcels import schedule_parcels
from practices.models import Company


class Command(BaseCommand):
    help = ('Draft the parcels that current prescriptions still need on the Weekly shipping list, for prescriptions '
            'written before parcels were drafted. Safe to run again: it carries on after each one\'s last parcel. '
            'Nothing is sent.')

    def add_arguments(self, parser):
        parser.add_argument('--company', required=True, help='Exact practice slug')

    @transaction.atomic
    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['company'], is_active=True).first()
        if company is None:
            raise CommandError('Choose an active practice.')
        prescriptions = TreatmentAuthorization.objects.for_company(company).filter(
            status=TreatmentAuthorization.Status.ACTIVE, dose__isnull=False, expires_on__gte=timezone.localdate(),
            patient__is_active=True,
        ).select_related('company', 'dose', 'product').order_by('patient_id', 'starts_on', 'pk')
        drafted = sum(len(schedule_parcels(prescription)) for prescription in prescriptions)
        self.stdout.write(self.style.SUCCESS(f'{drafted} draft parcel{"" if drafted == 1 else "s"} added for {company.name}.'))
