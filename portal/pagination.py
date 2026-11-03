"""Shared elided page links for dispatch history and stock movements."""

from urllib.parse import urlencode


def numbered_pagination(page, *, base_url, query=None):
    """Link a Django page with two adjacent pages and the paginator's normal end pages.

    Callers pass only their supported filters. A previous page parameter is replaced, never repeated.
    """
    filters = dict(query or {})
    filters.pop('page', None)

    def page_url(number):
        return f'{base_url}?{urlencode({**filters, "page": number}, doseq=True)}'

    return {
        'page_urls': [
            {'number': number, 'url': None if number == page.paginator.ELLIPSIS else page_url(number),
             'current': number == page.number, 'ellipsis': number == page.paginator.ELLIPSIS}
            for number in page.paginator.get_elided_page_range(page.number, on_each_side=2)
        ],
        'previous_url': page_url(page.previous_page_number()) if page.has_previous() else None,
        'next_url': page_url(page.next_page_number()) if page.has_next() else None,
    }
