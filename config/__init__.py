# Load the Celery app with Django so tasks are registered for .delay().
from .celery import app as celery_app

__all__ = ('celery_app',)
