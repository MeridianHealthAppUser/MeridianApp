"""Practice tasks and shared free-form labels, with scoped and audited writes."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company

from .models import ClinicalNote, ClinicalNoteTagAssignment, ClinicalTask, RecordTag, TaskTagAssignment
from .services import record_audit


def visible_tasks(company, actor, membership):
    tasks = ClinicalTask.objects.for_company(company).filter(
        Q(patient__isnull=True) | Q(patient__company=company, patient__is_active=True),
    )
    if membership.role == CompanyMembership.Role.DOCTOR:
        tasks = tasks.filter(Q(assigned_to=actor) | Q(created_by=actor))
    return tasks


def _staff_membership(company, actor):
    require_enabled_company(company)
    if not getattr(actor, 'is_active', False):
        raise PermissionDenied('An active practice staff account is required.')
    membership = CompanyMembership.objects.filter(company=company, company__is_active=True, user=actor,
        user__is_active=True, is_active=True, role__in=('doctor', 'practice_admin', 'super_admin')).first()
    if membership is None:
        raise PermissionDenied('You do not have access to this practice.')
    return membership


def _validate_record_actor(record, actor, membership):
    if record.patient_id and (not record.patient.is_active or record.patient.company_id != record.company_id):
        raise PermissionDenied('This patient record is not available in the active practice.')
    if isinstance(record, ClinicalNote):
        if not membership.is_clinician or record.author_id != actor.pk:
            raise PermissionDenied('Only the note author can edit its tags.')
    elif isinstance(record, ClinicalTask):
        from .clinical import WORKFLOW_TASK_ERROR, is_clinical_workflow_task

        if is_clinical_workflow_task(record):
            raise ValidationError(WORKFLOW_TASK_ERROR)
        if membership.role == CompanyMembership.Role.DOCTOR and actor.pk not in (record.assigned_to_id, record.created_by_id):
            raise PermissionDenied('Only the task creator or assignee can manage this task.')
    else:
        raise ValidationError('Tags are supported on tasks and clinical notes.')


def _sync_tags(record, actor, tags, new_tag):
    tags = list(tags)
    if any(tag.company_id != record.company_id for tag in tags):
        raise ValidationError('Choose tags from this practice only.')
    requested_ids = {tag.pk for tag in tags}
    tags = list(RecordTag.objects.for_company(record.company).filter(pk__in=requested_ids))
    if requested_ids != {tag.pk for tag in tags}:
        raise ValidationError('A selected tag changed or is no longer available in this practice. Reload the form.')
    name = ' '.join((new_tag or '').split())
    if len(name) > 64:
        raise ValidationError('Keep tag names to 64 characters or fewer.')
    if name:
        tag = RecordTag.objects.for_company(record.company).filter(name__iexact=name).first()
        if tag is None:
            tag = RecordTag(company=record.company, name=name, created_by=actor)
            tag.full_clean()
            tag.save()
        tags.append(tag)
    desired = {tag.pk: tag for tag in tags}
    relation = 'task' if isinstance(record, ClinicalTask) else 'note'
    through = TaskTagAssignment if relation == 'task' else ClinicalNoteTagAssignment
    assignments = through.objects.for_company(record.company).filter(**{relation: record})
    existing = set(assignments.values_list('tag_id', flat=True))
    # Only unapply labels from this one record; the reusable tags remain intact.
    assignments.exclude(tag_id__in=desired).delete()
    for pk in desired.keys() - existing:
        assignment = through(company=record.company, tag=desired[pk], **{relation: record})
        assignment.full_clean()
        assignment.save()
    return list(desired.values())


@transaction.atomic
def set_record_tags(*, record, actor, tags, new_tag='', request=None):
    company = Company.objects.select_for_update().get(pk=record.company_id)
    membership = _staff_membership(company, actor)
    record = type(record).objects.select_for_update().get(pk=record.pk, company=company)
    _validate_record_actor(record, actor, membership)
    result = _sync_tags(record, actor, tags, new_tag)
    record_audit(
        company=company, actor=actor, patient=record.patient,
        action='record.tags_updated', target=record, request=request,
        metadata={'tag_ids': sorted(tag.pk for tag in result)},
    )
    return result


@transaction.atomic
def save_task(*, company, actor, form, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    membership = _staff_membership(company, actor)
    if form.company.pk != company.pk or not form.is_valid():
        raise ValidationError('Correct the task fields before saving.')
    created = not form.instance.pk
    if created:
        task = ClinicalTask(company=company, created_by=actor)
    else:
        task = ClinicalTask.objects.select_for_update().get(pk=form.instance.pk, company=company)
        _validate_record_actor(task, actor, membership)
    old_status = task.status
    for field in ('title', 'description', 'patient', 'assigned_to', 'priority', 'status', 'due_at'):
        setattr(task, field, form.cleaned_data[field])
    if task.patient_id:
        patient = Patient.objects.for_company(company).filter(pk=task.patient_id, is_active=True).first()
        if patient is None:
            raise ValidationError('Choose an active patient in this practice.')
        task.patient = patient
    if task.status == ClinicalTask.Status.DONE:
        if created or old_status != ClinicalTask.Status.DONE or task.completed_at is None:
            task.completed_at = timezone.now()
    else:
        task.completed_at = None
    task.full_clean()
    task.save()
    tags = _sync_tags(task, actor, form.cleaned_data.get('tags', []), form.cleaned_data.get('new_tag', ''))
    record_audit(
        company=company, actor=actor, patient=task.patient,
        action='task.created' if created else 'task.updated', target=task, request=request,
        metadata={'assigned_to_id': task.assigned_to_id, 'tag_ids': sorted(tag.pk for tag in tags), 'status': task.status},
    )
    return task
