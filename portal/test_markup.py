"""Rendered action targets for UI assertions; script metadata is not a link."""

from html.parser import HTMLParser
from urllib.parse import urlsplit


def actionable_paths(markup):
    """Return URL paths from links and form targets, ignoring query/fragment."""
    class Targets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = set()

        def handle_starttag(self, tag, attrs):
            attribute = {'a': 'href', 'area': 'href', 'form': 'action',
                         'button': 'formaction', 'input': 'formaction'}.get(tag)
            if attribute is None:
                return
            value = dict(attrs).get(attribute)
            if not value:
                return
            try:
                path = urlsplit(value).path
            except ValueError:
                return
            if path:
                self.paths.add(path)

    if hasattr(markup, 'content'):
        markup = markup.content.decode(markup.charset)
    parser = Targets()
    parser.feed(markup)
    return parser.paths
