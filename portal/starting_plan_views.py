"""Starting plans in the Pharmacy catalogue: the standard ways to start a main medicine that Initiate prescription
offers on the patient record. Super Admins add and change them; practice administrators can read them."""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from care.models import StartingPlan
from care.operations import require_operations_actor
from care.prescriptions import WEEK_CHOICES, main_doses, weeks_text
from care.starting_plans import MAX_STEPS, STEP_WEEKS, plan_summary, save_starting_plan
from practices.models import Company, CompanyMembership

from .operations_views import OperationsView
from .workflow_context import validate_workflow_context


class StartingPlanForm(forms.Form):
    medicine = forms.ChoiceField(label='Medicine')
    name = forms.CharField(label='Name (optional)', max_length=80, required=False,
                           help_text='Shown above the plan in Initiate prescription, e.g. “Standard start”.')
    then_dose = forms.ChoiceField(label='Then', help_text='The dose the plan settles on, until the prescription ends.')
    default_weeks = forms.TypedChoiceField(label='Usual length of the prescription', coerce=int,
                                           choices=[(weeks, weeks_text(weeks)) for weeks in WEEK_CHOICES],
                                           help_text='Initiate prescription starts on this; the doctor can choose another.')
    is_active = forms.BooleanField(label='Offer this plan in Initiate prescription', required=False, initial=True)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        # The doses a doctor can prescribe, lowest first by medicine.
        doses = sorted(main_doses(company), key=lambda dose: (dose.medicine, dose.amount_mg, dose.frequency, dose.pk))
        self.doses = {str(dose.pk): dose for dose in doses}
        grouped = {}
        for dose in doses:
            grouped.setdefault(dose.medicine, []).append((str(dose.pk), str(dose)))
        choices = list(grouped.items())
        self.fields['medicine'].choices = [(name, name) for name in grouped]
        self.fields['then_dose'].choices = [('', 'Choose a dose'), *choices]
        weeks = [('', 'Weeks'), *((weeks, weeks_text(weeks)) for weeks in STEP_WEEKS)]
        for position in range(1, MAX_STEPS + 1):
            self.fields[f'step_{position}_dose'] = forms.ChoiceField(
                label=f'Step {position}', required=False, choices=[('', 'No step' if position > 1 else 'Choose a dose'), *choices])
            self.fields[f'step_{position}_weeks'] = forms.TypedChoiceField(
                label=f'Step {position} lasts', required=False, coerce=int, empty_value=None, choices=weeks)

    def step_rows(self):
        return [(self[f'step_{position}_dose'], self[f'step_{position}_weeks']) for position in range(1, MAX_STEPS + 1)]

    def clean(self):
        data = super().clean()
        steps = []
        for position in range(1, MAX_STEPS + 1):
            dose, weeks = data.get(f'step_{position}_dose'), data.get(f'step_{position}_weeks')
            if dose and not weeks:
                self.add_error(f'step_{position}_weeks', 'Choose how many weeks this step lasts.')
            elif dose:
                steps.append((self.doses[dose], weeks))
        data['steps'] = steps
        data['then'] = self.doses.get(data.get('then_dose') or '')
        return data


class StartingPlanEditorView(OperationsView):
    nav_section, page_title = 'catalogue', 'Starting plan'

    def display(self, request, plan, form, status=200):
        context = self.context(plan=plan, form=form, workflow_context=self.token('catalogue-starting-plan', plan),
                               plan_summary=plan_summary(plan) if plan else '')
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        return render(request, 'portal/operations_starting_plan_form.html', context, status=status)

    def plan(self, pk):
        return get_object_or_404(StartingPlan.objects.for_company(self.company).select_related('then_dose__pen').prefetch_related(
            'steps__dose__pen'), pk=pk) if pk else None

    def get(self, request, pk=None):
        plan = self.plan(pk)
        initial = {'default_weeks': 24, 'is_active': True}
        if plan:
            initial = {'medicine': plan.medicine, 'name': plan.name, 'then_dose': str(plan.then_dose_id),
                       'default_weeks': plan.default_weeks, 'is_active': plan.is_active}
            for step in plan.steps.all():
                initial[f'step_{step.position}_dose'] = str(step.dose_id)
                initial[f'step_{step.position}_weeks'] = step.weeks
        return self.display(request, plan, StartingPlanForm(company=self.company, initial=initial))

    @transaction.atomic
    def post(self, request, pk=None):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        plan = self.plan(pk)
        form = StartingPlanForm(request.POST, company=self.company)
        try:
            validate_workflow_context(request, self.company, 'catalogue-starting-plan', plan)
            if form.is_valid():
                data = form.cleaned_data
                saved = save_starting_plan(company=self.company, actor=request.user, plan=plan, medicine=data['medicine'],
                                           name=data['name'], steps=data['steps'], then_dose=data['then'],
                                           default_weeks=data['default_weeks'], is_active=data['is_active'], request=request)
                messages.success(request, 'Starting plan saved. Prescriptions already written from it keep their own steps.')
                return redirect('portal:ops-starting-plan-detail', pk=saved.pk)
        except ValidationError as error:
            form.is_valid()
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, plan, form, status=400)
