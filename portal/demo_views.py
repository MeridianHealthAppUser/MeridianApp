"""Explicit, Super Admin-only lifecycle controls for a practice's managed demo data."""
from types import SimpleNamespace
from urllib.parse import urlsplit

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from care.demo_data import clear_demo_data, create_demo_data, demo_status, rebuild_demo_data
from practices.models import CompanyMembership, DemoDataSet
from practices.services import active_membership_for, get_active_company
from .operations_views import OperationsView
from .workflow_context import make_workflow_context, validate_workflow_context


class DemoActionForm(forms.Form):
    next = forms.CharField(required=False, max_length=2048)
    dataset = forms.UUIDField(required=False)
    confirm = forms.ChoiceField(choices=(('yes', 'Confirm'),), required=False)

    def __init__(self, *args, action, **kwargs):
        super().__init__(*args, **kwargs)
        if action in ('clear', 'rebuild'):
            self.fields['dataset'].required = True
            self.fields['confirm'].required = True
            self.fields['confirm'].error_messages['required'] = 'Confirm that you want to change this managed demo dataset.'


def demo_return_url(request):
    data = request.POST if request.method == 'POST' else request.GET
    value = data.get('next', '').strip()
    fallback = reverse('portal:desktop-dashboard')
    if not value or len(value) > 2048 or not url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return fallback
    parsed = urlsplit(value)
    if not parsed.path.startswith('/') or parsed.path.startswith('//') or parsed.path.startswith('/demo-data/'):
        return fallback
    return parsed.path + ('?' + parsed.query if parsed.query else '')


def dataset_record(dataset):
    # Operational contexts use JSON, so represent the dataset UUID as a string.
    return SimpleNamespace(pk=str(dataset.pk), updated_at=dataset.updated_at)


class DemoDataView(OperationsView):
    nav_section, page_title, shipping = 'overview', 'Demo data', False
    http_method_names = ('get', 'head', 'options')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            company = get_active_company(request)
            membership = active_membership_for(request, company)
            if membership is None or membership.role != CompanyMembership.Role.SUPER_ADMIN:
                raise PermissionDenied('Demo data is managed by Super Admins.')
        return super().dispatch(request, *args, **kwargs)

    def display(self, request, *, error='', status=200):
        state = dict(demo_status(self.company))
        dataset = state.get('dataset')
        state['created_at'] = dataset.created_at if dataset is not None else None
        state['total'] = sum(state.get('counts', {}).values())
        keys = ('clear', 'rebuild') if state['active'] else ('create',)
        labels = {'create': 'Create demo data', 'clear': 'Delete demo data', 'rebuild': 'Rebuild demo data'}
        actions = []
        contexts = {}
        for key in keys:
            token = make_workflow_context(request, self.company, f'demo-{key}',
                                          dataset_record(dataset) if key != 'create' else None)
            contexts[f'demo_context_{key}'] = token
            actions.append(dict(key=key, label=labels[key], url=reverse(f'portal:demo-data-{key}'),
                workflow_context=token, dataset_id=str(dataset.pk) if dataset is not None else '',
                destructive=key != 'create'))
        counts = [{'label': str(key).replace('_', ' ').capitalize(), 'value': value}
                  for key, value in state.get('counts', {}).items()]
        return_url = demo_return_url(request)
        context = self.context(demo_status=state, demo_counts=counts, demo_actions=actions,
            demo_return_url=return_url, demo_next=return_url, demo_error=error, **contexts)
        fragment = request.method == 'GET' and request.GET.get('fragment') == '1'
        response = render(request, 'portal/includes/demo_data_content.html' if fragment else 'portal/demo_data.html',
                          context, status=status)
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        if fragment:
            response['X-Demo-Data-Fragment'] = '1'
        return response

    def get(self, request):
        return self.display(request)


class DemoDataActionView(DemoDataView):
    http_method_names = ('post', 'options')
    action = None

    def post(self, request):
        if self.action not in ('create', 'clear', 'rebuild'):
            raise Http404
        form = DemoActionForm(request.POST, action=self.action)
        if not form.is_valid():
            error = ' '.join(str(message) for errors in form.errors.values() for message in errors)
            return self.display(request, error=error, status=400)
        dataset = None
        if self.action != 'create':
            dataset = get_object_or_404(DemoDataSet, company=self.company, pk=form.cleaned_data['dataset'])
        try:
            intent = validate_workflow_context(request, self.company, f'demo-{self.action}',
                dataset_record(dataset) if dataset is not None else None, check_version=False)
            if self.action == 'create':
                create_demo_data(company=self.company, actor=request.user, submission_key=intent['key'], request=request)
            else:
                service = clear_demo_data if self.action == 'clear' else rebuild_demo_data
                service(company=self.company, actor=request.user, dataset=dataset,
                    submission_key=intent['key'], expected_updated=intent['updated'], request=request)
        except ValidationError as error:
            return self.display(request, error=' '.join(error.messages), status=400)
        messages.success(request, {'create': 'Demo data created.', 'clear': 'Demo data deleted.',
                                  'rebuild': 'Demo data rebuilt.'}[self.action])
        return redirect(demo_return_url(request))
