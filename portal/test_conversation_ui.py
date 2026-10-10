"""Who wrote each message: yours on the right as "You", everyone else on the left in their own colour."""

from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import MessageThread, PatientMessage
from care.team_messaging import post_team_message, start_team_conversation
from portal.templatetags.conversation_ui import TONES, message_time, toned_messages
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class TonedMessagesTests(SimpleTestCase):
    def messages(self, *senders):
        return [SimpleNamespace(sender_id=sender) for sender in senders]

    def test_yours_have_no_colour_and_the_others_follow_in_join_order(self):
        toned = toned_messages(self.messages(1, 2, 3, 4, 1), viewer_id=2, people=[1, 2, 3, 4])
        self.assertEqual([(message.is_mine, message.tone) for message in toned], [
            (False, 'blue'), (True, ''), (False, 'orange'), (False, 'pink'), (False, 'blue'),
        ])

    def test_someone_who_has_left_still_gets_the_next_colour(self):
        toned = toned_messages(self.messages(9, 1), viewer_id=2, people=[1, 2])
        self.assertEqual([message.tone for message in toned], ['orange', 'blue'])

    def test_a_message_with_no_sender_is_never_yours(self):
        self.assertFalse(toned_messages(self.messages(None), viewer_id=None, people=[])[0].is_mine)

    def test_colours_start_again_after_navy(self):
        people = list(range(1, len(TONES) + 2))
        toned = toned_messages(self.messages(*people), viewer_id=0, people=people)
        self.assertEqual(toned[-2].tone, 'navy')
        self.assertEqual(toned[-1].tone, 'blue')


class MessageTimeTests(SimpleTestCase):
    def test_today_shows_only_the_time_and_older_messages_the_date_too(self):
        today = timezone.localtime().replace(hour=6, minute=0, second=0, microsecond=0)
        self.assertEqual(message_time(today), '06:00')
        yesterday = today - timedelta(days=1)
        self.assertEqual(message_time(yesterday), f'{yesterday.day} {yesterday:%b}, 06:00')
        self.assertEqual(message_time(yesterday, 'j M Y, H:i'), f'{yesterday.day} {yesterday:%b} {yesterday.year}, 06:00')
        self.assertEqual(message_time(None), '')


@override_settings(MULTI_PRACTICE_ENABLED=True)
class GroupConversationTestCase(TestCase):
    """Nadia, with Sam, then Joshua, then Lerato added: two older messages, then two from today at 06:00."""

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Bubble Practice', slug='bubble-practice')
        cls.doctor = users.create_user(email='bubble-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.colleague = users.create_user(email='bubble-colleague@example.test', first_name='Joshua', last_name='Czech')
        cls.dietitian = users.create_user(email='bubble-dietitian@example.test', first_name='Lerato', last_name='Botha')
        for user in (cls.doctor, cls.colleague, cls.dietitian):
            CompanyMembership.objects.create(user=user, company=cls.company, role=CompanyMembership.Role.DOCTOR)
        cls.patient_user = users.create_user(email='bubble-patient@example.test', first_name='Nadia')
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena', assigned_doctor=cls.doctor,
        )
        cls.thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Something else', opened_by=cls.patient_user,
            last_message_at=timezone.now(),
        )
        for user in (cls.doctor, cls.colleague, cls.dietitian):
            add_participant(cls.thread, user)
        cls.today = timezone.localtime().replace(hour=6, minute=0, second=0, microsecond=0)
        for sender, body, sent in (
            (cls.patient_user, 'Hi Sam', cls.today - timedelta(days=4)),
            (cls.doctor, 'I have added Josh', cls.today - timedelta(days=4)),
            (cls.colleague, 'Hello Nadia, how can I help?', cls.today),
            (cls.dietitian, 'Happy to help with meal plans.', cls.today),
        ):
            message = PatientMessage.objects.create(company=cls.company, thread=cls.thread, sender=sender, body=body)
            PatientMessage.objects.filter(pk=message.pk).update(created_at=sent)

    def login(self, user, key=ACTIVE_COMPANY_SESSION_KEY):
        self.client.force_login(user)
        session = self.client.session
        session[key] = self.company.pk
        session.save()


class ConversationBubbleTests(GroupConversationTestCase):
    def bubble(self, tone, body, meta):
        mine = ' record-bubble--mine' if not tone else f' message-tone--{tone}'
        return f'<li class="record-bubble{mine}">\n      <p>{body}</p>\n      <span class="record-bubble__meta">{meta} &middot; '

    def test_each_clinician_sees_their_own_messages_as_you_and_everyone_else_in_colour(self):
        older = f'{(self.today - timedelta(days=4)).day} {self.today - timedelta(days=4):%b}, 06:00'
        for viewer, expected in (
            (self.doctor, [('blue', 'Hi Sam', 'Nadia Mokoena'), ('', 'I have added Josh', 'You'),
                           ('orange', 'Hello Nadia, how can I help?', 'Joshua Czech'),
                           ('pink', 'Happy to help with meal plans.', 'Lerato Botha')]),
            (self.colleague, [('blue', 'Hi Sam', 'Nadia Mokoena'), ('orange', 'I have added Josh', 'Sam Marchant'),
                              ('', 'Hello Nadia, how can I help?', 'You'),
                              ('pink', 'Happy to help with meal plans.', 'Lerato Botha')]),
        ):
            with self.subTest(viewer=viewer.first_name):
                self.login(viewer)
                page = self.client.get(f"{reverse('portal:staff-inbox')}?thread={self.thread.pk}")
                for tone, body, meta in expected:
                    self.assertContains(page, self.bubble(tone, body, meta))
                self.assertContains(page, f'">{older}</time>')
                self.assertContains(page, '">06:00</time>', count=2)

    def test_the_patient_sees_their_own_messages_as_you_and_each_clinician_in_colour(self):
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        page = self.client.get(f"{reverse('portal:patient-messages')}?thread={self.thread.pk}")
        self.assertContains(page, '<li class="patient-conversation-message patient-conversation-message--mine">\n              <p>Hi Sam</p>')
        self.assertContains(page, '<strong>You</strong>', count=1)
        for tone, body in (('blue', 'I have added Josh'), ('orange', 'Hello Nadia, how can I help?'),
                           ('pink', 'Happy to help with meal plans.')):
            self.assertContains(page, f'<li class="patient-conversation-message message-tone--{tone}">\n              <p>{body}</p>')

    def test_team_conversations_show_you_and_colleagues_in_colour(self):
        thread = start_team_conversation(company=self.company, actor=self.doctor, members=[self.colleague],
                                         subject='Billing', body='Please update the billing')
        post_team_message(thread=thread, sender=self.colleague, body='Will do')
        self.login(self.doctor)
        page = self.client.get(f"{reverse('portal:team-inbox')}?thread={thread.pk}")
        self.assertContains(page, '<li class="record-bubble record-bubble--mine">\n                      <p>Please update the billing</p>\n                      <span class="record-bubble__meta">You &middot; ')
        self.assertContains(page, '<li class="record-bubble message-tone--blue">\n                      <p>Will do</p>\n                      <span class="record-bubble__meta">Joshua Czech &middot; ')


class ConversationListPreviewTests(GroupConversationTestCase):
    """In a list, the latest message is "You: …" for yours and "Lerato: …" for a colleague's in a group
    conversation. The patient, or the only other person, goes unnamed. "Closed" sits at the top right."""

    def preview(self, body, sender=''):
        return f'<span class="record-inbox-thread__preview">{f"<strong>{sender}:</strong> " if sender else ""}{body}</span>'

    def test_a_colleague_is_named_before_their_message_and_the_patient_is_not(self):
        self.login(self.doctor)
        record = f"{reverse('portal:patient-detail', args=[self.patient.pk])}?tab=messages"
        for url in (reverse('portal:staff-inbox'), record):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), self.preview('Happy to help with meal plans.', 'Lerato'))
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.patient_user, body='Thanks both!')
        self.assertContains(self.client.get(reverse('portal:staff-inbox')), self.preview('Thanks both!'))
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.doctor, body='Speak soon')
        self.assertContains(self.client.get(reverse('portal:staff-inbox')), self.preview('Speak soon', 'You'))

    def test_the_patient_sees_a_clinician_named_when_more_than_one_is_in_the_conversation(self):
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        self.assertContains(self.client.get(reverse('portal:patient-messages')),
                            '<span class="patient-inbox-thread__preview"><strong>Lerato:</strong> Happy to help with meal plans.</span>')

    def test_closed_sits_at_the_top_right_after_the_subject(self):
        MessageThread.objects.filter(pk=self.thread.pk).update(is_closed=True)
        self.login(self.doctor)
        self.assertContains(self.client.get(reverse('portal:staff-inbox')),
                            '<strong><span>Something else</span><span class="record-inbox-pill">Closed</span>')
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        self.assertContains(self.client.get(f"{reverse('portal:patient-messages')}?status=closed"),
                            '<strong><span>Something else</span><span class="patient-inbox-pill">Closed</span>')

    def test_a_colleague_is_named_in_a_group_team_conversation(self):
        thread = start_team_conversation(company=self.company, actor=self.doctor, members=[self.colleague, self.dietitian],
                                         subject='Rota', body='Who covers Friday?')
        post_team_message(thread=thread, sender=self.colleague, body='I can')
        self.login(self.doctor)
        self.assertContains(self.client.get(reverse('portal:team-inbox')), self.preview('I can', 'Joshua'))


class PeopleTagTests(GroupConversationTestCase):
    """Who's in a conversation shows as tags by role, instead of "With …"."""

    def test_patient_messages_tag_the_patient_doctors_and_practice_admins(self):
        admin = get_user_model().objects.create_user(email='bubble-admin@example.test', first_name='Lindiwe', last_name='Mahlangu')
        CompanyMembership.objects.create(user=admin, company=self.company, role=CompanyMembership.Role.PRACTICE_ADMIN)
        add_participant(self.thread, admin)
        get_user_model().objects.filter(pk=self.colleague.pk).update(title='DR')
        self.login(self.doctor)
        response = self.client.get(f"{reverse('portal:staff-inbox')}?thread={self.thread.pk}")
        listed = next(thread for thread in response.context['threads'] if thread.pk == self.thread.pk)
        # Clinicians by title, initial and surname; administrators by first name and surname initial.
        self.assertEqual([(tag['name'], tag['kind']) for tag in listed.people_tags], [
            ('Nadia Mokoena', 'patient'), ('Dr J. Czech', 'doctor'), ('L. Botha', 'doctor'), ('Lindiwe M.', 'admin'),
        ])
        # Hovering shows the full name and role.
        self.assertContains(response, '<span class="person-tag person-tag--doctor" title="Joshua Czech · Doctor">')
        # The same tags over the open conversation.
        html = response.content.decode()
        header = html[html.index('<p class="record-messages__people record-messages__tags person-tags">'):]
        header = header[:header.index('</p>')]
        for kind, name in (('patient', 'Nadia Mokoena'), ('doctor', 'Dr J. Czech'), ('admin', 'Lindiwe M.')):
            self.assertIn(f'<span class="person-tag person-tag--{kind}"', header)
            self.assertIn(f'{name}<span class="sr-only">', header)
