"""Audited, tenant-scoped supplier maintenance; this does not place purchase orders."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
import re
from uuid import UUID

from .demo_safety import require_same_demo_data

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import PROVINCES, AuditEvent, MedicationProduct, Supplier, SupplierProduct
from .operations import _lock_company
from .services import record_audit


_ACTIONS = ('supplier.created', 'supplier.updated', 'supplier.deleted')
# The details a purchase order prints about a supplier, besides its name. save_supplier takes each as a keyword.
SUPPLIER_DETAIL_FIELDS = (
    'contact_name', 'email', 'phone', 'registered_name', 'registration_number', 'vat_registered', 'vat_number',
    'address_street', 'address_suburb', 'address_city', 'address_province', 'address_postal_code',
    'accounts_email', 'payment_terms', 'account_number', 'sahpra_licence_number', 'sahpra_licence_expiry',
    'uses_own_order_form',
)
# The supplier form requires these (plus a VAT number while VAT-registered); a blank one marks the supplier incomplete.
_REQUIRED_DETAILS = ('registered_name', 'address_street', 'address_city', 'address_province', 'address_postal_code',
                     'email', 'payment_terms')
_VAT_NUMBER = re.compile(r'4\d{9}')
_PROVINCE_CODES = {code for code, _label in PROVINCES}


def normalise_vat_number(value):
    """A South African VAT number: ten digits starting with 4. Spaces are ignored; blank stays blank."""
    if not isinstance(value, str):
        raise ValidationError('Enter a 10-digit VAT number starting with 4.')
    value = re.sub(r'\s+', '', value)
    if value and not _VAT_NUMBER.fullmatch(value):
        raise ValidationError('Enter a 10-digit VAT number starting with 4.')
    return value


def supplier_flags(supplier, today):
    """What the supplier list warns about: details a purchase order needs are missing, or the licence has expired."""
    incomplete = any(not getattr(supplier, field) for field in _REQUIRED_DETAILS) or (
        supplier.vat_registered and not supplier.vat_number)
    expiry = supplier.sahpra_licence_expiry
    return {'incomplete': incomplete, 'licence_expired': expiry is not None and expiry < today}


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


def _json(value):
    return value.isoformat() if isinstance(value, date) else value


def _price_text(value):
    return None if value is None else str(value)


def _snapshot(supplier):
    links = supplier.product_links.filter(company=supplier.company, product__company=supplier.company).order_by(
        'product_id').values('product_id', 'supplier_code', 'description', 'pack_size', 'unit_price')
    return {
        'name': supplier.name, **{field: _json(getattr(supplier, field)) for field in SUPPLIER_DETAIL_FIELDS},
        'products': [dict(link, unit_price=_price_text(link['unit_price'])) for link in links],
    }


def _unit_price(value):
    """A supplier's unit price excl. VAT: blank (None), or rands and cents from 0 to 99 999 999.99."""
    if value is None:
        return None
    if (not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value >= Decimal('100000000')
            or value.as_tuple().exponent < -2):
        raise ValidationError('Enter a unit price in rands and cents, from R 0.00, or leave it blank.')
    return value.quantize(Decimal('0.01'))


def _details(values):
    """Type, length and format checks for the supplier's details. What is required is the supplier form's rule."""
    fields = {
        'contact_name': _text(values['contact_name'], 'contact name', 120),
        'email': _text(values['email'], 'email address', 254),
        'phone': _text(values['phone'], 'phone number', 40),
        'registered_name': _text(values['registered_name'], 'the registered company name', 255),
        'registration_number': _text(values['registration_number'], 'the company registration number', 40),
        'address_street': _text(values['address_street'], 'the street address', 200),
        'address_suburb': _text(values['address_suburb'], 'the suburb', 120),
        'address_city': _text(values['address_city'], 'the city or town', 120),
        'address_province': _text(values['address_province'], 'the province', 3),
        'address_postal_code': _text(values['address_postal_code'], 'the postal code', 10),
        'accounts_email': _text(values['accounts_email'], 'accounts email address', 254),
        'payment_terms': _text(values['payment_terms'], 'the payment terms', 120),
        'account_number': _text(values['account_number'], 'our account number', 40),
        'sahpra_licence_number': _text(values['sahpra_licence_number'], 'the SAHPRA licence number', 40),
    }
    if fields['address_province'] and fields['address_province'] not in _PROVINCE_CODES:
        raise ValidationError('Choose a South African province.')
    for flag in ('vat_registered', 'uses_own_order_form'):
        if not isinstance(values[flag], bool):
            raise ValidationError('Choose yes or no for each supplier setting.')
        fields[flag] = values[flag]
    vat_number = normalise_vat_number(values['vat_number'])
    fields['vat_number'] = vat_number if fields['vat_registered'] else ''
    expiry = values['sahpra_licence_expiry']
    if expiry is not None and (not isinstance(expiry, date) or isinstance(expiry, datetime)):
        raise ValidationError('Choose a valid licence expiry date, or leave it blank.')
    fields['sahpra_licence_expiry'] = expiry
    return fields


@transaction.atomic
def save_supplier(*, company, actor, supplier=None, name, contact_name='', email='', phone='',
                  registered_name='', registration_number='', vat_registered=True, vat_number='',
                  address_street='', address_suburb='', address_city='', address_province='', address_postal_code='',
                  accounts_email='', payment_terms='', account_number='', sahpra_licence_number='',
                  sahpra_licence_expiry=None, uses_own_order_form=False,
                  products=(), submission_key, expected_updated=None, request=None):
    """Create/update a supplier and its complete product mapping atomically, once per key."""
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    supplier_id = _supplier_id(supplier, company) if supplier is not None else None
    fields = {'name': _text(name, 'a supplier name', 255, required=True), **_details(dict(
        contact_name=contact_name, email=email, phone=phone, registered_name=registered_name,
        registration_number=registration_number, vat_registered=vat_registered, vat_number=vat_number,
        address_street=address_street, address_suburb=address_suburb, address_city=address_city,
        address_province=address_province, address_postal_code=address_postal_code, accounts_email=accounts_email,
        payment_terms=payment_terms, account_number=account_number, sahpra_licence_number=sahpra_licence_number,
        sahpra_licence_expiry=sahpra_licence_expiry, uses_own_order_form=uses_own_order_form))}
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
        selected[product.pk] = {
            'supplier_code': code,
            'description': _text(values.get('description', ''), 'the supplier description', 255),
            'pack_size': _text(values.get('pack_size', ''), 'the size', 40),
            'unit_price': _unit_price(values.get('unit_price')),
        }
        codes.add(code.casefold())
    product_payload = [{'product_id': pk, **line, 'unit_price': _price_text(line['unit_price'])}
                       for pk, line in sorted(selected.items())]
    payload = dict({field: _json(value) for field, value in fields.items()}, supplier_id=supplier_id,
                   products=product_payload, operation='save')
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
    for product_id, line in sorted(selected.items()):
        link = SupplierProduct(company=company, supplier=saved, product_id=product_id, **line)
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
