from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from django.contrib.auth.views import (
    LoginView, LogoutView, PasswordChangeDoneView, PasswordChangeView, PasswordResetCompleteView,
    PasswordResetConfirmView, PasswordResetDoneView, PasswordResetView,
)
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views import View

from practices.tenancy import enabled_companies, scope_queryset

from .forms import AccountPasswordResetForm, AccountProfileForm, EmailAuthenticationForm
from .profile import make_profile_context, save_own_profile


@method_decorator(never_cache, name='dispatch')
class AccountProfileView(LoginRequiredMixin, View):
    """An account-level page, never an administrator's editor for another person."""

    http_method_names = ('get', 'post', 'head', 'options')

    def display(self, request, form=None):
        from practices.models import CompanyMembership, Patient

        user = get_object_or_404(get_user_model(), pk=request.user.pk, is_active=True)
        memberships = scope_queryset(CompanyMembership.objects.filter(user=user, is_active=True, company__is_active=True))
        patients = scope_queryset(Patient.objects.filter(user=user, is_active=True, company__is_active=True))
        has_staff_access = memberships.exists()
        has_patient_access = patients.exists()
        companies = enabled_companies().filter(
            Q(memberships__in=memberships) | Q(practices_patient_records__in=patients)
        ).distinct().order_by('name', 'pk').prefetch_related(
            Prefetch('memberships', queryset=memberships, to_attr='profile_memberships'),
            Prefetch('practices_patient_records', queryset=patients, to_attr='profile_patients'),
        )
        page = Paginator(companies, 20).get_page(request.GET.get('page'))
        # Staff details are kept by a Super Admin in Users and roles, so staff read them here and edit none.
        editable = {'profile_context': request.POST.get('profile_context', '') if request.method == 'POST' else make_profile_context(user)}
        if not has_staff_access:
            editable['form'] = form if form is not None else AccountProfileForm(instance=user)
        staff = staff_profile_context(request, user, memberships) if has_staff_access else {}
        return render(request, 'accounts/profile.html', {
            **staff,
            **editable,
            'profile_user': user,
            'membership_page': page,
            'has_staff_access': has_staff_access,
            'has_patient_access': has_patient_access,
        })

    def get(self, request):
        from practices.models import CompanyMembership, Patient

        # Patients have their own My profile in the patient app, with their details as the practice holds them.
        if (scope_queryset(Patient.objects.filter(user=request.user, is_active=True, company__is_active=True)).exists() and
                not scope_queryset(CompanyMembership.objects.filter(user=request.user, is_active=True, company__is_active=True)).exists()):
            return redirect('portal:patient-profile')
        return self.display(request)

    def post(self, request):
        from practices.models import CompanyMembership, Patient

        # Staff names are maintained centrally (Users and roles), and a patient's by their care team: neither changes them here.
        if any(scope_queryset(model.objects.filter(user=request.user, is_active=True, company__is_active=True)).exists()
               for model in (CompanyMembership, Patient)):
            raise PermissionDenied('Your details are maintained by the practice.')
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


def staff_profile_context(request, user, memberships):
    """My profile for staff: their details, the practice they're in, and a clinician's month so far."""
    from care.reporting import doctor_activity, provisional_total
    from practices.services import active_membership_for, get_active_company

    company = get_active_company(request)
    membership = active_membership_for(request, company) or memberships.select_related('company').first()
    is_clinician = any(member.is_clinician for member in memberships)
    month_to_date = None
    if membership is not None and membership.is_clinician:
        today = timezone.localdate()
        counts, _ = doctor_activity(company=membership.company, doctor=user, start=today.replace(day=1), end=today)
        total = provisional_total(company=membership.company, doctor=user, counts=counts)
        month_to_date = {'first': counts['initial'], 'review': counts['review'] + counts['follow_up'],
                         'messages': counts['messages'],
                         # Rand with a space between thousands, as in "R14 350.00".
                         'total': f'R{total:,.2f}'.replace(',', '\u00a0') if total is not None else ''}
    return {'staff_membership': membership, 'staff_is_clinician': is_clinician, 'month_to_date': month_to_date}


@method_decorator(never_cache, name='dispatch')
class AccountSignOutOthersView(LoginRequiredMixin, View):
    """End every other session for this account; this device stays signed in."""

    http_method_names = ('post',)

    def post(self, request):
        from django.contrib.auth import SESSION_KEY
        from django.contrib.sessions.models import Session

        removed = 0
        for session in Session.objects.filter(expire_date__gt=timezone.now()).exclude(session_key=request.session.session_key).iterator():
            if str(session.get_decoded().get(SESSION_KEY)) == str(request.user.pk):
                session.delete()
                removed += 1
        from care.services import record_audit

        for company in enabled_companies().filter(memberships__user=request.user, memberships__is_active=True).distinct():
            record_audit(company=company, actor=request.user, action='account.sessions_ended', target=request.user,
                         request=request, metadata={'sessions': removed})
        messages.success(request, 'Signed out of every other device. This one stays signed in.' if removed
                         else 'No other devices were signed in. This one stays signed in.')
        return redirect('accounts:profile')


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


class PatientAccountPageMixin:
    """People with only a patient record see their account pages in the patient app's look."""

    def get_context_data(self, **kwargs):
        from practices.models import CompanyMembership, Patient

        context = super().get_context_data(**kwargs)
        user = self.request.user
        has_staff_access = scope_queryset(CompanyMembership.objects.filter(user=user, is_active=True, company__is_active=True)).exists()
        has_patient_access = scope_queryset(Patient.objects.filter(user=user, is_active=True, company__is_active=True)).exists()
        context['is_patient_portal'] = has_patient_access and not has_staff_access
        return context


@method_decorator(never_cache, name='dispatch')
class AccountPasswordChangeView(PatientAccountPageMixin, PasswordChangeView):
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

        form.user.last_change_password = timezone.now()  # Saved with the new password.
        response = super().form_valid(form)
        companies = enabled_companies().filter(
            Q(memberships__user=self.request.user, memberships__is_active=True) |
            Q(practices_patient_records__user=self.request.user, practices_patient_records__is_active=True)
        ).distinct()
        for company in companies:
            record_audit(company=company, actor=self.request.user, action='account.password_changed',
                         target=self.request.user, request=self.request)
        return response


@method_decorator(never_cache, name='dispatch')
class AccountPasswordChangeDoneView(LoginRequiredMixin, PatientAccountPageMixin, PasswordChangeDoneView):
    template_name = 'accounts/password_change_done.html'


# Reset requests per hour: per email address, and per network address across emails.
RESET_LIMITS = (('email', 5), ('ip', 20))


def _reset_allowed(request, email):
    """Count this request; False once a limit is reached. The response never says which."""
    allowed = True
    for kind, limit in RESET_LIMITS:
        value = email.lower() if kind == 'email' else (request.META.get('REMOTE_ADDR') or 'unknown')
        key = f'password-reset:{kind}:{value}'
        cache.add(key, 0, timeout=60 * 60)
        if cache.incr(key) > limit:
            allowed = False
    return allowed


@method_decorator(never_cache, name='dispatch')
class AccountPasswordResetView(PasswordResetView):
    """Email a one-hour, single-use link; the reply is the same whether or not the account exists."""

    template_name = 'accounts/password_reset.html'
    form_class = AccountPasswordResetForm
    email_template_name = 'accounts/password_reset_email.txt'
    html_email_template_name = 'accounts/password_reset_email.html'
    subject_template_name = 'accounts/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password-reset-done')
    extra_email_context = {'site_name': 'Meridian Health'}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['reset_email_available'] = settings.PASSWORD_RESET_EMAIL_ENABLED
        return context

    def post(self, request, *args, **kwargs):
        if not settings.PASSWORD_RESET_EMAIL_ENABLED:
            return redirect('accounts:password-reset')
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if not _reset_allowed(self.request, form.cleaned_data['email']):
            return redirect(self.success_url)
        return super().form_valid(form)


class AccountPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'accounts/password_reset_done.html'


@method_decorator(never_cache, name='dispatch')
class AccountPasswordResetConfirmView(PasswordResetConfirmView):
    """Set a new password from the emailed link; every other session is signed out."""

    template_name = 'accounts/password_reset_confirm.html'
    success_url = reverse_lazy('accounts:password-reset-complete')

    def form_valid(self, form):
        from care.services import record_audit

        form.user.last_forgot_password_reset = timezone.now()  # Saved with the new password.
        response = super().form_valid(form)
        user = form.user
        companies = enabled_companies().filter(
            Q(memberships__user=user, memberships__is_active=True) |
            Q(practices_patient_records__user=user, practices_patient_records__is_active=True)
        ).distinct()
        for company in companies:
            record_audit(company=company, actor=user, action='account.password_reset', target=user, request=self.request)
        return response


class AccountPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'accounts/password_reset_complete.html'
