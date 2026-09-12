from django.core.exceptions import PermissionDenied

from .services import get_active_company


class ActiveCompanyRequiredMixin:
    """Attach the active company to tenant-aware views before querying their data."""

    company = None

    def dispatch(self, request, *args, **kwargs):
        self.company = get_active_company(request)
        if self.company is None:
            raise PermissionDenied('Select a company before accessing company data.')
        return super().dispatch(request, *args, **kwargs)
