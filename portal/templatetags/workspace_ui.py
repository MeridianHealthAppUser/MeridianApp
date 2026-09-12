"""Small presentation helpers; public pages retain their own design."""
from django import template

register = template.Library()


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


@register.simple_tag(takes_context=True)
def account_identity(context):
    """Display the current portal role without another membership lookup."""
    user = context.get('user')
    if user is None or not user.is_authenticated:
        return {}
    is_patient = bool(context.get('is_patient_portal') or
                      (not context.get('active_company') and context.get('active_patient')))
    membership = context.get('active_membership')
    role = 'Patient' if is_patient else membership.get_role_display() if membership else 'Account'
    initials = ''.join(part[:1] for part in (user.first_name.strip(), user.last_name.strip()) if part).upper()
    return {'name': user.full_name, 'initials': initials or user.email[:1].upper(), 'role': role,
            'is_patient': is_patient}
