"""Staff appointment lists in the format of a patient's My Appointments (Nov 2026): Calendar's Appointments tab, where
Calendar opens, and a record's Appointments tab (portal/staff_appointments.py)."""

from datetime import timedelta
from urllib.parse import urlencode

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from care.widgets import floor_quarter_hour
from practices.models import CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class StaffAppointmentListTests(AppointmentLifecycleFixture):
    def login(self, actor, company=None):
        self.client.force_login(actor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def url(self, name, *args):
        return reverse(f'portal:{name}', args=args)

    def at(self, delta, **overrides):
        # On a quarter hour, as bookings are, so Edit can keep or move the time.
        return self.appointment(starts_at=floor_quarter_hour(timezone.now() + delta), **overrides)

    def listed(self, url=None, **query):
        response = self.client.get(url or self.url('schedule-appointments'), query)
        self.assertEqual(response.status_code, 200)
        return response, [appointment.pk for appointment in response.context['appointments']]

    def row(self, html, appointment):
        """One appointment's row in the list, from its date to the end of its actions."""
        start = html.index(f'href="{self.url("appointment-detail", appointment.pk)}"', html.index('<tbody>'))
        start = html.rindex('<tr', 0, start)
        return html[start:html.index('</tr>', start)]

    def assert_actions(self, html, appointment, expected):
        row = self.row(html, appointment)
        markers = (
            ('Open meeting room', 'Open meeting room</a>'), ('Edit', f'data-modal-open="edit-appointment-{appointment.pk}"'),
            ('Cancel', f'data-modal-open="cancel-appointment-{appointment.pk}"'),
            ('Complete', f'data-modal-open="complete-appointment-{appointment.pk}"'),
            ('No-show', f'data-modal-open="no-show-appointment-{appointment.pk}"'), ('Details', '>Details<span'),
        )
        found = [label for label, marker in markers if marker in row]
        self.assertEqual(found, expected, appointment)
        # In that order, as small buttons, as on a patient's My Appointments.
        positions = [row.index(marker) for _, marker in markers if marker in row]
        self.assertEqual(positions, sorted(positions))
        # Cancel is a plain button, not red, as for patients (James, 20 Nov 2026).
        self.assertNotIn('console-button--cancel', row)
        for label, dialog in (('Edit', 'edit'), ('Cancel', 'cancel'), ('Complete', 'complete'), ('No-show', 'no-show')):
            self.assertEqual(f'id="{dialog}-appointment-{appointment.pk}"' in html, label in expected, (label, appointment))

    def test_calendar_opens_on_appointments_its_first_tab(self):
        self.login(self.admin)
        calendar = self.client.get(self.url('staff-schedule'))
        html = calendar.content.decode()
        tabs = html[html.index('<nav class="records-tabs schedule-tabs" aria-label="Calendar"'):]
        tabs = tabs[:tabs.index('</nav>')]
        # Appointments | Calendar | Working hours & availability, in sentence case (James, 20 Nov 2026).
        self.assertLess(tabs.index('>Appointments</a>'), tabs.index('>Calendar</a>'))
        self.assertLess(tabs.index('>Calendar</a>'), tabs.index('>Working hours &amp; availability</a>'))
        self.assertIn(f'<a href="{self.url("schedule-appointments")}">Appointments</a>', tabs)
        self.assertIn('class="is-active" aria-current="page">Calendar</a>', tabs)
        self.assertEqual(calendar.context['nav_section'], 'calendar')
        self.assertContains(calendar, '<h1>Calendar</h1>')
        self.assertEqual(self.client.get(self.url('staff-schedule'), {'section': 'availability'}).context['nav_section'], 'availability')
        # The menu's Calendar opens Appointments, and is current on all three tabs and an appointment's page.
        from .navigation import build_flat_menu

        membership = CompanyMembership.objects.get(company=self.company, user=self.admin)
        for nav_section, current in (('schedule', 'page'), ('calendar', 'true'), ('availability', 'true')):
            item = next(item for item in build_flat_menu(membership, nav_section=nav_section) if item['key'] == 'schedule')
            self.assertEqual((item['label'], item['url'], item['current']), ('Calendar', self.url('schedule-appointments'), current))
        page = self.client.get(self.url('schedule-appointments'))
        self.assertTemplateUsed(page, 'portal/staff_appointments.html')
        self.assertEqual(page.context['nav_section'], 'schedule')
        self.assertContains(page, '<h1>Calendar</h1>')
        self.assertContains(page, f'<a class="console-nav__item is-active" aria-current="page" href="{self.url("schedule-appointments")}" '
                                  f'data-navigation-match="{self.url("staff-schedule")}">')
        self.assertContains(page, f'<a href="{self.url("schedule-appointments")}" class="is-active" aria-current="page">Appointments</a>')
        self.assertContains(page, f'href="{self.url("staff-schedule")}?')
        self.assertContains(page, 'section=availability')
        # Create appointment, as on the Calendar.
        self.assertContains(page, 'data-modal-open="create-appointment" aria-haspopup="dialog">Create appointment</a>')
        self.assertContains(page, '<dialog class="app-dialog" id="create-appointment"')
        self.assertContains(page, 'css/staff_appointments.css')
        # A clinician chosen on the Calendar is the one Appointments lists, and the other way round.
        calendar = self.client.get(self.url('staff-schedule'), {'clinician': self.colleague.pk})
        self.assertEqual(calendar.context['appointments_tab_url'], f'{self.url("schedule-appointments")}?clinician={self.colleague.pk}')
        page = self.client.get(self.url('schedule-appointments'), {'clinician': self.colleague.pk})
        self.assertIn(f'clinician={self.colleague.pk}', page.context['bookings_tab_url'])

    def test_upcoming_runs_soonest_first_until_each_ends_and_past_runs_latest_first(self):
        later = self.at(timedelta(days=3))
        sooner = self.at(timedelta(days=1))
        under_way = self.at(-timedelta(minutes=10), duration_minutes=30)
        cancelled = self.at(timedelta(days=2), status='cancelled')
        ended = self.at(-timedelta(hours=2), duration_minutes=30, status='completed')
        older = self.at(-timedelta(days=3), status='no_show')
        self.login(self.admin)
        response, rows = self.listed()
        self.assertEqual(rows, [under_way.pk, sooner.pk, cancelled.pk, later.pk])
        self.assertContains(response, '<span class="staff-page-card__count">4 appointments</span>')
        list_url = self.url('schedule-appointments')
        self.assertContains(response, f'<nav class="patient-order-subtabs" aria-label="Appointments"><a href="{list_url}" class="is-active" '
                                      f'aria-current="page">Upcoming</a><a href="{list_url}?list=past">Past</a></nav>')
        # My Appointments' columns, with the patient's name after the date.
        self.assertContains(response, '<th scope="col">Date and time</th><th scope="col">Patient</th><th scope="col">Appointment</th>'
                                      '<th scope="col">Clinician</th><th scope="col">Status</th><th class="patient-table__fit" scope="col">Actions</th>')
        local = timezone.localtime(sooner.starts_at)
        self.assertContains(response, f'<span>{local:%H:%M} · 30 minutes</span>')
        self.assertContains(response, '<span class="console-status console-status--cancelled">Cancelled</span>')
        self.assertContains(response, 'class="patient-table patient-appointment-table staff-phone-cards"')
        response, rows = self.listed(list='past')
        self.assertEqual(rows, [ended.pk, older.pk])
        self.assertContains(response, '<span class="console-status console-status--no_show">No show</span>')
        # Anything else is Upcoming.
        self.assertEqual(self.listed(list='nonsense')[1], [under_way.pk, sooner.pk, cancelled.pk, later.pk])

    def test_filters_carry_across_upcoming_and_past_except_status(self):
        mine = self.at(timedelta(days=1), appointment_type='initial')
        theirs = self.at(timedelta(days=2), clinician=self.colleague, appointment_type='follow_up')
        cancelled = self.at(timedelta(days=3), appointment_type='follow_up', status='cancelled')
        past_theirs = self.at(-timedelta(days=2), clinician=self.colleague, appointment_type='follow_up', status='completed')
        past_mine = self.at(-timedelta(days=1), appointment_type='follow_up')
        self.login(self.admin)
        response, rows = self.listed()
        menu = response.context['filter_menu']
        self.assertEqual([section['label'] for section in menu['sections']], ['Status', 'Date', 'Clinician', 'Appointment type'])
        # Upcoming offers only what's possible before an appointment ends; the clinicians are those in the list.
        self.assertEqual([option['value'] for option in menu['sections'][0]['options']], ['booked', 'cancelled'])
        self.assertEqual({option['value'] for option in menu['sections'][2]['options']}, {str(self.doctor.pk), str(self.colleague.pk)})
        self.assertEqual(self.listed(status='cancelled')[1], [cancelled.pk])
        self.assertEqual(self.listed(clinician=self.colleague.pk)[1], [theirs.pk])
        self.assertEqual(self.listed(type='follow_up')[1], [theirs.pk, cancelled.pk])
        self.assertEqual(self.listed(type='follow_up', status=['booked', 'cancelled'])[1], [theirs.pk, cancelled.pk])
        # A status Upcoming doesn't offer, or someone not in the list, shows nothing rather than everything, and opens
        # Filters to say so, as a record's other tables do. "all", as older links send, is no filter.
        response, rows = self.listed(status='completed')
        self.assertEqual(rows, [])
        self.assertContains(response, 'filter-menu__error')
        self.assertContains(response, 'No upcoming appointments match these filters.')
        self.assertEqual(self.listed(clinician=self.admin.pk)[1], [])
        self.assertEqual(self.listed(when='fortnight')[1], [])
        self.assertEqual(self.listed(status='all')[1], [mine.pk, theirs.pk, cancelled.pk])
        self.assertEqual(self.listed(clinician=[self.doctor.pk, self.doctor.pk])[1], [mine.pk, cancelled.pk])
        # Past keeps the clinician and type, not the status.
        response, rows = self.listed(type='follow_up', clinician=self.colleague.pk, status='booked')
        self.assertEqual(rows, [theirs.pk])
        past = next(tab for tab in response.context['appointment_tabs'] if tab['label'] == 'Past')
        self.assertEqual(past['url'], f"{self.url('schedule-appointments')}?{urlencode({'list': 'past', 'clinician': self.colleague.pk, 'type': 'follow_up'})}")
        response, rows = self.listed(list='past', type='follow_up', clinician=self.colleague.pk)
        self.assertEqual(rows, [past_theirs.pk])
        self.assertContains(response, '<input type="hidden" name="list" value="past">')
        self.assertContains(response, f'<a class="filter-menu__clear" href="{self.url("schedule-appointments")}?list=past">Clear all</a>')
        self.assertEqual([option['value'] for option in response.context['filter_menu']['sections'][0]['options']],
                         ['booked', 'completed', 'cancelled', 'no_show'])
        self.assertEqual(self.listed(list='past', status='completed')[1], [past_theirs.pk])
        self.assertEqual(self.listed(list='past')[1], [past_mine.pk, past_theirs.pk])
        # Today only, when an appointment later today is still today.
        today = self.appointment(starts_at=timezone.now() + timedelta(minutes=1))
        if timezone.localdate(today.starts_at) == timezone.localdate():
            self.assertEqual(self.listed(when='today')[1], [today.pk])
        self.assertContains(self.client.get(self.url('schedule-appointments'), {'type': 'ad_hoc'}),
                            'No upcoming appointments match these filters.')
        self.assertContains(self.client.get(self.url('schedule-appointments'), {'list': 'past', 'type': 'ad_hoc'}),
                            'No past appointments match these filters.')

    def test_a_practice_administrator_edits_and_cancels_any_booking_and_opens_details(self):
        mine = self.at(timedelta(days=1))
        theirs = self.at(timedelta(days=2), clinician=self.colleague)
        cancelled = self.at(timedelta(days=3), status='cancelled')
        under_way = self.at(-timedelta(minutes=5), duration_minutes=30)
        past = self.at(-timedelta(days=1))
        self.login(self.admin)
        html = self.client.get(self.url('schedule-appointments')).content.decode()
        for appointment in (mine, theirs, under_way):
            self.assert_actions(html, appointment, ['Edit', 'Cancel', 'Details'])
        self.assert_actions(html, cancelled, ['Details'])
        # Practice administrators don't open records or join consultations.
        self.assertNotIn(self.url('patient-detail', self.patient.pk), html)
        self.assertIn(f'<span class="staff-schedule-table__patient">{self.patient}</span>', html)
        self.assertNotIn('Open meeting room', html)
        # Without scripts the buttons open the same dialogs on the appointment's page.
        self.assertIn(f'href="{self.url("appointment-detail", mine.pk)}?update=time" data-modal-open="edit-appointment-{mine.pk}"', html)
        self.assertIn(f'href="{self.url("appointment-detail", mine.pk)}?update=status" data-modal-open="cancel-appointment-{mine.pk}"', html)
        # The dialogs name the patient and come back to the list.
        start = html.index(f'<dialog class="app-dialog" id="edit-appointment-{mine.pk}"')
        dialog = html[start:html.index('</dialog>', start)]
        self.assertIn(f'<p class="app-dialog__sub">{self.patient} · ', dialog)
        self.assertIn(f'<input type="hidden" name="next" value="{self.url("schedule-appointments")}">', dialog)
        self.assertIn('data-booking-form', dialog)
        html = self.client.get(self.url('schedule-appointments'), {'list': 'past'}).content.decode()
        self.assert_actions(html, past, ['Details'])
        self.assertNotIn('app-dialog" id="edit-appointment-', html)

    def test_a_super_admin_edits_any_booking_and_cancels_or_joins_only_their_own(self):
        others = self.at(timedelta(days=1), clinician=self.colleague)
        self.login(self.super_admin)
        html = self.client.get(self.url('schedule-appointments')).content.decode()
        self.assert_actions(html, others, ['Edit', 'Details'])
        self.assertIn(self.url('patient-detail', self.patient.pk), html)
        # A Super Admin who is also a doctor, with their own booking.
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(role='super_admin')
        own = self.at(timedelta(days=2))
        self.login(self.doctor)
        response = self.client.get(self.url('schedule-appointments'))
        html = response.content.decode()
        self.assertEqual([section['label'] for section in response.context['filter_menu']['sections']],
                         ['Status', 'Date', 'Clinician', 'Appointment type'])
        self.assert_actions(html, own, ['Open meeting room', 'Edit', 'Cancel', 'Details'])
        self.assert_actions(html, others, ['Edit', 'Details'])
        # Greyed out until the room opens, with when it opens behind the (i).
        self.assertIn(f'role="link" data-appointment-video-join aria-disabled="true" aria-describedby="video-join-{own.pk}"><svg', html)
        self.assertIn(f'id="video-join-{own.pk}" role="tooltip">', html)
        self.assertNotIn(f'video-join-{others.pk}', html)

    @override_settings(DEBUG=True, VIDEO_ENABLED=True)
    def test_a_doctor_sees_only_their_own_and_cancels_only_before_it_starts(self):
        own = self.at(timedelta(days=1))
        under_way = self.at(-timedelta(minutes=5), duration_minutes=30)
        cancelled = self.at(timedelta(days=2), status='cancelled')
        others = self.at(timedelta(days=1, hours=1), clinician=self.colleague)
        self.login(self.doctor)
        response, rows = self.listed()
        self.assertEqual(rows, [under_way.pk, own.pk, cancelled.pk])
        # Their own only, as on the Calendar, so no Clinician filter, and naming someone else's shows nothing more.
        self.assertEqual([section['label'] for section in response.context['filter_menu']['sections']],
                         ['Status', 'Date', 'Appointment type'])
        self.assertEqual(self.listed(clinician=self.colleague.pk)[1], [under_way.pk, own.pk, cancelled.pk])
        self.assertNotIn(others.pk, self.listed(list='past')[1])
        html = response.content.decode()
        self.assert_actions(html, own, ['Open meeting room', 'Edit', 'Cancel', 'Details'])
        # Once it has started: into the room, which is open now, and Edit, but no Cancel.
        self.assert_actions(html, under_way, ['Open meeting room', 'Edit', 'Details'])
        self.assertIn(f'<a class="console-button console-button--primary patient-row-action" role="link" data-appointment-video-join '
                      f'href="/video/appointments/{under_way.pk}/" aria-describedby="video-join-{under_way.pk}"><svg', self.row(html, under_way))
        self.assert_actions(html, cancelled, ['Details'])
        # Their patients' records are a click away.
        self.assertIn(f'<a class="staff-schedule-table__patient" href="{self.url("patient-detail", self.patient.pk)}">', html)

    def test_only_staff_of_the_practice_see_its_appointments(self):
        mine = self.at(timedelta(days=1))
        elsewhere = Appointment.objects.create(company=self.beta, patient=self.beta_patient, clinician=self.doctor,
                                               starts_at=timezone.now() + timedelta(days=1), duration_minutes=30)
        inactive = Patient.objects.create(company=self.company, first_name='Former', last_name='Patient', is_active=False)
        gone = self.at(timedelta(days=2), patient=inactive)
        url = self.url('schedule-appointments')
        # Signed out: to sign in. A patient: refused.
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        self.login(self.patient_user)
        self.assertEqual(self.client.get(url).status_code, 403)
        # Staff see their practice's, for active patients only.
        for actor in (self.admin, self.super_admin, self.doctor):
            self.login(actor)
            rows = self.listed()[1]
            self.assertIn(mine.pk, rows)
            self.assertNotIn(elsewhere.pk, rows)
            self.assertNotIn(gone.pk, rows)
        self.login(self.doctor, company=self.beta)
        self.assertEqual(self.listed()[1], [elsewhere.pk])

    def test_the_record_tab_is_the_same_list_without_a_patient_column(self):
        booked = self.at(timedelta(days=1))
        past = self.at(-timedelta(days=1), status='completed')
        someone_else = self.at(timedelta(days=1), patient=self.other_patient)
        record = self.url('patient-detail', self.patient.pk)
        self.login(self.super_admin)
        response, rows = self.listed(record, tab='appointments')
        self.assertEqual(rows, [booked.pk])
        self.assertNotIn(someone_else.pk, rows)
        self.assertNotContains(response, '<th scope="col">Patient</th>')
        self.assertContains(response, '<h2 id="staff-appointments-heading">Appointments</h2>')
        self.assert_actions(response.content.decode(), booked, ['Edit', 'Details'])
        # The record's tab stays in every link and form: Upcoming and Past, Filters and pages.
        self.assertContains(response, f'<a href="{record}?tab=appointments&amp;list=past">Past</a>')
        self.assertContains(response, '<input type="hidden" name="tab" value="appointments">')
        self.assertEqual(response.context['pagination_query'], 'tab=appointments')
        # Book an appointment, after Filters, for whoever changes the record.
        html = response.content.decode()
        button = f'href="{self.url("appointment-book")}?patient={self.patient.pk}" data-modal-open="book-appointment" aria-haspopup="dialog">Book an appointment</a>'
        self.assertLess(html.index('data-filter-menu'), html.index(button))
        self.assertContains(response, 'css/staff_appointments.css')
        # The dialogs don't repeat the patient's name, and come back to the record's tab.
        start = html.index(f'<dialog class="app-dialog" id="edit-appointment-{booked.pk}"')
        dialog = html[start:html.index('</dialog>', start)]
        self.assertNotIn(f'{self.patient} · ', dialog)
        self.assertIn(f'<input type="hidden" name="next" value="{record}?tab=appointments">', dialog)
        response, rows = self.listed(record, tab='appointments', list='past')
        self.assertEqual(rows, [past.pk])
        # Practice administrators still don't open records; a doctor in the Clinician role works from the Overview.
        self.login(self.admin)
        self.assertRedirects(self.client.get(record, {'tab': 'appointments'}), reverse('portal:desktop-dashboard'),
                             fetch_redirect_response=False)
        self.login(self.doctor)
        self.assertRedirects(self.client.get(record, {'tab': 'appointments'}), f'{record}?tab=overview', fetch_redirect_response=False)

    def test_edit_and_cancel_come_back_to_the_list_they_were_made_from(self):
        moving = self.at(timedelta(days=2))
        cancelling = self.at(timedelta(days=3))
        list_url = f"{self.url('schedule-appointments')}?{urlencode({'type': 'initial', 'clinician': self.doctor.pk})}"
        Appointment.objects.filter(pk__in=[moving.pk, cancelling.pk]).update(appointment_type='initial')
        self.login(self.admin)
        page = self.client.get(list_url)
        rows = {row.pk: row for row in page.context['appointments']}
        new_start = moving.starts_at + timedelta(days=1, hours=2)
        local = timezone.localtime(new_start)
        moved = self.client.post(self.url('appointment-change-time', moving.pk), {
            'workflow_context': rows[moving.pk].time_context, 'next': list_url,
            'date': local.date().isoformat(), 'time': f'{local:%H:%M}'})
        self.assertRedirects(moved, list_url, fetch_redirect_response=False,
                             msg_prefix=str(moved.context and moved.context.get('time_form') and moved.context['time_form'].errors))
        moving.refresh_from_db()
        self.assertEqual(moving.starts_at, new_start)
        cancelled = self.client.post(self.url('appointment-detail', cancelling.pk), {
            'workflow_context': rows[cancelling.pk].cancel_context, 'next': list_url, 'status': 'cancelled',
            'reason': 'Clinic closed. Please book another time.'})
        self.assertRedirects(cancelled, list_url, fetch_redirect_response=False)
        cancelling.refresh_from_db()
        self.assertEqual(cancelling.status, 'cancelled')

    def test_a_change_from_a_record_comes_back_to_that_record_only(self):
        first = self.at(timedelta(days=2))
        second = self.at(timedelta(days=3))
        third = self.at(timedelta(days=4))
        record = f"{self.url('patient-detail', self.patient.pk)}?tab=appointments&list=past"
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(role='super_admin')
        self.login(self.doctor)
        page = self.client.get(self.url('patient-detail', self.patient.pk), {'tab': 'appointments'})
        rows = {row.pk: row for row in page.context['appointments']}
        cancelled = self.client.post(self.url('appointment-detail', first.pk), {
            'workflow_context': rows[first.pk].cancel_context, 'next': record, 'status': 'cancelled', 'reason': 'Unwell.'})
        self.assertRedirects(cancelled, record, fetch_redirect_response=False)
        local = timezone.localtime(second.starts_at + timedelta(hours=1))
        moved = self.client.post(self.url('appointment-change-time', second.pk), {
            'workflow_context': rows[second.pk].time_context, 'next': record,
            'date': local.date().isoformat(), 'time': f'{local:%H:%M}'})
        self.assertRedirects(moved, record, fetch_redirect_response=False)
        # Another patient's record, or another site, isn't somewhere to come back to: the appointment's page instead.
        for hours, target in enumerate((f"{self.url('patient-detail', self.other_patient.pk)}?tab=appointments",
                                        f"https://example.com{self.url('patient-detail', self.patient.pk)}?tab=appointments",
                                        f"//example.com{self.url('schedule-appointments')}"), start=1):
            page = self.client.get(self.url('patient-detail', self.patient.pk), {'tab': 'appointments'})
            row = next(item for item in page.context['appointments'] if item.pk == third.pk)
            when = timezone.localtime(third.starts_at + timedelta(hours=hours))
            response = self.client.post(self.url('appointment-change-time', third.pk), {
                'workflow_context': row.time_context, 'next': target, 'date': when.date().isoformat(), 'time': f'{when:%H:%M}'})
            self.assertRedirects(response, self.url('appointment-detail', third.pk), fetch_redirect_response=False, msg_prefix=target)

    def test_pages_keep_the_tab_and_filters(self):
        for day in range(1, 23):
            self.at(timedelta(days=day), appointment_type='follow_up')
        self.login(self.admin)
        response, rows = self.listed(type='follow_up')
        self.assertEqual(len(rows), 20)
        self.assertContains(response, '<nav class="patient-pagination" aria-label="Appointment pages">')
        self.assertContains(response, 'href="?type=follow_up&amp;page=2"')
        self.assertEqual(len(self.listed(type='follow_up', page=2)[1]), 2)

    def test_the_booked_clinician_completes_or_marks_a_no_show_on_past_only(self):
        """Complete and No-show (James, 20 Nov 2026) follow recording attendance on the appointment's page and in
        care.appointment_lifecycle: only the booked clinician, only once it has ended, only while still booked; No-show
        not once the consultation note is signed. So never on Upcoming, which keeps an appointment until it ends."""
        from care.clinical import save_consultation

        under_way = self.at(-timedelta(minutes=10), duration_minutes=30)
        ended = self.at(-timedelta(minutes=45), duration_minutes=30)
        signed = self.at(-timedelta(days=1))
        save_consultation(company=self.company, patient=self.patient, actor=self.doctor, summary='Signed.',
                          occurred_at=signed.starts_at, appointment=signed, sign=True)
        completed = self.at(-timedelta(days=2), status='completed')
        cancelled = self.at(-timedelta(days=3), status='cancelled')
        theirs = self.at(-timedelta(days=1, hours=2), clinician=self.colleague)
        self.login(self.doctor)
        self.assert_actions(self.client.get(self.url('schedule-appointments')).content.decode(), under_way,
                            ['Open meeting room', 'Edit', 'Details'])
        past_url = f"{self.url('schedule-appointments')}?list=past"
        response = self.client.get(past_url)
        html = response.content.decode()
        self.assert_actions(html, ended, ['Complete', 'No-show', 'Details'])
        self.assert_actions(html, signed, ['Complete', 'Details'])
        self.assert_actions(html, completed, ['Details'])
        self.assert_actions(html, cancelled, ['Details'])
        # Icon buttons, each named for a screen reader and in a tooltip; without scripts, the appointment's own dialog.
        row = self.row(html, ended)
        self.assertIn(f'<a class="console-button console-button--secondary patient-row-action staff-row-icon" '
                      f'href="{self.url("appointment-detail", ended.pk)}?update=status" data-modal-open="complete-appointment-{ended.pk}" '
                      f'aria-haspopup="dialog" data-tooltip="Complete"><svg', row)
        self.assertIn(f'<span class="sr-only">Complete the appointment with {self.patient}</span></a>', row)
        self.assertIn(f'data-modal-open="no-show-appointment-{ended.pk}" aria-haspopup="dialog" data-tooltip="No-show"><svg', row)
        self.assertIn(f'<span class="sr-only">No-show for the appointment with {self.patient}</span></a>', row)
        # Each is confirmed in a dialog, as on the appointment's page, posting there and back to this list.
        start = html.index(f'<dialog class="app-dialog" id="no-show-appointment-{ended.pk}"')
        dialog = html[start:html.index('</dialog>', start)]
        self.assertIn('>Mark as no-show</h2>', dialog)
        self.assertIn(f'<p class="app-dialog__sub">{self.patient} · ', dialog)
        self.assertIn(f'action="{self.url("appointment-detail", ended.pk)}"', dialog)
        self.assertIn(f'<input type="hidden" name="next" value="{past_url}">', dialog)
        self.assertIn('<input type="hidden" name="status" value="no_show">', dialog)
        self.assertIn('type="submit">Mark as no-show</button>', dialog)
        self.assertIn('>Complete appointment</h2>', html)
        # Nobody else marks them: a practice administrator, a Super Admin who isn't a clinician, or another doctor.
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            html = self.client.get(past_url).content.decode()
            for appointment in (ended, signed, theirs):
                self.assert_actions(html, appointment, ['Details'])
        self.login(self.colleague)
        html = self.client.get(past_url).content.decode()
        self.assert_actions(html, theirs, ['Complete', 'No-show', 'Details'])
        self.assertNotIn(f'appointment-{ended.pk}"', html)
        # A Super Admin who is also a doctor marks their own, on Calendar and on the patient's record.
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(role='super_admin')
        self.login(self.doctor)
        html = self.client.get(past_url).content.decode()
        self.assert_actions(html, ended, ['Complete', 'No-show', 'Details'])
        self.assert_actions(html, theirs, ['Details'])
        record = self.client.get(self.url('patient-detail', self.patient.pk), {'tab': 'appointments', 'list': 'past'})
        self.assert_actions(record.content.decode(), ended, ['Complete', 'No-show', 'Details'])

    def test_complete_and_no_show_come_back_to_the_list_they_were_made_from(self):
        attended = self.at(-timedelta(hours=2), duration_minutes=30, appointment_type='follow_up')
        missed = self.at(-timedelta(hours=3), duration_minutes=30, appointment_type='follow_up')
        list_url = f"{self.url('schedule-appointments')}?{urlencode({'list': 'past', 'type': 'follow_up'})}"
        self.login(self.doctor)
        rows = {row.pk: row for row in self.client.get(list_url).context['appointments']}
        for appointment, status, message in ((attended, 'completed', 'marked as completed'), (missed, 'no_show', 'marked as a no-show')):
            response = self.client.post(self.url('appointment-detail', appointment.pk), {
                'workflow_context': rows[appointment.pk].attendance_context, 'next': list_url, 'status': status})
            self.assertRedirects(response, list_url, fetch_redirect_response=False)
            appointment.refresh_from_db()
            self.assertEqual(appointment.status, status)
            self.assertContains(self.client.get(list_url), f'Appointment with {self.patient} {message}.')
        # Without a list to go back to, as from the appointment's own page, it stays there.
        own_page = self.at(-timedelta(hours=4), duration_minutes=30)
        token = self.client.get(self.url('appointment-detail', own_page.pk)).context['workflow_context']
        response = self.client.post(self.url('appointment-detail', own_page.pk), {'workflow_context': token, 'status': 'completed'})
        self.assertRedirects(response, self.url('appointment-detail', own_page.pk), fetch_redirect_response=False)
        # Before it has ended, or for someone else's, the rules on the appointment's page still refuse it.
        upcoming = self.at(-timedelta(minutes=5), duration_minutes=30)
        token = self.client.get(self.url('appointment-detail', upcoming.pk)).context['workflow_context']
        response = self.client.post(self.url('appointment-detail', upcoming.pk), {'workflow_context': token, 'next': list_url, 'status': 'completed'})
        self.assertEqual(response.status_code, 400)
        upcoming.refresh_from_db()
        self.assertEqual(upcoming.status, 'booked')
        ended = self.at(-timedelta(hours=5), duration_minutes=30)
        self.login(self.admin)
        token = self.client.get(self.url('appointment-detail', ended.pk)).context['workflow_context']
        response = self.client.post(self.url('appointment-detail', ended.pk), {'workflow_context': token, 'next': list_url, 'status': 'no_show'})
        self.assertEqual(response.status_code, 400)
        ended.refresh_from_db()
        self.assertEqual(ended.status, 'booked')

    @override_settings(DEBUG=True, VIDEO_ENABLED=True)
    def test_open_meeting_room_switches_on_by_itself_as_for_patients(self):
        later = self.at(timedelta(days=1))
        under_way = self.at(-timedelta(minutes=5), duration_minutes=30)
        theirs = self.at(timedelta(days=1, hours=2), clinician=self.colleague)
        self.login(self.doctor)
        response = self.client.get(self.url('schedule-appointments'))
        status_url = self.url('staff-appointment-video-status')
        # The patient page's script, asking a staff address, for the rows with a meeting room.
        self.assertContains(response, 'js/appointment_video.js')
        self.assertContains(response, f'data-appointment-video data-status-url="{status_url}" data-video-scope="staff:{self.company.pk}:{self.doctor.pk}"')
        self.assertContains(response, f'<tr data-video-appointment-id="{later.pk}" data-video-opens-at=')
        self.assertContains(response, 'data-appointment-video-announcement role="status" aria-live="polite"')
        # Closed: its note says when it opens; open: hidden.
        html = response.content.decode()
        self.assertIn('<span data-appointment-video-note><span class="info-tip"', self.row(html, later))
        self.assertIn('<span data-appointment-video-note hidden>', self.row(html, under_way))
        scope = {'HTTP_X_MERIDIAN_SCOPE': f'staff:{self.company.pk}:{self.doctor.pk}'}
        data = self.client.get(status_url, {'id': [later.pk, under_way.pk, theirs.pk]}, **scope).json()
        self.assertEqual(data['scope'], f'staff:{self.company.pk}:{self.doctor.pk}')
        rows = {item['id']: item for item in data['appointments']}
        self.assertEqual(set(rows), {later.pk, under_way.pk})
        self.assertIsNone(rows[later.pk]['joinUrl'])
        self.assertIn('opens 15 minutes before', rows[later.pk]['message'])
        self.assertEqual(rows[under_way.pk]['joinUrl'], f'/video/appointments/{under_way.pk}/')
        self.assertEqual(self.client.get(status_url, {'id': 'x'}).status_code, 400)
        self.assertEqual(self.client.get(status_url, {'id': later.pk}, HTTP_X_MERIDIAN_SCOPE='staff:0:0').status_code, 409)
        # Nobody else has a room in them; a practice administrator's list has no meeting rooms to watch.
        self.login(self.colleague)
        self.assertEqual(self.client.get(status_url, {'id': [later.pk, under_way.pk]}).json()['appointments'], [])
        self.login(self.admin)
        self.assertNotContains(self.client.get(self.url('schedule-appointments')), 'data-appointment-video ')
        self.login(self.patient_user)
        self.assertEqual(self.client.get(status_url, {'id': later.pk}).status_code, 403)
        # Past has no meeting rooms.
        self.login(self.doctor)
        self.assertNotContains(self.client.get(self.url('schedule-appointments'), {'list': 'past'}), 'data-appointment-video ')

    def test_cancel_is_a_plain_button_on_the_day_view_and_an_appointments_page_too(self):
        booked = self.at(timedelta(days=1))
        own = self.at(timedelta(days=1, hours=1))
        self.login(self.admin)
        day = self.client.get(self.url('staff-schedule'), {'date': timezone.localdate(booked.starts_at).isoformat()})
        self.assertContains(day, f'data-modal-open="cancel-appointment-{booked.pk}"')
        self.assertNotContains(day, 'console-button--cancel')
        page = self.client.get(self.url('appointment-detail', booked.pk))
        self.assertContains(page, '<a class="console-button console-button--secondary" href="?update=status" data-modal-open="appointment-status" '
                                  'aria-haspopup="dialog">Cancel appointment</a>')
        self.login(self.doctor)
        day = self.client.get(self.url('staff-schedule'), {'date': timezone.localdate(own.starts_at).isoformat()})
        self.assertContains(day, f'data-modal-open="cancel-appointment-{own.pk}"')
        self.assertNotContains(day, 'console-button--cancel')

    def test_links_to_the_section_open_appointments_and_links_to_a_day_keep_the_calendar(self):
        appointment = self.at(timedelta(days=1))
        self.login(self.admin)
        # The phone overview's All, the booking page, and an appointment's breadcrumb go to Calendar's first tab.
        self.assertContains(self.client.get(reverse('portal:mobile-dashboard')), f'href="{self.url("schedule-appointments")}">All</a>')
        booking = self.client.get(self.url('appointment-book'))
        self.assertContains(booking, f'href="{self.url("schedule-appointments")}">View schedule</a>')
        self.assertContains(booking, 'type="submit">Confirm appointment</button>')
        detail = self.client.get(self.url('appointment-detail', appointment.pk))
        self.assertContains(detail, f'href="{self.url("schedule-appointments")}"')
        # Back to the day, and the desktop overview's View calendar, are the Calendar tab on purpose.
        day = timezone.localtime(appointment.starts_at).date().isoformat()
        self.assertContains(detail, f'href="{self.url("staff-schedule")}?view=table&amp;date={day}">Back to the day</a>')
        self.assertContains(self.client.get(reverse('portal:desktop-dashboard')), f'href="{self.url("staff-schedule")}">View calendar</a>')

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_switching_practice_keeps_the_calendar_tab_you_were_on(self):
        self.login(self.doctor)
        for came_from, lands in ((f"{self.url('schedule-appointments')}?list=past", self.url('schedule-appointments')),
                                 (f"{self.url('staff-schedule')}?view=week&date=2026-11-20", self.url('staff-schedule'))):
            response = self.client.post(reverse('portal:activate-company', args=[self.beta.slug]), {'next': came_from})
            self.assertRedirects(response, lands, fetch_redirect_response=False)
