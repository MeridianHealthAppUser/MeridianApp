"""The staff menu, defined once for the desktop sidebar, the mobile bar and the mobile section pages."""

from django.urls import reverse


def _item(key, label, url_name, *, also=()):
    return {'key': key, 'label': label, 'url': reverse(url_name), 'also': also}


def build_staff_menu(membership, *, multi_practice=False, nav_section=''):
    """Sections the member may use, each with the pages inside it and which one is current."""
    role = membership.role
    clinical = membership.has_clinical_access
    admin = role in ('practice_admin', 'super_admin')
    super_admin = role == 'super_admin'
    sections = [
        ('care', 'Care', 'Care', [
            # Clinicians limited to their own work see "My tasks".
            _item('tasks', 'My tasks' if role == 'doctor' else 'Tasks', 'portal:staff-tasks'),
            # An open patient record belongs to Patients; it does not get its own menu item.
            _item('patients', 'Patients', 'portal:patient-list', also=('record',)),
            *([_item('consultations', 'Consultations', 'portal:clinical-consultations'),
               _item('labs', 'Blood tests', 'portal:clinical-labs'),
               _item('authorisations', 'Authorisations', 'portal:treatment-authorisations'),
               _item('compounding', 'Compounding records', 'portal:compounding-list')] if clinical else []),
            _item('schedule', 'Schedule and availability', 'portal:staff-schedule'),
            _item('messages', 'Messages', 'portal:staff-inbox'),
        ]),
        ('operations', 'Operations', 'Operations', [
            _item('subscriptions', 'Subscriptions', 'portal:treatment-subscriptions'),
            _item('orders', 'Supply requests', 'portal:ops-orders'),
            _item('shipping', 'Weekly shipping list', 'portal:ops-shipping'),
            _item('history', 'Dispatch history', 'portal:ops-history'),
            _item('stock', 'Batches and stock', 'portal:ops-stock'),
            _item('catalogue', 'Catalogue', 'portal:ops-catalogue'),
        ]),
        ('administration', 'Administration', 'Admin', [
            _item('leads', 'Leads', 'portal:staff-leads'),
            _item('dropouts', 'Dropouts', 'portal:dropouts'),
            _item('privacy_requests', 'Data requests', 'portal:staff-data-requests'),
        ] if admin else []),
        ('insights', 'Insights', 'Insights', [
            _item('metrics', 'Metrics and cohorts', 'portal:metrics'),
            *([_item('statements', 'Activity statements', 'portal:activity-statements')] if clinical else []),
        ]),
        ('settings', 'Settings', 'Settings', [
            _item('review_rules', 'Review rules', 'portal:treatment-review-rules'),
            *([*([_item('practices', 'Practices', 'portal:management-practices')] if multi_practice else []),
               _item('users', 'Users and roles', 'portal:management-users'),
               _item('policies', 'Policy versions', 'portal:policy-list'),
               _item('practice_settings', 'Practice settings', 'portal:practice-settings')] if super_admin else []),
        ] if clinical else []),
    ]
    menu = []
    for key, label, short_label, items in sections:
        if not items:
            continue
        for item in items:
            item['current'] = 'page' if nav_section == item['key'] else ('true' if nav_section in item['also'] else '')
        menu.append({
            'key': key, 'label': label, 'short_label': short_label, 'items': items,
            'url': reverse('portal:staff-menu-section', args=[key]),
            'active': nav_section == f'section:{key}' or any(item['current'] for item in items),
        })
    return menu
