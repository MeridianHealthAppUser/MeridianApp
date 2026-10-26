"""An authorized page shell and a shared transport for Django-rendered pages.

The shell registry is deliberately explicit: a page with an additional access
gate must register that gate before it can skip its expensive content builder.
Unregistered pages retain their ordinary view and can still return content to
the navigation client. No fragment is a substitute for the original view.
"""

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from django.contrib.staticfiles.storage import staticfiles_storage
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import URLResolver, get_resolver, get_script_prefix
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme


VERSION = 1
NAVIGATION_HEADER = 'X-Meridian-Navigation'
SCOPE_HEADER = 'X-Meridian-Navigation-Scope'


@dataclass(frozen=True)
class Page:
    title: str
    section: str
    access: str = 'member'


# Main destinations, including their GET-level gates. Object editors/downloads
# keep normal SSR until they explicitly provide a lightweight access resolver.
STAFF_PAGES = {
    'desktop-dashboard': Page('Overview', 'overview'),
    'mobile-dashboard': Page('Overview', 'overview'),
    'staff-tasks': Page('My tasks', 'tasks'),
    'patient-list': Page('Patient Records & Messages', 'patients', 'directory'),
    'staff-inbox': Page('Patient messages', 'messages', 'staff_messages'),
    'team-inbox': Page('Team messages', 'team', 'team_messages'),
    'staff-schedule': Page('Calendar & Bookings', 'schedule'),
    'clinical-consultations': Page('Consultation notes', 'consultations', 'clinical'),
    'clinical-labs': Page('Blood tests', 'labs', 'clinical'),
    'treatment-authorisations': Page('Authorisations', 'authorisations', 'clinical'),
    'compounding-list': Page('Compounding records', 'compounding', 'clinical'),
    'treatment-subscriptions': Page('Subscriptions', 'subscriptions', 'admin'),
    'ops-catalogue': Page('Catalogue', 'catalogue', 'admin'),
    'ops-shipping': Page('Weekly shipping', 'shipping', 'shipping'),
    'ops-history': Page('Dispatch history', 'history', 'shipping'),
    'ops-orders': Page('Non-prescription requests', 'orders', 'admin'),
    'staff-leads': Page('Leads and enquiries', 'leads', 'admin'),
    'staff-lead-history': Page('Lead history', 'leads', 'admin'),
    'dropouts': Page('Dropouts', 'dropouts', 'admin'),
    'metrics': Page('Metrics and cohorts', 'metrics', 'super_admin'),
    'activity-statements': Page('Activity statements', 'statements', 'statements'),
    'staff-data-requests': Page('Privacy requests', 'privacy_requests', 'privacy'),
    'policy-list': Page('Policy versions', 'policies', 'policies'),
    'account-access-history': Page('My access history', 'access_history', 'super_admin'),
    'practice-settings': Page('Practice settings', 'practice_settings', 'super_admin'),
    'treatment-review-rules': Page('Review planning rules', 'review_rules', 'super_admin'),
    'management-users': Page('Users and roles', 'users', 'super_admin'),
    'management-practices': Page('Practices', 'practices', 'super_admin'),
    'patient-detail': Page('Patient record', 'record', 'record'),
}
PATIENT_PAGES = {
    'patient-dashboard': Page('Home', 'overview'),
    'patient-appointments': Page('My Appointments', 'appointments'),
    'patient-progress': Page('Your progress', 'progress'),
    'patient-messages': Page('Your messages', 'messages', 'patient_messages'),
    'patient-labs': Page('Blood results', 'labs'),
    'patient-treatment': Page('My Treatment', 'treatment'),
    'patient-pharmacy': Page('My Medications', 'pharmacy'),
    'patient-orders': Page('My orders', 'orders'),
    'patient-medical-profile': Page('Medical profile', 'medical_profile'),
    'patient-updates': Page('Updates', 'updates'),
    'patient-profile': Page('My profile', 'profile'),
}


# HTML transport eligibility is independent of the lightweight-shell access
# registry. These detail/editor views still run their complete original gates.
# Downloads, background fragments, status APIs and POST-only actions must never
# be requested speculatively as page content, even when their URL has no suffix.
HTML_PAGE_ROUTES = frozenset(STAFF_PAGES) | frozenset(PATIENT_PAGES) | frozenset((
    'activity-statement-create', 'activity-statement-detail',
    'appointment-book', 'appointment-detail',
    'clinical-consultation-create', 'clinical-consultation-detail',
    'clinical-lab-create', 'clinical-lab-detail',
    'compounding-create', 'compounding-detail',
    'conversation-manage', 'team-conversation-manage',
    'dropout-detail', 'lead-detail', 'lead-follow-up',
    'email-journeys', 'email-journey-create', 'email-journey-edit', 'email-log',
    'management-user-create', 'management-membership-edit',
    'management-practice-create', 'management-practice-edit',
    'ops-product-create', 'ops-product-detail', 'ops-dose-create', 'ops-dose-detail',
    'ops-starting-plan-create', 'ops-starting-plan-detail',
    'ops-shipping-create', 'ops-shipment-detail', 'ops-order-detail', 'ops-stock',
    'ops-billing', 'ops-billing-detail', 'ops-delivery-address', 'ops-parcel-payment',
    'patient-appointment-detail', 'patient-book-appointment',
    'patient-data-request-create', 'patient-data-request-detail',
    'patient-lab-detail', 'patient-basket', 'patient-order-detail', 'patient-subscription',
    'patient-billing', 'patient-invoice', 'patient-delivery-address',
    'patient-billing-sandbox-mandate', 'patient-billing-sandbox-checkout',
    'patient-details-edit', 'policy-create', 'staff-data-request-detail', 'staff-menu-section',
    'staff-patient-record', 'task-create', 'task-edit',
    'treatment-authorisation-create', 'treatment-authorisation-detail', 'treatment-authorisation-renew',
    'treatment-review-rule-create', 'treatment-review-rule-detail',
))

PATIENT_HTML_PAGE_ROUTES = frozenset(PATIENT_PAGES) | frozenset((
    'patient-appointment-detail', 'patient-book-appointment',
    'patient-data-request-create', 'patient-data-request-detail',
    'patient-lab-detail', 'patient-basket', 'patient-order-detail', 'patient-subscription',
    'patient-billing', 'patient-invoice', 'patient-delivery-address',
    'patient-billing-sandbox-mandate', 'patient-billing-sandbox-checkout',
))
STAFF_HTML_PAGE_ROUTES = HTML_PAGE_ROUTES - PATIENT_HTML_PAGE_ROUTES

# An intentional native/API classification makes new GET endpoints fail the
# registry inventory check until their transport is chosen explicitly.
NATIVE_GET_ROUTES = frozenset((
    'patient-appointment-video-status', 'staff-message-status', 'patient-message-status',
    'public-terms', 'public-privacy', 'public-contact',
    'questionnaire', 'questionnaire-result', 'questionnaire-checkout',
    'appointment-book-times', 'patient-booking-times', 'patient-appointment-calendar',
    'compounding-print', 'metrics-export', 'activity-statement-export',
    'staff-patient-record-export', 'staff-patient-record-history',
    'staff-patient-record-excel', 'staff-patient-record-workbook',
    'ops-manifest', 'ops-week-order', 'ops-history-export',
    'clinical-lab-result-download', 'patient-lab-result-download', 'patient-lab-form',
    'record-conversation-search', 'patient-blood-test-form', 'patient-blood-result-report',
    'email-unsubscribe', 'ops-shipment-label',
    # Preparing a provider card form or settling its return has GET side effects;
    # a content request must be rejected before either view runs.
    'patient-card-form', 'patient-payment-return',
))


def _html_route(match):
    return bool(match and match.namespace == 'portal' and match.url_name in HTML_PAGE_ROUTES)


def navigation_paths(urlconf=None, script_prefix='/', *, layout=None):
    """Return JavaScript-compatible, anchored path patterns for known HTML views."""
    return _resolver_navigation_paths(get_resolver(urlconf), script_prefix, layout)


@lru_cache(maxsize=8)
def _resolver_navigation_paths(resolver, script_prefix, layout):
    paths = []
    routes = {'patient': PATIENT_HTML_PAGE_ROUTES, 'staff': STAFF_HTML_PAGE_ROUTES, None: HTML_PAGE_ROUTES}.get(layout, ())

    def component(pattern):
        value = pattern.regex.pattern.removeprefix('^').removesuffix(r'\Z').removesuffix('$')
        return re.sub(r'\(\?P<[^>]+>', '(?:', value)

    def visit(patterns, prefix='', namespaces=()):
        for entry in patterns:
            path = prefix + component(entry.pattern)
            if isinstance(entry, URLResolver):
                visit(entry.url_patterns, path, namespaces + ((entry.namespace,) if entry.namespace else ()))
            elif namespaces == ('portal',) and entry.name in routes:
                paths.append('^' + re.escape(script_prefix) + path + '$')

    visit(resolver.url_patterns)
    return tuple(dict.fromkeys(paths))


def canonical_url(request):
    query = request.GET.copy()
    query.pop('__shell', None)
    query.pop('__full', None)
    encoded = query.urlencode()
    return request.path + (f'?{encoded}' if encoded else '')


def is_content_request(request):
    return getattr(settings, 'PAGE_NAVIGATION_ENABLED', True) and request.method == 'GET' and request.headers.get(NAVIGATION_HEADER) == 'content'


def is_section_request(request):
    return getattr(settings, 'PAGE_NAVIGATION_ENABLED', True) and request.method == 'GET' and request.headers.get(NAVIGATION_HEADER) == 'section'


def wants_shell(request):
    return (
        getattr(settings, 'PAGE_NAVIGATION_ENABLED', True) and request.method == 'GET'
        and request.GET.get('__full') != '1'
        and request.headers.get(NAVIGATION_HEADER) not in ('content', 'section')
        and not request.headers.get('X-Meridian-Realtime')
        and (request.GET.get('__shell') == '1' or 'text/html' in request.headers.get('Accept', ''))
    )


def bind_navigation_context(request, *, company, membership=None, patient=None):
    """Bind only objects already resolved by the view's tenant/authentication gates."""
    if not getattr(settings, 'PAGE_NAVIGATION_ENABLED', True):
        return None
    if patient is not None:
        layout = 'patient'
        scope = f'patient:{company.pk}:{patient.pk}:{request.user.pk}'
    else:
        layout = 'staff'
        scope = f'staff:{company.pk}:{request.user.pk}:{membership.pk}:{membership.role}:{membership.clinician_type}'
    request._page_navigation = {'scope': scope, 'layout': layout}
    request._page_navigation_company = company
    request._page_navigation_membership = membership
    request._page_navigation_patient = patient
    expected = request.headers.get(SCOPE_HEADER)
    if expected and expected != scope:
        return _json(request, {'error': 'Your account, practice or role changed. Reload this page.'}, status=409)
    return None


def navigation_response(view, request, *, company, membership=None, patient=None):
    """Shared transport steps, called only after the view's tenant/auth gates."""
    if response := bind_navigation_context(request, company=company, membership=membership, patient=patient):
        return response
    if response := maybe_navigation_section(view, request):
        return response
    return maybe_navigation_shell(view, request)


@lru_cache(maxsize=4)
def _source_build_signature(base_dir):
    """Fingerprint deployable source when unhashed static storage is in use.

    Compute once per process/build, from public assets, templates and application
    source only. Git metadata, environment files, databases and uploaded patient
    files are deliberately outside the input set.
    """
    root = Path(base_dir)
    files = set()
    static_types = frozenset(('.css', '.js', '.json', '.map', '.txt', '.svg', '.png', '.jpg', '.jpeg',
                              '.webp', '.gif', '.ico', '.avif', '.woff', '.woff2', '.ttf', '.otf',
                              '.eot', '.pdf', '.webm', '.mp4', '.ogg'))

    def deployable(path, suffixes):
        return (path.is_file() and path.suffix.lower() in suffixes
                and not any(part.startswith('.') or part == '__pycache__' for part in path.relative_to(root).parts))

    for name in ('accounts', 'care', 'config', 'portal', 'practices', 'video'):
        files.update(path for path in (root / name).rglob('*.py') if deployable(path, {'.py'}))
    for name, suffixes in (('static', static_types), ('templates', {'.html', '.txt'})):
        files.update(path for path in (root / name).rglob('*') if deployable(path, suffixes))
    files.update(path for name in ('Dockerfile', 'requirements.txt') if (path := root / name).is_file())
    digest = sha256()
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(65536), b''):
                digest.update(chunk)
        digest.update(b'\0')
    return 'source-' + digest.hexdigest()


def build_signature():
    manifest = getattr(staticfiles_storage, 'manifest_hash', '')
    return manifest or _source_build_signature(str(settings.BASE_DIR))


def _metadata(request):
    return {
        'version': VERSION, 'canonical_url': canonical_url(request),
        'build': build_signature(),
        'navigation': {'route': getattr(getattr(request, 'resolver_match', None), 'url_name', '')},
        **getattr(request, '_page_navigation', {}),
    }


def page_navigation_context(request):
    context = {}
    if getattr(request, '_page_navigation', None):
        context['page_navigation_config'] = {
            **_metadata(request), 'shell': bool(getattr(request, '_navigation_shell', False)),
            'navigation_paths': navigation_paths(getattr(request, 'urlconf', None), get_script_prefix(),
                                                 layout=request._page_navigation['layout']),
        }
    if getattr(request, '_navigation_shell', False):
        # Rendering the empty shell must not consume the flash-message storage.
        context['messages'] = []
    return context


def _private(response):
    add_never_cache_headers(response)
    patch_vary_headers(response, ('Accept', NAVIGATION_HEADER, SCOPE_HEADER, 'X-Meridian-Section'))
    return response


def _json(request, data, *, status=200):
    return _private(JsonResponse({**_metadata(request), **data}, status=status))


def _check_page_access(view, request, page):
    """The checks the normal registered GET runs before any page-specific data."""
    membership = getattr(view, 'membership', None)
    company = getattr(view, 'company', None) or getattr(view, 'patient_company', None)
    access = page.access
    if access == 'admin' and not membership.is_admin:
        raise PermissionDenied('This page is for practice administrators.')
    if access == 'shipping':
        from care.operations import can_ship
        if not can_ship(company, membership):
            raise PermissionDenied('This page is for practice administrators.')
    if access == 'super_admin' and membership.role != 'super_admin':
        raise PermissionDenied('This page is for practice Super Admins.')
    if access == 'clinical' and not membership.has_clinical_access:
        raise PermissionDenied('Clinical records are available to clinicians and practice Super Admins only.')
    if access == 'statements' and not (membership.is_clinician or membership.role == 'super_admin'):
        raise PermissionDenied('Activity statements are available to the clinician and practice Super Admin.')
    if access == 'directory' and membership.role == 'practice_admin':
        return redirect('portal:staff-inbox')
    if access in ('privacy', 'policies'):
        from care.privacy import require_privacy_admin
        require_privacy_admin(request.user, company, policies=access == 'policies')
    selected_thread = 'thread' in request.GET if access in ('patient_messages', 'team_messages') else bool(request.GET.get('thread'))
    if access in ('staff_messages', 'team_messages', 'patient_messages') and selected_thread:
        from .patient_views import _thread_id
        thread_id = _thread_id(request.GET['thread'])
        if access == 'staff_messages':
            from .inbox import inbox_thread
            inbox_thread(company, request.user, thread_id)
        elif access == 'team_messages':
            from care.models import TeamThread
            get_object_or_404(TeamThread.objects.for_company(company), pk=thread_id, members=request.user)
        else:
            from care.models import MessageThread
            get_object_or_404(MessageThread.objects.for_company(company), pk=thread_id, patient=view.patient)
    if access == 'record':
        from practices.models import Patient
        from .patient_workspace import CLINICAL_TABS, TABS, hidden_tab_destination, shows_tab
        from .record_access import require_record_view
        patient = get_object_or_404(Patient.objects.for_company(company), pk=view.kwargs['pk'], is_active=True)
        require_record_view(membership, request.user, patient)
        tab = request.GET.get('tab', 'messages' if 'thread' in request.GET else 'weights' if 'weight_page' in request.GET else 'overview')
        if tab not in dict(TABS):
            raise Http404
        if tab in CLINICAL_TABS and not membership.has_clinical_access:
            raise PermissionDenied('This clinical section is restricted to clinicians and practice Super Admins.')
        if not shows_tab(membership, request.user, patient, tab):
            return redirect(hidden_tab_destination(request, patient, tab))
        if tab == 'messages' and 'thread' in request.GET:
            from .patient_views import _thread_id
            from care.messaging import staff_threads
            get_object_or_404(staff_threads(company, request.user).filter(patient=patient), pk=_thread_id(request.GET['thread']))
    return None


def maybe_navigation_shell(view, request):
    """Called after the common tenant gate, before a registered GET builds data."""
    if not wants_shell(request):
        return None
    match = request.resolver_match
    if match is None or match.namespace != 'portal':
        return None
    layout = getattr(request, '_page_navigation', {}).get('layout')
    page = (PATIENT_PAGES if layout == 'patient' else STAFF_PAGES).get(match.url_name)
    if page is None:
        return None
    if response := _check_page_access(view, request, page):
        return response
    request._navigation_shell = True
    # Shell navigation renders identity and permitted links; dynamic badges catch
    # up through the existing authorized status endpoint after the page loads.
    request._staff_message_counts = {'patient_unread_total': 0, 'team_unread_total': 0}
    request._open_data_requests = 0
    company = request._page_navigation_company
    membership = request._page_navigation_membership
    patient = request._page_navigation_patient
    context = {
        'company': company, 'active_company': company if layout == 'staff' else None,
        'active_membership': membership, 'patient_company': company, 'patient': patient,
        'is_patient_portal': layout == 'patient', 'page_title': page.title, 'nav_section': page.section,
        'navigation_layout': layout, 'patient_section': page.section, 'patient_unread_messages': 0,
        'full_page_url': canonical_url(request) + ('&' if '?' in canonical_url(request) else '?') + '__full=1',
    }
    if layout == 'patient':
        from .patient_views import PATIENT_NAV_GROUPS
        context['patient_nav'] = PATIENT_NAV_GROUPS.get(page.section, '')
    return _private(render(request, 'portal/navigation_shell.html', context))


def maybe_navigation_section(view, request):
    """Small registered sections load after the destination's primary content."""
    if not is_section_request(request):
        return None
    route = getattr(request.resolver_match, 'url_name', '')
    section = request.headers.get('X-Meridian-Section')
    if route not in ('desktop-dashboard', 'mobile-dashboard') or section != 'dashboard-metrics':
        return _json(request, {'error': 'This page section was not found.'}, status=404)
    if view.membership.role != 'super_admin':
        raise PermissionDenied('Practice metrics are available to practice Super Admins.')
    from .views import staff_dashboard_metrics
    html = render_to_string('portal/includes/dashboard_metrics.html', {
        'metrics': staff_dashboard_metrics(view.company, request.user, view.membership),
        'metrics_class': 'mobile-metrics' if route == 'mobile-dashboard' else 'console-metrics',
        'metrics_empty_title': 'Care activity' if route == 'mobile-dashboard' else 'Practice activity',
        'metrics_empty_hint': 'New activity will show here.' if route == 'mobile-dashboard' else 'Metrics will appear as care is recorded.',
    })
    return _json(request, {'section': section, 'html': html})


class _RenderedPage(HTMLParser):
    """Extract trusted server-rendered content; never re-render or invoke a view."""
    VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'))

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.found = False
        self.parts = []
        self.title = []
        self.in_title = False
        self.styles = []
        self.scripts = []
        self.body_class = ''
        self.extras = {'live_messages_html': {'depth': 0, 'parts': []}, 'toast_html': {'depth': 0, 'parts': []}}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name, capture in self.extras.items():
            begins = ('data-live-messages' in attrs if name == 'live_messages_html'
                      else 'toast-stack' in (attrs.get('class') or '').split())
            if capture['depth'] or (begins and not capture['parts']):
                capture['parts'].append(self.get_starttag_text())
                if tag not in self.VOID:
                    capture['depth'] += 1
        if tag == 'link' and attrs.get('rel') == 'stylesheet' and attrs.get('href'):
            self.styles.append(attrs['href'])
        if tag == 'script' and attrs.get('src'):
            self.scripts.append(attrs['src'])
        if tag == 'title':
            self.in_title = True
        if tag == 'body':
            self.body_class = attrs.get('class', '')
        if not self.found and tag == 'main' and attrs.get('id') == 'workspace-content':
            self.depth = 1
            self.found = True
        elif self.depth:
            self.parts.append(self.get_starttag_text())
            if tag not in self.VOID:
                self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            # Preserve the original self-closing SVG/XML tag without inventing
            # a second closing tag in the returned fragment.
            if self.depth:
                self.depth -= 1
            for capture in self.extras.values():
                if capture['depth']:
                    capture['depth'] -= 1

    def handle_endtag(self, tag):
        for capture in self.extras.values():
            if capture['depth'] and tag not in self.VOID:
                capture['parts'].append(f'</{tag}>')
                capture['depth'] -= 1
        if tag == 'title':
            self.in_title = False
        if self.depth and tag not in self.VOID:
            self.depth -= 1
            if self.depth:
                self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        for capture in self.extras.values():
            if capture['depth']:
                capture['parts'].append(data)
        if self.depth:
            self.parts.append(data)
        if self.in_title:
            self.title.append(data)

    def handle_entityref(self, name):
        self.handle_data(f'&{name};')

    def handle_charref(self, name):
        self.handle_data(f'&#{name};')

    def handle_comment(self, data):
        for capture in self.extras.values():
            if capture['depth']:
                capture['parts'].append(f'<!--{data}-->')
        if self.depth:
            self.parts.append(f'<!--{data}-->')


def _local_url(request, url):
    url = urljoin(request.build_absolute_uri(), url)
    if not url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return None
    value = urlsplit(url)
    return (value.path or '/') + (f'?{value.query}' if value.query else '') + (f'#{value.fragment}' if value.fragment else '')


class PageNavigationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Fail closed before invoking a download/action view. In particular, do
        # not record an extra download audit before falling back to the browser.
        content, section = is_content_request(request), is_section_request(request)
        if (content or section) and not _html_route(request.resolver_match):
            if section:
                return _json(request, {'error': 'This page section was not found.'}, status=404)
            view_class = getattr(view_func, 'view_class', None)
            if view_class is not None and ('get' not in view_class.http_method_names or not hasattr(view_class, 'get')):
                # Preserve the original method/authentication response for a
                # GET sent to a POST-only action; no GET body can execute.
                return None
            return _json(request, {'full_navigation': canonical_url(request)}, status=409)

    def __call__(self, request):
        response = self.get_response(request)
        section = is_section_request(request)
        if not (is_content_request(request) or section):
            return response
        if section and response.get('Content-Type', '').startswith('application/json'):
            return _private(response)
        # Preserve the normal view's redirect before fetch follows it and would
        # otherwise inject a login/error destination under the requested URL.
        if 300 <= response.status_code < 400 and response.get('Location'):
            target = _local_url(request, response['Location'])
            if target:
                return _json(request, {'redirect_url': target})
            return _json(request, {'error': 'This destination requires a full page navigation.'}, status=409)
        if response.status_code >= 400:
            if response.get('Content-Type', '').startswith('application/json'):
                return _private(response)
            labels = {401: 'Please sign in to continue.', 403: 'You do not have access to this page.',
                      404: 'This page was not found.', 409: 'Your account or practice changed. Reload this page.'}
            return _json(request, {'error': labels.get(response.status_code, 'This page could not be loaded. Please try again.')}, status=response.status_code)
        if response.streaming or not response.get('Content-Type', '').startswith('text/html') or response.get('Content-Disposition'):
            return _json(request, {'full_navigation': canonical_url(request)}, status=409)
        if not getattr(request, '_page_navigation', None):
            return _json(request, {'full_navigation': canonical_url(request)}, status=409)
        page = _RenderedPage()
        page.feed(response.content.decode(response.charset))
        if not page.found or page.depth:
            return _json(request, {'full_navigation': canonical_url(request)}, status=409)
        # Another site's script (Peach's card form) is never added to a swapped-in page, so that page loads fully.
        if any(_local_url(request, value) is None for value in page.scripts):
            return _json(request, {'full_navigation': canonical_url(request)}, status=409)
        def assets(values):
            return list(dict.fromkeys(url for value in values if (url := _local_url(request, value))))
        return _json(request, {
            'title': unescape(''.join(page.title)), 'html': ''.join(page.parts),
            'assets': {'styles': assets(page.styles), 'scripts': assets(page.scripts)},
            'body_class': page.body_class,
            **{name: ''.join(capture['parts']) for name, capture in page.extras.items()},
        })
