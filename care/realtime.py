"""Content-free message invalidations, delivered only after the database commits.

PostgreSQL remains the source of messages and read positions. A socket event only
asks the browser to refetch its own authorized view; it never contains a message,
patient identifier, conversation identifier, or recipient list.
"""

import asyncio
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver

from practices.models import CompanyMembership
from practices.tenancy import enabled_companies
from .models import (
    MessageThread, MessageThreadParticipant, PatientMessage,
    TeamMessage, TeamThread, TeamThreadMember,
)

logger = logging.getLogger(__name__)
BROADCAST_TIMEOUT_SECONDS = 5


def user_group(user_id):
    return f'messages.user.{user_id}'


def _recipients(kind, thread_id, using):
    model = MessageThread if kind == 'patient' else TeamThread
    thread = model.objects.using(using).filter(
        pk=thread_id, company__in=enabled_companies().using(using),
    ).first()
    if thread is None:
        return set()
    staff = CompanyMembership.objects.using(using).filter(
        company_id=thread.company_id, is_active=True, user__is_active=True,
    )
    if kind == 'team':
        member_ids = TeamThreadMember.objects.using(using).filter(
            thread_id=thread.pk, company_id=thread.company_id,
        ).values('user_id')
        return set(staff.filter(user_id__in=member_ids).values_list('user_id', flat=True))

    patient = thread.patient
    if not patient.is_active or patient.company_id != thread.company_id:
        return set()
    participant_ids = MessageThreadParticipant.objects.using(using).filter(
        thread_id=thread.pk, company_id=thread.company_id,
    ).values('user_id')
    recipients = set(staff.filter(
        Q(user_id__in=participant_ids) | Q(role=CompanyMembership.Role.SUPER_ADMIN),
    ).values_list('user_id', flat=True))
    if patient.user_id and patient.user.is_active:
        recipients.add(patient.user_id)
    return recipients


def _deliver(kind, thread_id, using):
    try:
        recipients = _recipients(kind, thread_id, using)
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
    except Exception as error:
        # A transport or lookup failure must never turn a committed send into a
        # failed HTTP response that tempts the sender to submit it a second time.
        logger.warning('Could not prepare realtime message update (%s).', type(error).__name__)
        return
    try:
        async_to_sync(_broadcast)(channel_layer, recipients)
    except Exception as error:
        logger.warning('Could not deliver realtime message update (%s).', type(error).__name__)


async def _broadcast(channel_layer, recipients):
    # Bound the whole fan-out, not each recipient separately: a stalled Redis
    # connection must not hold the committed HTTP send/read response indefinitely.
    # The browser's authorized reconciliation request recovers missed updates.
    results = await asyncio.wait_for(asyncio.gather(*(
        channel_layer.group_send(user_group(user_id), {'type': 'messages.changed'}) for user_id in recipients
    ), return_exceptions=True), timeout=BROADCAST_TIMEOUT_SECONDS)
    for result in results:
        if isinstance(result, Exception):
            logger.warning('Could not deliver realtime message update (%s).', type(result).__name__)


def _queue(kind, thread, using=None):
    thread_id = getattr(thread, 'pk', thread)
    using = using or getattr(getattr(thread, '_state', None), 'db', None) or DEFAULT_DB_ALIAS
    transaction.on_commit(lambda: _deliver(kind, thread_id, using), using=using)


def patient_thread_changed(thread, *, using=None):
    """Also call after bulk updates, which deliberately do not emit model signals."""
    _queue('patient', thread, using=using)


def team_thread_changed(thread, *, using=None):
    _queue('team', thread, using=using)


@receiver(post_save, sender=PatientMessage)
@receiver(post_save, sender=TeamMessage)
def message_saved(sender, instance, raw, using, **kwargs):
    if not raw:
        callback = patient_thread_changed if sender is PatientMessage else team_thread_changed
        callback(instance.thread_id, using=using)


@receiver(post_save, sender=MessageThread)
@receiver(post_save, sender=TeamThread)
def thread_saved(sender, instance, raw, created, using, update_fields, **kwargs):
    # A message already publishes its own update. Email bookkeeping and the
    # last-message timestamp do not need a second event.
    if not raw and not created and (update_fields is None or {'subject', 'is_closed'} & update_fields):
        callback = patient_thread_changed if sender is MessageThread else team_thread_changed
        callback(instance, using=using)


@receiver(post_save, sender=MessageThreadParticipant)
@receiver(post_save, sender=TeamThreadMember)
def participant_saved(sender, instance, raw, created, using, update_fields, **kwargs):
    if not raw and (created or update_fields is None or 'last_read_at' in update_fields):
        callback = patient_thread_changed if sender is MessageThreadParticipant else team_thread_changed
        callback(instance.thread_id, using=using)
