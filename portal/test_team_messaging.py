"""Staff message colleagues; only members see a team conversation and patients never do."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, ClinicianAssignment, TeamMessage, TeamThread, TeamThreadMember
from care.team_messaging import add_team_member, manage_team_conversation, post_team_message, start_team_conversation, team_threads
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class TeamMessagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Team Practice', slug='team-practice')
        cls.other_company = Company.objects.create(name='Other Team Practice', slug='other-team-practice')
        cls.doctor = User.objects.create_user(email='doctor@team.test', first_name='Dora', last_name='Doctor')
        cls.dietitian = User.objects.create_user(email='dietitian@team.test', first_name='Dee', last_name='Dietitian')
        cls.admin = User.objects.create_user(email='admin@team.test', first_name='Ada', last_name='Admin')
        cls.outsider = User.objects.create_user(email='outsider@team.test', first_name='Otto', last_name='Outsider')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.dietitian, role='doctor', clinician_type='dietitian')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='practice_admin')
        CompanyMembership.objects.create(company=cls.other_company, user=cls.outsider, role='doctor')
        cls.patient_user = User.objects.create_user(email='patient@team.test')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Pat', last_name='Patient', assigned_doctor=cls.doctor)

    def login(self, user, company=None):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def start(self, **overrides):
        values = dict(company=self.company, actor=self.doctor, members=[self.admin], subject='Delivery for Pat',
                      body='Has the parcel gone out?', patient=self.patient)
        values.update(overrides)
        return start_team_conversation(**values)

    def test_any_staff_member_can_start_a_conversation_with_colleagues(self):
        self.login(self.admin)
        response = self.client.post(reverse('portal:team-thread-create'), {
            'members': [self.doctor.pk, self.dietitian.pk], 'subject': 'Rota', 'body': 'Can someone cover Friday?'})
        thread = TeamThread.objects.get()
        self.assertRedirects(response, f'{reverse("portal:team-inbox")}?thread={thread.pk}', fetch_redirect_response=False)
        self.assertEqual(set(thread.members.all()), {self.admin, self.doctor, self.dietitian})
        self.assertIsNone(thread.patient)
        self.assertEqual(thread.messages.get().sender, self.admin)

    def test_the_list_matches_patient_messages_and_marks_read_or_unread(self):
        thread = self.start()
        self.login(self.admin)
        page = self.client.get(reverse('portal:team-inbox'))
        # Subject, the others in it as tags by role, the patient as a tag on its own line, then the latest message;
        # nothing opens until chosen.
        self.assertContains(page, '<span class="record-inbox-thread__people person-tags"><span class="sr-only">With </span><span class="person-tag person-tag--doctor" title="Dora Doctor · Doctor">')
        self.assertContains(page, 'D. Doctor<span class="sr-only"> (Doctor)</span></span></span>')
        self.assertContains(page, '<span class="record-inbox-thread__about"><span class="person-tags__label">Patient:</span><span class="person-tag person-tag--patient" title="Pat Patient · Patient">')
        self.assertContains(page, 'Pat Patient<span class="sr-only"> (Patient)</span></span></span>')
        # The open conversation shows the same tags, the patient under the others.
        opened = self.client.get(f"{reverse('portal:team-inbox')}?thread={thread.pk}")
        html = opened.content.decode()
        self.assertLess(html.index('<p class="record-messages__people record-messages__tags person-tags">'),
                        html.index('<p class="record-messages__about"><span class="person-tags__label">Patient:</span><span class="person-tag person-tag--patient"'))
        self.assertContains(page, '<span class="record-inbox-thread__preview">Has the parcel gone out?</span>', html=True)
        self.assertContains(page, 'Choose a team conversation')
        url = reverse('portal:team-thread-read-state', args=[thread.pk])
        self.assertContains(page, f'action="{url}"')
        self.client.post(url, {'state': 'read', 'page': '1'})
        self.assertEqual(team_threads(self.company, self.admin).get().unread_count, 0)
        response = self.client.post(url, {'state': 'unread', 'page': '1'})
        self.assertRedirects(response, reverse('portal:team-inbox'), fetch_redirect_response=False)
        self.assertEqual(team_threads(self.company, self.admin).get().unread_count, 1)
        # Your own messages are never unread for you, so the sender has nothing to mark.
        self.login(self.doctor)
        self.client.post(url, {'state': 'unread'})
        self.assertEqual(team_threads(self.company, self.doctor).get().unread_count, 0)
        self.assertNotContains(self.client.get(reverse('portal:team-inbox')), f'action="{url}"')
        self.assertContains(self.client.get(reverse('portal:team-inbox')), '<strong>You:</strong> Has the parcel gone out?')

    def test_search_finds_messages_colleagues_and_patients_in_your_conversations(self):
        about_pat = self.start()
        rota = self.start(members=[self.dietitian], subject='Rota', body='Can you cover Friday?', patient=None)
        not_yours = start_team_conversation(company=self.company, actor=self.admin, members=[self.dietitian],
                                            subject='Parcel rota', body='Parcel question', patient=None)
        self.login(self.doctor)

        def found(search):
            return {thread.pk for thread in self.client.get(reverse('portal:team-inbox'), {'q': search}).context['threads']}

        self.assertEqual(found('parcel'), {about_pat.pk})  # message text; never a conversation you are not in
        self.assertEqual(found('dietitian'), {rota.pk})  # a colleague in it
        self.assertEqual(found('pat patient'), {about_pat.pk})  # the patient it is about
        self.assertEqual(found('rota friday'), {rota.pk})  # every word has to match
        self.assertNotIn(not_yours.pk, found('rota'))
        response = self.client.get(reverse('portal:team-inbox'), {'q': 'parcel'})
        self.assertContains(response, f'href="{reverse("portal:team-inbox")}?q=parcel&amp;thread={about_pat.pk}"')
        self.assertContains(self.client.get(reverse('portal:team-inbox'), {'q': 'nothing here'}), 'No conversations match “nothing here”.')
        # With nothing found, the page still waits for a choice rather than starting a new conversation.
        self.assertFalse(self.client.get(reverse('portal:team-inbox'), {'q': 'nothing here'}).context['composing'])

    def test_only_members_see_or_write_and_other_practices_cannot_be_added(self):
        thread = self.start()
        self.login(self.dietitian)
        self.assertEqual(self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk}).status_code, 404)
        self.assertEqual(self.client.post(reverse('portal:team-message-create', args=[thread.pk]), {'body': 'Hi'}).status_code, 404)
        with self.assertRaises(PermissionDenied):
            post_team_message(thread=thread, sender=self.dietitian, body='Not a member')
        with self.assertRaises(ValidationError):
            self.start(members=[self.outsider])
        with self.assertRaises(ValidationError):
            self.start(members=[])
        self.assertEqual(TeamMessage.objects.count(), 1)

    def manage(self, thread, **data):
        url = reverse('portal:team-conversation-manage', args=[thread.pk])
        token = self.client.get(url).context['workflow_context']
        return self.client.post(url, {'workflow_context': token, 'confirm': 'on', **data})

    def test_message_action_adds_a_colleague_who_then_sees_the_whole_conversation(self):
        thread = self.start()
        # Message action sits with the conversation's other actions at the top, as on a patient conversation.
        self.login(self.admin)
        manage_url = reverse('portal:team-conversation-manage', args=[thread.pk])
        html = self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk}).content.decode()
        tools = html[html.index('class="record-messages__tools"'):html.index('record-messages__people')]
        self.assertIn(f'record-messages__tool" href="{manage_url}" data-modal-open="team-conversation-manage" aria-haspopup="dialog">Message action</a>', tools)
        # The modal has the same form as the action's own page (the link without scripts), posting to it.
        self.assertIn(f'<form class="console-form" method="post" action="{manage_url}" data-conversation-manage>', html)
        self.assertIn('<div data-shows-for-action="add"><div class="console-form__field"><label for="team_manage_user">Colleague to add</label>', html)
        page = self.client.get(manage_url)
        # "Colleague to add" only shows while adding a colleague is the chosen action (the script hides it otherwise).
        self.assertContains(page, '<div data-shows-for-action="add"><div class="console-form__field"><label for="id_user">Colleague to add</label>')
        self.assertContains(page, 'js/conversation_management.js')
        self.assertEqual(self.manage(thread, action='add').status_code, 400)  # nobody chosen
        response = self.manage(thread, action='add', user=self.dietitian.pk)
        self.assertRedirects(response, f"{reverse('portal:team-inbox')}?thread={thread.pk}", fetch_redirect_response=False)
        self.assertEqual(TeamThreadMember.objects.get(thread=thread, user=self.dietitian).added_by, self.admin)
        self.login(self.dietitian)
        response = self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk})
        self.assertContains(response, 'Has the parcel gone out?')
        self.assertContains(response, 'Ada Admin added Dee Dietitian')
        with self.assertRaises(ValidationError):
            add_team_member(thread=thread, actor=self.dietitian, user=self.dietitian)
        # Only people in the conversation can manage it.
        self.login(self.outsider, self.other_company)
        self.assertEqual(self.client.get(manage_url).status_code, 404)

    def test_any_member_can_close_and_reopen_and_a_closed_conversation_takes_no_replies(self):
        thread = self.start()
        self.login(self.admin)
        response = self.manage(thread, action='close')
        self.assertRedirects(response, f"{reverse('portal:team-inbox')}?thread={thread.pk}", fetch_redirect_response=False)
        thread.refresh_from_db()
        self.assertTrue(thread.is_closed)
        self.assertEqual(AuditEvent.objects.filter(action='team_thread.closed').count(), 1)
        page = self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk})
        self.assertContains(page, 'This conversation is closed. Reopen it under Message action to reply')
        self.assertNotContains(page, 'data-record-reply')
        self.assertContains(page, '<span class="record-inbox-pill">Closed</span>')
        self.assertEqual(self.client.post(reverse('portal:team-message-create', args=[thread.pk]), {'body': 'Still there?'}).status_code, 400)
        with self.assertRaises(ValidationError):
            post_team_message(thread=thread, sender=self.doctor, body='Still there?')
        with self.assertRaises(ValidationError):
            add_team_member(thread=thread, actor=self.doctor, user=self.dietitian)
        self.assertEqual(TeamMessage.objects.filter(thread=thread).count(), 1)
        # Someone else in it reopens it, and replies work again.
        self.login(self.doctor)
        self.manage(thread, action='reopen')
        thread.refresh_from_db()
        self.assertFalse(thread.is_closed)
        post_team_message(thread=thread, sender=self.doctor, body='Back open')
        self.assertEqual(AuditEvent.objects.filter(action='team_thread.reopened').count(), 1)
        with self.assertRaises(ValidationError):
            manage_team_conversation(thread=thread, actor=self.doctor, action='archive')

    def test_open_and_closed_filters(self):
        open_thread = self.start(subject='Still going')
        closed_thread = self.start(subject='All done')
        manage_team_conversation(thread=closed_thread, actor=self.doctor, action='close')
        self.login(self.doctor)
        inbox = reverse('portal:team-inbox')
        for status, expected in (([], {open_thread.pk, closed_thread.pk}), (['open'], {open_thread.pk}),
                                 (['closed'], {closed_thread.pk}), (['open', 'closed'], {open_thread.pk, closed_thread.pk}),
                                 (['all'], {open_thread.pk, closed_thread.pk})):
            with self.subTest(status=status):
                response = self.client.get(inbox, {'status': status})
                self.assertEqual({thread.pk for thread in response.context['threads']}, expected)
        # The filter is kept when a conversation is opened, replied to or marked unread.
        response = self.client.get(inbox, {'status': 'open'})
        self.assertContains(response, '<input type="checkbox" name="status" value="open" checked>')
        self.assertContains(response, f'href="{inbox}?status=open&amp;thread={open_thread.pk}"')
        reply = self.client.post(reverse('portal:team-message-create', args=[open_thread.pk]), {'body': 'More', 'status': 'open'})
        self.assertRedirects(reply, f'{inbox}?status=open&thread={open_thread.pk}', fetch_redirect_response=False)
        unread = self.client.post(reverse('portal:team-thread-read-state', args=[open_thread.pk]), {'state': 'unread', 'status': 'closed'})
        self.assertRedirects(unread, f'{inbox}?status=closed', fetch_redirect_response=False)

    def test_filters_menu_has_status_and_patient_sections_beside_the_search(self):
        about_pat = self.start(subject='About Pat')
        general = self.start(subject='Rota', patient=None)
        closed_general = self.start(subject='Old rota', patient=None)
        manage_team_conversation(thread=closed_general, actor=self.doctor, action='close')
        self.login(self.doctor)
        inbox = reverse('portal:team-inbox')

        def listed(**query):
            return {thread.pk for thread in self.client.get(inbox, query).context['threads']}

        self.assertEqual(listed(about='patient'), {about_pat.pk})
        self.assertEqual(listed(about='general'), {general.pk, closed_general.pk})
        self.assertEqual(listed(about='general', status='open'), {general.pk})
        self.assertEqual(listed(about=['patient', 'general']), {about_pat.pk, general.pk, closed_general.pk})
        self.assertEqual(listed(about='nonsense'), {about_pat.pk, general.pk, closed_general.pk})
        response = self.client.get(inbox, {'status': 'open', 'about': 'general', 'q': 'rota'})
        html = response.content.decode()
        # The Filters menu comes just before the search, with no row of filter buttons.
        self.assertLess(html.index('<details class="filter-menu filter-menu--compact" data-filter-menu>'), html.index('data-conversation-search'))
        self.assertNotContains(response, 'class="inbox-filters"')
        for label in ('Status', 'Patient'):
            self.assertContains(response, f'<span class="filter-menu__label">{label}</span>')
        self.assertContains(response, '<input type="checkbox" name="about" value="general" checked><span>No patient</span>')
        self.assertContains(response, '<input type="checkbox" name="about" value="patient"><span>With patient</span>')
        # Apply keeps the search; Clear all keeps it too.
        self.assertContains(response, '<input type="hidden" name="q" value="rota">')
        self.assertContains(response, f'<a class="filter-menu__clear" href="{inbox}?q=rota">Clear all</a>')
        # Opening, replying to or marking a conversation keeps both filters.
        self.assertContains(response, f'href="{inbox}?status=open&amp;about=general&amp;q=rota&amp;thread={general.pk}"')
        reply = self.client.post(reverse('portal:team-message-create', args=[general.pk]), {'body': 'Sorted', 'status': 'open', 'about': 'general'})
        self.assertRedirects(reply, f'{inbox}?status=open&about=general&thread={general.pk}', fetch_redirect_response=False)
        unread = self.client.post(reverse('portal:team-thread-read-state', args=[general.pk]), {'state': 'unread', 'about': 'general'})
        self.assertRedirects(unread, f'{inbox}?about=general', fetch_redirect_response=False)

    def test_each_member_has_their_own_unread_count(self):
        thread = self.start(members=[self.admin, self.dietitian])
        self.assertEqual(team_threads(self.company, self.admin).get().unread_count, 1)
        self.assertEqual(team_threads(self.company, self.doctor).get().unread_count, 0)
        self.login(self.admin)
        response = self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk})
        self.assertEqual(response.context['team_unread_total'], 0)
        self.assertEqual(team_threads(self.company, self.dietitian).get().unread_count, 1)
        post_team_message(thread=thread, sender=self.dietitian, body='On it.')
        self.assertEqual(team_threads(self.company, self.admin).get().unread_count, 1)
        self.assertEqual(self.client.get(reverse('portal:staff-inbox')).context['team_unread_total'], 1)

    def test_patient_link_shows_on_the_record_for_members_only_and_never_to_the_patient(self):
        thread = self.start()
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.login(self.doctor)
        self.assertEqual([t.pk for t in self.client.get(record, {'tab': 'messages'}).context['team_threads_about_patient']], [thread.pk])
        # The dietitian saw Pat before, so can open the record, but isn't in the team conversation.
        ClinicianAssignment.objects.create(company=self.company, patient=self.patient, clinician=self.dietitian,
                                           ended_at=timezone.now())
        self.login(self.dietitian)
        self.assertEqual(self.client.get(record, {'tab': 'messages'}).context['team_threads_about_patient'], [])
        self.client.force_login(self.patient_user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        page = self.client.get(reverse('portal:patient-messages'))
        self.assertNotContains(page, 'Delivery for Pat')
        self.assertNotContains(page, 'Has the parcel gone out?')
        self.assertEqual(self.client.get(reverse('portal:team-inbox')).status_code, 403)

    def test_compose_from_a_record_preselects_the_patient(self):
        self.login(self.doctor)
        response = self.client.get(reverse('portal:team-inbox'), {'compose': 1, 'patient': self.patient.pk})
        self.assertTrue(response.context['composing'])
        self.assertEqual(str(response.context['thread_form']['patient'].value()), str(self.patient.pk))
        self.assertNotIn(self.doctor, response.context['thread_form'].fields['members'].queryset)
        self.assertContains(response, 'Ada Admin · Practice administrator')
