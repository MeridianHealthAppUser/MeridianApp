"""Celery tasks."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def send_pathcare_form_email(self, form_id):
    """Email a PathCare form to the patient, retrying a failed send three times before marking it failed."""
    from .pathcare import deliver, mark_failed

    try:
        deliver(form_id)
    except Exception as error:  # Any mail-server or connection error.
        if self.request.retries < self.max_retries and not self.request.is_eager:
            raise self.retry(exc=error)
        mark_failed(form_id)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def send_notification(self, kind, object_id, recipient_id=None, actor_id=None):
    """Without a recipient, work out who to email and send each their own (care/notifications.py); with one, send
    that person's email, retrying a failed send three times. A failure is logged and never reaches the page."""
    from .notifications import deliver, recipients

    try:
        if recipient_id is None:
            for user_id in recipients(kind, object_id, actor_id):
                send_notification.delay(kind, object_id, recipient_id=user_id, actor_id=actor_id)
        else:
            deliver(kind, object_id, recipient_id, actor_id)
    except Exception as error:  # Any mail-server or connection error, or the object is gone.
        if self.request.retries < self.max_retries and not self.request.is_eager:
            raise self.retry(exc=error)
        logger.exception('The %s email about %s was not sent.', kind, object_id)
