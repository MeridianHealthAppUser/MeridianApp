from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone

from care.models import MedicationDose, MedicationProduct, PatientSubscription, PharmacyOrder, Shipment
from practices.models import Patient

from .filter_menus import TickedChoiceField


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
        self.fields['strength'].help_text = 'For a main medicine, the pen: e.g. 0.25 mg. The doses a doctor prescribes are kept below the products.'

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
                                or self.instance.order_items.exists()):
            locked_fields = {'name', 'strength', 'category', 'requires_authorisation', 'requires_cold_chain', 'is_compounded', 'allowance_group'}
            if locked_fields.intersection(self.changed_data):
                raise ValidationError('This product has clinical/supply history. Create a new product for medicine, strength or safety-rule changes. Price, description and visibility remain editable.')
        return data


class DoseForm(forms.ModelForm):
    """A dose in the Pharmacy catalogue. Once prescribed, what is taken and what is sent stay fixed: a change to
    those is a new dose. Price, billing, tier and whether it is offered remain editable."""

    LOCKED = ('pen', 'amount_mg', 'frequency', 'pens_per_dispatch', 'dispatch_weeks')

    class Meta:
        model = MedicationDose
        fields = ('pen', 'amount_mg', 'frequency', 'pens_per_dispatch', 'dispatch_weeks', 'price', 'billing_weeks',
                  'tier', 'is_active')
        labels = {
            'pen': 'Pen', 'amount_mg': 'Dose (mg)', 'frequency': 'How often a dose is taken',
            'pens_per_dispatch': 'Pens per parcel', 'dispatch_weeks': 'Sent every (weeks)', 'price': 'Price (R)',
            'billing_weeks': 'Billed every (weeks)', 'is_active': 'Offered to doctors',
        }
        help_texts = {
            'pen': 'The catalogue product the dispensary sends for this dose. Several doses can share a pen.',
            'dispatch_weeks': 'How long one parcel lasts. The dispensary can send the pens above once in each of these periods.',
            'price': 'What the patient pays each billing period. Leave blank until it is known: the dose can’t be prescribed without a price.',
            'tier': 'Step-up and only-if-necessary doses are marked when a doctor chooses a dose.',
        }

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.company = company
        pens = MedicationProduct.objects.for_company(company).filter(requires_authorisation=True, is_active=True)
        if self.instance.pen_id:
            pens = pens | MedicationProduct.objects.filter(pk=self.instance.pen_id)
        self.fields['pen'].queryset = pens.distinct().order_by('name', 'strength', 'pk')
        # 7.5, not 7.500: browsers with a decimal comma would show that as 7,500.
        if self.instance.pk:
            self.initial['amount_mg'] = Decimal(self.instance.amount_text)
        for name, limit in (('pens_per_dispatch', 100), ('dispatch_weeks', 26), ('billing_weeks', 26)):
            self.fields[name].widget.attrs.update(min=1, max=limit)
            self.fields[name].min_value, self.fields[name].max_value = 1, limit
            self.fields[name].validators += [MinValueValidator(1), MaxValueValidator(limit)]

    def clean_amount_mg(self):
        value = self.cleaned_data['amount_mg']
        if value is not None and value <= 0:
            raise ValidationError('Enter a dose above 0 mg.')
        return value

    def clean_price(self):
        value = self.cleaned_data['price']
        if value is not None and value < 0:
            raise ValidationError('The price cannot be negative.')
        return value

    def clean(self):
        data = super().clean()
        pen, amount, frequency = data.get('pen'), data.get('amount_mg'), data.get('frequency')
        # A medicine offers each dose once. A hidden one can be replaced, e.g. by the same dose in another pen.
        if pen and amount is not None and frequency and data.get('is_active'):
            duplicates = MedicationDose.objects.for_company(self.instance.company).filter(
                pen__name=pen.name, amount_mg=amount, frequency=frequency, is_active=True)
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise ValidationError(f'{pen.name} already offers this dose. Edit that one, or hide it first to replace it.')
        if self.instance.pk and set(self.LOCKED).intersection(self.changed_data) and (
                self.instance.authorizations.exists() or self.instance.starting_plan_steps.exists()
                or self.instance.starting_plans_ending.exists()):
            raise ValidationError('This dose has been prescribed or is in a starting plan, so its pen, amount and supply '
                                  'stay as they are. Add a new dose for those changes. Price, billing, tier and whether it '
                                  'is offered remain editable.')
        return data


# Each Operations list has a search at the top right of its table and a Filters menu before it (filter_menus.list_tools).
class CatalogueFilterForm(forms.Form):
    q = forms.CharField(label='Search catalogue', required=False, max_length=100)
    status = TickedChoiceField(label='Visibility', choices=(('active', 'Published'), ('hidden', 'Hidden')))


class OrderFilterForm(forms.Form):
    q = forms.CharField(label='Search non-prescription requests', required=False, max_length=100)
    status = TickedChoiceField(label='Status', choices=[choice for choice in PharmacyOrder.Status.choices
                                                         if choice[0] != PharmacyOrder.Status.DRAFT])


class ShippingFilterForm(forms.Form):
    q = forms.CharField(label='Search shipments', required=False, max_length=100)
    week = forms.DateField(label='Week beginning', widget=forms.DateInput(attrs={'type': 'date'}))
    status = TickedChoiceField(label='Status', choices=Shipment.Status.choices)


class HistoryFilterForm(forms.Form):
    q = forms.CharField(label='Search dispatches', required=False, max_length=100)
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
    confirm = forms.BooleanField(label='Add this as a draft parcel; nothing is dispatched automatically')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)
        self.fields['product'].queryset = MedicationProduct.objects.for_company(company).filter(is_active=True)

    def clean_scheduled_for(self):
        value = self.cleaned_data['scheduled_for']
        if value < timezone.localdate():
            raise ValidationError('Choose today or a future dispatch date.')
        return value


class ShipmentChangeForm(forms.Form):
    """When a parcel that isn't locked goes out, and how many pens each line has."""

    scheduled_for = forms.DateField(label='Planned dispatch date', widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, items=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.items = list(items)
        for item in self.items:
            self.fields[f'pens_{item.pk}'] = forms.IntegerField(
                label=f'Pens: {item.product}', min_value=1, max_value=100, initial=item.quantity)

    def quantities(self):
        return {item.pk: self.cleaned_data[f'pens_{item.pk}'] for item in self.items}


class ShipmentActionForm(forms.Form):
    action = forms.ChoiceField(choices=(('prepare', 'Mark ready'),
                                       ('hold', 'Hold'), ('cancel', 'Cancel before dispatch'),
                                       ('dispatch', 'Record actual dispatch'), ('deliver', 'Record actual delivery')))
    reason = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 3}))
    tracking_number = forms.CharField(required=False, max_length=120)
    confirm = forms.BooleanField(label='I confirm this action and have checked the shipment')

    def __init__(self, *args, items=(), **kwargs):
        super().__init__(*args, **kwargs)
        # An optional batch and expiry for each line, noted when the parcel is dispatched, for tracing a recall.
        self.items = list(items)
        for item in self.items:
            self.fields[f'batch_{item.pk}'] = forms.CharField(
                label=f'Batch and expiry: {item.product} (optional)', required=False, max_length=120, initial=item.batch_note,
                help_text='Recorded with the dispatch, e.g. SG-2604, expiry 04/2027.')

    def batch_notes(self):
        return {item.pk: self.cleaned_data.get(f'batch_{item.pk}', '') for item in self.items}


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
