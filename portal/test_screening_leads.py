"""Staff see each lead's screening answers in the wording of the version answered, with flags, and can filter flagged leads."""

from django.urls import reverse

from care.models import Lead, ScreeningFormVersion, ScreeningQuestion, ScreeningQuestionnaire
from care.screening import live_version
from care.test_operations import OperationsFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class ScreeningLeadPageTests(OperationsFixture):
    def setUp(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        self.v1 = live_version(self.company)

    def make_version(self, number, status, surgery_label):
        version = ScreeningFormVersion.objects.create(company=self.company, number=number, status=status)
        ScreeningQuestion.objects.create(
            company=version.company, version=version, key='surgery', tier='custom', answer_type='yes_no',
            label=surgery_label, position=10,
            options=[{'value': 'yes', 'label': 'Yes', 'flag': True, 'reason': 'Surgery <b>x</b>'},
                     {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''}])
        return version

    def lead(self, version, answers, flagged=False, first='Sam'):
        lead = Lead.objects.create(company=self.company, first_name=first, last_name='Lead', email=f'{first}@example.test',
                                   screening_flagged=flagged)
        ScreeningQuestionnaire.objects.create(company=self.company, lead=lead, stage=1, form_version=version, answers=answers)
        return lead

    def page(self, lead):
        response = self.client.get(reverse('portal:lead-detail', args=[lead.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_lead_page_uses_the_answered_versions_wording(self):
        self.v1.status = 'superseded'
        self.v1.save()
        v2 = self.make_version(2, 'superseded', 'Bariatric surgery?')
        lead = self.lead(v2, {'form_version': 2, 'custom': {'surgery': 'no'}})
        v2.status = 'superseded'
        v3 = self.make_version(3, 'published', 'Weight-loss surgery?')
        self.assertEqual(v3.number, 3)
        response = self.page(lead)
        self.assertContains(response, 'Bariatric surgery?')
        self.assertNotContains(response, 'Weight-loss surgery?')
        self.assertContains(response, 'Answered Version 2')

    def test_flagged_answer_is_highlighted_with_reason(self):
        lead = self.lead(self.make_version(2, 'draft', 'Bariatric surgery?'),
                         {'custom': {'surgery': 'yes'}, 'flags': [{'key': 'surgery', 'answer': 'yes', 'reason': 'Needs review'}]},
                         flagged=True)
        self.assertContains(self.page(lead), '<p class="staff-lead-flag">', html=False)
        self.assertContains(self.page(lead), 'Needs review')

    def test_flag_reasons_are_escaped_on_lead_page(self):
        lead = self.lead(self.make_version(2, 'draft', 'Bariatric surgery?'),
                         {'custom': {'surgery': 'yes'}, 'flags': [{'key': 'surgery', 'answer': 'yes', 'reason': '<b>x</b>'}]})
        response = self.page(lead)
        self.assertContains(response, '&lt;b&gt;x&lt;/b&gt;')
        self.assertNotContains(response, '<b>x</b>')

    def test_questionnaire_without_version_uses_version_1(self):
        lead = self.lead(None, {'adult': 'yes', 'pancreatitis': 'unsure'})
        response = self.page(lead)
        self.assertContains(response, 'Have you ever had pancreatitis?')
        self.assertContains(response, 'Not sure')

    def test_leads_list_marks_and_filters_flagged(self):
        self.lead(self.v1, {}, flagged=True, first='Flaggy')
        self.lead(self.v1, {}, flagged=False, first='Calmly')
        url = reverse('portal:staff-lead-history')
        response = self.client.get(url)
        self.assertContains(response, '⚑')
        self.assertContains(response, 'aria-label="Flagged for review"')
        response = self.client.get(url, {'flags': 'flagged'})
        self.assertContains(response, 'Flaggy')
        self.assertNotContains(response, 'Calmly')
