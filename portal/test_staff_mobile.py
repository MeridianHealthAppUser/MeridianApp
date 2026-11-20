"""Staff pages on a phone (Nov 2026): the phone stylesheet on staff pages, a bottom bar whose labels fit, inboxes as a
list then one conversation with a way back, and tables that become cards with each value beside its column's name.
The layout itself is static/css/staff_mobile.css. Like patient_mobile.css, it changes nothing above phone width: the
web layout stays as it was (James, 15 Nov 2026)."""
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from care.messaging import add_participant
from care.models import MessageThread
from care.team_messaging import start_team_conversation
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY

from .inbox import staff_inbox_url
from .patient_workspace import workspace_url

CSS = settings.BASE_DIR / 'static' / 'css'
TEMPLATES = settings.BASE_DIR / 'templates'


def top_level_rules(css):
    """A stylesheet's rules that sit outside every @media, @supports and @keyframes block, whitespace collapsed."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    rules, depth, start, prelude = [], 0, 0, ''
    for at, char in enumerate(css):
        if char == '{':
            if depth == 0:
                prelude = css[start:at].strip()
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                if not prelude.startswith('@'):
                    rules.append(re.sub(r'\s+', ' ', css[start:at + 1].strip()))
                start = at + 1
    return rules


class PhoneStylesheetTests(SimpleTestCase):
    def test_the_phone_stylesheets_leave_the_web_layout_alone(self):
        # Outside a phone-width block they only hide what exists for phones (the shortened label, the ways back).
        self.assertEqual(top_level_rules((CSS / 'staff_mobile.css').read_text()), ['.record-inbox__back { display: none; }'])
        self.assertEqual(top_level_rules((CSS / 'patient_mobile.css').read_text()),
                         ['.patient-navigation__short { display: none; }', '.patient-conversation__back { display: none; }'])
        for name in ('staff_mobile.css', 'patient_mobile.css'):
            queries = re.findall(r'@media([^{]*)\{', (CSS / name).read_text())
            self.assertTrue(queries)
            for query in queries:
                with self.subTest(stylesheet=name, query=query):
                    width = re.search(r'max-width: ([\d.]+)rem', query)
                    self.assertIsNotNone(width)
                    self.assertLessEqual(float(width.group(1)), 52)

    def test_every_card_table_cell_is_named_after_its_column(self):
        # A table that becomes cards on a phone names each value; a column added later needs its data-label too.
        tables = 0
        for path in TEMPLATES.rglob('*.html'):
            for table in re.finditer(r'<table\b[^>]*staff-phone-cards[^>]*>(.*?)</table>', path.read_text(), re.S):
                tables += 1
                head, _, body = table.group(1).partition('</thead>')
                columns = re.findall(r'<th\b[^>]*>(.*?)</th>', head, re.S)
                for row in re.findall(r'<tr\b.*?</tr>', body, re.S):
                    cells = re.findall(r'<td\b[^>]*>', row)
                    if len(cells) < 2:
                        continue  # "Nothing here" spans the card.
                    with self.subTest(template=str(path.relative_to(TEMPLATES)), row=row[:80]):
                        unnamed = [cell for cell in cells if 'data-label=' not in cell]
                        # Only a column with no heading (a tick box) goes without a name.
                        self.assertLessEqual(len(unnamed), sum(1 for column in columns if not re.sub(r'<[^>]+>', '', column).strip()))
        self.assertGreaterEqual(tables, 15)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class StaffPhoneLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Staff phone practice', slug='staff-phone-layout')
        cls.doctor = users.create_user(email='phone-staff-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.colleague = users.create_user(email='phone-staff-colleague@example.test', first_name='Joshua', last_name='Czech')
        cls.admin = users.create_user(email='phone-staff-admin@example.test', first_name='Lindiwe', last_name='Mahlangu')
        cls.super_admin = users.create_user(email='phone-staff-super@example.test', first_name='Josh', last_name='Super')
        for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'), (cls.admin, 'practice_admin'),
                           (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        cls.patient_user = users.create_user(email='phone-staff-patient@example.test', first_name='Nadia')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia',
                                             last_name='Mokoena', assigned_doctor=cls.doctor)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='About my dose',
                                                  opened_by=cls.patient_user)
        add_participant(cls.thread, cls.doctor)

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_staff_pages_load_the_phone_layout(self):
        self.login(self.doctor)
        for name in ('desktop-dashboard', 'staff-tasks', 'patient-list', 'staff-inbox', 'team-inbox', 'staff-schedule'):
            with self.subTest(page=name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'css/staff_mobile.css')
                self.assertNotContains(response, 'css/patient_mobile.css')

    def test_the_bottom_bar_says_leads_and_the_side_menu_says_leads_and_dropouts(self):
        for user in (self.admin, self.super_admin):
            with self.subTest(role=user.email):
                self.login(user)
                page = self.client.get(reverse('portal:staff-tasks')).content.decode()
                bar = page.split('class="mobile-bottom-nav staff-mobile-nav"', 1)[1].split('</nav>', 1)[0]
                self.assertRegex(bar, r'title="Leads and dropouts">.*?<span>Leads</span>')
                self.assertIn('Leads and dropouts', page.split('class="mobile-bottom-nav staff-mobile-nav"', 1)[0])

    def test_patient_messages_show_the_list_or_one_conversation_with_a_way_back(self):
        self.login(self.doctor)
        inbox = reverse('portal:staff-inbox')
        listing = self.client.get(inbox, {'q': 'dose'})
        self.assertNotContains(listing, 'record-inbox--open')
        self.assertNotContains(listing, 'class="record-inbox__back"')
        reading = self.client.get(inbox, {'q': 'dose', 'thread': self.thread.pk})
        self.assertContains(reading, 'class="staff-page-card record-inbox record-inbox--fill record-inbox--open"')
        # The card's header (title, count, Filters, search) goes with the list on a phone, so the conversation starts at
        # the top; above phone width it stays (staff_mobile.css).
        self.assertContains(reading, '<header class="staff-page-card__header staff-list-header record-inbox__header">')
        self.assertIn('.workspace-page .record-inbox--open > .record-inbox__header', (CSS / 'staff_mobile.css').read_text())
        self.assertContains(reading, f'<a class="record-inbox__back" href="{staff_inbox_url(search="dose")}">')
        self.assertContains(self.client.get(inbox, {'compose': 1}), 'record-inbox--open')

    def test_a_records_messages_show_the_list_or_one_conversation_with_a_way_back(self):
        self.login(self.doctor)
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.assertNotContains(self.client.get(record, {'tab': 'messages'}), 'record-inbox--open')
        reading = self.client.get(record, {'tab': 'messages', 'thread': self.thread.pk})
        self.assertContains(reading, 'class="record-inbox record-inbox--open"')
        self.assertContains(reading, f'<a class="record-inbox__back" href="{workspace_url(self.patient, "messages")}">')

    def test_team_messages_show_the_list_or_one_conversation_with_a_way_back(self):
        self.login(self.doctor)
        team = reverse('portal:team-inbox')
        # With no conversations yet, the new-message form is all there is, so there is no list to go back to.
        self.assertNotContains(self.client.get(team, {'compose': 1}), 'record-inbox--open')
        start_team_conversation(company=self.company, actor=self.doctor, members=[self.colleague],
                                subject='Rota', body='Can you cover Friday?')
        self.assertNotContains(self.client.get(team), 'record-inbox--open')
        composing = self.client.get(team, {'compose': 1})
        self.assertContains(composing, 'record-inbox--open')
        self.assertContains(composing, 'class="record-inbox__back"')
