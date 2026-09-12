"""Bounded, practice-scoped weight presentation for the staff patient record."""

from django.core.paginator import Paginator
from django.urls import reverse

from care.models import WeightEntry
from .patient_progress import weight_chart


def record_weight_context(request, company, patient):
    weights = WeightEntry.objects.for_company(company).filter(
        patient=patient, patient__company=company,
    ).select_related('recorded_by').order_by('-recorded_on', '-pk')
    page = Paginator(weights, 10).get_page(request.GET.get('weight_page'))
    latest = weights.first()
    first = weights.last()
    # The chart remains bounded independently of the exact-values table page.
    chart = weight_chart(reversed(list(weights[:300])))
    path = reverse('portal:patient-detail', args=[patient.pk])
    return {
        'weights': page.object_list,
        'record_weight_page': page,
        'record_weight_total': page.paginator.count,
        'record_weight_first': first,
        'record_weight_latest': latest,
        'record_weight_change': latest.weight_kg - first.weight_kg if latest else None,
        'record_weight_chart': chart,
        'record_weight_chart_limited': page.paginator.count > 300,
        'record_weight_previous_url': f'{path}?weight_page={page.previous_page_number()}#weights' if page.has_previous() else '',
        'record_weight_next_url': f'{path}?weight_page={page.next_page_number()}#weights' if page.has_next() else '',
    }
