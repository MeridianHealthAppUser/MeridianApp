"""Shared signed intent for tenant-scoped operational forms."""

import uuid

from django.core import signing
from django.core.exceptions import ValidationError


SALT = 'portal.operational-context.v1'


def make_workflow_context(request, company, kind, record=None, *, patient=None):
    return signing.dumps({
        'actor': request.user.pk, 'company': company.pk, 'kind': kind,
        'record': record.pk if record else None, 'patient': patient.pk if patient else None,
        'updated': record.updated_at.isoformat() if record else None, 'key': str(uuid.uuid4()),
    }, salt=SALT)


def validate_workflow_context(request, company, kind, record=None, *, patient=None, check_version=True):
    try:
        token = request.POST.get('workflow_context', '')
        if not token or len(token) > 4096:
            raise ValueError
        data = signing.loads(token, salt=SALT, max_age=12 * 60 * 60)
        expected = {'actor': request.user.pk, 'company': company.pk, 'kind': kind,
                    'record': record.pk if record else None, 'patient': patient.pk if patient else None}
        if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
            raise ValueError
        if check_version and record and data.get('updated') != record.updated_at.isoformat():
            raise ValidationError('This record changed in another tab. Reload before making changes.')
        uuid.UUID(data['key'])
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise ValidationError('The form expired or its practice/record changed. Reload and check the selected practice.') from None
    return data
