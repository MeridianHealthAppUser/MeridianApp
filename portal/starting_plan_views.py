"""Starting plans in the Pharmacy catalogue: the standard ways to start a main medicine that Initiate prescription
offers on the patient record. Super Admins add and change them; practice administrators can read them."""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from care.models import MedicationProduct, StartingPlan
from care.operations import require_operations_actor
from care.prescriptions import MONTH_CHOICES, dose_label, strength_mg
from care.starting_plans import MAX_STEPS, STEP_MONTHS, plan_summary, save_starting_plan
from practices.models import Company, CompanyMembership

from .operations_views import OperationsView
from .workflow_context import validate_workflow_context


def main_products(company):
    """Strengths of the main medicines (weight management, needing a prescription), lowest first."""
    products = list(MedicationProduct.objects.for_company(company).filter(
        category=MedicationProduct.Category.WEIGHT_MANAGEMENT, requires_authorisation=True, is_active=True,
    ))
    products.sort(key=lambda product: (product.name, strength_mg(product) is None, strength_mg(product) or 0, product.strength))
    return products


class StartingPlanForm(forms.Form):
    medicine = forms.ChoiceField(label='Medicine')
    name = forms.CharField(label='Name (optional)', max_length=80, required=False,
                           help_text='Shown above the plan in Initiate prescription, e.g. “Standard start”.')
    then_product = forms.ChoiceField(label='Then', help_text='The strength the plan settles on, until the prescription ends.')
    default_months = forms.TypedChoiceField(label='Usual length of the prescription', coerce=int,
                                            choices=[(months, f'{months} month{"" if months == 1 else "s"}') for months in MONTH_CHOICES],
                                            help_text='Initiate prescription starts on this; the doctor can choose another.')
    is_active = forms.BooleanField(label='Offer this plan in Initiate prescription', required=False, initial=True)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.products = {str(product.pk): product for product in main_products(company)}
        grouped = {}
        for product in self.products.values():
            grouped.setdefault(product.name, []).append((str(product.pk), f'{product.name} {dose_label(product)}'))
        doses = [(name, choices) for name, choices in grouped.items()]
        self.fields['medicine'].choices = [(name, name) for name in grouped]
        self.fields['then_product'].choices = [('', 'Choose a strength'), *doses]
        months = [('', 'Months'), *((months, f'{months} month{"" if months == 1 else "s"}') for months in STEP_MONTHS)]
        for position in range(1, MAX_STEPS + 1):
            self.fields[f'step_{position}_product'] = forms.ChoiceField(
                label=f'Step {position}', required=False, choices=[('', 'No step' if position > 1 else 'Choose a strength'), *doses])
            self.fields[f'step_{position}_months'] = forms.TypedChoiceField(
                label=f'Step {position} lasts', required=False, coerce=int, empty_value=None, choices=months)

    def step_rows(self):
        return [(self[f'step_{position}_product'], self[f'step_{position}_months']) for position in range(1, MAX_STEPS + 1)]

    def clean(self):
        data = super().clean()
        steps = []
        for position in range(1, MAX_STEPS + 1):
            product, months = data.get(f'step_{position}_product'), data.get(f'step_{position}_months')
            if product and not months:
                self.add_error(f'step_{position}_months', 'Choose how many months this step lasts.')
            elif product:
                steps.append((self.products[product], months))
        data['steps'] = steps
        data['then'] = self.products.get(data.get('then_product') or '')
        return data


class StartingPlanEditorView(OperationsView):
    nav_section, page_title = 'catalogue', 'Starting plan'

    def display(self, request, plan, form, status=200):
        context = self.context(plan=plan, form=form, workflow_context=self.token('catalogue-starting-plan', plan),
                               plan_summary=plan_summary(plan) if plan else '')
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        return render(request, 'portal/operations_starting_plan_form.html', context, status=status)

    def plan(self, pk):
        return get_object_or_404(StartingPlan.objects.for_company(self.company).select_related('then_product').prefetch_related(
            'steps__product'), pk=pk) if pk else None

    def get(self, request, pk=None):
        plan = self.plan(pk)
        initial = {'default_months': 6, 'is_active': True}
        if plan:
            initial = {'medicine': plan.medicine, 'name': plan.name, 'then_product': str(plan.then_product_id),
                       'default_months': plan.default_months, 'is_active': plan.is_active}
            for step in plan.steps.all():
                initial[f'step_{step.position}_product'] = str(step.product_id)
                initial[f'step_{step.position}_months'] = step.months
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
                                           name=data['name'], steps=data['steps'], then_product=data['then'],
                                           default_months=data['default_months'], is_active=data['is_active'], request=request)
                messages.success(request, 'Starting plan saved. Prescriptions already written from it keep their own steps.')
                return redirect('portal:ops-starting-plan-detail', pk=saved.pk)
        except ValidationError as error:
            form.is_valid()
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, plan, form, status=400)
