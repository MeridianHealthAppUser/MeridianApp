"""Background tasks, such as emailing a PathCare form, run by a Celery worker.

Without a broker (local development and tests) tasks run straight away in the web process.
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
