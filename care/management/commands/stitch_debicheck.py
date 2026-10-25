from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from care.stitch import apply_results_file, collection_file, debicheck_in_use
from practices.tenancy import enabled_companies


class Command(BaseCommand):
    help = ('Stitch DebiCheck collection files (care/stitch.py). --write DIR writes the next collection file into DIR '
            '(for an SFTP sync to send to Stitch’s Outgoing/Collections folder); --read FILE applies a REPLY or OUTPUT '
            'file from Stitch’s Incoming/Collections folder. Run --write after the morning debit run.')

    def add_arguments(self, parser):
        parser.add_argument('--write', metavar='DIR', help='Write the next collection file into this folder.')
        parser.add_argument('--read', metavar='FILE', nargs='+', help='Apply these REPLY or OUTPUT files.')

    def handle(self, *args, **options):
        if not (options['write'] or options['read']):
            raise CommandError('Give --write DIR or --read FILE.')
        for company in enabled_companies():
            if not debicheck_in_use(company):
                self.stdout.write(f'{company.name}: Stitch holds no DebiCheck debit orders.')
                continue
            if options['write']:
                made = collection_file(company)
                if made is None:
                    self.stdout.write(f'{company.name}: no DebiCheck collections waiting.')
                else:
                    name, text, count = made
                    path = Path(options['write']) / name
                    path.write_text(text)
                    self.stdout.write(f'{company.name}: {count} collection{"" if count == 1 else "s"} written to {path}.')
            for name in options['read'] or ():
                try:
                    counts = apply_results_file(company, Path(name).read_text(encoding='utf-8-sig'))
                except OSError as error:
                    raise CommandError(f'{name}: {error}') from None
                except ValidationError as error:
                    raise CommandError(f'{name}: {" ".join(error.messages)}') from None
                self.stdout.write(f'{company.name}: {name}: {counts["paid"]} paid, {counts["failed"]} failed, '
                                  f'{counts["pending"]} pending, {counts["ignored"]} ignored.')
