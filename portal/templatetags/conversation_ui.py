"""Who wrote each message in a conversation, shown by colour.

Your own messages sit on the right in white. Everyone else's sit on the left, each person in their own colour:
in a patient conversation the patient is blue; in a team conversation the first other member is. The others
follow in the order they joined the conversation: orange, pink, yellow, purple, dark green, red, light green,
then navy, starting again at blue after that. Colours are worked out for whoever is reading, so the same
person can have a different colour for someone else.
"""

from django import template
from django.utils import timezone
from django.utils.dateformat import format as date_format

register = template.Library()

TONES = ('blue', 'orange', 'pink', 'yellow', 'purple', 'dark-green', 'red', 'light-green', 'navy')


def toned_messages(messages, viewer_id, people):
    """Each message with is_mine, and tone (blank for your own). people: user ids, the main person first."""
    tones = {}

    def tone_for(person):
        if person not in tones:
            tones[person] = TONES[len(tones) % len(TONES)]
        return tones[person]

    for person in people:
        if person != viewer_id:
            tone_for(person)
    messages = list(messages)
    for message in messages:
        message.is_mine = message.sender_id is not None and message.sender_id == viewer_id
        # Someone who has left the conversation, or whose account is gone, still gets a colour of their own.
        message.tone = '' if message.is_mine else tone_for(message.sender_id)
    return messages


def _rows(thread, loaded, related):
    rows = getattr(thread, loaded, None)
    return rows if rows is not None else getattr(thread, related).all()


@register.simple_tag
def patient_thread_messages(thread, viewer):
    """A patient conversation's messages for the reader: the patient first, then staff in the order they joined."""
    people = [thread.patient.user_id] + [row.user_id for row in _rows(thread, 'participant_rows', 'participant_links')]
    return toned_messages(thread.conversation_messages, viewer.pk, people)


@register.simple_tag
def team_thread_messages(thread, viewer):
    """A team conversation's messages for the reader, members in the order they joined."""
    people = [row.user_id for row in _rows(thread, 'member_rows', 'member_links')]
    return toned_messages(thread.conversation_messages, viewer.pk, people)


@register.filter
def message_time(value, older_format='j M, H:i'):
    """When a message was sent: just the time for today (06:00), with the date for anything older (7 Oct, 20:37)."""
    if not value:
        return ''
    local = timezone.localtime(value)
    return date_format(local, 'H:i' if local.date() == timezone.localdate() else older_format)
