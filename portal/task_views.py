from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from care.models import ClinicalNote
from care.tag_forms import ClinicalNoteTagsForm
from care.task_services import save_task, set_record_tags, visible_tasks
from practices.models import Company, CompanyMembership

from .task_forms import TaskEditorForm
from .clinical_tasks import attach_clinical_task_links
from .views import StaffCompanyRequiredMixin, _patient_record_context
from .workflow_context import make_workflow_context, validate_workflow_context
from .patient_action_context import patient_action_context, uses_patient_workspace, workspace_destination, workspace_redirect


@method_decorator(never_cache, name='dispatch')
class TaskEditorView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post')

    def get_task(self):
        if 'pk' not in self.kwargs:
            return None
        return get_object_or_404(
            visible_tasks(self.company, self.request.user, self.membership).select_related('patient', 'created_by'),
            pk=self.kwargs['pk'],
        )

    def render_form(self, form, task):
        context = {
            'company': self.company, 'active_membership': self.membership, 'nav_section': 'tasks',
            'page_title': 'Edit task' if task else 'Create task', 'task': task, 'form': form,
            'workflow_context': self.request.POST.get('workflow_context', '') if self.request.method == 'POST' else make_workflow_context(self.request, self.company, 'task-editor', task),
        }
        context.update(patient_action_context(self, task.patient if task and task.patient_id else None, 'tasks',
            content_template='portal/includes/task_editor_content.html', title=context['page_title']))
        return render(self.request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/staff_task_form.html', context)

    def scope_patient_field(self, form, task):
        if task and task.patient_id and uses_patient_workspace(self.request):
            # This editor is inside one patient's workspace. Moving the task
            # between patients remains an explicit action in the global editor.
            form.fields['patient'].disabled = True
        return form

    def get(self, request, *args, **kwargs):
        task = self.get_task()
        if task:
            attach_clinical_task_links([task], request.user, self.membership)
            if task.workflow_url:
                return redirect(workspace_destination(request, task.workflow_url))
            if task.is_clinical_workflow:
                raise PermissionDenied('This task is managed by its responsible doctor in the clinical workflow.')
        form = TaskEditorForm(company=self.company, instance=task, initial={'assigned_to': request.user.pk} if task is None else None)
        return self.render_form(self.scope_patient_field(form, task), task)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        task = self.get_task()
        if task:
            attach_clinical_task_links([task], request.user, self.membership)
            if task.is_clinical_workflow:
                raise PermissionDenied('Complete this task through its clinical workflow, not the task editor.')
        form = self.scope_patient_field(TaskEditorForm(request.POST, company=self.company, instance=task), task)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'task-editor', task)
            if valid:
                saved = save_task(company=self.company, actor=request.user, form=form, request=request)
                messages.success(request, 'Task saved.')
                if visible_tasks(self.company, request.user, self.membership).filter(pk=saved.pk).exists():
                    return workspace_redirect(request, 'portal:task-edit', pk=saved.pk)
                if uses_patient_workspace(request) and saved.patient_id:
                    from .patient_workspace import workspace_url
                    return redirect(workspace_url(saved.patient, 'tasks'))
                return redirect('portal:staff-tasks')
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages))
        return self.render_form(form, task)


class ClinicalNoteTagsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, patient_pk, pk):
        note = get_object_or_404(
            ClinicalNote.objects.for_company(self.company).filter(
                patient_id=patient_pk, patient__company=self.company, patient__is_active=True,
                author=request.user,
            ), pk=pk,
        )
        if not self.membership.is_clinician:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied('Only the note author can edit its tags.')
        form = ClinicalNoteTagsForm(request.POST, company=self.company, instance=note)
        if form.is_valid():
            try:
                set_record_tags(record=note, actor=request.user, tags=form.cleaned_data['tags'], new_tag=form.cleaned_data['new_tag'], request=request)
            except ValidationError as error:
                form.add_error(None, ' '.join(error.messages))
            else:
                messages.success(request, 'Note tags updated.')
                from .patient_workspace import workspace_url
                return redirect(workspace_url(note.patient, 'notes') + f'#note-{note.pk}')
        from .patient_workspace import render_workspace_form_error
        return render_workspace_form_error(request, self.company, self.membership, note.patient,
            'note_tags_form', form, failed_note_tag_id=note.pk)
