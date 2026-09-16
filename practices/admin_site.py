from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.sites import AdminSite

class ScopedAdminSite(AdminSite):
    def get_log_entries(self, request):
        from .tenancy import multi_practice_enabled
        entries = super().get_log_entries(request)
        # Historical log entries retain unscoped object_repr text, including
        # deleted objects whose original company can no longer be resolved.
        return entries if multi_practice_enabled() else entries.none()


class ScopedAdminConfig(AdminConfig):
    default_site = 'practices.admin_site.ScopedAdminSite'
