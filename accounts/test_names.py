"""How a person's title and name are put together."""

from django.test import SimpleTestCase

from accounts.models import User


class TitledNameTests(SimpleTestCase):
    def test_the_title_is_never_doubled(self):
        for first, title, expected in (
            ('James', 'DR', 'Dr James Westraadt'),
            ('Dr James', 'DR', 'Dr James Westraadt'),
            ('Dr. James', 'DR', 'Dr James Westraadt'),
            ('Dr James', '', 'Dr James Westraadt'),
            ('James', '', 'James Westraadt'),
            ('Drew', 'DR', 'Dr Drew Westraadt'),
        ):
            with self.subTest(first=first, title=title):
                self.assertEqual(User(first_name=first, last_name='Westraadt', title=title).titled_name, expected)
        self.assertEqual(User(first_name='Dr James', title='DR').given_name, 'James')
        self.assertEqual(User(email='only@example.test', title='DR').titled_name, 'only@example.test')

    def test_the_formal_name_is_title_and_surname(self):
        self.assertEqual(User(first_name='Dr James', last_name='Westraadt', title='DR').formal_name, 'Dr Westraadt')
        self.assertEqual(User(first_name='James', last_name='Westraadt', title='').formal_name, 'James Westraadt')
        self.assertEqual(User(first_name='James', title='DR').formal_name, 'Dr James')
