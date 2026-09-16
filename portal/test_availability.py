"""Availability editing is a signed, self-doctor action in the selected practice."""

from django.test import override_settings
from datetime import datetime, time, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, AvailabilitySlot, DoctorTimeOff, DoctorWorkingPattern
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AvailabilityPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Schedule Practice', slug='alpha-schedule-edit')
        cls.beta = Company.objects.create(name='Beta Schedule Practice', slug='beta-schedule-edit')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='schedule-owner@example.test', first_name='Owner doctor')
        cls.colleague = users.create_user(email='schedule-colleague@example.test', first_name='Colleague doctor')
        cls.admin = users.create_user(email='schedule-admin@example.test')
        cls.super_admin = users.create_user(email='schedule-super@example.test')
        cls.patient_user = users.create_user(email='schedule-patient@example.test')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR), (cls.colleague, CompanyMembership.Role.DOCTOR),
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN), (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Patient', last_name='Alpha')
        today = timezone.localdate()
        cls.monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        cls.sast = ZoneInfo('Africa/Johannesburg')

    def at(self, hour, minute=0):
        return datetime.combine(self.monday, time(hour, minute), tzinfo=self.sast)

    def login(self, user=None, company=None, client=None):
        client = client or self.client
        client.force_login(user or self.doctor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def token(self, *, company=None, clinician=None):
        from .availability_forms import make_schedule_context

        request = RequestFactory().get('/schedule/')
        request.user = self.doctor
        return make_schedule_context(request, company or self.company, clinician or self.doctor)

    def pattern_data(self):
        data = {
            'schedule_context': self.token(),
            'pattern-TOTAL_FORMS': '7', 'pattern-INITIAL_FORMS': '7',
            'pattern-MIN_NUM_FORMS': '7', 'pattern-MAX_NUM_FORMS': '7',
        }
        for day in range(7):
            data[f'pattern-{day}-starts_at'] = '09:00' if day < 5 else ''
            data[f'pattern-{day}-ends_at'] = '17:00' if day < 5 else ''
            if day < 5:
                data[f'pattern-{day}-is_working'] = 'on'
        return data

    def time_off_data(self):
        return {
            'schedule_context': self.token(), 'timeoff-reason': 'sick',
            'timeoff-starts_at': self.at(10).strftime('%Y-%m-%dT%H:%M'),
            'timeoff-ends_at': self.at(11).strftime('%Y-%m-%dT%H:%M'),
        }

    def leave(self, *, company=None, clinician=None):
        from care.availability import create_time_off

        clinician = clinician or self.doctor
        return create_time_off(
            company=company or self.company, clinician=clinician, actor=clinician,
            starts_at=self.at(10), ends_at=self.at(11), reason='sick',
        )

    def mutation_snapshot(self):
        return {
            model._meta.label: list(model.objects.order_by('pk').values())
            for model in (DoctorWorkingPattern, DoctorTimeOff, Appointment, AvailabilitySlot, AuditEvent)
        }

    def assert_schedule_redirect(self, response, *, view='table', day=None):
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('portal:staff-schedule'))
        query = parse_qs(destination.query)
        self.assertEqual(query.get('view'), [view])
        if day:
            self.assertEqual(query.get('date'), [day.isoformat()])

    def test_availability_actions_require_login_and_are_post_only(self):
        leave = self.leave()
        urls = (
            reverse('portal:availability-working-pattern'), reverse('portal:availability-time-off-create'),
            reverse('portal:availability-time-off-cancel', args=[leave.pk]),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {}).status_code, 302)
        self.login()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 405)

    def test_all_availability_actions_require_csrf(self):
        leave = self.leave()
        browser = Client(enforce_csrf_checks=True)
        self.login(client=browser)
        before = self.mutation_snapshot()
        for url, data in (
            (reverse('portal:availability-working-pattern'), self.pattern_data()),
            (reverse('portal:availability-time-off-create'), self.time_off_data()),
            (reverse('portal:availability-time-off-cancel', args=[leave.pk]), {'schedule_context': self.token()}),
        ):
            with self.subTest(url=url):
                self.assertEqual(browser.post(url, data).status_code, 403)
        self.assertEqual(self.mutation_snapshot(), before)

    def test_admins_are_read_only_and_patients_cannot_change_availability(self):
        leave = self.leave()
        before = self.mutation_snapshot()
        for user in (self.admin, self.super_admin, self.patient_user):
            self.login(user)
            for url, data in (
                (reverse('portal:availability-working-pattern'), self.pattern_data()),
                (reverse('portal:availability-time-off-create'), self.time_off_data()),
                (reverse('portal:availability-time-off-cancel', args=[leave.pk]), {'schedule_context': self.token()}),
            ):
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.mutation_snapshot(), before)

    def test_doctor_saves_seven_day_pattern_and_preserves_calendar_date(self):
        self.login()
        response = self.client.post(
            f'{reverse("portal:availability-working-pattern")}?date={self.monday.isoformat()}&view=calendar',
            self.pattern_data(),
        )
        self.assert_schedule_redirect(response, view='calendar', day=self.monday)
        rows = DoctorWorkingPattern.objects.filter(company=self.company, clinician=self.doctor).order_by('weekday')
        self.assertEqual(rows.count(), 7)
        self.assertEqual(list(rows.values_list('weekday', flat=True)), list(range(7)))
        self.assertEqual(rows.get(weekday=0).starts_at, time(9))
        self.assertFalse(rows.get(weekday=6).is_working)

    def test_bad_pattern_retains_bound_values_and_does_not_partially_save(self):
        self.login()
        data = self.pattern_data()
        data['pattern-0-ends_at'] = '08:00'
        before = self.mutation_snapshot()
        response = self.client.post(reverse('portal:availability-working-pattern'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/staff_schedule.html')
        formset = response.context['working_pattern_formset']
        self.assertTrue(formset.is_bound)
        self.assertTrue(formset.errors[0])
        self.assertEqual(formset.forms[0]['ends_at'].value(), '08:00')
        self.assertEqual(self.mutation_snapshot(), before)

    def test_incomplete_pattern_management_data_cannot_replace_a_week(self):
        self.login()
        data = self.pattern_data()
        data['pattern-TOTAL_FORMS'] = '6'
        response = self.client.post(reverse('portal:availability-working-pattern'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['working_pattern_formset'].non_form_errors())
        self.assertFalse(DoctorWorkingPattern.objects.exists())

    def test_posted_weekday_and_company_fields_cannot_redirect_pattern_ownership(self):
        self.login()
        data = self.pattern_data()
        data.update({'company': self.beta.pk, 'clinician': self.colleague.pk, 'pattern-0-weekday': '6'})
        response = self.client.post(reverse('portal:availability-working-pattern'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DoctorWorkingPattern.objects.count(), 7)
        self.assertFalse(DoctorWorkingPattern.objects.exclude(company=self.company, clinician=self.doctor).exists())
        self.assertEqual(DoctorWorkingPattern.objects.get(weekday=0).starts_at, time(9))

    def test_time_off_is_saved_in_sast_and_cancellation_is_soft(self):
        self.login()
        response = self.client.post(reverse('portal:availability-time-off-create'), self.time_off_data())
        self.assertEqual(response.status_code, 302)
        leave = DoctorTimeOff.objects.get()
        self.assertEqual((leave.company_id, leave.clinician_id), (self.company.pk, self.doctor.pk))
        self.assertEqual((leave.starts_at, leave.ends_at), (self.at(10), self.at(11)))
        response = self.client.post(reverse('portal:availability-time-off-cancel', args=[leave.pk]), {'schedule_context': self.token()})
        self.assertEqual(response.status_code, 302)
        leave.refresh_from_db()
        self.assertFalse(leave.is_active)
        self.assertEqual(leave.cancelled_by_id, self.doctor.pk)
        self.assertIsNotNone(leave.cancelled_at)
        self.assertEqual(DoctorTimeOff.objects.count(), 1)

    def test_invalid_time_off_preserves_its_draft_on_schedule(self):
        self.login()
        data = self.time_off_data()
        data['timeoff-ends_at'] = self.at(9).strftime('%Y-%m-%dT%H:%M')
        before = self.mutation_snapshot()
        response = self.client.post(reverse('portal:availability-time-off-create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/staff_schedule.html')
        self.assertTrue(response.context['time_off_form'].errors)
        self.assertEqual(response.context['time_off_form']['ends_at'].value(), data['timeoff-ends_at'])
        self.assertEqual(self.mutation_snapshot(), before)

    def test_other_doctor_or_other_practice_time_off_cannot_be_cancelled_by_url(self):
        colleague_leave = self.leave(clinician=self.colleague)
        beta_leave = self.leave(company=self.beta)
        self.login()
        before = self.mutation_snapshot()
        for leave in (colleague_leave, beta_leave):
            with self.subTest(leave=leave.pk):
                response = self.client.post(reverse('portal:availability-time-off-cancel', args=[leave.pk]), {'schedule_context': self.token()})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.mutation_snapshot(), before)

    def test_missing_and_invalid_schedule_context_blocks_all_mutations(self):
        self.login()
        leave = self.leave()
        before = self.mutation_snapshot()
        for token in ('', 'invalid-token', self.token() + 'tampered'):
            for url, data in (
                (reverse('portal:availability-working-pattern'), self.pattern_data()),
                (reverse('portal:availability-time-off-create'), self.time_off_data()),
                (reverse('portal:availability-time-off-cancel', args=[leave.pk]), {}),
            ):
                with self.subTest(url=url, token=token):
                    response = self.client.post(url, {**data, 'schedule_context': token})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(self.mutation_snapshot(), before)

    def test_expired_schedule_context_cannot_save_a_pattern(self):
        self.login()
        data = self.pattern_data()
        before = self.mutation_snapshot()
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 12 * 3600 + 1):
            response = self.client.post(reverse('portal:availability-working-pattern'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mutation_snapshot(), before)

    def test_old_tab_cannot_save_availability_into_the_newly_selected_practice(self):
        self.login()
        pattern = self.pattern_data()
        time_off = self.time_off_data()
        self.client.post(reverse('portal:activate-company', args=[self.beta.slug]), {'next': reverse('portal:staff-schedule')})
        before = self.mutation_snapshot()
        for route, data in (('availability-working-pattern', pattern), ('availability-time-off-create', time_off)):
            with self.subTest(route=route):
                response = self.client.post(reverse(f'portal:{route}'), data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.mutation_snapshot(), before)

    def test_inactive_doctor_membership_cannot_write(self):
        self.login()
        CompanyMembership.objects.filter(user=self.doctor, company=self.company).update(is_active=False)
        # With the other membership also inactive, there is no authorised fallback.
        CompanyMembership.objects.filter(user=self.doctor, company=self.beta).update(is_active=False)
        before = self.mutation_snapshot()
        self.assertEqual(self.client.post(reverse('portal:availability-working-pattern'), self.pattern_data()).status_code, 403)
        self.assertEqual(self.mutation_snapshot(), before)

    def test_schedule_get_has_no_availability_writes_and_admin_views_are_read_only(self):
        self.login()
        before = self.mutation_snapshot()
        response = self.client.get(reverse('portal:staff-schedule'), {'date': self.monday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="schedule_context"')
        self.assertEqual(self.mutation_snapshot(), before)
        for user in (self.admin, self.super_admin):
            self.login(user)
            response = self.client.get(reverse('portal:staff-schedule'), {'date': self.monday.isoformat(), 'clinician': self.doctor.pk})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context['can_manage_availability'])
            for name in ('availability-working-pattern', 'availability-time-off-create'):
                self.assertNotContains(response, f'action="{reverse(f"portal:{name}")}')

    def test_existing_booking_is_not_moved_when_doctor_adds_time_off(self):
        appointment = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor, starts_at=self.at(10), duration_minutes=30,
        )
        self.login()
        response = self.client.post(reverse('portal:availability-time-off-create'), self.time_off_data())
        self.assertEqual(response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual((appointment.starts_at, appointment.status), (self.at(10), Appointment.Status.BOOKED))
        self.assertEqual(Appointment.objects.count(), 1)

    def test_double_time_off_post_does_not_duplicate_the_record_or_audit(self):
        self.login()
        data = self.time_off_data()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.client.post(reverse('portal:availability-time-off-create'), data).status_code, 302)
        self.assertEqual(DoctorTimeOff.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='availability.time_off_created').count(), 1)

    def assert_panel_url(self, url, parameter, page):
        parsed = urlsplit(url)
        self.assertEqual(parsed.path, reverse('portal:staff-schedule'))
        params = parse_qs(parsed.query)
        self.assertEqual(params.get('date'), [self.monday.isoformat()])
        self.assertEqual(params.get('view'), ['calendar'])
        self.assertEqual(params.get('clinician'), [str(self.doctor.pk)])
        self.assertEqual(params.get(parameter), [str(page)])

    def test_time_off_page_two_exposes_record_twenty_one_and_its_cancel_action(self):
        records = []
        for day in range(21):
            start = self.at(10) + timedelta(days=day)
            records.append(DoctorTimeOff.objects.create(
                company=self.company, clinician=self.doctor, starts_at=start, ends_at=start + timedelta(hours=1), reason='leave',
            ))
        foreign = self.leave(company=self.beta)
        self.login()
        response = self.client.get(reverse('portal:staff-schedule'), {
            'date': self.monday.isoformat(), 'clinician': self.doctor.pk, 'view': 'calendar',
        })
        self.assertEqual(len(response.context['time_off_page_obj']), 20)
        self.assertEqual(response.context['time_off_page_obj'].paginator.count, 21)
        next_url = response.context['time_off_next_url']
        self.assert_panel_url(next_url, 'time_off_page', 2)
        second = self.client.get(next_url)
        self.assertEqual(second.context['time_off_page_obj'].number, 2)
        self.assertEqual([entry.pk for entry in second.context['time_off_entries']], [records[-1].pk])
        self.assert_panel_url(second.context['time_off_previous_url'], 'time_off_page', 1)
        entry = second.context['time_off_entries'][0]
        self.assertContains(second, reverse('portal:availability-time-off-cancel', args=[entry.pk]))
        self.assertNotContains(second, reverse('portal:availability-time-off-cancel', args=[foreign.pk]))
        response = self.client.post(entry.cancel_url, {'schedule_context': second.context['schedule_context']})
        self.assertEqual(response.status_code, 302)
        records[-1].refresh_from_db()
        foreign.refresh_from_db()
        self.assertFalse(records[-1].is_active)
        self.assertTrue(foreign.is_active)

    def test_affected_appointments_page_two_is_reachable_with_filters_preserved(self):
        DoctorWorkingPattern.objects.bulk_create([
            DoctorWorkingPattern(company=company, clinician=self.doctor, weekday=day, is_working=False)
            for company in (self.company, self.beta) for day in range(7)
        ])
        records = [Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=self.at(9) + timedelta(days=day), duration_minutes=30,
        ) for day in range(21)]
        beta_patient = Patient.objects.create(company=self.beta, first_name='Secret', last_name='Beta Patient')
        foreign = Appointment.objects.create(company=self.beta, patient=beta_patient, clinician=self.doctor, starts_at=self.at(9), duration_minutes=30)
        self.login()
        response = self.client.get(reverse('portal:staff-schedule'), {
            'date': self.monday.isoformat(), 'clinician': self.doctor.pk, 'view': 'calendar',
        })
        self.assertEqual(response.context['affected_count'], 21)
        self.assertEqual(len(response.context['affected_page_obj']), 20)
        next_url = response.context['affected_next_url']
        self.assert_panel_url(next_url, 'affected_page', 2)
        second = self.client.get(next_url)
        self.assertEqual(second.context['affected_page_obj'].number, 2)
        self.assertEqual([appointment.pk for appointment in second.context['affected_appointments']], [records[-1].pk])
        self.assert_panel_url(second.context['affected_previous_url'], 'affected_page', 1)
        self.assertNotContains(second, 'Secret Beta Patient')
        self.assertNotIn(foreign.pk, [appointment.pk for appointment in second.context['affected_appointments']])

    def test_all_clinicians_admin_view_shows_company_scoped_affected_card(self):
        DoctorWorkingPattern.objects.bulk_create([
            DoctorWorkingPattern(company=self.company, clinician=doctor, weekday=day, is_working=False)
            for doctor in (self.doctor, self.colleague) for day in range(7)
        ])
        own = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor, starts_at=self.at(9))
        colleague_patient = Patient.objects.create(company=self.company, first_name='Colleague', last_name='Patient')
        colleague = Appointment.objects.create(company=self.company, patient=colleague_patient, clinician=self.colleague, starts_at=self.at(10))
        beta_patient = Patient.objects.create(company=self.beta, first_name='Secret', last_name='Other Practice')
        Appointment.objects.create(company=self.beta, patient=beta_patient, clinician=self.doctor, starts_at=self.at(10))
        self.leave(company=self.beta)
        self.login(self.admin)
        response = self.client.get(reverse('portal:staff-schedule'), {'date': self.monday.isoformat(), 'clinician': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Appointments needing attention')
        self.assertEqual({appointment.pk for appointment in response.context['affected_appointments']}, {own.pk, colleague.pk})
        self.assertNotContains(response, 'Secret Other Practice')
