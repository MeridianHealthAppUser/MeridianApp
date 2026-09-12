from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from care.models import MedicationProduct, MedicationBatch, PatientSubscription, Shipment
from practices.models import Patient


class ProductForm(forms.ModelForm):
    class Meta:
        model = MedicationProduct
        fields = ('name', 'strength', 'description', 'category', 'price', 'allowance', 'allowance_group',
                  'requires_authorisation', 'requires_cold_chain', 'is_compounded', 'is_active')
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}
        labels = {'is_active': 'Published in the patient catalogue', 'allowance_group': 'Shared allowance group (optional)'}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.company = company
        self.fields['allowance_group'].help_text = 'Products in this group share a quantity allowance. Each product still requires its own explicit authorisation.'

    def clean_price(self):
        value = self.cleaned_data['price']
        if value < 0:
            raise ValidationError('The price cannot be negative.')
        return value

    def clean_allowance_group(self):
        return self.cleaned_data['allowance_group'].strip().casefold()

    def clean(self):
        data = super().clean()
        duplicates = MedicationProduct.objects.for_company(self.instance.company).filter(
            name=data.get('name'), strength=data.get('strength', ''))
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if data.get('name') and duplicates.exists():
            raise ValidationError('This practice already has a product with this name and strength.')
        if self.instance.pk and (self.instance.authorizations.exists() or self.instance.shipment_items.exists()
                                or self.instance.order_items.exists() or self.instance.batches.exists()):
            locked_fields = {'name', 'strength', 'category', 'requires_authorisation', 'requires_cold_chain', 'is_compounded', 'allowance_group'}
            if locked_fields.intersection(self.changed_data):
                raise ValidationError('This product has clinical/supply history. Create a new product for medicine, strength or safety-rule changes. Price, description and visibility remain editable.')
        return data


class CatalogueFilterForm(forms.Form):
    q = forms.CharField(label='Search catalogue', required=False, max_length=100)
    status = forms.ChoiceField(choices=(('all', 'All products'), ('active', 'Published'), ('hidden', 'Hidden')))


class BatchReceiptForm(forms.Form):
    product = forms.ModelChoiceField(queryset=MedicationProduct.objects.none())
    batch_number = forms.CharField(max_length=80)
    received_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)
    expires_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    quantity = forms.IntegerField(min_value=1, max_value=100000)
    cold_chain_confirmed = forms.BooleanField(required=False, label='Cold chain confirmed on receipt')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = MedicationProduct.objects.for_company(company).filter(is_active=True)


class StockFilterForm(forms.Form):
    status = forms.ChoiceField(choices=(('all', 'All states'), *MedicationBatch.Status.choices))
    expiry = forms.ChoiceField(choices=(('all', 'All expiry dates'), ('soon', 'Expiring within 60 days'), ('expired', 'Expired')))


class BatchActionForm(forms.Form):
    action = forms.ChoiceField(choices=(('quarantine', 'Quarantine'), ('release', 'Release from quarantine'),
                                       ('adjust', 'Adjust stock count'), ('write_off', 'Write off remaining stock')))
    reason = forms.CharField(max_length=255, widget=forms.Textarea(attrs={'rows': 3}))
    quantity = forms.IntegerField(label='Adjustment (+ received correction / − loss)', required=False, min_value=-100000, max_value=100000)
    cold_chain_confirmed = forms.BooleanField(required=False, label='Cold-chain issue reviewed; stock confirmed safe to release')
    confirm = forms.BooleanField(label='I confirm this stock action')


class ShippingFilterForm(forms.Form):
    week = forms.DateField(label='Week beginning', widget=forms.DateInput(attrs={'type': 'date'}))
    status = forms.ChoiceField(choices=(('all', 'All shipments'), *Shipment.Status.choices))


class HistoryFilterForm(forms.Form):
    start = forms.DateField(label='From', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='Until', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end') and data['end'] < data['start']:
            raise ValidationError('The end date must not be before the start date.')
        return data


class ShipmentCreateForm(forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none())
    product = forms.ModelChoiceField(queryset=MedicationProduct.objects.none())
    quantity = forms.IntegerField(min_value=1, max_value=100)
    scheduled_for = forms.DateField(label='Planned dispatch date', widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)
    confirm = forms.BooleanField(label='Create a local shipment record only; nothing is dispatched automatically')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)
        self.fields['product'].queryset = MedicationProduct.objects.for_company(company).filter(is_active=True)

    def clean_scheduled_for(self):
        value = self.cleaned_data['scheduled_for']
        if value < timezone.localdate():
            raise ValidationError('Choose today or a future dispatch date.')
        return value


class ShipmentActionForm(forms.Form):
    action = forms.ChoiceField(choices=(('prepare', 'Prepare / re-prepare with eligible stock'), ('hold', 'Hold'),
                                       ('cancel', 'Cancel before dispatch'), ('dispatch', 'Record actual dispatch'),
                                       ('deliver', 'Record actual delivery')))
    reason = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 3}))
    tracking_number = forms.CharField(required=False, max_length=120)
    confirm = forms.BooleanField(label='I confirm this action and have checked the shipment')


class BasketQuantityForm(forms.Form):
    quantity = forms.IntegerField(min_value=0, max_value=100, initial=1, label='Quantity (0 removes the item)')


class SupplyRequestForm(forms.Form):
    line1 = forms.CharField(label='Street address', max_length=200)
    line2 = forms.CharField(label='Address line 2', required=False, max_length=200)
    city = forms.CharField(max_length=200)
    province = forms.CharField(max_length=200)
    postal_code = forms.CharField(max_length=20)
    phone = forms.CharField(label='Delivery contact number', max_length=32)
    note = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))
    confirm = forms.BooleanField(label='Request these items for practice review; no payment is collected')


class OrderReviewForm(forms.Form):
    action = forms.ChoiceField(choices=(('accept', 'Accept for preparation'), ('cancel', 'Cancel unaccepted request')))
    scheduled_for = forms.DateField(label='Planned dispatch date', required=False,
                                    widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)
    confirm = forms.BooleanField(label='I confirm this supply-request action')


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label='I confirm cancellation')
