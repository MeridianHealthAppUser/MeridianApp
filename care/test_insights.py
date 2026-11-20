"""Insights and Cohorts (care.insights, portal.insights_views): Dr Czech's measures, Nov 2026."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from care.insights import MONTH_DAYS, cohorts, overview, weight_lost
from care.models import (Appointment, InsightFigure, InsightNote, Lead, MedicationProduct, PatientSubscription,
                         ScreeningQuestionnaire, SubscriptionCycle, TreatmentAuthorization, WeightEntry)
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from practices.models import Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY

TODAY = date(2026, 11, 19)


def at(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class InsightsFixture(AppointmentLifecycleFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sema = MedicationProduct.objects.create(company=cls.company, name='Semaglutide', strength='1 mg', price=Decimal('2310'))
        cls.tirz = MedicationProduct.objects.create(company=cls.company, name='Tirzepatide', strength='5 mg', price=Decimal('3295'))

    def person(self, name):
        return Patient.objects.create(company=self.company, first_name=name, last_name='Insight', assigned_doctor=self.doctor)

    def subscribe(self, patient, product, start, *, paid, amount='2310', cancelled=None, doctor=None, days=28):
        authorization = TreatmentAuthorization.objects.create(
            company=self.company, patient=patient, product=product, prescribed_by=doctor or self.doctor, max_dose='1 mg',
            starts_on=start, expires_on=start + timedelta(days=365))
        subscription = PatientSubscription.objects.create(
            company=self.company, patient=patient, authorization=authorization, plan_name=product.name, starts_on=start,
            status='cancelled' if cancelled else 'active', cancelled_at=at(cancelled) if cancelled else None)
        day = start
        for number in range(1, paid + 1):
            SubscriptionCycle.objects.create(company=self.company, patient=patient, subscription=subscription, cycle_number=number,
                                             starts_on=day, ends_on=day + timedelta(days=days - 1), due_on=day,
                                             amount=Decimal(amount), status='paid')
            day += timedelta(days=days)
        return subscription

    def screened(self, patient, *, weight='100', heard_from=(), submitted=None):
        lead = Lead.objects.create(company=self.company, first_name=patient.first_name, last_name='Insight',
                                   email=f'{patient.first_name.lower()}@insights.test', converted_patient=patient)
        return ScreeningQuestionnaire.objects.create(company=self.company, lead=lead, stage=1, status='submitted',
                                                     answers={'weight_kg': weight, 'heard_from': list(heard_from)},
                                                     submitted_at=at(submitted or TODAY))


class InsightsMeasureTests(InsightsFixture):
    def test_bookings_and_sources_count_the_app_and_the_earlier_figures(self):
        def book(kind, day, **values):
            return Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                              appointment_type=kind, starts_at=at(day), duration_minutes=15, **values)
        book('initial', date(2026, 10, 5), status='completed')
        book('follow_up', date(2026, 10, 12))
        book('initial', date(2026, 10, 14), status='cancelled')  # Not a booking.
        book('review', date(2026, 11, 3))
        InsightFigure.objects.create(company=self.company, month=date(2026, 9, 1), metric='new_bookings', value=40)
        InsightFigure.objects.create(company=self.company, month=date(2026, 9, 1), metric='source_google', value=31)
        self.screened(self.person('Booked'), heard_from=['google', 'facebook'], submitted=date(2026, 10, 2))
        # An enquiry that never booked isn't a source of a booking.
        lead = Lead.objects.create(company=self.company, first_name='Enquiry', last_name='Only', email='enquiry@insights.test')
        ScreeningQuestionnaire.objects.create(company=self.company, lead=lead, stage=1, status='submitted',
                                              answers={'heard_from': ['instagram']}, submitted_at=at(date(2026, 10, 2)))

        data = overview(self.company, today=TODAY)
        self.assertEqual(data['keys'], ['2026-09', '2026-10', '2026-11'])
        self.assertTrue(data['all_time'])
        self.assertEqual(data['bookings'], {'all': [40, 2, 1], 'new': [40, 1, 0], 'follow_up': [0, 1, 1]})
        self.assertEqual(data['sources']['google'], [31, 1, 0])
        self.assertEqual(data['sources']['facebook'], [0, 1, 0])
        self.assertEqual(data['sources']['instagram'], [0, 0, 0])
        # A period narrows every chart to its months.
        narrowed = overview(self.company, start=date(2026, 10, 1), end=date(2026, 10, 31), today=TODAY)
        self.assertEqual((narrowed['keys'], narrowed['bookings']['all']), (['2026-10'], [2]))
        # Only the period's days count, so a period from 10 October leaves out the 5th.
        from_tenth = overview(self.company, start=date(2026, 10, 10), today=TODAY)
        self.assertEqual((from_tenth['keys'], from_tenth['bookings']['all']), (['2026-10', '2026-11'], [1, 1]))
        to_twelfth = overview(self.company, start=date(2026, 10, 1), end=date(2026, 10, 12), today=TODAY)
        self.assertEqual(to_twelfth['bookings']['all'], [2])

    def test_subscriptions_revenue_churn_lifetime_and_doctors(self):
        self.subscribe(self.person('Sema'), self.sema, date(2026, 7, 1), paid=5)
        self.subscribe(self.person('Tirz'), self.tirz, date(2026, 8, 3), paid=2, amount='3295', cancelled=date(2026, 10, 1),
                       doctor=self.colleague)
        data = overview(self.company, start=date(2026, 7, 1), today=TODAY)
        self.assertEqual(data['keys'], ['2026-07', '2026-08', '2026-09', '2026-10', '2026-11'])
        self.assertEqual(data['signups'], {'all': [1, 1, 0, 0, 0], 'semaglutide': [1, 0, 0, 0, 0], 'tirzepatide': [0, 1, 0, 0, 0]})
        # At the end of August both are paid up: each cycle's amount scaled from 28 days to a month.
        self.assertEqual(data['mrr'][1], round(2310 * MONTH_DAYS / 28 + 3295 * MONTH_DAYS / 28))
        # Today only the first is paid up (to 17 Nov).
        self.assertEqual(data['mrr'][-1], round(2310 * MONTH_DAYS / 28))
        self.assertEqual(data['dropouts'], [0, 0, 0, 1, 0])
        self.assertEqual(data['running'][3], 2)
        self.assertEqual(data['churn'][3], 50.0)
        kpis = data['kpis']
        self.assertEqual((kpis['duration'], kpis['duration_count']), (round(56 / MONTH_DAYS, 1), 1))
        self.assertEqual(kpis['ltv'], round(kpis['duration'] * kpis['arpu']))
        self.assertEqual(kpis['active'], 1)
        self.assertCountEqual([doctor['id'] for doctor in data['doctors']], [self.doctor.pk, self.colleague.pk])
        by_doctor = {doctor['id']: position for position, doctor in enumerate(data['doctors'])}
        july = next(row for row in data['doctor_months'] if row['month'] == date(2026, 7, 1))
        self.assertEqual(july['cells'][by_doctor[self.doctor.pk]], {'consults': 0, 'semaglutide': 1, 'tirzepatide': 0})
        august = next(row for row in data['doctor_months'] if row['month'] == date(2026, 8, 1))
        self.assertEqual(august['cells'][by_doctor[self.colleague.pk]], {'consults': 0, 'semaglutide': 0, 'tirzepatide': 1})

    def test_weight_lost_from_the_screening_form_or_the_first_weight_to_the_latest(self):
        screened = self.person('Screened')
        self.screened(screened, weight='100')
        for day, kg in ((date(2026, 9, 1), '97'), (date(2026, 10, 1), '95')):
            WeightEntry.objects.create(company=self.company, patient=screened, recorded_on=day, weight_kg=Decimal(kg))
        logged = self.person('Logged')
        for day, kg in ((date(2026, 9, 1), '90'), (date(2026, 10, 1), '88')):
            WeightEntry.objects.create(company=self.company, patient=logged, recorded_on=day, weight_kg=Decimal(kg))
        once = self.person('Once')  # One weight and no screening form: nothing to compare.
        WeightEntry.objects.create(company=self.company, patient=once, recorded_on=date(2026, 9, 1), weight_kg=Decimal('80'))
        self.assertEqual(weight_lost(self.company), (7.0, 2))

    def test_cohorts_only_measure_months_every_subscriber_has_reached(self):
        self.subscribe(self.person('Stays'), self.sema, date(2026, 7, 1), paid=5)            # Paid to 17 Nov.
        self.subscribe(self.person('Leaves'), self.sema, date(2026, 7, 10), paid=1)          # Paid to 6 Aug.
        self.subscribe(self.person('Tirz'), self.tirz, date(2026, 8, 3), paid=2, amount='3295')  # Paid to 27 Sep.
        data = cohorts(self.company, today=TODAY)
        july = next(row for row in data['rows'] if row['month'] == date(2026, 7, 1))
        august = next(row for row in data['rows'] if row['month'] == date(2026, 8, 1))
        self.assertEqual((july['size'], july['cells'][:5]), (2, [100.0, 50.0, 50.0, 50.0, None]))
        self.assertEqual((august['size'], august['cells'][:4]), (1, [100.0, 100.0, 0.0, None]))
        self.assertEqual(data['years'][0]['values'][1], round(100 * 2 / 3, 1))
        third = next(row for row in data['quarters'] if row['label'] == 'Q3 2026')
        self.assertEqual(third['cells'][:3], [100.0, round(100 * 2 / 3, 1), None])
        self.assertEqual(data['kpis'][1], (round(100 * 2 / 3, 1), 3))
        tirzepatide = cohorts(self.company, medicine='tirzepatide', today=TODAY)
        self.assertEqual([row['size'] for row in tirzepatide['rows']], [1, 0, 0, 0])
        self.assertEqual(cohorts(self.company, medicine='semaglutide', today=TODAY)['size'], 2)

    def test_earlier_figures_load_from_the_spreadsheet_file(self):
        call_command('import_insight_figures', company=self.company.slug, stdout=open('/dev/null', 'w'))
        figures = {(figure.month, figure.metric): figure.value for figure in InsightFigure.objects.filter(company=self.company)}
        self.assertEqual(figures[(date(2026, 4, 1), 'new_bookings')], 103)
        self.assertEqual(figures[(date(2026, 4, 1), 'source_instagram')], 17)
        self.assertEqual(figures[(date(2025, 8, 1), 'source_facebook')], 0)
        self.assertNotIn((date(2025, 7, 1), 'source_google'), figures)  # Sources were kept from August 2025.


class InsightsPageTests(InsightsFixture):
    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_only_the_super_admin_sees_insights_and_cohorts(self):
        self.subscribe(self.person('Sema'), self.sema, date(2026, 7, 1), paid=3)
        for user in (self.admin, self.doctor):
            self.login(user)
            for name in ('metrics', 'insights-cohorts', 'metrics-export'):
                self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403, (user, name))
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:metrics'))
        self.assertContains(page, 'Bookings per month')
        self.assertContains(page, 'id="insight-charts"')
        self.assertContains(page, 'Historical data')
        cohorts_page = self.client.get(reverse('portal:insights-cohorts'), {'medicine': 'semaglutide', 'group': 'quarter'})
        self.assertContains(cohorts_page, 'Cohort heatmap')
        self.assertContains(cohorts_page, 'aria-current="page">Semaglutide')
        export = self.client.get(reverse('portal:metrics-export'), {'start': '2026-07-01'})
        self.assertEqual(export['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('filename="insights-2026-07-01-to-', export['Content-Disposition'])
        from io import BytesIO
        from openpyxl import load_workbook
        sheet = load_workbook(BytesIO(b''.join(export.streaming_content))).active
        self.assertEqual([cell.value for cell in sheet[1][:3]], ['Month', 'All bookings', 'New bookings'])
        self.assertEqual(sheet['A2'].value, '2026-07')
        self.assertContains(self.client.get(reverse('portal:metrics'), {'start': '2026-10-01', 'end': '2026-09-01'}),
                            'The end date must be on or after the start date.')

    def test_the_period_is_a_quick_range_or_custom_dates(self):
        from care.reporting import reporting_today

        today = reporting_today()
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:metrics'))
        self.assertEqual(page.context['period'], {'range': 'all', 'start': None, 'end': None})
        self.assertContains(page, '<option value="all" selected>All time</option>', html=True)
        self.assertContains(page, 'data-date-range')
        self.assertNotContains(page, '>Update<')
        for key, start in (('last-30', today - timedelta(days=29)), ('last-90', today - timedelta(days=89)),
                           ('last-365', today - timedelta(days=364)), ('this-month', today.replace(day=1)),
                           ('year-to-date', date(today.year, 1, 1))):
            # A quick range runs from today, whatever dates come with it.
            page = self.client.get(reverse('portal:metrics'), {'range': key, 'start': '2020-01-01', 'end': '2020-02-01'})
            self.assertEqual(page.context['period'], {'range': key, 'start': start, 'end': None}, key)
            self.assertEqual(page.context['shown'], (start, today), key)
            self.assertContains(page, f'<option value="{key}" selected>', msg_prefix=key)
            self.assertContains(page, f'value="{start:%Y-%m-%d}"', msg_prefix=key)
        page = self.client.get(reverse('portal:metrics'), {'range': 'custom', 'start': '2026-07-01', 'end': '2026-08-15'})
        self.assertEqual(page.context['period'], {'range': 'custom', 'start': date(2026, 7, 1), 'end': date(2026, 8, 15)})
        self.assertContains(page, '1 Jul 2026 – 15 Aug 2026')
        # Dates alone, as older links have, are Custom.
        page = self.client.get(reverse('portal:metrics'), {'start': '2026-07-01'})
        self.assertEqual(page.context['period']['range'], 'custom')
        # Custom without dates is all time; an unknown range is all time.
        for query in ({'range': 'custom'}, {'range': 'fortnight'}):
            self.assertEqual(self.client.get(reverse('portal:metrics'), query).context['period']['range'], 'all', query)

    def test_insights_are_private_uncached_and_read_only(self):
        from care.models import AuditEvent

        for name in ('metrics', 'insights-cohorts', 'metrics-export'):
            response = self.client.get(reverse(f'portal:{name}'))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse('accounts:login'), response['Location'])
        self.client.force_login(self.patient_user)
        for name in ('metrics', 'insights-cohorts', 'metrics-export'):
            self.assertIn(self.client.get(reverse(f'portal:{name}')).status_code, (302, 403), name)
        from care.models import PracticeSettings
        PracticeSettings.objects.update_or_create(company=self.company, defaults={'store_view_log': True})  # Exports are logged.
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:metrics'))
        self.assertIn('no-store', page['Cache-Control'])
        export = self.client.get(reverse('portal:metrics-export'))
        self.assertIn('no-store', export['Cache-Control'])
        self.assertEqual(export['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(AuditEvent.objects.filter(action='metrics.exported').count(), 1)
        self.client.head(reverse('portal:metrics-export'))
        self.assertEqual(AuditEvent.objects.filter(action='metrics.exported').count(), 1)
        for name in ('metrics', 'insights-cohorts', 'metrics-export'):
            self.assertEqual(self.client.post(reverse(f'portal:{name}')).status_code, 405, name)
        # A note or figures need the page's signed form.
        self.assertRedirects(self.client.post(reverse('portal:insight-notes'), {'on': '2026-11-03', 'label': 'Forged'}),
                             reverse('portal:metrics'), fetch_redirect_response=False)
        self.assertFalse(InsightNote.objects.exists())

    def test_notes_and_earlier_figures_are_saved_from_the_page(self):
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:metrics'))
        next_url = reverse('portal:metrics') + '?start=2026-01-01'
        added = self.client.post(reverse('portal:insight-notes'), {
            'workflow_context': page.context['note_context'], 'next': next_url, 'on': '2026-11-03', 'label': 'Decreased price to R1000'})
        self.assertRedirects(added, next_url, fetch_redirect_response=False)
        note = InsightNote.objects.get(company=self.company)
        self.assertEqual((note.on, note.label, note.created_by), (date(2026, 11, 3), 'Decreased price to R1000', self.super_admin))
        page = self.client.get(reverse('portal:metrics'))
        self.assertContains(page, 'Decreased price to R1000')
        self.assertIn({'id': note.pk, 'month': '2026-11', 'on': '2026-11-03', 'label': 'Decreased price to R1000'},
                      page.context['chart_notes'])
        # Elsewhere than Insights isn't somewhere to go back to.
        removed = self.client.post(reverse('portal:insight-note-delete', args=[note.pk]), {
            'workflow_context': page.context['note_context'], 'next': 'https://example.test/'})
        self.assertRedirects(removed, reverse('portal:metrics'), fetch_redirect_response=False)
        self.assertFalse(InsightNote.objects.exists())

        InsightFigure.objects.create(company=self.company, month=date(2026, 1, 1), metric='source_google', value=52)
        saved = self.client.post(reverse('portal:insight-figures'), {
            'workflow_context': page.context['figures_context'],
            'figure-2026_02-new_bookings': '72', 'figure-2026_01-source_google': '', 'figure-2099_01-new_bookings': '9'})
        self.assertRedirects(saved, reverse('portal:metrics'), fetch_redirect_response=False)
        self.assertEqual(list(InsightFigure.objects.filter(company=self.company).values_list('month', 'metric', 'value')),
                         [(date(2026, 2, 1), 'new_bookings', 72)])
        refused = self.client.post(reverse('portal:insight-figures'), {
            'workflow_context': page.context['figures_context'], 'figure-2026_03-new_bookings': 'lots'})
        self.assertRedirects(refused, reverse('portal:metrics'), fetch_redirect_response=False)
        self.assertFalse(InsightFigure.objects.filter(month=date(2026, 3, 1)).exists())
        # Only the Super Admin.
        self.login(self.admin)
        self.assertEqual(self.client.post(reverse('portal:insight-notes'), {'on': '2026-11-03', 'label': 'x'}).status_code, 403)
