"""Stitch (care.payment_providers): DebiCheck debit orders, cards kept for the subscription, and once-off payments,
through Stitch's GraphQL API (docs.stitch.money, October 2026).

- A debit order is a Stitch payment consent request. DebiCheck: the patient adds their bank account on Stitch's page,
  then approves the mandate in their banking app. Card: they enter the card on Stitch's page. Either way they come
  back to the app's return page, and the consent's id is what the app collects with.
- Collecting a card: initiateTransaction on the kept card (the first charge the patient's own, later ones the
  practice's), answered at once.
- Collecting a DebiCheck debit order: not an API call. Each collection goes in a CSV collection file, at least three
  days before its date and on the mandate's weekday; Stitch's daily OUTPUT file says which were paid. Until Stitch
  gives the practice SFTP access, the files are downloaded and uploaded on Operations > Billing (or with
  `manage.py stitch_debicheck`).
- DebiCheck mandates are weekly, monthly and so on: there is no "every 4 weeks". A weekly mandate on the collection
  weekday is used, and collected every billing period (4 weeks by default). A variable mandate's maximum can't be more
  than 1.5 times its usual amount, so the usual amount is raised when the prescription's highest price needs it.
- Once-off payments: a Stitch payment request (card or bank), on Stitch's page.
- Webhooks are signed (Svix): payment-consent-request, payment and transaction events settle what they name.

Stitch issues test clients itself; none was available when this was written, so it was checked against Stitch's
published schema and examples only.
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .payment_providers import (
    Approval, Collection, Notice, ProviderUnavailable, http_call, payment_reference, record_id, return_url, webhook_secret,
)

TOKEN_URL = 'https://secure.stitch.money/connect/token'
API_URL = 'https://api.stitch.money/graphql'
SCOPES = 'client_paymentrequest client_recurringpaymentconsentrequest transaction_initiate'
# A DebiCheck collection must be in a file at least this many days before its date.
DEBICHECK_LEAD_DAYS = 3
WEBHOOK_TOLERANCE_SECONDS = 300
_token = {'value': '', 'expires': 0.0}

CONSENT_STATES = {
    'PaymentConsentGranted': 'approved', 'PaymentConsentFailed': 'declined', 'PaymentConsentCancelled': 'declined',
    'PaymentConsentExpired': 'declined', 'PaymentConsentRevoked': 'revoked', 'PaymentConsentPaused': 'revoked',
}
WEBHOOK_CONSENT_STATES = {'GRANTED': 'approved', 'FAILED': 'declined', 'CANCELLED': 'declined', 'EXPIRED': 'declined',
                          'REVOKED': 'revoked', 'PAUSED': 'revoked'}
REASONS = {
    'insufficientFunds': 'Insufficient funds.', 'INSUFFICIENT_FUNDS': 'Insufficient funds.',
    'PAYMENT_SUSPENDED': 'Stopped by the account holder.', 'AUTHENTICATION_REQUIRED': 'The bank needs the debit order approved again.',
    'MANDATE_SUSPENDED': 'The debit order is suspended at the bank.', 'INACTIVE_ACCOUNT': 'The bank account is closed or frozen.',
    'INVALID_ACCOUNT': 'The bank account can’t be debited.', 'BANK_ERROR': 'The bank had a system error.',
    'MFA_REJECTED': 'The patient declined it in their banking app.', 'MFA_EXPIRED': 'It wasn’t approved in the banking app in time.',
    'INVALID_PERSONAL_DETAILS': 'The bank couldn’t match the patient’s details.', 'ACCOUNT_INVALID': 'The bank account isn’t valid.',
}


def _reason(code, fallback):
    return REASONS.get(code, f'{fallback} ({code})' if code else fallback)


def _money(amount):
    return {'quantity': f'{Decimal(amount):.2f}', 'currency': 'ZAR'}


def _with_param(url, name, value):
    """Stitch's page, with where to send the patient back (its domain allowlisted by Stitch)."""
    return f"{url}{'&' if '?' in url else '?'}{name}={quote(value, safe='')}"


def collection_date(mandate, earliest, today=None):
    """The first day on or after earliest, and at least the lead time from today, on the mandate's weekday."""
    today = today or timezone.localdate()
    day = max(earliest, today + timedelta(days=DEBICHECK_LEAD_DAYS))
    weekday = mandate.collection_day or day.isoweekday()
    return day + timedelta(days=(weekday - day.isoweekday()) % 7)


class StitchProvider:
    name = 'stitch'
    label = 'Stitch'
    hosted = True
    methods = ('debicheck', 'card')

    def lead_days(self, mandate):
        return DEBICHECK_LEAD_DAYS if mandate.method == 'debicheck' else 0

    # The API

    def _access_token(self):
        if not (settings.STITCH_CLIENT_ID and settings.STITCH_CLIENT_SECRET):
            raise ValidationError('Stitch isn’t set up: add STITCH_CLIENT_ID and STITCH_CLIENT_SECRET to the app’s settings.')
        if _token['value'] and _token['expires'] > time.time() + 60:
            return _token['value']
        status, response = http_call('POST', TOKEN_URL, form={
            'grant_type': 'client_credentials', 'client_id': settings.STITCH_CLIENT_ID,
            'client_secret': settings.STITCH_CLIENT_SECRET, 'scope': SCOPES, 'audience': TOKEN_URL,
        })
        if status != 200 or not response.get('access_token'):
            raise ValidationError(f'Stitch refused the app’s credentials: {response.get("error_description") or response.get("error") or status}')
        _token.update(value=response['access_token'], expires=time.time() + int(response.get('expires_in') or 3600))
        return _token['value']

    def graphql(self, query, variables, *, idempotency_key=''):
        headers = {'Authorization': f'Bearer {self._access_token()}'}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        _, response = http_call('POST', API_URL, headers=headers, body={'query': query, 'variables': variables})
        if response.get('errors'):
            message = '; '.join(str(error.get('message', error)) for error in response['errors'])[:300]
            raise ValidationError(f'Stitch refused the request: {message}')
        return response.get('data') or {}

    # Debit orders: a payment consent request

    def consent_input(self, mandate):
        patient, subscription = mandate.patient, mandate.subscription
        payer = {'payerId': f'patient-{patient.pk}', 'fullName': f'{patient.first_name} {patient.last_name}'.strip()}
        if patient.user_id and patient.user.email:
            payer['email'] = patient.user.email
        if patient.phone:
            payer['mobileNumber'] = patient.phone
        # A South African ID number (13 digits). Anything else (a passport) is left for Stitch's page to ask for.
        id_number = ''.join(patient.id_number.split())
        if len(id_number) == 13 and id_number.isdigit():
            payer['identifyingDocument'] = {'identityDocument': {'number': id_number, 'country': 'ZA'}}
        if mandate.method == 'card':
            methods = {'card': {'enabled': True, 'recurringPayment': {
                'agreement': {'reference': mandate.reference, 'scenario': 'StandingOrder', 'variability': 'variable'},
                'source': 'payer'}}}
        else:
            from .billing import price_on

            # Collected on the weekday of the subscription's debit date; a first debit is as soon as a collection can be.
            first = (subscription.next_debit_on if subscription and subscription.next_debit_on
                     else timezone.localdate() + timedelta(days=DEBICHECK_LEAD_DAYS))
            mandate.collection_day = first.isoweekday()
            usual = price_on(subscription, timezone.localdate()) if subscription else None
            # A variable mandate's maximum is at most 1.5 times its usual amount.
            usual = max(usual or mandate.amount, Decimal(math.ceil(mandate.amount / Decimal('1.5'))))
            methods = {'debicheck': {'enabled': True, 'details': {
                'contractReference': mandate.reference,
                'collection': {'collectionFrequency': 'weekly', 'collectionDay': mandate.collection_day,
                               'instalmentAmount': _money(min(usual, mandate.amount)),
                               'maximumCollectionAmount': _money(mandate.amount)},
            }}}
        return {'nonce': str(uuid.uuid4()), 'externalReference': mandate.reference, 'payerInformation': payer,
                'paymentMethods': methods}

    def start_mandate(self, mandate):
        data = self.graphql('''mutation CreateConsent($input: CreatePaymentConsentRequestInput!) {
              paymentConsentRequestCreate(input: $input) { id url } }''', {'input': self.consent_input(mandate)})
        consent = data.get('paymentConsentRequestCreate') or {}
        if not consent.get('id') or not consent.get('url'):
            raise ValidationError('Stitch didn’t return a page to approve the debit order on.')
        mandate.provider_reference = consent['id']
        return _with_param(consent['url'], 'returnUrl' if mandate.method == 'debicheck' else 'redirect_uri',
                           return_url('mandate', mandate))

    def finish_mandate(self, mandate, params):
        try:
            data = self.graphql('''query Consent($id: ID!) { node(id: $id) { ... on PaymentConsentRequest {
                  id status { __typename } statusReason } } }''', {'id': mandate.provider_reference})
        except ProviderUnavailable:
            return Approval('pending')
        node = data.get('node') or {}
        state = CONSENT_STATES.get((node.get('status') or {}).get('__typename'), 'pending')
        if state == 'approved':
            return Approval('approved', reference=node.get('id', mandate.provider_reference), account_label=self._label(mandate))
        if state in ('declined', 'revoked'):
            return Approval(state, reason=_reason(node.get('statusReason'), 'Not approved.'))
        return Approval('pending')

    def _label(self, mandate):
        """A label for the approved debit order: the bank account's or card's last digits, when Stitch says them."""
        try:
            data = self.graphql('''query ConsentDetails($id: ID!) { node(id: $id) { ... on PaymentConsentRequest {
                  consentDetails { ... on DebicheckPaymentConsentDetails { customer { accountNumber bankId } }
                                   ... on CardPaymentConsentDetails { card { last4 network } } } } } }''',
                                {'id': mandate.provider_reference})
            details = (data.get('node') or {}).get('consentDetails') or {}
        except (ValidationError, ProviderUnavailable):
            details = {}
        if details.get('customer', {}).get('accountNumber'):
            customer = details['customer']
            return f"{str(customer.get('bankId') or 'Bank').upper()} ···{customer['accountNumber'][-4:]}"
        if (details.get('card') or {}).get('last4'):
            return f"{details['card'].get('network') or 'Card'} ···{details['card']['last4']}"
        return 'DebiCheck debit order' if mandate.method == 'debicheck' else 'Card'

    def cancel_mandate(self, mandate):
        if not mandate.provider_reference:
            return
        try:
            self.graphql('''mutation Revoke($id: ID!, $reason: String!) {
                  paymentConsentRequestRevoke(consentRequestId: $id, reason: $reason) { id } }''',
                         {'id': mandate.provider_reference, 'reason': 'GENERAL'})
        except (ValidationError, ProviderUnavailable):
            pass  # Cancelled here either way; nothing more is collected with it.

    # Collecting

    def collect(self, payment, mandate):
        if mandate.method == 'debicheck':
            # Sent with the next collection file; Stitch's OUTPUT file settles it.
            return Collection('pending', reason='Waiting for the next DebiCheck collection file.')
        from .models import Payment

        first = not Payment.objects.filter(provider=self.name, subscription_cycle__mandate_id=mandate.pk, checkout=False,
                                           status=Payment.Status.PAID).exists()
        reference = payment_reference(payment)
        try:
            data = self.graphql('''mutation Charge($input: TransactionInitiateInput!) {
                  initiateTransaction(input: $input) { id state { __typename ... on TransactionFailure { reason } } } }''',
                                {'input': {
                                    'nonce': reference, 'amount': _money(payment.amount), 'externalReference': reference,
                                    'token': mandate.provider_reference,
                                    'payerInformation': {'payerId': f'patient-{payment.patient_id}'},
                                    'paymentMethods': {'card': {'recurringPayment': {
                                        'agreement': {'reference': mandate.reference, 'scenario': 'StandingOrder',
                                                      'variability': 'variable'},
                                        'source': 'payer' if first else 'merchant'}}},
                                }}, idempotency_key=reference)
        except ProviderUnavailable:
            return Collection('pending', reason='Stitch didn’t answer; waiting for its confirmation.')
        except ValidationError as error:
            return Collection('failed', reason=' '.join(error.messages)[:300])
        transaction = data.get('initiateTransaction') or {}
        state = (transaction.get('state') or {}).get('__typename')
        if state == 'TransactionSuccess':
            return Collection('paid', transaction.get('id', ''))
        if state == 'TransactionFailure':
            return Collection('failed', transaction.get('id', ''), _reason(transaction['state'].get('reason'), 'The card was declined.'))
        return Collection('pending', transaction.get('id', ''))

    # Once-off payments: a payment request

    def start_checkout(self, payment):
        reference = payment_reference(payment)
        data = self.graphql('''mutation Pay($input: ClientPaymentInitiationRequestCreateInput!) {
              clientPaymentInitiationRequestCreate(input: $input) { paymentInitiationRequest { id url } } }''',
                            {'input': {'amount': _money(payment.amount), 'payerReference': reference[:12],
                                       'beneficiaryReference': reference[:20], 'externalReference': reference,
                                       'paymentMethods': {'card': {'enabled': True}, 'eft': {'enabled': True}}}})
        request = (data.get('clientPaymentInitiationRequestCreate') or {}).get('paymentInitiationRequest') or {}
        if not request.get('id') or not request.get('url'):
            raise ValidationError('Stitch didn’t return a page to pay on.')
        payment.provider_reference = request['id']
        payment.save(update_fields=('provider_reference', 'updated_at'))
        return _with_param(request['url'], 'redirect_uri', return_url('payment', payment))

    def finish_checkout(self, payment, params):
        try:
            data = self.graphql('''query Payment($id: ID!) { node(id: $id) { ... on PaymentInitiationRequest {
                  id state { __typename } } } }''', {'id': payment.provider_reference})
        except ProviderUnavailable:
            return Collection('pending')
        node = data.get('node') or {}
        state = (node.get('state') or {}).get('__typename')
        if state in ('PaymentInitiationRequestCompleted', 'PaymentInitiationRequestManuallyCompleted'):
            return Collection('paid', node.get('id', ''))
        if state in ('PaymentInitiationRequestCancelled', 'PaymentInitiationRequestExpired'):
            return Collection('failed', node.get('id', ''), 'The payment wasn’t completed.')
        return Collection('pending', node.get('id', ''))

    # Webhooks (Svix-signed)

    def read_webhook(self, request):
        verify_signature(request.body, request.headers, webhook_secret(self.name))
        try:
            event = json.loads(request.body)
        except ValueError:
            raise ValidationError('The webhook isn’t JSON.') from None
        kind, data = event.get('type'), event.get('data') or {}
        if kind == 'payment-consent-request':
            mandate_id = record_id(data.get('externalReference'), 'MH-D')
            state = WEBHOOK_CONSENT_STATES.get(str(data.get('status', '')).upper())
            if mandate_id is None or state is None:
                return []
            reason = _reason(data.get('statusReason') or (data.get('consentDetails') or {}).get('bankReasonCode'), 'Not approved.')
            return [Notice(Approval(state, reference=data.get('id', ''), reason=reason if state != 'approved' else ''),
                           mandate_id=mandate_id)]
        if kind == 'transaction':
            payment_id = record_id(data.get('externalReference'), 'MHP')
            status = str(data.get('status', '')).upper()
            if payment_id is None or status not in ('SUCCESS', 'FAILURE', 'CANCELLED'):
                return []
            if status == 'SUCCESS':
                return [Notice(Collection('paid', data.get('id', '')), payment_id=payment_id)]
            return [Notice(Collection('failed', data.get('id', ''), _reason(data.get('statusReason'), 'The card was declined.')),
                           payment_id=payment_id)]
        if kind == 'payment':
            node = (((data.get('client') or {}).get('paymentInitiationRequests') or {}).get('node')) or {}
            payment_id = record_id(node.get('externalReference'), 'MHP')
            state = (node.get('state') or {}).get('__typename')
            if payment_id is None:
                return []
            if state == 'PaymentInitiationRequestCompleted':
                return [Notice(Collection('paid', node.get('id', '')), payment_id=payment_id)]
            if state in ('PaymentInitiationRequestCancelled', 'PaymentInitiationRequestExpired'):
                return [Notice(Collection('failed', node.get('id', ''), 'The payment wasn’t completed.'), payment_id=payment_id)]
        return []


def verify_signature(body, headers, secret, *, now=None):
    """Svix's signature on a Stitch webhook: base64 HMAC-SHA256, with the secret after "whsec_", of
    "{svix-id}.{svix-timestamp}.{body}", one of the "v1,<signature>" entries in svix-signature, sent in the last five
    minutes."""
    if not secret:
        raise ValidationError('No webhook secret is set.')
    message_id, stamp, signatures = (headers.get('svix-id', ''), headers.get('svix-timestamp', ''),
                                     headers.get('svix-signature', ''))
    try:
        sent = int(stamp)
    except ValueError:
        raise ValidationError('The webhook has no timestamp.') from None
    if abs((now or time.time()) - sent) > WEBHOOK_TOLERANCE_SECONDS:
        raise ValidationError('The webhook is too old, or from the future.')
    try:
        key = base64.b64decode(secret.removeprefix('whsec_'))
    except ValueError:
        raise ValidationError('The webhook secret isn’t a Stitch signing secret.') from None
    payload = body if isinstance(body, bytes) else body.encode()
    expected = base64.b64encode(hmac.new(key, f'{message_id}.{stamp}.'.encode() + payload, hashlib.sha256).digest()).decode()
    if not any(hmac.compare_digest(expected, entry.split(',', 1)[1]) for entry in signatures.split() if entry.startswith('v1,')):
        raise ValidationError('The webhook signature doesn’t match.')


# DebiCheck collection files

def debicheck_in_use(company):
    """Whether the practice has DebiCheck collection files to exchange with Stitch: Stitch takes new debit orders, or
    still holds some from before a move to another provider."""
    from .models import DebitMandate

    return settings.PAYMENT_PROVIDER == StitchProvider.name or DebitMandate.objects.filter(
        company=company, provider=StitchProvider.name, method=DebitMandate.Method.DEBICHECK).exclude(
        status__in=(DebitMandate.Status.CANCELLED, DebitMandate.Status.FAILED)).exists() or debicheck_waiting(company).exists()


def debicheck_waiting(company):
    """DebiCheck debits waiting to go in a collection file."""
    from .models import Payment

    return Payment.objects.filter(company=company, provider=StitchProvider.name, method=Payment.Method.DEBICHECK,
                                  status=Payment.Status.PENDING, checkout=False, provider_reference='')


def collection_file(company, *, actor=None, today=None):
    """The next DebiCheck collection file (Stitch's CSV) with every debit waiting, each on its collection date, and
    the debits marked as sent. Returns (file name, CSV text, how many), or None when nothing is waiting."""
    from .models import SubscriptionCycle
    from .services import record_audit

    today = today or timezone.localdate()
    payments = list(debicheck_waiting(company).select_related('subscription_cycle__mandate').order_by('pk'))
    if not payments:
        return None
    from .models import AuditEvent

    index = AuditEvent.objects.filter(company=company, action='billing.debicheck_file').count()
    now = timezone.now()
    batch = f'MHB{now:%Y%m%d%H%M%S}{index}'
    rows, total = [], Decimal('0')
    for payment in payments:
        mandate = payment.subscription_cycle.mandate if payment.subscription_cycle_id else None
        if mandate is None or not mandate.provider_reference:
            continue
        day = collection_date(mandate, payment.due_on, today)
        rows.append(['D', payment_reference(payment), mandate.provider_reference, mandate.reference,
                     f'{Decimal(payment.amount):.2f}', payment_reference(payment), day.isoformat(), ''])
        total += Decimal(payment.amount)
        payment.provider_reference, payment.due_on = f'batch:{batch}', day
        payment.save(update_fields=('provider_reference', 'due_on', 'updated_at'))
        SubscriptionCycle.objects.filter(pk=payment.subscription_cycle_id).update(
            failure_reason=f'In the DebiCheck collection for {day:%-d %B}.', updated_at=now)
    if not rows:
        return None
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['RECORD_TYPE', 'CLIENT_ID', 'PRODUCT', 'CHANNEL', 'FILE_TYPE'])
    writer.writerow(['P', settings.STITCH_FILES_CLIENT_ID, 'COLLECTIONS', 'DEBICHECK', 'COLLECTION'])
    writer.writerow(['RECORD_TYPE', 'EXTERNAL_BATCH_REFERENCE', 'SUBMISSION_DATETIME'])
    writer.writerow(['H', batch, now.strftime('%Y-%m-%dT%H:%M:%SZ')])
    writer.writerow(['RECORD_TYPE', 'NONCE', 'CONSENT_ID', 'CONTRACT_REFERENCE', 'VALUE', 'EXTERNAL_COLLECTION_REFERENCE',
                     'COLLECTION_DATE', 'TRACKING_PERIOD'])
    writer.writerows(rows)
    writer.writerow(['RECORD_TYPE', 'TOTAL_RECORDS', 'TOTAL_VALUE', 'TOTAL_TRACKING_RECORDS', 'TOTAL_TRACKING_VALUE'])
    writer.writerow(['T', len(rows), f'{total:.2f}', 0, '0.00'])
    record_audit(company=company, actor=actor, action='billing.debicheck_file', metadata={'batch': batch, 'collections': len(rows),
                                                                                          'total': f'{total:.2f}'})
    return f'{now:%Y%m%d%H%M%S%f}_{index}.csv', out.getvalue(), len(rows)


def apply_results_file(company, text, *, actor=None):
    """Apply a REPLY or OUTPUT file from Stitch: each collection it names is paid, failed or still pending. Returns
    {'paid', 'failed', 'pending', 'ignored'} counts."""
    from .billing import settle_payment
    from .models import Payment
    from .services import record_audit

    counts = {'paid': 0, 'failed': 0, 'pending': 0, 'ignored': 0}
    file_type, titles = '', []
    for row in csv.reader(io.StringIO(text.lstrip('﻿'))):
        if not row:
            continue
        if row[0] == 'RECORD_TYPE':
            titles = row
            continue
        record = dict(zip(titles, row))
        if row[0] == 'P':
            file_type = record.get('FILE_TYPE', '')
            continue
        if row[0] != 'D' or file_type not in ('REPLY', 'OUTPUT'):
            continue
        payment_id = record_id(record.get('EXTERNAL_COLLECTION_REFERENCE'), 'MHP')
        payment = Payment.objects.filter(pk=payment_id, company=company, provider=StitchProvider.name).first() if payment_id else None
        if payment is not None and record.get('COLLECTION_STATUS') == 'DISPUTED' and payment.status == Payment.Status.PAID:
            counts['failed'] += dispute(payment, actor=actor)
            continue
        if payment is None or payment.status != Payment.Status.PENDING:
            counts['ignored'] += 1
            continue
        if file_type == 'REPLY':
            if record.get('STATUS') != 'FAILED':
                counts['pending'] += 1
                continue
            outcome = Collection('failed', reason=f"Stitch didn’t accept the collection: {record.get('REASON_DESCRIPTION') or record.get('STATUS_REASON')}")
        else:
            status = record.get('COLLECTION_STATUS', '')
            if status == 'SUCCESS':
                outcome = Collection('paid', record.get('COLLECTION_ID', ''))
            elif status == 'FAILED':
                outcome = Collection('failed', record.get('COLLECTION_ID', ''), _reason(record.get('COLLECTION_REASON'), 'The debit order was returned.'))
            else:
                counts['pending'] += 1
                continue
        settle_payment(payment=payment, collection=outcome, actor=actor)
        counts[outcome.status] += 1
    if not file_type:
        raise ValidationError('This isn’t a Stitch DebiCheck REPLY or OUTPUT file.')
    record_audit(company=company, actor=actor, action='billing.debicheck_results', metadata={'file_type': file_type, **counts})
    return counts


def dispute(payment, *, actor=None):
    """The patient disputed a DebiCheck collection and their bank refunded it: the payment is marked refunded, its
    period unpaid again (in the failure queue), and the practice gets a task. Returns 1 when something changed."""
    from django.db import transaction

    from .billing import _failure_task
    from .models import Invoice, Payment, SubscriptionCycle
    from .services import record_audit

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != Payment.Status.PAID:
            return 0
        reason = 'Disputed by the patient: their bank refunded the debit.'
        payment.status, payment.failure_reason = Payment.Status.REFUNDED, reason
        payment.save(update_fields=('status', 'failure_reason', 'updated_at'))
        cycle = SubscriptionCycle.objects.filter(pk=payment.subscription_cycle_id).first()
        if cycle is not None:
            cycle.status, cycle.paid_at, cycle.failure_reason = SubscriptionCycle.Status.FAILED, None, reason
            cycle.save(update_fields=('status', 'paid_at', 'failure_reason', 'updated_at'))
            Invoice.objects.filter(subscription_cycle=cycle).update(status=Invoice.Status.ISSUED, updated_at=timezone.now())
            _failure_task(cycle, reason)
        record_audit(company=payment.company, actor=actor, patient=payment.patient, action='billing.debit_disputed', target=payment)
    return 1
