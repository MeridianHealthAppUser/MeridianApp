"""Company-scoped quantities for placing and receiving supplier orders."""
from django import forms
from care.models import MedicationProduct, PurchaseOrderItem, Supplier


class PurchaseOrderCreateForm(forms.Form):
    supplier = forms.ModelChoiceField(label='Supplier', queryset=Supplier.objects.none())
    eta = forms.DateField(label='ETA (optional)', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].queryset = Supplier.objects.filter(company=company).order_by('name', 'pk')


class PurchaseOrderLineForm(forms.Form):
    product = forms.ModelChoiceField(queryset=MedicationProduct.objects.none(), widget=forms.HiddenInput)
    quantity = forms.IntegerField(min_value=1, max_value=2147483647)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = MedicationProduct.objects.filter(company=company, is_active=True)


class PurchaseOrderLineFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        products = [form.cleaned_data['product'].pk for form in self.forms if form.cleaned_data]
        if len(products) != len(set(products)):
            raise forms.ValidationError('Choose each product only once per purchase order.')


OrderLines = forms.formset_factory(PurchaseOrderLineForm, formset=PurchaseOrderLineFormSet,
    extra=0, min_num=1, validate_min=True, max_num=100, validate_max=True, absolute_max=100)


def order_lines(company, data=None):
    return OrderLines(data, prefix='items', form_kwargs={'company': company})


class PurchaseOrderReceiptLineForm(forms.Form):
    item = forms.ModelChoiceField(queryset=PurchaseOrderItem.objects.none(), widget=forms.HiddenInput)
    quantity = forms.IntegerField(label='Receive now', required=False, min_value=0, max_value=2147483647)
    batch = forms.CharField(required=False, max_length=120)
    expires_on = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, company, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['item'].queryset = PurchaseOrderItem.objects.filter(company=company,
            purchase_order=order, purchase_order__company=company, product__company=company)



class PurchaseOrderReceiptFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        lines = [form.cleaned_data for form in self.forms if form.cleaned_data.get('quantity')]
        if not lines:
            raise forms.ValidationError('Enter a quantity for at least one product to receive.')
        ids = [line['item'].pk for line in lines]
        if len(ids) != len(set(ids)):
            raise forms.ValidationError('Receive each product only once in this delivery.')


ReceiptLines = forms.formset_factory(PurchaseOrderReceiptLineForm, formset=PurchaseOrderReceiptFormSet,
    extra=0, max_num=100, validate_max=True, absolute_max=100)


def receipt_lines(company, order, data=None, *, items=None):
    initial = None if data is not None else [{'item': item.pk, 'quantity': item.quantity - item.quantity_received}
        for item in (items if items is not None else order.items.all()) if item.quantity > item.quantity_received]
    return ReceiptLines(data, prefix='receipt', initial=initial, form_kwargs={'company': company, 'order': order})
