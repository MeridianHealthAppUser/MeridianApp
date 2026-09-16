"""Practice-scoped task editing and free-form labels, using isolated records."""

from django.test import override_settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from care.models import AuditEvent, ClinicalNote, ClinicalTask, RecordTag, TaskTagAssignment
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class TaskManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Task Alpha Practice', slug='task-alpha')
        cls.other_company = Company.objects.create(name='Task Beta Practice', slug='task-beta')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='task-doctor@example.test', first_name='First doctor')
        cls.second_doctor = users.create_user(email='task-second@example.test', first_name='Second doctor')
        cls.admin = users.create_user(email='task-admin@example.test', first_name='Practice administrator')
        cls.super_admin = users.create_user(email='task-super@example.test', first_name='Practice super administrator')
        cls.foreign_staff = users.create_user(email='task-foreign@example.test')
        cls.inactive_staff = users.create_user(email='task-inactive@example.test', is_active=False)
        cls.revoked_staff = users.create_user(email='task-revoked@example.test')
        cls.patient_user = users.create_user(email='task-patient@example.test')
        for user, role, active in (
            (cls.doctor, CompanyMembership.Role.DOCTOR, True),
            (cls.second_doctor, CompanyMembership.Role.DOCTOR, True),
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN, True),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN, True),
            (cls.inactive_staff, CompanyMembership.Role.DOCTOR, True),
            (cls.revoked_staff, CompanyMembership.Role.DOCTOR, False),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, is_active=active)
        for user in (cls.doctor, cls.foreign_staff):
            CompanyMembership.objects.create(company=cls.other_company, user=user, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Task patient',
            assigned_doctor=cls.doctor,
        )
        cls.other_patient = Patient.objects.create(company=cls.other_company, first_name='Beta', last_name='Patient')
        cls.inactive_patient = Patient.objects.create(
            company=cls.company, first_name='Inactive', last_name='Patient', is_active=False,
        )
        cls.tag = RecordTag.objects.create(company=cls.company, name='Important')
        cls.foreign_tag = RecordTag.objects.create(company=cls.other_company, name='Beta-only label')
        cls.own_task = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Own patient task',
            assigned_to=cls.doctor, created_by=cls.admin,
        )
        cls.created_task = ClinicalTask.objects.create(
            company=cls.company, title='Delegated general task',
            assigned_to=cls.second_doctor, created_by=cls.doctor,
        )
        cls.other_task = ClinicalTask.objects.create(
            company=cls.company, title='Unrelated colleague task',
            assigned_to=cls.second_doctor, created_by=cls.admin,
        )
        cls.foreign_task = ClinicalTask.objects.create(
            company=cls.other_company, title='Other practice task', assigned_to=cls.doctor, created_by=cls.doctor,
        )
        cls.inactive_task = ClinicalTask.objects.create(
            company=cls.company, patient=cls.inactive_patient, title='Inactive patient task',
            assigned_to=cls.doctor, created_by=cls.doctor,
        )
        cls.note = ClinicalNote.objects.create(
            company=cls.company, patient=cls.patient, author=cls.doctor, body='Original signed clinical content.',
        )
        cls.foreign_note = ClinicalNote.objects.create(
            company=cls.other_company, patient=cls.other_patient, author=cls.doctor, body='Other practice note.',
        )

    def login(self, user=None, *, client=None):
        client = client or self.client
        client.force_login(user or self.doctor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def task_data(self, *, record=None, **overrides):
        response = self.client.get(self.edit_url(record) if record else reverse('portal:task-create'))
        token = response.context['workflow_context'] if response.status_code == 200 else ''
        return {
            'title': 'Arrange practice training', 'description': 'Keep this task note on errors.',
            'patient': '', 'assigned_to': self.doctor.pk, 'priority': ClinicalTask.Priority.NORMAL,
            'status': ClinicalTask.Status.OPEN, 'due_at': '', 'new_tag': '',
            'workflow_context': token,
            **overrides,
        }

    def edit_url(self, task=None):
        return reverse('portal:task-edit', args=[(task or self.own_task).pk])

    def note_tags_url(self, note=None, patient=None):
        note = note or self.note
        return reverse('portal:patient-note-tags', args=[(patient or note.patient).pk, note.pk])

    def test_create_and_edit_require_login_and_reject_patient_role(self):
        urls = (reverse('portal:task-create'), self.edit_url())
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)
        self.login(self.patient_user)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, self.task_data()).status_code, 403)

    def test_every_active_staff_role_can_open_task_create_page(self):
        for user in (self.doctor, self.admin, self.super_admin):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(reverse('portal:task-create')).status_code, 200)

    def test_create_general_task_signs_creator_and_company_on_server(self):
        self.login()
        response = self.client.post(reverse('portal:task-create'), self.task_data(
            company=self.other_company.pk, created_by=self.second_doctor.pk,
            assigned_to=self.admin.pk, new_tag='  Needs a call back  ', tags=[self.tag.pk],
        ))
        self.assertEqual(response.status_code, 302)
        task = ClinicalTask.objects.get(title='Arrange practice training')
        self.assertIsNone(task.patient_id)
        self.assertEqual((task.company_id, task.created_by_id, task.assigned_to_id),
                         (self.company.pk, self.doctor.pk, self.admin.pk))
        self.assertEqual(set(task.tags.values_list('name', flat=True)), {'Important', 'Needs a call back'})
        event = AuditEvent.objects.get(action='task.created', target_id=str(task.pk))
        self.assertEqual((event.actor_id, event.company_id, event.patient_id),
                         (self.doctor.pk, self.company.pk, None))
        self.assertNotIn(task.description, str(event.metadata))

    def test_patient_task_can_be_created_from_tasks_page(self):
        self.login()
        response = self.client.post(reverse('portal:task-create'), self.task_data(patient=self.patient.pk))
        self.assertEqual(response.status_code, 302)
        task = ClinicalTask.objects.get(title='Arrange practice training')
        self.assertEqual(task.patient_id, self.patient.pk)

    def test_task_create_rejects_cross_practice_and_inactive_relations_without_writes(self):
        self.login()
        initial_count = ClinicalTask.objects.count()
        for field, value in (
            ('patient', self.other_patient.pk), ('patient', self.inactive_patient.pk),
            ('assigned_to', self.foreign_staff.pk), ('assigned_to', self.inactive_staff.pk),
            ('assigned_to', self.revoked_staff.pk), ('assigned_to', self.patient_user.pk),
            ('tags', [self.foreign_tag.pk]),
        ):
            with self.subTest(field=field, value=value):
                response = self.client.post(reverse('portal:task-create'), self.task_data(**{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Keep this task note on errors.')
                self.assertEqual(ClinicalTask.objects.count(), initial_count)
        self.assertFalse(AuditEvent.objects.filter(action='task.created').exists())

    def test_doctor_lists_assigned_and_created_tasks_in_a_table(self):
        self.login()
        response = self.client.get(reverse('portal:staff-tasks'), {'status': 'all'})
        self.assertEqual({task.pk for task in response.context['tasks']},
                         {self.own_task.pk, self.created_task.pk})
        self.assertContains(response, '<table', html=False)
        self.assertContains(response, 'General')
        self.assertNotContains(response, self.foreign_task.title)
        self.assertNotContains(response, self.inactive_task.title)

    def test_administrators_list_every_active_patient_and_general_task_in_practice(self):
        for user in (self.admin, self.super_admin):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(reverse('portal:staff-tasks'), {'status': 'all'})
                self.assertEqual({task.pk for task in response.context['tasks']},
                                 {self.own_task.pk, self.created_task.pk, self.other_task.pk})

    def test_task_type_and_freeform_tag_filters_compose(self):
        TaskTagAssignment.objects.create(company=self.company, task=self.created_task, tag=self.tag)
        self.login()
        response = self.client.get(reverse('portal:staff-tasks'), {'kind': 'general', 'tag': self.tag.pk})
        self.assertEqual([task.pk for task in response.context['tasks']], [self.created_task.pk])
        response = self.client.get(reverse('portal:staff-tasks'), {'kind': 'patient', 'tag': self.tag.pk})
        self.assertEqual(list(response.context['tasks']), [])
        response = self.client.get(reverse('portal:staff-tasks'), {'kind': 'patient'})
        self.assertEqual([task.pk for task in response.context['tasks']], [self.own_task.pk])

    def test_invalid_type_and_foreign_tag_filters_do_not_fall_back_to_all_tasks(self):
        self.login()
        for field, value in (('kind', 'unsupported-type'), ('tag', self.foreign_tag.pk)):
            with self.subTest(field=field):
                response = self.client.get(reverse('portal:staff-tasks'), {field: value})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['filter_form'].errors)
                self.assertEqual(list(response.context['tasks']), [])

    def test_doctor_can_edit_assigned_or_created_task_but_not_unrelated_tasks(self):
        self.login()
        for task in (self.own_task, self.created_task):
            with self.subTest(task=task.title):
                self.assertEqual(self.client.get(self.edit_url(task)).status_code, 200)
        for task in (self.other_task, self.foreign_task, self.inactive_task):
            with self.subTest(task=task.title):
                self.assertEqual(self.client.get(self.edit_url(task)).status_code, 404)
                self.assertEqual(self.client.post(self.edit_url(task), self.task_data()).status_code, 404)

    def test_edit_can_reassign_and_label_without_spoofing_original_creator(self):
        self.login()
        response = self.client.post(self.edit_url(), self.task_data(
            record=self.own_task,
            title=self.own_task.title, patient=self.patient.pk, assigned_to=self.second_doctor.pk,
            created_by=self.doctor.pk, new_tag='Patient prefers calls',
        ))
        self.assertEqual(response.status_code, 302)
        self.own_task.refresh_from_db()
        self.assertEqual(self.own_task.created_by_id, self.admin.pk)
        self.assertEqual(self.own_task.assigned_to_id, self.second_doctor.pk)
        self.assertEqual(list(self.own_task.tags.values_list('name', flat=True)), ['Patient prefers calls'])
        self.assertTrue(AuditEvent.objects.filter(action='task.updated', target_id=str(self.own_task.pk)).exists())

    def test_general_task_can_be_completed_and_reopened(self):
        self.login()
        response = self.client.post(reverse('portal:task-complete', args=[self.created_task.pk]), {'return_to': 'tasks'})
        self.assertEqual(response.status_code, 302)
        self.created_task.refresh_from_db()
        self.assertEqual(self.created_task.status, ClinicalTask.Status.DONE)
        self.assertIsNotNone(self.created_task.completed_at)
        response = self.client.post(self.edit_url(self.created_task), self.task_data(
            record=self.created_task,
            title=self.created_task.title, assigned_to=self.second_doctor.pk, status=ClinicalTask.Status.IN_PROGRESS,
        ))
        self.assertEqual(response.status_code, 302)
        self.created_task.refresh_from_db()
        self.assertEqual(self.created_task.status, ClinicalTask.Status.IN_PROGRESS)
        self.assertIsNone(self.created_task.completed_at)

    def test_invalid_edits_preserve_note_and_existing_record(self):
        self.login()
        original_title = self.own_task.title
        for field, value in (('status', 'made_up_status'), ('new_tag', 'x' * 65)):
            with self.subTest(field=field):
                response = self.client.post(self.edit_url(), self.task_data(record=self.own_task, **{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Keep this task note on errors.')
                self.own_task.refresh_from_db()
                self.assertEqual(self.own_task.title, original_title)
        self.assertFalse(AuditEvent.objects.filter(action='task.updated').exists())

    def test_task_mutations_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(client=csrf_client)
        for url in (reverse('portal:task-create'), self.edit_url(),
                    reverse('portal:task-complete', args=[self.created_task.pk])):
            with self.subTest(url=url):
                self.assertEqual(csrf_client.post(url, self.task_data()).status_code, 403)

    def test_note_creation_reuses_freeform_tags_without_patient_visibility(self):
        self.login()
        response = self.client.post(reverse('portal:patient-note-create', args=[self.patient.pk]), {
            'note_type': 'consult', 'body': 'A tagged consultation note.', 'new_tag': ' IMPORTANT ',
            'company': self.other_company.pk, 'author': self.second_doctor.pk,
        })
        self.assertEqual(response.status_code, 302)
        note = ClinicalNote.objects.get(body='A tagged consultation note.')
        self.assertEqual((note.company_id, note.author_id), (self.company.pk, self.doctor.pk))
        self.assertEqual(list(note.tags.values_list('pk', flat=True)), [self.tag.pk])
        self.assertEqual(RecordTag.objects.for_company(self.company).count(), 1)
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-dashboard'))
        self.assertNotContains(response, note.body)
        self.assertNotContains(response, self.tag.name)

    def test_note_author_can_change_labels_without_changing_clinical_content(self):
        self.login()
        response = self.client.post(self.note_tags_url(), {
            'tags': [self.tag.pk], 'new_tag': 'Discuss at next visit',
            'body': 'Tampered text', 'author': self.second_doctor.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, 'Original signed clinical content.')
        self.assertEqual(self.note.author_id, self.doctor.pk)
        self.assertEqual(set(self.note.tags.values_list('name', flat=True)), {'Important', 'Discuss at next visit'})

    def test_only_author_doctor_may_edit_note_labels(self):
        for user in (self.second_doctor, self.admin, self.super_admin, self.patient_user):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertIn(self.client.post(self.note_tags_url(), {'tags': [self.tag.pk]}).status_code, (403, 404))
        self.assertFalse(self.note.tags.exists())

    def test_note_labels_are_scoped_to_note_patient_and_practice(self):
        self.login()
        self.assertEqual(self.client.post(self.note_tags_url(self.foreign_note), {'tags': [self.tag.pk]}).status_code, 404)
        wrong_patient = Patient.objects.create(company=self.company, first_name='Wrong', last_name='Record')
        self.assertEqual(self.client.post(self.note_tags_url(patient=wrong_patient), {'tags': [self.tag.pk]}).status_code, 404)
        response = self.client.post(self.note_tags_url(), {'tags': [self.foreign_tag.pk], 'new_tag': 'Keep this label'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Keep this label')
        self.assertFalse(self.note.tags.exists())
        self.assertFalse(RecordTag.objects.filter(name='Keep this label').exists())

    def test_note_tag_action_is_post_only_and_csrf_protected(self):
        self.login()
        self.assertEqual(self.client.get(self.note_tags_url()).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(client=csrf_client)
        self.assertEqual(csrf_client.post(self.note_tags_url(), {'tags': [self.tag.pk]}).status_code, 403)
