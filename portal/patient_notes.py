"""A patient's notes, as the record's Notes tab and the Patient notes list under a conversation show them.

Signed consult notes (labelled "Consult note", linking to the consultation) and the general notes
written on the record, newest first. Another clinician's private notes, unsigned consult drafts and
the note a signed consult keeps as its snapshot are left out. Clinical staff only.
"""

from types import SimpleNamespace

from django.db.models import Prefetch, Q
from django.urls import reverse

from care.models import ClinicalEncounter, ClinicalNote, RecordTag


def note_entry(note):
    """A general note, labelled by its type. It stays the note itself, so its tags can be edited."""
    note.kind, note.anchor, note.label, note.at = 'note', f'note-{note.pk}', note.get_note_type_display(), note.created_at
    note.by = note.author.full_name if note.author_id else ''
    note.text, note.private, note.url = note.body, note.is_private, ''
    return note


def consult_entry(encounter):
    return SimpleNamespace(
        pk=None, kind='consult', anchor=f'consult-{encounter.pk}', label='Consult note', at=encounter.occurred_at,
        by=(encounter.signed_by or encounter.clinician).full_name, text=encounter.clinical_summary, private=False,
        url=reverse('portal:clinical-consultation-detail', args=[encounter.pk]), tags_form=None,
    )


class PatientNotes:
    """Both kinds merged newest first. A slice reads only that far into each, so a page never loads everything."""

    def __init__(self, company, membership, patient, viewer):
        self.consults = ClinicalEncounter.objects.for_company(company).filter(
            patient=patient, status=ClinicalEncounter.Status.SIGNED,
        ).select_related('clinician', 'signed_by').order_by('-occurred_at', '-pk')
        visible = Q(is_private=False)
        if membership.is_clinician:
            visible |= Q(author=viewer)
        self.notes = ClinicalNote.objects.for_company(company).filter(
            visible, patient=patient, signed_encounter__isnull=True,
        ).select_related('author').prefetch_related(
            Prefetch('tags', queryset=RecordTag.objects.for_company(company)),
        ).order_by('-created_at', '-pk')

    def count(self):
        return self.consults.count() + self.notes.count()

    def __len__(self):
        return self.count()

    def none(self):
        return []

    def __getitem__(self, index):
        if not isinstance(index, slice):
            return self[index:index + 1][0]
        stop = index.stop if index.stop is not None else self.count()
        entries = [consult_entry(encounter) for encounter in self.consults[:stop]]
        entries += [note_entry(note) for note in self.notes[:stop]]
        entries.sort(key=lambda entry: entry.at, reverse=True)
        return entries[index]
