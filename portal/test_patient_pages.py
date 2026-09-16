"""Patient navigation is split into owned pages, never a staff-data dashboard."""

from django.test import override_settings
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import (
    Appointment, AuditEvent, ClinicalNote, ClinicalTask, MessageThread,
    PatientEvent, PatientMessage, RecordTag, TaskTagAssignment, WeightEntry,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class PatientNavigationParser(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.links = []
        self.nav_depth = 0
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav':
            self.nav_depth += 1
        elif tag == 'a' and self.nav_depth:
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.nav_depth -= 1


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientStandalonePagesTests(TestCase):
    page_names = ('patient-dashboard', 'patient-appointments', 'patient-messages', 'patient-progress', 'patient-account')

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Patient Practice', slug='alpha-patient-pages')
        cls.other_company = Company.objects.create(name='Beta Patient Practice', slug='beta-patient-pages')
        cls.user = get_user_model().objects.create_user(email='pages-patient@example.test', first_name='Alice')
        cls.other_user = get_user_model().objects.create_user(email='pages-other-patient@example.test', first_name='Beth')
        cls.doctor = get_user_model().objects.create_user(email='pages-only-doctor@example.test', first_name='Doctor')
        for company in (cls.company, cls.other_company):
            CompanyMembership.objects.create(company=company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.user, first_name='Alice', last_name='Alpha Patient',
            phone='082 000 0001', city='Cape Town', id_number='ALPHA-IDENTITY', assigned_doctor=cls.doctor,
        )
        cls.beta_patient = Patient.objects.create(
            company=cls.other_company, user=cls.user, first_name='Alice', last_name='Beta Patient',
            phone='082 000 0002', city='Durban', id_number='BETA-IDENTITY', assigned_doctor=cls.doctor,
        )
        cls.other_patient = Patient.objects.create(
            company=cls.company, user=cls.other_user, first_name='Beth', last_name='Other Patient',
        )
        cls.thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Selected Alpha conversation', opened_by=cls.user,
        )
        cls.second_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Unselected Alpha conversation', opened_by=cls.user,
        )
        cls.closed_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Closed Alpha conversation', opened_by=cls.user, is_closed=True,
        )
        cls.beta_thread = MessageThread.objects.create(
            company=cls.other_company, patient=cls.beta_patient, subject='Beta private conversation', opened_by=cls.user,
        )
        cls.foreign_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.other_patient, subject='Beth private conversation', opened_by=cls.other_user,
        )
        cls.upcoming = cls.appointment(days=7)
        cls.past = cls.appointment(days=-7, status=Appointment.Status.COMPLETED)
        cls.cancelled = cls.appointment(days=8, status=Appointment.Status.CANCELLED)
        cls.beta_appointment = cls.appointment(days=9, company=cls.other_company, patient=cls.beta_patient)
        cls.foreign_appointment = cls.appointment(days=10, patient=cls.other_patient)
        cls.weight = WeightEntry.objects.create(
            company=cls.company, patient=cls.patient, recorded_by=cls.user,
            recorded_on=timezone.localdate() - timedelta(days=2), weight_kg='91.25', note='Owned weight note.',
        )
        cls.beta_weight = WeightEntry.objects.create(
            company=cls.other_company, patient=cls.beta_patient, recorded_by=cls.user,
            recorded_on=timezone.localdate() - timedelta(days=2), weight_kg='101.25', note='Secret beta weight note.',
        )
        cls.foreign_weight = WeightEntry.objects.create(
            company=cls.company, patient=cls.other_patient, recorded_by=cls.other_user,
            recorded_on=timezone.localdate() - timedelta(days=2), weight_kg='111.25', note='Secret other patient weight note.',
        )

    @classmethod
    def appointment(cls, *, days=1, **overrides):
        values = {
            'company': cls.company, 'patient': cls.patient, 'clinician': cls.doctor,
            'starts_at': timezone.now() + timedelta(days=days), 'duration_minutes': 30,
            'video_link': f'https://example.test/patient-consult-{days}',
        }
        values.update(overrides)
        return Appointment.objects.create(**values)

    def login(self, user=None, company=None, client=None):
        client = client or self.client
        client.force_login(user or self.user)
        session = client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_COMPANY_SESSION_KEY] = self.other_company.pk
        session.save()

    def page(self, name, **params):
        return self.client.get(reverse(f'portal:{name}'), params)

    def message(self, *, thread=None, sender=None, body='Incoming team message'):
        thread = thread or self.thread
        return PatientMessage.objects.create(
            company=thread.company, thread=thread, sender=sender or self.doctor, body=body,
        )

    def message_destination(self, thread):
        return f'{reverse("portal:patient-messages")}?thread={thread.pk}#patient-conversation'

    def patient_token(self, patient=None):
        from .patient_context import make_patient_context

        patient = patient or self.patient
        request = RequestFactory().get('/patient/')
        request.user = patient.user
        return make_patient_context(request, patient.company, patient)

    def write_snapshot(self):
        return {
            'patients': list(Patient.objects.order_by('pk').values(
                'pk', 'company_id', 'user_id', 'phone', 'city', 'updated_at',
            )),
            'counts': {
                model._meta.label: model.objects.count()
                for model in (WeightEntry, MessageThread, PatientMessage, PatientEvent, AuditEvent)
            },
            'read_markers': list(PatientMessage.objects.order_by('pk').values_list('pk', 'read_at')),
        }

    def guarded_posts(self):
        return (
            ('patient-account', 'contact_form', {'phone': '083 222 1111', 'city': 'Changed city'}),
            ('patient-weight-add', 'weight_form', {
                'recorded_on': timezone.localdate().isoformat(), 'weight_kg': '88.25', 'note': 'Guarded weight draft.',
            }),
            ('patient-thread-create', 'thread_form', {'subject': 'Guarded new thread', 'body': 'Guarded message draft.'}),
            ('patient-messages', 'thread_form', {'subject': 'Guarded legacy thread', 'body': 'Guarded legacy draft.'}),
        )

    def assert_context_rejected(self, route, form_name, data):
        before = self.write_snapshot()
        response = self.client.post(reverse(f'portal:{route}'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context[form_name].non_field_errors())
        self.assertEqual(self.write_snapshot(), before)
        return response

    def test_all_patient_pages_require_login(self):
        for name in self.page_names:
            with self.subTest(page=name):
                url = reverse(f'portal:{name}')
                self.assertRedirects(self.client.get(url), f'{reverse("accounts:login")}?next={url}', fetch_redirect_response=False)

    def test_staff_membership_alone_cannot_open_patient_pages(self):
        self.login(self.doctor)
        for name in self.page_names:
            with self.subTest(page=name):
                self.assertEqual(self.page(name).status_code, 403)

    def test_each_patient_page_has_a_dedicated_template_and_real_navigation(self):
        self.login()
        templates = {
            'patient-dashboard': 'portal/patient_dashboard.html',
            'patient-appointments': 'portal/patient_appointments.html',
            'patient-messages': 'portal/patient_messages.html',
            'patient-progress': 'portal/patient_progress.html',
            'patient-account': 'portal/patient_account.html',
        }
        for name, template in templates.items():
            with self.subTest(page=name):
                response = self.page(name)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, template)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''))
                navigation = PatientNavigationParser(response.content.decode()).links
                hrefs = {item.get('href') for item in navigation}
                for section in self.page_names:
                    self.assertIn(reverse(f'portal:{section}'), hrefs)
                current = [item for item in navigation if item.get('aria-current') == 'page']
                self.assertTrue(current)
                self.assertEqual({item.get('href') for item in current}, {reverse(f'portal:{name}')})

    def test_overview_has_summaries_without_inline_write_forms_or_conversations(self):
        self.login()
        long_body = 'Private secure conversation detail. ' * 10 + 'End of full message.'
        self.message(body=long_body)
        response = self.page('patient-dashboard')
        self.assertNotContains(response, long_body)
        for action in (
            reverse('portal:patient-weight-add'), reverse('portal:patient-thread-create'),
            reverse('portal:patient-message-create', args=[self.thread.pk]),
            reverse('portal:patient-appointment-propose', args=[self.thread.pk]),
        ):
            self.assertNotContains(response, f'action="{action}"')

    def test_overview_and_non_message_pages_never_mark_incoming_messages_read(self):
        incoming = self.message()
        self.login()
        for name in ('patient-dashboard', 'patient-appointments', 'patient-progress', 'patient-account'):
            with self.subTest(page=name):
                self.assertEqual(self.page(name).status_code, 200)
                incoming.refresh_from_db()
                self.assertIsNone(incoming.read_at)

    def test_opening_selected_thread_marks_only_its_incoming_messages_read(self):
        incoming = self.message()
        own = self.message(sender=self.user)
        unselected = self.message(thread=self.second_thread)
        beta = self.message(thread=self.beta_thread)
        foreign = self.message(thread=self.foreign_thread)
        self.login()
        response = self.page('patient-messages', thread=self.thread.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread'].pk, self.thread.pk)
        for message in (incoming, own, unselected, beta, foreign):
            message.refresh_from_db()
        self.assertIsNotNone(incoming.read_at)
        for message in (own, unselected, beta, foreign):
            self.assertIsNone(message.read_at)

    def test_foreign_or_malformed_thread_ids_are_not_found_and_do_not_mark_read(self):
        incoming = self.message()
        self.login()
        for value in (
            self.beta_thread.pk, self.foreign_thread.pk, 'nonsense', '-1', '999999999',
            '9223372036854775808', '9' * 200,
        ):
            with self.subTest(thread=value):
                self.assertEqual(self.page('patient-messages', thread=value).status_code, 404)
                incoming.refresh_from_db()
                self.assertIsNone(incoming.read_at)

    def test_message_filters_separate_open_and_closed_threads_without_other_patients(self):
        self.login()
        for status, present, absent in (
            ('open', self.thread, self.closed_thread),
            ('closed', self.closed_thread, self.thread),
        ):
            with self.subTest(status=status):
                response = self.page('patient-messages', status=status)
                self.assertContains(response, present.subject)
                self.assertNotContains(response, absent.subject)
                self.assertNotContains(response, self.beta_thread.subject)
                self.assertNotContains(response, self.foreign_thread.subject)

    def test_message_history_opens_latest_page_and_preserves_older_messages(self):
        entries = [
            self.message(body=f'History message {number:03d} — complete clinical conversation text.')
            for number in range(55)
        ]
        self.login()
        response = self.page('patient-messages', thread=self.thread.pk)
        self.assertContains(response, 'History message 054 — complete clinical conversation text.')
        self.assertNotContains(response, 'History message 000 — complete clinical conversation text.')
        history_page = response.context['message_page_obj']
        self.assertEqual(history_page.paginator.per_page, 50)
        self.assertEqual(len(history_page), 50)
        entries[0].refresh_from_db()
        entries[-1].refresh_from_db()
        self.assertIsNone(entries[0].read_at)
        self.assertIsNotNone(entries[-1].read_at)
        response = self.client.get(response.context['older_messages_url'])
        self.assertContains(response, 'History message 000 — complete clinical conversation text.')
        # The thread list may still preview its latest message; only the open
        # conversation's paginated history should switch to the older entries.
        visible_ids = {item.pk for item in response.context['selected_thread'].conversation_messages}
        self.assertEqual(visible_ids, {item.pk for item in entries[:5]})
        entries[0].refresh_from_db()
        self.assertIsNotNone(entries[0].read_at)

    def test_appointments_upcoming_history_and_all_are_practice_owned(self):
        self.login()
        for status, expected in (
            ('upcoming', {self.upcoming.pk}),
            ('history', {self.past.pk, self.cancelled.pk}),
            ('all', {self.upcoming.pk, self.past.pk, self.cancelled.pk}),
        ):
            with self.subTest(status=status):
                response = self.page('patient-appointments', status=status)
                self.assertEqual({appointment.pk for appointment in response.context['page_obj']}, expected)

    def test_appointments_are_paginated_twenty_at_a_time(self):
        for days in range(30, 55):
            self.appointment(days=days)
        self.login()
        first = self.page('patient-appointments', status='upcoming')
        second = self.page('patient-appointments', status='upcoming', page=2)
        self.assertEqual(len(first.context['page_obj']), 20)
        self.assertEqual(len(second.context['page_obj']), 6)
        first_ids = {record.pk for record in first.context['page_obj']}
        self.assertFalse(first_ids & {record.pk for record in second.context['page_obj']})
        self.assertEqual(first.context['page_obj'].paginator.count, 26)

    def test_invalid_appointment_filter_does_not_show_an_unfiltered_list(self):
        self.login()
        response = self.page('patient-appointments', status='invented-status')
        self.assertEqual(response.status_code, 200)
        self.assertIn('status', response.context['filter_form'].errors)
        self.assertEqual(list(response.context['page_obj']), [])

    def test_progress_history_contains_only_current_patients_weights(self):
        self.login()
        response = self.page('patient-progress')
        self.assertContains(response, self.weight.note)
        self.assertNotContains(response, self.beta_weight.note)
        self.assertNotContains(response, self.foreign_weight.note)
        self.assertEqual({item.pk for item in response.context['page_obj']}, {self.weight.pk})

    def test_progress_history_is_paginated_twenty_at_a_time(self):
        for days in range(10, 35):
            WeightEntry.objects.create(
                company=self.company, patient=self.patient, weight_kg='90.00',
                recorded_on=timezone.localdate() - timedelta(days=days),
            )
        self.login()
        first = self.page('patient-progress')
        second = self.page('patient-progress', page=2)
        self.assertEqual(len(first.context['page_obj']), 20)
        self.assertEqual(len(second.context['page_obj']), 6)
        self.assertEqual(first.context['page_obj'].paginator.count, 26)

    def test_weight_submission_redirects_to_progress_and_ignores_forged_ownership(self):
        self.login()
        response = self.client.post(reverse('portal:patient-weight-add'), {
            'patient_context': self.patient_token(),
            'recorded_on': timezone.localdate().isoformat(), 'weight_kg': '90.50', 'note': 'Today’s owned entry.',
            'patient': self.other_patient.pk, 'company': self.other_company.pk, 'recorded_by': self.doctor.pk,
        })
        self.assertRedirects(response, reverse('portal:patient-progress'), fetch_redirect_response=False)
        entry = WeightEntry.objects.get(note='Today’s owned entry.')
        self.assertEqual((entry.company_id, entry.patient_id, entry.recorded_by_id), (self.company.pk, self.patient.pk, self.user.pk))
        self.assertEqual(entry.weight_kg, Decimal('90.50'))

    def test_invalid_weight_submission_renders_progress_with_bound_values(self):
        self.login()
        before = WeightEntry.objects.count()
        response = self.client.post(reverse('portal:patient-weight-add'), {
            'patient_context': self.patient_token(),
            'recorded_on': timezone.localdate().isoformat(), 'weight_kg': '-1', 'note': 'Retain my progress draft.',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/patient_progress.html')
        self.assertIn('weight_kg', response.context['weight_form'].errors)
        self.assertContains(response, 'Retain my progress draft.')
        self.assertEqual(WeightEntry.objects.count(), before)

    def test_duplicate_weight_date_remains_an_error_without_losing_draft(self):
        self.login()
        response = self.client.post(reverse('portal:patient-weight-add'), {
            'patient_context': self.patient_token(),
            'recorded_on': self.weight.recorded_on.isoformat(), 'weight_kg': '89.00', 'note': 'Duplicate date draft.',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/patient_progress.html')
        self.assertIn('recorded_on', response.context['weight_form'].errors)
        self.assertContains(response, 'Duplicate date draft.')
        self.weight.refresh_from_db()
        self.assertEqual(self.weight.weight_kg, Decimal('91.25'))

    def test_reply_redirects_to_selected_conversation(self):
        self.login()
        response = self.client.post(reverse('portal:patient-message-create', args=[self.thread.pk]), {'body': 'A useful reply.'})
        self.assertRedirects(response, self.message_destination(self.thread), fetch_redirect_response=False)
        reply = self.thread.messages.get()
        self.assertEqual((reply.company_id, reply.sender_id, reply.body), (self.company.pk, self.user.pk, 'A useful reply.'))

    def test_invalid_reply_preserves_selected_thread_and_draft_on_messages_page(self):
        self.login()
        draft = 'd' * 5001
        response = self.client.post(reverse('portal:patient-message-create', args=[self.thread.pk]), {'body': draft})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/patient_messages.html')
        self.assertEqual(response.context['selected_thread'].pk, self.thread.pk)
        self.assertIn('body', response.context['message_form'].errors)
        self.assertEqual(response.context['message_form']['body'].value(), draft)
        self.assertFalse(PatientMessage.objects.exists())

    def test_new_thread_redirects_to_its_own_dedicated_conversation(self):
        self.login()
        self.assertEqual(reverse('portal:patient-thread-create'), '/patient/messages/new/')
        response = self.client.post(reverse('portal:patient-thread-create'), {
            'patient_context': self.patient_token(),
            'subject': 'New dedicated thread', 'body': 'New enquiry from my account.',
        })
        thread = MessageThread.objects.get(subject='New dedicated thread')
        self.assertRedirects(response, self.message_destination(thread), fetch_redirect_response=False)
        self.assertEqual((thread.company_id, thread.patient_id, thread.opened_by_id), (self.company.pk, self.patient.pk, self.user.pk))

    def test_legacy_messages_post_still_creates_an_owned_thread(self):
        self.login()
        response = self.client.post('/patient/messages/', {
            'patient_context': self.patient_token(), 'subject': 'Legacy form thread', 'body': 'Compatible submit.',
        })
        thread = MessageThread.objects.get(subject='Legacy form thread')
        self.assertRedirects(response, self.message_destination(thread), fetch_redirect_response=False)
        self.assertEqual((thread.company_id, thread.patient_id), (self.company.pk, self.patient.pk))

    def test_account_exposes_only_contact_edit_fields(self):
        self.login()
        response = self.page('patient-account')
        self.assertEqual(set(response.context['contact_form'].fields), {'phone', 'city'})
        self.assertEqual(response.context['contact_form']['phone'].value(), self.patient.phone)
        self.assertEqual(response.context['contact_form']['city'].value(), self.patient.city)

    def test_contact_update_changes_only_owned_patient_contact_and_audits_field_names(self):
        self.login()
        response = self.client.post(reverse('portal:patient-account'), {
            'patient_context': self.patient_token(),
            'phone': '083 765 4321', 'city': 'Johannesburg',
            'email': 'forged-email@example.test', 'user': self.other_user.pk, 'id_number': 'FORGED-ID',
            'practice': self.other_company.pk, 'company': self.other_company.pk, 'patient': self.other_patient.pk,
            'assigned_doctor': self.other_user.pk, 'first_name': 'Forged name',
        })
        self.assertRedirects(response, reverse('portal:patient-account'), fetch_redirect_response=False)
        self.patient.refresh_from_db()
        self.beta_patient.refresh_from_db()
        self.other_patient.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual((self.patient.phone, self.patient.city), ('083 765 4321', 'Johannesburg'))
        self.assertEqual((self.patient.user_id, self.patient.company_id, self.patient.assigned_doctor_id), (self.user.pk, self.company.pk, self.doctor.pk))
        self.assertEqual((self.patient.id_number, self.patient.first_name), ('ALPHA-IDENTITY', 'Alice'))
        self.assertEqual(self.user.email, 'pages-patient@example.test')
        self.assertEqual((self.beta_patient.phone, self.beta_patient.city), ('082 000 0002', 'Durban'))
        self.assertEqual((self.other_patient.phone, self.other_patient.city), ('', ''))
        event = AuditEvent.objects.get(patient=self.patient, actor=self.user)
        self.assertEqual(event.company_id, self.company.pk)
        self.assertCountEqual(event.metadata['changed_fields'], ['phone', 'city'])
        self.assertEqual(set(event.metadata), {'changed_fields'})
        self.assertNotIn('083 765 4321', str(event.metadata))
        self.assertNotIn('Johannesburg', str(event.metadata))

    def test_invalid_contact_values_preserve_form_and_do_not_write(self):
        self.login()
        response = self.client.post(reverse('portal:patient-account'), {
            'patient_context': self.patient_token(), 'phone': '1' * 33, 'city': 'Keep my city draft',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/patient_account.html')
        self.assertIn('phone', response.context['contact_form'].errors)
        self.assertContains(response, 'Keep my city draft')
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.phone, '082 000 0001')
        self.assertEqual(self.patient.city, 'Cape Town')
        self.assertFalse(AuditEvent.objects.filter(patient=self.patient, actor=self.user).exists())

    def test_contact_weight_and_message_writes_require_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        self.login(client=browser)
        for url, data in (
            (reverse('portal:patient-account'), {'phone': '123', 'city': 'City'}),
            (reverse('portal:patient-weight-add'), {'recorded_on': timezone.localdate().isoformat(), 'weight_kg': '90'}),
            (reverse('portal:patient-thread-create'), {'subject': 'Forbidden', 'body': 'Forbidden'}),
            (reverse('portal:patient-message-create', args=[self.thread.pk]), {'body': 'Forbidden'}),
        ):
            with self.subTest(url=url):
                self.assertEqual(browser.post(url, data).status_code, 403)
        self.assertFalse(PatientMessage.objects.exists())
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.phone, '082 000 0001')

    def test_patient_write_forms_include_their_signed_context(self):
        self.login()
        for name in ('patient-account', 'patient-progress', 'patient-messages'):
            with self.subTest(page=name):
                response = self.page(name)
                self.assertTrue(response.context['patient_context'])
                self.assertContains(response, 'name="patient_context"')

    def test_missing_patient_context_blocks_every_guarded_write_including_legacy(self):
        self.login()
        self.message(thread=self.beta_thread, body='Do not mark this during a failed write.')
        for route, form_name, data in self.guarded_posts():
            with self.subTest(route=route):
                self.assert_context_rejected(route, form_name, data)

    def test_invalid_or_tampered_patient_context_blocks_every_guarded_write(self):
        self.login()
        for token in ('invalid-token', self.patient_token() + 'tampered', 'x' * 2049):
            for route, form_name, data in self.guarded_posts():
                with self.subTest(route=route, token=token[:30]):
                    self.assert_context_rejected(route, form_name, {**data, 'patient_context': token})

    def test_expired_patient_context_blocks_every_guarded_write(self):
        from .patient_context import PATIENT_CONTEXT_MAX_AGE

        self.login()
        token = self.patient_token()
        future = timezone.now().timestamp() + PATIENT_CONTEXT_MAX_AGE + 1
        with patch('django.core.signing.time.time', return_value=future):
            for route, form_name, data in self.guarded_posts():
                with self.subTest(route=route):
                    self.assert_context_rejected(route, form_name, {**data, 'patient_context': token})

    def test_valid_context_for_another_patient_cannot_authorize_a_write(self):
        self.login()
        token = self.patient_token(self.other_patient)
        for route, form_name, data in self.guarded_posts():
            with self.subTest(route=route):
                self.assert_context_rejected(route, form_name, {**data, 'patient_context': token})

    def assert_stale_write_rejected(self, page_name, route, form_name, data):
        self.login()
        token = self.page(page_name).context['patient_context']
        incoming = self.message(thread=self.beta_thread, body='New-practice unread message must stay unread.')
        self.client.post(reverse('portal:activate-patient-company', args=[self.other_company.slug]), {'next': reverse(f'portal:{page_name}')})
        self.assertEqual(self.client.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY], self.other_company.pk)
        response = self.assert_context_rejected(route, form_name, {**data, 'patient_context': token})
        incoming.refresh_from_db()
        self.assertIsNone(incoming.read_at)
        self.assertEqual(response.context['patient'].pk, self.beta_patient.pk)

    def test_old_tab_contact_form_cannot_update_the_newly_selected_practice(self):
        self.assert_stale_write_rejected('patient-account', 'patient-account', 'contact_form', {
            'phone': '083 999 9999', 'city': 'Old tab contact values',
        })

    def test_old_tab_weight_form_cannot_write_into_the_newly_selected_practice(self):
        self.assert_stale_write_rejected('patient-progress', 'patient-weight-add', 'weight_form', {
            'recorded_on': timezone.localdate().isoformat(), 'weight_kg': '88.5', 'note': 'Old tab weight values.',
        })

    def test_old_tab_new_thread_cannot_write_or_mark_read_in_the_new_practice(self):
        self.assert_stale_write_rejected('patient-messages', 'patient-thread-create', 'thread_form', {
            'subject': 'Old tab subject', 'body': 'Old tab message values.',
        })

    def test_head_messages_does_not_mark_read_or_write_audit_events(self):
        self.message()
        self.login()
        before = self.write_snapshot()
        response = self.client.head(reverse('portal:patient-messages'), {'thread': self.thread.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'')
        self.assertEqual(self.write_snapshot(), before)

    def test_invalid_message_filter_does_not_open_a_thread_or_mark_read(self):
        self.message()
        self.login()
        before = self.write_snapshot()
        response = self.page('patient-messages', status='invented-status')
        self.assertEqual(response.status_code, 200)
        self.assertIn('status', response.context['filter_form'].errors)
        self.assertIsNone(response.context['selected_thread'])
        self.assertEqual(list(response.context['page_obj']), [])
        self.assertEqual(self.write_snapshot(), before)

    def test_patient_practice_switch_preserves_each_page_but_clears_filters(self):
        for name in self.page_names:
            with self.subTest(page=name):
                self.login()
                url = reverse(f'portal:{name}')
                response = self.client.post(reverse('portal:activate-patient-company', args=[self.other_company.slug]), {
                    'next': f'{url}?thread={self.thread.pk}&status=open&page=3#patient-conversation',
                })
                self.assertRedirects(response, url, fetch_redirect_response=False)
                self.assertEqual(self.client.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY], self.other_company.pk)
                response = self.client.get(url)
                self.assertEqual(response.context['patient'].pk, self.beta_patient.pk)

    def test_patient_practice_switch_rejects_unsafe_or_unknown_destinations(self):
        for destination in ('https://example.test/evil', '//example.test/evil', '/patient/messages/123/', '/leads/', '/unknown/'):
            with self.subTest(destination=destination):
                self.login()
                response = self.client.post(reverse('portal:activate-patient-company', args=[self.other_company.slug]), {'next': destination})
                self.assertRedirects(response, reverse('portal:patient-dashboard'), fetch_redirect_response=False)

    def test_dual_staff_patient_account_still_sees_only_owned_patient_context(self):
        CompanyMembership.objects.create(company=self.company, user=self.user, role=CompanyMembership.Role.SUPER_ADMIN)
        self.login(company=self.other_company)
        for name in self.page_names:
            with self.subTest(page=name):
                response = self.page(name)
                self.assertEqual(response.context['patient'].pk, self.beta_patient.pk)
                self.assertContains(response, 'id="patient-company-select"')
                self.assertNotContains(response, 'id="company-select"')
                self.assertNotContains(response, self.foreign_thread.subject)

    def test_inactive_patient_record_cannot_be_selected_or_read(self):
        self.patient.is_active = False
        self.patient.save(update_fields=['is_active'])
        self.login()
        # Existing fallback behavior safely selects the person's other active record.
        response = self.page('patient-dashboard')
        self.assertEqual(response.context['patient'].pk, self.beta_patient.pk)
        self.assertEqual(self.page('patient-messages', thread=self.thread.pk).status_code, 404)
        response = self.client.post(reverse('portal:activate-patient-company', args=[self.company.slug]))
        self.assertEqual(response.status_code, 403)

    def test_inactive_practice_without_another_active_record_denies_patient_pages(self):
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        Patient.objects.filter(pk=self.beta_patient.pk).update(is_active=False)
        self.login()
        for name in self.page_names:
            with self.subTest(page=name):
                self.assertEqual(self.page(name).status_code, 403)

    def test_patient_pages_never_expose_internal_notes_tasks_tags_or_hidden_events(self):
        internal_note = 'Internal clinician-only narrative.'
        internal_task = 'Internal follow-up workflow item.'
        internal_tag = 'Internal difficult-patient label.'
        hidden_event = 'Internal audit event detail.'
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor, body=internal_note)
        task = ClinicalTask.objects.create(company=self.company, patient=self.patient, title=internal_task, assigned_to=self.doctor)
        tag = RecordTag.objects.create(company=self.company, name=internal_tag)
        TaskTagAssignment.objects.create(company=self.company, task=task, tag=tag)
        PatientEvent.objects.create(company=self.company, patient=self.patient, title=hidden_event, is_patient_visible=False)
        self.login()
        for name in self.page_names:
            with self.subTest(page=name):
                response = self.page(name)
                for private_text in (internal_note, internal_task, internal_tag, hidden_event):
                    self.assertNotContains(response, private_text)
