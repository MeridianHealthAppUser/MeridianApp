"""Date-and-time and time pickers only allow quarter hours: XX:00, XX:15, XX:30 and XX:45."""

import re
from datetime import datetime, time

from django.test import SimpleTestCase

from care.forms import AppointmentProposalForm, ClinicalTaskForm
from care.widgets import (
    QuarterHourDateTimeField, QuarterHourDateTimeWidget, QuarterHourTimeField, QuarterHourTimeWidget,
    floor_quarter_hour, on_quarter_hour,
)
from portal.availability_forms import SpecialDayForm, WorkingDayForm
from portal.clinical_forms import ConsultationForm
from portal.task_forms import TaskEditorForm

QUARTER_HOURS = [f'{hour:02d}:{minute:02d}' for hour in range(24) for minute in (0, 15, 30, 45)]


def options(html, name):
    select = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', html, re.S).group(1)
    return re.findall(r'<option value="([^"]*)"', select)


class QuarterHourPickerTests(SimpleTestCase):
    def test_a_date_then_a_time_list_of_quarter_hours(self):
        html = QuarterHourDateTimeWidget().render('when', datetime(2026, 10, 16, 10, 45))
        self.assertIn('<input type="date" name="when_0" value="2026-10-16"', html)
        self.assertEqual(options(html, 'when_1'), ['', *QUARTER_HOURS])
        self.assertIn('<option value="10:45" selected>10:45</option>', html)
        html = QuarterHourTimeWidget().render('from', time(8, 30))
        self.assertNotIn('type="date"', html)
        self.assertEqual(options(html, 'from'), ['', *QUARTER_HOURS])
        self.assertIn('<option value="08:30" selected>08:30</option>', html)

    def test_the_two_boxes_are_sent_as_one_time_and_any_other_minute_is_refused(self):
        field = QuarterHourDateTimeField()
        value = field.widget.value_from_datadict({'when_0': '2030-10-16', 'when_1': '10:45'}, {}, 'when')
        self.assertEqual(field.clean(value).strftime('%Y-%m-%d %H:%M'), '2030-10-16 10:45')
        for sent in ({'when_0': '2030-10-16', 'when_1': '10:47'}, {'when': '2030-10-16T10:47'}):
            with self.subTest(sent=sent), self.assertRaisesMessage(Exception, 'Choose a time on the quarter hour'):
                field.clean(field.widget.value_from_datadict(sent, {}, 'when'))
        time_field = QuarterHourTimeField()
        self.assertEqual(time_field.clean(time_field.widget.value_from_datadict({'at': '08:15'}, {}, 'at')), time(8, 15))
        with self.assertRaisesMessage(Exception, 'Choose a time on the quarter hour'):
            time_field.clean('08:10')

    def test_a_time_saved_off_the_quarter_hour_shows_at_the_quarter_hour_before_it(self):
        html = QuarterHourDateTimeWidget().render('when', datetime(2026, 10, 4, 21, 39))
        self.assertIn('<option value="21:30" selected>21:30</option>', html)
        self.assertEqual(floor_quarter_hour(datetime(2026, 10, 10, 11, 52, 30)), datetime(2026, 10, 10, 11, 45))
        for value, expected in (('2026-10-16T10:45', True), ('2026-10-16 10:47', False), ('10:30:00', True),
                                ('10:30:20', False), ('', True), (None, True), (time(9, 0, 5), False)):
            with self.subTest(value=value):
                self.assertEqual(on_quarter_hour(value), expected)

    def test_every_date_and_time_picker_on_the_platform_uses_quarter_hours(self):
        fields = (
            (ClinicalTaskForm, 'due_at'), (TaskEditorForm, 'due_at'), (AppointmentProposalForm, 'proposed_starts_at'),
            (ConsultationForm, 'occurred_at'), (WorkingDayForm, 'starts_at'), (WorkingDayForm, 'ends_at'),
            (SpecialDayForm, 'starts_at'), (SpecialDayForm, 'ends_at'),
        )
        for form, name in fields:
            with self.subTest(form=form.__name__, field=name):
                self.assertIsInstance(form.base_fields[name], (QuarterHourDateTimeField, QuarterHourTimeField))
