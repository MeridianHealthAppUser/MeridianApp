"""The staff menu, defined once for the desktop sidebar, the mobile bar and the mobile section pages.

Clinicians and practice administrators get one flat list with icons. Super Admins get sections.
Operations and Administration are each one page of tabs (page_tabs) for whoever opens them.
"""

from django.urls import reverse

# Each tab: its nav_section key, label and page. Super Admins see the same pages listed in their sections.
OPERATIONS_TABS = (
    ('subscriptions', 'Subscriptions', 'portal:treatment-subscriptions'),
    ('orders', 'Non-prescription requests', 'portal:ops-orders'),
    ('shipping', 'Weekly shipping list', 'portal:ops-shipping'),
    ('stock', 'Dispatch stock', 'portal:ops-stock'),
    ('history', 'Dispatch history', 'portal:ops-history'),
    ('catalogue', 'Catalogue', 'portal:ops-catalogue'),
)
ADMINISTRATION_TABS = (
    ('leads', 'Leads', 'portal:staff-leads'),
    ('dropouts', 'Dropouts', 'portal:dropouts'),
    ('privacy_requests', 'Data requests', 'portal:staff-data-requests'),
)
TAB_GROUPS = {'operations': ('Operations', OPERATIONS_TABS), 'administration': ('Administration', ADMINISTRATION_TABS)}


def _item(key, label, url_name, *, also=(), short_label=None, mobile_url_name=None):
    return {'key': key, 'label': label, 'url': reverse(url_name), 'also': also, 'short_label': short_label or label,
            'mobile_url': reverse(mobile_url_name or url_name), 'badge': 0, 'badge_label': 'unread message'}


def _set_badges(items, *, unread=0, data_requests=0):
    """Patient Records & Messages (Messages for practice administrators) counts unread messages; Data requests, and
    Administration that holds it, count requests still waiting on the practice."""
    for item in items:
        if item['key'] == 'records':
            item['badge'] = unread
        elif item['key'] in ('privacy_requests', 'administration'):
            item['badge'], item['badge_label'] = data_requests, 'open data request'


def _current(item, nav_section):
    return 'page' if nav_section == item['key'] else ('true' if nav_section in item['also'] else '')


def uses_flat_menu(membership):
    """Clinicians and practice administrators get one flat list like the patient app instead of sections."""
    return membership.role in ('doctor', 'practice_admin')


def page_tabs(nav_section, *, data_requests=0):
    """The Operations or Administration tabs for the page at nav_section, with the current one marked; None for any
    other page. Data requests carries its count, as the menu does."""
    for key, (label, tabs) in TAB_GROUPS.items():
        if nav_section in (tab[0] for tab in tabs):
            items = [dict(key=tab_key, label=tab_label, url=reverse(url_name), active=tab_key == nav_section,
                          badge=data_requests if tab_key == 'privacy_requests' else 0, badge_label='open data request')
                     for tab_key, tab_label, url_name in tabs]
            return {'key': key, 'label': label, 'tabs': items}
    return None


def _tab_items(group):
    return [_item(key, label, url_name) for key, label, url_name in TAB_GROUPS[group][1]]


def _group_item(group, short_label):
    """Operations or Administration as one menu entry: it opens the first tab and stays current on all of them."""
    label, tabs = TAB_GROUPS[group]
    return _item(group, label, tabs[0][2], also=tuple(tab[0] for tab in tabs), short_label=short_label)


RECORDS_TITLE = 'Patient Records & Messages'
MESSAGES_TITLE = 'Messages'


def messages_title(membership):
    """Messages and the patient list are one page, Patient Records & Messages. Practice administrators don't browse
    patient records, so theirs is just Messages: Patient messages and Team messages."""
    return MESSAGES_TITLE if membership.role == 'practice_admin' else RECORDS_TITLE


def _records_item(membership):
    if membership.role == 'practice_admin':
        return _item('records', MESSAGES_TITLE, 'portal:staff-inbox', also=('messages', 'team'))
    # All patients (the default tab), Patient messages and Team messages share one page; an open record belongs to it too.
    return _item('records', RECORDS_TITLE, 'portal:patient-list', also=('messages', 'patients', 'team', 'record'),
                 short_label='Records')


def build_flat_menu(membership, *, nav_section='', unread=0, data_requests=0):
    """Overview and the member's pages, each with an icon; Patient Records & Messages (Messages for practice
    administrators) carries the unread count. Practice administrators also get Operations and Administration, each one
    page of tabs."""
    items = [
        _item('overview', 'Overview', 'portal:desktop-dashboard', mobile_url_name='portal:mobile-dashboard'),
        _item('tasks', 'My tasks', 'portal:staff-tasks', short_label='Tasks'),
        _records_item(membership),
        _item('schedule', 'Calendar & Bookings', 'portal:staff-schedule', short_label='Calendar'),
    ]
    if membership.is_admin:
        items += [_group_item('operations', 'Operations'), _group_item('administration', 'Admin')]
    for item in items:
        item['current'] = _current(item, nav_section)
    _set_badges(items, unread=unread, data_requests=data_requests)
    return items


def build_staff_menu(membership, *, multi_practice=False, nav_section='', unread=0, data_requests=0):
    """Sections the member may use, each with the pages inside it and which one is current.

    Clinicians and practice administrators have no sections: their menu is flat (build_flat_menu), so this returns
    none for them.
    """
    if uses_flat_menu(membership):
        return []
    role = membership.role
    clinical = membership.has_clinical_access
    admin = membership.is_admin
    super_admin = role == 'super_admin'
    sections = [
        ('care', 'Care', 'Care', [
            _item('tasks', 'Tasks', 'portal:staff-tasks'),
            _records_item(membership),
            *([_item('consultations', 'Consultations', 'portal:clinical-consultations'),
               _item('labs', 'Blood tests', 'portal:clinical-labs'),
               _item('authorisations', 'Authorisations', 'portal:treatment-authorisations'),
               _item('compounding', 'Compounding records', 'portal:compounding-list')] if clinical else []),
            _item('schedule', 'Calendar & Bookings', 'portal:staff-schedule'),
        ]),
        ('operations', 'Operations', 'Operations', _tab_items('operations') if admin else []),
        ('administration', 'Administration', 'Admin', _tab_items('administration') if admin else []),
        # Insights is for Super Admins only.
        ('insights', 'Insights', 'Insights', [
            _item('metrics', 'Metrics and cohorts', 'portal:metrics'),
            _item('statements', 'Activity statements', 'portal:activity-statements'),
        ] if super_admin else []),
        ('settings', 'Settings', 'Settings', [
            _item('review_rules', 'Review rules', 'portal:treatment-review-rules'),
            *([_item('practices', 'Practices', 'portal:management-practices')] if multi_practice else []),
            _item('users', 'Users and roles', 'portal:management-users'),
            _item('policies', 'Policy versions', 'portal:policy-list'),
            _item('practice_settings', 'Practice settings', 'portal:practice-settings'),
        ] if super_admin else []),
    ]
    menu = []
    for key, label, short_label, items in sections:
        if not items:
            continue
        for item in items:
            item['current'] = _current(item, nav_section)
        _set_badges(items, unread=unread, data_requests=data_requests)
        labels = {item['badge_label'] for item in items if item['badge']}
        menu.append({
            'key': key, 'label': label, 'short_label': short_label, 'items': items,
            'url': reverse('portal:staff-menu-section', args=[key]),
            'active': nav_section == f'section:{key}' or any(item['current'] for item in items),
            # The mobile bar shows sections only, so a section carries the counts of the pages inside it.
            'badge': sum(item['badge'] for item in items),
            'badge_label': labels.pop() if len(labels) == 1 else 'waiting item',
        })
    return menu


def build_staff_navigation(membership, *, multi_practice=False, nav_section='', unread=0, data_requests=0):
    if uses_flat_menu(membership):
        return {'flat': build_flat_menu(membership, nav_section=nav_section, unread=unread, data_requests=data_requests),
                'sections': []}
    return {'flat': [], 'sections': build_staff_menu(membership, multi_practice=multi_practice, nav_section=nav_section,
                                                     unread=unread, data_requests=data_requests)}
