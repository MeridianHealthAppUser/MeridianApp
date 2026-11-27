"""The staff menu, defined once for the desktop sidebar, the mobile bar and the mobile section pages.

Clinicians and practice administrators get one flat list with icons. Super Admins get sections.
Operations and Administration are each one page of tabs (page_tabs) for whoever opens them: their menu row opens the
page, and their tabs open beside it on hover. Calendar's three tabs (schedule_tabs.html) are pages in Care for Super
Admins, and one Calendar entry, opening on Appointments, in the flat menu.
"""

from django.urls import reverse

from care.models import MedicationProduct
from care.operations import can_ship

# Each tab: its nav_section key, label and page.
# Patient orders moved to Patient Records & Messages (James, 26 Nov 2026: care and patient data more than shipping).
OPERATIONS_TABS = (
    # Weekly shipping holds what was Dispatch stock (the week's pens) since 24 Oct 2026.
    ('shipping', 'Weekly shipping', 'portal:ops-shipping'),
    ('history', 'Dispatch history', 'portal:ops-history'),
    ('billing', 'Billing', 'portal:ops-billing'),
)
ADMINISTRATION_TABS = (
    ('leads', 'Leads', 'portal:staff-leads'),
    ('dropouts', 'Dropouts', 'portal:dropouts'),
    # No Data requests tab (Dr Czech, 26 Nov 2026): patients raise none in the app, and a request comes by message.
    # The communications log: every email sent to a patient or an enquiry.
    ('emails', 'Emails', 'portal:email-log'),
)
# The Inventory page's own tabs (inventory_tabs.html), which the menu lists beside Inventory on hover.
INVENTORY_TABS = (
    ('inventory', 'Inventory', 'portal:inventory'),
    ('consumption', 'Consumption', 'portal:inventory-consumption'),
    ('catalogue', 'Catalogue', 'portal:ops-catalogue'),
)
# Insights and Settings are one page of tabs each too, for Super Admins (James, 6 Nov 2026).
INSIGHTS_TABS = (
    ('metrics', 'Insights', 'portal:metrics'),
    # Cohort retention on its own tab, the insight Dr Czech uses most (Nov 2026).
    ('cohorts', 'Cohorts', 'portal:insights-cohorts'),
    ('statements', 'Activity statements', 'portal:activity-statements'),
)
SETTINGS_TABS = (
    ('review_rules', 'Review rules', 'portal:treatment-review-rules'),
    ('users', 'Users and roles', 'portal:management-users'),
    ('screening', 'Screening questions', 'portal:screening-questions'),
    ('email_journeys', 'Email journeys', 'portal:email-journeys'),
    ('policies', 'Policy versions', 'portal:policy-list'),
    ('practice_settings', 'Practice settings', 'portal:practice-settings'),
)
# Calendar's tabs (Calendar & Bookings until Nov 2026; templates/portal/includes/schedule_tabs.html). For Super Admins
# they are three pages in Care (James, 20 Nov 2026); the flat menu has one Calendar entry opening Appointments.
# Appointment pages and booking count as Appointments ('schedule'); the Calendar page is 'calendar', or 'availability'
# on its Working hours tab.
SCHEDULE_TABS = (
    ('schedule', 'Appointments', 'portal:schedule-appointments'),
    ('calendar', 'Calendar', 'portal:staff-schedule'),
    ('availability', 'Working hours & availability', 'portal:staff-schedule'),
)
SCHEDULE_TAB_QUERY = {'availability': '?section=availability'}
TAB_GROUPS = {'operations': ('Shipping', OPERATIONS_TABS), 'administration': ('Leads and dropouts', ADMINISTRATION_TABS),
              'insights': ('Insights', INSIGHTS_TABS), 'settings': ('Settings', SETTINGS_TABS)}
# The Operations tabs a doctor has when the practice lets doctors ship (Practice settings).
SHIPPING_TABS = ('shipping', 'history')


def _item(key, label, url_name, *, also=(), short_label=None, mobile_url_name=None):
    return {'key': key, 'label': label, 'url': reverse(url_name), 'also': also, 'short_label': short_label or label,
            'mobile_url': reverse(mobile_url_name or url_name), 'badge': 0, 'badge_label': 'unread message'}


def _set_badges(items, *, unread=0):
    """Patient Records & Messages (Messages for practice administrators) counts unread messages."""
    for item in items:
        if item['key'] == 'records':
            item['badge'] = unread


def _current(item, nav_section):
    return 'page' if nav_section == item['key'] else ('true' if nav_section in item['also'] else '')


def uses_flat_menu(membership):
    """Clinicians and practice administrators get one flat list like the patient app instead of sections."""
    return membership.role in ('doctor', 'practice_admin')


def page_tabs(nav_section, *, membership=None):
    """The Operations, Administration, Insights or Settings tabs for the page at nav_section, with the current one
    marked; None for any other page. A doctor sees only Weekly shipping and Dispatch history (so no Insights tabs on
    Activity statements)."""
    for key, (label, tabs) in TAB_GROUPS.items():
        if key in ('insights', 'settings') and membership is not None and membership.role != 'super_admin':
            continue  # Super Admins' pages; anyone else allowed one of them gets just that page.
        if membership is not None and membership.role == 'doctor':
            tabs = tuple(tab for tab in tabs if tab[0] in SHIPPING_TABS)
        if nav_section in (tab[0] for tab in tabs):
            items = [dict(key=tab_key, label=tab_label, url=reverse(url_name), active=tab_key == nav_section,
                          badge=0, badge_label='')
                     for tab_key, tab_label, url_name in tabs]
            return {'key': key, 'label': label, 'tabs': items}
    return None


def _tab_items(group):
    return [_item(key, label, url_name) for key, label, url_name in TAB_GROUPS[group][1]]


def _group_item(group, short_label):
    """A group of tabs as one menu entry: it opens the first tab and stays current on all of them (and on Practices
    under Settings)."""
    label, tabs = TAB_GROUPS[group]
    extra = {'settings': ('practices',)}.get(group, ())
    return _item(group, label, tabs[0][2], also=tuple(tab[0] for tab in tabs) + extra, short_label=short_label)


# Super Admin sections that are one page of tabs: the section's own row opens that page in one click, and its tabs
# open beside it on hover.
SINGLE_PAGE_SECTIONS = ('inventory', 'operations', 'administration', 'insights', 'settings')


def _inventory_item():
    """Inventory as one menu entry: it opens the Inventory tab and stays current on Consumption and Catalogue."""
    return _item('inventory', 'Inventory', 'portal:inventory', also=tuple(tab[0] for tab in INVENTORY_TABS[1:]))


def _schedule_item():
    """Calendar as one entry in the flat menu: it opens Appointments and stays current on the Calendar and Working hours tabs,
    also after moving between them without a full page load (match: app_navigation.js)."""
    item = _item('schedule', 'Calendar', 'portal:schedule-appointments', also=tuple(tab[0] for tab in SCHEDULE_TABS[1:]))
    item['match'] = reverse('portal:staff-schedule')
    return item


def _schedule_tab_items():
    items = []
    for key, label, url_name in SCHEDULE_TABS:
        item = _item(key, label, url_name)
        item['url'] = item['mobile_url'] = item['url'] + SCHEDULE_TAB_QUERY.get(key, '')
        items.append(item)
    return items


def _section_link(key):
    return _inventory_item() if key == 'inventory' else _group_item(key, TAB_GROUPS[key][0])


RECORDS_TITLE = 'Patient Records & Messages'
MESSAGES_TITLE = 'Messages'


def messages_title(membership):
    """Messages and the patient list are one page, Patient Records & Messages. Practice administrators don't browse
    patient records, so theirs is just Messages: Patient messages and Team messages."""
    return MESSAGES_TITLE if membership.role == 'practice_admin' else RECORDS_TITLE


def _records_item(membership):
    # Patient orders (Subscriptions and Non-prescription requests) is one of its tabs (James, 26 Nov 2026).
    if membership.role == 'practice_admin':
        return _item('records', MESSAGES_TITLE, 'portal:staff-inbox', also=('messages', 'team', 'subscriptions', 'orders'))
    # All patients (the default tab), Patient orders, Patient messages and Team messages share one page; an open record
    # belongs to it too.
    return _item('records', RECORDS_TITLE, 'portal:patient-list',
                 also=('messages', 'patients', 'team', 'record', 'subscriptions', 'orders'), short_label='Records')


def build_flat_menu(membership, *, nav_section='', unread=0):
    """Overview and the member's pages, each with an icon; Patient Records & Messages (Messages for practice
    administrators) carries the unread count. Practice administrators also get Operations and Administration, each one
    page of tabs."""
    items = [
        _item('overview', 'Overview', 'portal:desktop-dashboard', mobile_url_name='portal:mobile-dashboard'),
        _item('tasks', 'My tasks', 'portal:staff-tasks', short_label='Tasks'),
        _records_item(membership),
        _schedule_item(),
    ]
    if membership.is_admin:
        items += [_item('purchasing', 'Purchasing', 'portal:purchasing', short_label='Purchase'),
                  _inventory_item(),
                  _group_item('operations', 'Shipping'), _group_item('administration', 'Leads')]
    elif membership.role == 'doctor' and can_ship(membership.company, membership):
        # Only when Practice settings lets doctors ship; off by default.
        items.append(_item('operations', 'Weekly shipping', 'portal:ops-shipping', also=SHIPPING_TABS, short_label='Shipping'))
    for item in items:
        item['current'] = _current(item, nav_section)
    _set_badges(items, unread=unread)
    return items


def build_staff_menu(membership, *, multi_practice=False, nav_section='', unread=0):
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
               _item('authorisations', 'Authorisations', 'portal:treatment-authorisations')] if clinical else []),
            # Only for a practice whose catalogue has a compounded product (Meridian has none).
            *([_item('compounding', 'Compounding records', 'portal:compounding-list')]
              if clinical and MedicationProduct.objects.filter(company=membership.company, is_compounded=True).exists() else []),
            # Calendar's three tabs, where Calendar & Bookings was (James, 20 Nov 2026).
            *_schedule_tab_items(),
        ]),
        # Procurement (James, 20 Nov 2026), with its pages Purchasing and Suppliers.
        ('purchasing', 'Procurement', 'Procurement', [
            _item('purchasing', 'Purchasing', 'portal:purchasing'),
            *([_item('suppliers', 'Suppliers', 'portal:supplier-list')] if super_admin else []),
        ] if admin else []),
        ('inventory', 'Inventory', 'Inventory', [_item(*tab) for tab in INVENTORY_TABS] if admin else []),
        ('operations', 'Shipping', 'Shipping', _tab_items('operations') if admin else []),
        ('administration', 'Leads and dropouts', 'Leads', _tab_items('administration') if admin else []),
        # Insights and Settings are for Super Admins only.
        ('insights', 'Insights', 'Insights', _tab_items('insights') if super_admin else []),
        # Practices, only when the app runs more than one, is in the menu but not among the Settings tabs.
        ('settings', 'Settings', 'Settings', [
            *_tab_items('settings'),
            *([_item('practices', 'Practices', 'portal:management-practices')] if multi_practice else []),
        ] if super_admin else []),
    ]
    menu = []
    for key, label, short_label, items in sections:
        if not items:
            continue
        # A single-page section's row is a link to that page, on the desktop menu and the mobile bar alike.
        link = _section_link(key) if key in SINGLE_PAGE_SECTIONS else None
        for item in items + ([link] if link else []):
            item['current'] = _current(item, nav_section)
        _set_badges(items + ([link] if link else []), unread=unread)
        labels = {item['badge_label'] for item in items if item['badge']}
        menu.append({
            'key': key, 'label': label, 'short_label': short_label, 'items': items, 'link': link,
            'url': link['mobile_url'] if link else reverse('portal:staff-menu-section', args=[key]),
            'active': nav_section == f'section:{key}' or any(item['current'] for item in items),
            'current': 'page' if nav_section == f'section:{key}' else (link['current'] if link else 'true'),
            # The mobile bar shows sections only, so a section carries the counts of the pages inside it.
            'badge': sum(item['badge'] for item in items),
            'badge_label': labels.pop() if len(labels) == 1 else 'waiting item',
        })
    return menu


def breadcrumb_section(membership, nav_section):
    """The menu section a page sits in, as a step in its breadcrumbs (James, 20 Nov 2026): Care or Procurement, whose
    own page is the list of their pages (Overview > Care > Blood tests). Only Super Admins have sections; a section
    that is one page of tabs (Insights, Shipping...) is already in its pages' trails."""
    if membership is None or not nav_section or nav_section.startswith('section:') or uses_flat_menu(membership):
        return None
    for section in build_staff_menu(membership, nav_section=nav_section):
        if section['link'] is None and section['active']:
            return section['label'], section['url']
    return None


def build_staff_navigation(membership, *, multi_practice=False, nav_section='', unread=0):
    if uses_flat_menu(membership):
        return {'flat': build_flat_menu(membership, nav_section=nav_section, unread=unread),
                'sections': []}
    return {'flat': [], 'sections': build_staff_menu(membership, multi_practice=multi_practice, nav_section=nav_section,
                                                     unread=unread)}
