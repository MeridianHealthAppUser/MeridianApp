import json

from django.contrib.auth import get_user_model
from django.urls import reverse

from care.models import Lead, ScreeningFormVersion, ScreeningQuestion
from care.screening import draft_version, live_version
from care.test_billing import BillingFixture
from practices.models import CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class ScreeningBuilderTests(BillingFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.super_admin = get_user_model().objects.create_user(email='screening-super@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.super_admin, role='super_admin')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def number(self):
        return draft_version(self.company).pk

    def start(self):
        self.login(self.super_admin)
        return self.client.post(reverse('portal:screening-draft-start'))

    def test_only_super_admins_reach_the_builder(self):
        self.login(self.super_admin)
        self.client.post(reverse('portal:screening-draft-start'))
        draft = draft_version(self.company)
        key = draft.questions.filter(tier='core').first().key
        gets = [reverse('portal:screening-questions'), reverse('portal:screening-version', args=[1]),
                reverse('portal:screening-draft'), reverse('portal:screening-preview', args=[draft.number])]
        posts = [reverse('portal:screening-draft-start'), reverse('portal:screening-question-add'),
                 reverse('portal:screening-question-save', args=[key]), reverse('portal:screening-question-retire', args=[key]),
                 reverse('portal:screening-reorder'), reverse('portal:screening-publish'), reverse('portal:screening-discard')]
        for user in (self.admin, self.doctor):
            self.login(user)
            for url in gets:
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
            for url in posts:
                with self.subTest(user=user.email, url=url, method='post'):
                    self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(draft_version(self.company).pk, draft.pk)
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:screening-questions'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Version 1')
        self.assertContains(page, 'Set up from the existing form')

    def test_settings_tab_lists_screening_questions(self):
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:screening-questions'))
        self.assertContains(page, f'href="{reverse("portal:screening-questions")}"')
        self.assertContains(page, 'Screening questions')
        self.assertContains(page, 'aria-label="Settings sections"')

    def test_editor_flow_start_add_save_publish(self):
        response = self.start()
        self.assertRedirects(response, reverse('portal:screening-draft'), fetch_redirect_response=False)
        add = self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        key = draft_version(self.company).questions.get(tier='custom').key
        self.assertRedirects(add, f'{reverse("portal:screening-draft")}?q={key}', fetch_redirect_response=False)
        options = [{'value': 'yes', 'label': 'Yes', 'flag': True, 'reason': 'Needs a doctor to look'},
                   {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''}]
        saved = self.client.post(reverse('portal:screening-question-save', args=[key]), {
            'draft_id': self.number(), 'answer_type': 'yes_no', 'label': 'Surgery?', 'options': json.dumps(options)}, HTTP_ACCEPT='application/json')
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['ok'])
        self.assertIn('Surgery?', saved.json()['row_html'])
        published = self.client.post(reverse('portal:screening-publish'), {'draft_id': self.number()})
        self.assertRedirects(published, reverse('portal:screening-questions'), fetch_redirect_response=False)
        self.assertEqual(live_version(self.company).number, 2)
        page = self.client.get(reverse('portal:screening-questions'))
        self.assertContains(page, 'Version 2')
        row = page.content.decode().split('Version 2</td>', 1)[1].split('</tr>', 1)[0]
        self.assertIn('data-label="Status">Published<', row)

    def test_save_without_json_redirects_back_to_the_editor(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        key = draft_version(self.company).questions.get(tier='custom').key
        response = self.client.post(reverse('portal:screening-question-save', args=[key]), {
            'draft_id': self.number(), 'answer_type': 'yes_no', 'label': 'Plain post', 'options': '[]'})
        self.assertRedirects(response, f'{reverse("portal:screening-draft")}?q={key}', fetch_redirect_response=False)
        self.assertEqual(ScreeningQuestion.objects.get(version=draft_version(self.company), key=key).label, 'Plain post')

    def test_save_with_errors_returns_400_json(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        key = draft_version(self.company).questions.get(tier='custom').key
        response = self.client.post(reverse('portal:screening-question-save', args=[key]), {
            'draft_id': self.number(), 'answer_type': 'single', 'label': 'Pick', 'options': '[]'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])
        self.assertIn('options', response.json()['errors'])

    def test_question_lookup_only_uses_the_current_draft(self):
        self.login(self.super_admin)
        live_key = live_version(self.company).questions.filter(tier='core').first().key
        # No draft: 404, never a lookup in the live version.
        self.assertEqual(self.client.post(reverse('portal:screening-question-save', args=[live_key]), {
            'label': 'Hacked', 'draft_id': 0}).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal:screening-draft')).status_code, 404)
        self.assertNotEqual(live_version(self.company).questions.get(key=live_key).label, 'Hacked')

    def test_reorder_and_stale_responses(self):
        self.start()
        draft = draft_version(self.company)
        from care.screening import movable_questions
        keys = [q.key for q in movable_questions(draft)]
        ok = self.client.post(reverse('portal:screening-reorder'), json.dumps({'keys': keys[::-1], 'draft_id': draft.pk}), content_type='application/json')
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()['ok'])
        bad = self.client.post(reverse('portal:screening-reorder'), json.dumps({'keys': keys[:1], 'draft_id': draft.pk}), content_type='application/json')
        self.assertEqual(bad.status_code, 409)
        self.assertIn('__all__', bad.json()['errors'])

    def test_publish_error_redirects_back_with_a_message(self):
        self.login(self.super_admin)
        response = self.client.post(reverse('portal:screening-publish'), {'draft_id': 0}, follow=True)
        self.assertRedirects(response, reverse('portal:screening-questions'))
        self.assertContains(response, 'This draft was published or discarded')

    def test_retire_discard_and_read_only_version(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        key = draft_version(self.company).questions.get(tier='custom').key
        self.assertRedirects(self.client.post(reverse('portal:screening-question-retire', args=[key]), {'draft_id': self.number()}),
                             reverse('portal:screening-draft'), fetch_redirect_response=False)
        self.assertFalse(draft_version(self.company).questions.filter(key=key).exists())
        self.assertRedirects(self.client.post(reverse('portal:screening-discard'), {'draft_id': self.number()}), reverse('portal:screening-questions'),
                             fetch_redirect_response=False)
        self.assertIsNone(draft_version(self.company))
        page = self.client.get(reverse('portal:screening-version', args=[1]))
        self.assertContains(page, 'Version 1')
        self.assertNotContains(page, 'data-screening-panel')
        self.assertEqual(self.client.get(reverse('portal:screening-version', args=[99])).status_code, 404)

    def test_preview_is_get_only_same_origin_and_creates_no_lead(self):
        self.start()
        draft = draft_version(self.company)
        before = Lead.objects.count()
        url = reverse('portal:screening-preview', args=[draft.number])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page['X-Frame-Options'], 'SAMEORIGIN')
        self.assertContains(page, 'Preview, not submitted')
        self.assertRegex(page.content.decode(), r'<button class="questionnaire-button" type="submit" disabled')
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(Lead.objects.count(), before)

    def test_editor_markup_hooks(self):
        self.start()
        page = self.client.get(reverse('portal:screening-draft'))
        draft = draft_version(self.company)
        self.assertContains(page, 'data-screening-list')
        self.assertContains(page, f'data-reorder-url="{reverse("portal:screening-reorder")}"')
        self.assertContains(page, 'data-question-key="adult"')
        self.assertContains(page, 'data-move="up"')
        self.assertContains(page, 'data-screening-panel="adult"')
        self.assertContains(page, f'src="{reverse("portal:screening-preview", args=[draft.number])}"')
        self.assertContains(page, 'class="screening-preview"')
        self.assertContains(page, 'title="Patient preview"')
        self.assertContains(page, 'data-preview-open')
        self.assertContains(page, 'js/screening_builder.js?v=20261127a')
        self.assertContains(page, 'css/screening_builder.css?v=20261127a')
        self.assertContains(page, 'data-preview-slot')
        self.assertContains(page, 'id="screening-preview-dialog"')
        for text in ('The app needs these', 'Used for BMI', 'Wording comes from Policy versions',
                     'The eligibility checks and the medical profile use this answer', 'Core · reword only'):
            self.assertContains(page, text)

    def test_every_question_renders_the_remaining_script_hooks(self):
        from portal.screening_views import FIXED_CORE_KEYS
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        draft = draft_version(self.company)
        page = self.client.get(reverse('portal:screening-draft'))
        html = page.content.decode()
        questions = list(draft.questions.all())
        custom = [q for q in questions if q.tier != ScreeningQuestion.Tier.CORE]
        self.assertTrue(custom)
        self.assertContains(page, 'data-screening-rows')
        self.assertContains(page, f'data-draft-id="{draft.pk}"')
        for question in questions:
            with self.subTest(key=question.key):
                self.assertEqual(html.count(f'data-panel-for="{question.key}"'), 1)
                self.assertEqual(html.count(f'data-screening-panel="{question.key}"'), 1)
                row_attr = 'data-fixed-key' if question.key in FIXED_CORE_KEYS else 'data-question-key'
                self.assertEqual(html.count(f'{row_attr}="{question.key}"'), 1)
        self.assertEqual(html.count('data-panel-errors'), len(questions))
        self.assertEqual(html.count('data-screening-options'), len(custom))
        self.assertEqual(html.count('name="options"'), len(custom))
        self.assertEqual(html.count('screening-number'), len(custom))

    def test_custom_panel_shows_flag_hint(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        page = self.client.get(reverse('portal:screening-draft'))
        self.assertContains(page, 'The patient sees this reason.')
        self.assertContains(page, 'Flag for review')

    def test_publish_dialog_text(self):
        self.start()
        page = self.client.get(reverse('portal:screening-draft'))
        self.assertContains(page, 'New enquiries on the website and your WordPress page get these questions from now on. '
                                  'Leads that already answered keep Version 1.')
        self.assertContains(page, 'Version 2')

    def test_read_only_version_page_has_no_editing_hooks(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        self.client.post(reverse('portal:screening-publish'), {'draft_id': self.number()})
        self.client.post(reverse('portal:screening-draft-start'))
        page = self.client.get(reverse('portal:screening-version', args=[2])).content.decode()
        self.assertIn('c_new-question', page)
        for hook in ('data-move', 'data-reorder-url', 'data-screening-list', '/retire/', 'data-screening-edit'):
            self.assertNotIn(hook, page)

    def test_stale_editor_tabs_are_refused_and_change_nothing(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        draft = draft_version(self.company)
        key = draft.questions.get(tier='custom').key
        from care.screening import movable_questions
        keys = [q.key for q in movable_questions(draft)]
        old = draft.pk - 1
        save = self.client.post(reverse('portal:screening-question-save', args=[key]), {
            'draft_id': old, 'answer_type': 'yes_no', 'label': 'Stale', 'options': '[]'}, HTTP_ACCEPT='application/json')
        self.assertEqual(save.status_code, 409)
        self.assertEqual(draft.questions.get(key=key).label, 'New question')
        pub = self.client.post(reverse('portal:screening-publish'), {'draft_id': old}, follow=True)
        self.assertContains(pub, 'This draft was published or discarded')
        self.assertEqual(draft_version(self.company).pk, draft.pk)
        self.assertEqual(live_version(self.company).number, 1)
        order = self.client.post(reverse('portal:screening-reorder'), json.dumps({'keys': keys[::-1], 'draft_id': old}),
                                 content_type='application/json')
        self.assertEqual(order.status_code, 409)
        self.assertEqual([q.key for q in movable_questions(draft)], keys)
        self.assertEqual(self.client.post(reverse('portal:screening-discard'), {'draft_id': old}).status_code, 302)
        self.assertIsNotNone(draft_version(self.company))

    def test_option_editor_hooks(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        page = self.client.get(reverse('portal:screening-draft'))
        for hook in ('data-option-row', 'data-option-remove', 'data-option-add', 'data-option-template', 'data-option-value',
                     'data-option-label', 'data-option-flag', 'data-option-reason', '+ Add answer', '>Remove<'):
            self.assertContains(page, hook)

    def test_discarded_drafts_pk_is_refused_even_though_the_number_is_reused(self):
        self.start()
        self.client.post(reverse('portal:screening-question-add'), {'draft_id': self.number()})
        old = draft_version(self.company)
        key = old.questions.get(tier='custom').key
        old_pk, old_number = old.pk, old.number
        self.client.post(reverse('portal:screening-discard'), {'draft_id': old_pk})
        self.client.post(reverse('portal:screening-draft-start'))
        new = draft_version(self.company)
        self.assertEqual(new.number, old_number)
        self.assertNotEqual(new.pk, old_pk)
        new_key = lambda: [q.key for q in new.questions.all()]
        before = new_key()
        for url, data in ((reverse('portal:screening-question-save', args=['adult']), {'draft_id': old_pk, 'label': 'Stale'}),
                          (reverse('portal:screening-publish'), {'draft_id': old_pk}),
                          (reverse('portal:screening-question-add'), {'draft_id': old_pk}),
                          (reverse('portal:screening-publish'), {})):
            with self.subTest(url=url):
                self.client.post(url, data, HTTP_ACCEPT='application/json')
        self.assertEqual(draft_version(self.company).pk, new.pk)
        self.assertEqual(live_version(self.company).number, 1)
        self.assertEqual(new_key(), before)
        self.assertNotEqual(new.questions.get(key='adult').label, 'Stale')
