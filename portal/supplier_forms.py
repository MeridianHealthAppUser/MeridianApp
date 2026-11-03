"""Supplier details and the practice-scoped products they can supply."""
from django import forms

from care.models import MedicationProduct


class SupplierSearchForm(forms.Form):
    q = forms.CharField(label='Search suppliers', required=False, max_length=100)


class SupplierForm(forms.Form):
    name = forms.CharField(label='Supplier name', max_length=255)
    contact_name = forms.CharField(label='Contact person', required=False, max_length=120)
    email = forms.EmailField(label='Email', required=False, max_length=254)
    phone = forms.CharField(label='Phone', required=False, max_length=40)


class SupplierProductForm(forms.Form):
    product = forms.ModelChoiceField(label='Product', queryset=MedicationProduct.objects.none())
    supplier_code = forms.CharField(label='Supplier product code', max_length=100)

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
            'product', 'supplier_code'))
    return SupplierProducts(data, initial=initial, prefix='products', form_kwargs={'company': company})
