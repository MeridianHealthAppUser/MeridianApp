from django import forms
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from care.models import ConsentRecord, Lead, ScreeningQuestionnaire
from care.services import record_audit
from practices.models import CompanyMembership

from .staff_views import StaffPageView
from .followup_views import with_follow_up
from django.utils import timezone


class LeadFilterForm(forms.Form):
    q = forms.CharField(label='Search leads', max_length=200, required=False,
                        widget=forms.TextInput(attrs={'type': 'search', 'placeholder': 'Name, email, phone or ID'}))
    screening_status = forms.ChoiceField(label='Screening', required=False, choices=(('', 'All outcomes'), *Lead.ScreeningStatus.choices))
    stage = forms.ChoiceField(label='Stage', required=False, choices=(('', 'All stages'), *Lead.Stage.choices))
    follow_up = forms.ChoiceField(label='Follow-up', required=False, choices=(('', 'All follow-ups'), ('due', 'Due for contact'), ('open', 'Open'), ('done', 'Completed'), ('none', 'Not yet followed up')))


@method_decorator(never_cache, name='dispatch')
class LeadPageView(StaffPageView):
    nav_section = 'leads'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.membership.role not in (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN):
            raise PermissionDenied('Only practice administrators and practice Super Admins can access leads.')
        return context


class LeadListView(LeadPageView):
    template_name = 'portal/staff_leads.html'
    page_title = 'Leads and enquiries'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = with_follow_up(Lead.objects.for_company(self.company).order_by('-created_at', '-pk'), 'lead')
        context['metrics'] = {
            'total': queryset.count(),
            'cleared': queryset.filter(screening_status=Lead.ScreeningStatus.CLEARED, converted_patient__isnull=True).count(),
            'referred': queryset.filter(screening_status=Lead.ScreeningStatus.REFERRED).count(),
            'converted': queryset.filter(stage=Lead.Stage.CONVERTED).count(),
        }
        form = LeadFilterForm(self.request.GET)
        filters = {}
        if form.is_valid():
            filters = form.cleaned_data
            for term in filters['q'].split():
                queryset = queryset.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term) |
                                           Q(email__icontains=term) | Q(phone__icontains=term) | Q(id_number__icontains=term))
            if filters['screening_status']:
                queryset = queryset.filter(screening_status=filters['screening_status'])
            if filters['stage']:
                queryset = queryset.filter(stage=filters['stage'])
            if filters['follow_up'] == 'due':
                queryset = queryset.filter(follow_up_status='open', next_contact_on__lte=timezone.localdate())
            elif filters['follow_up'] == 'none':
                queryset = queryset.filter(follow_up_status__isnull=True)
            elif filters['follow_up']:
                queryset = queryset.filter(follow_up_status=filters['follow_up'])
        else:
            queryset = queryset.none()
        context.update(self.paginate(queryset, **filters))
        context.update(filter_form=form, leads=context['page_obj'].object_list)
        return context


class LeadDetailView(LeadPageView):
    template_name = 'portal/staff_lead_detail.html'
    page_title = 'Lead details'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lead = get_object_or_404(Lead.objects.for_company(self.company), pk=self.kwargs['pk'])
        questionnaire = ScreeningQuestionnaire.objects.for_company(self.company).filter(lead=lead, stage=1).first()
        fields = (('height_cm', 'Height (cm)'), ('weight_kg', 'Weight (kg)'), ('bmi', 'BMI at submission'),
                  ('adult', '18 or older'), ('weight_related_condition', 'Weight-related condition'),
                  ('pregnancy', 'Pregnant, breastfeeding or planning pregnancy within a year'),
                  ('thyroid_history', 'Personal/family history of medullary thyroid cancer or MEN2'),
                  ('pancreatitis', 'History of pancreatitis'), ('health_context', 'Chronic medication and allergies'))
        answers = questionnaire.answers if questionnaire else {}
        display = []
        for key, label in fields:
            value = answers.get(key)
            if key in ('adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis'):
                value = {'yes': 'Yes', 'no': 'No', 'unsure': 'Not sure'}.get(value, 'Not recorded')
            display.append((label, value or 'Not recorded'))
        context.update(lead=lead, questionnaire=questionnaire, answers_display=display,
                       screening_reasons=answers.get('screening_reasons', []),
                       consents=ConsentRecord.objects.for_company(self.company).filter(lead=lead).select_related('document'))
        record_audit(company=self.company, actor=self.request.user, action='lead.viewed', target=lead, request=self.request)
        return context
