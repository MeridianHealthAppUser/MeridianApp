"""Who is in a conversation, as tags (templates/portal/includes/person_tag.html), instead of "With …".

The patient is white with teal and a person, a doctor teal with a light teal fill and a medical cross, a dietitian green with a leaf,
and practice administrators light blue with a briefcase. Someone no longer in the practice gets a plain grey tag.
Clinicians show as their title, first initial and surname ("Dr J. Czech"), administrators as their first name and
surname initial ("Lindiwe M."); the patient, and anyone without both names, by their full name.
"""

from django.contrib.auth import get_user_model
from django.db.models import Prefetch

from practices.models import CompanyMembership

LINKS = {'participants': 'message_thread_links', 'members': 'team_thread_links'}
LABELS = {'patient': 'Patient', 'doctor': 'Doctor', 'dietitian': 'Dietitian', 'admin': 'Practice admin', 'other': 'Former staff'}


def staff_kinds(company, user_ids):
    """Each person's tag in this practice: their clinician type, or 'admin' for administrators. One query."""
    rows = CompanyMembership.objects.filter(company=company, user_id__in=set(user_ids)).values_list('user_id', 'clinician_type')
    return {user_id: clinician_type or 'admin' for user_id, clinician_type in rows}


def in_join_order(relation):
    """Prefetch a conversation's staff ('participants' of a patient conversation, 'members' of a team one) in the
    order they joined, as the open conversation and its colours have them. Plain, they come back in no set order."""
    link = LINKS[relation]
    return Prefetch(relation, queryset=get_user_model().objects.order_by(f'{link}__created_at', f'{link}__pk'))


def person_tag(name, kind, full_name=None):
    return dict(name=name, kind=kind, label=LABELS[kind], full_name=full_name or name)


def short_name(person, kind):
    first, last = getattr(person, 'given_name', person.first_name).strip(), person.last_name.strip()
    if not first or not last:
        return person.full_name
    if kind == 'admin':
        return f'{first} {last[0]}.'
    if kind in ('doctor', 'dietitian'):
        title = person.get_title_display() if person.title else ''
        return ' '.join(filter(None, [title, f'{first[0]}.', last]))
    return person.full_name


def people_tags(people, kinds, *, patient=None):
    """The patient first, when they're in the conversation, then everyone else in the order given."""
    tags = [person_tag(str(patient), 'patient')] if patient is not None else []
    for person in people:
        kind = kinds.get(person.pk, 'other')
        tags.append(person_tag(short_name(person, kind), kind, person.full_name))
    return tags
