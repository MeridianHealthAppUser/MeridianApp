import uuid
from datetime import timedelta

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from care.checkout import can_check_out
from care.forms import EligibilityQuestionnaireForm
from care.intake import notice_fingerprint, practice_notices, save_intake, screening_outcome
from care.models import ConsentDocument, Lead, ScreeningFormVersion, ScreeningQuestion, ScreeningQuestionnaire
from care.screening import evaluate_flags, live_version
from practices.models import Company

AT = ScreeningQuestion.AnswerType
SURGERY_REASON = "Previous weight-loss surgery needs a clinician's review."
MEDS_REASON = 'This medicine needs a clinician’s review.'


class ScreeningIntakeFlagTests(TestCase):
    def setUp(self):
        self.company = Company.objects.get_or_create(slug=settings.SINGLE_PRACTICE_SLUG, defaults={'name': 'Flag Clinic'})[0]
        v1 = live_version(self.company)
        v1.status = ScreeningFormVersion.Status.SUPERSEDED
        v1.save()
        self.version = ScreeningFormVersion.objects.create(
            company=self.company, number=2, status=ScreeningFormVersion.Status.PUBLISHED)
        for q in v1.questions.all():
            q.pk = None
            q.version = self.version
            q.save()
        ScreeningQuestion.objects.create(
            company=self.version.company, version=self.version, key='c_surgery', tier=ScreeningQuestion.Tier.CUSTOM,
            answer_type=AT.YES_NO, label='Surgery?', position=90, options=[
                {'value': 'yes', 'label': 'Yes', 'flag': True, 'reason': SURGERY_REASON},
                {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''}])
        ScreeningQuestion.objects.create(
            company=self.version.company, version=self.version, key='c_meds', tier=ScreeningQuestion.Tier.CUSTOM,
            answer_type=AT.MULTIPLE, label='Medicines?', position=91, required=False, options=[
                {'value': 'a', 'label': 'A', 'flag': True, 'reason': MEDS_REASON},
                {'value': 'b', 'label': 'B', 'flag': True, 'reason': MEDS_REASON},
                {'value': 'c', 'label': 'C', 'flag': False, 'reason': ''}])

    def data(self, **extra):
        data = {
            'practice': self.company.pk, 'submission_token': 'x', 'first_name': 'A', 'last_name': 'B',
            'email': 'a@example.com', 'phone': '0821234567', 'id_number': '123', 'height_cm': '168', 'weight_kg': '94',
            'adult': 'yes', 'weight_related_condition': 'no', 'pregnancy': 'no', 'thyroid_history': 'no',
            'pancreatitis': 'no', 'service_consent': 'on', 'custom_c_surgery': 'no',
        }
        data.update(extra)
        return data

    def save(self, **extra):
        form = EligibilityQuestionnaireForm(self.data(**extra), version=self.version)
        self.assertTrue(form.is_valid(), form.errors)
        notices = notice_fingerprint(practice_notices(self.company))
        return save_intake(form=form, submission_key=uuid.uuid4(), expected_notices=notices)

    def questionnaire(self, lead):
        return ScreeningQuestionnaire.objects.get(lead=lead, stage=1)

    def test_evaluate_flags_lists_flagged_choices_only(self):
        flags = evaluate_flags(self.version, {'c_surgery': 'yes', 'c_meds': ['a', 'b', 'c']})
        self.assertEqual(flags, [
            {'key': 'c_surgery', 'answer': 'yes', 'reason': SURGERY_REASON},
            {'key': 'c_meds', 'answer': 'a', 'reason': MEDS_REASON},
            {'key': 'c_meds', 'answer': 'b', 'reason': MEDS_REASON},
        ])
        self.assertEqual(evaluate_flags(self.version, {'c_surgery': 'no', 'c_meds': ['c']}), [])
        self.assertEqual(evaluate_flags(self.version, {}), [])

    @override_settings(DEBUG=True)
    def test_debug_flag_refers_and_adds_reason_after_core_reasons(self):
        lead = self.save(adult='no', custom_c_surgery='yes')
        answers = self.questionnaire(lead).answers
        self.assertEqual(lead.screening_status, Lead.ScreeningStatus.REFERRED)
        self.assertEqual(answers['screening_reasons'][-1], SURGERY_REASON)
        self.assertEqual(len(answers['screening_reasons']), 2)
        self.assertTrue(lead.screening_flagged)

    @override_settings(DEBUG=False)
    def test_production_flag_keeps_pending_with_reason(self):
        for kind in (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH):
            ConsentDocument.objects.create(
                company=self.company, kind=kind, version=f'{kind}-v1', title=kind, body=f'{kind} notice.',
                effective_from=timezone.localdate() - timedelta(days=1))
        lead = self.save(custom_c_surgery='yes')
        answers = self.questionnaire(lead).answers
        self.assertEqual(lead.screening_status, Lead.ScreeningStatus.PENDING)
        self.assertEqual(answers['screening_reasons'], [SURGERY_REASON])
        self.assertTrue(lead.screening_flagged)

    @override_settings(DEBUG=True)
    def test_duplicate_reasons_are_listed_once(self):
        lead = self.save(custom_c_surgery='no', custom_c_meds=['a', 'b'])
        answers = self.questionnaire(lead).answers
        self.assertEqual(answers['screening_reasons'], [MEDS_REASON])
        self.assertEqual(len(answers['flags']), 2)

    def test_flags_never_decline(self):
        data = {'adult': 'yes', 'pregnancy': 'no', 'thyroid_history': 'no', 'pancreatitis': 'no',
                'weight_related_condition': 'no', 'weight_kg': 94, 'height_cm': 168}
        flags = [{'key': 'c', 'answer': 'yes', 'reason': 'r'}]
        for debug in (True, False):
            with override_settings(DEBUG=debug):
                for f in ((), flags):
                    self.assertNotEqual(screening_outcome(data, f)[0], Lead.ScreeningStatus.DECLINED)
                    self.assertNotEqual(screening_outcome({**data, 'adult': 'no'}, f)[0], Lead.ScreeningStatus.DECLINED)

    @override_settings(DEBUG=True)
    def test_saved_questionnaire_links_its_version(self):
        lead = self.save()
        questionnaire = self.questionnaire(lead)
        self.assertEqual(questionnaire.form_version, self.version)
        self.assertEqual(questionnaire.answers['form_version'], self.version.number)
        self.assertEqual(questionnaire.answers['flags'], [])
        self.assertFalse(lead.screening_flagged)

    @override_settings(DEBUG=True)
    def test_flagged_lead_cannot_check_out(self):
        lead = self.save(custom_c_surgery='yes')
        self.assertFalse(can_check_out(lead, self.questionnaire(lead)))
