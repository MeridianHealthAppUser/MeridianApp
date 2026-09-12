import secrets
import uuid

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.forms import EligibilityQuestionnaireForm
from care.intake import notice_fingerprint, practice_notices, save_intake
from care.models import Lead, PracticeSettings, ScreeningQuestionnaire
from practices.models import Company


TOKEN_SALT = 'meridian.public-intake.v1'
TOKEN_MAX_AGE = 24 * 60 * 60
OWNER_KEY = 'public_intake_owner'
LEAD_KEY = 'public_intake_lead_id'


@method_decorator(never_cache, name='dispatch')
class PrivateIntakeView(View):
    """Anonymous health information is session-owned and never cacheable."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        # Keep the same-origin form Origin/Referer usable by CSRF checks while
        # withholding the referrer from external sites. no-referrer can cause
        # browsers to submit Origin: null on the local HTTP development site.
        response['Referrer-Policy'] = 'same-origin'
        return response

    def owned_lead(self):
        lead_id = self.request.session.get(LEAD_KEY)
        if not lead_id:
            raise Http404('There is no questionnaire in this browser session.')
        return get_object_or_404(Lead, pk=lead_id, company__is_active=True, submission_key__isnull=False)


class QuestionnaireView(PrivateIntakeView):
    http_method_names = ('get', 'post', 'head', 'options')

    def notice_context(self):
        return [{'practice_id': company.pk, 'name': company.name, 'documents': practice_notices(company)}
                for company in Company.objects.filter(is_active=True).order_by('name')]

    def make_token(self, key, notices):
        owner = self.request.session.setdefault(OWNER_KEY, secrets.token_urlsafe(32))
        return signing.dumps({'key': str(key), 'owner': owner,
                              'notices': {str(item['practice_id']): notice_fingerprint(item['documents']) for item in notices}},
                             salt=TOKEN_SALT, compress=True)

    def render_form(self, form, *, editing, notices):
        selected = form['practice'].value()
        return render(self.request, 'portal/questionnaire.html', {
            'form': form, 'editing': editing, 'selected_practice_id': str(selected or ''),
            'consent_documents_by_practice': notices,
            'demo_screening': settings.DEBUG,
        })

    def get(self, request, *args, **kwargs):
        editing = request.GET.get('edit') == '1'
        lead = self.owned_lead() if editing else None
        notices = self.notice_context()
        initial = {}
        if lead:
            if lead.converted_patient_id or lead.stage in (Lead.Stage.CONVERTED, Lead.Stage.CLOSED):
                raise Http404('This enquiry is no longer editable.')
            initial.update({field: getattr(lead, field) for field in ('first_name', 'last_name', 'email', 'phone', 'id_number')})
            questionnaire = ScreeningQuestionnaire.objects.for_company(lead.company).filter(lead=lead, stage=1).first()
            if questionnaire:
                initial.update({key: value for key, value in questionnaire.answers.items() if key in EligibilityQuestionnaireForm.base_fields})
            initial.update(practice=lead.company_id, service_consent=False)
        else:
            choices = {str(item['practice_id']) for item in notices}
            requested = request.GET.get('practice')
            default = Company.objects.filter(slug='meridian-health', is_active=True).first()
            initial['practice'] = requested if requested in choices else default.pk if default else None
        initial['submission_token'] = self.make_token(lead.submission_key if lead else uuid.uuid4(), notices)
        form = EligibilityQuestionnaireForm(initial=initial, bound_practice=lead.company if lead else None)
        return self.render_form(form, editing=editing, notices=notices)

    def post(self, request, *args, **kwargs):
        editing = request.GET.get('edit') == '1'
        lead = self.owned_lead() if editing else None
        form = EligibilityQuestionnaireForm(request.POST, bound_practice=lead.company if lead else None)
        notices = self.notice_context()
        if form.is_valid():
            try:
                payload = signing.loads(form.cleaned_data['submission_token'], salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
                owner = request.session.get(OWNER_KEY)
                if not owner or not secrets.compare_digest(payload.get('owner', ''), owner):
                    raise signing.BadSignature('Different browser session.')
                submission_key = uuid.UUID(payload['key'])
                if lead and submission_key != lead.submission_key:
                    raise signing.BadSignature('Different enquiry.')
                expected = payload['notices'].get(str(form.cleaned_data['practice'].pk))
                saved = save_intake(form=form, submission_key=submission_key, expected_notices=expected, request=request)
            except (signing.BadSignature, ValueError, KeyError, TypeError):
                form.add_error(None, 'This questionnaire has expired or belongs to another browser. Reload it before submitting.')
            except ValidationError as error:
                form.add_error(None, ' '.join(error.messages))
            else:
                request.session[LEAD_KEY] = saved.pk
                return redirect('portal:questionnaire-result')
        return self.render_form(form, editing=editing, notices=notices)


class QuestionnaireResultView(PrivateIntakeView):
    http_method_names = ('get', 'head', 'options')
    template_name = 'portal/questionnaire_result.html'

    def get_context(self):
        lead = self.owned_lead()
        questionnaire = ScreeningQuestionnaire.objects.for_company(lead.company).filter(lead=lead, stage=1).first()
        return {
            'lead': lead, 'company': lead.company, 'questionnaire': questionnaire,
            'screening_reasons': questionnaire.answers.get('screening_reasons', []) if questionnaire else [],
            'pricing': PracticeSettings.objects.for_company(lead.company).first(),
            'demo_screening': settings.DEBUG,
            'can_continue': bool(settings.DEBUG and questionnaire and
                                 questionnaire.answers.get('screening_rules_version') == 'prototype-rev2.6-demo' and
                                 lead.screening_status == Lead.ScreeningStatus.CLEARED and
                                 lead.stage not in (Lead.Stage.CONVERTED, Lead.Stage.CLOSED) and not lead.converted_patient_id),
        }

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self.get_context())


class QuestionnaireCheckoutView(QuestionnaireResultView):
    """Pricing preview only: there is deliberately no payment/convert POST."""

    template_name = 'portal/questionnaire_checkout.html'

    def get(self, request, *args, **kwargs):
        context = self.get_context()
        if not context['can_continue']:
            return redirect('portal:questionnaire-result')
        return render(request, self.template_name, context)
