import importlib

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from care.forms import EligibilityQuestionnaireForm
from care.models import Lead, ScreeningFormVersion, ScreeningQuestion, ScreeningQuestionnaire
from care.screening import MOVABLE_CORE_KEYS, live_version
from care.screening_v1 import VERSION_1_NOTE, VERSION_1_QUESTIONS
from practices.models import Company


class ScreeningModelTests(TestCase):
    def test_one_draft_and_one_published_version_per_company(self):
        company = Company.objects.create(name='Alpha Screening', slug='alpha-screening')
        other = Company.objects.create(name='Beta Screening', slug='beta-screening')
        Status = ScreeningFormVersion.Status
        ScreeningFormVersion.objects.create(company=company, number=1, status=Status.DRAFT)
        ScreeningFormVersion.objects.create(company=company, number=2, status=Status.PUBLISHED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScreeningFormVersion.objects.create(company=company, number=3, status=Status.DRAFT)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScreeningFormVersion.objects.create(company=company, number=4, status=Status.PUBLISHED)
        ScreeningFormVersion.objects.create(company=company, number=5, status=Status.SUPERSEDED)
        ScreeningFormVersion.objects.create(company=company, number=6, status=Status.SUPERSEDED)
        ScreeningFormVersion.objects.create(company=other, number=1, status=Status.DRAFT)

    def test_question_keys_are_unique_within_a_version(self):
        company = Company.objects.create(name='Gamma Screening', slug='gamma-screening')
        v1 = ScreeningFormVersion.objects.create(company=company, number=1)
        v2 = ScreeningFormVersion.objects.create(company=company, number=2, status=ScreeningFormVersion.Status.SUPERSEDED)
        kwargs = dict(company=company, tier=ScreeningQuestion.Tier.CUSTOM,
                      answer_type=ScreeningQuestion.AnswerType.YES_NO, label='Smoker?', position=1)
        ScreeningQuestion.objects.create(version=v1, key='smoker', **kwargs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScreeningQuestion.objects.create(version=v1, key='smoker', **kwargs)
        ScreeningQuestion.objects.create(version=v2, key='smoker', **kwargs)


class VersionOneTests(TestCase):
    def test_live_version_creates_version_1_once(self):
        company = Company.objects.create(name='V1 Practice', slug='v1-practice')
        version = live_version(company)
        self.assertEqual((version.number, version.status, version.note), (1, 'published', VERSION_1_NOTE))
        rows = [(q.key, q.label, q.help_text, q.required, q.answer_type, q.tier, q.options, q.position)
                for q in version.questions.order_by('position')]
        expected = [(q['key'], q['label'], q['help_text'], q['required'], q['answer_type'], q['tier'], q['options'], q['position'])
                    for q in VERSION_1_QUESTIONS]
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows, expected)
        self.assertEqual({q.company_id for q in version.questions.all()}, {company.pk})
        self.assertEqual(live_version(company).pk, version.pk)
        self.assertEqual(ScreeningFormVersion.objects.filter(company=company).count(), 1)

    def test_live_version_fast_path_does_not_lock(self):
        company = Company.objects.create(name='Fast Practice', slug='fast-practice')
        version = live_version(company)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(live_version(company).pk, version.pk)
        self.assertEqual(len(ctx), 1)
        self.assertFalse(any('FOR UPDATE' in q['sql'] for q in ctx.captured_queries))

    def test_version_1_matches_todays_form(self):
        by_key = {q['key']: q for q in VERSION_1_QUESTIONS}
        for key in MOVABLE_CORE_KEYS:
            field = EligibilityQuestionnaireForm.base_fields[key]
            self.assertEqual([o['value'] for o in by_key[key]['options']], [v for v, _ in field.choices])
            self.assertEqual(by_key[key]['required'], field.required)
        # A point-in-time check of Version 1 against today's Lead.HEARD_FROM.
        self.assertEqual([(o['value'], o['label']) for o in by_key['heard_from']['options']], list(Lead.HEARD_FROM))

    def test_migration_copy_matches_module(self):
        migration = importlib.import_module('care.migrations.0071_screening_version_1')
        self.assertEqual(migration.VERSION_1_QUESTIONS, VERSION_1_QUESTIONS)
        self.assertEqual(migration.VERSION_1_NOTE, VERSION_1_NOTE)

    def test_migration_backfills_existing_questionnaires(self):
        migration = importlib.import_module('care.migrations.0071_screening_version_1')
        company = Company.objects.create(name='Backfill Practice', slug='backfill-practice')
        lead = Lead.objects.create(company=company, first_name='A', last_name='B', email='ab@example.com')
        questionnaire = ScreeningQuestionnaire.objects.create(company=company, lead=lead)
        migration.create_version_1(django_apps, None)
        questionnaire.refresh_from_db()
        self.assertEqual(questionnaire.form_version.number, 1)
        self.assertEqual(questionnaire.form_version.questions.count(), 7)
        before = ScreeningFormVersion.objects.count(), ScreeningQuestion.objects.count()
        migration.create_version_1(django_apps, None)
        self.assertEqual((ScreeningFormVersion.objects.count(), ScreeningQuestion.objects.count()), before)


class BuilderServiceTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.company = Company.objects.create(name='Builder Practice', slug='builder-practice')
        self.actor = get_user_model().objects.create_user(email='builder-admin@example.com', password='x')
        self.live = live_version(self.company)

    def _audit(self, action):
        from care.models import AuditEvent
        return AuditEvent.objects.filter(company=self.company, action=action)

    def _custom(self, label='Surgery?'):
        from care.screening import add_question, save_question
        row = add_question(company=self.company, actor=self.actor)
        from care.forms import ScreeningQuestionForm
        form = ScreeningQuestionForm({'answer_type': 'yes_no', 'label': label, 'options': '[]'}, question=row)
        self.assertTrue(form.is_valid(), form.errors)
        return save_question(question=row, actor=self.actor, cleaned=form.cleaned_data)

    def test_start_draft_copies_live_and_returns_existing_draft(self):
        from care.screening import start_draft
        draft = start_draft(company=self.company, actor=self.actor)
        self.assertEqual((draft.number, draft.status, draft.based_on_id, draft.created_by_id), (2, 'draft', self.live.pk, self.actor.pk))
        self.assertEqual([(q.key, q.position, q.options) for q in draft.questions.order_by('position')],
                         [(q.key, q.position, q.options) for q in self.live.questions.order_by('position')])
        self.assertNotEqual({q.pk for q in draft.questions.all()}, {q.pk for q in self.live.questions.all()})
        self.assertEqual(start_draft(company=self.company, actor=self.actor).pk, draft.pk)
        self.assertEqual(self._audit('screening.draft_started').count(), 1)

    def test_core_questions_only_take_label_and_help_text(self):
        from care.screening import save_question, start_draft
        draft = start_draft(company=self.company, actor=self.actor)
        row = draft.questions.get(key='adult')
        save_question(question=row, actor=self.actor, cleaned={
            'label': 'Are you an adult?', 'help_text': 'Help', 'answer_type': 'short_text', 'required': False,
            'options': [{'value': 'x', 'label': 'X', 'flag': True, 'reason': 'r'}]})
        row.refresh_from_db()
        self.assertEqual((row.label, row.help_text, row.answer_type, row.required), ('Are you an adult?', 'Help', 'yes_no', True))
        self.assertEqual([o['value'] for o in row.options], ['yes', 'no'])

    def test_custom_keys_are_never_reused(self):
        from care.screening import add_question, publish_draft, retire_question, save_question, start_draft
        from care.forms import ScreeningQuestionForm
        start_draft(company=self.company, actor=self.actor)
        row = self._custom()
        self.assertEqual(row.key, 'c_surgery')
        publish_draft(company=self.company, actor=self.actor)
        draft = start_draft(company=self.company, actor=self.actor)
        retire_question(question=draft.questions.get(key='c_surgery'), actor=self.actor)
        again = self._custom('Surgery?')
        self.assertEqual(again.key, 'c_surgery-2')

    def test_new_custom_key_from_label(self):
        from care.screening import new_custom_key
        self.assertEqual(new_custom_key(self.company, 'Surgery?'), 'c_surgery')
        self.assertLessEqual(len(new_custom_key(self.company, 'a' * 200)), 50)

    def test_saves_to_a_published_or_missing_draft_are_refused(self):
        from care.screening import add_question, discard_draft, publish_draft, save_question, start_draft
        start_draft(company=self.company, actor=self.actor)
        row = add_question(company=self.company, actor=self.actor)
        publish_draft(company=self.company, actor=self.actor)
        with self.assertRaises(ValidationError):
            save_question(question=row, actor=self.actor, cleaned={'label': 'x'})
        with self.assertRaises(ValidationError):
            add_question(company=self.company, actor=self.actor)
        start_draft(company=self.company, actor=self.actor)
        draft_row = ScreeningQuestion.objects.filter(version__status='draft', tier='custom').first()
        discard_draft(company=self.company, actor=self.actor)
        self.assertFalse(ScreeningQuestion.objects.filter(pk=draft_row.pk).exists())
        with self.assertRaises(ValidationError):
            save_question(question=draft_row, actor=self.actor, cleaned={'label': 'x'})

    def test_question_form_limits(self):
        from care.forms import ScreeningQuestionForm
        import json
        opts = lambda n, **kw: json.dumps([{'label': f'Opt {i}', 'flag': False, 'reason': '', **kw} for i in range(n)])
        base = {'answer_type': 'single', 'label': 'Q?'}
        self.assertFalse(ScreeningQuestionForm({**base, 'options': opts(1)}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'options': opts(13)}).is_valid())
        self.assertTrue(ScreeningQuestionForm({**base, 'options': opts(12)}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'options': json.dumps(
            [{'label': 'A', 'flag': True, 'reason': ''}, {'label': 'B'}])}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'label': 'x' * 301, 'options': opts(2)}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'help_text': 'x' * 501, 'options': opts(2)}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'options': json.dumps(
            [{'label': 'A' * 121}, {'label': 'B'}])}).is_valid())
        self.assertFalse(ScreeningQuestionForm({**base, 'options': 'not json'}).is_valid())
        text = ScreeningQuestionForm({'answer_type': 'short_text', 'label': 'Q?', 'options': opts(3, flag=True)})
        self.assertTrue(text.is_valid())
        self.assertEqual(text.cleaned_data['options'], [])
        yes_no = ScreeningQuestionForm({'answer_type': 'yes_no', 'label': 'Q?', 'options': json.dumps(
            [{'value': 'yes', 'label': 'Yep', 'flag': True, 'reason': 'Check'}])})
        self.assertTrue(yes_no.is_valid(), yes_no.errors)
        self.assertEqual([(o['value'], o['label'], o['flag']) for o in yes_no.cleaned_data['options']],
                         [('yes', 'Yep', True), ('no', 'No', False)])

    def test_26th_custom_question_is_refused(self):
        from care.screening import add_question, start_draft
        start_draft(company=self.company, actor=self.actor)
        for _ in range(25):
            add_question(company=self.company, actor=self.actor)
        with self.assertRaises(ValidationError):
            add_question(company=self.company, actor=self.actor)

    def test_renaming_an_answer_keeps_its_value(self):
        import json
        from care.forms import ScreeningQuestionForm
        from care.screening import add_question, save_question, start_draft
        start_draft(company=self.company, actor=self.actor)
        row = add_question(company=self.company, actor=self.actor)
        row.answer_type, row.options = 'single', [
            {'value': 'sleeve', 'label': 'Sleeve', 'flag': False, 'reason': ''},
            {'value': 'bypass', 'label': 'Bypass', 'flag': False, 'reason': ''}]
        row.save()
        form = ScreeningQuestionForm({'answer_type': 'single', 'label': 'Surgery', 'options': json.dumps([
            {'value': 'sleeve', 'label': 'Gastric sleeve'}, {'value': 'bypass', 'label': 'Bypass'},
            {'label': 'Band'}, {'label': 'Band'}])}, question=row)
        self.assertTrue(form.is_valid(), form.errors)
        row = save_question(question=row, actor=self.actor, cleaned=form.cleaned_data)
        self.assertEqual([(o['value'], o['label']) for o in row.options],
                         [('sleeve', 'Gastric sleeve'), ('bypass', 'Bypass'), ('band', 'Band'), ('band-2', 'Band')])

    def test_reorder_requires_exactly_the_movable_keys(self):
        from care.screening import movable_questions, reorder, start_draft
        draft = start_draft(company=self.company, actor=self.actor)
        keys = [q.key for q in movable_questions(draft)]
        with self.assertRaises(ValidationError):
            reorder(company=self.company, actor=self.actor, keys=keys[:-1])
        with self.assertRaises(ValidationError):
            reorder(company=self.company, actor=self.actor, keys=keys + ['health_context'])
        with self.assertRaises(ValidationError):
            reorder(company=self.company, actor=self.actor, keys=keys + [keys[0]])
        reorder(company=self.company, actor=self.actor, keys=list(reversed(keys)))
        self.assertEqual([q.key for q in movable_questions(draft)], list(reversed(keys)))
        self.assertEqual([q.position for q in movable_questions(draft)], [1, 2, 3, 4, 5])

    def test_publish_supersedes_and_records_changes(self):
        from care.screening import publish_draft, start_draft
        start_draft(company=self.company, actor=self.actor)
        self._custom('Surgery?')
        published = publish_draft(company=self.company, actor=self.actor)
        self.live.refresh_from_db()
        self.assertEqual((self.live.status, published.status, published.published_by_id), ('superseded', 'published', self.actor.pk))
        self.assertIsNotNone(published.published_at)
        event = self._audit('screening.published').get()
        self.assertEqual(event.metadata['number'], 2)
        self.assertEqual(event.metadata['changes']['added'], ['Surgery?'])
        self.assertFalse(event.metadata['changes']['reordered'])

    def test_change_summary(self):
        from care.screening import change_summary, movable_questions, save_question, start_draft
        draft = start_draft(company=self.company, actor=self.actor)
        save_question(question=draft.questions.get(key='adult'), actor=self.actor, cleaned={'label': 'Over 18?', 'help_text': ''})
        self._custom('Surgery?')
        summary = change_summary(draft, self.live)
        self.assertEqual(summary, {'added': ['Surgery?'], 'reworded': ['Over 18?'], 'retired': [], 'reordered': False})
        self.assertEqual(change_summary(self.live, draft)['retired'], ['Surgery?'])

    def test_publish_refuses_an_invalid_draft(self):
        from care.screening import add_question, publish_draft, start_draft
        start_draft(company=self.company, actor=self.actor)
        row = add_question(company=self.company, actor=self.actor)
        ScreeningQuestion.objects.filter(pk=row.pk).update(answer_type='single', options=[], label='Broken one')
        with self.assertRaisesMessage(ValidationError, 'Broken one'):
            publish_draft(company=self.company, actor=self.actor)
        self.assertEqual(ScreeningFormVersion.objects.get(company=self.company, number=2).status, 'draft')

    def test_other_company_cannot_touch_the_draft(self):
        from care.screening import save_question, start_draft
        other = Company.objects.create(name='Other Practice', slug='other-practice')
        draft = start_draft(company=self.company, actor=self.actor)
        row = draft.questions.get(key='adult')
        other_row = start_draft(company=other, actor=self.actor).questions.get(key='adult')
        save_question(question=other_row, actor=self.actor, cleaned={'label': 'Theirs'})
        row.refresh_from_db()
        self.assertEqual(row.label, 'Are you 18 or older?')
