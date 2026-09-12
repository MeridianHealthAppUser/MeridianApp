from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from care.compounding import (cancel_compounding_record, create_compounding_record, mark_compounding_submitted,
                              review_compounding_record, update_compounding_draft)
from care.models import CompoundingRecord, TreatmentAuthorization
from care.review_automation import refresh_review_tasks
from care.services import record_audit

from .clinical_forms import make_clinical_context, validate_clinical_context
from .clinical_views import StaffClinicalView, _error, _listing
from .compounding_forms import CompoundingCancelForm, CompoundingDraftForm, CompoundingReviewForm, CompoundingSubmissionForm, ReviewRunForm
from .review_forms import validate_review_context
from .review_views import ReviewRuleListView


class CompoundingBase(StaffClinicalView):
    nav_section = 'compounding'
    page_title = 'Manual compounding tracking'

    def records(self):
        records = CompoundingRecord.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True,
        ).select_related('company', 'patient', 'clinician', 'authorization__product', 'reviewed_by', 'task')
        if self.is_doctor:
            return records.filter(clinician=self.request.user)
        return records.filter(status__in=(CompoundingRecord.Status.READY, CompoundingRecord.Status.SUBMITTED))

    def token(self, request, patient, kind, record=None):
        return request.POST.get('clinical_context') or make_clinical_context(request, self.company, patient, kind, record)


class CompoundingListView(CompoundingBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        records = self.records().order_by('-created_at', '-pk')
        today = timezone.localdate()
        week = today - timedelta(days=today.weekday())
        metrics = records.aggregate(awaiting=Count('pk', filter=Q(status='draft')), ready=Count('pk', filter=Q(status='ready')),
                                    submitted=Count('pk', filter=Q(status='submitted', submitted_at__date__gte=week)))
        return render(request, 'portal/compounding_list.html', self.context(
            **_listing(request, self.company, records, CompoundingRecord.Status.choices), metrics=metrics,
        ))


class CompoundingCreateView(CompoundingBase):
    def authorization(self, pk):
        self.require_doctor()
        return get_object_or_404(TreatmentAuthorization.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True, prescribed_by=self.request.user,
            product__company=self.company, product__is_compounded=True,
        ).select_related('patient', 'product'), pk=pk)

    def display(self, request, authorization, form, status=200):
        return render(request, 'portal/compounding_create.html', self.context(
            authorization=authorization, patient=authorization.patient, form=form,
            clinical_context=self.token(request, authorization.patient, f'compounding-create:{authorization.pk}'),
        ), status=status)

    def get(self, request, authorization_pk):
        return self.display(request, self.authorization(authorization_pk), CompoundingDraftForm())

    def post(self, request, authorization_pk):
        authorization, form = self.authorization(authorization_pk), CompoundingDraftForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_clinical_context(request, self.company, authorization.patient, f'compounding-create:{authorization.pk}')
            if valid:
                record = create_compounding_record(company=self.company, authorization=authorization, actor=request.user,
                                                     submission_key=token['submission_key'], request=request, **form.cleaned_data)
                messages.success(request, 'Manual compounding tracking draft saved. No prescription or external submission was created.')
                return redirect('portal:compounding-detail', pk=record.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, authorization, form, status=400)


class CompoundingDetailView(CompoundingBase):
    def display(self, request, record, failed_form=None, failed_action=None, status=200):
        forms = dict(draft_form=CompoundingDraftForm(initial={'preparation_note': record.preparation_note}, auto_id='draft_%s'),
                     review_form=CompoundingReviewForm(auto_id='review_%s'),
                     submission_form=CompoundingSubmissionForm(auto_id='submission_%s'), cancel_form=CompoundingCancelForm(auto_id='cancel_%s'))
        key = {'save': 'draft_form', 'review': 'review_form', 'submit': 'submission_form', 'cancel': 'cancel_form'}.get(failed_action)
        if key:
            forms[key] = failed_form
        return render(request, 'portal/compounding_detail.html', self.context(
            record=record, patient=record.patient, can_edit=self.is_doctor and record.clinician_id == request.user.pk,
            clinical_context=self.token(request, record.patient, 'compounding-record', record), failed_form=failed_form, **forms,
        ), status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.records(), pk=pk))

    def post(self, request, pk):
        record = get_object_or_404(self.records(), pk=pk)
        self.require_doctor(record.clinician_id)
        action = request.POST.get('action')
        form_class, prefix = {
            'save': (CompoundingDraftForm, 'draft'), 'review': (CompoundingReviewForm, 'review'),
            'submit': (CompoundingSubmissionForm, 'submission'), 'cancel': (CompoundingCancelForm, 'cancel'),
        }.get(action, (CompoundingCancelForm, 'cancel'))
        form = form_class(request.POST, auto_id=f'{prefix}_%s')
        valid = form.is_valid()
        try:
            token = validate_clinical_context(request, self.company, record.patient, 'compounding-record', record)
            services = {'save': update_compounding_draft, 'review': review_compounding_record,
                        'submit': mark_compounding_submitted, 'cancel': cancel_compounding_record}
            if action not in services:
                raise ValidationError('Choose a valid compounding workflow action.')
            if valid:
                services[action](record=record, actor=request.user, expected_revision=token['revision'], request=request, **form.cleaned_data)
                messages.success(request, 'Manual compounding workflow updated. Nothing was emailed or submitted by the app.')
                return redirect('portal:compounding-detail', pk=record.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, record, form, action, status=400)


class CompoundingPrintView(CompoundingBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        record = get_object_or_404(self.records(), pk=pk)
        self.require_doctor(record.clinician_id)
        if request.method == 'GET':
            record_audit(company=self.company, actor=request.user, patient=record.patient,
                         action='compounding.summary_viewed', target=record, request=request)
        return render(request, 'portal/compounding_print.html', self.context(record=record, patient=record.patient))


class ReviewRunView(ReviewRuleListView):
    http_method_names = ('post', 'options')

    def post(self, request):
        self.require_edit()
        form = ReviewRunForm(request.POST, auto_id='run_%s')
        valid = form.is_valid()
        try:
            validate_review_context(request, self.company, 'review-run')
            if valid:
                stats = refresh_review_tasks(company=self.company, actor=request.user,
                                             within_days=form.cleaned_data['within_days'], request=request)
                messages.success(request, f"Local review check: {stats['created']} reminders created; {stats['existing']} already recorded; "
                                 f"{stats['shipments_held']} undispatched shipments held; {stats['skipped_inactive_doctor']} need an active doctor. No emails sent.")
                return redirect('portal:treatment-review-rules')
        except ValidationError as error:
            _error(form, error)
        return self.display(request, run_form=form, status=400)
