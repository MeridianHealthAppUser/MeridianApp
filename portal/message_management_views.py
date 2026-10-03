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
from care.messaging import staff_threads
from care.patient_assignment import active_doctors
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class ConversationManagementForm(forms.Form):
    action = forms.ChoiceField(choices=(('handover', 'Add a clinician to this conversation'), ('close', 'Close conversation'), ('reopen', 'Reopen conversation')))
    doctor = forms.ModelChoiceField(label='Clinician to add', queryset=get_user_model().objects.none(), required=False,
                                    help_text='They can read this whole conversation and reply. The patient sees that they joined.')
    confirm = forms.BooleanField(label='Confirm this action. Messages are kept; no appointment, clinical record or email changes.')

    def __init__(self, *args, company, thread, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['doctor'].queryset = active_doctors(company).exclude(pk__in=thread.participant_links.values('user_id'))
        self.fields['doctor'].label_from_instance = lambda user: user.full_name

    def clean(self):
        data = super().clean()
        if data.get('action') == 'handover' and not data.get('doctor'):
            self.add_error('doctor', 'Choose a clinician to add.')
        return data


@method_decorator(never_cache, name='dispatch')
class ConversationManagementView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def thread(self, pk):
        return get_object_or_404(staff_threads(self.company, self.request.user).select_related('patient'), pk=pk)

    def display(self, request, thread, form=None, status=200):
        form = form if form is not None else ConversationManagementForm(company=self.company, thread=thread)
        return render(request, 'portal/message_management.html', dict(company=self.company, active_membership=self.membership,
            nav_section='messages', thread=thread, form=form,
            participants=[link.user for link in thread.participant_links.select_related('user')],
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'conversation-management', thread)), status=status)

    def get(self, request, pk):
        return self.display(request, self.thread(pk))

    def post(self, request, pk):
        thread = self.thread(pk)
        form = ConversationManagementForm(request.POST, company=self.company, thread=thread)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'conversation-management', thread)
            if valid:
                manage_conversation(thread=thread, actor=request.user, expected_updated=token['updated'], request=request, **form.cleaned_data)
                if form.cleaned_data['action'] == 'handover':
                    messages.success(request, f"{form.cleaned_data['doctor'].full_name} can now read and reply in this conversation.")
                else:
                    messages.success(request, 'Conversation status updated. Existing messages are retained.')
                return redirect('portal:conversation-manage', pk=thread.pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, thread, form, 400)
