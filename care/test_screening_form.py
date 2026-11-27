from django.conf import settings
from django.test import TestCase

from care.forms import EligibilityQuestionnaireForm, custom_field_name
from care.models import ScreeningFormVersion, ScreeningQuestion
from care.screening import live_version
from practices.models import Company

AT = ScreeningQuestion.AnswerType


class ScreeningFormTests(TestCase):
    def setUp(self):
        self.company = Company.objects.get_or_create(slug=settings.SINGLE_PRACTICE_SLUG, defaults={'name': 'Form Clinic'})[0]
        self.v1 = live_version(self.company)

    def custom(self, version, key, answer_type, position, **kwargs):
        return ScreeningQuestion.objects.create(
            company=version.company, version=version, key=key, tier=ScreeningQuestion.Tier.CUSTOM,
            answer_type=answer_type, label=kwargs.pop('label', f'Question {key}'), position=position, **kwargs)

    def v2(self):
        self.v1.status = ScreeningFormVersion.Status.SUPERSEDED
        self.v1.save()
        version = ScreeningFormVersion.objects.create(
            company=self.company, number=2, status=ScreeningFormVersion.Status.PUBLISHED)
        for q in self.v1.questions.all():
            q.pk = None
            q.version = version
            q.save()
        return version

    def valid_data(self, **extra):
        data = {
            'practice': self.company.pk, 'submission_token': 'x', 'first_name': 'A', 'last_name': 'B',
            'email': 'a@example.com', 'phone': '0821234567', 'id_number': '123', 'height_cm': '170', 'weight_kg': '90',
            'adult': 'yes', 'weight_related_condition': 'no', 'pregnancy': 'no', 'thyroid_history': 'no',
            'pancreatitis': 'no', 'service_consent': 'on',
        }
        data.update(extra)
        return data

    def form(self, version, **data):
        return EligibilityQuestionnaireForm(self.valid_data(**data), version=version)

    def test_version_1_labels_and_help_come_from_the_version(self):
        q = self.v1.questions.get(key='pancreatitis')
        q.label = 'Pancreatitis ever?'
        q.help_text = 'Inflamed pancreas.'
        q.save()
        original = EligibilityQuestionnaireForm.base_fields['pancreatitis']
        form = EligibilityQuestionnaireForm(version=self.v1)
        self.assertEqual(form.fields['pancreatitis'].label, 'Pancreatitis ever?')
        self.assertEqual(form.fields['pancreatitis'].help_text, 'Inflamed pancreas.')
        self.assertEqual(form.fields['pancreatitis'].choices, original.choices)
        self.assertTrue(form.fields['pancreatitis'].required)
        self.assertNotEqual(original.label, 'Pancreatitis ever?')  # class-level fields untouched

    def test_none_version_keeps_code_labels_and_adds_no_custom_fields(self):
        form = EligibilityQuestionnaireForm(version=None)
        self.assertEqual(form.fields['pancreatitis'].label, 'Have you ever had pancreatitis?')
        self.assertEqual(form.screening_items, [])
        self.assertFalse([n for n in form.fields if n.startswith('custom_')])

    def test_items_follow_position_and_fixed_items_are_keyed(self):
        version = self.v2()
        self.custom(version, 'c_a', AT.YES_NO, 99)
        form = EligibilityQuestionnaireForm(version=version)
        keys = [q.key for q, _ in form.screening_items]
        self.assertEqual(keys[-1], 'c_a')
        self.assertEqual(set(keys[:-1]), {'adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis'})
        self.assertEqual(set(form.fixed_items), {'health_context', 'heard_from'})
        question, bound = form.screening_items[-1]
        self.assertEqual(bound.name, 'custom_c_a')

    def test_each_custom_type_validates(self):
        version = self.v2()
        self.custom(version, 'c_yn', AT.YES_NO, 10, required=True, options=[{'value': 'yes', 'label': 'Yes'}, {'value': 'no', 'label': 'No'}])
        self.custom(version, 'c_one', AT.SINGLE, 11, options=[{'value': 'a', 'label': 'A'}, {'value': 'b', 'label': 'B'}])
        self.custom(version, 'c_many', AT.MULTIPLE, 12, options=[{'value': 'a', 'label': 'A'}, {'value': 'b', 'label': 'B'}])
        self.custom(version, 'c_txt', AT.SHORT_TEXT, 13)
        self.custom(version, 'c_num', AT.NUMBER, 14, number_min=18, number_max=99)
        good = dict(custom_c_yn='yes', custom_c_one='a', custom_c_many=['a', 'b'], custom_c_txt='hi', custom_c_num='40')
        form = self.form(version, **good)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_answers()['custom'],
                         {'c_yn': 'yes', 'c_one': 'a', 'c_many': ['a', 'b'], 'c_txt': 'hi', 'c_num': '40'})
        for bad in ({'custom_c_yn': ''}, {'custom_c_one': 'zzz'}, {'custom_c_txt': 'x' * 201}, {'custom_c_num': '17'},
                    {'custom_c_many': ['nope']}):
            with self.subTest(bad=bad):
                self.assertFalse(self.form(version, **{**good, **bad}).is_valid())

    def test_number_answer_accepts_a_comma_and_is_saved_plainly(self):
        version = self.v2()
        self.custom(version, 'c_num', AT.NUMBER, 10, number_min=18, number_max=99)
        form = self.form(version, custom_c_num='72,5')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_answers()['custom'], {'c_num': '72.5'})
        sci = self.form(version, custom_c_num='2e1')
        if sci.is_valid():
            self.assertEqual(sci.cleaned_answers()['custom'], {'c_num': '20'})

    def test_long_text_and_optional_blanks(self):
        version = self.v2()
        self.custom(version, 'c_long', AT.LONG_TEXT, 10, required=False)
        self.custom(version, 'c_one', AT.SINGLE, 11, required=False, options=[{'value': 'a', 'label': 'A'}])
        self.custom(version, 'c_many', AT.MULTIPLE, 12, required=False, options=[{'value': 'a', 'label': 'A'}])
        self.custom(version, 'c_num', AT.NUMBER, 13, required=False)
        form = self.form(version)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_answers()['custom'], {'c_long': '', 'c_one': '', 'c_many': [], 'c_num': ''})
        self.assertFalse(self.form(version, custom_c_long='x' * 2001).is_valid())

    def test_cleaned_answers_keep_core_keys_and_add_custom(self):
        version = self.v2()
        self.custom(version, 'c_surgery', AT.YES_NO, 10, required=True, options=[{'value': 'yes', 'label': 'Yes'}, {'value': 'no', 'label': 'No'}])
        form = self.form(version, custom_c_surgery='yes')
        self.assertTrue(form.is_valid(), form.errors)
        answers = form.cleaned_answers()
        self.assertEqual(answers['adult'], 'yes')
        self.assertEqual(answers['bmi'], '31.14')
        self.assertEqual(answers['custom'], {'c_surgery': 'yes'})

    def test_custom_labels_are_escaped(self):
        version = self.v2()
        self.custom(version, 'c_x', AT.SHORT_TEXT, 10, label='<script>x</script>')
        html = str(EligibilityQuestionnaireForm(version=version)['custom_c_x'].label_tag())
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_custom_field_name(self):
        self.assertEqual(custom_field_name('c_x'), 'custom_c_x')
