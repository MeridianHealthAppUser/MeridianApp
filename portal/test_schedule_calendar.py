"""Calendar and day-table views share one authorised, read-only diary."""

from django.test import override_settings
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AppointmentProposal, AvailabilitySlot, AuditEvent, PatientEvent
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class ScheduleMarkupParser(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ScheduleCalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Calendar Practice', slug='calendar-practice')
        cls.other_company = Company.objects.create(name='Other Calendar Practice', slug='other-calendar-practice')
        cls.doctor = users.create_user(email='calendar-doctor@example.test', first_name='Calendar', last_name='Doctor')
        cls.colleague = users.create_user(email='calendar-colleague@example.test', first_name='Other', last_name='Doctor')
        cls.foreign_doctor = users.create_user(email='calendar-foreign@example.test', first_name='Foreign', last_name='Doctor')
        cls.admin = users.create_user(email='calendar-admin@example.test', first_name='Practice', last_name='Admin')
        cls.super_admin = users.create_user(email='calendar-super@example.test', first_name='Practice', last_name='Super')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.colleague, CompanyMembership.Role.DOCTOR),
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        for user in (cls.doctor, cls.foreign_doctor):
            CompanyMembership.objects.create(company=cls.other_company, user=user, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, first_name='Calendar', last_name='Patient')
        cls.foreign_patient = Patient.objects.create(company=cls.other_company, first_name='Foreign', last_name='Patient')
        cls.inactive_patient = Patient.objects.create(
            company=cls.company, first_name='Inactive', last_name='Patient', is_active=False,
        )
        cls.day = date(2038, 5, 18)
        cls.sast = ZoneInfo('Africa/Johannesburg')

    def setUp(self):
        self.timezone_override = timezone.override(self.sast)
        self.timezone_override.__enter__()
        self.addCleanup(self.timezone_override.__exit__, None, None, None)
        self.login()

    def login(self, user=None, company=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def appointment(self, *, day=None, hour=9, starts_at=None, **overrides):
        values = {
            'company': self.company, 'patient': self.patient, 'clinician': self.doctor,
            'starts_at': starts_at or datetime.combine(day or self.day, time(hour), tzinfo=self.sast),
            'duration_minutes': 15,
        }
        values.update(overrides)
        return Appointment.objects.create(**values)

    def page(self, **params):
        return self.client.get(reverse('portal:staff-schedule'), {'date': self.day.isoformat(), **params})

    def calendar_days(self, response):
        return {day['date']: day for week in response.context['calendar_weeks'] for day in week}

    def calendar_ids(self, response):
        return {appointment.pk for day in self.calendar_days(response).values() for appointment in day['appointments']}

    def assert_schedule_url(self, url, *, day=None, clinician=None, view='calendar'):
        parsed = urlsplit(url)
        self.assertEqual(parsed.path, reverse('portal:staff-schedule'))
        expected = {'date': [(day or self.day).isoformat()], 'view': [view]}
        if clinician is not None:
            expected['clinician'] = [str(clinician.pk)]
        self.assertEqual(parse_qs(parsed.query), expected)

    def test_table_is_default_and_invalid_modes_fall_back_without_calendar(self):
        booking = self.appointment()
        for mode in (None, '', 'month', 'CALENDAR', '<script>'):
            with self.subTest(mode=mode):
                response = self.page(**({} if mode is None else {'view': mode}))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['schedule_view'], 'table')
                self.assertEqual(response.context['calendar_weeks'], [])
                self.assertIsNone(response.context['calendar_month'])
                self.assertEqual([item.pk for item in response.context['appointments']], [booking.pk])

    def test_toggle_and_filter_form_preserve_date_clinician_and_drop_pagination(self):
        self.login(self.admin)
        for mode in ('table', 'calendar'):
            with self.subTest(mode=mode):
                response = self.page(view=mode, clinician=self.colleague.pk, page=4)
                self.assert_schedule_url(response.context['table_view_url'], clinician=self.colleague, view='table')
                self.assert_schedule_url(response.context['calendar_view_url'], clinician=self.colleague)
                markup = ScheduleMarkupParser(response.content.decode())
                self.assertIn(('input', {'type': 'hidden', 'name': 'view', 'value': mode}), markup.elements)
                for target_mode in ('table', 'calendar'):
                    url = response.context[f'{target_mode}_view_url']
                    anchors = [attrs for tag, attrs in markup.elements if tag == 'a' and attrs.get('href') == url]
                    self.assertTrue(anchors)
                    active = any(attrs.get('aria-current') == 'page' for attrs in anchors)
                    self.assertEqual(active, target_mode == mode)

    def test_calendar_month_and_day_links_preserve_authorised_diary(self):
        response = self.page(view='calendar', page=9)
        self.assertEqual(response.context['calendar_month'], self.day.replace(day=1))
        self.assert_schedule_url(response.context['prev_month_url'], day=date(2038, 4, 18), clinician=self.doctor)
        self.assert_schedule_url(response.context['next_month_url'], day=date(2038, 6, 18), clinician=self.doctor)
        self.assert_schedule_url(response.context['today_url'], day=timezone.localdate(), clinician=self.doctor)
        self.assertTrue(all(len(week) == 7 for week in response.context['calendar_weeks']))
        self.assertTrue(all(week[0]['date'].weekday() == 0 for week in response.context['calendar_weeks']))
        for day in self.calendar_days(response).values():
            self.assert_schedule_url(day['url'], day=day['date'], clinician=self.doctor)

    def test_month_navigation_clamps_day_to_last_day_of_adjacent_month(self):
        response = self.page(view='calendar', date='2038-03-31')
        self.assert_schedule_url(response.context['prev_month_url'], day=date(2038, 2, 28), clinician=self.doctor)
        self.assert_schedule_url(response.context['next_month_url'], day=date(2038, 4, 30), clinician=self.doctor)

    def test_calendar_is_doctors_own_diary_and_current_practice_only(self):
        own = self.appointment()
        later_in_month = self.appointment(day=self.day + timedelta(days=3))
        self.appointment(clinician=self.colleague)
        self.appointment(company=self.other_company, patient=self.foreign_patient, hour=10)
        self.appointment(patient=self.inactive_patient, hour=11)
        response = self.page(view='calendar')
        self.assertEqual(self.calendar_ids(response), {own.pk, later_in_month.pk})
        self.assertEqual([item.pk for item in response.context['appointments']], [own.pk])
        self.assertEqual(response.context['metrics']['booked'], 1)

    def test_administrators_can_see_all_doctors_or_filter_one_within_practice(self):
        own = self.appointment()
        other = self.appointment(clinician=self.colleague, hour=10)
        self.appointment(company=self.other_company, patient=self.foreign_patient, hour=11)
        for user in (self.admin, self.super_admin):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.page(view='calendar')
                self.assertEqual(self.calendar_ids(response), {own.pk, other.pk})
                self.assert_schedule_url(response.context['calendar_view_url'])
                response = self.page(view='calendar', clinician=self.colleague.pk)
                self.assertEqual(self.calendar_ids(response), {other.pk})
                self.assert_schedule_url(response.context['calendar_view_url'], clinician=self.colleague)

    def test_switching_practice_changes_calendar_even_for_shared_doctor(self):
        self.appointment()
        other = self.appointment(company=self.other_company, patient=self.foreign_patient)
        self.login(company=self.other_company)
        response = self.page(view='calendar')
        self.assertEqual(self.calendar_ids(response), {other.pk})

    def test_midnight_uses_sast_day_not_utc_date(self):
        # 22:00 UTC is midnight on the following South African day.
        midnight = datetime(2038, 5, 17, 22, tzinfo=datetime_timezone.utc)
        before = self.appointment(starts_at=midnight - timedelta(minutes=1))
        first = self.appointment(starts_at=midnight)
        last = self.appointment(starts_at=midnight + timedelta(hours=23, minutes=59))
        after = self.appointment(starts_at=midnight + timedelta(days=1))
        response = self.page(view='calendar')
        days = self.calendar_days(response)
        self.assertEqual({item.pk for item in days[self.day]['appointments']}, {first.pk, last.pk})
        self.assertEqual([item.pk for item in days[self.day - timedelta(days=1)]['appointments']], [before.pk])
        self.assertEqual([item.pk for item in days[self.day + timedelta(days=1)]['appointments']], [after.pk])
        self.assertEqual({item.pk for item in response.context['appointments']}, {first.pk, last.pk})

    def test_month_query_includes_exact_sast_boundaries_and_excludes_outside_month(self):
        start = datetime(2038, 4, 30, 22, tzinfo=datetime_timezone.utc)
        end = datetime(2038, 5, 31, 22, tzinfo=datetime_timezone.utc)
        self.appointment(starts_at=start - timedelta(minutes=1))
        first = self.appointment(starts_at=start)
        last = self.appointment(starts_at=end - timedelta(minutes=1))
        self.appointment(starts_at=end)
        response = self.page(view='calendar')
        self.assertEqual(self.calendar_ids(response), {first.pk, last.pk})
        days = self.calendar_days(response)
        self.assertEqual(days[date(2038, 5, 1)]['count'], 1)
        self.assertEqual(days[date(2038, 5, 31)]['count'], 1)
        for day in days.values():
            if not day['is_current_month']:
                self.assertEqual(day['appointments'], [])
                self.assertEqual(day['count'], 0)

    def test_three_previews_and_uncapped_totals_do_not_lose_paginated_bookings(self):
        start = datetime.combine(self.day, time(8), tzinfo=self.sast)
        bookings = [self.appointment(starts_at=start + timedelta(minutes=15 * number)) for number in range(25)]
        later = self.appointment(day=self.day + timedelta(days=1))
        first = self.page(view='calendar', clinician=self.doctor.pk)
        second = self.page(view='calendar', clinician=self.doctor.pk, page=2)
        for response in (first, second):
            days = self.calendar_days(response)
            self.assertEqual(days[self.day]['count'], 25)
            self.assertEqual(days[self.day]['more_count'], 22)
            self.assertEqual([item.pk for item in days[self.day]['appointments']], [item.pk for item in bookings[:3]])
            self.assertEqual([item.pk for item in days[self.day + timedelta(days=1)]['appointments']], [later.pk])
            self.assertEqual(response.context['metrics']['booked'], 25)
            self.assertEqual(response.context['paginator'].count, 25)
            self.assertEqual(parse_qs(response.context['pagination_query']), {
                'date': [self.day.isoformat()], 'clinician': [str(self.doctor.pk)], 'view': ['calendar'],
            })
        first_ids = {item.pk for item in first.context['appointments']}
        second_ids = {item.pk for item in second.context['appointments']}
        self.assertEqual((len(first_ids), len(second_ids)), (20, 5))
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, {item.pk for item in bookings})
        self.assertContains(first, '+22 more')

    def test_calendar_totals_include_all_appointment_statuses(self):
        bookings = [self.appointment(hour=9 + number, status=status) for number, status in enumerate(Appointment.Status.values)]
        response = self.page(view='calendar')
        day = self.calendar_days(response)[self.day]
        self.assertEqual(day['count'], len(bookings))
        self.assertEqual(len(day['appointments']), 3)
        self.assertEqual(day['more_count'], 1)
        self.assertEqual(response.context['metrics'], {status: 1 for status in Appointment.Status.values})

    def test_selected_day_link_keeps_calendar_and_updates_day_table_only(self):
        own = self.appointment()
        next_day = self.day + timedelta(days=1)
        next_booking = self.appointment(day=next_day)
        first = self.page(view='calendar')
        days = self.calendar_days(first)
        self.assertEqual([day['date'] for day in days.values() if day['is_selected']], [self.day])
        response = self.client.get(days[next_day]['url'])
        self.assertEqual(response.context['schedule_view'], 'calendar')
        self.assertEqual(response.context['selected_date'], next_day)
        self.assertEqual([item.pk for item in response.context['appointments']], [next_booking.pk])
        self.assertEqual(self.calendar_ids(response), {own.pk, next_booking.pk})
        self.assertEqual([day['date'] for day in self.calendar_days(response).values() if day['is_selected']], [next_day])

    def test_today_highlight_and_today_link_use_sast(self):
        today = timezone.localdate()
        response = self.page(view='calendar', date=today.isoformat())
        self.assertTrue(self.calendar_days(response)[today]['is_today'])
        self.assertTrue(self.calendar_days(response)[today]['is_selected'])
        self.assert_schedule_url(response.context['today_url'], day=today, clinician=self.doctor)

    def test_next_appointment_day_keeps_calendar_or_table_mode_and_diary(self):
        next_day = self.day + timedelta(days=2)
        self.appointment(day=self.day + timedelta(days=1), status=Appointment.Status.CANCELLED)
        self.appointment(day=next_day)
        self.appointment(day=self.day + timedelta(days=1), clinician=self.colleague)
        for mode in ('table', 'calendar'):
            with self.subTest(mode=mode):
                response = self.page(view=mode, page=3)
                self.assertEqual(response.context['next_appointment_date'], next_day)
                self.assert_schedule_url(response.context['next_appointment_url'], day=next_day, clinician=self.doctor, view=mode)

    def test_invalid_filters_show_no_calendar_or_appointments_without_broadening_scope(self):
        self.appointment()
        for user, params, field in (
            (self.doctor, {'date': 'invalid'}, 'date'),
            (self.doctor, {'date': '2038-02-30'}, 'date'),
            (self.doctor, {'date': '1899-12-31'}, 'date'),
            (self.doctor, {'date': '2101-01-01'}, 'date'),
            (self.doctor, {'clinician': self.colleague.pk}, 'clinician'),
            (self.admin, {'clinician': self.foreign_doctor.pk}, 'clinician'),
        ):
            with self.subTest(params=params, role=user.email):
                self.login(user)
                response = self.page(view='calendar', **params)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['filter_form'].errors)
                self.assertEqual(response.context['calendar_weeks'], [])
                self.assertEqual(response.context['appointments'], [])
                self.assertEqual(response.context['open_slots'], [])
                self.assertEqual(response.context['metrics'], {status: 0 for status in Appointment.Status.values})
                self.assertNotContains(response, 'id="calendar-heading"')

    def test_supported_year_boundaries_disable_out_of_range_navigation(self):
        for chosen, unavailable_key, available_key in (
            (date(1900, 1, 1), 'prev_month_url', 'next_month_url'),
            (date(2100, 12, 31), 'next_month_url', 'prev_month_url'),
        ):
            with self.subTest(day=chosen):
                response = self.page(view='calendar', date=chosen.isoformat())
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context[unavailable_key])
                self.assertIsNotNone(response.context[available_key])
                self.assertTrue(response.context['calendar_weeks'])
                for day in self.calendar_days(response).values():
                    enabled = date(1900, 1, 1) <= day['date'] <= date(2100, 12, 31)
                    self.assertEqual(day['is_enabled'], enabled)
                    self.assertEqual(day['url'] is not None, enabled)

    def test_calendar_and_table_gets_never_modify_bookings_or_create_workflow_records(self):
        booking = self.appointment()
        tracked_models = (Appointment, AppointmentProposal, AvailabilitySlot, AuditEvent, PatientEvent)
        before = {model: list(model.objects.order_by('pk').values()) for model in tracked_models}
        for mode in ('table', 'calendar'):
            response = self.page(view=mode)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item.pk for item in response.context['appointments']], [booking.pk])
        after = {model: list(model.objects.order_by('pk').values()) for model in tracked_models}
        self.assertEqual(before, after)
