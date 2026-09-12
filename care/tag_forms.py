from django import forms

from .models import RecordTag


class TagFieldsMixin:
    def setup_tag_fields(self, company, instance=None):
        self.fields['tags'] = forms.ModelMultipleChoiceField(
            label='Free-form tags', queryset=RecordTag.objects.for_company(company), required=False,
            widget=forms.CheckboxSelectMultiple,
            help_text='Choose any labels that apply. Tags do not change completion status.',
        )
        self.fields['new_tag'] = forms.CharField(
            label='Create and add a tag', max_length=64, required=False,
            widget=forms.TextInput(attrs={'placeholder': 'Any label you choose'}),
            help_text='Saved to this practice and available on tasks and clinical notes.',
        )
        if instance is not None and instance.pk:
            self.initial['tags'] = list(instance.tags.filter(company=company).values_list('pk', flat=True))

    def clean_new_tag(self):
        return ' '.join(self.cleaned_data['new_tag'].split())


class ClinicalNoteTagsForm(TagFieldsMixin, forms.Form):
    def __init__(self, *args, company, instance=None, **kwargs):
        if instance is not None:
            kwargs.setdefault('auto_id', f'note_tags_{instance.pk}_%s')
        super().__init__(*args, **kwargs)
        self.setup_tag_fields(company, instance)
