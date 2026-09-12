from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import MaxLengthValidator

from care.forms import CompanyBoundModelForm
from care.models import ClinicalTask
from care.tag_forms import TagFieldsMixin
from practices.models import Patient


class TaskEditorForm(TagFieldsMixin, CompanyBoundModelForm):
    class Meta:
        model = ClinicalTask
        # Tag through-records are written by the service, with their company key.
        fields = ('title', 'description', 'patient', 'assigned_to', 'priority', 'status', 'due_at')
        labels = {'description': 'Note', 'due_at': 'Due date and time (SAST)'}
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'due_at': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, company, **kwargs):
        kwargs.setdefault('auto_id', 'task_editor_%s')
        super().__init__(*args, company=company, **kwargs)
        self.fields['description'].max_length = 10000
        self.fields['description'].validators.append(MaxLengthValidator(10000))
        self.fields['description'].widget.attrs['maxlength'] = 10000
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)
        self.fields['patient'].required = False
        self.fields['patient'].empty_label = 'General practice — no patient'
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(
            is_active=True, company_memberships__company=company, company_memberships__is_active=True,
        ).distinct().order_by('first_name', 'last_name', 'email')
        self.setup_tag_fields(company, self.instance)
