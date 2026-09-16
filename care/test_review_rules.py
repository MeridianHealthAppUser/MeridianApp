from django.test import override_settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from .models import AuditEvent, ClinicalTask, LabRequest, PracticeSettings, ReviewRule, TreatmentAuthorization
from .review_rules import save_review_rule, update_review_default
from . import test_treatment as treatment_fixtures


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ReviewRuleServiceTests(TestCase):
    setUpTestData = classmethod(treatment_fixtures.TreatmentLifecycleTests.setUpTestData.__func__)
    authorization = treatment_fixtures.TreatmentLifecycleTests.authorization

    def rule(self, **overrides):
        values = dict(company=self.company, actor=self.super_admin, name='Annual planning', product_category='other',
                      review_interval_days=365, blood_tests_required=False, is_active=True)
        values.update(overrides)
        return save_review_rule(**values)

    def test_super_admin_can_create_update_and_deactivate_rule_without_clinical_writes(self):
        auth = self.authorization()
        before = TreatmentAuthorization.objects.filter(pk=auth.pk).values().get()
        rule = self.rule()
        changed = self.rule(rule=rule, expected_updated_at=rule.updated_at.isoformat(), review_interval_days=120, is_active=False)
        self.assertEqual((changed.review_interval_days, changed.is_active), (120, False))
        self.assertEqual(TreatmentAuthorization.objects.filter(pk=auth.pk).values().get(), before)
        self.assertFalse(ClinicalTask.objects.exists())
        self.assertFalse(LabRequest.objects.exists())

    def test_only_active_practice_super_admin_can_change_rules(self):
        for actor in (self.doctor, self.admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.rule(actor=actor)
        with self.assertRaises(PermissionDenied):
            self.rule(company=self.beta)

    def test_exact_replay_is_idempotent_and_stale_change_rejected(self):
        rule = self.rule()
        old = rule.updated_at.isoformat()
        count = AuditEvent.objects.count()
        self.assertEqual(self.rule().pk, rule.pk)
        self.assertEqual(AuditEvent.objects.count(), count)
        self.rule(rule=rule, expected_updated_at=old, review_interval_days=30)
        with self.assertRaises(ValidationError):
            self.rule(rule=rule, expected_updated_at=old, review_interval_days=60)

    def test_default_updates_only_operational_setting_not_existing_authorizations(self):
        auth = self.authorization()
        before = TreatmentAuthorization.objects.filter(pk=auth.pk).values().get()
        settings = PracticeSettings.objects.get(company=self.company)
        result = update_review_default(company=self.company, actor=self.super_admin, review_interval_days=90,
                                       expected_updated_at=settings.updated_at.isoformat())
        self.assertEqual(result.review_interval_days, 90)
        self.assertEqual(TreatmentAuthorization.objects.filter(pk=auth.pk).values().get(), before)
        with self.assertRaises(ValidationError):
            update_review_default(company=self.company, actor=self.super_admin, review_interval_days=60,
                                  expected_updated_at=settings.updated_at.isoformat())

    def test_invalid_rule_fields_do_not_create_record(self):
        for values in ({'name': ''}, {'product_category': 'invented'}, {'review_interval_days': 0}, {'is_active': 'yes'}):
            with self.assertRaises(ValidationError):
                self.rule(**values)
        self.assertFalse(ReviewRule.objects.exists())
