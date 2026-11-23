"""Supplier details and the practice-scoped products they can supply."""
from django import forms
from django.core.exceptions import ValidationError

from care.models import PROVINCES, MedicationProduct
from care.suppliers import SUPPLIER_DETAIL_FIELDS, normalise_vat_number


class SupplierSearchForm(forms.Form):
    q = forms.CharField(label='Search suppliers', required=False, max_length=100)


# What each product row saves besides the product itself (care.suppliers.save_supplier).
PRODUCT_LINK_FIELDS = ('supplier_code', 'description', 'pack_size', 'unit_price')
ORDER_FORM_CHOICES = (('false', 'Meridian-generated PDF'), ('true', 'Supplier-generated PDF'))


class SupplierForm(forms.Form):
    """What a purchase order needs about a supplier. The service checks formats; this form decides what is required."""

    name = forms.CharField(label='Supplier name', max_length=255)
    registered_name = forms.CharField(label='Registered company name', max_length=255)
    registration_number = forms.CharField(label='Company registration number', required=False, max_length=40,
                                          help_text='Leave blank for a sole proprietor.')
    vat_registered = forms.BooleanField(label='VAT-registered', required=False, initial=True)
    vat_number = forms.CharField(label='VAT number', required=False, max_length=20)
    address_street = forms.CharField(label='Street address', max_length=200)
    address_suburb = forms.CharField(label='Suburb', required=False, max_length=120)
    address_city = forms.CharField(label='City or town', max_length=120)
    address_province = forms.ChoiceField(label='Province', choices=(('', 'Choose a province'), *PROVINCES))
    address_postal_code = forms.CharField(label='Postal code', max_length=10)
    email = forms.EmailField(label='Orders email', max_length=254, help_text='Purchase orders are emailed here.')
    accounts_email = forms.EmailField(label='Accounts email', required=False, max_length=254)
    contact_name = forms.CharField(label='Contact person', required=False, max_length=120)
    phone = forms.CharField(label='Phone', required=False, max_length=40)
    payment_terms = forms.CharField(label='Payment terms', max_length=120,
                                    help_text='For example: 30 days from statement.')
    account_number = forms.CharField(label='Our account number', required=False, max_length=40,
                                     help_text='The account number this supplier gave the practice.')
    sahpra_licence_number = forms.CharField(label='SAHPRA licence number', required=False, max_length=40)
    sahpra_licence_expiry = forms.DateField(label='Licence expiry', required=False,
                                            widget=forms.DateInput(attrs={'type': 'date'}))
    uses_own_order_form = forms.TypedChoiceField(label='Purchase order PDF', choices=ORDER_FORM_CHOICES,
        coerce=lambda value: value == 'true', initial='false', widget=forms.RadioSelect)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('vat_registered'):
            try:
                cleaned['vat_number'] = normalise_vat_number(cleaned.get('vat_number', ''))
            except ValidationError as error:
                self.add_error('vat_number', error)
            else:
                if not cleaned['vat_number']:
                    self.add_error('vat_number', 'Enter their VAT number, or untick VAT-registered.')
        else:
            cleaned['vat_number'] = ''
        return cleaned


def supplier_initial(supplier):
    """The supplier form's values for a saved supplier, with the order-form choice as its radio value."""
    initial = {field: getattr(supplier, field) for field in ('name', *SUPPLIER_DETAIL_FIELDS)}
    initial['uses_own_order_form'] = 'true' if supplier.uses_own_order_form else 'false'
    return initial


def require_licence(form, formset):
    """A supplier of prescription products needs a SAHPRA licence number and its expiry on record."""
    prescription = any(line.cleaned_data.get('product') is not None
                       and line.cleaned_data['product'].requires_authorisation
                       for line in formset if line.cleaned_data and not line.cleaned_data.get('DELETE'))
    if not prescription:
        return
    if not form.cleaned_data.get('sahpra_licence_number', '').strip():
        form.add_error('sahpra_licence_number', 'Enter their SAHPRA licence number: they supply prescription medicines.')
    if form.cleaned_data.get('sahpra_licence_expiry') is None and 'sahpra_licence_expiry' not in form.errors:
        form.add_error('sahpra_licence_expiry', 'Enter when their SAHPRA licence expires.')


class SupplierProductForm(forms.Form):
    product = forms.ModelChoiceField(label='Product', queryset=MedicationProduct.objects.none())
    supplier_code = forms.CharField(label='Supplier product code', max_length=100)
    description = forms.CharField(label='Supplier description', required=False, max_length=255,
                                  widget=forms.TextInput(attrs={'placeholder': 'As on their price list'}))
    pack_size = forms.CharField(label='Size', required=False, max_length=40,
                                widget=forms.TextInput(attrs={'placeholder': 'e.g. 2,4ml'}))
    unit_price = forms.DecimalField(label='Unit price excl. VAT (R)', required=False, min_value=0, max_digits=10,
                                    decimal_places=2, widget=forms.NumberInput(attrs={
                                        'step': '0.01', 'min': '0', 'inputmode': 'decimal',
                                        'placeholder': 'Reference price'}))

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        # Hidden products remain selectable so their supplier references can still be maintained.
        self.fields['product'].queryset = MedicationProduct.objects.filter(company=company).order_by('name', 'strength', 'pk')


class SupplierProductFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [form.cleaned_data for form in self.forms
                if form.cleaned_data and not form.cleaned_data.get('DELETE')]
        products = [row['product'].pk for row in rows]
        if len(products) != len(set(products)):
            raise forms.ValidationError('Add each product only once for this supplier.')
        codes = [row['supplier_code'].casefold() for row in rows if row['supplier_code']]
        if len(codes) != len(set(codes)):
            raise forms.ValidationError('Use a different supplier product code for each product.')


SupplierProducts = forms.formset_factory(SupplierProductForm, formset=SupplierProductFormSet,
    extra=1, min_num=0, max_num=100, validate_max=True, absolute_max=100, can_delete=True)


def supplier_product_forms(company, data=None, *, supplier=None):
    initial = []
    # The browser submits the complete replacement list with INITIAL_FORMS=0.
    # Seeding persisted initial values on a bound formset would classify an
    # unchanged product row as an empty extra and silently discard that link.
    if supplier is not None and data is None:
        initial = list(supplier.product_links.filter(company=company, product__company=company).order_by('pk').values(
            'product', *PRODUCT_LINK_FIELDS))
    return SupplierProducts(data, initial=initial, prefix='products', form_kwargs={'company': company})
