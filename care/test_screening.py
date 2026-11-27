import importlib

from django.apps import apps as django_apps
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
