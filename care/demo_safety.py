"""Permanent isolation of managed demonstration records from live integrations.

The ownership flag is persisted, not inferred from a person's name or email. Reads at
integration boundaries deliberately recheck the database so stale queued work is safe.
"""

from functools import wraps
from inspect import signature

from django.core.exceptions import ValidationError


def is_demo_record(record):
    if record is None:
        return False
    if hasattr(record, 'demo_data_set_id'):
        if record.pk is None or record._state.adding:
            return record.demo_data_set_id is not None
        return type(record).objects.filter(pk=record.pk, demo_data_set__isnull=False).exists()
    patient_id = getattr(record, 'patient_id', None)
    if patient_id:
        from practices.models import Patient

        return Patient.objects.filter(pk=patient_id, demo_data_set__isnull=False).exists()
    return False


def require_same_demo_data(*records):
    """All live, or all from one demo run; ignore caller-cached ownership values."""
    roots = [record for record in records if record is not None]
    ownership, persisted = set(), {}
    for record in roots:
        if record.pk is None or record._state.adding:
            ownership.add(record.demo_data_set_id)
        else:
            persisted.setdefault(type(record), set()).add(record.pk)
    for model, keys in persisted.items():
        rows = dict(model.objects.filter(pk__in=keys).values_list('pk', 'demo_data_set_id'))
        if rows.keys() != keys:
            raise ValidationError('A record no longer exists. Refresh before continuing.')
        ownership.update(rows.values())
    if len(ownership) > 1:
        raise ValidationError('Demo records can only be used with products and suppliers from the same demo data set.')
    return next(iter(ownership), None)


def block_demo_billing(*record_names):
    """Guard manual/provider entry points before creating any financial record or HTTP call."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def guarded(*args, **kwargs):
            values = parameters.bind(*args, **kwargs).arguments
            if any(is_demo_record(values.get(name)) for name in record_names):
                raise ValidationError('Billing is disabled for demo data. Demo parcel payment is simulated.')
            return function(*args, **kwargs)

        return guarded
    return decorate
