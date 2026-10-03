"""Staff message colleagues; only members see a team conversation and patients never do."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from care.models import TeamMessage, TeamThread, TeamThreadMember
from care.team_messaging import add_team_member, post_team_message, start_team_conversation, team_threads
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
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Pat', last_name='Patient')

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
        self.assertRedirects(response, f'{reverse("portal:team-inbox")}?thread={thread.pk}#team-conversation', fetch_redirect_response=False)
        self.assertEqual(set(thread.members.all()), {self.admin, self.doctor, self.dietitian})
        self.assertIsNone(thread.patient)
        self.assertEqual(thread.messages.get().sender, self.admin)

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

    def test_members_add_colleagues_who_then_see_the_whole_conversation(self):
        thread = self.start()
        add_team_member(thread=thread, actor=self.admin, user=self.dietitian)
        self.assertEqual(TeamThreadMember.objects.get(thread=thread, user=self.dietitian).added_by, self.admin)
        self.login(self.dietitian)
        response = self.client.get(reverse('portal:team-inbox'), {'thread': thread.pk})
        self.assertContains(response, 'Has the parcel gone out?')
        self.assertContains(response, 'Ada Admin added Dee Dietitian')
        with self.assertRaises(ValidationError):
            add_team_member(thread=thread, actor=self.dietitian, user=self.dietitian)

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
