from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from urllib.parse import urlencode

from care.followups import append_follow_up, confirm_called, resolve_help_request
from care.messaging import is_participant
from care.models import AdministrativeFollowUp, CallbackRequest, HelpRequest, Lead, PatientSubscription
from care.services import record_audit
from practices.models import Company, CompanyMembership
from .filter_menus import TickedChoiceField, list_tools, ticked_sections
from .inbox import staff_inbox_url
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


FOLLOW_UP_CHOICES = (('due', 'Due for contact'), ('open', 'Open'), ('done', 'Completed'), ('none', 'Not yet followed up'))


def follow_up_matches(states):
    """Any of the ticked follow-up states, for a queryset annotated by with_follow_up."""
    condition = Q()
    for state in states:
        if state == 'due':
            condition |= Q(follow_up_status='open', next_contact_on__lte=timezone.localdate())
        elif state == 'none':
            condition |= Q(follow_up_status__isnull=True)
        else:
            condition |= Q(follow_up_status=state)
    return condition


class FollowUpFilter(forms.Form):
    q = forms.CharField(label='Search', required=False, max_length=200)
    status = TickedChoiceField(label='Plan status', choices=(('paused', 'Paused'), ('cancelled', 'Cancelled')))
    follow_up = TickedChoiceField(label='Follow-up', choices=FOLLOW_UP_CHOICES)


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


class ResolveForm(forms.Form):
    note = forms.CharField(label='Note (optional)', max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 3}))


class DropoutListView(AdminFollowUpView):
    """Dropouts: patients who said something is wrong, until someone confirms it's resolved, then the cancelled and
    paused subscriptions (expandable). Please call: people who asked for a call at the end of the questionnaire."""

    nav_section = 'dropouts'
    http_method_names = ('get', 'head', 'options')

    def help_requests(self, request):
        page = Paginator(HelpRequest.objects.for_company(self.company).filter(
            patient__company=self.company, status=HelpRequest.Status.OPEN,
        ).select_related('patient__assigned_doctor', 'patient__user', 'thread'), 20).get_page(request.GET.get('help_page'))
        for item in page:
            patient, doctor = item.patient, item.patient.assigned_doctor
            item.doctor = doctor
            # Message doctor starts a team conversation about the patient; Message patient opens the conversation the
            # request started, when you're in it.
            item.message_doctor_url = f"{reverse('portal:team-inbox')}?{urlencode({'compose': 1, 'patient': patient.pk, **({'members': doctor.pk} if doctor else {}), 'subject': f'{patient}: something is not working'})}"
            item.message_patient_url = (staff_inbox_url(item.thread) if item.thread_id and is_participant(item.thread, request.user)
                                        else f"{reverse('portal:staff-inbox')}?{urlencode({'compose': 1, 'patient': patient.pk})}")
            item.resolve_context = make_workflow_context(request, self.company, 'help-request-resolve', item)
        return page

    def callbacks(self, request):
        page = Paginator(CallbackRequest.objects.for_company(self.company).filter(status=CallbackRequest.Status.OPEN),
                         20).get_page(request.GET.get('call_page'))
        for item in page:
            item.called_context = make_workflow_context(request, self.company, 'callback-called', item)
        return page

    def get(self, request):
        self.require_admin()
        form = FollowUpFilter(request.GET)
        queryset = with_follow_up(PatientSubscription.objects.for_company(self.company).filter(patient__company=self.company, status__in=('paused', 'cancelled')).select_related('patient__assigned_doctor', 'patient__user').order_by('-updated_at', '-pk'), 'subscription')
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                queryset = queryset.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term) | Q(plan_name__icontains=term))
            if form.cleaned_data['status']:
                queryset = queryset.filter(status__in=form.cleaned_data['status'])
            if form.cleaned_data['follow_up']:
                queryset = queryset.filter(follow_up_matches(form.cleaned_data['follow_up']))
        else:
            queryset = queryset.none()
        page = Paginator(queryset, 20).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        tools = list_tools(action=reverse('portal:dropouts'), data=request.GET, form=form, label='Filter dropouts',
                           sections=ticked_sections(form, request.GET, ('status', 'follow_up'), icons={'follow_up': 'list'}),
                           placeholder='Patient or plan')
        count = page.paginator.count
        searching = any(request.GET.get(name) for name in ('q', 'status', 'follow_up', 'page'))
        # Each list pages on its own; its links keep the others where they were.
        others = {name: request.GET.copy() for name in ('help_page', 'call_page')}
        for name, kept in others.items():
            kept.pop(name, None)
        return render(request, 'portal/followup_dropouts.html', self.context(
            filter_form=form, page_obj=page, pagination_query=query.urlencode(), ended_open=searching,
            count_label=f'{count} subscription{pluralize(count)}', help_page=self.help_requests(request),
            call_page=self.callbacks(request), resolve_form=ResolveForm(), help_query=others['help_page'].urlencode(),
            call_query=others['call_page'].urlencode(), **tools))


class HelpRequestResolveView(AdminFollowUpView):
    """Confirm resolved, on a Dropouts row: off the list, with the note kept."""

    http_method_names = ('post',)

    def post(self, request, pk):
        self.require_admin()
        item = get_object_or_404(HelpRequest.objects.for_company(self.company).filter(patient__company=self.company), pk=pk)
        form = ResolveForm(request.POST)
        try:
            validate_workflow_context(request, self.company, 'help-request-resolve', item)
            if not form.is_valid():
                raise ValidationError('Keep the note to 1,000 characters or fewer.')
            resolve_help_request(help_request=item, actor=request.user, note=form.cleaned_data['note'], request=request)
            messages.success(request, f'{item.patient} marked resolved and taken off the Dropouts list.')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect('portal:dropouts')


class CallbackCalledView(AdminFollowUpView):
    """Confirm called, on a Please call row: off the list, with the note kept."""

    http_method_names = ('post',)

    def post(self, request, pk):
        self.require_admin()
        item = get_object_or_404(CallbackRequest.objects.for_company(self.company), pk=pk)
        form = ResolveForm(request.POST)
        try:
            validate_workflow_context(request, self.company, 'callback-called', item)
            if not form.is_valid():
                raise ValidationError('Keep the note to 1,000 characters or fewer.')
            confirm_called(callback=item, actor=request.user, note=form.cleaned_data['note'], request=request)
            messages.success(request, f'Call to {item.name} confirmed and taken off the Please call list.')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect(f"{reverse('portal:dropouts')}#please-call-heading")


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
