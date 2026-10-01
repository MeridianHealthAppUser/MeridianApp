import secrets
import uuid

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.availability import SAST
from care.checkout import (
    CONSULT_MINUTES, can_check_out, checkout_identity, complete_checkout, consultation_slots,
    is_test_code, quote_for,
)
from care.forms import EligibilityQuestionnaireForm
from care.intake import notice_fingerprint, practice_notices, save_intake
from care.models import Lead, PracticeSettings, ScreeningQuestionnaire
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY
from practices.tenancy import enabled_companies, multi_practice_enabled, scope_queryset
from .checkout_forms import CHECKOUT_SLOT_MAX_AGE, CheckoutDateForm, CheckoutForm, make_checkout_slot, read_checkout_slot


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
        return get_object_or_404(scope_queryset(Lead.objects.all()), pk=lead_id,
                                 company__is_active=True, submission_key__isnull=False)


class QuestionnaireView(PrivateIntakeView):
    http_method_names = ('get', 'post', 'head', 'options')

    def notice_context(self):
        return [{'practice_id': company.pk, 'name': company.name, 'documents': practice_notices(company)}
                for company in enabled_companies().order_by('name')]

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
            if not multi_practice_enabled() and requested is not None and requested not in choices:
                raise Http404('This practice is not available.')
            default = (enabled_companies().filter(slug='meridian-health').first()
                       if multi_practice_enabled() else enabled_companies().first())
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
            'can_continue': can_check_out(lead, questionnaire),
        }

    def get(self, request, *args, **kwargs):
        context = self.get_context()
        if context['lead'].converted_patient_id:
            return redirect('portal:patient-dashboard')
        return render(request, self.template_name, context)


class QuestionnaireCheckoutView(QuestionnaireResultView):
    """Book and pay for the initial consultation.

    No payment provider is connected: card and EFT cannot be submitted. Only the
    configured test code brings the total to R0 and runs the full conversion.
    """

    http_method_names = ('get', 'post', 'head', 'options')
    template_name = 'portal/questionnaire_checkout.html'
    FIRST_OPEN_DAY_SEARCH = 14

    def checkout_day(self, source):
        """The requested diary day, or the first of the next two weeks with a free time."""
        if source.get('date'):
            date_form = CheckoutDateForm({'date': source.get('date')})
            return date_form, (date_form.cleaned_data['date'] if date_form.is_valid() else None)
        today = timezone.localdate()
        company = self.lead.company
        for offset in range(self.FIRST_OPEN_DAY_SEARCH):
            day = today + timedelta(days=offset)
            if consultation_slots(company, day, limit=1):
                break
        else:
            day = today
        return CheckoutDateForm(initial={'date': day}), day

    def render_checkout(self, context, form):
        request = self.request
        lead = self.lead
        source = request.POST if request.method == 'POST' else request.GET
        date_form, day = self.checkout_day(source)
        owner = request.session.get(OWNER_KEY)
        selected = None
        if form.is_bound and form.data.get('slot'):
            try:
                selected = read_checkout_slot(form.data.get('slot'), lead, owner)
            except ValidationError:
                pass
        slots = []
        if day is not None and owner:
            for slot in consultation_slots(lead.company, day):
                slots.append({
                    'token': make_checkout_slot(lead, owner, slot), 'starts_at': slot.starts_at, 'clinician': slot.clinician,
                    'selected': selected == (slot.clinician_id, slot.starts_at),
                })
        code = form.data.get('discount_code', '') if form.is_bound else ''
        quote = quote_for(context['pricing'], code)
        identity = checkout_identity(lead, request.user)
        context.update(
            form=form, date_form=date_form, day=day, slots=slots, quote=quote, identity=identity,
            code_applied=bool(code.strip()) and is_test_code(code),
            pay_enabled=bool(quote and quote.total == 0 and slots and identity in ('new_account', 'signed_in')),
            consult_minutes=CONSULT_MINUTES,
            slot_expiry_minutes=CHECKOUT_SLOT_MAX_AGE // 60,
            login_url=f'{reverse("accounts:login")}?{urlencode({"next": request.get_full_path()})}',
        )
        return render(request, self.template_name, context)

    def checkout_context(self):
        context = self.get_context()
        self.lead = context['lead']
        if self.lead.converted_patient_id:
            return context, redirect('portal:patient-dashboard')
        if not context['can_continue']:
            return context, redirect('portal:questionnaire-result')
        return context, None

    def get(self, request, *args, **kwargs):
        context, response = self.checkout_context()
        if response:
            return response
        return self.render_checkout(context, CheckoutForm(lead=self.lead))

    def post(self, request, *args, **kwargs):
        context, response = self.checkout_context()
        if response:
            return response
        paying = request.POST.get('action') == 'pay'
        identity = checkout_identity(self.lead, request.user)
        form = CheckoutForm(request.POST, lead=self.lead, paying=paying, needs_password=identity == 'new_account')
        if not form.is_valid():
            return self.render_checkout(context, form)
        code = form.cleaned_data['discount_code']
        if code.strip() and not is_test_code(code):
            form.add_error('discount_code', 'This code is not valid.')
            return self.render_checkout(context, form)
        if not paying:
            return self.render_checkout(context, form)
        try:
            clinician_id, starts_at = read_checkout_slot(form.cleaned_data['slot'], self.lead, request.session.get(OWNER_KEY))
            result = complete_checkout(
                lead=self.lead, user=request.user, password=form.cleaned_data['password1'],
                clinician_id=clinician_id, starts_at=starts_at, code=code, request=request,
            )
        except ValidationError as error:
            if Lead.objects.filter(pk=self.lead.pk, converted_patient__isnull=False).exists():
                return redirect('portal:patient-dashboard')  # A concurrent submit already converted it.
            for message in error.messages:
                form.add_error(None, message)
            return self.render_checkout(context, form)
        if not request.user.is_authenticated:
            login(request, result.user)
        request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = result.patient.company_id
        when = timezone.localtime(result.appointment.starts_at, SAST)
        messages.success(request, (
            f'Your initial consultation with {result.appointment.clinician.full_name} is booked for '
            f'{when:%d %B %Y at %H:%M} SAST. The test code covered the fee, so no money was taken. '
            'Next, complete your medical profile.'
        ))
        return redirect('portal:patient-medical-profile')
