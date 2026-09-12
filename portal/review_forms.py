from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from care.models import MedicationProduct


REVIEW_CONTEXT_SALT = 'portal.review-planning-context.v1'


def make_review_context(request, company, kind, record=None):
    return signing.dumps(dict(actor_id=request.user.pk, company_id=company.pk, kind=kind,
                              record_id=record.pk if record else None,
                              updated_at=record.updated_at.isoformat() if record else None), salt=REVIEW_CONTEXT_SALT)


def validate_review_context(request, company, kind, record=None):
    error = 'This form has expired or the selected practice has changed. Reload the page before saving.'
    token = request.POST.get('review_context', '')
    if not token or len(token) > 4096:
        raise ValidationError(error)
    try:
        data = signing.loads(token, salt=REVIEW_CONTEXT_SALT, max_age=12 * 60 * 60)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(error) from None
    scope = dict(actor_id=request.user.pk, company_id=company.pk, kind=kind, record_id=record.pk if record else None)
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in scope.items()):
        raise ValidationError(error)
    return data


class ReviewRuleForm(forms.Form):
    name = forms.CharField(max_length=255)
    product_category = forms.ChoiceField(choices=MedicationProduct.Category.choices)
    review_interval_days = forms.IntegerField(label='Planning review interval (days)', min_value=1, max_value=3650)
    blood_tests_required = forms.BooleanField(label='Flag blood tests in review planning', required=False,
                                             help_text='Planning only. A doctor must separately request and review tests.')
    is_active = forms.BooleanField(label='Active planning rule', required=False, initial=True)


class ReviewDefaultForm(forms.Form):
    review_interval_days = forms.IntegerField(label='Default review planning interval (days)', min_value=1, max_value=3650)
