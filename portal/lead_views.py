from django import forms
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from care.models import ConsentRecord, Lead, ScreeningQuestionnaire
from care.screening import display_answers
from care.services import record_audit
from practices.models import CompanyMembership

from django.template.defaultfilters import pluralize
from django.urls import reverse

from .filter_menus import TickedChoiceField, list_tools, ticked_sections
from .staff_views import StaffPageView
from .followup_views import FOLLOW_UP_CHOICES, follow_up_matches, please_call, with_follow_up


class LeadFilterForm(forms.Form):
    q = forms.CharField(label='Search leads', max_length=200, required=False)
    screening_status = TickedChoiceField(label='Screening', choices=Lead.ScreeningStatus.choices)
    stage = TickedChoiceField(label='Stage', choices=Lead.Stage.choices)
    follow_up = TickedChoiceField(label='Follow-up', choices=FOLLOW_UP_CHOICES)
    flags = TickedChoiceField(label='Flags', choices=(('flagged', 'Flagged for review'),))


@method_decorator(never_cache, name='dispatch')
class LeadPageView(StaffPageView):
    nav_section = 'leads'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.membership.role not in (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN):
            raise PermissionDenied('Only practice administrators and practice Super Admins can access leads.')
        return context


class LeadListView(LeadPageView):
    """Leads: the Please call list (Dr Czech, 4 Nov 2026; it was on Dropouts). Lead history (history=True) is every
    enquiry, converted ones too, with search and filters."""

    template_name = 'portal/staff_leads.html'
    page_title = 'Leads and enquiries'
    history = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.history:
            context.update(please_call(self.request, self.company), lead_history=False)
            return context
        queryset = with_follow_up(Lead.objects.for_company(self.company).order_by('-created_at', '-pk'), 'lead')
        form = LeadFilterForm(self.request.GET)
        filters = {}
        if form.is_valid():
            filters = form.cleaned_data
            for term in filters['q'].split():
                queryset = queryset.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term) |
                                           Q(email__icontains=term) | Q(phone__icontains=term) | Q(id_number__icontains=term))
            if filters['screening_status']:
                queryset = queryset.filter(screening_status__in=filters['screening_status'])
            if filters['stage']:
                queryset = queryset.filter(stage__in=filters['stage'])
            if filters['flags']:
                queryset = queryset.filter(screening_flagged=True)
            if filters['follow_up']:
                queryset = queryset.filter(follow_up_matches(filters['follow_up']))
        else:
            queryset = queryset.none()
        context.update(self.paginate(queryset, **filters))
        count = context['page_obj'].paginator.count
        context.update(filter_form=form, leads=context['page_obj'].object_list, lead_history=True,
                       count_label=f'{count} enquir{pluralize(count, "y,ies")}')
        context.update(list_tools(
            action=reverse('portal:staff-lead-history'), data=self.request.GET,
            form=form, label='Filter leads',
            sections=ticked_sections(form, self.request.GET, ('screening_status', 'stage', 'follow_up', 'flags'),
                                     icons={'stage': 'list', 'follow_up': 'list', 'flags': 'list'}),
            placeholder='Name, email, phone or ID'))
        return context


class LeadDetailView(LeadPageView):
    template_name = 'portal/staff_lead_detail.html'
    page_title = 'Lead details'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lead = get_object_or_404(Lead.objects.for_company(self.company), pk=self.kwargs['pk'])
        questionnaire = (ScreeningQuestionnaire.objects.for_company(self.company).select_related('form_version')
                         .filter(lead=lead, stage=1).first())
        answers = questionnaire.answers if questionnaire else {}
        display = display_answers(questionnaire) if questionnaire else []
        context.update(lead=lead, questionnaire=questionnaire, answers_display=display,
                       form_version_number=questionnaire.form_version.number if questionnaire and questionnaire.form_version else None,
                       screening_reasons=answers.get('screening_reasons', []),
                       consents=ConsentRecord.objects.for_company(self.company).filter(lead=lead).select_related('document'))
        record_audit(company=self.company, actor=self.request.user, action='lead.viewed', target=lead, request=self.request)
        return context
