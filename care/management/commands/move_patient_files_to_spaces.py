"""Move patient files still held in the database to the configured DigitalOcean Space.

Run it once after setting up the Space (and any time later: it only moves what is still in the
database, so running it again is safe). Each file is uploaded, checked, and only then cleared from
the database. Output names files by id only.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from care.models import PatientFile
from care.patient_files import get_storage, move_to_storage


class Command(BaseCommand):
    help = 'Move patient files (PathCare forms, lab reports) from the database to the configured Space.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only count what would be moved.')
        parser.add_argument('--batch-size', type=int, default=50, help='Files to look up at a time (default 50).')

    def handle(self, *args, dry_run=False, batch_size=50, **options):
        if not getattr(settings, 'SPACES_CONFIGURED', False) or get_storage() is None:
            raise CommandError('Spaces is not configured. Set SPACES_BUCKET, SPACES_ACCESS_KEY_ID, '
                               'SPACES_SECRET_ACCESS_KEY and SPACES_REGION, then run this again.')
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')
        pending = PatientFile.objects.filter(storage_key='', content__isnull=False)
        totals = pending.aggregate(size=Sum('size'))
        count = pending.count()
        if dry_run:
            self.stdout.write(f'{count} file(s), {(totals["size"] or 0) / 1024 / 1024:.1f} MB, would be moved '
                              f'to the Space "{settings.SPACES_BUCKET}". Nothing was changed.')
            return
        moved = skipped = failed = 0
        last_pk = 0
        while True:
            batch = list(pending.filter(pk__gt=last_pk).order_by('pk').values_list('pk', flat=True)[:batch_size])
            if not batch:
                break
            for pk in batch:
                try:
                    if move_to_storage(pk):
                        moved += 1
                    else:
                        skipped += 1
                except Exception as error:  # Leave it in the database and carry on with the rest.
                    failed += 1
                    self.stderr.write(f'Patient file {pk} was not moved ({type(error).__name__}); it stays in the database.')
            last_pk = batch[-1]
            self.stdout.write(f'{moved} moved so far…')
        self.stdout.write(f'Done: {moved} moved, {skipped} already moved, {failed} failed.')
        if failed:
            raise CommandError(f'{failed} file(s) could not be moved. They are still in the database; run this again.')
