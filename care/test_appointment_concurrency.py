"""Real row-lock races; SQLite cannot verify PostgreSQL locking semantics."""

from datetime import timedelta
from queue import Queue
from threading import Barrier, BrokenBarrierError, Thread
from traceback import format_exc
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .appointment_lifecycle import book_staff_appointment
from .models import Appointment, AuditEvent, PatientEvent


@skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locks')
class AppointmentConcurrencyTests(TransactionTestCase):
    """Only the shared person lock can serialize these different practices."""

    def setUp(self):
        self.alpha = Company.objects.create(name='Race Alpha', slug='race-alpha')
        self.beta = Company.objects.create(name='Race Beta', slug='race-beta')
        users = get_user_model().objects
        self.doctor = users.create_user(email='race-doctor@example.test')
        self.colleague = users.create_user(email='race-colleague@example.test')
        self.alpha_admin = users.create_user(email='race-alpha-admin@example.test')
        self.beta_admin = users.create_user(email='race-beta-admin@example.test')
        self.patient_user = users.create_user(email='race-patient@example.test')
        self.other_patient_user = users.create_user(email='race-other-patient@example.test')
        for company, actor, role in (
            (self.alpha, self.doctor, 'doctor'), (self.beta, self.doctor, 'doctor'),
            (self.beta, self.colleague, 'doctor'),
            (self.alpha, self.alpha_admin, 'practice_admin'),
            (self.beta, self.beta_admin, 'practice_admin'),
        ):
            CompanyMembership.objects.create(company=company, user=actor, role=role)
        self.patient = Patient.objects.create(company=self.alpha, user=self.patient_user,
            first_name='Shared', last_name='Patient')
        self.beta_patient = Patient.objects.create(company=self.beta, user=self.patient_user,
            first_name='Shared', last_name='Patient')
        self.other_patient = Patient.objects.create(company=self.beta, user=self.other_patient_user,
            first_name='Other', last_name='Patient')
        self.starts_at = timezone.now().replace(microsecond=0) + timedelta(days=7)

    def race(self, beta_patient, beta_doctor, expected_error):
        # The main thread releases both fully connected workers together. No
        # fixture or connection is shared by the worker threads.
        gate = Barrier(3, timeout=10)
        results = Queue()
        test_database_name = connection.settings_dict['NAME']
        specifications = (
            (self.alpha.pk, self.patient.pk, self.alpha_admin.pk, self.doctor.pk),
            (self.beta.pk, beta_patient.pk, self.beta_admin.pk, beta_doctor.pk),
        )

        def book(specification):
            worker_connection = connections['default']
            backend_pid = None
            try:
                worker_connection.close()
                with worker_connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute('SELECT pg_backend_pid(), current_database()')
                    backend_pid, database_name = cursor.fetchone()
                if database_name != test_database_name:
                    raise AssertionError('Worker did not connect to the isolated test database.')
                company_id, patient_id, actor_id, doctor_id = specification
                # Fetch independent instances outside the contested transaction.
                values = dict(company=Company.objects.get(pk=company_id),
                    patient=Patient.objects.get(pk=patient_id),
                    actor=get_user_model().objects.get(pk=actor_id),
                    clinician=get_user_model().objects.get(pk=doctor_id))
                gate.wait()
                appointment = book_staff_appointment(**values, starts_at=self.starts_at,
                    duration_minutes=30, appointment_type='initial')
                results.put(('booked', appointment.pk, backend_pid))
            except ValidationError as error:
                results.put(('rejected', ' '.join(error.messages), backend_pid))
            except BaseException:
                # Unexpected connection/timeout/deadlock failures must fail the
                # test, not accidentally count as an expected rejected booking.
                results.put(('unexpected', format_exc(), backend_pid))
            finally:
                worker_connection.close()

        workers = [Thread(target=book, args=(specification,), name=f'appointment-race-{index}', daemon=True)
                   for index, specification in enumerate(specifications)]
        for worker in workers:
            worker.start()
        gate_failure = None
        try:
            gate.wait()
        except BrokenBarrierError:
            gate_failure = 'Both booking workers did not reach the start barrier.'
            gate.abort()
        finally:
            # PostgreSQL timeouts are shorter than this join, so failed locks
            # are cleaned up before TransactionTestCase flushes its database.
            for worker in workers:
                worker.join(timeout=15)
        self.assertFalse(any(worker.is_alive() for worker in workers), 'Booking worker exceeded its bounded timeout.')
        outcomes = [results.get_nowait() for _ in range(results.qsize())]
        self.assertEqual(len(outcomes), 2, outcomes)
        self.assertIsNone(gate_failure, f'{gate_failure}\n{outcomes}')
        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ['booked', 'rejected'], outcomes)
        self.assertEqual(len({outcome[2] for outcome in outcomes}), 2, 'Workers must use distinct PostgreSQL sessions.')
        rejection = next(outcome[1] for outcome in outcomes if outcome[0] == 'rejected')
        self.assertIn(expected_error, rejection)
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='appointment.created').count(), 1)
        self.assertEqual(PatientEvent.objects.filter(title='Appointment booked').count(), 1)
        winner = Appointment.objects.get()
        self.assertEqual(winner.starts_at, self.starts_at)
        self.assertEqual(winner.status, 'booked')

    def test_simultaneous_practices_cannot_book_same_doctor_for_different_patients(self):
        self.race(self.other_patient, self.doctor, 'clinician is no longer available')

    def test_simultaneous_practices_cannot_book_same_patient_with_different_doctors(self):
        self.race(self.beta_patient, self.colleague, 'patient already has an appointment')
