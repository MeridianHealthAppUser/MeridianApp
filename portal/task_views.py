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
        return render(self.request, 'portal/staff_task_form.html', {
            'company': self.company, 'active_membership': self.membership, 'nav_section': 'tasks',
            'page_title': 'Edit task' if task else 'Create task', 'task': task, 'form': form,
            'workflow_context': self.request.POST.get('workflow_context', '') if self.request.method == 'POST' else make_workflow_context(self.request, self.company, 'task-editor', task),
        })

    def get(self, request, *args, **kwargs):
        task = self.get_task()
        if task:
            attach_clinical_task_links([task], request.user, self.membership)
            if task.workflow_url:
                return redirect(task.workflow_url)
            if task.is_clinical_workflow:
                raise PermissionDenied('This task is managed by its responsible doctor in the clinical workflow.')
        form = TaskEditorForm(company=self.company, instance=task, initial={'assigned_to': request.user.pk} if task is None else None)
        return self.render_form(form, task)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        task = self.get_task()
        if task:
            attach_clinical_task_links([task], request.user, self.membership)
            if task.is_clinical_workflow:
                raise PermissionDenied('Complete this task through its clinical workflow, not the task editor.')
        form = TaskEditorForm(request.POST, company=self.company, instance=task)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'task-editor', task)
            if valid:
                saved = save_task(company=self.company, actor=request.user, form=form, request=request)
                messages.success(request, 'Task saved.')
                if visible_tasks(self.company, request.user, self.membership).filter(pk=saved.pk).exists():
                    return redirect('portal:task-edit', pk=saved.pk)
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
        if self.membership.role != CompanyMembership.Role.DOCTOR:
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
                return redirect(f'{reverse("portal:patient-detail", args=[note.patient_id])}#note-{note.pk}')
        return render(request, 'portal/patient_detail.html', _patient_record_context(
            request, self.company, self.membership, note.patient,
            failed_note_tag_id=note.pk, note_tags_form=form,
        ))
