from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.followups import append_follow_up
from care.models import AdministrativeFollowUp, Lead, PatientSubscription
from care.services import record_audit
from practices.models import Company, CompanyMembership
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class FollowUpForm(forms.Form):
    note = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 5}))
    status = forms.ChoiceField(choices=AdministrativeFollowUp.Status.choices)
    next_contact_on = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    assigned_to = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, empty_label='Unassigned')
    stage = forms.ChoiceField(required=False, choices=(('questionnaire', 'Questionnaire / enquiry'), ('booking', 'Interested in booking'), ('closed', 'Closed enquiry')))

    def __init__(self, *args, company, is_lead, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(is_active=True, company_memberships__company=company, company_memberships__is_active=True, company_memberships__role__in=('practice_admin', 'super_admin')).distinct().order_by('first_name', 'last_name')
        if not is_lead:
            del self.fields['stage']


class FollowUpFilter(forms.Form):
    q = forms.CharField(label='Search', required=False, max_length=200)
    follow_up = forms.ChoiceField(required=False, choices=(('', 'All follow-ups'), ('due', 'Due for contact'), ('open', 'Open'), ('done', 'Completed'), ('none', 'Not yet followed up')))


def with_follow_up(queryset, field):
    latest = AdministrativeFollowUp.objects.filter(company_id=OuterRef('company_id'), **{field + '_id': OuterRef('pk')}).order_by('-created_at', '-pk')
    return queryset.annotate(follow_up_status=Subquery(latest.values('status')[:1]), next_contact_on=Subquery(latest.values('next_contact_on')[:1]))


@method_decorator(never_cache, name='dispatch')
class AdminFollowUpView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')
    nav_section = 'leads'

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        return response

    def require_admin(self):
        if self.membership.role not in ('practice_admin', 'super_admin'):
            raise PermissionDenied('Administrative follow-ups are available to practice administrators and Super Admins.')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section, **kwargs)


class DropoutListView(AdminFollowUpView):
    nav_section = 'dropouts'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        self.require_admin()
        form = FollowUpFilter(request.GET)
        queryset = with_follow_up(PatientSubscription.objects.for_company(self.company).filter(patient__company=self.company, status__in=('paused', 'cancelled')).select_related('patient').order_by('-updated_at', '-pk'), 'subscription')
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                queryset = queryset.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term) | Q(plan_name__icontains=term))
            state = form.cleaned_data['follow_up']
            if state == 'due':
                queryset = queryset.filter(follow_up_status='open', next_contact_on__lte=timezone.localdate())
            elif state == 'none':
                queryset = queryset.filter(follow_up_status__isnull=True)
            elif state:
                queryset = queryset.filter(follow_up_status=state)
        else:
            queryset = queryset.none()
        page = Paginator(queryset, 20).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        return render(request, 'portal/followup_dropouts.html', self.context(filter_form=form, page_obj=page, pagination_query=query.urlencode()))


class FollowUpDetailView(AdminFollowUpView):
    is_lead = True

    def target(self, pk):
        self.require_admin()
        queryset = Lead.objects.for_company(self.company) if self.is_lead else PatientSubscription.objects.for_company(self.company).filter(patient__company=self.company, status__in=('paused', 'cancelled')).select_related('patient')
        return get_object_or_404(queryset, pk=pk)

    def display(self, request, target, form=None, status=200):
        self.nav_section = 'leads' if self.is_lead else 'dropouts'
        latest = target.follow_ups.filter(company=self.company).select_related('assigned_to').first()
        initial = dict(stage=target.stage if self.is_lead else '', status=latest.status if latest else 'open', assigned_to=latest.assigned_to if latest else None, next_contact_on=latest.next_contact_on if latest else None)
        form = form if form is not None else FollowUpForm(company=self.company, is_lead=self.is_lead, initial=initial)
        history = Paginator(target.follow_ups.filter(company=self.company).select_related('author', 'assigned_to'), 20).get_page(request.GET.get('page'))
        return render(request, 'portal/followup_detail.html', self.context(target=target, is_lead=self.is_lead, form=form, page_obj=history,
            can_edit=not (self.is_lead and (target.converted_patient_id or target.stage == 'converted')),
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'administrative-follow-up', target)), status=status)

    def get(self, request, pk):
        target = self.target(pk)
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='follow_up.viewed', target=target, request=request)
        return self.display(request, target)

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        target = self.target(pk)
        form = FollowUpForm(request.POST, company=self.company, is_lead=self.is_lead)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'administrative-follow-up', target)
            if valid:
                append_follow_up(target=target, actor=request.user, submission_key=token['key'], expected_updated=token['updated'], request=request, **form.cleaned_data)
                messages.success(request, 'Follow-up saved. No email was sent and no account or payment was created.')
                return redirect('portal:lead-follow-up' if self.is_lead else 'portal:dropout-detail', pk=target.pk)
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, target, form, 400)
