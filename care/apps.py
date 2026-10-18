from django.apps import AppConfig


class CareConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'care'

    def ready(self):
        from . import realtime  # noqa: F401 — register message change signals
