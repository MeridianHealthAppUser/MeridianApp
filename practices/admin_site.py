from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.sites import AdminSite


class ScopedAdminSite(AdminSite):
    site_header = 'Meridian Health'
    site_title = 'Meridian Health Admin'
    index_title = 'Practice overview'
    index_template = 'admin/meridian_index.html'

    def index(self, request, extra_context=None):
        from .admin_dashboard import build_admin_dashboard

        context = {**(extra_context or {}), 'meridian_dashboard': build_admin_dashboard(self, request)}
        return super().index(request, extra_context=context)

    def get_log_entries(self, request):
        from .tenancy import multi_practice_enabled
        entries = super().get_log_entries(request)
        # Historical log entries retain unscoped object_repr text, including
        # deleted objects whose original company can no longer be resolved.
        return entries if multi_practice_enabled() else entries.none()


class ScopedAdminConfig(AdminConfig):
    default_site = 'practices.admin_site.ScopedAdminSite'
