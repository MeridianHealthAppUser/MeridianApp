from decimal import Decimal
import re

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone

from care.models import PROVINCES, MedicationDose, MedicationProduct, PharmacyOrder, Shipment
from practices.models import CompanyMembership, Patient

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

    # A dose that has a price changes it from a date (care.dose_prices): today, or later to keep the current price
    # until then.
    price_from = forms.DateField(
        label='New price applies from', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
        help_text='Today changes the price now. A later date keeps the current price until then: billing periods that '
                  'start on or after it use the new price, and on that day patients on this dose are asked to approve '
                  'their debit order for the new amount.')

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
        # The price before this form: a change to it is scheduled (price_change), not saved with the dose.
        self.current_price = self.instance.price if self.instance.pk else None
        if self.current_price is None:
            del self.fields['price_from']  # A dose's first price applies straight away.
        else:
            self.initial.setdefault('price_from', timezone.localdate())
            self.order_fields(['pen', 'amount_mg', 'frequency', 'pens_per_dispatch', 'dispatch_weeks', 'price',
                               'price_from', 'billing_weeks', 'tier', 'is_active'])
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

    def clean_price_from(self):
        value = self.cleaned_data.get('price_from')
        if value and value < timezone.localdate():
            raise ValidationError('Choose today or a later date.')
        return value

    @property
    def price_change(self):
        """(new price, the day it applies from) when the dose's price is changed, which the view hands to
        care.dose_prices.change_price; None otherwise. Clearing the price takes it away now."""
        if self.current_price is None or 'price' not in self.changed_data or self.cleaned_data.get('price') is None:
            return None
        return self.cleaned_data['price'], self.cleaned_data.get('price_from') or timezone.localdate()

    def save(self, commit=True):
        if self.price_change:
            self.instance.price = self.current_price  # Changed from its day by care.dose_prices.change_price.
        return super().save(commit)

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


SHIPPING_FILTER_FIELDS = ('status', 'product', 'doctor', 'payment')


class ShippingFilterForm(forms.Form):
    q = forms.CharField(label='Search shipments', required=False, max_length=100)
    week = forms.DateField(label='Week beginning', widget=forms.DateInput(attrs={'type': 'date'}))
    status = TickedChoiceField(label='Status', choices=Shipment.Status.choices)
    product = TickedChoiceField(label='Product', choices=())
    doctor = TickedChoiceField(label='Doctor', choices=())
    payment = TickedChoiceField(label='Payment', choices=(('paid', 'Paid'), ('unpaid', 'Not paid')))

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is None:
            return
        self.fields['product'].choices = [(str(product.pk), str(product))
            for product in MedicationProduct.objects.for_company(company).order_by('name', 'strength', 'pk')]
        memberships = CompanyMembership.objects.filter(company=company, clinician_type='doctor').values('user_id')
        prescribers = Shipment.objects.for_company(company).filter(patient__company=company).values('authorization__prescribed_by_id')
        assigned = Patient.objects.for_company(company).values('assigned_doctor_id')
        doctors = get_user_model().objects.filter(
            Q(pk__in=memberships) | Q(pk__in=prescribers) | Q(pk__in=assigned),
        ).order_by('first_name', 'last_name', 'pk')
        self.fields['doctor'].choices = [(str(doctor.pk), doctor.titled_name) for doctor in doctors]


class YearMonthField(forms.DateField):
    def to_python(self, value):
        if isinstance(value, str) and value and not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', value):
            raise ValidationError(self.error_messages['invalid'], code='invalid')
        return super().to_python(value)


class HistoryFilterForm(forms.Form):
    q = forms.CharField(label='Search dispatches', required=False, max_length=100)
    start_month = YearMonthField(label='From month', required=False, input_formats=('%Y-%m',),
        widget=forms.DateInput(format='%Y-%m', attrs={'type': 'month', 'placeholder': 'YYYY-MM'}),
        error_messages={'invalid': 'Enter a month in YYYY-MM format.'})
    end_month = YearMonthField(label='To month', required=False, input_formats=('%Y-%m',),
        widget=forms.DateInput(format='%Y-%m', attrs={'type': 'month', 'placeholder': 'YYYY-MM'}),
        error_messages={'invalid': 'Enter a month in YYYY-MM format.'})

    def clean(self):
        data = super().clean()
        if data.get('start_month') and data.get('end_month') and data['end_month'] < data['start_month']:
            raise ValidationError('To month must not be before From month.')
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
    stock_override = forms.BooleanField(label='Allow dispatch despite insufficient stock', required=False)
    stock_override_reason = forms.CharField(label='Stock override reason', max_length=500, required=False)

    action = forms.ChoiceField(choices=(('confirm', 'Confirm & lock'),
                                       ('hold', 'Hold'), ('cancel', 'Cancel before dispatch'),
                                       ('dispatch', 'Record actual dispatch'), ('deliver', 'Record actual delivery')))
    reason = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 3}))
    tracking_number = forms.CharField(label='Waybill', required=False, max_length=120, widget=forms.HiddenInput)

    def __init__(self, *args, shipment, items=(), **kwargs):
        super().__init__(*args, **kwargs)
        allowed = set()
        if shipment.status in (Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY):
            allowed.update(('hold', 'cancel'))
            if shipment.status == Shipment.Status.HELD or not shipment.locked_at:
                allowed.add('confirm')
            if shipment.status == Shipment.Status.READY and shipment.locked_at:
                allowed.add('dispatch')
        elif shipment.status == Shipment.Status.DISPATCHED:
            allowed.add('deliver')
        self.fields['action'].choices = [(value, label) for value, label in self.fields['action'].choices if value in allowed]
        # An optional batch and expiry for each line, noted when the parcel is dispatched, for tracing a recall.
        self.items = list(items)
        for item in self.items:
            self.fields[f'batch_{item.pk}'] = forms.CharField(
                label=f'Batch and expiry: {item.product} (optional)', required=False, max_length=120, initial=item.batch_note,
                help_text='Recorded with the dispatch, e.g. SG-2604, expiry 04/2027.')

    def batch_notes(self):
        return {item.pk: self.cleaned_data.get(f'batch_{item.pk}', '') for item in self.items}


class DeliveryAddressForm(forms.Form):
    """Where parcels go and the number the courier calls (care.addresses checks it again when it's saved)."""

    recipient = forms.CharField(label='Recipient', required=False, max_length=150,
                                help_text='Leave blank to use the patient’s name.')
    business = forms.CharField(label='Business or complex name', required=False, max_length=150,
                               help_text='For an office, or a complex with a unit number.')
    street = forms.CharField(label='Street address', max_length=200)
    suburb = forms.CharField(label='Suburb', max_length=120)
    city = forms.CharField(label='City or town', max_length=120)
    province = forms.ChoiceField(label='Province', choices=(('', 'Choose a province'), *PROVINCES))
    postal_code = forms.CharField(label='Postal code', max_length=4, min_length=4,
                                  widget=forms.TextInput(attrs={'inputmode': 'numeric', 'pattern': '[0-9]*'}))
    phone = forms.CharField(label='Contact number', max_length=32, help_text='The courier calls this number.',
                            widget=forms.TextInput(attrs={'type': 'tel'}))
    instructions = forms.CharField(label='Delivery instructions', required=False, max_length=300,
                                   widget=forms.Textarea(attrs={'rows': 2}), help_text='Optional, such as a gate code.')

    def __init__(self, *args, instance_address=None, patient=None, for_patient=False, **kwargs):
        from care.addresses import FIELDS

        # The address on file, else what the patient record has; anything passed in initial wins.
        if instance_address is not None:
            known = {name: getattr(instance_address, name) for name in FIELDS}
        else:
            known = {'phone': patient.phone, 'city': patient.city} if patient is not None else {}
        kwargs['initial'] = {**known, **(kwargs.get('initial') or {})}
        super().__init__(*args, **kwargs)
        if for_patient:
            self.fields['recipient'].help_text = 'Leave blank to use your own name.'
            # The patient's own address, so their phone can fill it in (never on the practice's form for a patient).
            for name, token in (('recipient', 'shipping name'), ('business', 'shipping organization'),
                                ('street', 'shipping address-line1'), ('suburb', 'shipping address-level3'),
                                ('city', 'shipping address-level2'), ('province', 'shipping address-level1'),
                                ('postal_code', 'shipping postal-code'), ('phone', 'shipping tel')):
                self.fields[name].widget.attrs['autocomplete'] = token

    def address(self):
        from care.addresses import FIELDS

        return {name: self.cleaned_data.get(name, '') for name in FIELDS}


class BasketQuantityForm(forms.Form):
    """How many of a once-off item go in the patient's basket, from My Treatment or the basket (0 removes it)."""

    quantity = forms.IntegerField(min_value=0, max_value=100, initial=1, label='Quantity')


class OrderForm(forms.Form):
    """Placing the basket as an order. It goes to the delivery address on file, so only a note is asked for."""

    note = forms.CharField(label='Note for the practice (optional)', required=False, max_length=1000,
                           widget=forms.Textarea(attrs={'rows': 2}))


class OrderReviewForm(forms.Form):
    action = forms.ChoiceField(choices=(('accept', 'Accept for preparation'), ('cancel', 'Cancel unaccepted request')))
    scheduled_for = forms.DateField(label='Planned dispatch date', required=False,
                                    widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)
    confirm = forms.BooleanField(label='I confirm this supply-request action')


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label='I confirm cancellation')
