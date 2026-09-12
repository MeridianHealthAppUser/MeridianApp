"""Presentation-only patient context for editors also used by global work lists.

The marker never selects a patient or grants access. Every caller must first
resolve its record through the existing permission- and company-scoped queryset.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse


def uses_patient_workspace(request):
    return request.GET.get('workspace') == 'patient' or request.POST.get('workspace') == 'patient'


def patient_action_context(view, patient, tab, *, content_template, stylesheets=(), title=None):
    if not uses_patient_workspace(view.request) or patient is None:
        return {}
    if patient.company_id != view.company.pk or not patient.is_active:
        raise Http404
    from .patient_workspace import TABS, patient_workspace_context, workspace_url

    context = patient_workspace_context(view.request, view.company, view.membership, patient, tab)
    context.update(action_content_template=content_template, action_stylesheets=stylesheets,
                   workspace_return_url=workspace_url(patient, tab),
                   workspace_return_label=f'Back to {dict(TABS)[tab].lower()}',
                   workspace_action_title=title or view.page_title, workspace_query='?workspace=patient')
    return context


def workspace_destination(request, url, *, force=False):
    if not force and not uses_patient_workspace(request):
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != 'workspace']
    query.append(('workspace', 'patient'))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def workspace_redirect(request, route, **kwargs):
    return redirect(workspace_destination(request, reverse(route, kwargs=kwargs)))
