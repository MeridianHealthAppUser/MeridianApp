"""Audited, tenant-scoped supplier maintenance; this does not place purchase orders."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from .demo_safety import require_same_demo_data

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AuditEvent, MedicationProduct, Supplier, SupplierProduct
from .operations import _lock_company
from .services import record_audit


_ACTIONS = ('supplier.created', 'supplier.updated', 'supplier.deleted')


def _key(value):
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Use a valid supplier submission key.') from None


def _text(value, label, maximum, *, required=False):
    if not isinstance(value, str):
        raise ValidationError(f'Enter a valid {label}.')
    value = value.strip()
    if required and not value:
        raise ValidationError(f'Enter {label}.')
    if len(value) > maximum:
        raise ValidationError(f'Keep {label} to {maximum} characters or fewer.')
    return value


def _supplier_id(supplier, company, *, allow_stub=False):
    if (not isinstance(supplier, Supplier) or supplier.pk is None or (supplier._state.adding and not allow_stub)
            or supplier.company_id != company.pk):
        raise ValidationError('Choose a supplier from this practice.')
    return supplier.pk


def _version(saved, expected):
    if isinstance(expected, datetime):
        expected = expected.isoformat()
    if expected != saved.updated_at.isoformat():
        raise ValidationError('This supplier changed in another tab. Reload before making changes.')


def _previous(company, actor, key, payload):
    previous = AuditEvent.objects.filter(company=company, action__in=_ACTIONS,
                                         metadata__submission_key=key).first()
    if previous is not None and (previous.actor_id != actor.pk or previous.metadata.get('request') != payload):
        raise ValidationError('This submission has already been used for a different supplier change. Reload the form.')
    return previous


def _snapshot(supplier):
    return {
        'name': supplier.name, 'contact_name': supplier.contact_name, 'email': supplier.email, 'phone': supplier.phone,
        'products': list(supplier.product_links.filter(company=supplier.company,
            product__company=supplier.company).order_by('product_id').values('product_id', 'supplier_code')),
    }


@transaction.atomic
def save_supplier(*, company, actor, supplier=None, name, contact_name='', email='', phone='',
                  products=(), submission_key, expected_updated=None, request=None):
    """Create/update a supplier and its complete product mapping atomically, once per key."""
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    supplier_id = _supplier_id(supplier, company) if supplier is not None else None
    fields = {
        'name': _text(name, 'a supplier name', 255, required=True),
        'contact_name': _text(contact_name, 'contact name', 120),
        'email': _text(email, 'email address', 254),
        'phone': _text(phone, 'phone number', 40),
    }
    if not isinstance(products, (list, tuple)) or len(products) > 100:
        raise ValidationError('Link up to 100 products to a supplier.')
    selected, codes = {}, set()
    for values in products:
        if not isinstance(values, Mapping):
            raise ValidationError('Choose a product and enter its supplier product code.')
        product = values.get('product')
        if (not isinstance(product, MedicationProduct) or product.pk is None or product._state.adding
                or product.company_id != company.pk):
            raise ValidationError('Choose products from this practice.')
        if product.pk in selected:
            raise ValidationError('Link each product only once per supplier.')
        code = _text(values.get('supplier_code'), 'the supplier product code', 100, required=True)
        if code.casefold() in codes:
            raise ValidationError('Use a different supplier product code for each linked product.')
        selected[product.pk] = code
        codes.add(code.casefold())
    product_payload = [{'product_id': pk, 'supplier_code': code} for pk, code in sorted(selected.items())]
    payload = dict(fields, supplier_id=supplier_id, products=product_payload, operation='save')
    previous = _previous(company, actor, key, payload)
    if previous is not None:
        saved = Supplier.objects.filter(company=company, pk=previous.target_id).first()
        if saved is None:
            raise ValidationError('This supplier was already saved and has since been deleted. Start a new supplier.')
        return saved

    if supplier_id is not None:
        saved = Supplier.objects.select_for_update().filter(company=company, pk=supplier_id).first()
        if saved is None:
            raise ValidationError('This supplier no longer exists. Reload the supplier list.')
        _version(saved, expected_updated if expected_updated is not None else supplier.updated_at)
        before = _snapshot(saved)
    else:
        saved, before = Supplier(company=company), None
    if Supplier.objects.filter(company=company, name__iexact=fields['name']).exclude(pk=supplier_id).exists():
        raise ValidationError('A supplier with this name already exists in this practice.')
    # Fetch persisted records rather than trusting caller-supplied product/company attributes.
    available = list(MedicationProduct.objects.select_for_update().filter(
        company=company, pk__in=selected).order_by('pk'))
    if {product.pk for product in available} != selected.keys():
        raise ValidationError('Choose products from this practice.')
    require_same_demo_data(saved, *available)
    for field, value in fields.items():
        setattr(saved, field, value)
    saved.full_clean()
    saved.save()
    saved.product_links.all().delete()
    for product_id, code in sorted(selected.items()):
        link = SupplierProduct(company=company, supplier=saved, product_id=product_id, supplier_code=code)
        link.full_clean()
        link.save()
    record_audit(company=company, actor=actor, target=saved, request=request,
        action='supplier.updated' if supplier_id is not None else 'supplier.created',
        metadata={'submission_key': key, 'request': payload, 'before': before, 'after': _snapshot(saved)})
    return saved


@transaction.atomic
def delete_supplier(*, company, actor, supplier, submission_key, expected_updated=None, request=None):
    """Delete the supplier/mappings, retaining the details in audit and leaving stock/products untouched."""
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    supplier_id = _supplier_id(supplier, company, allow_stub=True)
    payload = {'supplier_id': supplier_id, 'operation': 'delete'}
    if _previous(company, actor, key, payload) is not None:
        return None
    saved = Supplier.objects.select_for_update().filter(company=company, pk=supplier_id).first()
    if saved is None:
        raise ValidationError('This supplier no longer exists. Reload the supplier list.')
    _version(saved, expected_updated if expected_updated is not None else supplier.updated_at)
    record_audit(company=company, actor=actor, target=saved, request=request, action='supplier.deleted',
                 metadata={'submission_key': key, 'request': payload, 'before': _snapshot(saved)})
    saved.delete()
    return None
