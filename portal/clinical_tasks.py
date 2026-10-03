"""Present generated tasks as links to their protected clinical workflow."""

from django.urls import reverse

from care.models import ClinicalEncounter
from practices.models import CompanyMembership


def attach_clinical_task_links(tasks, actor, membership):
    for task in tasks:
        encounter = getattr(task, 'encounter_signing', None)
        lab_request = getattr(task, 'lab_review_request', None)
        compounding = getattr(task, 'compounding_record', None)
        reminder = getattr(task, 'authorization_reminder', None)
        task.is_clinical_workflow = encounter is not None or lab_request is not None or compounding is not None
        task.workflow_url = ''
        task.workflow_label = ''
        clinical_access = membership.has_clinical_access
        if encounter and encounter.company_id == task.company_id and encounter.patient_id == task.patient_id:
            if clinical_access and (encounter.status == ClinicalEncounter.Status.SIGNED or (
                membership.is_clinician and encounter.clinician_id == actor.pk
            )):
                task.workflow_url = reverse('portal:clinical-consultation-detail', args=[encounter.pk])
                task.workflow_label = 'Open consultation'
        elif lab_request and lab_request.company_id == task.company_id and lab_request.patient_id == task.patient_id:
            if clinical_access:
                task.workflow_url = reverse('portal:clinical-lab-detail', args=[lab_request.pk])
                task.workflow_label = 'Open blood tests'
        elif compounding and compounding.company_id == task.company_id and compounding.patient_id == task.patient_id:
            if (membership.is_prescriber and compounding.clinician_id == actor.pk) or (
                membership.role == CompanyMembership.Role.SUPER_ADMIN and compounding.status in ('ready', 'submitted')
            ):
                task.workflow_url = reverse('portal:compounding-detail', args=[compounding.pk])
                task.workflow_label = 'Open compounding record'
        elif reminder and reminder.company_id == task.company_id and reminder.patient_id == task.patient_id and clinical_access:
            task.workflow_url = reverse('portal:treatment-authorisation-detail', args=[reminder.authorization_id])
            task.workflow_label = 'Review authorisation'
    return tasks
