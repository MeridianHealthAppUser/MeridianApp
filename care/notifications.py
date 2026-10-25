"""Emails about what happens in the app, sent once the change is saved: in the background when a worker runs
(care/tasks.py), otherwise straight away. Their look is in care/emails.py.

- New message: to everyone else in the conversation, the patient and staff alike, and to the other members of a
  team conversation. Someone is emailed once about a conversation until they've opened it since; after that the
  next message emails them again. The message itself is never in the email.
- Appointment confirmed, when booked or moved to a new time: to the patient, and to the clinician unless they did
  it themselves.
- Appointment cancelled: to the patient, with the practice's reason when the practice cancelled it, and to the
  clinician unless they cancelled it themselves. The patient's own reason is never emailed.
- Appointment reminders, a day before and shortly before it starts, to the patient and the clinician
  (`manage.py send_appointment_reminders`, every 15 minutes). A recent confirmation replaces only the day-before
  reminder; the 15-minute reminder records successful delivery separately for each recipient.
- Welcome: to a patient who made their account at checkout.
- Win-back: once, to a patient who cancels their subscription.
- Billing (care.billing): a debit that failed (the first time, and when its retries run out); a debit order that
  needs a new amount approved after a prescription or price change; and the practice asking for a new debit order.

Patients are emailed only with an active login, and staff only while active in the practice. Every email a patient
is sent is logged against them (EmailDelivery), which the communications log lists.
"""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from practices.models import CompanyMembership
from practices.tenancy import enabled_companies
from .emails import appointment_details, build_email, greeting_name, person_name, site_link
from .models import (
    Appointment, AppointmentStartReminder, DebitMandate, EmailDelivery, MessageThread, MessageThreadParticipant,
    PatientMessage, PatientSubscription, SubscriptionCycle, TeamMessage, TeamThreadMember,
)

logger = logging.getLogger(__name__)
_paused = ContextVar('notifications_paused', default=False)


@contextmanager
def paused():
    """Send nothing for changes made inside, such as demo data."""
    token = _paused.set(True)
    try:
        yield
    finally:
        _paused.reset(token)


def queue(kind, object_id, actor_id=None):
    """Email about this once the change is saved. Nothing is sent when it isn't, or without email set up."""
    if not settings.EMAIL_ENABLED or _paused.get():
        return
    from .tasks import send_notification

    def send():
        try:
            send_notification.delay(kind, object_id, actor_id=actor_id)
        except Exception:  # The queue can't be reached; the change itself still stands.
            logger.exception('Could not queue the %s email for %s.', kind, object_id)

    transaction.on_commit(send)


def recipients(kind, object_id, actor_id=None):
    """Who to email, as user ids. For messages this also records that they were emailed."""
    return KINDS[kind][1](KINDS[kind][0](object_id), actor_id)


def deliver(kind, object_id, recipient_id, actor_id=None):
    load, _, build = KINDS[kind]
    obj, recipient = load(object_id), get_user_model().objects.get(pk=recipient_id)
    message = build(obj, recipient, actor_id)
    message.send(fail_silently=False)
    log_delivery(kind, obj, recipient, message.subject)


def _about_patient(obj):
    """The patient an email's object is about, if any."""
    from practices.models import Patient

    if isinstance(obj, PatientMessage):
        return obj.thread.patient
    if hasattr(obj, 'patient_id') and getattr(obj, 'patient_id', None):
        return obj.patient
    if isinstance(obj, get_user_model()):
        return Patient.objects.filter(user=obj, is_active=True).order_by('-pk').first()
    return None


def log_delivery(kind, obj, recipient, subject):
    """Log an email sent to a patient against their record. Staff emails and anything not about a patient aren't."""
    patient = _about_patient(obj)
    if patient is None or patient.user_id != recipient.pk:
        return None
    return EmailDelivery.objects.create(company_id=patient.company_id, patient=patient, recipient_email=recipient.email,
                                        kind=kind, subject=subject[:255], sent_at=timezone.now(), status='sent')


def send_due_reminders(*, retry_delays=()):
    """Send day-before and independent 15-minute reminders; count appointments with a delivery.

    The scheduled command supplies short retry delays for failed start reminders.
    Waiting happens outside database transactions, and every retry reloads the
    appointment and skips recipients whose email already succeeded.
    """
    if not settings.EMAIL_ENABLED:
        return 0
    from .tasks import send_notification

    now = timezone.now()
    due = Appointment.objects.filter(
        status=Appointment.Status.BOOKED, reminder_sent_at__isnull=True,
        starts_at__gt=now + timedelta(minutes=settings.APPOINTMENT_START_REMINDER_MINUTES),
        starts_at__lte=now + timedelta(hours=settings.APPOINTMENT_REMINDER_HOURS),
        patient__is_active=True, company__in=enabled_companies(),
    )
    reminded = set()
    for appointment_id in due.values_list('pk', flat=True):
        # Claimed first, so two runs at once never remind twice.
        if Appointment.objects.filter(pk=appointment_id, reminder_sent_at__isnull=True).update(reminder_sent_at=now):
            send_notification.delay('appointment_reminder', appointment_id)
            reminded.add(appointment_id)
    sent, pending = _send_start_reminders()
    reminded.update(sent)
    for delay in retry_delays:
        if not pending:
            break
        time.sleep(delay)
        sent, pending = _send_start_reminders(appointment_ids=pending)
        reminded.update(sent)
    return len(reminded)


def _send_start_reminders(*, appointment_ids=None):
    now = timezone.now()
    due = Appointment.objects.filter(
        status=Appointment.Status.BOOKED, starts_at__gt=now,
        starts_at__lte=now + timedelta(minutes=settings.APPOINTMENT_START_REMINDER_MINUTES),
        patient__is_active=True, company__in=enabled_companies(),
    )
    if appointment_ids is not None:
        due = due.filter(pk__in=appointment_ids)
    sent, pending = set(), set()
    for appointment_id in due.values_list('pk', flat=True):
        delivered, failed = _send_start_reminder(appointment_id)
        if delivered:
            sent.add(appointment_id)
        if failed:
            pending.add(appointment_id)
    return sent, pending


@transaction.atomic
def _send_start_reminder(appointment_id):
    # Serialize overlapping invocations, then recheck cancellation, start time,
    # and recipients. Holding this appointment lock during the bounded SMTP send
    # also prevents a concurrent cancellation from being followed by a reminder.
    now = timezone.now()
    appointment = Appointment.objects.select_for_update(of=('self',)).filter(
        pk=appointment_id, status=Appointment.Status.BOOKED, starts_at__gt=now,
        starts_at__lte=now + timedelta(minutes=settings.APPOINTMENT_START_REMINDER_MINUTES),
        patient__is_active=True, company__in=enabled_companies(),
    ).first()
    if appointment is None or appointment.patient.company_id != appointment.company_id:
        return False, False
    successful = set(AppointmentStartReminder.objects.filter(
        appointment=appointment, starts_at=appointment.starts_at,
    ).values_list('recipient_id', flat=True))
    delivered = failed = False
    for recipient_id in set(_appointment_people(appointment, None)) - successful:
        # A previous send may have waited on SMTP until the appointment started.
        if appointment.starts_at <= timezone.now():
            break
        user = get_user_model().objects.filter(pk=recipient_id, is_active=True).exclude(email='').first()
        if user is None:
            continue
        try:
            # Send in this job even when ordinary notification emails use Celery:
            # the success record must reflect SMTP acceptance, not queue insertion.
            message = _appointment_email(appointment, user, reminder=True, starting=True)
            accepted = message.send(fail_silently=False)
            if not accepted:
                raise RuntimeError('The email backend did not accept the reminder.')
        except Exception as error:
            logger.warning('Appointment start reminder could not be sent (%s).', type(error).__name__)
            failed = True
            continue
        AppointmentStartReminder.objects.create(
            company_id=appointment.company_id, appointment=appointment,
            starts_at=appointment.starts_at, recipient=user, sent_at=timezone.now(),
        )
        log_delivery('appointment_reminder', appointment, user, message.subject)
        delivered = True
    return delivered, failed


def _active_staff(company_id):
    return CompanyMembership.objects.filter(user_id=OuterRef('user_id'), company_id=company_id, is_active=True,
                                            user__is_active=True)


def _patient_login(patient):
    """The patient's login when they can sign in to see what the email is about."""
    user = patient.user if patient.user_id else None
    return user if user is not None and user.is_active and patient.is_active and user.email else None


def _first_emailed_since_read(links, sent_at):
    """Of these participant or member rows, the ones to email: never emailed, or they've read it since. Each is
    marked emailed as it's chosen, so a message sent at the same moment can't email them twice."""
    chosen = []
    for pk, user_id in links.values_list('pk', 'user_id'):
        if links.model.objects.filter(pk=pk).filter(
            Q(last_emailed_at__isnull=True) | Q(last_read_at__gte=F('last_emailed_at')),
        ).update(last_emailed_at=sent_at):
            chosen.append(user_id)
    return chosen


# New message in a patient conversation

def _patient_message(pk):
    return PatientMessage.objects.select_related('thread__patient__user', 'thread__company', 'sender').get(pk=pk)


def _patient_message_recipients(message, actor_id):
    thread = message.thread
    chosen = []
    patient_user = _patient_login(thread.patient)
    if patient_user is not None and message.sender_id != patient_user.pk:
        # The patient has opened it since when the message we last emailed about is read.
        unread_emailed = PatientMessage.objects.filter(
            thread_id=OuterRef('pk'), created_at=OuterRef('patient_emailed_at'), read_at__isnull=True,
        ).exclude(sender_id=patient_user.pk)
        if MessageThread.objects.filter(pk=thread.pk).filter(
            Q(patient_emailed_at__isnull=True) | ~Exists(unread_emailed),
        ).update(patient_emailed_at=message.created_at):
            chosen.append(patient_user.pk)
    staff = MessageThreadParticipant.objects.filter(thread=thread).exclude(user_id=message.sender_id).filter(
        Exists(_active_staff(thread.company_id)))
    return chosen + _first_emailed_since_read(staff, message.created_at)


def _patient_message_email(message, user, actor_id):
    thread = message.thread
    sender = person_name(message.sender) if message.sender else 'someone'
    if user.pk == thread.patient.user_id:
        return build_email(to=user.email, subject='New message from Meridian Health', template='patient_message', context={
            'greeting': user.first_name, 'sender': sender if message.sender else 'your care team',
            'url': site_link('portal:patient-messages', query=f'thread={thread.pk}', fragment='patient-conversation'),
        })
    patient = str(thread.patient)
    from_patient = message.sender_id is not None and message.sender_id == thread.patient.user_id
    return build_email(
        to=user.email, template='staff_message',
        subject=f'New message from {patient}' if from_patient else f'New message in your conversation with {patient}',
        context={'greeting': greeting_name(user), 'sender': sender, 'patient': patient, 'from_patient': from_patient,
                 'url': site_link('portal:staff-inbox', query=f'thread={thread.pk}')},
    )


# New message in a team conversation

def _team_message(pk):
    return TeamMessage.objects.select_related('thread', 'sender').get(pk=pk)


def _team_message_recipients(message, actor_id):
    members = TeamThreadMember.objects.filter(thread_id=message.thread_id).exclude(user_id=message.sender_id).filter(
        Exists(_active_staff(message.thread.company_id)))
    return _first_emailed_since_read(members, message.created_at)


def _team_message_email(message, user, actor_id):
    sender = person_name(message.sender) if message.sender else 'A colleague'
    return build_email(to=user.email, subject=f'New team message from {sender}', template='team_message', context={
        'greeting': greeting_name(user), 'sender': sender,
        'url': site_link('portal:team-inbox', query=f'thread={message.thread_id}'),
    })


# Appointments: confirmed (booked or moved) and reminders

def _appointment(pk):
    return Appointment.objects.select_related('patient__user', 'clinician', 'company').get(pk=pk)


def _appointment_people(appointment, actor_id):
    if appointment.status != Appointment.Status.BOOKED:
        return []
    patient_user = _patient_login(appointment.patient)
    chosen = [patient_user.pk] if patient_user is not None else []
    if appointment.clinician_id != actor_id and CompanyMembership.objects.filter(
            user_id=appointment.clinician_id, company_id=appointment.company_id, is_active=True,
            user__is_active=True).exists():
        chosen.append(appointment.clinician_id)
    return chosen


def _cancellation_recipients(appointment, actor_id):
    if appointment.status != Appointment.Status.CANCELLED:
        return []
    patient_user = _patient_login(appointment.patient)
    chosen = [patient_user.pk] if patient_user is not None else []
    if appointment.clinician_id != actor_id and CompanyMembership.objects.filter(
            user_id=appointment.clinician_id, company_id=appointment.company_id, is_active=True,
            user__is_active=True).exists():
        chosen.append(appointment.clinician_id)
    return chosen


def _cancellation_email(appointment, user, actor_id):
    when = _when(appointment)
    by_patient = actor_id is not None and actor_id == appointment.patient.user_id
    if user.pk == appointment.patient.user_id:
        return build_email(to=user.email, subject=f'Appointment cancelled: {when}', template='appointment_cancelled', context={
            'greeting': user.first_name, 'by_patient': by_patient,
            'reason': '' if by_patient else appointment.cancellation_reason,
            'details': appointment_details(appointment, clinician=person_name(appointment.clinician)),
            'url': site_link('portal:patient-appointments'),
        })
    patient = str(appointment.patient)
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    cancelled_by = '' if actor is None else (f'{patient}, the patient' if by_patient else person_name(actor))
    starts = timezone.localtime(appointment.starts_at)
    return build_email(to=user.email, subject=f'Appointment cancelled: {patient} on {when}',
                       template='staff_appointment_cancelled', context={
                           'greeting': greeting_name(user), 'cancelled_by': cancelled_by,
                           'details': appointment_details(appointment, patient=patient),
                           'url': site_link('portal:staff-schedule', query=f'view=table&date={starts:%Y-%m-%d}'),
                       })


def _confirmation_recipients(appointment, actor_id):
    # Booked within the reminder time, the confirmation is the reminder.
    Appointment.objects.filter(
        pk=appointment.pk, reminder_sent_at__isnull=True,
        starts_at__lte=timezone.now() + timedelta(hours=settings.APPOINTMENT_REMINDER_HOURS),
    ).update(reminder_sent_at=timezone.now())
    return _appointment_people(appointment, actor_id)


def _when(appointment):
    starts = timezone.localtime(appointment.starts_at)
    return f'{starts:%A} {starts.day} {starts:%B} at {starts:%H:%M}'


def _appointment_email(appointment, user, *, moved=False, reminder=False, starting=False, actor_id=None):
    when = _when(appointment)
    if user.pk == appointment.patient.user_id:
        if reminder:
            subject = f'{"Starting soon" if starting else "Reminder"}: your appointment on {when}'
        else:
            subject = f'Your appointment has moved to {when}' if moved else f'Appointment confirmed: {when}'
        return build_email(
            to=user.email, subject=subject, template='appointment_reminder' if reminder else 'appointment_confirmed',
            context={'greeting': user.first_name, 'moved': moved,
                     'details': appointment_details(appointment, clinician=person_name(appointment.clinician)),
                     'url': site_link('portal:patient-appointment-detail', appointment.pk) if reminder
                     else site_link('portal:patient-appointments')},
        )
    patient = str(appointment.patient)
    if reminder:
        subject = f'{"Starting soon" if starting else "Reminder"}: {patient} on {when}'
    else:
        subject = f'Appointment moved: {patient} on {when}' if moved else f'New booking: {patient} on {when}'
    booked_by = ''
    if actor_id and not moved and not reminder:
        actor = get_user_model().objects.filter(pk=actor_id).first()
        if actor is not None:
            booked_by = f'{patient}, the patient' if actor.pk == appointment.patient.user_id else person_name(actor)
    starts = timezone.localtime(appointment.starts_at)
    return build_email(
        to=user.email, subject=subject,
        template='staff_appointment_reminder' if reminder else 'staff_appointment_confirmed',
        context={'greeting': greeting_name(user), 'moved': moved, 'booked_by': booked_by,
                 'details': appointment_details(appointment, patient=patient),
                 'url': site_link('portal:appointment-detail', appointment.pk) if reminder
                 else site_link('portal:staff-schedule', query=f'view=table&date={starts:%Y-%m-%d}')},
    )


# Welcome, for a patient's new account

def _user(pk):
    return get_user_model().objects.get(pk=pk)


def _welcome_recipients(user, actor_id):
    return [user.pk] if user.is_active and user.email else []


def _welcome_email(user, recipient, actor_id):
    return build_email(to=recipient.email, subject='Welcome to Meridian Health', template='welcome', context={
        'greeting': recipient.first_name, 'email': recipient.email, 'url': site_link('portal:patient-dashboard'),
    })


# Win-back, once, after a patient cancels their subscription

def _subscription(pk):
    return PatientSubscription.objects.select_related('patient__user').get(pk=pk)


def _winback_recipients(subscription, actor_id):
    user = subscription.patient.user
    return [user.pk] if user is not None and user.is_active and user.email and subscription.patient.is_active else []


def _winback_email(subscription, user, actor_id):
    return build_email(to=user.email, subject='Can we help before you go?', template='subscription_winback', context={
        'greeting': subscription.patient.first_name,
        'url': site_link('portal:patient-messages', query='compose=1&subject=Before+I+go'),
    })


# Billing: a failed debit, a debit order to approve again

def _cycle(pk):
    return SubscriptionCycle.objects.select_related('patient__user', 'subscription').get(pk=pk)


def _mandate(pk):
    return DebitMandate.objects.select_related('patient__user').get(pk=pk)


def _patient_only(obj, actor_id):
    user = _patient_login(obj.patient)
    return [user.pk] if user is not None else []


def _payment_failed_email(cycle, user, actor_id):
    from .billing import money

    return build_email(to=user.email, subject='We couldn’t collect your payment', template='payment_failed', context={
        'greeting': cycle.patient.first_name, 'amount': money(cycle.amount), 'retry_on': cycle.next_attempt_on,
        'final': cycle.status == SubscriptionCycle.Status.FAILED, 'url': site_link('portal:patient-billing'),
    })


def _debit_order_update_email(mandate, user, actor_id):
    from .billing import mandate_amount, money

    needed = mandate_amount(mandate.subscription) if mandate.subscription_id else None
    return build_email(to=user.email, subject='Please approve your new debit order', template='debit_order_update', context={
        'greeting': mandate.patient.first_name, 'amount': money(needed) if needed else '',
        'weeks': mandate.interval_weeks, 'url': site_link('portal:patient-billing'),
    })


def _debit_order_request_email(subscription, user, actor_id):
    return build_email(to=user.email, subject='Please set up your debit order again', template='debit_order_request',
                       context={'greeting': subscription.patient.first_name, 'url': site_link('portal:patient-billing')})


# kind: (load the object, who to email, build one person's email)
KINDS = {
    'patient_message': (_patient_message, _patient_message_recipients, _patient_message_email),
    'team_message': (_team_message, _team_message_recipients, _team_message_email),
    'appointment_booked': (_appointment, _confirmation_recipients,
                           lambda appointment, user, actor_id: _appointment_email(appointment, user, actor_id=actor_id)),
    'appointment_moved': (_appointment, _confirmation_recipients,
                          lambda appointment, user, actor_id: _appointment_email(appointment, user, moved=True)),
    'appointment_cancelled': (_appointment, _cancellation_recipients, _cancellation_email),
    'appointment_reminder': (_appointment, lambda appointment, actor_id: _appointment_people(appointment, None),
                             lambda appointment, user, actor_id: _appointment_email(appointment, user, reminder=True)),
    'welcome': (_user, _welcome_recipients, _welcome_email),
    'subscription_winback': (_subscription, _winback_recipients, _winback_email),
    'payment_failed': (_cycle, _patient_only, _payment_failed_email),
    'debit_order_update': (_mandate, _patient_only, _debit_order_update_email),
    'debit_order_request': (_subscription, _patient_only, _debit_order_request_email),
}
