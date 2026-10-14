"""Patient tabs and legacy actions keep one patient context without stacked pages."""

from django.test import override_settings
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, BloodResult, ClinicalNote, ClinicalTask, MessageThread, PatientMessage, Payment
from practices.models import CompanyMembership
from . import test_workflows
from .patient_workspace import CLINICAL_TABS, TABS

# The tabs a doctor in the Clinician role sees on their own patient's record.
DOCTOR_TABS = [('overview', 'Overview'), ('blood-tests', 'Blood results'), ('messages', 'Messages')]


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientWorkspaceTests(TestCase):
    setUpTestData = classmethod(test_workflows.PortalWorkflowTests.setUpTestData.__func__)
    login = test_workflows.PortalWorkflowTests.login
    add_message = test_workflows.PortalWorkflowTests.add_message

    def path(self, patient=None):
        return reverse('portal:patient-detail', args=[(patient or self.patient).pk])

    def page(self, tab='overview', **query):
        return self.client.get(self.path(), {'tab': tab, **query})

    def overview(self):
        return f'{self.path()}?tab=overview'

    def login_doctor_super_admin(self):
        # A Super Admin who is also a doctor keeps every tab, with a clinician's forms.
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(clinician_type='doctor')
        self.login(self.super_admin)

    def test_each_authorized_tab_has_one_patient_heading_and_patient_bound_navigation(self):
        for actor in (self.doctor, self.administrator, self.super_admin):
            self.login(actor)
            for tab, _ in TABS:
                if actor == self.administrator and tab in CLINICAL_TABS:
                    continue
                with self.subTest(actor=actor.pk, tab=tab):
                    response = self.page(tab)
                    if actor == self.doctor and tab not in dict(DOCTOR_TABS):
                        # A doctor works from the Overview: the tabs they don't see send them there.
                        self.assertRedirects(response, self.overview(), fetch_redirect_response=False)
                        continue
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['patient'].pk, self.patient.pk)
                    self.assertEqual(response.context['workspace_tab'], tab)
                    self.assertContains(response, f'<h1>{self.patient}</h1>', count=1)
                    self.assertContains(response, f'data-patient-section="{tab}"', count=1)
                    for link in response.context['workspace_tabs']:
                        split = urlsplit(link['url'])
                        self.assertEqual(split.path, self.path())
                        self.assertEqual(parse_qs(split.query)['tab'], [link['name']])
                    self.assertEqual([link['name'] for link in response.context['workspace_tabs'] if link['active']], [tab])
                    if actor == self.doctor:
                        self.assertEqual([(link['name'], link['label']) for link in response.context['workspace_tabs']], DOCTOR_TABS)

    def test_overview_logs_general_notes_and_blood_results_but_no_conversations_and_the_messages_tab_waits_for_a_choice(self):
        self.login(self.doctor)
        incoming = self.add_message(body='UNREAD_PRIVATE_CONVERSATION_BODY')
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor, body='CLINICAL_NOTE_BODY')
        BloodResult.objects.create(company=self.company, patient=self.patient, taken_on=timezone.localdate(),
                                   lab_name='Lancet', results='RECORDED_BLOOD_RESULT', recorded_by=self.doctor)
        response = self.page()
        for private in ('UNREAD_PRIVATE_CONVERSATION_BODY', 'name="summary"', 'name="starts_at"', 'id="record-messages"', 'Choose a conversation'):
            self.assertNotContains(response, private)
        # A clinician's Overview is the clinical one: its log lists general notes and blood results, and Create note
        # adds a note there. Their conversations are on the Messages tab, where none opens (or is marked read)
        # until one is chosen.
        self.assertContains(response, 'data-record-overview')
        self.assertContains(response, 'CLINICAL_NOTE_BODY')
        self.assertContains(response, 'RECORDED_BLOOD_RESULT')
        self.assertContains(response, f'<form method="post" action="{reverse("portal:patient-note-create", args=[self.patient.pk])}">')
        self.assertContains(response, '<input type="hidden" name="return_to" value="overview">')
        self.assertContains(self.page('messages'), 'Choose a conversation')
        incoming.refresh_from_db()
        self.assertIsNone(incoming.staff_read_at)
        opened = self.page('messages', thread=incoming.thread_id)
        self.assertEqual(opened.context['record_thread'].pk, incoming.thread_id)
        incoming.refresh_from_db()
        self.assertIsNotNone(incoming.staff_read_at)

    def test_local_payments_tab_is_readonly_and_does_not_expose_provider_metadata(self):
        self.login(self.administrator)
        Payment.objects.create(company=self.company, patient=self.patient, amount='630.00', due_on=timezone.localdate(),
                               status='paid', provider_reference='PRIVATE_PROVIDER_REFERENCE', failure_reason='PRIVATE_PROVIDER_DIAGNOSTIC')
        Payment.objects.create(company=self.other_company, patient=self.other_practice_patient, amount='8888.88', due_on=timezone.localdate())
        before_users = get_user_model().objects.count()
        response = self.page('payments')
        self.assertContains(response, 'R630.00')
        self.assertContains(response, 'Live payment processing is not enabled')
        for hidden in ('8888.88', 'PRIVATE_PROVIDER_REFERENCE', 'PRIVATE_PROVIDER_DIAGNOSTIC'):
            self.assertNotContains(response, hidden)
        self.assertEqual(self.client.post(self.path() + '?tab=payments', {'paid': 'true'}).status_code, 405)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(get_user_model().objects.count(), before_users)

    def test_administrative_history_exposes_only_fixed_operational_labels(self):
        self.login(self.administrator)
        AuditEvent.objects.create(company=self.company, patient=self.patient, action='shipment.dispatched',
                                  metadata={'private': 'PRIVATE_AUDIT_META'}, ip_address='192.0.2.25')
        AuditEvent.objects.create(company=self.company, patient=self.patient, action='consultation.signed')
        response = self.page('history')
        self.assertContains(response, 'Delivery dispatched')
        self.assertContains(response, 'Administrative activity only')
        self.assertNotContains(response, 'PRIVATE_AUDIT_META')
        self.assertNotContains(response, '192.0.2.25')
        self.assertEqual(response.context['workspace_row_count'], 1)

    def test_appointments_are_paginated_without_changing_patient_or_tab(self):
        self.login_doctor_super_admin()
        Appointment.objects.bulk_create([Appointment(company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() + timedelta(days=index + 1), duration_minutes=30) for index in range(22)])
        response = self.page('appointments', status='booked')
        self.assertEqual(len(response.context['rows']), 20)
        next_url = response.context['workspace_next_url']
        self.assertEqual(urlsplit(next_url).path, self.path())
        self.assertEqual(parse_qs(urlsplit(next_url).query), {'tab': ['appointments'], 'status': ['booked'], 'page': ['2']})
        self.assertEqual(len(self.client.get(next_url).context['rows']), 2)
        invalid = self.page('appointments', status='invented')
        self.assertTrue(invalid.context['workspace_filter'].errors)
        self.assertEqual(invalid.context['rows'], [])

    def test_message_and_new_conversation_success_return_to_the_exact_patient_thread(self):
        self.login(self.doctor)
        response = self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]), {'body': 'Clinician reply'})
        self.assertRedirects(response, self.path() + f'?tab=messages&thread={self.thread.pk}', fetch_redirect_response=False)
        self.login(self.administrator)
        response = self.client.post(reverse('portal:staff-thread-create', args=[self.patient.pk]), {'subject': 'A new thread', 'body': 'New administrative message'})
        new_thread = MessageThread.objects.get(subject='A new thread')
        self.assertRedirects(response, self.path() + f'?tab=messages&thread={new_thread.pk}', fetch_redirect_response=False)
        self.assertEqual(new_thread.patient_id, self.patient.pk)
        # The administrator sees their own conversation, never the doctor's.
        threads = self.page('messages').context['record_threads']
        self.assertEqual([thread.pk for thread in threads], [new_thread.pk])
        # A Super Admin sees every conversation with the patient.
        self.login(self.super_admin)
        threads = self.page('messages').context['record_threads']
        self.assertEqual({thread.pk for thread in threads}, {self.thread.pk, new_thread.pk})

    def test_add_note_is_a_button_that_opens_the_note_form_in_a_modal(self):
        self.login_doctor_super_admin()
        response = self.page('notes')
        self.assertContains(response, f'href="{self.path()}?tab=notes&amp;update=note" data-modal-open="add-note" aria-haspopup="dialog">Add note</a>')
        self.assertContains(response, '<dialog class="app-dialog" id="add-note" aria-labelledby="add-note-title">')
        self.assertContains(response, '<legend>Free-form tags</legend>')
        self.assertContains(response, 'js/dialogs.js')
        self.assertNotContains(response, '<summary>Add a general clinical note</summary>')
        # Without scripts its link opens the form; after a mistake it opens again, with the error.
        opened = 'id="add-note" aria-labelledby="add-note-title" open>'
        self.assertContains(self.page('notes', update='note'), opened)
        failed = self.client.post(reverse('portal:patient-note-create', args=[self.patient.pk]), {'body': '', 'note_type': 'clinical'})
        self.assertContains(failed, opened)
        self.assertContains(failed, 'This field is required.')

    def test_create_task_is_a_button_that_opens_the_task_form_in_a_modal(self):
        self.login_doctor_super_admin()
        response = self.page('tasks')
        html = response.content.decode()
        button = f'href="{self.path()}?tab=tasks&amp;update=task" data-modal-open="create-task" aria-haspopup="dialog">Create task</a>'
        self.assertContains(response, button)
        self.assertLess(html.index('data-filter-menu'), html.index(button))
        self.assertContains(response, '<dialog class="app-dialog" id="create-task" aria-labelledby="create-task-title">')
        self.assertNotContains(response, '<summary>Create a task for this patient</summary>')
        opened = 'id="create-task" aria-labelledby="create-task-title" open>'
        self.assertContains(self.page('tasks', update='task'), opened)
        failed = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]),
                                  {'patient': self.patient.pk, 'title': '', 'description': 'Kept draft', 'priority': 'high'})
        self.assertContains(failed, opened)
        self.assertContains(failed, 'Kept draft')

    def test_history_filters_are_in_the_filters_menu(self):
        self.login_doctor_super_admin()
        response = self.page('history')
        self.assertContains(response, '<details class="filter-menu" data-filter-menu>')
        for label in ('Activity', 'Date'):
            self.assertContains(response, f'<span class="filter-menu__label">{label}</span>')
        self.assertContains(response, '<input type="radio" name="category" value="all" checked>')
        self.assertNotContains(response, 'class="record-filters"')
        response = self.page('history', category='clinical', date_from='2026-01-01')
        self.assertContains(response, '<input type="radio" name="category" value="clinical" checked>')
        self.assertContains(response, '<span class="filter-menu__badge">2<span class="sr-only"> applied</span></span>')
        self.assertContains(response, f'<a class="filter-menu__clear" href="{self.path()}?tab=history">Clear all</a>')
        # A wrong range opens the menu with the error, and shows no timeline.
        response = self.page('history', date_from='2026-02-01', date_to='2026-01-01')
        self.assertContains(response, '<details class="filter-menu" data-filter-menu open>')
        self.assertContains(response, 'The end date must be on or after the start date.')
        self.assertNotContains(response, 'data-record-timeline')

    def test_invalid_legacy_forms_render_only_their_own_tab_and_preserve_inputs(self):
        self.login_doctor_super_admin()
        incoming = self.add_message(body='Keep unread while validating another section')
        forms = (
            ('patient-task-create', 'tasks', {'patient': self.patient.pk, 'title': '', 'description': 'Retained task draft', 'priority': 'high'}, 'Retained task draft'),
            ('patient-note-create', 'notes', {'body': '', 'note_type': 'clinical'}, 'This field is required.'),
            ('patient-appointment-create', 'appointments', {'patient': self.patient.pk, 'clinician': self.doctor.pk, 'starts_at': 'invalid'}, 'This patient form is out of date'),
            ('staff-thread-create', 'messages', {'subject': '', 'body': 'Retained new-thread draft'}, 'Retained new-thread draft'),
        )
        for route, tab, data, expected in forms:
            with self.subTest(route=route):
                response = self.client.post(reverse(f'portal:{route}', args=[self.patient.pk]), data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['workspace_tab'], tab)
                self.assertContains(response, 'patient-workspace-tabs')
                if route != 'patient-appointment-create':
                    self.assertContains(response, expected)
                self.assertNotContains(response, 'record-action-grid')
        # A doctor doesn't see the Tasks, Notes or Appointments tabs, so those forms come back on the Overview.
        self.login(self.doctor)
        for route, tab, data, _ in forms:
            with self.subTest(doctor_route=route):
                response = self.client.post(reverse(f'portal:{route}', args=[self.patient.pk]), data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['workspace_tab'], 'messages' if tab == 'messages' else 'overview')
        incoming.refresh_from_db()
        self.assertIsNone(incoming.read_at)

    def test_legacy_thread_and_weight_pagination_bookmarks_choose_the_correct_tab(self):
        self.login(self.doctor)
        response = self.client.get(self.path(), {'thread': self.thread.pk})
        self.assertEqual(response.context['workspace_tab'], 'messages')
        # Weight isn't a doctor's tab, so its bookmark opens their Overview; a Super Admin gets the Weight tab.
        self.assertRedirects(self.client.get(self.path(), {'weight_page': 2}), self.overview(), fetch_redirect_response=False)
        self.login(self.super_admin)
        response = self.client.get(self.path(), {'weight_page': 2})
        self.assertEqual(response.context['workspace_tab'], 'weights')
        # A doctor covering an appointment isn't the Prescribing doctor: a conversation link opens in Patient messages.
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.second_doctor,
                                   starts_at=timezone.now() + timedelta(days=1), duration_minutes=30)
        self.login(self.second_doctor)
        self.assertRedirects(self.client.get(self.path(), {'thread': self.thread.pk}),
                             f'{reverse("portal:staff-inbox")}?thread={self.thread.pk}', fetch_redirect_response=False)
