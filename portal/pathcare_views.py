"""Request blood tests from the patient record: preview, send and resend the PathCare form, and view it.

Anything that stops a form being made or emailed is shown as an error toast.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin

from care.models import PathCareRequestForm
from care.patient_files import read_patient_file
from care.pathcare import EMAIL_NOT_SET_UP, FILENAME, clean_tests, fill_form, form_values, request_blood_tests, resend_pathcare_form
from care.services import record_audit
from practices.models import Patient

from .record_access import require_record_change, require_record_view
from .views import StaffCompanyRequiredMixin
from .workflow_context import validate_workflow_context


logger = logging.getLogger(__name__)
UNEXPECTED_ERROR = 'Something went wrong making the blood test form, so nothing was sent. Try again in a moment.'


def _pdf_response(content, filename):
    response = HttpResponse(bytes(content), content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@method_decorator(never_cache, name='dispatch')
class BloodTestMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    http_method_names = ('post',)

    def setup_request(self, pk):
        # Requesting blood tests is a doctor's act, as it is everywhere else in the record.
        if not self.membership.is_prescriber:
            raise PermissionDenied('Only a doctor can request blood tests.')
        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user'), pk=pk, is_active=True)
        require_record_change(self.membership, self.request.user, patient)
        return patient

    def overview_url(self, patient, query='', anchor='record-bloods'):
        from .patient_workspace import workspace_url

        return f'{workspace_url(patient)}{query}#{anchor}'

    def render_overview(self, patient, status=400, **overrides):
        from .patient_workspace import workspace_tab_context

        context = workspace_tab_context(self.request, self.company, self.membership, patient, 'overview', **overrides)
        return render(self.request, 'portal/patient_workspace.html', context, status=status)


class BloodTestPreviewView(BloodTestMixin, View):
    """Without scripts, the card posts here to open the preview with the filled values."""

    def post(self, request, pk):
        patient = self.setup_request(pk)
        tests = request.POST.get('tests', '')
        try:
            validate_workflow_context(request, self.company, 'patient-bloods', patient=patient)
            tests = clean_tests(tests)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return self.render_overview(patient, bloods_tests=tests)
        return self.render_overview(patient, status=200, bloods_tests=tests, open_dialog='bloods')


@method_decorator(xframe_options_sameorigin, name='dispatch')
class BloodTestPreviewPdfView(BloodTestMixin, View):
    """The filled form for the preview, shown inside the preview dialog. Nothing is stored or sent."""

    def post(self, request, pk):
        patient = self.setup_request(pk)
        try:
            validate_workflow_context(request, self.company, 'patient-bloods', patient=patient)
            content = fill_form(form_values(company=self.company, patient=patient, doctor=request.user,
                                            tests=clean_tests(request.POST.get('tests', ''))))
        except ValidationError as error:
            # The preview dialog shows this in its frame and as an error toast.
            return HttpResponse(f'<!doctype html><meta charset="utf-8"><p style="font:14px sans-serif;color:#a73f37;padding:1rem" data-preview-error>'
                                f'{escape(" ".join(error.messages))}</p>', status=400)
        return _pdf_response(content, FILENAME)


class BloodTestRequestView(BloodTestMixin, View):
    def post(self, request, pk):
        patient = self.setup_request(pk)
        tests = request.POST.get('tests', '')
        try:
            validate_workflow_context(request, self.company, 'patient-bloods', patient=patient)
            form = request_blood_tests(company=self.company, patient=patient, actor=request.user, tests=tests,
                                       submission_key=request.POST.get('submission_key') or None, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return self.render_overview(patient, bloods_tests=tests)
        except (PermissionDenied, Http404):
            raise
        except Exception:
            logger.exception('Blood test request failed for patient %s.', patient.pk)
            messages.error(request, UNEXPECTED_ERROR)
            return self.render_overview(patient, status=500, bloods_tests=tests)
        if email_failed(form):
            messages.error(request, EMAIL_NOT_SET_UP if not settings.EMAIL_ENABLED else
                           f'The blood test form is in the clinical log, but the email to {form.email_to} '
                           'couldn’t be sent. Use Resend in the log to try again.')
        else:
            messages.success(request, f'Blood test form emailed to {form.email_to}. It is in the clinical log.')
        return redirect(self.overview_url(patient, '&log=clinical', f'log-blood-{form.lab_request_id}'))


class BloodTestResendView(BloodTestMixin, View):
    def post(self, request, pk, form_pk):
        patient = self.setup_request(pk)
        form = get_object_or_404(PathCareRequestForm.objects.for_company(self.company), pk=form_pk, patient=patient)
        try:
            validate_workflow_context(request, self.company, 'patient-bloods', patient=patient)
            form = resend_pathcare_form(form=form, actor=request.user, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        except (PermissionDenied, Http404):
            raise
        except Exception:
            logger.exception('Resending PathCare form %s failed.', form.pk)
            messages.error(request, 'Something went wrong resending the blood test form. Try again in a moment.')
        else:
            if email_failed(form):
                messages.error(request, EMAIL_NOT_SET_UP if not settings.EMAIL_ENABLED else
                               f'The blood test form couldn’t be emailed to {form.email_to}. Try again in a moment.')
            else:
                messages.success(request, f'Blood test form sent again to {form.email_to}.')
        return redirect(self.overview_url(patient, '&log=clinical', f'log-blood-{form.lab_request_id}'))


def email_failed(form):
    """Whether the email has already failed. Without a task queue it is sent straight away, so this is known now."""
    return PathCareRequestForm.objects.filter(pk=form.pk, email_status=PathCareRequestForm.EmailStatus.FAILED).exists()


@method_decorator(never_cache, name='dispatch')
class BloodTestFormView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """The stored form exactly as it was emailed."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk, form_pk):
        if not self.membership.has_clinical_access:
            raise PermissionDenied('Blood test forms are available to clinicians and Super Admins only.')
        patient = get_object_or_404(Patient.objects.for_company(self.company), pk=pk, is_active=True)
        require_record_view(self.membership, request.user, patient)
        form = get_object_or_404(PathCareRequestForm.objects.for_company(self.company), pk=form_pk, patient=patient)
        content = read_patient_file(form.file)
        record_audit(company=self.company, actor=request.user, patient=patient, action='pathcare_form.viewed',
                     target=form, request=request)
        return _pdf_response(content, FILENAME)
