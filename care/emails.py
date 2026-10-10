"""Meridian's emails: one look for all of them (templates/emails/base.html and base.txt).

Each email is a plain-text part and an HTML part from templates/emails/<name>.txt and .html. The HTML part has
the Meridian logo under the signature and a smaller "Please do not reply" line pointing to support. Emails never
include message text or clinical details: a button takes the reader into the app instead.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone


def site_link(name, *args, query='', fragment=''):
    """An absolute link into the app for an email, which is read outside the app."""
    return f"{settings.SITE_URL}{reverse(name, args=args)}{f'?{query}' if query else ''}{f'#{fragment}' if fragment else ''}"


def person_name(user):
    """Staff as "Dr Joshua Czech": their title, when they have one, and full name."""
    return user.titled_name


def greeting_name(user):
    """How an email to staff greets them: title and surname ("Dr Czech") when they have both, else their first name."""
    if getattr(user, 'title', '') and user.last_name.strip():
        return f'{user.get_title_display()} {user.last_name.strip()}'
    return user.first_name


def appointment_details(appointment, *, patient=None, clinician=None):
    """The rows of an appointment's details box, in the practice's time (SAST)."""
    starts = timezone.localtime(appointment.starts_at)
    rows = [('Patient', patient)] if patient else []
    rows += [
        ('Appointment', appointment.get_appointment_type_display()),
        ('Date', f'{starts:%A} {starts.day} {starts:%B %Y}'),
        ('Time', f'{starts:%H:%M} {starts.tzname()} · {appointment.duration_minutes} minutes'),
    ]
    if clinician:
        rows.append(('With', clinician))
    return rows


def build_email(*, to, subject, template, context=None, site_url=None):
    """The email, ready to send: plain text with an HTML alternative."""
    subject = ' '.join(subject.split())  # Names in it are typed by people; a line break would stop the send.
    context = {'subject': subject, 'site_url': site_url or settings.SITE_URL, 'support_email': settings.SUPPORT_EMAIL,
               **(context or {})}
    message = EmailMultiAlternatives(subject=subject, body=render_to_string(f'emails/{template}.txt', context),
                                     from_email=settings.DEFAULT_FROM_EMAIL, to=[to])
    message.attach_alternative(render_to_string(f'emails/{template}.html', context), 'text/html')
    return message
