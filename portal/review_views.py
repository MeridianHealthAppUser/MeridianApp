from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from care.models import PracticeSettings, ReviewRule
from care.review_rules import save_review_rule, update_review_default
from care.review_automation import due_authorizations
from practices.models import CompanyMembership

from .clinical_views import StaffClinicalView, _error
from .review_forms import ReviewDefaultForm, ReviewRuleForm, make_review_context, validate_review_context
from .compounding_forms import ReviewRunForm


class ReviewPlanningView(StaffClinicalView):
    nav_section = 'review_rules'
    page_title = 'Review planning rules'

    @property
    def can_edit_rules(self):
        return self.membership.role == CompanyMembership.Role.SUPER_ADMIN

    def require_edit(self):
        if not self.can_edit_rules:
            raise PermissionDenied('Only a Super Admin can change operational review planning rules.')


class ReviewRuleListView(ReviewPlanningView):
    http_method_names = ('get', 'head', 'options')

    def display(self, request, form=None, run_form=None, status=200):
        settings = PracticeSettings.objects.for_company(self.company).first()
        bound = form is not None
        form = form if bound else ReviewDefaultForm(initial={'review_interval_days': settings.review_interval_days} if settings else {})
        return render(request, 'portal/treatment_review_rules.html', self.context(
            page_obj=Paginator(ReviewRule.objects.for_company(self.company).order_by('name', 'pk'), 20).get_page(request.GET.get('page')),
            settings=settings, form=form, can_edit_rules=self.can_edit_rules,
            review_context=request.POST.get('review_context', '') if bound else make_review_context(request, self.company, 'review-default', settings),
            run_form=run_form if run_form is not None else ReviewRunForm(auto_id='run_%s'),
            run_context=request.POST.get('review_context', '') if run_form is not None else make_review_context(request, self.company, 'review-run'),
            due_page=Paginator(due_authorizations(company=self.company), 20).get_page(request.GET.get('due_page')),
        ), status=status)

    def get(self, request):
        return self.display(request)


class ReviewDefaultUpdateView(ReviewRuleListView):
    http_method_names = ('post', 'options')

    def post(self, request):
        self.require_edit()
        settings = PracticeSettings.objects.for_company(self.company).first()
        form = ReviewDefaultForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_review_context(request, self.company, 'review-default', settings)
            if valid:
                update_review_default(company=self.company, actor=request.user, expected_updated_at=token.get('updated_at'),
                                      review_interval_days=form.cleaned_data['review_interval_days'], request=request)
                messages.success(request, 'Review planning default saved. Existing authorisations are unchanged.')
                return redirect('portal:treatment-review-rules')
        except ValidationError as error:
            _error(form, error)
        return self.display(request, form, status=400)


class ReviewRuleEditorView(ReviewPlanningView):
    def display(self, request, rule, form, status=200):
        return render(request, 'portal/treatment_review_rule_form.html', self.context(
            rule=rule, form=form, can_edit_rules=self.can_edit_rules,
            review_context=request.POST.get('review_context') or make_review_context(request, self.company, 'review-rule', rule),
        ), status=status)

    def get(self, request, pk=None):
        if pk is None:
            self.require_edit()
        rule = get_object_or_404(ReviewRule.objects.for_company(self.company), pk=pk) if pk else None
        initial = {name: getattr(rule, name) for name in ReviewRuleForm.base_fields} if rule else {}
        return self.display(request, rule, ReviewRuleForm(initial=initial))

    def post(self, request, pk=None):
        self.require_edit()
        rule = get_object_or_404(ReviewRule.objects.for_company(self.company), pk=pk) if pk else None
        form = ReviewRuleForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_review_context(request, self.company, 'review-rule', rule)
            if valid:
                saved = save_review_rule(company=self.company, actor=request.user, rule=rule,
                                         expected_updated_at=token.get('updated_at'), request=request, **form.cleaned_data)
                messages.success(request, 'Review planning rule saved. No clinical record, test order or prescription was changed.')
                return redirect('portal:treatment-review-rule-detail', pk=saved.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, rule, form, status=400)
