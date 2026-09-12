from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.message_management import manage_conversation
from care.models import ClinicalTask, MessageThread
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class ConversationManagementForm(forms.Form):
    action = forms.ChoiceField(choices=(('escalate', 'Create a reply task for a doctor'), ('close', 'Close conversation'), ('reopen', 'Reopen conversation')))
    doctor = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False)
    priority = forms.ChoiceField(choices=ClinicalTask.Priority.choices, initial='normal')
    note = forms.CharField(label='Care-team task note (not sent as a patient message)', required=False, max_length=2000, widget=forms.Textarea(attrs={'rows': 4}))
    confirm = forms.BooleanField(label='Confirm this action. No appointment time, clinical record or email is changed.')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['doctor'].queryset = get_user_model().objects.filter(is_active=True, company_memberships__company=company, company_memberships__is_active=True, company_memberships__role='doctor').distinct().order_by('first_name', 'last_name')


@method_decorator(never_cache, name='dispatch')
class ConversationManagementView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def thread(self, pk):
        return get_object_or_404(MessageThread.objects.for_company(self.company).filter(patient__company=self.company, patient__is_active=True).select_related('patient'), pk=pk)

    def display(self, request, thread, form=None, status=200):
        form = form if form is not None else ConversationManagementForm(company=self.company, initial={'doctor': thread.patient.assigned_doctor_id})
        return render(request, 'portal/message_management.html', dict(company=self.company, active_membership=self.membership,
            nav_section='messages', thread=thread, form=form,
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'conversation-management', thread)), status=status)

    def get(self, request, pk):
        return self.display(request, self.thread(pk))

    def post(self, request, pk):
        thread = self.thread(pk)
        form = ConversationManagementForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'conversation-management', thread)
            if valid:
                result = manage_conversation(thread=thread, actor=request.user, expected_updated=token['updated'], request=request, **form.cleaned_data)
                if isinstance(result, ClinicalTask):
                    messages.success(request, 'Doctor reply task created or already open. The conversation is unchanged.')
                    return redirect('portal:task-edit', pk=result.pk)
                messages.success(request, 'Conversation status updated. Existing messages are retained.')
                return redirect('portal:conversation-manage', pk=thread.pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, thread, form, 400)
