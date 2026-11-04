"""Once-off items on My Treatment: what a patient can buy without a prescription, such as creatine sachets.

The basket, ordering and paying are in operations_views (PatientBasketItemView, PatientBasketView and
PatientOrderDetailView); care.pharmacy keeps the rules, and a paid order goes onto Weekly shipping by itself.
"""

from care.models import MedicationProduct, PharmacyOrder
from care.operations import product_allowance

from .operations_forms import BasketQuantityForm
from .workflow_context import make_workflow_context


def shop_products(company, patient):
    """Active items that need no prescription and have a price, that this patient can order."""
    products = MedicationProduct.objects.for_company(company).filter(
        is_active=True, requires_authorisation=False, price__gt=0).order_by('name', 'strength', 'pk')
    # The allowance check also keeps demo products to demo patients.
    return [product for product in products if not product_allowance(patient, product)[2]]


def shop_context(request, company, patient):
    """The Once-off items section: each item with its own add form, and the basket count."""
    basket = PharmacyOrder.objects.filter(company=company, patient=patient, status=PharmacyOrder.Status.DRAFT).first()
    in_basket = {item.product_id: item.quantity for item in basket.items.all()} if basket else {}
    products = shop_products(company, patient)
    for product in products:
        product.in_basket = in_basket.get(product.pk, 0)
        product.quantity_form = BasketQuantityForm(initial={'quantity': product.in_basket or 1}, auto_id=f'product_{product.pk}_%s')
        product.workflow_context = make_workflow_context(request, company, 'basket-product', product, patient=patient)
    return {
        'shop_products': products, 'basket_count': sum(in_basket.values()),
        'has_orders': PharmacyOrder.objects.filter(company=company, patient=patient).exclude(
            status=PharmacyOrder.Status.DRAFT).exists(),
    }


def order_address(address):
    """The delivery address on file, as an order keeps it (care.pharmacy.submit_basket)."""
    return {'line1': address.street, 'line2': address.suburb, 'city': address.city, 'province': address.province,
            'postal_code': address.postal_code, 'phone': address.phone}
