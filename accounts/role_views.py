"""Explicit own-account role switching for the single-practice deployment."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from practices.role_switching import switch_own_practice_role


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
class PracticeRoleView(View):
    http_method_names = ('post',)

    def post(self, request):
        # There is deliberately no user ID, company ID or redirect target to
        # trust. Reject forged/ambiguous fields rather than accepting them.
        if set(request.POST) - {'role', 'csrfmiddlewaretoken'} or len(request.POST.getlist('role')) != 1:
            return HttpResponseBadRequest('Submit one supported practice role.')
        try:
            membership = switch_own_practice_role(
                actor=request.user, role=request.POST['role'], request=request,
            )
        except ValidationError:
            return HttpResponseBadRequest('Choose a supported practice role.')
        messages.success(
            request,
            f'Your practice role is now {membership.get_role_display()}. '
            'This applies across all open sessions. You remain signed in as the same person.',
        )
        return redirect('portal:desktop-dashboard')
