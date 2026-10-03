"""Standalone staff sections preserve practice boundaries and role-specific scope."""

from django.test import override_settings
from datetime import datetime, time, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import Appointment, AvailabilitySlot, ClinicalTask, MessageThread
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class StaffNavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nav_depth = 0
        self.anchor = None
        self.links = []
        self.active_links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav':
            self.nav_depth += 1
        elif tag == 'a' and self.nav_depth:
            self.anchor = [
                attrs.get('href', ''), [],
                attrs.get('aria-current') == 'page' or 'is-active' in attrs.get('class', '').split(),
            ]

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ' '.join(''.join(self.anchor[1]).split())))
            if self.anchor[2]:
                self.active_links.add(self.anchor[0])
            self.anchor = None
        elif tag == 'nav':
            self.nav_depth -= 1


@override_settings(MULTI_PRACTICE_ENABLED=True)
class StaffPagesTests(TestCase):
    section_names = ('staff-tasks', 'patient-list', 'staff-schedule')

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Alpha Staff Practice', slug='alpha-staff-pages')
        cls.other_company = Company.objects.create(name='Beta Staff Practice', slug='beta-staff-pages')
        cls.doctor = users.create_user(email='pages-doctor@example.test', first_name='Doctor')
        cls.second_doctor = users.create_user(email='pages-second-doctor@example.test', first_name='Second doctor')
        cls.foreign_doctor = users.create_user(email='pages-foreign-doctor@example.test', first_name='Beta doctor')
        cls.administrator = users.create_user(email='pages-admin@example.test', first_name='Administrator')
        cls.super_admin = users.create_user(email='pages-super@example.test', first_name='Super administrator')
        cls.patient_user = users.create_user(email='pages-patient@example.test', first_name='Alice')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.second_doctor, CompanyMembership.Role.DOCTOR),
            (cls.administrator, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(user=user, company=cls.company, role=role)
        for user in (cls.doctor, cls.foreign_doctor):
            CompanyMembership.objects.create(user=user, company=cls.other_company, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Needle',
            assigned_doctor=cls.doctor, medical_record_number='ALPHA-001',
        )
        cls.second_patient = Patient.objects.create(
            company=cls.company, first_name='Beth', last_name='Patient', assigned_doctor=cls.second_doctor,
            medical_record_number='ALPHA-002',
        )
        cls.other_practice_patient = Patient.objects.create(
            company=cls.other_company, user=cls.patient_user, first_name='Alice', last_name='Needle',
            assigned_doctor=cls.doctor,
        )
        cls.inactive_patient = Patient.objects.create(
            company=cls.company, first_name='Inactive', last_name='Needle', is_active=False,
        )
        cls.own_open = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Own open task', assigned_to=cls.doctor,
            due_at=timezone.now() - timedelta(days=1),
        )
        cls.own_progress = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Own ongoing task', assigned_to=cls.doctor,
            status=ClinicalTask.Status.IN_PROGRESS, due_at=timezone.now() + timedelta(days=1),
        )
        cls.own_done = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Own completed task', assigned_to=cls.doctor,
            status=ClinicalTask.Status.DONE, completed_at=timezone.now(),
        )
        cls.own_cancelled = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Own cancelled task', assigned_to=cls.doctor,
            status=ClinicalTask.Status.CANCELLED,
        )
        cls.other_task = ClinicalTask.objects.create(
            company=cls.company, patient=cls.second_patient, title='Other doctor task', assigned_to=cls.second_doctor,
        )
        cls.unassigned_task = ClinicalTask.objects.create(company=cls.company, patient=cls.patient, title='Unassigned task')
        cls.foreign_task = ClinicalTask.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient, title='Beta task', assigned_to=cls.doctor,
        )
        cls.inactive_task = ClinicalTask.objects.create(
            company=cls.company, patient=cls.inactive_patient, title='Inactive patient task', assigned_to=cls.doctor,
        )
        cls.day = timezone.localdate() + timedelta(days=7)
        cls.day_start = datetime.combine(cls.day, time.min, tzinfo=ZoneInfo('Africa/Johannesburg'))
        cls.midnight_appointment = cls.make_appointment(starts_at=cls.day_start)
        cls.late_appointment = cls.make_appointment(starts_at=cls.day_start + timedelta(hours=23, minutes=59))
        cls.previous_day_appointment = cls.make_appointment(starts_at=cls.day_start - timedelta(minutes=1))
        cls.next_day_appointment = cls.make_appointment(starts_at=cls.day_start + timedelta(days=1))
        cls.other_doctor_appointment = cls.make_appointment(
            starts_at=cls.day_start + timedelta(hours=12), clinician=cls.second_doctor, patient=cls.second_patient,
        )
        cls.cancelled_appointment = cls.make_appointment(
            starts_at=cls.day_start + timedelta(hours=14), status=Appointment.Status.CANCELLED,
        )
        cls.foreign_appointment = cls.make_appointment(
            starts_at=cls.day_start + timedelta(hours=15), company=cls.other_company, patient=cls.other_practice_patient,
        )
        cls.inactive_appointment = cls.make_appointment(
            starts_at=cls.day_start + timedelta(hours=16), patient=cls.inactive_patient,
        )

    @classmethod
    def make_appointment(cls, *, starts_at, **kwargs):
        values = {
            'company': cls.company, 'patient': cls.patient, 'clinician': cls.doctor,
            'starts_at': starts_at, 'duration_minutes': 15,
        }
        values.update(kwargs)
        return Appointment.objects.create(**values)

    def login(self, user=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, name, **params):
        return self.client.get(reverse(f'portal:{name}'), params)

    def assert_invalid_filter(self, response, field, list_name):
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context['filter_form'].errors)
        self.assertEqual(list(response.context[list_name]), [])

    def test_new_sections_require_login(self):
        for name in self.section_names:
            with self.subTest(section=name):
                url = reverse(f'portal:{name}')
                self.assertRedirects(
                    self.client.get(url), f'{reverse("accounts:login")}?next={url}', fetch_redirect_response=False,
                )

    def test_patient_cannot_enter_staff_sections(self):
        self.login(self.patient_user)
        for name in self.section_names:
            with self.subTest(section=name):
                self.assertEqual(self.page(name).status_code, 403)

    def test_all_active_staff_roles_can_open_each_section(self):
        for user in (self.doctor, self.administrator, self.super_admin):
            self.login(user)
            for name in self.section_names:
                with self.subTest(user=user.email, section=name):
                    self.assertEqual(self.page(name).status_code, 200)

    def test_new_list_sections_are_get_only(self):
        self.login()
        for name in self.section_names:
            with self.subTest(section=name):
                self.assertEqual(self.client.post(reverse(f'portal:{name}'), {}).status_code, 405)

    def test_directory_lists_only_active_patients_in_current_practice(self):
        self.login()
        response = self.page('patient-list')
        self.assertEqual({patient.pk for patient in response.context['patients']}, {self.patient.pk, self.second_patient.pk})

    def test_directory_search_and_clinician_filter_work_within_practice(self):
        self.login(self.administrator)
        response = self.page('patient-list', q='nEeDlE')
        self.assertEqual({patient.pk for patient in response.context['patients']}, {self.patient.pk})
        response = self.page('patient-list', clinician=self.second_doctor.pk)
        self.assertEqual({patient.pk for patient in response.context['patients']}, {self.second_patient.pk})
        response = self.page('patient-list', q='Needle', clinician=self.second_doctor.pk)
        self.assertEqual(list(response.context['patients']), [])

    def test_invalid_directory_filters_show_errors_without_falling_back_to_all_patients(self):
        self.login(self.administrator)
        for params, field in (({'clinician': self.foreign_doctor.pk}, 'clinician'), ({'q': 'x' * 201}, 'q')):
            with self.subTest(field=field):
                self.assert_invalid_filter(self.page('patient-list', **params), field, 'patients')

    def test_directory_paginates_twenty_patients_without_losing_rows(self):
        additional = Patient.objects.bulk_create([
            Patient(company=self.company, first_name=f'Extra {number}', last_name='Directory') for number in range(22)
        ])
        self.login()
        first = self.page('patient-list')
        second = self.page('patient-list', page=2)
        first_ids = {patient.pk for patient in first.context['patients']}
        second_ids = {patient.pk for patient in second.context['patients']}
        self.assertTrue(first.context['is_paginated'])
        self.assertEqual((len(first_ids), len(second_ids)), (20, 4))
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, {self.patient.pk, self.second_patient.pk, *(patient.pk for patient in additional)})

    def test_doctor_task_page_lists_only_own_tasks_and_scopes_metrics(self):
        self.login()
        response = self.page('staff-tasks', status='all')
        self.assertEqual({task.pk for task in response.context['tasks']},
                         {self.own_open.pk, self.own_progress.pk, self.own_done.pk, self.own_cancelled.pk})
        self.assertEqual(response.context['metrics']['open'], 2)
        self.assertEqual(response.context['metrics']['overdue'], 1)
        self.assertEqual(response.context['metrics']['completed'], 1)

    def test_administrator_task_page_includes_unassigned_and_other_doctor_tasks(self):
        self.login(self.administrator)
        response = self.page('staff-tasks', status='all')
        self.assertEqual({task.pk for task in response.context['tasks']}, {
            self.own_open.pk, self.own_progress.pk, self.own_done.pk, self.own_cancelled.pk,
            self.other_task.pk, self.unassigned_task.pk,
        })
        self.assertEqual(response.context['metrics']['open'], 4)

    def test_task_status_and_patient_filters_preserve_role_scope(self):
        self.login()
        for status, expected in (
            ('open', {self.own_open.pk, self.own_progress.pk}),
            ('done', {self.own_done.pk}),
            ('cancelled', {self.own_cancelled.pk}),
        ):
            with self.subTest(status=status):
                response = self.page('staff-tasks', status=status, patient=self.patient.pk)
                self.assertEqual({task.pk for task in response.context['tasks']}, expected)
        self.assertEqual(list(self.page('staff-tasks', patient=self.second_patient.pk).context['tasks']), [])

    def test_invalid_task_filters_show_errors_and_no_rows(self):
        self.login()
        for params, field in (({'patient': self.other_practice_patient.pk}, 'patient'), ({'status': 'unknown'}, 'status')):
            with self.subTest(field=field):
                self.assert_invalid_filter(self.page('staff-tasks', **params), field, 'tasks')

    def test_task_completion_returns_to_filtered_task_list(self):
        self.login()
        next_url = f'{reverse("portal:staff-tasks")}?status=open&patient={self.patient.pk}'
        response = self.client.post(reverse('portal:task-complete', args=[self.own_open.pk]), {'next': next_url})
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.own_open.refresh_from_db()
        self.assertEqual(self.own_open.status, ClinicalTask.Status.DONE)

    def test_doctor_cannot_complete_other_unassigned_foreign_or_inactive_patient_task(self):
        self.login()
        for task in (self.other_task, self.unassigned_task, self.foreign_task, self.inactive_task):
            with self.subTest(task=task.pk):
                response = self.client.post(reverse('portal:task-complete', args=[task.pk]), {
                    'next': reverse('portal:staff-tasks'),
                })
                self.assertEqual(response.status_code, 404)
                task.refresh_from_db()
                self.assertEqual(task.status, ClinicalTask.Status.OPEN)

    def test_doctor_schedule_contains_own_diary_for_the_south_african_day(self):
        self.login()
        response = self.page('staff-schedule', date=self.day.isoformat())
        self.assertEqual({appointment.pk for appointment in response.context['appointments']},
                         {self.midnight_appointment.pk, self.late_appointment.pk, self.cancelled_appointment.pk})

    def test_schedule_day_includes_sast_midnight_but_excludes_next_midnight(self):
        self.login()
        self.assertEqual(self.day_start.astimezone(ZoneInfo('UTC')).hour, 22)
        response = self.page('staff-schedule', date=self.day.isoformat())
        ids = {appointment.pk for appointment in response.context['appointments']}
        self.assertIn(self.midnight_appointment.pk, ids)
        self.assertIn(self.late_appointment.pk, ids)
        self.assertNotIn(self.previous_day_appointment.pk, ids)
        self.assertNotIn(self.next_day_appointment.pk, ids)

    def test_administrator_schedule_can_show_all_practice_doctors_or_filter_one(self):
        self.login(self.administrator)
        response = self.page('staff-schedule', date=self.day.isoformat())
        self.assertEqual({appointment.pk for appointment in response.context['appointments']}, {
            self.midnight_appointment.pk, self.late_appointment.pk, self.cancelled_appointment.pk,
            self.other_doctor_appointment.pk,
        })
        response = self.page('staff-schedule', date=self.day.isoformat(), clinician=self.second_doctor.pk)
        self.assertEqual({appointment.pk for appointment in response.context['appointments']}, {self.other_doctor_appointment.pk})

    def test_doctor_cannot_select_another_doctors_schedule(self):
        self.login()
        self.assert_invalid_filter(
            self.page('staff-schedule', date=self.day.isoformat(), clinician=self.second_doctor.pk),
            'clinician', 'appointments',
        )

    def test_schedule_rejects_foreign_clinician_and_invalid_date_without_mutation(self):
        self.login(self.administrator)
        before = list(Appointment.objects.order_by('pk').values('pk', 'starts_at', 'status', 'clinician_id'))
        for params, field in (
            ({'date': self.day.isoformat(), 'clinician': self.foreign_doctor.pk}, 'clinician'),
            ({'date': '2026-99-99'}, 'date'),
        ):
            with self.subTest(field=field):
                self.assert_invalid_filter(self.page('staff-schedule', **params), field, 'appointments')
        self.assertEqual(list(Appointment.objects.order_by('pk').values('pk', 'starts_at', 'status', 'clinician_id')), before)

    def test_schedule_extreme_dates_return_safe_validation_errors(self):
        self.login(self.administrator)
        for value in ('0001-01-01', '9999-12-31'):
            with self.subTest(date=value):
                self.assert_invalid_filter(self.page('staff-schedule', date=value), 'date', 'appointments')

    def test_schedule_shows_only_real_future_unbooked_non_conflicting_slots(self):
        valid = AvailabilitySlot.objects.create(
            company=self.company, clinician=self.doctor,
            starts_at=self.day_start + timedelta(hours=10), ends_at=self.day_start + timedelta(hours=10, minutes=30),
        )
        AvailabilitySlot.objects.create(
            company=self.company, clinician=self.doctor, is_booked=True,
            starts_at=self.day_start + timedelta(hours=11), ends_at=self.day_start + timedelta(hours=11, minutes=30),
        )
        AvailabilitySlot.objects.create(
            company=self.company, clinician=self.doctor, starts_at=self.day_start,
            ends_at=self.day_start + timedelta(minutes=30),
        )
        AvailabilitySlot.objects.create(
            company=self.other_company, clinician=self.doctor,
            starts_at=self.day_start + timedelta(hours=13), ends_at=self.day_start + timedelta(hours=13, minutes=30),
        )
        self.login()
        response = self.page('staff-schedule', date=self.day.isoformat())
        self.assertEqual({slot.pk for slot in response.context['open_slots']}, {valid.pk})

    def test_primary_navigation_routes_to_real_sections_on_desktop_and_mobile(self):
        expected = {reverse(f'portal:{name}') for name in (*self.section_names, 'staff-inbox')}
        self.login()
        # The mobile bar holds only the sections; Care's page lists the same destinations.
        mobile = StaffNavigationParser()
        mobile.feed(self.page('mobile-dashboard').content.decode())
        self.assertIn(reverse('portal:staff-menu-section', args=['care']), {href for href, label in mobile.links})
        care = self.client.get(reverse('portal:staff-menu-section', args=['care'])).content.decode()
        self.assertTrue(all(url in care for url in expected))
        for name in ('desktop-dashboard', *self.section_names, 'staff-inbox'):
            with self.subTest(page=name):
                response = self.page(name)
                self.assertEqual(response.status_code, 200)
                parser = StaffNavigationParser()
                parser.feed(response.content.decode())
                self.assertTrue(expected.issubset({href for href, label in parser.links}))
                for href, label in parser.links:
                    if label.lower() in ('overview', 'patients', 'tasks', 'schedule', 'messages', 'inbox'):
                        self.assertFalse(urlsplit(href).fragment, f'{label} still uses an in-page anchor: {href}')

    def test_patient_record_sidebar_shares_real_section_links_and_marks_patients_active(self):
        self.login()
        record_url = reverse('portal:patient-detail', args=[self.patient.pk])
        response = self.client.get(record_url)
        self.assertEqual(response.status_code, 200)
        parser = StaffNavigationParser()
        parser.feed(response.content.decode())
        expected = {reverse(f'portal:{name}') for name in (*self.section_names, 'staff-inbox')}
        self.assertTrue(expected.issubset({href for href, label in parser.links}))
        # An open record highlights Patients rather than adding its own menu item.
        self.assertIn(reverse('portal:patient-list'), parser.active_links)
        self.assertNotIn(reverse('portal:patient-detail', args=[self.patient.pk]), {href for href, label in parser.links})
        self.assertNotIn(reverse('portal:staff-patient-record', args=[self.patient.pk]), parser.active_links)
        self.assertTrue(all(not urlsplit(href).fragment for href, label in parser.links))

    def test_schedule_proposal_link_preselects_eligible_appointment_in_conversation(self):
        thread = MessageThread.objects.create(
            company=self.company, patient=self.patient, subject='Review appointment arrangements',
        )
        add_participant(thread, self.doctor)
        self.login()
        response = self.page('staff-schedule', date=self.day.isoformat())
        appointment = next(item for item in response.context['appointments'] if item.pk == self.midnight_appointment.pk)
        proposal_url = urlsplit(appointment.proposal_url)
        self.assertEqual(proposal_url.path, reverse('portal:staff-inbox'))
        self.assertEqual(parse_qs(proposal_url.query)['appointment'], [str(appointment.pk)])
        self.assertEqual(parse_qs(proposal_url.query)['thread'], [str(thread.pk)])
        response = self.client.get(appointment.proposal_url)
        self.assertEqual(response.status_code, 200)
        form = response.context['selected_thread'].proposal_form
        self.assertEqual(str(form['appointment'].value()), str(appointment.pk))
        self.assertTrue(form.fields['appointment'].queryset.filter(pk=appointment.pk).exists())
        response = self.client.get(reverse('portal:staff-inbox'), {
            'thread': thread.pk, 'appointment': self.foreign_appointment.pk,
        })
        self.assertEqual(response.status_code, 200)
        form = response.context['selected_thread'].proposal_form
        self.assertFalse(form.fields['appointment'].queryset.filter(pk=self.foreign_appointment.pk).exists())
        self.assertNotIn(f'<option value="{self.foreign_appointment.pk}"', str(form['appointment']))

    def test_practice_switch_preserves_each_section_and_removes_old_filters(self):
        for name in self.section_names:
            with self.subTest(section=name):
                self.login()
                section_url = reverse(f'portal:{name}')
                response = self.client.post(reverse('portal:activate-company', args=[self.other_company.slug]), {
                    'next': f'{section_url}?q=Needle&status=all&date={self.day.isoformat()}&clinician={self.second_doctor.pk}&page=2',
                })
                self.assertRedirects(response, section_url, fetch_redirect_response=False)
                self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.other_company.pk)
