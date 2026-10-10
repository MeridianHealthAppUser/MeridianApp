"""Bounded, practice-scoped weight presentation for the staff patient record."""

from django.core.paginator import Paginator
from django.urls import reverse

from care.models import WeightEntry
from .patient_progress import weight_chart
from .record_chart import doctor_weight_chart


def _weight_history(request, company, patient):
    """The summary figures and the paginated table of exact check-ins, newest first."""
    weights = WeightEntry.objects.for_company(company).filter(
        patient=patient, patient__company=company,
    ).select_related('recorded_by').order_by('-recorded_on', '-pk')
    page = Paginator(weights, 10).get_page(request.GET.get('weight_page'))
    latest = weights.first()
    first = weights.last()
    path = reverse('portal:patient-detail', args=[patient.pk])
    return weights, {
        'weights': page.object_list,
        'record_weight_page': page,
        'record_weight_total': page.paginator.count,
        'record_weight_first': first,
        'record_weight_latest': latest,
        'record_weight_change': latest.weight_kg - first.weight_kg if latest else None,
        'record_weight_previous_url': f'{path}?weight_page={page.previous_page_number()}#weights' if page.has_previous() else '',
        'record_weight_next_url': f'{path}?weight_page={page.next_page_number()}#weights' if page.has_next() else '',
    }


def record_weight_context(request, company, patient):
    weights, context = _weight_history(request, company, patient)
    # The chart remains bounded independently of the exact-values table page.
    context.update(record_weight_chart=weight_chart(reversed(list(weights[:300]))),
                   record_weight_chart_limited=context['record_weight_total'] > 300)
    return context


def record_weight_tab_context(request, company, membership, patient):
    """The Weight tab: the Overview's weight chart beside the check-in history.

    Clinicians and Super Admins see it as on the Overview, with the goal and projection, the dose
    bars and the consult dots. Practice administrators see the weights and the expected range only.
    """
    weights, context = _weight_history(request, company, patient)
    if membership.has_clinical_access:
        from care.clinical_summary import get_summary
        from .record_overview import dispensed, record_chart, signed_consults

        consults = signed_consults(company, patient)
        context.update(weight_chart=record_chart(company, patient, get_summary(company, patient), consults,
                                                 dispensed(company, patient)),
                       consult_notes=consults, weight_chart_clinical=True)
    else:
        context['weight_chart'] = doctor_weight_chart(
            weights=list(weights.order_by('recorded_on', 'pk').values_list('recorded_on', 'weight_kg')))
    return context
