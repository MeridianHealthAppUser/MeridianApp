"""The email journeys: six tracks of emails that follow a person through their care (contract section 3.3).

1. Completed screening but did not book (an enquiry that hasn't become a patient).
2. Had a consultation (a first consultation done, no subscription started yet).
3. Treatment months 1 to 6 (from the day the subscription starts): what to expect, recaps.
4. Treatment month 5 onwards (from day 120): bloods, follow-up reviews, weight check-ins.
5. Cancelled subscription (from the day it's cancelled, until they start again).
6. Failed subscription payment (from a debit that didn't go through, until it's paid).

Someone is in a track while its condition holds, so they move between tracks as their care changes: booking
takes them out of 1, starting a subscription out of 2 and into 3, cancelling into 5, a payment into or out of 6.
Each email of a track (EmailJourney) goes delay_days after they joined it, once for that time in the track, and only
while they are still in it. `manage.py send_journey_emails` sends what's due, once a day.

The emails start switched off with placeholder wording (default_journeys): a Super Admin rewrites and turns each on
under Settings > Email journeys. Every journey email carries an unsubscribe link (journey emails stop; emails about
payments, appointments and messages still go). A step whose day passed more than CATCH_UP_DAYS ago isn't sent, so
switching a track on doesn't send a backlog. Emails link into the app and never carry clinical detail.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from practices.models import Patient

from .emails import build_email, site_link
from .models import Appointment, EmailDelivery, EmailJourney, Lead, PatientCommunicationPreference, PatientSubscription, SubscriptionCycle

logger = logging.getLogger(__name__)
Track = EmailJourney.Track
CATCH_UP_DAYS = 3
ONGOING_FROM_DAY = 120
EARLY_UNTIL_DAY = 182
SCREENED_FOR_DAYS = 90
UNSUBSCRIBE_SALT = 'care.journeys.unsubscribe'
# Emails about money owed still go to someone who unsubscribed from the journeys.
SERVICE_TRACKS = (Track.PAYMENT_FAILED,)

BUTTONS = {
    EmailJourney.Button.QUESTIONNAIRE: lambda: site_link('questionnaire'),
    EmailJourney.Button.BOOK: lambda: site_link('portal:patient-appointments', query='book=1'),
    EmailJourney.Button.SUBSCRIPTION: lambda: site_link('portal:patient-treatment', fragment='start-subscription'),
    EmailJourney.Button.WEIGHT: lambda: site_link('portal:patient-dashboard', fragment='weight-log'),
    EmailJourney.Button.MEDICAL: lambda: site_link('portal:patient-medical-profile'),
    EmailJourney.Button.BLOODS: lambda: site_link('portal:patient-labs'),
    EmailJourney.Button.TREATMENT: lambda: site_link('portal:patient-treatment'),
    EmailJourney.Button.BILLING: lambda: site_link('portal:patient-billing'),
    EmailJourney.Button.MESSAGES: lambda: site_link('portal:patient-messages'),
}


@dataclass(frozen=True)
class Member:
    """Someone in a track: the patient (or the enquiry, before there is one), when they joined, and which time."""

    track: str
    joined_on: object
    entry_key: str
    email: str
    first_name: str
    patient: Patient = None
    lead: Lead = None


def _patient_member(track, patient, joined_on, entry_key):
    user = patient.user if patient.user_id else None
    if user is None or not user.is_active or not user.email or not patient.is_active:
        return None
    return Member(track, joined_on, entry_key, user.email, patient.first_name, patient=patient)


def members(company, today=None):
    """Everyone in each track today."""
    today = today or timezone.localdate()
    found = []
    # 1. Screened, not booked: enquiries still open, for SCREENED_FOR_DAYS.
    # Only enquiries that passed screening: one referred for review, or waiting for it, isn't told to book.
    for lead in Lead.objects.filter(company=company, converted_patient__isnull=True, emails_unsubscribed_at__isnull=True,
                                    screening_status=Lead.ScreeningStatus.CLEARED,
                                    created_at__date__gte=today - timedelta(days=SCREENED_FOR_DAYS)).exclude(
            stage__in=(Lead.Stage.CONVERTED, Lead.Stage.CLOSED)):
        found.append(Member(Track.SCREENED, timezone.localdate(lead.created_at), f'lead-{lead.pk}', lead.email, lead.first_name, lead=lead))
    subscriptions = PatientSubscription.objects.filter(company=company, patient__is_active=True).select_related('patient__user')
    started = {plan.patient_id for plan in subscriptions.exclude(status=PatientSubscription.Status.CANCELLED)}
    # 2. Had a consultation: their first consultation done, and no subscription yet.
    consulted = set()
    for appointment in Appointment.objects.filter(company=company, status=Appointment.Status.COMPLETED,
                                                  appointment_type=Appointment.Type.INITIAL).exclude(
            patient_id__in=started).select_related('patient__user').order_by('patient_id', 'starts_at'):
        if appointment.patient_id in consulted:
            continue
        consulted.add(appointment.patient_id)
        member = _patient_member(Track.CONSULTED, appointment.patient, timezone.localdate(appointment.starts_at), f'consult-{appointment.pk}')
        if member:
            found.append(member)
    for plan in subscriptions:
        if plan.status in (PatientSubscription.Status.ACTIVE, PatientSubscription.Status.PAYMENT_RETRY, PatientSubscription.Status.PAUSED):
            # 3 and 4. Treatment: months 1 to 6 from the start, and month 5 onwards.
            if (today - plan.starts_on).days < EARLY_UNTIL_DAY:
                member = _patient_member(Track.EARLY, plan.patient, plan.starts_on, f'subscription-{plan.pk}')
                if member:
                    found.append(member)
            if (today - plan.starts_on).days >= ONGOING_FROM_DAY and plan.status != PatientSubscription.Status.PAUSED:
                member = _patient_member(Track.ONGOING, plan.patient, plan.starts_on + timedelta(days=ONGOING_FROM_DAY), f'subscription-{plan.pk}')
                if member:
                    found.append(member)
        elif plan.status == PatientSubscription.Status.CANCELLED and plan.cancelled_at and plan.patient_id not in started:
            # 5. Cancelled, until they start a new subscription.
            member = _patient_member(Track.CANCELLED, plan.patient, timezone.localdate(plan.cancelled_at), f'subscription-{plan.pk}')
            if member:
                found.append(member)
    # 6. Failed payment: a debit that didn't go through and isn't paid yet.
    for cycle in SubscriptionCycle.objects.filter(company=company, attempts__gt=0, status__in=(
            SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)).select_related('patient__user'):
        member = _patient_member(Track.PAYMENT_FAILED, cycle.patient, cycle.due_on, f'cycle-{cycle.pk}')
        if member:
            found.append(member)
    return found


def _unsubscribed(member):
    if member.track in SERVICE_TRACKS:
        return False
    if member.lead is not None:
        return member.lead.emails_unsubscribed_at is not None
    return PatientCommunicationPreference.objects.filter(patient=member.patient, journey_emails_off_at__isnull=False).exists()


def unsubscribe_token(member_or_record):
    record = member_or_record.patient or member_or_record.lead if isinstance(member_or_record, Member) else member_or_record
    kind = 'patient' if isinstance(record, Patient) else 'lead'
    return signing.dumps({'kind': kind, 'id': record.pk, 'company': record.company_id}, salt=UNSUBSCRIBE_SALT)


def unsubscribe_url(member):
    # A preview is for no one in particular, so its link goes nowhere.
    if member.patient is None and member.lead is None:
        return site_link('portal:email-unsubscribe', 'preview')
    return site_link('portal:email-unsubscribe', unsubscribe_token(member))


def render(journey, member):
    """The email for one person: the message with their first name in it, a button into the app, and (except for
    payment emails) the unsubscribe link, also as a List-Unsubscribe header for one-click email apps."""
    body = journey.body_template.replace('{first_name}', member.first_name or 'there')
    button = BUTTONS.get(journey.button)
    unsubscribe = None if journey.track in SERVICE_TRACKS else unsubscribe_url(member)
    message = build_email(to=member.email, subject=journey.subject_template.replace('{first_name}', member.first_name or ''),
                          template='journey', context={
                              'greeting': member.first_name, 'paragraphs': [part.strip() for part in body.split('\n\n') if part.strip()],
                              'url': button() if button else '', 'label': journey.get_button_display(),
                              'unsubscribe_url': unsubscribe})
    if unsubscribe:
        message.extra_headers.update({'List-Unsubscribe': f'<{unsubscribe}>', 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
    return message


def due(company, today=None):
    """(journey, member) for every journey email due today and not sent yet."""
    today = today or timezone.localdate()
    steps = {}
    for journey in EmailJourney.objects.filter(company=company, is_active=True):
        steps.setdefault(journey.track, []).append(journey)
    if not steps:
        return []
    pending = []
    for member in members(company, today):
        for journey in steps.get(member.track, ()):
            send_on = member.joined_on + timedelta(days=journey.delay_days)
            if not (today - timedelta(days=CATCH_UP_DAYS) <= send_on <= today) or _unsubscribed(member):
                continue
            sent = EmailDelivery.objects.filter(journey=journey, entry_key=member.entry_key)
            sent = sent.filter(patient=member.patient) if member.patient else sent.filter(lead=member.lead, patient__isnull=True)
            if not sent.exists():
                pending.append((journey, member))
    return pending


def send_due(company, today=None):
    """Send today's journey emails for a practice and log each against the person. Returns how many were sent."""
    if not settings.EMAIL_ENABLED:
        return 0
    count = 0
    for journey, member in due(company, today):
        message = render(journey, member)
        try:
            with transaction.atomic():
                # The log row is claimed first, so two runs at once can't send twice.
                delivery = EmailDelivery.objects.create(
                    company=company, journey=journey, patient=member.patient, lead=member.lead, recipient_email=member.email,
                    kind='journey', subject=message.subject[:255], entry_key=member.entry_key, status='sending')
        except IntegrityError:
            continue
        try:
            message.send(fail_silently=False)
        except Exception as error:  # The mail server refused it: the row stays, marked failed, and it isn't retried.
            logger.warning('Journey email %s could not be sent (%s).', journey.pk, type(error).__name__)
            EmailDelivery.objects.filter(pk=delivery.pk).update(status='failed')
            continue
        EmailDelivery.objects.filter(pk=delivery.pk).update(status='sent', sent_at=timezone.now())
        count += 1
    return count


@transaction.atomic
def unsubscribe(token):
    """Stop journey emails for whoever the token is for. Returns the patient or enquiry, or None for a bad link."""
    try:
        data = signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None
    now = timezone.now()
    if data.get('kind') == 'lead':
        lead = Lead.objects.filter(pk=data.get('id'), company_id=data.get('company')).first()
        if lead is not None and lead.emails_unsubscribed_at is None:
            lead.emails_unsubscribed_at = now
            lead.save(update_fields=('emails_unsubscribed_at', 'updated_at'))
        return lead
    patient = Patient.objects.filter(pk=data.get('id'), company_id=data.get('company')).first()
    if patient is not None:
        preference, _ = PatientCommunicationPreference.objects.get_or_create(company_id=patient.company_id, patient=patient)
        if preference.journey_emails_off_at is None:
            preference.journey_emails_off_at = now
            preference.save(update_fields=('journey_emails_off_at', 'updated_at'))
    return patient


# The wording each practice starts with: placeholders for the practice to rewrite, all switched off.
DEFAULT_JOURNEYS = (
    (Track.SCREENED, 1, 'Your screening is done: book your consultation', 'Your consultation is waiting, {first_name}',
     'Thank you for completing your screening with us.\n\nThe next step is a 30-minute video consultation with one of our '
     'doctors, who will talk through whether treatment is right for you. You can book a time that suits you.', 'questionnaire'),
    (Track.SCREENED, 5, 'Screening follow-up: questions answered', 'Any questions before your consultation?',
     'Booking a consultation doesn’t commit you to treatment. It’s a conversation with a doctor about your goals and '
     'whether medication is right for you.\n\nIf you’d rather talk to us first, reply through the questionnaire’s '
     '“Please call me” option and we’ll phone you.', 'questionnaire'),
    (Track.CONSULTED, 1, 'After your consultation: start your subscription', 'Your next step, {first_name}',
     'Thank you for your consultation. If your doctor prescribed treatment, start your subscription so your first parcel '
     'can be sent.', 'subscription'),
    (Track.CONSULTED, 7, 'After your consultation: a reminder', 'Still thinking it over?',
     'Your prescription is ready when you are. If anything from your consultation is unclear, message your care team.', 'messages'),
    (Track.EARLY, 0, 'Month 1: what to expect', 'Welcome to your treatment, {first_name}',
     'Your first weeks are on a starter dose, so your body can get used to it. Little change on the scale is normal at '
     'first.\n\nLog your weight once a week: it goes onto the chart your doctor reads at your follow-ups.', 'weight'),
    (Track.EARLY, 28, 'Month 2: your dose steps up', 'Your first month is done',
     'Well done on your first month. Your dose may step up now, as your doctor planned. If you have any side effects '
     'you’re worried about, message your care team.', 'treatment'),
    (Track.EARLY, 84, 'Month 3: your recap', 'Three months in, {first_name}',
     'Three months in: take a look at your progress chart, and log this week’s weight.', 'weight'),
    (Track.ONGOING, 0, 'Month 5: blood tests and your review', 'Your six-month review is coming up',
     'Your six-month review is coming up. Your doctor may ask for blood tests before it, and booking your follow-up now '
     'keeps your treatment running without a gap.', 'book'),
    (Track.ONGOING, 30, 'Monthly check-in: weight and medical information', 'Your monthly check-in',
     'Please log your weight, and check that your medical information is still up to date.', 'medical'),
    (Track.CANCELLED, 14, 'After cancelling: we’re here if you need us', 'How are you getting on, {first_name}?',
     'You cancelled your subscription a couple of weeks ago. If you’d like to talk about a different dose or medicine, '
     'or start again, your care team is here.', 'messages'),
    (Track.PAYMENT_FAILED, 3, 'Failed payment: reminder', 'Your payment is still outstanding',
     'We still couldn’t collect your subscription payment, so your next parcel is waiting. Update your payment details, '
     'or pay now by card.', 'billing'),
)


def default_journeys(company):
    """Add any of the default journey emails the practice doesn't have yet (by name), switched off. A practice's own
    edits are never overwritten. Returns how many were added."""
    existing = set(EmailJourney.objects.filter(company=company).values_list('name', flat=True))
    missing = [EmailJourney(company=company, track=track, delay_days=days, name=name, subject_template=subject,
                            body_template=body, button=button, is_active=False)
               for track, days, name, subject, body, button in DEFAULT_JOURNEYS if name not in existing]
    EmailJourney.objects.bulk_create(missing)
    return len(missing)
