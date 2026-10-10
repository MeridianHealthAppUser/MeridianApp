"""Celery tasks."""

from celery import shared_task


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
