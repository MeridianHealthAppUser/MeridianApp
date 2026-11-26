"""Patient orders under Patient Records & Messages (James, 26 Nov 2026): Subscriptions with each one's weekly timeline
(payments moved to Shipping > Billing), patients' names opening their records, and the same tables for one patient
on the record's Treatment tab."""

import uuid
from datetime import timedelta

from django.urls import reverse

from care.models import PharmacyOrder, PharmacyOrderItem

from .test_billing import BillingPageFixture


class PatientOrdersTests(BillingPageFixture):
    def test_subscriptions_link_to_the_record_and_open_the_weekly_timeline(self):
        self.approved()
        url = reverse('portal:treatment-subscriptions')
        timeline = reverse('portal:subscription-timeline', args=[self.plan.pk])
        self.login(self.super_admin)
        page = self.client.get(url)
        self.assertContains(page, '<th scope="col">Weekly timeline</th>', html=True)
        # As All patients shows them: initials, the name a link to the record, the email below (26 Nov 2026).
        self.assertContains(page, f'<a class="patient-cell__link" href="{reverse("portal:patient-detail", args=[self.patient.pk])}">{self.patient}</a>', html=False)
        self.assertContains(page, f'<small class="patient-cell__email">{self.patient_user.email}</small>', html=False)
        self.assertContains(page, f'href="{timeline}?return_to=/subscriptions/" data-timeline-open', html=False)
        self.assertContains(page, 'data-timeline-dialog')
        # Practice administrators don't open patient records: the name is plain.
        self.login(self.admin)
        page = self.client.get(url)
        self.assertNotContains(page, 'class="patient-cell__link"')
        self.assertContains(page, f'<span class="patient-cell__name">{self.patient}</span>', html=False)
        self.assertContains(page, 'data-timeline-open')

    def test_the_timeline_is_the_patients_weeks_in_the_practices_words(self):
        self.approved()
        url = reverse('portal:subscription-timeline', args=[self.plan.pk])
        self.login(self.admin)
        fragment = self.client.get(url, {'fragment': '1', 'return_to': '/billing/'})
        self.assertTemplateUsed(fragment, 'portal/includes/staff_treatment_timeline.html')
        self.assertTemplateNotUsed(fragment, 'portal/staff_page_base.html')
        self.assertContains(fragment, 'Treatment timeline')
        self.assertContains(fragment, f'{self.patient} · {self.plan.plan_name}')
        self.assertContains(fragment, 'class="patient-timeline__weeks"')
        self.assertContains(fragment, 'This week')
        self.assertNotContains(fragment, 'You are here')
        self.assertNotContains(fragment, 'prescription-changes')
        self.assertContains(fragment, 'href="/billing/" data-dialog-close', html=False)
        # Without scripts: a page with it open. Another site is never somewhere to go back to.
        page = self.client.get(url, {'return_to': 'https://example.test/'})
        self.assertContains(page, '<dialog class="app-dialog app-dialog--wide patient-timeline" id="subscription-timeline" aria-labelledby="subscription-timeline-heading" open>', html=False)
        self.assertContains(page, f'href="{reverse("portal:treatment-subscriptions")}" data-dialog-close', html=False)
        # A doctor opens those of the patients whose records they can open (the record's Treatment tab), with Close
        # back to it; not another doctor's patient's, and never a patient.
        self.login(self.doctor)
        mine = self.client.get(url, {'fragment': '1'})
        self.assertContains(mine, 'class="patient-timeline__weeks"')
        self.assertContains(mine, f'href="{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=treatment" data-dialog-close', html=False)
        from django.contrib.auth import get_user_model
        from practices.models import CompanyMembership

        other = get_user_model().objects.create_user(email='other-doctor@example.test')
        CompanyMembership.objects.create(company=self.company, user=other, role='doctor', clinician_type='doctor')
        for user in (other, self.patient_user):
            self.login(user)
            self.assertIn(self.client.get(url, {'fragment': '1'}).status_code, (302, 403))

    def test_the_record_treatment_tab_has_the_patients_orders(self):
        self.approved()
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status=PharmacyOrder.Status.SUBMITTED)
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.login(self.super_admin)
        page = self.client.get(record, {'tab': 'treatment'})
        self.assertContains(page, 'aria-label="Orders"')
        self.assertContains(page, 'class="is-active" aria-current="page">Subscriptions</a>', html=False)
        self.assertContains(page, '<th scope="col">Weekly timeline</th>', html=True)
        self.assertNotContains(page, '<th scope="col">Patient</th>', html=True)
        # No Plan column (James, 26 Nov 2026); the status in the directory's colours.
        self.assertNotContains(page, '<th scope="col">Plan</th>', html=True)
        self.assertContains(page, f'<span class="console-status console-status--plan-{self.plan.status}">', html=False)
        self.assertContains(page, 'data-timeline-open')
        self.assertContains(page, 'data-timeline-dialog')
        requests = self.client.get(record, {'tab': 'treatment', 'orders': 'requests'})
        self.assertContains(requests, 'class="is-active" aria-current="page">Non-prescriptions</a>', html=False)
        self.assertContains(requests, f'href="{reverse("portal:ops-order-detail", args=[order.pk])}"', html=False)
        self.assertContains(requests, '<th scope="col">Subtotal</th>', html=True)
        self.assertNotContains(requests, '<th scope="col">Patient</th>', html=True)
        self.assertEqual([item.pk for item in requests.context['treatment_requests']], [order.pk])
        # The patient's doctor has the tab too (James, 26 Nov 2026), without Open request: that page is Operations'.
        self.login(self.doctor)
        page = self.client.get(record, {'tab': 'treatment'})
        self.assertContains(page, 'data-timeline-open')
        requests = self.client.get(record, {'tab': 'treatment', 'orders': 'requests'})
        self.assertContains(requests, f'aria-controls="request-items-{order.pk}"', html=False)
        self.assertNotContains(requests, reverse('portal:ops-order-detail', args=[order.pk]))


class NonPrescriptionRowsTests(BillingPageFixture):
    def test_each_row_opens_its_items_and_its_delivery_address(self):
        # James, 26 Nov 2026: the request's number shows its items below it, as its page lists them, and the house
        # button its delivery address in a small dialog.
        order = PharmacyOrder.objects.create(
            company=self.company, patient=self.patient, status=PharmacyOrder.Status.SUBMITTED, subtotal='240.00',
            delivery_address={'line1': '12 Kloof Street', 'city': 'Cape Town', 'province': 'WC', 'postal_code': '8001',
                              'phone': '082 555 0100'})
        PharmacyOrderItem.objects.create(company=self.company, order=order, product=self.dose.pen, quantity=1,
                                         unit_price='240.00', product_name='Creatine monohydrate sachets', strength='30 × 5 g')
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-orders'))
        self.assertContains(page, '<h2>Non-prescriptions</h2>', html=True)
        self.assertNotContains(page, 'Once-off items patients order')
        self.assertContains(page, f'aria-controls="request-items-{order.pk}"', html=False)
        self.assertContains(page, f'#{order.pk}<span class="sr-only">: show its items</span>', html=False)
        self.assertContains(page, f'<tr class="ops-row-details" id="request-items-{order.pk}" hidden>', html=False)
        self.assertContains(page, 'Creatine monohydrate sachets<small>30 × 5 g</small>', html=False)
        self.assertContains(page, '<th scope="col">Subtotal</th>', html=True)
        self.assertNotContains(page, 'Reference subtotal')
        # Payment status before Status, and no Subtotal line under the items (James, 26 Nov 2026).
        self.assertContains(page, '<th scope="col">Payment status</th><th scope="col">Status</th>', html=False)
        self.assertContains(page, '<td data-label="Payment status"><span class="ops-paid ops-paid--unpaid">Unpaid</span></td>', html=False)
        self.assertNotContains(page, 'class="ops-total"')
        self.assertContains(page, f'data-modal-open="request-address-{order.pk}"', html=False)
        self.assertContains(page, f'<dialog class="app-dialog ops-address-dialog" id="request-address-{order.pk}"', html=False)
        self.assertContains(page, '12 Kloof Street<br>')
        self.assertContains(page, '<form class="app-dialog__actions" method="dialog">', html=False)


class SuperAdminRenewalTests(BillingPageFixture):
    def test_the_super_admin_renews_from_the_overview_without_being_a_doctor(self):
        # James, 26 Nov 2026: a Super Admin can always renew; initiating or changing a prescription stays a doctor's.
        from care.models import TreatmentAuthorization

        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.login(self.super_admin)
        page = self.client.get(record)
        self.assertContains(page, 'data-dialog-open="record-renew"', html=False)
        self.assertContains(page, 'id="record-renew"', html=False)
        self.assertNotContains(page, 'data-dialog-open="record-change"', html=False)
        self.assertNotContains(page, 'data-dialog-open="record-initiate"', html=False)
        before = TreatmentAuthorization.objects.filter(patient=self.patient).count()
        response = self.client.post(reverse('portal:patient-prescription-renew', args=[self.patient.pk]), {
            'workflow_context': page.context['prescription_context_token'], 'current': self.auth.pk, 'weeks': '12',
            'acknowledge_flags': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TreatmentAuthorization.objects.filter(patient=self.patient).count(), before + 1)
        # Not a doctor: renewed for the doctor who prescribed it, and the clinical log says who renewed it.
        renewal = TreatmentAuthorization.objects.filter(patient=self.patient).latest('pk')
        self.assertEqual(renewal.prescribed_by, self.doctor)
        self.assertIn(f'Renewed by {self.super_admin.titled_name} (Super Admin).', renewal.log_text)
        # A practice administrator can't.
        self.login(self.admin)
        self.assertIn(self.client.post(reverse('portal:patient-prescription-renew', args=[self.patient.pk]), {
            'current': self.auth.pk, 'weeks': '12'}).status_code, (302, 403))

    def test_the_directory_shows_each_subscription_status_as_a_coloured_pill(self):
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:patient-list'))
        self.assertContains(page, f'<span class="console-status console-status--plan-{self.plan.status}">{self.plan.get_status_display()}</span>', html=False)
        # One practice per deployment: no Practice column (James, 26 Nov 2026), unless the app runs several.
        self.assertContains(page, '<th scope="col">Practice</th>', html=True)
        from django.test import override_settings

        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='billing-practice'):
            single = self.client.get(reverse('portal:patient-list'))
        self.assertEqual(single.status_code, 200)
        self.assertNotContains(single, '<th scope="col">Practice</th>', html=True)
        self.assertContains(single, 'console-status--plan-')


class RevokedPrescriptionTests(BillingPageFixture):
    def test_a_revoked_prescription_shows_as_revoked_and_nothing_more_is_planned(self):
        # James, 26 Nov 2026: after a revoke the Overview shows it as revoked (not an older step), the clinical log
        # says so, the paid weeks stay on the timeline and no more payments are planned.
        from care.models import SubscriptionCycle, TreatmentAuthorization
        from care.prescriptions import current_prescription, revoked_prescription
        from care.subscription_discounts import payment_schedule
        from care.treatment import change_authorization_status

        from .patient_summary import treatment_timeline
        from .record_overview import clinical_log

        self.approved()
        auth = TreatmentAuthorization.objects.get(pk=self.auth.pk)
        self.assertTrue(any(row['projected'] for row in payment_schedule(self.plan)['rows']))
        change_authorization_status(authorization=auth, actor=self.doctor, action='revoke')
        self.assertIsNone(current_prescription(self.company, self.patient))
        revoked = revoked_prescription(self.company, self.patient)
        self.assertEqual((revoked.pk, revoked.revoked_on, revoked.revoked_by), (auth.pk, self.today, self.doctor))
        self.plan.refresh_from_db()
        self.assertFalse(any(row['projected'] for row in payment_schedule(self.plan)['rows']))
        self.assertTrue(clinical_log(self.company, self.patient)[0]['text'].startswith('Prescription revoked: '))
        auth.refresh_from_db()
        weeks = treatment_timeline(self.company, self.patient, auth)['weeks']
        paid_to = SubscriptionCycle.objects.filter(subscription=self.plan, status='paid').latest('ends_on').ends_on
        self.assertGreaterEqual(weeks[-1]['date'] + timedelta(days=6), paid_to - timedelta(days=6))
        self.assertTrue(all(week['dose'] for week in weeks if week['number']))
        self.assertFalse([payment for week in weeks for payment in week['payments'] if payment['state'] == 'planned'])
        # The week it was revoked has a Prescription revoked tile, with the day and who did it.
        revoked_events = [event for week in weeks for event in week['events'] if event['text'] == 'Prescription revoked']
        self.assertEqual([(event['date'], event['by']) for event in revoked_events], [(self.today, self.doctor.titled_name)])
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(page, f'Prescription revoked {self.today:%-d %b}')
        self.assertContains(page, '<dt>Revoked</dt>', html=False)
        # A new prescription supersedes the revoke.
        TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=auth.product, dose=auth.dose,
            max_dose=auth.max_dose, starts_on=self.today, expires_on=self.today + timedelta(weeks=12))
        self.assertIsNone(revoked_prescription(self.company, self.patient))
        self.assertIsNotNone(current_prescription(self.company, self.patient))

    def test_a_revoked_plan_step_ends_the_course_even_if_a_later_step_is_still_active(self):
        # James, 26 Nov 2026 (Ashleigh): revoking a starting plan's first step ends the timeline with the weeks paid
        # for. Revoking now cancels the later steps; one revoked before that still has them active, so the timeline
        # leaves them out too.
        from care.models import SubscriptionCycle, TreatmentAuthorization
        from care.treatment import change_authorization_status

        from .patient_summary import treatment_course, treatment_timeline

        self.approved()
        auth = TreatmentAuthorization.objects.get(pk=self.auth.pk)
        key = uuid.uuid4()
        TreatmentAuthorization.objects.filter(pk=auth.pk).update(plan_key=key, plan_step=1)
        later = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=auth.product, dose=auth.dose,
            max_dose=auth.max_dose, starts_on=auth.expires_on + timedelta(days=1),
            expires_on=auth.expires_on + timedelta(weeks=8), plan_key=key, plan_step=2)
        auth.refresh_from_db()
        change_authorization_status(authorization=auth, actor=self.doctor, action='revoke')
        later.refresh_from_db()
        self.assertEqual(later.status, TreatmentAuthorization.Status.CANCELLED)
        # As it was before revoking cancelled the later steps.
        TreatmentAuthorization.objects.filter(pk=later.pk).update(status=TreatmentAuthorization.Status.ACTIVE)
        auth.refresh_from_db()
        paid_to = SubscriptionCycle.objects.filter(subscription=self.plan, status='paid').latest('ends_on').ends_on
        for start in (auth, later):
            course = treatment_course(start, self.today)
            self.assertEqual([segment['prescription'].pk for segment in course['segments']], [auth.pk])
            self.assertEqual(course['current'].pk, auth.pk)
            self.assertLessEqual(course['ends_on'], max(paid_to, self.today))
        weeks = treatment_timeline(self.company, self.patient, auth)['weeks']
        self.assertLessEqual(weeks[-1]['date'], paid_to)
        self.assertFalse([payment for week in weeks for payment in week['payments'] if payment['state'] == 'planned'])


class RevokedParcelTests(BillingPageFixture):
    def test_a_parcel_paid_for_before_the_revoke_can_still_be_sent(self):
        # James, 26 Nov 2026: it stays held, but the practice may Confirm & lock it (the patient paid for its pens);
        # an unpaid one can't.
        from care.billing import paid_before_revoke
        from care.models import Shipment, TreatmentAuthorization
        from care.operations import shipment_hold_reason
        from care.parcels import schedule_parcels
        from care.treatment import change_authorization_status

        auth = TreatmentAuthorization.objects.get(pk=self.auth.pk)
        schedule_parcels(auth)
        self.approved()
        change_authorization_status(authorization=TreatmentAuthorization.objects.get(pk=auth.pk), actor=self.doctor, action='revoke')
        parcels = list(Shipment.objects.filter(authorization=auth).exclude(status='cancelled').order_by('scheduled_for'))
        paid, unpaid = parcels[0], parcels[-1]
        self.assertEqual((paid.status, unpaid.status), ('held', 'held'))
        self.assertTrue(paid_before_revoke(paid))
        self.assertEqual(shipment_hold_reason(paid), '')
        self.assertFalse(paid_before_revoke(unpaid))
        self.assertNotEqual(shipment_hold_reason(unpaid), '')
        # Its shipment page says so; the unpaid one's doesn't.
        self.login(self.admin)
        self.assertContains(self.client.get(reverse('portal:ops-shipment-detail', args=[paid.pk])), 'Already paid for')
        self.assertNotContains(self.client.get(reverse('portal:ops-shipment-detail', args=[unpaid.pk])), 'Already paid for')

    def test_the_directory_says_record_and_the_record_header_has_the_doctors_title(self):
        self.login(self.super_admin)
        directory = self.client.get(reverse('portal:patient-list'))
        self.assertContains(directory, '<th scope="col">Record</th>', html=True)
        self.assertContains(directory, f'<td data-label="Prescribing doctor">{self.doctor.titled_name}</td>', html=False)
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(record, f'<dt>Prescribing doctor</dt><dd>{self.doctor.titled_name}</dd>', html=False)
