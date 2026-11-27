from django.db import IntegrityError, transaction
from django.test import TestCase

from care.models import ScreeningFormVersion, ScreeningQuestion
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
