"""The staff menu, defined once for the desktop sidebar, the mobile bar and the mobile section pages."""

from django.urls import reverse


def _item(key, label, url_name, *, also=(), short_label=None, mobile_url_name=None):
    return {'key': key, 'label': label, 'url': reverse(url_name), 'also': also, 'short_label': short_label or label,
            'mobile_url': reverse(mobile_url_name or url_name), 'badge': 0}


def _current(item, nav_section):
    return 'page' if nav_section == item['key'] else ('true' if nav_section in item['also'] else '')


def uses_flat_menu(membership):
    """Clinicians work in Care only, so they get one flat list like the patient app instead of sections."""
    return membership.role == 'doctor'


RECORDS_TITLE = 'Patient Records & Messages'


def messages_title(membership):
    """Messages and the patient list are one page now, Patient Records & Messages, for every role."""
    return RECORDS_TITLE


def _records_item():
    # All patients (the default tab), Patient messages and Team messages share one page; an open record belongs to it too.
    return _item('records', RECORDS_TITLE, 'portal:patient-list', also=('messages', 'patients', 'team', 'record'),
                 short_label='Records')


def build_flat_menu(membership, *, nav_section='', unread=0):
    """Overview and the clinician's pages, each with an icon; Patient Records & Messages carries the unread count."""
    items = [
        _item('overview', 'Overview', 'portal:desktop-dashboard', mobile_url_name='portal:mobile-dashboard'),
        _item('tasks', 'My tasks', 'portal:staff-tasks', short_label='Tasks'),
        _records_item(),
        _item('schedule', 'Calendar & Bookings', 'portal:staff-schedule', short_label='Calendar'),
    ]
    for item in items:
        item['current'] = _current(item, nav_section)
        if item['key'] == 'records':
            item['badge'] = unread
    return items


def build_staff_menu(membership, *, multi_practice=False, nav_section='', unread=0):
    """Sections the member may use, each with the pages inside it and which one is current.

    Clinicians have no sections: their menu is flat (build_flat_menu), so this returns none for them.
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
            _records_item(),
            *([_item('consultations', 'Consultations', 'portal:clinical-consultations'),
               _item('labs', 'Blood tests', 'portal:clinical-labs'),
               _item('authorisations', 'Authorisations', 'portal:treatment-authorisations'),
               _item('compounding', 'Compounding records', 'portal:compounding-list')] if clinical else []),
            _item('schedule', 'Calendar & Bookings', 'portal:staff-schedule'),
        ]),
        ('operations', 'Operations', 'Operations', [
            _item('subscriptions', 'Subscriptions', 'portal:treatment-subscriptions'),
            _item('orders', 'Supply requests', 'portal:ops-orders'),
            _item('shipping', 'Weekly shipping list', 'portal:ops-shipping'),
            _item('history', 'Dispatch history', 'portal:ops-history'),
            _item('catalogue', 'Catalogue', 'portal:ops-catalogue'),
        ] if admin else []),
        ('administration', 'Administration', 'Admin', [
            _item('leads', 'Leads', 'portal:staff-leads'),
            _item('dropouts', 'Dropouts', 'portal:dropouts'),
            _item('privacy_requests', 'Data requests', 'portal:staff-data-requests'),
        ] if admin else []),
        ('insights', 'Insights', 'Insights', [
            _item('metrics', 'Metrics and cohorts', 'portal:metrics'),
            *([_item('statements', 'Activity statements', 'portal:activity-statements')] if super_admin else []),
        ] if admin else []),
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
            if item['key'] == 'records':
                item['badge'] = unread
        menu.append({
            'key': key, 'label': label, 'short_label': short_label, 'items': items,
            'url': reverse('portal:staff-menu-section', args=[key]),
            'active': nav_section == f'section:{key}' or any(item['current'] for item in items),
            # The mobile bar shows sections only, so a section carries the unread count of a page inside it.
            'badge': sum(item['badge'] for item in items),
        })
    return menu


def build_staff_navigation(membership, *, multi_practice=False, nav_section='', unread=0):
    if uses_flat_menu(membership):
        return {'flat': build_flat_menu(membership, nav_section=nav_section, unread=unread), 'sections': []}
    return {'flat': [], 'sections': build_staff_menu(membership, multi_practice=multi_practice,
                                                     nav_section=nav_section, unread=unread)}
