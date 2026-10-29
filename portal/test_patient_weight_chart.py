"""Reference geometry must remain distinct from actual measurements and their dates."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from portal.patient_progress import weight_chart, weight_reference


class PatientWeightChartTests(SimpleTestCase):
    start = date(2026, 1, 1)

    def row(self, days, weight):
        return SimpleNamespace(recorded_on=self.start + timedelta(days=days), weight_kg=Decimal(weight))

    def reference(self, baseline=None, *, end=168):
        return weight_reference(baseline or self.row(0, '100'), {
            'started_on': self.start, 'ends_on': self.start + timedelta(days=end),
        })

    def test_no_check_ins_or_unknown_course_has_no_invented_reference(self):
        self.assertIsNone(weight_chart([], reference=self.reference()))
        self.assertIsNone(weight_reference(None))
        self.assertIsNone(weight_reference(self.row(0, '100')))

    def test_reference_uses_actual_dates_and_starts_only_after_four_weeks(self):
        chart = weight_chart([self.row(0, '100'), self.row(14, '103'), self.row(56, '94')], reference=self.reference())
        band = chart['reference']['rows']
        self.assertEqual(band[0], {'date': self.start + timedelta(days=28), 'lower': 100, 'upper': 100})
        self.assertEqual(band[1], {'date': self.start + timedelta(days=56), 'lower': 96, 'upper': 98})
        self.assertEqual([point['weight'] for point in chart['points']], [100, 103, 94])
        self.assertEqual([point['x'] for point in chart['points']], ['62.00', '219.50', '692.00'])
        self.assertEqual(chart['reference']['starter_x'], '377.00')
        self.assertLess(Decimal(chart['points'][1]['y']), Decimal(chart['points'][0]['y']))

    def test_single_check_in_keeps_a_real_dot_and_finite_reference_inside_course(self):
        row = self.row(0, '100')
        chart = weight_chart([row], reference=self.reference(row, end=42))
        self.assertEqual(chart['count'], 1)
        self.assertEqual(chart['recorded_end'], self.start)
        self.assertEqual(chart['end'], self.start + timedelta(weeks=6))
        self.assertEqual(len(chart['polyline'].split()), 1)
        self.assertEqual(chart['reference']['rows'][-1]['date'], self.start + timedelta(days=42))

    def test_late_initial_check_in_does_not_invent_earlier_weight_or_restart_horizon(self):
        row = self.row(70, '96')
        reference = self.reference(row)
        self.assertEqual(reference['treatment_start'], self.start)
        self.assertEqual(reference['starts_on'], row.recorded_on)
        self.assertEqual(reference['ends_on'], self.start + timedelta(weeks=16))
        self.assertEqual(weight_chart([row], reference=reference)['reference']['rows'][0]['lower'], 96)
        self.assertIsNone(self.reference(self.row(150, '96')))

    def test_reference_does_not_reset_when_earlier_check_ins_are_outside_the_chart(self):
        chart = weight_chart([self.row(56, '95'), self.row(84, '93')], reference=self.reference())
        self.assertEqual(chart['reference']['rows'][0], {
            'date': self.start + timedelta(days=56), 'lower': 96, 'upper': 98,
        })
        self.assertEqual(chart['reference']['rows'][1]['lower'], 92)

    def test_eighty_eight_weeks_of_actuals_do_not_extend_reference_or_flatten_actual_range(self):
        chart = weight_chart([self.row(0, '100'), self.row(616, '96')], reference=self.reference(end=700))
        self.assertEqual(chart['end'], self.start + timedelta(weeks=88))
        self.assertEqual(chart['reference']['rows'][-1], {
            'date': self.start + timedelta(weeks=16), 'lower': 88, 'upper': 94,
        })
        self.assertGreater(Decimal(chart['ticks'][-1]['label']), 80)
        self.assertEqual([point['x'] for point in chart['points']], ['62.00', '692.00'])
        self.assertEqual([point['weight'] for point in chart['points']], [100, 96])

    def test_short_prescription_has_no_reference_beyond_the_starter_period(self):
        self.assertIsNone(self.reference(end=14))
        self.assertIsNone(self.reference(end=28))

    def test_extreme_measurements_stay_within_plot_and_low_import_does_not_draw_negative_reference(self):
        self.assertIsNone(self.reference(self.row(0, '1')))
        chart = weight_chart([self.row(0, '1'), self.row(3500, '999.99')])
        for point in chart['points']:
            self.assertTrue(62 <= Decimal(point['x']) <= 692)
            self.assertTrue(32 <= Decimal(point['y']) <= 212)

    def test_recorded_only_history_chart_keeps_single_point_centered(self):
        chart = weight_chart([self.row(0, '100')])
        self.assertEqual(chart['points'][0]['x'], '377.00')
        self.assertIsNone(chart['reference'])

    def test_old_reference_outside_visible_dates_does_not_change_actual_scale(self):
        rows = [self.row(1000, '90'), self.row(1030, '89')]
        chart = weight_chart(rows, reference=self.reference())
        self.assertEqual(chart['reference']['polygon'], '')
        self.assertEqual(chart['points'], weight_chart(rows)['points'])
        self.assertEqual(chart['ticks'], weight_chart(rows)['ticks'])
