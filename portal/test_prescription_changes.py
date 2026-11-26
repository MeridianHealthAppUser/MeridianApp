"""Prescription changes on My Treatment (James, 25 Nov 2026): the doctor's decisions, newest first, opened from the top of
the week-by-week timeline and from each decision in it. The patient menu has no Updates page."""

from datetime import timedelta

from django.urls import reverse

from care.models import MedicationDose
from care.parcels import first_shipping_day
from care.prescriptions import change_prescription

from .patient_summary import prescription_changes, treatment_timeline
from .test_billing import BillingPageFixture


class PrescriptionChangesTests(BillingPageFixture):
    def test_the_doctors_changes_are_listed_with_their_pens_and_payment_days(self):
        from django.utils import timezone

        from care.models import TreatmentAuthorization

        self.approved()  # Paid today; the prescription was written two weeks ago.
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(
            starts_on=self.today - timedelta(weeks=2), created_at=timezone.now() - timedelta(weeks=2))
        self.auth.refresh_from_db()
        first_pens = first_shipping_day(self.today, self.company)
        five_weekly = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295',
                                                    billing_weeks=5, dispatch_weeks=5)
        changed = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                      dose=five_weekly, weeks=16, reason='Private clinical note', acknowledge_flags=True)
        changes = prescription_changes(self.company, self.patient, changed)
        # Oldest first, as on the timeline, each with the week it was made in and the doctor's title.
        self.assertEqual([change['title'] for change in changes], ['Treatment started', 'Dose changed'])
        self.assertEqual({change['by'] for change in changes}, {f'Dr {self.doctor.full_name}'})
        latest = changes[1]
        self.assertEqual(latest['date'], self.today)
        # One line for what changed, then its facts as My Treatment shows them: each day with its timeline week.
        week = lambda day: (day - (first_pens - timedelta(days=first_pens.weekday()))).days // 7 + 1
        next_payment = first_pens + timedelta(weeks=4)  # A week before the second 5-weekly parcel.
        self.assertEqual(latest['change'], 'Semaglutide 1 mg weekly → Semaglutide 2 mg weekly')
        self.assertEqual(latest['facts'], [
            ('First shipment', f'{first_pens:%a, %-d %b}', 1),
            ('Payment schedule', 'Every 5 weeks', None),
            ('Next payment', f'{next_payment:%a, %-d %b}', week(next_payment)),
            ('Prescribed', f'16 weeks, to {first_pens + timedelta(weeks=16):%a, %-d %b %Y}', None),
        ])
        # The first prescription: its first payment (today, week 0) and how long it was written for, until the change.
        first = {label: (value, week) for label, value, week in changes[0]['facts']}
        # Week 0, unless paid on a Monday before its 15:00 cut-off: then the pens go out the next day, in the same week.
        same_week = self.today >= first_pens - timedelta(days=first_pens.weekday())
        self.assertEqual(first['First payment'], (f'{self.today:%a, %-d %b}', 1 if same_week else 0))
        self.assertRegex(first['Prescribed'][0], r'^\d+ weeks$')
        self.assertEqual(latest['key'], f'prescription-{changed.pk}')

        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        content = page.content.decode()
        # The doctor's reason stays on the clinical record.
        self.assertNotContains(page, 'Private clinical note')
        self.assertContains(page, '<dialog class="app-dialog patient-changes" id="prescription-changes"', html=False)
        # Opened from the top of the timeline and from each decision in it; not from the subscription card.
        timeline = content[content.index('id="treatment-timeline"'):content.index('id="prescription-changes"')]
        self.assertIn('class="patient-timeline__tools"', timeline)
        decisions = sum(len(week['events']) for week in treatment_timeline(self.company, self.patient, changed)['weeks'])
        self.assertEqual(content.count('data-modal-open="prescription-changes"'), 1 + decisions)
        self.assertEqual(timeline.count('patient-timeline__chip--link'), decisions)
        card = content[:content.index('id="treatment-timeline"')]
        self.assertNotIn('prescription-changes', card)
        # Without scripts, its link opens it on the page.
        opened = self.client.get(reverse('portal:patient-treatment'), {'changes': 1})
        self.assertContains(opened, 'aria-labelledby="prescription-changes-heading" open>', html=False)
        self.assertNotContains(opened, 'is-selected')
        # Each change has the timeline week it was made in; a decision's chip opens the list at it, outlined.
        events = [event for week in treatment_timeline(self.company, self.patient, changed)['weeks'] for event in week['events']
                  for event['week'] in [week['number']]]
        self.assertEqual([(event['key'], event['week']) for event in events],
                         [(change['key'], change['week']) for change in changes])
        self.assertEqual(events[-1]['by'], f'Dr {self.doctor.full_name}')
        self.assertContains(page, f'data-change="prescription-{changed.pk}"', html=False)
        self.assertContains(page, f'<span class="patient-pill patient-pill--teal patient-pill--plain">Week {latest["week"]}</span><strong>Dose changed</strong>', html=False)
        chosen = self.client.get(reverse('portal:patient-treatment'), {'changes': 1, 'change': f'prescription-{changed.pk}'})
        self.assertContains(chosen, f'class="patient-changes__item is-selected" id="change-prescription-{changed.pk}" aria-current="true" data-dialog-scroll>', html=False)
        self.assertEqual(chosen.content.decode().count('is-selected'), 1)
        self.assertContains(chosen, f'{self.today:%a, %-d %b %Y} · Dr {self.doctor.full_name}')
        # A fact's week is a grey pill beside its day; the card's own week, in its header, is the blue one.
        self.assertContains(chosen, f'<dd>{first_pens:%a, %-d %b} <span class="patient-pill patient-pill--plain">Week 1</span></dd>', html=False)

    def test_week_zero_holds_the_prescription_and_first_payment_and_week_one_the_pens(self):
        from care.models import TreatmentAuthorization

        first_pens = first_shipping_day(self.today, self.company)
        # As the app writes it today: valid from today, its 24 weeks from its first pens.
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=first_pens + timedelta(weeks=24))
        self.auth.refresh_from_db()
        self.approved()
        weeks = treatment_timeline(self.company, self.patient, self.auth)['weeks']
        paid = [('paid', f'paid {self.today:%a, %-d %b}')]
        if self.today >= first_pens - timedelta(days=first_pens.weekday()):
            # Paid on a Monday before its 15:00 cut-off (Dr Czech, 26 Nov 2026): the pens go out the next day, so the
            # prescription and the payment are in week 1 with them, and there is no week 0.
            self.assertEqual((weeks[0]['number'], weeks[0]['dose']), (1, '1 mg'))
            self.assertEqual([event['text'] for event in weeks[0]['events']], ['Prescription started'])
            self.assertEqual([(payment['state'], payment['note']) for payment in weeks[0]['payments']][:1], paid)
            self.assertEqual(len(weeks), 24)
            return
        zero = weeks[0]
        self.assertEqual((zero['number'], zero['dose'], zero['parcels']), (0, None, []))
        self.assertEqual([event['text'] for event in zero['events']], ['Prescription started'])
        self.assertEqual([(payment['state'], payment['note']) for payment in zero['payments']], paid)
        self.assertTrue(zero['current'])
        self.assertEqual((weeks[1]['number'], weeks[1]['dose'], weeks[1]['date']),
                         (1, '1 mg', first_pens - timedelta(days=first_pens.weekday())))
        self.assertEqual(len(weeks), 25)  # Week 0 and the 24 weeks of pens.
