"""Appointment negotiation keeps the booking unchanged until its recipient agrees."""

from django.test import override_settings
from datetime import timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AppointmentProposal, MessageThread
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class DecisionFormsParser(HTMLParser):
    """Collect action choices without coupling assertions to the card's styling."""

    def __init__(self, action):
        super().__init__()
        self.action = action
        self.in_form = False
        self.decisions = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.in_form = attrs.get('action') == self.action
        elif self.in_form and tag in ('button', 'input') and attrs.get('name') == 'decision':
            self.decisions.add(attrs.get('value'))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.in_form = False


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AppointmentProposalPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Alpha Practice', slug='alpha-proposals')
        cls.other_company = Company.objects.create(name='Beta Practice', slug='beta-proposals')
        cls.doctor = users.create_user(email='proposal-doctor@example.test', first_name='Doctor')
        cls.second_doctor = users.create_user(email='other-proposal-doctor@example.test', first_name='Other doctor')
        cls.administrator = users.create_user(email='proposal-admin@example.test', first_name='Administrator')
        cls.super_admin = users.create_user(email='proposal-super@example.test', first_name='Super administrator')
        cls.patient_user = users.create_user(email='proposal-patient@example.test', first_name='Alice')
        cls.other_patient_user = users.create_user(email='other-proposal-patient@example.test', first_name='Beth')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.second_doctor, CompanyMembership.Role.DOCTOR),
            (cls.administrator, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(user=user, company=cls.company, role=role)
        CompanyMembership.objects.create(user=cls.doctor, company=cls.other_company, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Patient',
            assigned_doctor=cls.doctor,
        )
        cls.other_practice_patient = Patient.objects.create(
            company=cls.other_company, user=cls.patient_user, first_name='Alice', last_name='Patient',
            assigned_doctor=cls.doctor,
        )
        cls.other_patient = Patient.objects.create(
            company=cls.company, user=cls.other_patient_user, first_name='Beth', last_name='Patient',
        )
        cls.thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Appointment arrangements', opened_by=cls.patient_user,
        )
        cls.other_practice_thread = MessageThread.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient,
            subject='Beta arrangements', opened_by=cls.patient_user,
        )
        cls.other_patient_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.other_patient,
            subject='Another patient arrangements', opened_by=cls.other_patient_user,
        )
        cls.original_start = (timezone.now() + timedelta(days=7)).replace(hour=8, minute=0, second=0, microsecond=0)
        cls.proposed_start = (cls.original_start + timedelta(days=1)).replace(hour=11)
        cls.appointment = Appointment.objects.create(
            company=cls.company, patient=cls.patient, clinician=cls.doctor,
            starts_at=cls.original_start, duration_minutes=30, appointment_type=Appointment.Type.REVIEW,
            video_link='https://example.test/existing-consult',
        )
        cls.other_practice_appointment = Appointment.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient, clinician=cls.doctor,
            starts_at=cls.original_start + timedelta(days=2), duration_minutes=30,
        )

    def login(self, user, *, company=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def propose_url(self, role='staff', thread=None):
        return reverse(f'portal:{role}-appointment-propose', args=[(thread or self.thread).pk])

    def respond_url(self, proposal, role='patient'):
        return reverse(f'portal:{role}-appointment-respond', args=[proposal.pk])

    def make_proposal(self, *, role='staff', appointment=None, thread=None, starts_at=None, company=None):
        user = self.doctor if role == 'staff' else self.patient_user
        appointment = appointment or self.appointment
        self.login(user, company=company)
        response = self.client.post(self.propose_url(role, thread), {
            'appointment': appointment.pk,
            'proposed_starts_at': (starts_at or self.proposed_start).isoformat(),
            'note': 'Would this time work for you?',
        })
        self.assertEqual(response.status_code, 302)
        return AppointmentProposal.objects.filter(appointment=appointment).latest('pk')

    def assert_original_booking(self):
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.original_start)
        self.assertEqual(self.appointment.status, Appointment.Status.BOOKED)
        self.assertEqual(self.appointment.clinician_id, self.doctor.pk)
        self.assertEqual(self.appointment.duration_minutes, 30)

    def assert_invalid_proposal(self, response, *, thread=None, field=None):
        thread = thread or self.thread
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['failed_form'], 'proposal_form')
        self.assertEqual(response.context['reply_thread_id'], thread.pk)
        rendered_thread = next(item for item in response.context['message_threads'] if item.pk == thread.pk)
        self.assertTrue(rendered_thread.proposal_form.is_bound)
        self.assertTrue(rendered_thread.proposal_form.errors)
        if field:
            self.assertIn(field, rendered_thread.proposal_form.errors)
        return rendered_thread.proposal_form

    def assert_invalid_response(self, response, proposal):
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['proposal_error'])
        self.assertEqual(response.context['failed_proposal_id'], proposal.pk)

    def test_anonymous_proposal_and_response_endpoints_redirect_to_login(self):
        for role in ('staff', 'patient'):
            for path in (
                self.propose_url(role),
                reverse(f'portal:{role}-appointment-respond', args=[999]),
            ):
                with self.subTest(path=path):
                    self.assertRedirects(
                        self.client.post(path, {}),
                        f'{reverse("accounts:login")}?next={path}', fetch_redirect_response=False,
                    )

    def test_proposal_and_acceptance_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(self.doctor, client=csrf_client)
        response = csrf_client.post(self.propose_url(), {
            'appointment': self.appointment.pk, 'proposed_starts_at': self.proposed_start.isoformat(),
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AppointmentProposal.objects.exists())
        proposal = self.make_proposal()
        self.login(self.patient_user, client=csrf_client)
        self.assertEqual(csrf_client.post(self.respond_url(proposal), {'decision': 'accept'}).status_code, 403)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        self.assert_original_booking()

    def test_proposals_and_responses_are_post_only(self):
        proposal = self.make_proposal()
        for role, user in (('staff', self.doctor), ('patient', self.patient_user)):
            self.login(user)
            for path in (self.propose_url(role), self.respond_url(proposal, role)):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 405)

    def test_doctor_proposal_captures_original_booking_without_changing_it(self):
        proposal = self.make_proposal()
        self.assert_original_booking()
        self.assertEqual((proposal.company_id, proposal.patient_id, proposal.thread_id),
                         (self.company.pk, self.patient.pk, self.thread.pk))
        self.assertEqual((proposal.proposed_by_id, proposal.recipient_id, proposal.proposer_role),
                         (self.doctor.pk, self.patient_user.pk, 'doctor'))
        self.assertEqual(proposal.proposed_starts_at, self.proposed_start)
        self.assertEqual((proposal.original_starts_at, proposal.original_status), (self.original_start, 'booked'))
        self.assertEqual((proposal.original_clinician_id, proposal.original_duration_minutes), (self.doctor.pk, 30))
        self.assertEqual((proposal.kind, proposal.status), ('reschedule', 'pending'))
        self.assertIsNone(proposal.resulting_appointment_id)

    def test_patient_proposal_targets_the_appointment_clinician(self):
        proposal = self.make_proposal(role='patient')
        self.assert_original_booking()
        self.assertEqual((proposal.proposed_by_id, proposal.recipient_id, proposal.proposer_role),
                         (self.patient_user.pk, self.doctor.pk, 'patient'))
        self.assertEqual(proposal.status, 'pending')

    def test_patient_acceptance_moves_existing_booking_and_repeat_is_idempotent(self):
        proposal = self.make_proposal()
        appointment_count = Appointment.objects.count()
        self.login(self.patient_user)
        url = self.respond_url(proposal)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertRedirects(
                    self.client.post(url, {'decision': 'accept'}),
                    f'{reverse("portal:patient-messages")}?thread={self.thread.pk}#patient-conversation',
                    fetch_redirect_response=False,
                )
                proposal.refresh_from_db()
                self.appointment.refresh_from_db()
                self.assertEqual(proposal.status, 'accepted')
                self.assertEqual(proposal.appointment_id, self.appointment.pk)
                self.assertIsNone(proposal.resulting_appointment_id)
                self.assertEqual(self.appointment.starts_at, self.proposed_start)
                self.assertEqual(self.appointment.status, 'booked')
                self.assertEqual(Appointment.objects.count(), appointment_count)

    def test_clinician_acceptance_of_patient_proposal_moves_booking(self):
        proposal = self.make_proposal(role='patient')
        self.login(self.doctor)
        response = self.client.post(self.respond_url(proposal, 'staff'), {'decision': 'accept'})
        self.assertRedirects(
            response, reverse('portal:patient-detail', args=[self.patient.pk]) + f'?tab=messages&thread={self.thread.pk}', fetch_redirect_response=False,
        )
        self.appointment.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.proposed_start)
        self.assertEqual(proposal.status, 'accepted')

    def test_declining_proposal_leaves_booking_unchanged(self):
        proposal = self.make_proposal()
        self.login(self.patient_user)
        self.assertEqual(self.client.post(self.respond_url(proposal), {'decision': 'decline'}).status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'declined')
        self.assertIsNone(proposal.resulting_appointment_id)
        self.assert_original_booking()

    def test_only_recipient_accepts_or_declines_and_only_proposer_withdraws(self):
        proposal = self.make_proposal()
        for decision in ('accept', 'decline'):
            with self.subTest(decision=decision):
                self.assertEqual(self.client.post(self.respond_url(proposal, 'staff'), {'decision': decision}).status_code, 403)
        self.login(self.patient_user)
        self.assertEqual(self.client.post(self.respond_url(proposal), {'decision': 'withdraw'}).status_code, 403)
        self.login(self.second_doctor)
        self.assertEqual(self.client.post(self.respond_url(proposal, 'staff'), {'decision': 'accept'}).status_code, 403)
        self.login(self.doctor)
        self.assertEqual(self.client.post(self.respond_url(proposal, 'staff'), {'decision': 'withdraw'}).status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'withdrawn')
        self.assert_original_booking()

    def test_practice_administrators_can_read_but_cannot_propose_or_respond(self):
        proposal = self.make_proposal()
        for user in (self.administrator, self.super_admin):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(reverse('portal:patient-detail', args=[self.patient.pk])).status_code, 200)
                self.assertEqual(self.client.post(self.propose_url(), {
                    'appointment': self.appointment.pk, 'proposed_starts_at': self.proposed_start.isoformat(),
                }).status_code, 403)
                self.assertEqual(self.client.post(self.respond_url(proposal, 'staff'), {'decision': 'accept'}).status_code, 403)
        self.assertEqual(AppointmentProposal.objects.count(), 1)
        self.assert_original_booking()

    def test_non_clinician_cannot_propose_for_another_doctors_appointment(self):
        self.login(self.second_doctor)
        response = self.client.post(self.propose_url(), {
            'appointment': self.appointment.pk, 'proposed_starts_at': self.proposed_start.isoformat(),
            'note': 'Keep my draft, but reject the appointment selection.',
        })
        self.assert_invalid_proposal(response, field='appointment')
        self.assertFalse(AppointmentProposal.objects.exists())
        self.assert_original_booking()

    def test_non_active_practice_thread_and_proposal_urls_are_not_accessible(self):
        proposal = self.make_proposal(
            appointment=self.other_practice_appointment, thread=self.other_practice_thread,
            company=self.other_company, starts_at=self.proposed_start + timedelta(days=3),
        )
        for role, user in (('staff', self.doctor), ('patient', self.patient_user)):
            with self.subTest(role=role):
                self.login(user)
                self.assertEqual(self.client.post(self.propose_url(role, self.other_practice_thread), {
                    'appointment': self.other_practice_appointment.pk,
                    'proposed_starts_at': self.proposed_start.isoformat(),
                }).status_code, 404)
                self.assertEqual(self.client.post(self.respond_url(proposal, role), {'decision': 'accept'}).status_code, 404)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')

    def test_patient_cannot_propose_in_another_patients_thread(self):
        self.login(self.patient_user)
        response = self.client.post(self.propose_url('patient', self.other_patient_thread), {
            'appointment': self.appointment.pk, 'proposed_starts_at': self.proposed_start.isoformat(),
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AppointmentProposal.objects.exists())

    def test_invalid_proposal_preserves_draft_in_correct_thread(self):
        self.login(self.patient_user)
        response = self.client.post(self.propose_url('patient'), {
            'appointment': self.appointment.pk, 'proposed_starts_at': 'not-a-date',
            'note': 'Please keep this explanation while I correct the time.',
        })
        form = self.assert_invalid_proposal(response, field='proposed_starts_at')
        self.assertEqual(form['proposed_starts_at'].value(), 'not-a-date')
        self.assertEqual(form['note'].value(), 'Please keep this explanation while I correct the time.')
        self.assertContains(response, 'Please keep this explanation while I correct the time.')
        self.assertFalse(AppointmentProposal.objects.exists())

    def test_past_proposed_time_is_rejected_without_mutation(self):
        self.login(self.doctor)
        response = self.client.post(self.propose_url(), {
            'appointment': self.appointment.pk,
            'proposed_starts_at': (timezone.now() - timedelta(days=1)).isoformat(),
        })
        self.assert_invalid_proposal(response)
        self.assertFalse(AppointmentProposal.objects.exists())
        self.assert_original_booking()

    def test_completed_appointment_cannot_be_proposed_for_change(self):
        self.appointment.status = Appointment.Status.COMPLETED
        self.appointment.save(update_fields=['status'])
        self.login(self.doctor)
        response = self.client.post(self.propose_url(), {
            'appointment': self.appointment.pk, 'proposed_starts_at': self.proposed_start.isoformat(),
        })
        self.assert_invalid_proposal(response, field='appointment')
        self.assertFalse(AppointmentProposal.objects.exists())
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, 'completed')
        self.assertEqual(self.appointment.starts_at, self.original_start)

    def test_conflict_added_after_proposal_prevents_acceptance_across_practices(self):
        proposal = self.make_proposal()
        Appointment.objects.create(
            company=self.other_company, patient=self.other_practice_patient, clinician=self.doctor,
            starts_at=self.proposed_start + timedelta(minutes=10), duration_minutes=30,
        )
        self.login(self.patient_user)
        response = self.client.post(self.respond_url(proposal), {'decision': 'accept'})
        self.assert_invalid_response(response, proposal)
        self.assert_original_booking()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        self.assertIsNone(proposal.resulting_appointment_id)

    def test_changed_original_booking_requires_new_proposal_before_acceptance(self):
        for field, value in (
            ('starts_at', self.original_start + timedelta(hours=2)),
            ('status', Appointment.Status.COMPLETED),
            ('clinician_id', self.second_doctor.pk),
            ('duration_minutes', 45),
        ):
            with self.subTest(field=field):
                appointment = Appointment.objects.create(
                    company=self.company, patient=self.patient, clinician=self.doctor,
                    starts_at=self.original_start + timedelta(days=20), duration_minutes=30,
                )
                proposal = self.make_proposal(appointment=appointment, starts_at=self.proposed_start + timedelta(days=20))
                setattr(appointment, field, value)
                appointment.save(update_fields=[field.removesuffix('_id') if field.endswith('_id') else field])
                self.login(self.patient_user)
                response = self.client.post(self.respond_url(proposal), {'decision': 'accept'})
                self.assert_invalid_response(response, proposal)
                appointment.refresh_from_db()
                proposal.refresh_from_db()
                self.assertEqual(getattr(appointment, field), value)
                self.assertEqual(proposal.status, 'pending')
                self.assertIsNone(proposal.resulting_appointment_id)

    def test_cancelled_and_no_show_rebooking_creates_new_booking_only_after_acceptance(self):
        for offset, status in enumerate((Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW), start=1):
            with self.subTest(status=status):
                original = Appointment.objects.create(
                    company=self.company, patient=self.patient, clinician=self.doctor,
                    starts_at=self.original_start - timedelta(days=20), duration_minutes=30,
                    appointment_type=Appointment.Type.REVIEW, status=status,
                )
                count = Appointment.objects.count()
                new_start = self.proposed_start + timedelta(days=offset + 4)
                proposal = self.make_proposal(appointment=original, starts_at=new_start)
                self.assertEqual(proposal.kind, 'rebook')
                self.assertEqual(Appointment.objects.count(), count)
                self.login(self.patient_user)
                self.assertEqual(self.client.post(self.respond_url(proposal), {'decision': 'accept'}).status_code, 302)
                proposal.refresh_from_db()
                original.refresh_from_db()
                self.assertEqual(original.status, status)
                self.assertEqual(original.starts_at, self.original_start - timedelta(days=20))
                self.assertEqual(Appointment.objects.count(), count + 1)
                replacement = proposal.resulting_appointment
                self.assertNotEqual(replacement.pk, original.pk)
                self.assertEqual((replacement.company_id, replacement.patient_id, replacement.clinician_id),
                                 (self.company.pk, self.patient.pk, self.doctor.pk))
                self.assertEqual((replacement.starts_at, replacement.status, replacement.duration_minutes),
                                 (new_start, 'booked', 30))

    def test_counterproposal_supersedes_previous_offer_without_changing_booking(self):
        previous = self.make_proposal()
        counter = self.make_proposal(role='patient', starts_at=self.proposed_start + timedelta(days=4))
        previous.refresh_from_db()
        self.assertEqual(previous.status, 'superseded')
        self.assertEqual(counter.status, 'pending')
        self.assertEqual((counter.proposed_by_id, counter.recipient_id), (self.patient_user.pk, self.doctor.pk))
        self.assertEqual(AppointmentProposal.objects.filter(appointment=self.appointment, status='pending').count(), 1)
        self.assert_original_booking()
        response = self.client.post(self.respond_url(previous), {'decision': 'accept'})
        self.assert_invalid_response(response, previous)
        self.assert_original_booking()

    def test_proposal_cards_show_recipient_actions_and_south_african_time(self):
        proposal = self.make_proposal()
        for role, user, page, expected in (
            ('staff', self.doctor, reverse('portal:patient-detail', args=[self.patient.pk]) + f'?tab=messages&thread={self.thread.pk}', {'withdraw'}),
            ('patient', self.patient_user, f'{reverse("portal:patient-messages")}?thread={self.thread.pk}', {'accept', 'decline'}),
        ):
            with self.subTest(role=role):
                self.login(user)
                response = self.client.get(page)
                parser = DecisionFormsParser(self.respond_url(proposal, role))
                parser.feed(response.content.decode())
                self.assertEqual(parser.decisions, expected)
                if role == 'staff':
                    self.assertContains(response, 'Waiting for Alice to respond.')
                self.assertContains(response, 'SAST')
                self.assertContains(response, self.proposed_start.astimezone(ZoneInfo('Africa/Johannesburg')).strftime('%H:%M'))

    def test_closed_thread_hides_proposal_response_and_withdraw_actions(self):
        proposal = self.make_proposal()
        self.thread.is_closed = True
        self.thread.save(update_fields=['is_closed'])
        for role, user, page in (
            ('staff', self.doctor, reverse('portal:patient-detail', args=[self.patient.pk]) + f'?tab=messages&thread={self.thread.pk}'),
            ('patient', self.patient_user, f'{reverse("portal:patient-messages")}?thread={self.thread.pk}'),
        ):
            with self.subTest(role=role):
                self.login(user)
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                parser = DecisionFormsParser(self.respond_url(proposal, role))
                parser.feed(response.content.decode())
                self.assertEqual(parser.decisions, set())
                thread = next(item for item in response.context['message_threads'] if item.pk == self.thread.pk)
                rendered_proposal = next(item for item in thread.appointment_proposals_list if item.pk == proposal.pk)
                self.assertFalse(rendered_proposal.can_respond)
                self.assertFalse(rendered_proposal.can_withdraw)
