"""Settings > Screening questions (Super Admins only): the overview, a read-only version, the draft editor, the
phone-width preview and the POST endpoints that call the care.screening services. Views stay thin."""

import json

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin

from care.forms import EligibilityQuestionnaireForm, ScreeningQuestionForm
from care.intake import practice_notices
from care.models import ScreeningFormVersion, ScreeningQuestion
from care.screening import (
    FIXED_CORE_KEYS, add_question, change_summary, discard_draft, draft_version, live_version,
    movable_questions, publish_draft, reorder, retire_question, save_question, start_draft)
from practices.models import CompanyMembership

from .intake_views import QuestionnaireView
from .views import StaffCompanyRequiredMixin

CORE_NOTE = 'The eligibility checks and the medical profile use this answer'
LOCKED_BLOCKS = (('Who you are', 'The app needs these'), ('Height and weight', 'Used for BMI'),
                 ('Before you submit', 'Wording comes from Policy versions'))


class CoreWordingForm(forms.Form):
    """A core question only has its wording changed."""

    label = forms.CharField(max_length=300)
    help_text = forms.CharField(max_length=500, required=False)

    def clean_label(self):
        return self.cleaned_data['label'].strip()


class SuperAdminView(View):
    """Runs after StaffCompanyRequiredMixin has set the practice and membership, and before any lookup."""

    def dispatch(self, request, *args, **kwargs):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can change the screening questions.')
        return super().dispatch(request, *args, **kwargs)

    def page(self, request, template, status=200, **context):
        return render(request, template, dict(
            company=self.company, active_membership=self.membership, nav_section='screening',
            page_title='Screening questions', **context), status=status)

    def draft_or_404(self):
        draft = draft_version(self.company)
        if draft is None:
            raise Http404('There is no draft.')
        return draft

    def wants_json(self, request):
        return 'application/json' in request.headers.get('Accept', '')

    def refused(self, request, error, *, to_editor=True):
        messages.error(request, ' '.join(error.messages))
        return redirect('portal:screening-draft' if to_editor and draft_version(self.company) else 'portal:screening-questions')


class ScreeningStaffView(LoginRequiredMixin, StaffCompanyRequiredMixin, SuperAdminView):
    pass


def decorate(question):
    """Template helpers on a question: flagged, core, movable, options as JSON."""
    question.flagged = any(isinstance(o, dict) and o.get('flag') for o in question.options)
    question.is_core = question.tier == ScreeningQuestion.Tier.CORE
    question.movable = question.key not in FIXED_CORE_KEYS
    question.options_json = json.dumps(question.options)
    return question


def row_html(request, question, selected=False):
    return render_to_string('portal/includes/screening_builder_row.html',
                            {'question': decorate(question), 'selected_key': question.key if selected else ''}, request=request)


def draft_url(key=''):
    return reverse('portal:screening-draft') + (f'?q={key}' if key else '')


@method_decorator(never_cache, name='dispatch')
class ScreeningOverviewView(ScreeningStaffView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        live = live_version(self.company)
        versions = list(ScreeningFormVersion.objects.filter(company=self.company).select_related('published_by')
                        .annotate(answered=Count('submissions')).order_by('-number'))
        draft = next((v for v in versions if v.status == ScreeningFormVersion.Status.DRAFT), None)
        return self.page(request, 'portal/screening_questions.html', live=live, draft=draft, versions=versions,
                         live_row=next(v for v in versions if v.pk == live.pk))


@method_decorator(never_cache, name='dispatch')
class ScreeningVersionView(ScreeningStaffView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, number):
        version = get_object_or_404(ScreeningFormVersion.objects.filter(company=self.company), number=number)
        questions = [decorate(q) for q in version.questions.all()]
        return self.page(request, 'portal/screening_version.html', version=version,
                         movable=[q for q in questions if q.movable], fixed=[q for q in questions if not q.movable],
                         locked_blocks=LOCKED_BLOCKS, core_note=CORE_NOTE)


@method_decorator(never_cache, name='dispatch')
class DraftStartView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request):
        try:
            start_draft(company=self.company, actor=request.user, request=request)
        except ValidationError as error:
            return self.refused(request, error, to_editor=False)
        return redirect('portal:screening-draft')


@method_decorator(never_cache, name='dispatch')
class DraftEditorView(ScreeningStaffView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        draft = self.draft_or_404()
        live = live_version(self.company)
        movable = [decorate(q) for q in movable_questions(draft)]
        fixed = [decorate(q) for q in draft.questions.filter(key__in=FIXED_CORE_KEYS).order_by('position')]
        keys = {q.key for q in movable + fixed}
        selected = request.GET.get('q') if request.GET.get('q') in keys else (movable[0].key if movable else '')
        return self.page(
            request, 'portal/screening_draft.html', draft=draft, live=live, movable=movable, fixed=fixed,
            selected_key=selected, summary=change_summary(draft, live), locked_blocks=LOCKED_BLOCKS, core_note=CORE_NOTE,
            answer_types=ScreeningQuestionForm.CUSTOM_TYPES)


@method_decorator(never_cache, name='dispatch')
class QuestionAddView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request):
        try:
            question = add_question(company=self.company, actor=request.user, request=request)
        except ValidationError as error:
            return self.refused(request, error)
        return redirect(draft_url(question.key))


@method_decorator(never_cache, name='dispatch')
class QuestionSaveView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def fail(self, request, key, status, errors):
        if self.wants_json(request):
            return JsonResponse({'ok': False, 'errors': errors}, status=status)
        messages.error(request, ' '.join(m for items in errors.values() for m in items))
        return redirect(draft_url(key) if draft_version(self.company) else 'portal:screening-questions')

    def post(self, request, key):
        draft = self.draft_or_404()
        question = get_object_or_404(draft.questions, key=key)
        if question.tier == ScreeningQuestion.Tier.CORE:
            form = CoreWordingForm(request.POST)
        else:
            form = ScreeningQuestionForm(request.POST, question=question)
        if not form.is_valid():
            return self.fail(request, key, 400, {f: [str(m) for m in errs] for f, errs in form.errors.items()})
        try:
            saved = save_question(question=question, actor=request.user, cleaned=form.cleaned_data, request=request)
        except ValidationError as error:
            return self.fail(request, key, 409, {'__all__': list(error.messages)})
        if self.wants_json(request):
            return JsonResponse({'ok': True, 'row_html': row_html(request, saved, selected=True)})
        messages.success(request, 'Question saved in the draft.')
        return redirect(draft_url(saved.key))


@method_decorator(never_cache, name='dispatch')
class QuestionRetireView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request, key):
        draft = self.draft_or_404()
        question = get_object_or_404(draft.questions, key=key)
        try:
            retire_question(question=question, actor=request.user, request=request)
        except ValidationError as error:
            return self.refused(request, error)
        messages.success(request, 'Question removed from the draft.')
        return redirect('portal:screening-draft')


@method_decorator(never_cache, name='dispatch')
class ReorderView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request):
        try:
            keys = json.loads(request.body or b'{}').get('keys') if 'json' in request.content_type else request.POST.getlist('keys')
        except (ValueError, AttributeError):
            keys = None
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return JsonResponse({'ok': False, 'errors': {'__all__': ['The order could not be read. Reload the page.']}}, status=400)
        try:
            reorder(company=self.company, actor=request.user, keys=keys, request=request)
        except ValidationError as error:
            return JsonResponse({'ok': False, 'errors': {'__all__': list(error.messages)}}, status=409)
        return JsonResponse({'ok': True})


@method_decorator(never_cache, name='dispatch')
class PublishView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request):
        try:
            version = publish_draft(company=self.company, actor=request.user, request=request)
        except ValidationError as error:
            return self.refused(request, error)
        messages.success(request, f'Version {version.number} is live. New enquiries get these questions from now on.')
        return redirect('portal:screening-questions')


@method_decorator(never_cache, name='dispatch')
class DiscardView(ScreeningStaffView):
    http_method_names = ('post', 'options')

    def post(self, request):
        try:
            discard_draft(company=self.company, actor=request.user, request=request)
        except ValidationError as error:
            return self.refused(request, error, to_editor=False)
        messages.success(request, 'Draft discarded.')
        return redirect('portal:screening-questions')


@method_decorator(xframe_options_sameorigin, name='dispatch')
@method_decorator(never_cache, name='dispatch')
class ScreeningPreviewView(ScreeningStaffView):
    """The patient form for one version, as the editor's preview: nothing posts and no lead is made."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, number):
        version = get_object_or_404(ScreeningFormVersion.objects.filter(company=self.company), number=number)
        form = EligibilityQuestionnaireForm(version=version, bound_practice=self.company, initial={'practice': self.company.pk})
        return render(request, 'portal/questionnaire.html', {
            'preview': True, 'form': form, 'editing': False, 'selected_practice_id': str(self.company.pk),
            'consent_documents_by_practice': [{'practice_id': self.company.pk, 'name': self.company.name,
                                               'documents': practice_notices(self.company)}],
            'screening_total': QuestionnaireView.screening_total(form), 'demo_screening': settings.DEBUG})
