"""Day, Week and Month views share one authorised, read-only diary.

The URL keeps its original values: view=table is Day, view=week is Week and view=calendar is Month.
"""

from django.test import override_settings
import re
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from care.availability import create_days_off, set_special_day
from care.models import Appointment, AppointmentProposal, AvailabilitySlot, AuditEvent, DoctorWorkingPattern, PatientEvent
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
        for mode in ('table', 'week', 'calendar'):
            with self.subTest(mode=mode):
                response = self.page(view=mode, clinician=self.colleague.pk, page=4)
                self.assert_schedule_url(response.context['table_view_url'], clinician=self.colleague, view='table')
                self.assert_schedule_url(response.context['week_view_url'], clinician=self.colleague, view='week')
                self.assert_schedule_url(response.context['calendar_view_url'], clinician=self.colleague)
                markup = ScheduleMarkupParser(response.content.decode())
                self.assertIn(('input', {'type': 'hidden', 'name': 'view', 'value': mode}), markup.elements)
                for target_mode in ('table', 'week', 'calendar'):
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
            # A day opens that date in Day view.
            self.assert_schedule_url(day['url'], day=day['date'], clinician=self.doctor, view='table')

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

    def test_every_booking_is_listed_in_time_order_without_losing_paginated_bookings(self):
        start = datetime.combine(self.day, time(8), tzinfo=self.sast)
        bookings = [self.appointment(starts_at=start + timedelta(minutes=15 * number)) for number in range(25)]
        later = self.appointment(day=self.day + timedelta(days=1))
        first = self.page(view='calendar', clinician=self.doctor.pk)
        second = self.page(view='calendar', clinician=self.doctor.pk, page=2)
        for response in (first, second):
            days = self.calendar_days(response)
            self.assertEqual(days[self.day]['count'], 25)
            self.assertEqual([item.pk for item in days[self.day]['appointments']], [item.pk for item in bookings])
            self.assertEqual([item.pk for item in days[self.day + timedelta(days=1)]['appointments']], [later.pk])
            self.assertEqual(response.context['metrics']['booked'], 25)
            self.assertEqual(response.context['month_booked'], 26)
            self.assertEqual(response.context['paginator'].count, 25)
            self.assertEqual(parse_qs(response.context['pagination_query']), {
                'date': [self.day.isoformat()], 'clinician': [str(self.doctor.pk)], 'view': ['calendar'],
            })
        first_ids = {item.pk for item in first.context['appointments']}
        second_ids = {item.pk for item in second.context['appointments']}
        self.assertEqual((len(first_ids), len(second_ids)), (20, 5))
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, {item.pk for item in bookings})
        self.assertContains(first, '26 booked this month')
        # Each booking is a chip with its start time and patient that opens the appointment's details.
        booking = bookings[0]
        self.assertContains(first, f'href="{reverse("portal:appointment-detail", args=[booking.pk])}"')
        self.assertContains(first, f'<time datetime="{booking.starts_at.astimezone(self.sast).isoformat()}">08:00</time> <span>{booking.patient}</span>', html=True)

    def test_calendar_shows_every_booking_coloured_by_status_but_counts_leave_out_cancelled(self):
        bookings = [self.appointment(hour=9 + number, status=status) for number, status in enumerate(Appointment.Status.values)]
        response = self.page(view='calendar')
        day = self.calendar_days(response)[self.day]
        self.assertEqual(day['count'], len(bookings) - 1)
        self.assertEqual([item.pk for item in day['appointments']], [item.pk for item in bookings])
        self.assertEqual(response.context['month_booked'], len(bookings) - 1)
        for booking in bookings:
            label = f'{booking.patient}, {booking.get_status_display().lower()}'
            self.assertContains(response, f'class="schedule-month__chip schedule-month__chip--{booking.status}"')
            self.assertContains(response, label)
        self.assertContains(response, 'aria-label="Booking colours"')
        for status in Appointment.Status.values:
            self.assertContains(response, f'<i class="schedule-month__chip-swatch schedule-month__chip--{status}"></i>')
        # Days with nothing booked are left empty.
        self.assertNotContains(response, '&mdash;</span>')
        self.assertEqual(response.context['metrics'], {status: 1 for status in Appointment.Status.values})

    def test_a_month_day_opens_that_date_in_day_view(self):
        self.appointment()
        next_day = self.day + timedelta(days=1)
        next_booking = self.appointment(day=next_day)
        first = self.page(view='calendar')
        days = self.calendar_days(first)
        self.assertEqual([day['date'] for day in days.values() if day['is_selected']], [self.day])
        response = self.client.get(days[next_day]['url'])
        self.assertEqual(response.context['schedule_view'], 'table')
        self.assertEqual(response.context['selected_date'], next_day)
        self.assertEqual([item.pk for item in response.context['appointments']], [next_booking.pk])
        self.assertContains(response, f'Booked &middot; {next_day:%A} {next_day.day} {next_day:%B}')

    def test_day_view_arrows_step_one_day_and_month_arrows_one_month(self):
        response = self.page()
        self.assert_schedule_url(response.context['prev_day_url'], day=self.day - timedelta(days=1), clinician=self.doctor, view='table')
        self.assert_schedule_url(response.context['next_day_url'], day=self.day + timedelta(days=1), clinician=self.doctor, view='table')
        for edge, key in ((date(1900, 1, 1), 'prev_day_url'), (date(2100, 12, 31), 'next_day_url')):
            with self.subTest(edge=edge):
                self.assertIsNone(self.page(date=edge.isoformat()).context[key])

    def test_month_shows_each_days_state_for_one_doctors_diary(self):
        DoctorWorkingPattern.objects.bulk_create([
            DoctorWorkingPattern(company=self.company, clinician=self.doctor, weekday=day, is_working=day < 5,
                                 starts_at=time(9) if day < 5 else None, ends_at=time(17) if day < 5 else None)
            for day in range(7)
        ])
        special_on, off_on = self.day + timedelta(days=1), self.day + timedelta(days=2)  # Tuesday and Wednesday
        with timezone.override(self.sast):
            set_special_day(company=self.company, clinician=self.doctor, actor=self.doctor, day=special_on,
                            starts_at=time(8), ends_at=time(10))
            create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor, first_day=off_on, last_day=off_on)
        response = self.page(view='calendar')
        days = self.calendar_days(response)
        self.assertEqual(days[self.day]['state']['kind'], 'work')
        self.assertEqual(days[special_on]['state'], {'kind': 'special', 'starts_at': time(8), 'ends_at': time(10)})
        self.assertEqual(days[off_on]['state']['kind'], 'off')
        self.assertEqual(days[self.day + timedelta(days=5)]['state']['kind'], 'not_working')
        self.assertContains(response, 'schedule-month__day--off')
        self.assertContains(response, 'schedule-month__day--special')
        self.assertContains(response, '<span class="schedule-month__tag">Leave</span>', html=True)
        self.assertContains(response, '08:00&ndash;10:00')
        self.assertContains(response, 'schedule-month__swatch--special')
        self.assertContains(response, 'Not working</li>')
        # Day view shows the day's state as a coloured band above the table.
        self.assertContains(self.page(date=off_on.isoformat()), '<p class="schedule-day-state schedule-day-state--off">Leave</p>', html=True)
        # With every clinician shown together there is no single day state.
        self.login(self.admin)
        response = self.page(view='calendar')
        self.assertIsNone(self.calendar_days(response)[self.day]['state'])
        self.assertNotContains(response, 'Not working</li>')

    def test_doctor_sees_only_their_calendar_without_a_diary_picker_or_open_times(self):
        self.appointment()
        response = self.page()
        # The heading matches the menu item.
        self.assertContains(response, '<h1>Calendar &amp; Bookings</h1>', html=True)
        self.assertNotContains(response, 'class="schedule-clinician"')
        self.assertNotContains(response, 'id="availability-heading"')
        self.assertEqual(response.context['day_diaries'], [])
        self.assertNotContains(response, '<th scope="col">Clinician</th>', html=True)
        self.login(self.admin)
        response = self.page()
        self.assertContains(response, '<h1>Calendar &amp; Bookings</h1>', html=True)
        # One Clinician dropdown above the diary, which applies when changed; no date field or View schedule button.
        self.assertContains(response, 'class="schedule-clinician"')
        self.assertContains(response, '<label for="id_clinician">Clinician</label>', html=True)
        self.assertContains(response, '<option value="" selected>All clinicians</option>', html=True)
        self.assertContains(response, 'data-submit-on-change')
        self.assertNotContains(response, 'View schedule')
        self.assertNotContains(response, 'staff-filter-form--schedule')
        self.assertContains(response, 'id="availability-heading"')
        self.assertContains(response, '<th scope="col">Clinician</th>', html=True)

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
        for mode in ('table', 'week', 'calendar'):
            with self.subTest(mode=mode):
                response = self.page(view=mode, page=3)
                self.assertEqual(response.context['next_appointment_date'], next_day)
                self.assert_schedule_url(response.context['next_appointment_url'], day=next_day, clinician=self.doctor, view=mode)
                self.assertContains(response, 'Next appointment day:')
        # Practice administrators don't get the button.
        self.login(self.admin)
        self.assertNotContains(self.page(), 'Next appointment day:')

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
                self.assertEqual(response.context['day_diaries'], [])
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
        for mode in ('table', 'week', 'calendar'):
            response = self.page(view=mode)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item.pk for item in response.context['appointments']], [booking.pk])
        after = {model: list(model.objects.order_by('pk').values()) for model in tracked_models}
        self.assertEqual(before, after)

    # Week view (view=week): Monday to Sunday of the ISO week the chosen date falls in.

    def week_days(self, response):
        return {day['date']: day for day in response.context['week_days']}

    def week_ids(self, response):
        return [appointment.pk for day in response.context['week_days'] for appointment in day['appointments']]

    def test_week_heading_uses_the_iso_week_number_and_starts_on_monday(self):
        for chosen, heading in (
            ('2026-10-07', 'Week 41 · 5 Oct 2026 – 11 Oct 2026'),
            ('2026-01-01', 'Week 1 · 29 Dec 2025 – 4 Jan 2026'),
            ('2025-12-29', 'Week 1 · 29 Dec 2025 – 4 Jan 2026'),
            ('2027-01-01', 'Week 53 · 28 Dec 2026 – 3 Jan 2027'),
            ('2038-05-23', 'Week 20 · 17 May 2038 – 23 May 2038'),
        ):
            with self.subTest(date=chosen):
                response = self.page(view='week', date=chosen)
                self.assertEqual(response.context['schedule_view'], 'week')
                found = re.search(r'<h2 id="week-heading" tabindex="-1">(.*?)</h2>', response.content.decode())
                self.assertEqual(unescape(re.sub(r'<[^>]+>', '', found.group(1))), heading)
                days = [day['date'] for day in response.context['week_days']]
                self.assertEqual(len(days), 7)
                self.assertEqual([day.weekday() for day in days], list(range(7)))
                self.assertEqual(days[0], response.context['week_start'])
                self.assertEqual(days[-1], response.context['week_end'])
                self.assertIn(date.fromisoformat(chosen), days)
        # Any day of the week shows the same week.
        for offset in range(7):
            with self.subTest(offset=offset):
                response = self.page(view='week', date=(date(2038, 5, 17) + timedelta(days=offset)).isoformat())
                self.assertEqual(response.context['week_start'], date(2038, 5, 17))
                self.assertEqual([day['is_selected'] for day in response.context['week_days']], [day == offset for day in range(7)])

    def test_week_arrows_step_a_week_and_days_open_in_day_view(self):
        response = self.page(view='week', page=2)
        self.assert_schedule_url(response.context['prev_week_url'], day=self.day - timedelta(days=7), clinician=self.doctor, view='week')
        self.assert_schedule_url(response.context['next_week_url'], day=self.day + timedelta(days=7), clinician=self.doctor, view='week')
        self.assert_schedule_url(response.context['today_url'], day=timezone.localdate(), clinician=self.doctor, view='week')
        self.assertContains(response, 'aria-label="Previous week"')
        self.assertContains(response, 'aria-label="Next week"')
        # Not this week: a link back to it.
        self.assertContains(response, '>This week</a>')
        for day in response.context['week_days']:
            self.assert_schedule_url(day['url'], day=day['date'], clinician=self.doctor, view='table')
            self.assertContains(response, f'href="{escape(day["url"])}#appointment-list-heading"')
        opened = self.client.get(response.context['week_days'][3]['url'])
        self.assertEqual(opened.context['schedule_view'], 'table')
        self.assertEqual(opened.context['selected_date'], date(2038, 5, 20))
        # This week: today is marked and there is no link back to it.
        today = timezone.localdate()
        response = self.page(view='week', date=today.isoformat())
        self.assertTrue(response.context['week_has_today'])
        self.assertNotContains(response, '>This week</a>')
        self.assertEqual([day['date'] for day in response.context['week_days'] if day['is_today']], [today])
        self.assertContains(response, 'schedule-week__day--today')
        self.assertContains(response, 'aria-current="date"', count=1)

    def test_week_arrows_stop_at_the_supported_dates(self):
        response = self.page(view='week', date='1900-01-03')
        self.assertIsNone(response.context['prev_week_url'])
        self.assert_schedule_url(response.context['next_week_url'], day=date(1900, 1, 10), clinician=self.doctor, view='week')
        # The last week runs past 31 Dec 2100: those days show without links, and there is no next week.
        response = self.page(view='week', date='2100-12-31')
        self.assertIsNone(response.context['next_week_url'])
        self.assert_schedule_url(response.context['prev_week_url'], day=date(2100, 12, 24), clinician=self.doctor, view='week')
        days = self.week_days(response)
        self.assertEqual([day['date'] for day in days.values() if not day['is_enabled']], [date(2101, 1, 1), date(2101, 1, 2)])
        self.assertTrue(all((day['url'] is None) == (not day['is_enabled']) for day in days.values()))
        # A week later than a Saturday lands on the last supported date.
        response = self.page(view='week', date='2100-12-25')
        self.assert_schedule_url(response.context['next_week_url'], day=date(2100, 12, 31), clinician=self.doctor, view='week')

    def test_day_week_month_switcher_in_that_order(self):
        urls = {'Day': 'table_view_url', 'Week': 'week_view_url', 'Month': 'calendar_view_url'}
        for mode, label in (('table', 'Day'), ('week', 'Week'), ('calendar', 'Month')):
            with self.subTest(mode=mode):
                response = self.page(view=mode)
                markup = response.content.decode()
                switcher = markup[markup.index('<nav class="staff-view-toggle'):]
                switcher = ScheduleMarkupParser(switcher[:switcher.index('</nav>')])
                links = [attrs for tag, attrs in switcher.elements if tag == 'a']
                self.assertEqual([attrs['href'] for attrs in links], [response.context[urls[name]] for name in ('Day', 'Week', 'Month')])
                self.assertEqual([attrs.get('aria-current') for attrs in links], ['page' if name == label else None for name in ('Day', 'Week', 'Month')])
                self.assertInHTML(f'<a class="is-active" aria-current="page" href="{response.context[urls[label]]}">{label}</a>', markup)

    def test_week_is_the_doctors_own_diary_and_this_practice_only(self):
        own = self.appointment()
        sunday = self.appointment(day=date(2038, 5, 23), hour=16)
        self.appointment(clinician=self.colleague, hour=10)
        self.appointment(company=self.other_company, patient=self.foreign_patient, hour=11)
        self.appointment(patient=self.inactive_patient, hour=12)
        self.appointment(day=date(2038, 5, 24))  # The next Monday.
        response = self.page(view='week')
        self.assertEqual(self.week_ids(response), [own.pk, sunday.pk])
        self.assertEqual(response.context['week_booked'], 2)
        self.assertContains(response, '2 booked this week')
        # No colleague's bookings (Create appointment still lists them as someone to book with).
        self.assertNotContains(response, '<span class="schedule-week__meta">Other Doctor</span>', html=True)
        # A doctor can't open a colleague's week.
        response = self.page(view='week', clinician=self.colleague.pk)
        self.assertIn('clinician', response.context['filter_form'].errors)
        self.assertEqual(response.context['week_days'], [])
        self.assertNotContains(response, 'id="week-heading"')

    def test_administrators_see_every_clinicians_week_or_choose_one(self):
        own = self.appointment()
        other = self.appointment(clinician=self.colleague, hour=10)
        self.appointment(company=self.other_company, patient=self.foreign_patient, hour=11)
        for user in (self.admin, self.super_admin):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.page(view='week')
                self.assertEqual(self.week_ids(response), [own.pk, other.pk])
                # With everyone's bookings together, each says whose it is.
                self.assertContains(response, '<span class="schedule-week__meta">Other Doctor</span>', html=True)
                self.assertNotContains(response, 'schedule-month__key')
                response = self.page(view='week', clinician=self.colleague.pk)
                self.assertEqual(self.week_ids(response), [other.pk])
                self.assertNotContains(response, '<span class="schedule-week__meta">Other Doctor</span>', html=True)
                self.assert_schedule_url(response.context['next_week_url'], day=self.day + timedelta(days=7), clinician=self.colleague, view='week')

    def test_week_shows_leave_and_special_hours_like_the_month(self):
        DoctorWorkingPattern.objects.bulk_create([
            DoctorWorkingPattern(company=self.company, clinician=self.doctor, weekday=day, is_working=day < 5,
                                 starts_at=time(9) if day < 5 else None, ends_at=time(17) if day < 5 else None)
            for day in range(7)
        ])
        special_on, off_on = self.day + timedelta(days=1), self.day + timedelta(days=2)  # Wednesday and Thursday
        set_special_day(company=self.company, clinician=self.doctor, actor=self.doctor, day=special_on,
                        starts_at=time(8), ends_at=time(10))
        create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor, first_day=off_on, last_day=off_on)
        on_leave = self.appointment(day=off_on, hour=11)
        response = self.page(view='week')
        days = self.week_days(response)
        self.assertEqual(days[self.day]['state']['kind'], 'work')
        self.assertEqual(days[special_on]['state'], {'kind': 'special', 'starts_at': time(8), 'ends_at': time(10)})
        self.assertEqual(days[off_on]['state']['kind'], 'off')
        self.assertEqual(days[date(2038, 5, 22)]['state']['kind'], 'not_working')
        self.assertContains(response, 'schedule-week__day--off', count=1)
        self.assertContains(response, 'schedule-week__day--special', count=1)
        self.assertContains(response, 'schedule-week__day--not-working', count=2)
        # Leave and special-hours days are tagged top right; the special tag reads "Special hours", then the icon and hours.
        self.assertContains(response, '<span class="schedule-week__tag schedule-week__tag--off">Leave</span>', html=True)
        self.assertContains(response, '<span class="schedule-week__tag-label">Special hours</span>', html=True)
        self.assertContains(response, 'class="special-hours-icon"', count=1)  # The day's tag; the key shows a blue swatch.
        self.assertContains(response, 'schedule-month__swatch--special')
        self.assertContains(response, '08:00&ndash;10:00</span>')
        self.assertContains(response, 'Not working</p>', count=2)
        self.assertContains(response, 'Not working</li>')
        # A booking left on leave shows its own status to the clinician, with no special mention.
        self.assertEqual(days[off_on]['appointments'][0].pk, on_leave.pk)
        self.assertIsNone(days[off_on]['appointments'][0].attention)
        self.assertNotContains(response, 'schedule-week__booking--attention')
        self.assertNotContains(response, 'Doctor unavailable')
        # The practice sees it marked, as in Day view.
        self.login(self.admin)
        response = self.page(view='week', clinician=self.doctor.pk)
        self.assertEqual(self.week_days(response)[off_on]['appointments'][0].attention, 'day_off')
        self.assertContains(response, 'schedule-week__booking--attention', count=1)
        self.assertContains(response, 'schedule-pill--leave">Doctor unavailable')
        # Every clinician together has no single day state.
        response = self.page(view='week')
        self.assertIsNone(self.week_days(response)[off_on]['state'])
        self.assertNotContains(response, 'schedule-week__day--off')

    def test_week_places_bookings_on_their_south_african_day(self):
        # 23:30 SAST is still 21:30 UTC the same day; 00:30 SAST is 22:30 UTC the day before.
        late = self.appointment(starts_at=datetime.combine(self.day, time(23, 30), tzinfo=self.sast))
        early = self.appointment(starts_at=datetime.combine(self.day + timedelta(days=1), time(0, 30), tzinfo=self.sast))
        sunday_late = self.appointment(starts_at=datetime.combine(date(2038, 5, 23), time(23, 30), tzinfo=self.sast))
        monday_first = self.appointment(starts_at=datetime(2038, 5, 16, 22, tzinfo=datetime_timezone.utc))  # 00:00 SAST Monday
        self.appointment(starts_at=datetime(2038, 5, 16, 21, 59, tzinfo=datetime_timezone.utc))  # 23:59 SAST the Sunday before
        self.appointment(starts_at=datetime(2038, 5, 23, 22, tzinfo=datetime_timezone.utc))  # 00:00 SAST the next Monday
        response = self.page(view='week')
        days = self.week_days(response)
        self.assertEqual([item.pk for item in days[date(2038, 5, 17)]['appointments']], [monday_first.pk])
        self.assertEqual([item.pk for item in days[self.day]['appointments']], [late.pk])
        self.assertEqual([item.pk for item in days[self.day + timedelta(days=1)]['appointments']], [early.pk])
        self.assertEqual([item.pk for item in days[date(2038, 5, 23)]['appointments']], [sunday_late.pk])
        self.assertEqual(len(self.week_ids(response)), 4)
        self.assertContains(response, '>23:30</time>', count=2)
        self.assertContains(response, '>00:30</time>', count=1)

    def test_week_lists_every_booking_in_time_order_with_its_status(self):
        statuses = [Appointment.Status.NO_SHOW, Appointment.Status.BOOKED, Appointment.Status.CANCELLED, Appointment.Status.COMPLETED]
        bookings = [self.appointment(hour=12 - number, status=status) for number, status in enumerate(statuses)]
        response = self.page(view='week')
        day = self.week_days(response)[self.day]
        self.assertEqual([item.pk for item in day['appointments']], [item.pk for item in reversed(bookings)])
        # Cancelled bookings stay listed but aren't counted as booked.
        self.assertEqual(day['count'], 3)
        self.assertEqual(response.context['week_booked'], 3)
        for status in statuses:
            self.assertContains(response, f'schedule-week__booking schedule-week__booking--{status}"')
            # A booking is booked unless it says otherwise, so only the other statuses get a pill.
            pill = f'<span class="console-status console-status--{status}">{Appointment.Status(status).label}</span>'
            self.assertContains(response, pill, count=0 if status == Appointment.Status.BOOKED else 1, html=True)
            if status != Appointment.Status.BOOKED:
                # The status sits top right, beside the time.
                self.assertContains(response, f'</time> <span class="console-status console-status--{status}">')
        booking = bookings[1]
        self.assertContains(response, f'href="{reverse("portal:appointment-detail", args=[booking.pk])}"')
        self.assertContains(response, f'<span class="schedule-week__patient">{booking.patient}</span>', html=True)
        self.assertContains(response, f'{booking.get_appointment_type_display()} &middot; <span class="schedule-week__duration">15 min</span>')
        # Empty days say so quietly.
        self.assertContains(response, '<p class="schedule-week__empty">Nothing booked</p>', count=6, html=True)

    def test_week_loads_its_bookings_in_one_query_however_many_days_are_busy(self):
        self.appointment()

        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.page(view='week').status_code, 200)
            return len(queries)

        one_day = count_queries()
        for offset in range(-1, 6):
            for hour in (8, 14):
                self.appointment(day=self.day + timedelta(days=offset), hour=hour)
        self.assertEqual(count_queries(), one_day)

    def test_changing_leave_from_week_view_returns_to_it(self):
        response = self.page(view='week')
        query = f'?date={self.day.isoformat()}&view=week&clinician={self.doctor.pk}'
        response = self.client.post(reverse('portal:availability-special-day-set') + query, {
            'schedule_context': response.context['schedule_context'],
            'special-date': self.day.isoformat(), 'special-starts_at': '08:00', 'special-ends_at': '12:00',
        })
        self.assertEqual(response.status_code, 302)
        self.assert_schedule_url(response.url, clinician=self.doctor, view='week')
