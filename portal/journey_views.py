"""The email journeys' pages (care.journeys): Settings > Email journeys for Super Admins, the communications log under
Administration, and the public unsubscribe page a journey email links to."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from care.journeys import Member, default_journeys, render as render_journey, unsubscribe
from care.models import EmailDelivery, EmailJourney
from care.services import record_audit
from practices.models import CompanyMembership

from .filter_menus import TickedChoiceField, list_tools, ticked_sections, without_all
from .views import StaffCompanyRequiredMixin


class JourneyForm(forms.ModelForm):
    class Meta:
        model = EmailJourney
        fields = ('name', 'track', 'delay_days', 'subject_template', 'body_template', 'button', 'is_active')
        labels = {'name': 'Name (for the practice)', 'is_active': 'Send this email'}
        widgets = {'body_template': forms.Textarea(attrs={'rows': 8})}
        help_texts = {'delay_days': 'Counted from the day the person joins the track.',
                      'subject_template': '{first_name} is replaced with the person’s first name.'}


@method_decorator(never_cache, name='dispatch')
class JourneyListView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Settings > Email journeys: each track and its emails, in the order they go. Super Admins only."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can change the email journeys.')
        default_journeys(self.company)
        journeys = list(EmailJourney.objects.filter(company=self.company))
        tracks = [{'value': value, 'label': label, 'emails': [journey for journey in journeys if journey.track == value]}
                  for value, label in EmailJourney.Track.choices]
        return render(request, 'portal/email_journeys.html', dict(
            company=self.company, active_membership=self.membership, nav_section='email_journeys', page_title='Email journeys',
            tracks=tracks, active_count=sum(1 for journey in journeys if journey.is_active)))


@method_decorator(never_cache, name='dispatch')
class JourneyEditView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def journey(self, pk):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can change the email journeys.')
        return get_object_or_404(EmailJourney.objects.filter(company=self.company), pk=pk) if pk else None

    def display(self, request, journey, form, status=200):
        preview = None
        if journey is not None:
            sample = Member(journey.track, None, 'preview', request.user.email, request.user.first_name or 'Nadia')
            message = render_journey(journey, sample)
            preview = {'subject': message.subject, 'body': message.body}
        return render(request, 'portal/email_journey_form.html', dict(
            company=self.company, active_membership=self.membership, nav_section='email_journeys', page_title='Email journey',
            journey=journey, form=form, preview=preview), status=status)

    def get(self, request, pk=None):
        journey = self.journey(pk)
        initial = {'track': request.GET.get('track')} if journey is None and request.GET.get('track') in EmailJourney.Track.values else None
        return self.display(request, journey, JourneyForm(instance=journey, initial=initial))

    def post(self, request, pk=None):
        journey = self.journey(pk)
        form = JourneyForm(request.POST, instance=journey or EmailJourney(company=self.company))
        if not form.is_valid():
            return self.display(request, journey, form, status=400)
        saved = form.save()
        record_audit(company=self.company, actor=request.user, action='journey.updated' if journey else 'journey.created',
                     target=saved, request=request, metadata={'changed_fields': form.changed_data, 'active': saved.is_active})
        messages.success(request, f'“{saved.name}” saved{" and switched on" if saved.is_active else ", switched off"}.')
        return redirect('portal:email-journeys')


class EmailLogFilterForm(forms.Form):
    q = forms.CharField(label='Search emails', required=False, max_length=100)
    kind = TickedChoiceField(label='Email', choices=(
        ('journey', 'Journey emails'), ('appointment', 'Appointments'), ('patient_message', 'Messages'),
        ('payment', 'Billing'), ('welcome', 'Welcome'), ('subscription_winback', 'Win-back')))


KIND_GROUPS = {
    'appointment': ('appointment_booked', 'appointment_moved', 'appointment_cancelled', 'appointment_reminder'),
    'payment': ('payment_failed', 'debit_order_update', 'debit_order_request'),
}


@method_decorator(never_cache, name='dispatch')
class EmailLogView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Administration > Emails: the communications log, every email sent to a patient or an enquiry."""

    admin_only = True
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = EmailLogFilterForm(without_all(request.GET, 'kind'))
        log = EmailDelivery.objects.filter(company=self.company).select_related('patient', 'lead', 'journey')
        if form.is_valid():
            kinds = [kind for group in form.cleaned_data['kind'] for kind in KIND_GROUPS.get(group, (group,))]
            if kinds:
                log = log.filter(kind__in=kinds)
            for term in form.cleaned_data['q'].split():
                log = log.filter(Q(recipient_email__icontains=term) | Q(subject__icontains=term)
                                 | Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                 | Q(lead__first_name__icontains=term) | Q(lead__last_name__icontains=term))
        else:
            log = log.none()
        page = Paginator(log.order_by('-created_at', '-pk'), 50).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        return render(request, 'portal/email_log.html', dict(
            company=self.company, active_membership=self.membership, nav_section='emails', page_title='Emails',
            filter_form=form, page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            count_label=f'{page.paginator.count} email{pluralize(page.paginator.count)}',
            **list_tools(action=reverse('portal:email-log'), data=request.GET, form=form, label='Filter emails',
                         sections=ticked_sections(form, request.GET, ('kind',)), placeholder='Name, email or subject')))


@method_decorator(csrf_exempt, name='dispatch')
class UnsubscribeView(View):
    """The unsubscribe link at the foot of a journey email. Opening it asks to confirm, so a mail scanner following
    links doesn't unsubscribe anyone; an email app's one-click unsubscribe POSTs straight here (RFC 8058)."""

    http_method_names = ('get', 'post', 'head', 'options')

    def get(self, request, token):
        return render(request, 'portal/email_unsubscribe.html', {'token': token, 'done': False})

    def post(self, request, token):
        person = unsubscribe(token)
        return render(request, 'portal/email_unsubscribe.html', {'token': token, 'done': person is not None,
                                                                  'invalid': person is None}, status=200 if person else 400)
