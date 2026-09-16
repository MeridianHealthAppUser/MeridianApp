"""Single-practice deployment boundaries include forms, services and technical admin."""

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, override_settings
from django.urls import reverse

from care.models import AuditEvent, Lead, WeightEntry
from .management_forms import PracticeForm, StaffUserForm
from .management_services import add_staff_user, create_practice, manageable_practices, update_membership, update_practice
from .models import Company, CompanyMembership, Patient
from .test_management import ManagementFixture


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='alpha')
class SinglePracticeManagementTests(ManagementFixture):
    def test_manageable_practices_and_form_choices_are_pinned(self):
        self.assertEqual(list(manageable_practices(self.owner)), [self.alpha])
        form = StaffUserForm(actor=self.owner, company=self.alpha)
        self.assertTrue(form.fields['practices'].widget.is_hidden)
        self.assertEqual(list(form.fields['practices'].queryset), [self.alpha])
        forged = StaffUserForm(self.new_staff_data(practices=[self.alpha.pk, self.beta.pk]), actor=self.owner, company=self.alpha)
        self.assertFalse(forged.is_valid())
        self.assertIn('practices', forged.errors)

    def test_practice_pages_get_and_forged_posts_are_disabled(self):
        self.login()
        routes = [reverse('portal:management-practices'), reverse('portal:management-practice-create'),
                  reverse('portal:management-practice-edit', args=[self.alpha.pk]),
                  reverse('portal:management-practice-edit', args=[self.beta.pk])]
        for url in routes:
            for method in ('get', 'post'):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url, {'name': 'Changed', 'slug': 'changed'}).status_code, 403)
        self.assertEqual(Company.objects.count(), 3)
        self.assertFalse(AuditEvent.objects.exists())
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.slug, 'alpha')

    def test_direct_practice_create_update_and_form_validation_cannot_bypass_mode(self):
        with self.assertRaises(PermissionDenied):
            create_practice(actor=self.owner, source_company=self.alpha, name='New', slug='new')
        with self.assertRaises(PermissionDenied):
            update_practice(actor=self.owner, company=self.alpha, name='New', slug='new')
        with self.assertRaises(PermissionDenied):
            PracticeForm({'name': 'New', 'slug': 'new'}).is_valid()
        self.assertEqual(Company.objects.count(), 3)
        self.assertFalse(AuditEvent.objects.exists())

    def test_users_and_roles_remain_usable_only_in_current_practice(self):
        self.login()
        self.assertEqual(self.client.get(reverse('portal:management-users')).status_code, 200)
        token = self.token('portal:management-user-create')
        response = self.client.post(reverse('portal:management-user-create'), self.new_staff_data(management_context=token))
        self.assertRedirects(response, reverse('portal:management-users'))
        user = get_user_model().objects.get(email='new.staff@example.test')
        self.assertEqual(list(user.company_memberships.values_list('company_id', flat=True)), [self.alpha.pk])
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(Lead.objects.count(), 0)
        token = self.token('portal:management-membership-edit', [self.doctor_alpha.pk])
        response = self.client.post(reverse('portal:management-membership-edit', args=[self.doctor_alpha.pk]),
                                    {'role': 'practice_admin', 'is_active': True, 'management_context': token})
        self.assertRedirects(response, reverse('portal:management-users'))
        self.doctor_alpha.refresh_from_db()
        self.doctor_beta.refresh_from_db()
        self.assertEqual(self.doctor_alpha.role, 'practice_admin')
        self.assertEqual(self.doctor_beta.role, 'doctor')

    def test_forged_hidden_practice_staff_creation_or_membership_edit_writes_nothing(self):
        self.login()
        token = self.token('portal:management-user-create')
        response = self.client.post(reverse('portal:management-user-create'),
                                    self.new_staff_data(practices=[self.beta.pk], management_context=token))
        self.assertEqual(response.status_code, 200)
        self.assertIn('practices', response.context['form'].errors)
        with self.assertRaises(PermissionDenied):
            add_staff_user(actor=self.owner, source_company=self.alpha, companies=[self.alpha, self.beta],
                           mode='create', email='forged@example.test', role='doctor', first_name='Forged', last_name='User',
                           password='Fresh-Forest!7Clouds')
        with self.assertRaises(PermissionDenied):
            update_membership(actor=self.owner, company=self.beta, membership=self.doctor_beta,
                              role='super_admin', is_active=True)
        self.assertEqual(self.client.get(reverse('portal:management-membership-edit', args=[self.doctor_beta.pk])).status_code, 404)
        self.assertEqual(get_user_model().objects.count(), 4)
        self.assertFalse(AuditEvent.objects.exists())

    @override_settings(SINGLE_PRACTICE_SLUG='missing-configured-practice')
    def test_missing_pinned_practice_does_not_fall_back_to_another_membership(self):
        self.login()
        self.assertFalse(manageable_practices(self.owner).exists())
        self.assertEqual(self.client.get(reverse('portal:management-users')).status_code, 403)

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_flag_restores_existing_management_without_modifying_records(self):
        self.assertEqual(set(manageable_practices(self.owner)), {self.alpha, self.beta})
        self.login()
        self.assertEqual(self.client.get(reverse('portal:management-practices')).status_code, 200)
        self.assertFalse(StaffUserForm(actor=self.owner, company=self.alpha).fields['practices'].widget.is_hidden)


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='alpha')
class SinglePracticeTechnicalAdminTests(ManagementFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.technical = get_user_model().objects.create_superuser('technical@example.test', 'Synthetic-Admin!7Cloud')
        cls.hidden_user = get_user_model().objects.create_user('hidden.identity@example.test', 'Synthetic-User!7Cloud')
        CompanyMembership.objects.create(company=cls.beta, user=cls.hidden_user, role='doctor')
        cls.hidden_patient = Patient.objects.create(company=cls.beta, user=cls.hidden_user, first_name='Hidden', last_name='PatientSentinel')
        cls.visible_lead = Lead.objects.create(company=cls.alpha, first_name='Visible', last_name='Lead')
        cls.hidden_lead = Lead.objects.create(company=cls.beta, first_name='Hidden', last_name='LeadSentinel')

    def setUp(self):
        self.client.force_login(self.technical)
        self.request = RequestFactory().get('/admin/')
        self.request.user = self.technical

    def test_company_management_disabled_including_direct_admin_saves(self):
        model_admin = admin.site._registry[Company]
        for name, args in [('admin:practices_company_changelist', []), ('admin:practices_company_add', []),
                           ('admin:practices_company_change', [self.alpha.pk]), ('admin:practices_company_change', [self.beta.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
        self.assertFalse(model_admin.has_module_permission(self.request))
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, Company(name='Forbidden', slug='forbidden'), None, False)
        self.assertEqual(Company.objects.count(), 3)

    def test_patient_lead_membership_and_user_lists_hide_other_practice(self):
        for model, expected, hidden in [(Patient, self.patient, self.hidden_patient),
                                        (Lead, self.visible_lead, self.hidden_lead),
                                        (CompanyMembership, self.owner_alpha, self.owner_beta)]:
            model_admin = admin.site._registry[model]
            self.assertTrue(model_admin.get_queryset(self.request).filter(pk=expected.pk).exists())
            self.assertFalse(model_admin.get_queryset(self.request).filter(pk=hidden.pk).exists())
            self.assertFalse(model_admin.has_view_permission(self.request, hidden))
            response = self.client.get(reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_changelist'))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.beta.name)
        user_admin = admin.site._registry[get_user_model()]
        self.assertTrue(user_admin.get_queryset(self.request).filter(pk=self.technical.pk).exists())
        self.assertTrue(user_admin.get_queryset(self.request).filter(pk=self.patient_user.pk).exists())
        self.assertFalse(user_admin.get_queryset(self.request).filter(pk=self.hidden_user.pk).exists())
        self.assertFalse(user_admin.has_change_permission(self.request, self.hidden_user))
        self.assertFalse(user_admin.has_delete_permission(self.request, self.owner))

    def test_admin_detail_ids_and_autocomplete_cannot_reveal_hidden_records(self):
        for model, obj in [(Lead, self.hidden_lead), (Patient, self.hidden_patient), (get_user_model(), self.hidden_user)]:
            response = self.client.get(reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_change', args=[obj.pk]))
            self.assertIn(response.status_code, (302, 403, 404))
        response = self.client.get(reverse('admin:autocomplete'), {'app_label': 'care', 'model_name': 'weightentry', 'field_name': 'patient', 'term': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['id'] for row in response.json()['results']}, {str(self.patient.pk)})

    def test_related_form_choices_and_direct_saves_reject_hidden_company_or_patient(self):
        model_admin = admin.site._registry[WeightEntry]
        form_class = model_admin.get_form(self.request)
        self.assertEqual(list(form_class.base_fields['company'].queryset), [self.alpha])
        self.assertEqual(list(form_class.base_fields['patient'].queryset), [self.patient])
        self.assertNotIn('company', model_admin.get_autocomplete_fields(self.request))
        entry = WeightEntry(company=self.beta, patient=self.hidden_patient, weight_kg='90', recorded_on='2026-09-15')
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, entry, None, False)
        entry.company = self.alpha
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, entry, None, False)
        self.assertFalse(WeightEntry.objects.exists())
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Lead].delete_queryset(self.request, Lead.objects.all())
        self.assertEqual(Lead.objects.count(), 2)

    def test_historical_admin_activity_does_not_expose_hidden_object_names(self):
        LogEntry.objects.create(user=self.technical, content_type=ContentType.objects.get_for_model(Lead),
                                object_id=str(self.hidden_lead.pk), object_repr='Hidden admin log sentinel', action_flag=CHANGE)
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Hidden admin log sentinel')
        self.assertFalse(admin.site.get_log_entries(self.request).exists())
        with self.settings(MULTI_PRACTICE_ENABLED=True):
            self.assertTrue(admin.site.get_log_entries(self.request).exists())
            self.assertTrue(admin.site._registry[Lead].get_queryset(self.request).filter(pk=self.hidden_lead.pk).exists())
