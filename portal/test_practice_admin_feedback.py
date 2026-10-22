"""The practice administrator's view after the owner's feedback (22 Oct 2026): their menu and account menu, read-only
working hours, moving and cancelling bookings from Day view, the Weekly shipping list, Dispatch stock, Dispatch
history by week, and Dropouts with Please call."""

import csv
import io
import re
from datetime import datetime, time, timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

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
        self.assertEqual(labels, ['Overview', 'My tasks', 'Messages', 'Calendar &amp; Bookings', 'Operations', 'Administration'])
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

    def test_leads_are_the_unconverted_enquiries_and_lead_history_has_them_all(self):
        waiting = Lead.objects.create(company=self.company, first_name='Tumi', last_name='Dlamini', email='tumi@example.test',
                                      stage=Lead.Stage.BOOKING)
        converted = Lead.objects.create(company=self.company, first_name='Jimmy', last_name='Patient', email='jimmy@example.test',
                                        stage=Lead.Stage.CONVERTED, converted_patient=self.patient)
        self.login(self.admin)
        leads = self.client.get(reverse('portal:staff-leads'))
        self.assertEqual([lead.pk for lead in leads.context['leads']], [waiting.pk])
        self.assertNotIn('converted', [option['value'] for option in leads.context['filter_menu']['sections'][1]['options']])
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
        response = self.client.post(reverse('portal:patient-doctor-assign', args=[self.patient.pk]), {'doctor': ''})
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

    def test_doctors_keep_open_record_and_have_no_edit_or_cancel(self):
        self.appointment()
        self.login(self.doctor)
        page = self.client.get(self.day_url())
        self.assertContains(page, 'Open record<span')
        self.assertNotContains(page, 'edit-appointment-')
        self.assertFalse(page.context['manage_bookings'])


class OperationsFeedbackTests(SessionMixin, OperationsFixture):
    def test_shipping_list_has_the_patients_doctor_product_quantity_status_and_notes(self):
        shipment = self.ready()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'))
        self.assertNotContains(page, 'ops-metric')
        self.assertNotContains(page, 'Pens to order')
        headings = re.findall(r'<th scope="col">([^<]+)</th>', page.content.decode())
        self.assertEqual(headings, ['Patient', 'Doctor', 'Product', 'Qty', 'Status', 'Notes'])
        self.assertContains(page, self.doctor.titled_name)
        row = page.context['shipments'][0]
        response = self.client.post(reverse('portal:ops-shipment-note', args=[shipment.pk]), {
            'workflow_context': row.note_context, 'note': 'Leave with   reception', 'list_query': ''})
        self.assertRedirects(response, reverse('portal:ops-shipping'), fetch_redirect_response=False)
        shipment.refresh_from_db()
        self.assertEqual(shipment.note, 'Leave with reception')
        self.assertTrue(AuditEvent.objects.filter(action='shipment.note_updated', target_id=str(shipment.pk)).exists())
        self.assertContains(self.client.get(reverse('portal:ops-shipping')), 'Leave with reception')
        # A doctor can't.
        self.login(self.doctor)
        self.assertEqual(self.client.post(reverse('portal:ops-shipment-note', args=[shipment.pk]), {'note': 'x'}).status_code, 403)

    def test_dispatch_stock_lists_each_catalogue_item_with_to_be_shipped_and_to_order(self):
        self.ready(self.shipment(quantity=2))
        self.shipment(quantity=3)  # A draft: going out, but not ordered until it's ready.
        self.shipment(quantity=5, status='cancelled')
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-stock'))
        rows = {row['product'].pk: row for row in page.context['rows']}
        self.assertEqual(set(rows), {self.product.pk, self.open_product.pk})
        self.assertEqual((rows[self.product.pk]['to_ship'], rows[self.product.pk]['to_order']), (5, 2))
        self.assertEqual((rows[self.open_product.pk]['to_ship'], rows[self.open_product.pk]['to_order']), (0, 0))
        self.assertNotContains(page, 'How To order is worked out')
        self.assertNotContains(page, 'In draft parcels')
        self.assertContains(page, '<th scope="col">On hand</th>', html=True)
        self.assertNotContains(page, 'Safety stock')
        self.assertContains(page, '<span class="ops-tbc">TBC</span>', count=2)
        self.assertContains(page, f'Week {page.context["week_number"]}')
        # Super Admins see how it's worked out.
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:ops-stock'))
        self.assertContains(page, 'How To order is worked out')
        self.assertContains(page, '<th scope="col">Safety stock</th>', html=True)
        self.assertEqual({row['product'].pk: row['draft'] for row in page.context['rows']}[self.product.pk], 3)
        # Next week is empty.
        later = self.client.get(page.context['next_week_url'])
        self.assertEqual(sum(row['to_ship'] for row in later.context['rows']), 0)

    def test_dispatch_history_by_week_without_tracking_and_exported(self):
        shipment = self.locked()
        Shipment.objects.filter(pk=shipment.pk).update(status='dispatched', dispatched_at=timezone.now(), tracking_number='TRK-1',
                                                       note='Fragile', dispatch_snapshot={'patient_name': 'Alpha Patient', 'items': [
                                                           {'product': 'Explicit prescribed product', 'dose': 'Explicit physician dose', 'quantity': 4}]})
        self.login(self.admin)
        week = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        # Week is the default view, on last week.
        default = self.client.get(reverse('portal:ops-history'))
        self.assertTrue(default.context['by_week'])
        self.assertEqual(default.context['week_start'], week - timedelta(days=7))
        self.assertEqual(list(default.context['shipments']), [])
        page = self.client.get(default.context['this_week_url'])
        self.assertTrue(page.context['by_week'])
        self.assertEqual([item.pk for item in page.context['shipments']], [shipment.pk])
        self.assertNotContains(page, 'Tracking reference')
        self.assertNotContains(page, 'TRK-1')
        self.assertContains(page, 'Export week CSV')
        self.assertEqual(page.context['week_start'], week)
        export = self.client.get(f"{reverse('portal:ops-history-export')}?{page.context['export_query']}")
        self.assertIn(f'dispatch-history-{week.isoformat()}-to-{(week + timedelta(days=6)).isoformat()}.csv', export['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        self.assertEqual(rows[0][:3], ['Shipment', 'Planned date', 'Dispatched'])
        self.assertEqual(rows[1][rows[0].index('Note')], 'Fragile')
        self.assertEqual(rows[1][rows[0].index('Doctor')], self.doctor.titled_name)
        # The list keeps its date range, and its export takes it.
        listed = self.client.get(reverse('portal:ops-history'), {'view': 'list', 'start': week.isoformat()})
        self.assertFalse(listed.context['by_week'])
        self.assertEqual([item.pk for item in listed.context['shipments']], [shipment.pk])
        self.assertEqual(listed.context['export_query'], f'start={week.isoformat()}')
        self.assertIn(('view', 'list'), listed.context['filter_menu']['hidden'])


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
        self.login(self.admin)
        page = self.client.get(reverse('portal:dropouts'))
        self.assertContains(page, 'What does it cost?')
        row = page.context['call_page'][0]
        self.client.post(reverse('portal:callback-called', args=[callback.pk]),
                         {'workflow_context': row.called_context, 'note': 'Explained prices.'})
        callback.refresh_from_db()
        self.assertEqual((callback.status, callback.called_by, callback.staff_note), ('called', self.admin, 'Explained prices.'))
        self.assertFalse(self.client.get(reverse('portal:dropouts')).context['call_page'].object_list)
        # Asking again opens a new one; booking (checkout) takes it off.
        again = request_callback(lead=lead, phone='083 382 7292')
        close_callbacks(lead)
        again.refresh_from_db()
        self.assertEqual(again.status, 'booked')
