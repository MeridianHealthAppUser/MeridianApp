"""Who wrote each message: yours on the right as "You", everyone else on the left in their own colour."""

from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
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
        message_id = self.thread.messages.get(body=body).pk
        return f'<li data-message-id="{message_id}" class="record-bubble{mine}">\n      <p>{body}</p>\n      <span class="record-bubble__meta">{meta} &middot; '

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
        mine_id = self.thread.messages.get(body='Hi Sam').pk
        self.assertContains(page, f'<li data-message-id="{mine_id}" class="patient-conversation-message patient-conversation-message--mine">\n              <p>Hi Sam</p>')
        self.assertContains(page, '<strong>You</strong>', count=1)
        for tone, body in (('blue', 'I have added Josh'), ('orange', 'Hello Nadia, how can I help?'),
                           ('pink', 'Happy to help with meal plans.')):
            message_id = self.thread.messages.get(body=body).pk
            self.assertContains(page, f'<li data-message-id="{message_id}" class="patient-conversation-message message-tone--{tone}">\n              <p>{body}</p>')

    def test_team_conversations_show_you_and_colleagues_in_colour(self):
        thread = start_team_conversation(company=self.company, actor=self.doctor, members=[self.colleague],
                                         subject='Billing', body='Please update the billing')
        post_team_message(thread=thread, sender=self.colleague, body='Will do')
        self.login(self.doctor)
        page = self.client.get(f"{reverse('portal:team-inbox')}?thread={thread.pk}")
        mine_id = thread.messages.get(body='Please update the billing').pk
        reply_id = thread.messages.get(body='Will do').pk
        self.assertContains(page, f'<li data-message-id="{mine_id}" class="record-bubble record-bubble--mine">\n                      <p>Please update the billing</p>\n                      <span class="record-bubble__meta">You &middot; ')
        self.assertContains(page, f'<li data-message-id="{reply_id}" class="record-bubble message-tone--blue">\n                      <p>Will do</p>\n                      <span class="record-bubble__meta">Joshua Czech &middot; ')


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
        # Sent after the fixture's 06:00 messages, whatever the time the tests run (before 06:00 too).
        for minutes, sender, body, shown in ((1, self.patient_user, 'Thanks both!', ''), (2, self.doctor, 'Speak soon', 'You')):
            message = PatientMessage.objects.create(company=self.company, thread=self.thread, sender=sender, body=body)
            PatientMessage.objects.filter(pk=message.pk).update(created_at=max(timezone.now(), self.today + timedelta(minutes=minutes)))
            self.assertContains(self.client.get(reverse('portal:staff-inbox')), self.preview(body, shown))

    def test_the_patient_sees_a_clinician_named_when_more_than_one_is_in_the_conversation(self):
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        self.assertContains(self.client.get(reverse('portal:patient-messages')),
                            '<span class="patient-inbox-thread__preview"><strong>Lerato:</strong> Happy to help with meal plans.</span>')

    def test_closed_sits_at_the_right_of_the_subject(self):
        MessageThread.objects.filter(pk=self.thread.pk).update(is_closed=True)
        self.login(self.doctor)
        self.assertContains(self.client.get(reverse('portal:staff-inbox')),
                            '<span class="record-inbox-thread__title"><strong class="record-inbox-thread__subject">Something else</strong>'
                            '<span class="record-inbox-pill">Closed</span></span>')
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        self.assertContains(self.client.get(f"{reverse('portal:patient-messages')}?status=closed"),
                            '<span class="patient-inbox-thread__title"><strong class="patient-inbox-thread__subject">Something else</strong>'
                            '<span class="patient-inbox-pill">Closed</span></span>')

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


class ConversationRowOrderTests(GroupConversationTestCase):
    """Each row in a list of conversations starts with who it's with, leaving out whoever is looking (and on a record,
    its patient), then the subject, then a team conversation's patient, then the latest message (James, 20 Nov 2026)."""

    def row(self, response, subject, row_class='record-inbox-thread'):
        """The row (its link) whose subject is this one."""
        html = response.content.decode()
        start = html.index(f'<a class="{row_class}', html.index(f'{row_class}__subject">{subject}</strong>') - 2500)
        while html.find(f'<a class="{row_class}', start + 1) != -1 and html.find(f'<a class="{row_class}', start + 1) < html.index(f'{row_class}__subject">{subject}</strong>'):
            start = html.find(f'<a class="{row_class}', start + 1)
        return html[start:html.index('</a>', start)]

    def assert_people_first(self, row, people_class, names, absent):
        head = row.index(f'<span class="{people_class} person-tags person-tags--line"><span class="sr-only">With </span>')
        subject = row.index('__subject">')
        self.assertLess(head, subject)
        people = row[head:subject]
        for name in names:
            self.assertIn(f'{name}<span class="sr-only">', people)
        for name in absent:
            self.assertNotIn(name, people)
        self.assertLess(subject, row.index('__last">'))

    def test_the_people_line_comes_before_the_subject_and_leaves_out_the_viewer(self):
        # Patient messages: the patient first, then the other staff; never you.
        self.login(self.doctor)
        row = self.row(self.client.get(reverse('portal:staff-inbox')), 'Something else')
        self.assert_people_first(row, 'record-inbox-thread__people', ['Nadia Mokoena', 'J. Czech', 'L. Botha'], ['Marchant'])
        self.assertLess(row.index('person-tag--patient'), row.index('person-tag--doctor'))
        # A record's Messages tab: the other staff, not its patient or you.
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages'})
        self.assert_people_first(self.row(record, 'Something else'), 'record-inbox-thread__people', ['J. Czech', 'L. Botha'], ['Nadia', 'Marchant'])
        # Team messages: the colleagues, then the subject, then the patient it's about.
        start_team_conversation(company=self.company, actor=self.doctor, members=[self.colleague], subject='Rota',
                                body='Who covers Friday?', patient=self.patient)
        row = self.row(self.client.get(reverse('portal:team-inbox')), 'Rota')
        self.assert_people_first(row, 'record-inbox-thread__people', ['J. Czech'], ['Marchant'])
        self.assertLess(row.index('record-inbox-thread__subject'), row.index('record-inbox-thread__about'))
        self.assertLess(row.index('record-inbox-thread__about'), row.index('record-inbox-thread__last'))
        # My Messages: the clinic staff the patient is talking to, never the patient.
        self.login(self.patient_user, ACTIVE_PATIENT_COMPANY_SESSION_KEY)
        row = self.row(self.client.get(reverse('portal:patient-messages')), 'Something else', 'patient-inbox-thread')
        self.assert_people_first(row, 'patient-inbox-thread__people', ['S. Marchant', 'J. Czech', 'L. Botha'], ['Nadia'])

    def test_the_unread_count_and_envelope_sit_on_the_people_line(self):
        self.login(self.doctor)
        html = self.client.get(reverse('portal:staff-inbox')).content.decode()
        # Side by side at the top right: the count, then the envelope (positioned at the row's top right).
        self.assertRegex(html, r'<li class="record-inbox__item--marks">\s*<a class="record-inbox-thread is-unread" [^>]*>\s*'
                               r'<span class="record-inbox-thread__head"><span class="record-inbox-thread__people [^"]*">.*?</span>'
                               r'<span class="record-inbox-thread__marks"><span class="nav-badge" aria-hidden="true">\d</span>'
                               r'<span class="sr-only">, \d unread messages?</span></span></span>\s*'
                               r'<span class="record-inbox-thread__title"><strong class="record-inbox-thread__subject">Something else</strong>')
        self.assertIn('.record-inbox__mark { position: absolute; right: .8rem; top: .8rem; }', (settings.BASE_DIR / 'static/css/conversations.css').read_text())

    def test_more_than_three_people_show_the_first_three_and_a_count_naming_the_rest(self):
        for first, last in (('Lindiwe', 'Mahlangu'), ('Thabo', 'Nkosi')):
            admin = get_user_model().objects.create_user(email=f'{first.lower()}@example.test', first_name=first, last_name=last)
            CompanyMembership.objects.create(user=admin, company=self.company, role=CompanyMembership.Role.PRACTICE_ADMIN)
            add_participant(self.thread, admin)
        self.login(self.doctor)
        row = self.row(self.client.get(reverse('portal:staff-inbox')), 'Something else')
        people = row[row.index('record-inbox-thread__people'):row.index('record-inbox-thread__subject')]
        # Nadia, Joshua and Lerato, then +2 for Lindiwe and Thabo, named on hover and to screen readers.
        self.assertEqual(people.count('class="person-tag person-tag--'), 4)
        self.assertIn('<span class="person-tag person-tag--more" title="Lindiwe Mahlangu · Practice admin, Thabo Nkosi · Practice admin">'
                      '<span aria-hidden="true">+2</span><span class="sr-only">and Lindiwe Mahlangu (Practice admin), Thabo Nkosi (Practice admin)</span></span>', people)
        self.assertNotIn('Lindiwe M.<span', people)

    def test_with_nobody_else_in_it_the_subject_comes_first(self):
        alone = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Note to self',
                                             opened_by=self.doctor, last_message_at=timezone.now())
        add_participant(alone, self.doctor)
        self.login(self.doctor)
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages'})
        row = self.row(record, 'Note to self')
        self.assertTrue(row.split('>', 1)[1].lstrip().startswith(
            '<span class="record-inbox-thread__head record-inbox-thread__head--subject"><strong class="record-inbox-thread__subject">Note to self</strong>'))
        self.assertNotIn('person-tags', row)

