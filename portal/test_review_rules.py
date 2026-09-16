from django.test import override_settings
from django.urls import reverse
from django.test import TestCase

from care.models import ReviewRule

from . import test_treatment as treatment_fixtures


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ReviewRulePageTests(TestCase):
    setUpTestData = classmethod(treatment_fixtures.TreatmentPageTests.setUpTestData.__func__)
    login = treatment_fixtures.TreatmentPageTests.login

    def test_review_page_access_and_doctor_read_only(self):
        url = reverse('portal:treatment-review-rules')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.doctor)
        self.assertContains(self.client.get(url), 'Review planning rules')
        self.assertEqual(self.client.get(reverse('portal:treatment-review-rule-create')).status_code, 403)
        self.assertEqual(self.client.post(reverse('portal:treatment-review-default')).status_code, 403)

    def test_super_admin_create_edit_with_signed_context_and_practice_isolation(self):
        self.login(self.super_admin)
        url = reverse('portal:treatment-review-rule-create')
        page = self.client.get(url)
        self.assertContains(page, 'New review planning rule')
        values = dict(name='Review planning', product_category='other', review_interval_days=90, is_active='on')
        rejected = self.client.post(url, values)
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(ReviewRule.objects.exists())
        values['review_context'] = page.context['review_context']
        response = self.client.post(url, values)
        self.assertEqual(response.status_code, 302)
        rule = ReviewRule.objects.get()
        detail = reverse('portal:treatment-review-rule-detail', args=[rule.pk])
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.login(self.doctor)
        self.assertContains(self.client.get(detail), 'Read-only')
        self.assertEqual(self.client.post(detail, values).status_code, 403)
        self.login(self.doctor, self.beta)
        self.assertEqual(self.client.get(detail).status_code, 404)

    def test_review_defaults_require_signed_context(self):
        self.login(self.super_admin)
        url = reverse('portal:treatment-review-default')
        self.assertEqual(self.client.get(url).status_code, 405)
        page = self.client.get(reverse('portal:treatment-review-rules'))
        self.assertEqual(self.client.post(url, {'review_interval_days': 90}).status_code, 400)
        response = self.client.post(url, {'review_interval_days': 90, 'review_context': page.context['review_context']})
        self.assertEqual(response.status_code, 302)
