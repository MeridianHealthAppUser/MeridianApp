"""The public questionnaire shows, validates and saves against the screening version the patient was shown."""

from datetime import timedelta

from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import (
    ConsentDocument, Lead, PracticeSettings, ScreeningFormVersion, ScreeningQuestion, ScreeningQuestionnaire,
)
from care.screening import live_version
from portal.intake_views import TOKEN_SALT
from practices.models import Company

AT = ScreeningQuestion.AnswerType
EXPIRED = 'This questionnaire has expired or belongs to another browser. Reload it before submitting.'
CORE_ORDER = ('Are you 18 or older?', 'Do you have a weight-related condition?',
              'Are you pregnant, breastfeeding, or planning a pregnancy within a year?',
              'Any personal or family history of medullary thyroid cancer or MEN2?', 'Have you ever had pancreatitis?')
YES_NO = [{'value': 'yes', 'label': 'Yes', 'flag': False, 'reason': ''}, {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''}]


def add_consents(company):
    PracticeSettings.objects.get_or_create(company=company)
    for kind in (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH):
        ConsentDocument.objects.create(
            company=company, kind=kind, version=f'{company.slug}-{kind}-v1', title=f'{company.name} {kind}',
            body=f'{company.name} {kind} notice.', effective_from=timezone.localdate() - timedelta(days=1))


def publish_next(company, *, custom=(), reword=None, drop=()):
    """Supersede the live version with a copy plus `custom` question rows (key, label, position, required)."""
    old = live_version(company)
    old.status = ScreeningFormVersion.Status.SUPERSEDED
    old.save()
    new = ScreeningFormVersion.objects.create(
        company=company, number=old.number + 1, status=ScreeningFormVersion.Status.PUBLISHED, published_at=timezone.now())
    for question in old.questions.exclude(key__in=drop):
        question.pk = None
        question.version = new
        if reword and question.key in reword:
            question.label = reword[question.key]
        question.save()
    for key, label, position, required in custom:
        ScreeningQuestion.objects.create(
            company=company, version=new, key=key, tier=ScreeningQuestion.Tier.CUSTOM, answer_type=AT.YES_NO,
            label=label, position=position, required=required, options=YES_NO)
    return new


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class QuestionnaireVersionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        add_consents(cls.company)

    def page(self, **query):
        return self.client.get(reverse('portal:questionnaire'), query)

    def post_data(self, response, **changes):
        data = {
            'practice': self.company.pk, 'first_name': 'Nadia', 'last_name': 'Intake', 'email': 'n@example.test',
            'phone': '082 461 7730', 'id_number': 'P-DEMO-001', 'height_cm': '168', 'weight_kg': '94',
            'adult': 'yes', 'weight_related_condition': 'no', 'pregnancy': 'no', 'thyroid_history': 'no',
            'pancreatitis': 'no', 'health_context': 'None', 'service_consent': 'on',
            'submission_token': response.context['form']['submission_token'].value(),
        }
        data.update(changes)
        return data

    def test_version_1_page_shows_todays_questions_in_order(self):
        html = self.page().content.decode()
        positions = [html.index(label) for label in CORE_ORDER]
        positions += [html.index('Chronic medication and allergies'), html.index('How did you hear about us?')]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(self.page(), '0 of 5 answered')
        self.assertContains(self.page(), 'id="screening-q-adult"')
        self.assertContains(self.page(), 'data-screening-question="pancreatitis"')

    def test_custom_question_appears_and_is_saved(self):
        publish_next(self.company, custom=[('c_surgery', 'Any weight-loss surgery?', 6, True)])
        response = self.page()
        html = response.content.decode()
        self.assertGreater(html.index('Any weight-loss surgery?'), html.index('Have you ever had pancreatitis?'))
        self.assertContains(response, '0 of 6 answered')
        post = self.client.post(reverse('portal:questionnaire'), self.post_data(response, custom_c_surgery='yes'))
        self.assertRedirects(post, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        self.assertEqual(ScreeningQuestionnaire.objects.get().answers['custom']['c_surgery'], 'yes')

    def test_doctor_text_is_escaped(self):
        publish_next(self.company, custom=[('c_x', '<b>Bold</b>?', 6, True)])
        html = self.page().content.decode()
        self.assertIn('&lt;b&gt;Bold&lt;/b&gt;?', html)
        self.assertNotIn('<b>Bold</b>', html)

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_form_uses_the_chosen_practices_version(self):
        second = Company.objects.create(name='Second Clinic', slug='second-clinic')
        add_consents(second)
        publish_next(second, reword={'pancreatitis': 'Second clinic pancreatitis wording?'})
        self.assertContains(self.page(practice=second.pk), 'Second clinic pancreatitis wording?')
        first = self.page(practice=self.company.pk)
        self.assertContains(first, 'Have you ever had pancreatitis?')
        self.assertNotContains(first, 'Second clinic pancreatitis wording?')

    def test_submission_after_publish_saves_against_shown_version(self):
        response = self.page()
        publish_next(self.company, custom=[('c_surgery', 'Any weight-loss surgery?', 6, True)])
        post = self.client.post(reverse('portal:questionnaire'), self.post_data(response))
        self.assertRedirects(post, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        self.assertEqual(ScreeningQuestionnaire.objects.get().form_version.number, 1)

    def test_tampered_version_is_rejected(self):
        other = Company.objects.create(name='Other', slug='other')
        foreign = live_version(other)
        response = self.page()
        token = response.context['form']['submission_token'].value()
        payload = signing.loads(token, salt=TOKEN_SALT)
        payload['versions'][str(self.company.pk)] = foreign.pk
        tampered = signing.dumps(payload, salt=TOKEN_SALT, compress=True)
        post = self.client.post(reverse('portal:questionnaire'), self.post_data(response, submission_token=tampered))
        self.assertEqual(post.status_code, 200)
        self.assertContains(post, EXPIRED)
        self.assertFalse(Lead.objects.exists())

    def test_bad_token_rerenders_live_version_without_lead(self):
        response = self.page()
        post = self.client.post(reverse('portal:questionnaire'), self.post_data(response, submission_token='junk'))
        self.assertContains(post, EXPIRED)
        self.assertFalse(Lead.objects.exists())

    def test_edit_uses_live_version_and_drops_retired_answers(self):
        publish_next(self.company, custom=[('c_surgery', 'Any weight-loss surgery?', 6, True)])
        response = self.page()
        self.client.post(reverse('portal:questionnaire'), self.post_data(response, custom_c_surgery='yes'))
        questionnaire = ScreeningQuestionnaire.objects.get()
        self.assertEqual(questionnaire.form_version.number, 2)
        publish_next(self.company, drop=['c_surgery'])
        edit_url = reverse('portal:questionnaire') + '?edit=1'
        edit = self.client.get(edit_url)
        self.assertNotContains(edit, 'Any weight-loss surgery?')
        post = self.client.post(edit_url, self.post_data(edit))
        self.assertRedirects(post, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        self.assertEqual(ScreeningQuestionnaire.objects.count(), 1)
        questionnaire.refresh_from_db()
        self.assertEqual(questionnaire.form_version.number, 3)
        self.assertNotIn('c_surgery', questionnaire.answers['custom'])

    def test_edit_prefills_custom_answers_of_current_questions(self):
        publish_next(self.company, custom=[('c_surgery', 'Any weight-loss surgery?', 6, True)])
        response = self.page()
        self.client.post(reverse('portal:questionnaire'), self.post_data(response, custom_c_surgery='yes'))
        edit = self.client.get(reverse('portal:questionnaire') + '?edit=1')
        self.assertEqual(edit.context['form']['custom_c_surgery'].value(), 'yes')

    def test_preview_context_shows_banner_and_disables_submit(self):
        from django.template.loader import render_to_string
        from care.forms import EligibilityQuestionnaireForm
        form = EligibilityQuestionnaireForm(version=live_version(self.company))
        html = render_to_string('portal/questionnaire.html', {'form': form, 'preview': True, 'consent_documents_by_practice': []})
        self.assertIn('Preview, not submitted', html)
        self.assertIn('disabled', html.split('questionnaire-button')[1].split('>')[0])
        self.assertNotIn('csrfmiddlewaretoken', html)
