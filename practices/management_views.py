from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from .management_forms import (MembershipForm, PracticeForm, StaffUserForm, UserFilterForm,
                              make_management_context, validate_management_context)
from .management_services import (add_staff_user, create_practice, manageable_practices,
                                  require_super_admin, update_membership, update_practice)
from .models import Company, CompanyMembership
from .services import active_membership_for, get_active_company
from .tenancy import multi_practice_enabled, require_multi_practice


@method_decorator(never_cache, name='dispatch')
class ManagementView(LoginRequiredMixin, View):
    nav_section = 'users'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.company = get_active_company(request)
        if self.company is None:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied('An active Super Admin membership is required.')
        require_super_admin(request.user, self.company)
        self.membership = active_membership_for(request, self.company)
        return super().dispatch(request, *args, **kwargs)

    def context(self, **kwargs):
        return {'company': self.company, 'active_membership': self.membership,
                'nav_section': self.nav_section, **kwargs}

    def form_page(self, request, *, form, title, description, kind, record=None,
                  cancel_name='portal:management-users', submit_label='Save changes', status=200):
        token = (request.POST.get('management_context', '') if request.method == 'POST'
                 else make_management_context(request, self.company, kind, record))
        # Preserve the original signed version on invalid POST; a stale form must be reloaded.
        return render(request, 'portal/management_form.html', self.context(
            form=form, page_title=title, description=description, record=record,
            management_context=token, cancel_url=reverse(cancel_name), submit_label=submit_label), status=status)

    @staticmethod
    def add_service_error(form, exc):
        if isinstance(exc, IntegrityError):
            form.add_error(None, 'Another request saved the same account or identifier. Reload and check the current records.')
        else:
            for message in exc.messages:
                form.add_error(None, message)


class ManagementUsersView(ManagementView):
    def get(self, request):
        form = UserFilterForm(request.GET)
        memberships = CompanyMembership.objects.filter(company=self.company).select_related('user')
        if form.is_valid():
            data = form.cleaned_data
            if data['q']:
                memberships = memberships.filter(Q(user__email__icontains=data['q']) |
                    Q(user__first_name__icontains=data['q']) | Q(user__last_name__icontains=data['q']))
            if data['status']:
                memberships = memberships.filter(is_active=data['status'] == 'active')
            if data['role']:
                memberships = memberships.filter(role=data['role'])
        else:
            memberships = memberships.none()
        page = Paginator(memberships.order_by('user__last_name', 'user__first_name', 'pk'), 20).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        return render(request, 'portal/management_users.html', self.context(
            filter_form=form, memberships=page.object_list, page_obj=page, pagination_query=query.urlencode()))


@method_decorator(sensitive_post_parameters('password1', 'password2'), name='dispatch')
class ManagementUserCreateView(ManagementView):
    def get(self, request):
        return self.show(request, StaffUserForm(actor=request.user, company=self.company))

    def show(self, request, form, status=200):
        return self.form_page(request, form=form, title='Add staff access', kind='staff-create',
            description=('Create a staff-only login with a password to share securely, or link an existing '
                         'account without changing its identity. No invitation email is sent. '
                         'Patient accounts are not created here.'), submit_label='Add staff access', status=status)

    def post(self, request):
        form = StaffUserForm(request.POST, actor=request.user, company=self.company)
        valid = form.is_valid()
        try:
            validate_management_context(request, self.company, 'staff-create')
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.show(request, form, status=400)
        if valid:
            data = form.cleaned_data
            try:
                add_staff_user(actor=request.user, source_company=self.company, companies=data['practices'],
                    mode=data['mode'], email=data['email'], role=data['role'], clinician_type=data['clinician_type'],
                    first_name=data.get('first_name', ''),
                    last_name=data.get('last_name', ''), password=data.get('password1', ''), request=request)
            except (ValidationError, IntegrityError) as exc:
                self.add_service_error(form, exc)
            else:
                messages.success(request, 'Staff access added. No email was sent; share new credentials securely if you created an account.')
                return redirect('portal:management-users')
        return self.show(request, form)


class ManagementMembershipEditView(ManagementView):
    def get_record(self, pk):
        return get_object_or_404(CompanyMembership.objects.select_related('user'), pk=pk, company=self.company)

    def show(self, request, form, record, status=200):
        return self.form_page(request, form=form, record=record, kind='membership',
            title=f'Staff access: {record.user.full_name}',
            description=f'{record.user.email} · These changes affect only {self.company.name}. '
                        'The shared account’s name, email and password are not editable here.', status=status)

    def get(self, request, pk):
        record = self.get_record(pk)
        return self.show(request, MembershipForm(initial={'role': record.role, 'clinician_type': record.clinician_type, 'is_active': record.is_active}), record)

    def post(self, request, pk):
        record = self.get_record(pk)
        form = MembershipForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_management_context(request, self.company, 'membership', record)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.show(request, form, record, status=400)
        if valid:
            try:
                updated = update_membership(actor=request.user, company=self.company, membership=record,
                    role=form.cleaned_data['role'], clinician_type=form.cleaned_data['clinician_type'],
                    is_active=form.cleaned_data['is_active'],
                    expected_updated_at=token['updated_at'], request=request)
            except (ValidationError, IntegrityError) as exc:
                self.add_service_error(form, exc)
            else:
                messages.success(request, 'Practice membership updated. The shared login and other practices are unchanged.'
                                 if multi_practice_enabled() else 'Staff access updated. The account and password are unchanged.')
                if updated.user_id == request.user.pk and (not updated.is_active or updated.role != CompanyMembership.Role.SUPER_ADMIN):
                    return redirect('portal:desktop-dashboard')
                return redirect('portal:management-users')
        return self.show(request, form, record)


class PracticeManagementView(ManagementView):
    def dispatch(self, request, *args, **kwargs):
        require_multi_practice()
        return super().dispatch(request, *args, **kwargs)


class ManagementPracticesView(PracticeManagementView):
    nav_section = 'practices'

    def get(self, request):
        page = Paginator(manageable_practices(request.user).order_by('name', 'pk'), 20).get_page(request.GET.get('page'))
        return render(request, 'portal/management_practices.html', self.context(page_obj=page, practices=page.object_list))


class ManagementPracticeCreateView(PracticeManagementView):
    nav_section = 'practices'

    def show(self, request, form, status=200):
        return self.form_page(request, form=form, title='Create a practice', kind='practice-create',
            description='This creates an empty practice and adds you as its Super Admin. It does not copy patients, '
                        'clinical rules, questionnaire approvals or other staff. No billing or email is activated.',
            cancel_name='portal:management-practices', submit_label='Create practice', status=status)

    def get(self, request):
        return self.show(request, PracticeForm())

    def post(self, request):
        form = PracticeForm(request.POST)
        valid = form.is_valid()
        try:
            validate_management_context(request, self.company, 'practice-create')
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.show(request, form, status=400)
        if valid:
            try:
                create_practice(actor=request.user, source_company=self.company, name=form.cleaned_data['name'],
                                slug=form.cleaned_data['slug'], request=request)
            except (ValidationError, IntegrityError) as exc:
                self.add_service_error(form, exc)
            else:
                messages.success(request, 'Practice created. Select it in the practice switcher to manage its staff and setup.')
                return redirect('portal:management-practices')
        return self.show(request, form)


class ManagementPracticeEditView(PracticeManagementView):
    nav_section = 'practices'

    def get_record(self, pk):
        return get_object_or_404(Company, pk=pk, id=self.company.pk, is_active=True)

    def show(self, request, form, record, status=200):
        return self.form_page(request, form=form, title='Practice details', kind='practice', record=record,
            description='Update the selected practice’s display name and unique identifier. '
                        'Clinical data, staff memberships and other practices are unchanged.',
            cancel_name='portal:management-practices', status=status)

    def get(self, request, pk):
        record = self.get_record(pk)
        return self.show(request, PracticeForm(instance=record), record)

    def post(self, request, pk):
        record = self.get_record(pk)
        # ModelForm.clean mutates its instance, so keep a separate record for the original version token.
        form = PracticeForm(request.POST, instance=Company.objects.get(pk=record.pk))
        valid = form.is_valid()
        try:
            token = validate_management_context(request, self.company, 'practice', record)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.show(request, form, record, status=400)
        if valid:
            try:
                update_practice(actor=request.user, company=record, name=form.cleaned_data['name'],
                    slug=form.cleaned_data['slug'], expected_updated_at=token['updated_at'], request=request)
            except (ValidationError, IntegrityError) as exc:
                self.add_service_error(form, exc)
            else:
                messages.success(request, 'Practice details saved.')
                return redirect('portal:management-practices')
        return self.show(request, form, record)
