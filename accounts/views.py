from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeDoneView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views import View

from .forms import AccountProfileForm, EmailAuthenticationForm
from .profile import make_profile_context, save_own_profile


@method_decorator(never_cache, name='dispatch')
class AccountProfileView(LoginRequiredMixin, View):
    """An account-level page, never an administrator's editor for another person."""

    http_method_names = ('get', 'post', 'head', 'options')

    def display(self, request, form=None):
        from practices.models import Company, CompanyMembership, Patient

        user = get_object_or_404(get_user_model(), pk=request.user.pk, is_active=True)
        memberships = CompanyMembership.objects.filter(user=user, is_active=True, company__is_active=True)
        patients = Patient.objects.filter(user=user, is_active=True, company__is_active=True)
        has_staff_access = memberships.exists()
        has_patient_access = patients.exists()
        companies = Company.objects.filter(is_active=True).filter(
            Q(memberships__in=memberships) | Q(practices_patient_records__in=patients)
        ).distinct().order_by('name', 'pk').prefetch_related(
            Prefetch('memberships', queryset=memberships, to_attr='profile_memberships'),
            Prefetch('practices_patient_records', queryset=patients, to_attr='profile_patients'),
        )
        page = Paginator(companies, 20).get_page(request.GET.get('page'))
        return render(request, 'accounts/profile.html', {
            'form': form if form is not None else AccountProfileForm(instance=user),
            'profile_user': user,
            'profile_context': request.POST.get('profile_context', '') if request.method == 'POST' else make_profile_context(user),
            'membership_page': page,
            'has_staff_access': has_staff_access,
            'has_patient_access': has_patient_access,
            'is_patient_portal': has_patient_access and not has_staff_access,
        })

    def get(self, request):
        return self.display(request)

    def post(self, request):
        user = get_object_or_404(get_user_model(), pk=request.user.pk, is_active=True)
        form = AccountProfileForm(request.POST, instance=user)
        if form.is_valid():
            try:
                _, changed = save_own_profile(actor=request.user, context_token=request.POST.get('profile_context'),
                    first_name=form.cleaned_data['first_name'], last_name=form.cleaned_data['last_name'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Your profile has been updated.' if changed else 'Your profile is already up to date.')
                return redirect('accounts:profile')
        return self.display(request, form)


class AccountLoginView(LoginView):
    """Sign a person into the shared Meridian account for all their practices."""

    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True
    template_name = 'accounts/login.html'

    def get_default_redirect_url(self):
        return reverse_lazy('portal:desktop-dashboard')

    def form_valid(self, form):
        """Use a browser-session cookie when a person opts out of persistence."""
        response = super().form_valid(form)
        if not form.cleaned_data.get('remember_me'):
            self.request.session.set_expiry(0)
        return response


class AccountLogoutView(LogoutView):
    """End the session and return to the public Meridian site."""

    next_page = reverse_lazy('landing')


@method_decorator(never_cache, name='dispatch')
class AccountPasswordChangeView(PasswordChangeView):
    """A person changes only their own shared password, after proving the old one."""

    template_name = 'accounts/password_change.html'
    success_url = reverse_lazy('accounts:password-change-done')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Bound POST checks the current stored password under a user lock, not a
        # potentially stale request.user loaded before another tab saved a change.
        if self.request.method == 'POST':
            kwargs['user'] = get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
        return kwargs

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        from care.services import record_audit
        from practices.models import Company

        response = super().form_valid(form)
        companies = Company.objects.filter(is_active=True).filter(
            Q(memberships__user=self.request.user, memberships__is_active=True) |
            Q(practices_patient_records__user=self.request.user, practices_patient_records__is_active=True)
        ).distinct()
        for company in companies:
            record_audit(company=company, actor=self.request.user, action='account.password_changed',
                         target=self.request.user, request=self.request)
        return response


@method_decorator(never_cache, name='dispatch')
class AccountPasswordChangeDoneView(LoginRequiredMixin, PasswordChangeDoneView):
    template_name = 'accounts/password_change_done.html'
