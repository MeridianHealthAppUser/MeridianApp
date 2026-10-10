"""Small presentation helpers; public pages retain their own design."""
from django import template
from django.urls import reverse
from django.utils import dateformat, timezone

register = template.Library()


def _message_counts(context):
    """Unread patient and team messages for the signed-in member, worked out once per request."""
    request = context.get('request')
    membership = context.get('active_membership')
    if request is None or membership is None or not request.user.is_authenticated:
        return {'patient_unread_total': 0, 'team_unread_total': 0}
    if not hasattr(request, '_staff_message_counts'):
        from portal.inbox import message_tab_counts

        request._staff_message_counts = message_tab_counts(membership.company, request.user)
    return request._staff_message_counts


def staff_unread_messages(context):
    counts = _message_counts(context)
    return counts['patient_unread_total'] + counts['team_unread_total']


@register.simple_tag(takes_context=True)
def message_counts(context):
    """The unread counts for the Messages and Team tabs."""
    return _message_counts(context)


@register.simple_tag(takes_context=True)
def staff_menu(context):
    """The member's menu: a flat list for clinicians, sections for everyone else, with the current page marked."""
    membership = context.get('active_membership')
    if membership is None:
        return {'flat': [], 'sections': []}
    from portal.navigation import build_staff_navigation

    return build_staff_navigation(membership, multi_practice=context.get('multi_practice_enabled', False),
                                  nav_section=context.get('nav_section') or '', unread=staff_unread_messages(context))


@register.inclusion_tag('includes/breadcrumbs.html', takes_context=True)
def breadcrumbs(context, *trail):
    """Overview (staff) or Home (patients), then label/link pairs, ending with this page's label.

    A link is a URL name such as 'portal:staff-leads' or an already resolved path.
    The back arrow returns to the last linked crumb, one level up.
    """
    *pairs, current = trail
    if len(pairs) % 2:
        raise template.TemplateSyntaxError('breadcrumbs takes label/link pairs followed by the current page label.')
    is_patient = bool(context.get('is_patient_portal') or
                      (not context.get('active_company') and context.get('active_patient')))
    items = [('Home', reverse('portal:patient-dashboard')) if is_patient else ('Overview', reverse('portal:desktop-dashboard'))]
    for label, link in zip(pairs[::2], pairs[1::2]):
        items.append((label, link if str(link).startswith('/') else reverse(link)))
    return {'items': items, 'current': current, 'back': items[-1]}


@register.simple_tag(takes_context=True)
def workspace_page(context):
    request = context.get('request')
    if request is None or not getattr(request.user, 'is_authenticated', False):
        return False
    match = request.resolver_match
    return bool(match and match.namespace == 'portal' and
                not (match.url_name or '').startswith(('public-', 'questionnaire')))


@register.filter
def metric_label(value):
    if value == 'appointments':
        return 'Appointments today'
    return str(value).replace('_', ' ').capitalize()


@register.filter
def kg(value):
    from portal.patient_summary import format_kg
    return format_kg(value)


@register.filter
def list_when(value):
    """A conversation list's time column: the time for today, otherwise the date (with the year if not this one)."""
    if not value:
        return ''
    local, today = timezone.localtime(value), timezone.localdate()
    if local.date() == today:
        return dateformat.format(local, 'H:i')
    return dateformat.format(local, 'j M' if local.year == today.year else 'j M Y')


@register.simple_tag(takes_context=True)
def account_identity(context):
    """Display the current portal role without another membership lookup."""
    user = context.get('user')
    if user is None or not user.is_authenticated:
        return {}
    is_patient = bool(context.get('is_patient_portal') or
                      (not context.get('active_company') and context.get('active_patient')))
    membership = context.get('active_membership')
    role = 'Patient' if is_patient else membership.title if membership else 'Account'
    initials = ''.join(part[:1] for part in (user.first_name.strip(), user.last_name.strip()) if part).upper()
    return {'name': user.full_name, 'initials': initials or user.email[:1].upper(), 'role': role,
            'is_patient': is_patient}
