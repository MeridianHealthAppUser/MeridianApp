from django import forms


class CompoundingDraftForm(forms.Form):
    preparation_note = forms.CharField(label='Internal preparation note', required=False, max_length=10000,
                                       strip=False, widget=forms.Textarea(attrs={'rows': 6}),
                                       help_text='A tracking note only. This does not create a legal prescription.')


class CompoundingReviewForm(forms.Form):
    confirm = forms.BooleanField(label='I have reviewed this tracking record and its current treatment authorisation.')


class CompoundingSubmissionForm(forms.Form):
    external_reference = forms.CharField(label='External manual submission reference', max_length=120)
    confirm = forms.BooleanField(label='I confirm that I completed the separate manual submission. This app has not sent it.')


class CompoundingCancelForm(forms.Form):
    confirm = forms.BooleanField(label='Cancel this tracking record before submission, keeping its history.')


class ReviewRunForm(forms.Form):
    within_days = forms.IntegerField(label='Look ahead (days)', min_value=0, max_value=90, initial=30)
    confirm = forms.BooleanField(label='Run this local check, create missing reminders and hold invalid-authorisation shipments. No emails will be sent.')
