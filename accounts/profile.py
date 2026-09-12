"""Self-service account names, without changing login identity or clinical records."""

import json

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.crypto import constant_time_compare, salted_hmac


PROFILE_CONTEXT_SALT = 'accounts.profile.form.v1'
PROFILE_CONTEXT_MAX_AGE = 12 * 60 * 60
PROFILE_CONTEXT_ERROR = (
    'This profile form has expired or your account details have changed. '
    'Reload My profile before saving again. No changes were saved.'
)


def _profile_version(user):
    # The signed form contains an opaque digest, not names or a login address.
    values = json.dumps([user.first_name, user.last_name, user.email])
    return salted_hmac('accounts.profile.version.v1', values).hexdigest()


def make_profile_context(user):
    return signing.dumps({'user_id': user.pk, 'version': _profile_version(user)}, salt=PROFILE_CONTEXT_SALT)


@transaction.atomic
def save_own_profile(*, actor, context_token, first_name, last_name, request=None):
    """Refresh and lock the session's own user; never accept a submitted target ID."""
    if not getattr(actor, 'is_authenticated', False):
        raise PermissionDenied
    user = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if user is None:
        raise PermissionDenied
    try:
        context = signing.loads(context_token or '', salt=PROFILE_CONTEXT_SALT, max_age=PROFILE_CONTEXT_MAX_AGE)
        if (not isinstance(context, dict) or context.get('user_id') != user.pk or
                not isinstance(context.get('version'), str) or
                not constant_time_compare(context['version'], _profile_version(user))):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError(PROFILE_CONTEXT_ERROR) from None

    changes = {}
    for field_name, value in (('first_name', first_name), ('last_name', last_name)):
        clean_value = user._meta.get_field(field_name).clean(value, user)
        if getattr(user, field_name) != clean_value:
            changes[field_name] = clean_value
    if not changes:
        return user, False
    for field_name, value in changes.items():
        setattr(user, field_name, value)
    user.save(update_fields=tuple(changes))

    from care.services import record_audit
    from practices.models import Company

    companies = Company.objects.filter(is_active=True).filter(
        Q(memberships__user=user, memberships__is_active=True) |
        Q(practices_patient_records__user=user, practices_patient_records__is_active=True)
    ).distinct()
    for company in companies:
        record_audit(company=company, actor=user, action='account.profile_updated', target=user,
                     request=request, metadata={'fields': sorted(changes)})
    return user, True
