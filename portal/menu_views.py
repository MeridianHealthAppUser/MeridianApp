"""A page per menu section, so the mobile bar can show only the top-level sections."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from practices.tenancy import multi_practice_enabled
from .navigation import build_staff_menu
from .views import StaffCompanyRequiredMixin


@method_decorator(never_cache, name='dispatch')
class StaffMenuSectionView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, section):
        nav_section = f'section:{section}'
        menu = build_staff_menu(self.membership, multi_practice=multi_practice_enabled(), nav_section=nav_section)
        current = next((item for item in menu if item['key'] == section), None)
        if current is None:
            raise Http404('This menu section is not available to you.')
        return render(request, 'portal/staff_menu_section.html', dict(
            company=self.company, active_membership=self.membership, nav_section=nav_section,
            page_title=current['label'], section=current))
