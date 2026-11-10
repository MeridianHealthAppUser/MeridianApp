"""Practice administrator menus, bookings, shipping, inventory, history and follow-ups."""

import re
from datetime import datetime, time, timedelta
from io import BytesIO
from unittest.mock import patch

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.followups import close_callbacks, request_callback
from care.messaging import add_participant
from care.models import (
    AuditEvent, CallbackRequest, ClinicalTask, DoctorTimeOff, DoctorWorkingPattern, HelpRequest, Lead, MessageThread,
    MessageThreadParticipant, PatientMessage, PatientSubscription, Shipment, TeamThread, TeamThreadMember,
)
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from care.test_operations import OperationsFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class SessionMixin:
    def login(self, actor, company=None):
        self.client.force_login(actor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def sidebar(self, response):
        html = response.content.decode()
        sidebar = html[html.index('class="console-nav'):]
        return sidebar[:sidebar.index('</nav>')]


class PracticeAdminMenuTests(SessionMixin, OperationsFixture):
    def test_menu_is_messages_without_all_patients_or_access_history(self):
        self.login(self.admin)
        page = self.client.get(reverse('portal:staff-inbox'))
        labels = re.findall(r'<span class="console-nav__text">([^<]+)</span>', self.sidebar(page))
        self.assertEqual(labels, ['Overview', 'My tasks', 'Messages', 'Calendar &amp; Bookings',
                                  'Purchasing', 'Inventory', 'Shipping', 'Leads and dropouts'])
        self.assertContains(page, '<h1>Messages</h1>', html=True)
        self.assertNotContains(page, '>All patients<')
        self.assertNotContains(page, 'My access history')
        self.assertIn(reverse('portal:staff-inbox'), self.sidebar(page))
        # Their Messages opens on Patient messages; access history isn't theirs.
        self.assertRedirects(self.client.get(reverse('portal:patient-list')), reverse('portal:staff-inbox'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('portal:account-access-history')).status_code, 403)
        # Super Admins keep all three.
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:staff-inbox'))
        self.assertContains(page, '<h1>Patient Records &amp; Messages</h1>', html=True)
        self.assertContains(page, '>All patients<')
        self.assertContains(page, 'My access history')
        self.assertEqual(self.client.get(reverse('portal:patient-list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('portal:account-access-history')).status_code, 200)

    def test_leads_is_the_please_call_list_and_lead_history_has_every_enquiry(self):
        waiting = Lead.objects.create(company=self.company, first_name='Tumi', last_name='Dlamini', email='tumi@example.test',
                                      stage=Lead.Stage.BOOKING)
        converted = Lead.objects.create(company=self.company, first_name='Jimmy', last_name='Patient', email='jimmy@example.test',
                                        stage=Lead.Stage.CONVERTED, converted_patient=self.patient)
        self.login(self.admin)
        # The enquiries table is only in Full Lead History now (Dr Czech, 4 Nov 2026).
        leads = self.client.get(reverse('portal:staff-leads'))
        self.assertContains(leads, '<h2 id="please-call-heading">Please call</h2>', html=True)
        self.assertNotContains(leads, 'Enquiries')
        self.assertNotContains(leads, 'tumi@example.test')
        self.assertNotIn('leads', leads.context)
        self.assertContains(leads, f'href="{reverse("portal:staff-lead-history")}"><svg')
        self.assertContains(leads, '</svg>Full Lead History</a>')
        history = self.client.get(reverse('portal:staff-lead-history'))
        self.assertEqual({lead.pk for lead in history.context['leads']}, {waiting.pk, converted.pk})
        self.assertEqual([lead.pk for lead in self.client.get(reverse('portal:staff-lead-history'), {'stage': 'converted'}).context['leads']],
                         [converted.pk])
        self.assertContains(history, 'Back to leads')
        self.assertEqual(history.context['search_action'], reverse('portal:staff-lead-history'))

    def test_practice_admins_never_open_a_patient_record(self):
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, opened_by=self.patient_user, subject='Parcel')
        add_participant(thread, self.admin)
        TeamThreadMember.objects.create(company=self.company, user=self.admin, thread=TeamThread.objects.create(
            company=self.company, subject='About Alpha', patient=self.patient, opened_by=self.admin))
        ClinicalTask.objects.create(company=self.company, patient=self.patient, title='Call about the parcel')
        self.login(self.admin)
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        for url in (record, f'{record}?tab=appointments', f'{record}?tab=payments'):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('portal:desktop-dashboard')), 'Patient records aren’t available to practice administrators.')
        # Nothing they see links to one.
        for url in (reverse('portal:staff-inbox') + f'?thread={thread.pk}', reverse('portal:team-inbox'), reverse('portal:staff-tasks')):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), f'href="{record}')
        # Nor do actions on a record work.
        response = self.client.post(reverse('portal:patient-details-edit', args=[self.patient.pk]), {'assigned_doctor': ''})
        self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)


class PracticeAdminCalendarTests(SessionMixin, AppointmentLifecycleFixture):
    def day_url(self):
        return f"{reverse('portal:staff-schedule')}?view=table&date={timezone.localdate(self.starts_at).isoformat()}"

    def test_working_hours_leave_and_special_days_are_read_only(self):
        self.login(self.admin)
        url = f"{reverse('portal:staff-schedule')}?section=availability&clinician={self.doctor.pk}"
        page = self.client.get(url)
        self.assertFalse(page.context['can_manage_days'])
        self.assertNotContains(page, 'Add special day')
        self.assertNotContains(page, 'Add leave')
        day = timezone.localdate() + timedelta(days=5)
        response = self.client.post(f"{reverse('portal:availability-time-off-create')}?clinician={self.doctor.pk}",
                                    {'timeoff-starts_on': day, 'timeoff-ends_on': day})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DoctorTimeOff.objects.exists())
        # A Super Admin still adds them.
        self.login(self.super_admin)
        page = self.client.get(url)
        self.assertContains(page, 'Add leave')

    def test_practice_admins_book_only_initial_consultations_and_follow_ups(self):
        # Six-month reviews and ad-hoc appointments are the clinician's to book (Dr Czech, 4 Nov 2026).
        self.login(self.admin)
        page = self.client.get(self.day_url())
        self.assertEqual([value for value, _ in page.context['booking_form'].fields['appointment_type'].choices if value],
                         ['initial', 'follow_up'])
        self.assertNotContains(page, 'Six-month review</option>')
        response = self.client.post(reverse('portal:appointment-book'), {'appointment_type': 'review'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('appointment_type', response.context['form'].errors)
        self.login(self.super_admin)
        page = self.client.get(self.day_url())
        self.assertEqual([value for value, _ in page.context['booking_form'].fields['appointment_type'].choices if value],
                         ['initial', 'follow_up', 'review', 'ad_hoc'])

    def test_day_view_moves_and_cancels_a_booking_in_dialogs_and_comes_back(self):
        appointment = self.appointment(appointment_type='follow_up')
        self.login(self.admin)
        page = self.client.get(self.day_url())
        self.assertContains(page, f'data-modal-open="edit-appointment-{appointment.pk}"')
        self.assertContains(page, f'data-modal-open="cancel-appointment-{appointment.pk}"')
        # Details | Edit | Cancel, each naming the patient for screen readers.
        for label in ('Details', 'Edit', 'Cancel'):
            self.assertContains(page, f'>{label}<span class="sr-only">')
        self.assertNotContains(page, 'Open record<span')
        self.assertNotContains(page, reverse('portal:patient-detail', args=[self.patient.pk]))
        # Its page stands alone, under Calendar & Bookings, not inside the patient's record.
        details = self.client.get(reverse('portal:appointment-detail', args=[appointment.pk]))
        self.assertTemplateUsed(details, 'portal/staff_appointment_detail.html')
        self.assertNotContains(details, reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(details, 'data-modal-open="appointment-time"')
        row = next(item for item in page.context['appointments'] if item.pk == appointment.pk)
        new_start = self.starts_at + timedelta(days=1)
        local = timezone.localtime(new_start)
        moved = self.client.post(reverse('portal:appointment-change-time', args=[appointment.pk]), {
            'workflow_context': row.time_context, 'next': self.day_url(), 'date': local.date().isoformat(), 'time': f'{local:%H:%M}'})
        self.assertRedirects(moved, self.day_url(), fetch_redirect_response=False)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, new_start)
        # Cancel, with the reason the patient sees; back to the same day.
        page = self.client.get(self.day_url())
        self.assertNotContains(page, f'cancel-appointment-{appointment.pk}')  # It's on the next day now.
        page = self.client.get(f"{reverse('portal:staff-schedule')}?view=table&date={local.date().isoformat()}")
        row = next(item for item in page.context['appointments'] if item.pk == appointment.pk)
        back = f"{reverse('portal:staff-schedule')}?view=table&date={local.date().isoformat()}"
        cancelled = self.client.post(reverse('portal:appointment-detail', args=[appointment.pk]), {
            'workflow_context': row.cancel_context, 'next': back, 'status': 'cancelled', 'confirm': 'on',
            'reason': 'Dr Doctor is unwell. Please book another time.'})
        self.assertRedirects(cancelled, back, fetch_redirect_response=False)
        appointment.refresh_from_db()
        self.assertEqual((appointment.status, appointment.cancellation_reason),
                         ('cancelled', 'Dr Doctor is unwell. Please book another time.'))
        # Only to Calendar & Bookings: anywhere else goes to the appointment's page.
        other = self.appointment(starts_at=self.starts_at + timedelta(days=4))
        page = self.client.get(reverse('portal:appointment-detail', args=[other.pk]))
        response = self.client.post(reverse('portal:appointment-detail', args=[other.pk]), {
            'workflow_context': page.context['workflow_context'], 'next': 'https://example.com/', 'status': 'cancelled',
            'confirm': 'on', 'reason': 'Clinic closed.'})
        self.assertRedirects(response, reverse('portal:appointment-detail', args=[other.pk]), fetch_redirect_response=False)

    def test_doctors_download_and_cancel_their_own_bookings_from_day_view(self):
        appointment = self.appointment(appointment_type='follow_up')
        cancelled = self.appointment(starts_at=self.starts_at + timedelta(hours=1), status='cancelled')
        self.login(self.doctor)
        page = self.client.get(self.day_url())
        calendar = reverse('portal:appointment-calendar', args=[appointment.pk])
        # Download | Details | Cancel | Open record; nothing to download or cancel once it's cancelled.
        self.assertContains(page, f'data-no-loader href="{calendar}">')
        self.assertNotContains(page, reverse('portal:appointment-calendar', args=[cancelled.pk]))
        self.assertContains(page, f'data-modal-open="cancel-appointment-{appointment.pk}"')
        self.assertNotContains(page, f'data-modal-open="cancel-appointment-{cancelled.pk}"')
        self.assertContains(page, f'id="cancel-appointment-{appointment.pk}"')
        html = page.content.decode()
        row = html[html.index(f'href="{calendar}"'):]
        self.assertLess(row.index('Details<span'), row.index(f'data-modal-open="cancel-appointment-{appointment.pk}"'))
        self.assertLess(row.index(f'data-modal-open="cancel-appointment-{appointment.pk}"'), row.index('Open record<span'))
        # The calendar file names no patient: calendars are often synced elsewhere.
        ics = self.client.get(calendar)
        self.assertEqual(ics['Content-Type'], 'text/calendar; charset=utf-8')
        body = ics.content.decode()
        self.assertIn('SUMMARY:Meridian: Follow-up', body)
        self.assertNotIn(self.patient.last_name, body)
        # Cancel in the dialog, with the reason the patient sees; back to the same day.
        row = next(item for item in page.context['appointments'] if item.pk == appointment.pk)
        response = self.client.post(reverse('portal:appointment-detail', args=[appointment.pk]), {
            'workflow_context': row.cancel_context, 'next': self.day_url(), 'status': 'cancelled', 'confirm': 'on',
            'reason': 'Called away. Please book another time.'})
        self.assertRedirects(response, self.day_url(), fetch_redirect_response=False)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'cancelled')
        # Another doctor can't download a booking for a patient whose record they can't open.
        self.login(self.colleague)
        self.assertNotEqual(self.client.get(calendar).get('Content-Type'), 'text/calendar; charset=utf-8')

    def test_available_time_is_each_doctors_working_day_in_15_minute_slots(self):
        day = timezone.localdate(self.starts_at)
        for clinician, periods in ((self.doctor, ((time(9), time(10)), (time(13), time(13, 30)))), (self.colleague, ((time(9), time(10)),))):
            for opens, closes in periods:
                DoctorWorkingPattern.objects.create(company=self.company, clinician=clinician, weekday=day.weekday(),
                                                    is_working=True, starts_at=opens, ends_at=closes)
        at = lambda hour, minute=0: timezone.make_aware(datetime.combine(day, time(hour, minute)))
        self.appointment(starts_at=at(9, 15), duration_minutes=30)
        DoctorTimeOff.objects.create(company=self.company, clinician=self.colleague,
                                     starts_at=timezone.make_aware(datetime.combine(day, time.min)),
                                     ends_at=timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min)))
        self.login(self.admin)
        page = self.client.get(self.day_url())
        diaries = {diary['clinician'].pk: diary for diary in page.context['day_diaries']}
        mine = diaries[self.doctor.pk]
        self.assertEqual(mine['kind'], 'work')
        self.assertEqual([[(timezone.localtime(slot['starts_at']).strftime('%H:%M'), slot['state']) for slot in period['slots']]
                          for period in mine['periods']],
                         [[('09:00', 'free'), ('09:15', 'booked'), ('09:30', 'booked'), ('09:45', 'free')],
                          [('13:00', 'free'), ('13:15', 'free')]])
        self.assertEqual(mine['free'], 4)
        self.assertEqual((diaries[self.colleague.pk]['kind'], diaries[self.colleague.pk]['periods']), ('off', []))
        # Each doctor is a section; a free slot books it, a booked one can't be chosen.
        self.assertContains(page, f'id="diary-{self.doctor.pk}-heading"')
        self.assertContains(page, f'data-book-slot data-clinician="{self.doctor.pk}" data-date="{day.isoformat()}" data-time="09:45"')
        self.assertNotContains(page, 'data-time="09:15"')
        self.assertContains(page, 'On leave')
        # Without scripts, the slot's link opens the booking page with it chosen.
        booking = self.client.get(reverse('portal:appointment-book'), {'clinician': self.doctor.pk, 'date': day.isoformat(), 'time': '09:45'})
        self.assertEqual({name: str(booking.context['form'][name].value()) for name in ('clinician', 'date', 'time')},
                         {'clinician': str(self.doctor.pk), 'date': day.isoformat(), 'time': '09:45'})
        # Choosing a clinician shows only theirs.
        only = self.client.get(f'{self.day_url()}&clinician={self.colleague.pk}')
        self.assertEqual([diary['clinician'] for diary in only.context['day_diaries']], [self.colleague])
        # It's one day's free time, so Week and Month views don't show it.
        for view in ('week', 'calendar'):
            with self.subTest(view=view):
                other = self.client.get(reverse('portal:staff-schedule'), {'view': view, 'date': day.isoformat()})
                self.assertEqual(other.context['day_diaries'], [])
                self.assertNotContains(other, 'id="availability-heading"')

    def test_booking_forms_get_the_doctors_day_as_a_grid_of_slots(self):
        day = timezone.localdate(self.starts_at)
        DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, weekday=day.weekday(),
                                            is_working=True, starts_at=time(9), ends_at=time(10))
        booked = self.appointment(patient=self.other_patient, starts_at=timezone.make_aware(datetime.combine(day, time(9, 15))),
                                  duration_minutes=30)
        url = reverse('portal:appointment-book-times')
        grid = lambda **query: [(slot['time'], slot['state'], slot['label']) for slot in self.client.get(
            url, {'clinician': self.doctor.pk, 'date': day.isoformat(), **query}).json()['slots']]
        self.login(self.admin)
        self.assertEqual(grid(duration=15), [('09:00', 'free', 'Free'), ('09:15', 'booked', 'Booked'),
                                             ('09:30', 'booked', 'Booked'), ('09:45', 'free', 'Free')])
        # 30 minutes doesn't fit before the booking, or before the end of the hours.
        self.assertEqual([state for _, state, _ in grid(duration=30)], ['short', 'booked', 'booked', 'short'])
        # Moving the booking: its own time is where it is now, and the rest of its time is free.
        self.assertEqual([state for _, state, _ in grid(duration=30, appointment=booked.pk)], ['free', 'current', 'free', 'short'])
        # The patient is booked elsewhere at 09:00.
        self.appointment(clinician=self.colleague, starts_at=timezone.make_aware(datetime.combine(day, time(9))), duration_minutes=15)
        self.assertEqual(grid(duration=15, patient=self.patient.pk)[0][1:], ('clash', 'Patient busy'))
        # Patients see only what they can book: someone else's booking is just Unavailable.
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-booking-times'), {'clinician': self.doctor.pk, 'date': day.isoformat()})
        self.assertEqual([(slot['time'], slot['label']) for slot in response.json()['slots']],
                         [('09:00', 'Unavailable'), ('09:15', 'Unavailable'), ('09:30', 'Unavailable'), ('09:45', 'Free')])
        self.assertNotIn('Booked', response.content.decode())

    def test_doctors_keep_open_record_and_have_no_edit(self):
        self.appointment()
        self.login(self.doctor)
        page = self.client.get(self.day_url())
        self.assertContains(page, 'Open record<span')
        self.assertNotContains(page, 'edit-appointment-')
        self.assertFalse(page.context['manage_bookings'])


class OperationsFeedbackTests(SessionMixin, OperationsFixture):
    def test_pending_shipping_list_has_delivery_details_due_date_and_note_action(self):
        shipment = self.ready()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'), {'week': timezone.localdate()})
        self.assertNotContains(page, 'ops-metric')
        self.assertNotContains(page, 'Pens to order')
        html = page.content.decode()
        table = html[html.index('ops-shipping-table'):]
        headings = [re.sub(r'<[^>]+>', '', heading) for heading in re.findall(r'<th scope="col">(.*?)</th>', table[:table.index('</thead>')])]
        # Pending parcels have a selection checkbox and one actions column; their workflow status is the tab itself.
        self.assertEqual(headings, ['', 'Patient', 'Doctor', 'Deliver to', 'Product', 'Qty', 'Paid', 'Due date', 'Waybill', 'Actions'])
        self.assertContains(page, self.doctor.titled_name)
        row = page.context['shipments'][0]
        response = self.client.post(reverse('portal:ops-shipment-note', args=[shipment.pk]), {
            'workflow_context': row.note_context, 'note': 'Leave with   reception', 'list_query': ''})
        self.assertRedirects(response, reverse('portal:ops-shipping'), fetch_redirect_response=False)
        shipment.refresh_from_db()
        self.assertEqual(shipment.note, 'Leave with reception')
        self.assertTrue(AuditEvent.objects.filter(action='shipment.note_updated', target_id=str(shipment.pk)).exists())
        self.assertContains(self.client.get(reverse('portal:ops-shipping'), {'week': timezone.localdate()}), 'Leave with reception')
        # A doctor can't.
        self.login(self.doctor)
        self.assertEqual(self.client.post(reverse('portal:ops-shipment-note', args=[shipment.pk]), {'note': 'x'}).status_code, 403)

    def test_stock_and_weekly_demand_are_in_inventory_instead_of_shipping(self):
        self.ready(self.shipment(quantity=2))
        self.shipment(quantity=3)
        self.shipment(quantity=5, status='cancelled')
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'), {'week': timezone.localdate()})
        self.assertNotIn('stock_rows', page.context)
        self.assertNotContains(page, 'Pens this week')
        week = self.today - timedelta(days=self.today.weekday())
        with patch('portal.operations_views.shipping_week', return_value=week):
            inventory = self.client.get(reverse('portal:inventory'))
        rows = {row['product'].pk: row for row in inventory.context['rows']}
        self.assertEqual(set(rows), {self.product.pk, self.open_product.pk})
        self.assertEqual(rows[self.product.pk]['demand']['to_ship'], 5)
        self.assertEqual(rows[self.open_product.pk]['demand']['to_ship'], 0)
        self.assertEqual((rows[self.product.pk]['on_hand'], rows[self.product.pk]['reserved'],
                          rows[self.product.pk]['available']), (100, 0, 100))
        self.assertContains(inventory, 'On hand | Reserved')
        self.assertContains(inventory, 'Safety stock')
        self.assertNotContains(inventory, 'ops-tbc')
        # Inventory and demand are shared with Super Admins; their additional controls edit planning inputs.
        self.login(self.super_admin)
        with patch('portal.operations_views.shipping_week', return_value=week):
            inventory = self.client.get(reverse('portal:inventory'))
        self.assertTrue(inventory.context['can_edit_metrics'])
        self.assertEqual({row['product'].pk: row['demand']['draft'] for row in inventory.context['rows']}[self.product.pk], 3)
        self.assertEqual(sum(row['demand']['next_week'] for row in inventory.context['rows']), 0)

    def test_dispatch_history_list_without_tracking_and_month_filtered_excel(self):
        shipment = self.locked()
        Shipment.objects.filter(pk=shipment.pk).update(status='dispatched', dispatched_at=timezone.now(), tracking_number='TRK-1',
                                                       note='Fragile', dispatch_snapshot={'patient_name': 'Alpha Patient', 'items': [
                                                           {'product': 'Explicit prescribed product', 'dose': 'Explicit physician dose', 'quantity': 4}]})
        self.login(self.admin)
        month = timezone.localdate().strftime('%Y-%m')
        # All recorded dispatches are shown by default; month bounds are optional.
        default = self.client.get(reverse('portal:ops-history'))
        self.assertEqual([item.pk for item in default.context['shipments']], [shipment.pk])
        self.assertNotContains(default, 'Dispatch history view')
        self.assertNotContains(default, 'Previous week')
        page = self.client.get(reverse('portal:ops-history'), {'start_month': month, 'end_month': month})
        self.assertEqual([item.pk for item in page.context['shipments']], [shipment.pk])
        self.assertNotContains(page, 'Tracking reference')
        self.assertNotContains(page, 'TRK-1')
        self.assertContains(page, 'Download Excel')
        export = self.client.get(f"{reverse('portal:ops-history-export')}?{page.context['export_query']}")
        self.assertIn('.xlsx', export['Content-Disposition'])
        self.assertEqual(export['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        content = b''.join(export.streaming_content)
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        rows = list(workbook.active.iter_rows(values_only=True))
        self.assertEqual(list(rows[0][:3]), ['Shipment', 'Planned date', 'Dispatched'])
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row['Note'], 'Fragile')
        self.assertEqual(row['Doctor'], self.doctor.titled_name)
        listed = self.client.get(reverse('portal:ops-history'), {'start_month': month})
        self.assertEqual([item.pk for item in listed.context['shipments']], [shipment.pk])
        self.assertEqual(listed.context['export_query'], f'start_month={month}')


@override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class DropoutsTests(SessionMixin, OperationsFixture):
    def help(self, reason='The nausea is too much.'):
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        self.assertContains(page, 'data-modal-open="treatment-help"')
        return self.client.post(reverse('portal:patient-help-request'), {'treatment_context': page.context['help_context'], 'reason': reason})

    def test_something_is_wrong_reaches_the_doctor_and_practice_and_the_dropouts_list_until_resolved(self):
        response = self.help()
        item = HelpRequest.objects.get()
        self.assertRedirects(response, f"{reverse('portal:patient-messages')}?thread={item.thread_id}#patient-conversation",
                             fetch_redirect_response=False)
        self.assertEqual(set(MessageThreadParticipant.objects.filter(thread=item.thread).values_list('user_id', flat=True)),
                         {self.doctor.pk, self.admin.pk})
        self.assertEqual(PatientMessage.objects.get(thread=item.thread).body, 'The nausea is too much.')
        # Asking again adds to the same conversation.
        self.help('Still not better.')
        self.assertEqual(HelpRequest.objects.count(), 1)
        self.assertEqual(PatientMessage.objects.filter(thread=item.thread).count(), 2)
        self.login(self.admin)
        page = self.client.get(reverse('portal:dropouts'))
        self.assertContains(page, 'Still not better.')
        row = page.context['help_page'][0]
        self.assertIn(f'thread={item.thread_id}', row.message_patient_url)
        self.assertIn(f'members={self.doctor.pk}', row.message_doctor_url)
        # Message doctor opens Team messages ready to write to them about the patient.
        compose = self.client.get(row.message_doctor_url)
        self.assertEqual(compose.context['thread_form'].initial['members'], [self.doctor.pk])
        self.assertEqual(compose.context['thread_form'].initial['patient'], str(self.patient.pk))
        response = self.client.post(reverse('portal:help-request-resolve', args=[item.pk]),
                                    {'workflow_context': row.resolve_context, 'note': 'Dose lowered.'})
        self.assertRedirects(response, reverse('portal:dropouts'), fetch_redirect_response=False)
        item.refresh_from_db()
        self.assertEqual((item.status, item.resolved_by, item.resolution_note), ('resolved', self.admin, 'Dose lowered.'))
        self.assertFalse(self.client.get(reverse('portal:dropouts')).context['help_page'].object_list)
        # Clinicians don't have the page or its actions.
        self.login(self.doctor)
        self.assertEqual(self.client.post(reverse('portal:help-request-resolve', args=[item.pk])).status_code, 403)

    def test_cancelling_needs_a_reason_moves_them_to_cancelled_and_sends_one_winback_email(self):
        self.help()
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        self.assertContains(page, 'data-modal-open="cancel-subscription"')
        url = reverse('portal:patient-subscription-status', args=[self.plan.pk])
        response = self.client.post(url, {'treatment_context': page.context['action_context'], 'action': 'cancel', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'id="cancel-subscription" aria-labelledby="cancel-subscription-title" open', status_code=400)
        self.assertEqual(PatientSubscription.objects.get(pk=self.plan.pk).status, 'active')
        before = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'treatment_context': response.context['action_context'], 'action': 'cancel',
                                              'confirm': 'on', 'reasons': ['cost', 'side_effects'], 'note': 'Too pricey for now'})
        self.assertEqual(response.status_code, 302)
        plan = PatientSubscription.objects.get(pk=self.plan.pk)
        self.assertEqual((plan.status, plan.cancellation_reasons, plan.cancellation_note),
                         ('cancelled', ['cost', 'side_effects'], 'Too pricey for now'))
        self.assertIsNotNone(plan.winback_sent_at)
        sent = mail.outbox[before:]
        self.assertEqual([(message.to, message.subject) for message in sent], [([self.patient_user.email], 'Can we help before you go?')])
        self.assertEqual(HelpRequest.objects.get().status, 'cancelled')
        self.login(self.admin)
        page = self.client.get(reverse('portal:dropouts'))
        self.assertFalse(page.context['help_page'].object_list)
        self.assertContains(page, 'The cost, Side effects')
        self.assertContains(page, 'email sent')

    def test_please_call_from_the_questionnaire_until_called_or_booked(self):
        lead = Lead.objects.create(company=self.company, first_name='Thandi', last_name='Caller', email='thandi@example.test',
                                   submission_key='8d3b5a6e-6f63-4b1c-9b21-0d1c7c3f4a10')
        session = self.client.session
        session['public_intake_lead_id'] = lead.pk
        session.save()
        result = self.client.get(reverse('portal:questionnaire-result'))
        self.assertContains(result, 'Rather talk to us first?')
        response = self.client.post(reverse('portal:questionnaire-call-me'), {'phone': '083 382 7292', 'note': 'What does it cost?'})
        self.assertRedirects(response, f"{reverse('portal:questionnaire-result')}#call-me-heading", fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('portal:questionnaire-result')), 'will call you on 083 382 7292')
        callback = CallbackRequest.objects.get()
        self.assertEqual((callback.name, callback.phone, callback.note, callback.email),
                         ('Thandi Caller', '083 382 7292', 'What does it cost?', 'thandi@example.test'))
        # Please call is on the Leads tab, not Dropouts (Dr Czech, 4 Nov 2026).
        self.login(self.admin)
        self.assertNotContains(self.client.get(reverse('portal:dropouts')), 'please-call-heading')
        page = self.client.get(reverse('portal:staff-leads'))
        self.assertContains(page, 'What does it cost?')
        row = page.context['call_page'][0]
        response = self.client.post(reverse('portal:callback-called', args=[callback.pk]),
                                    {'workflow_context': row.called_context, 'note': 'Explained prices.'})
        self.assertRedirects(response, f"{reverse('portal:staff-leads')}#please-call-heading", fetch_redirect_response=False)
        callback.refresh_from_db()
        self.assertEqual((callback.status, callback.called_by, callback.staff_note), ('called', self.admin, 'Explained prices.'))
        self.assertFalse(self.client.get(reverse('portal:staff-leads')).context['call_page'].object_list)
        # Asking again opens a new one; booking (checkout) takes it off.
        again = request_callback(lead=lead, phone='083 382 7292')
        close_callbacks(lead)
        again.refresh_from_db()
        self.assertEqual(again.status, 'booked')
