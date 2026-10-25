"""Peach Payments (care.payment_providers): cards only, through Peach's card API (COPYandPAY and server-to-server).

- Keeping a card (a card "debit order"): the patient enters their card in Peach's form on the app's card page
  (COPYandPAY standalone tokenisation; card details go straight to Peach). Peach keeps it and returns a registration
  id, which is all the app keeps, with a label such as "Visa ···4242".
- Each period: the app charges the kept card (POST /v1/registrations/{id}/payments). The first charge on a card is
  marked as the customer's (INITIAL, CIT); later ones are the practice's (REPEATED, MIT) on Peach's recurring entity.
  Peach answers at once: paid, failed with its reason, or pending.
- Once-off payments (a non-prescription request, or a failed debit paid now): a payment checkout in the same form.
- Webhooks: Peach encrypts them (AES-256-GCM) with the webhook secret; PAYMENT ones settle a payment by its
  merchantTransactionId.

Peach doesn't offer DebiCheck, so a patient can only choose a card. Peach's documentation, and the public sandbox
credentials it publishes, were used to write and check this (developer.peachpayments.com, October 2026). Peach steers
new merchants to its newer Orchestration API: if the practice's account is set up there instead, this adapter's calls
change (the flow doesn't).
"""

import json
import re
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse

from .payment_providers import (
    Approval, Collection, Notice, ProviderUnavailable, http_call, payment_reference, record_id, return_url, webhook_secret,
)

# Peach's result codes (developer.peachpayments.com/docs/dashboard-response-codes).
SUCCESS = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36]|000\.400\.[1][12]0)')
SUCCESS_FOR_REVIEW = re.compile(r'^(000\.400\.0[^3]|000\.400\.100)')
PENDING = re.compile(r'^(000\.200|800\.400\.5|100\.400\.500)')
# The issuer says not to try again (for example 700.601.021, recurring payment cancelled by the cardholder).
DO_NOT_RETRY = re.compile(r'^(700\.600|700\.601)')
CHECKOUT_PREFIX = 'checkout:'
BRANDS = 'VISA MASTER AMEX'
CURRENCY = 'ZAR'


def outcome(code):
    """'paid', 'pending' or 'failed' for a Peach result code."""
    code = str(code or '')
    if SUCCESS.match(code) or SUCCESS_FOR_REVIEW.match(code):
        return 'paid'
    if PENDING.match(code):
        return 'pending'
    return 'failed'


def _reason(response):
    result = response.get('result') or {}
    description = str(result.get('description') or 'The card payment was declined.')
    if DO_NOT_RETRY.match(str(result.get('code') or '')):
        description += ' The bank says not to try this card again: ask the patient for another.'
    return description[:300]


def _label(response):
    brand = {'MASTER': 'Mastercard', 'VISA': 'Visa', 'AMEX': 'Amex'}.get(response.get('paymentBrand'), response.get('paymentBrand') or 'Card')
    last4 = (response.get('card') or {}).get('last4Digits')
    return f'{brand} ···{last4}' if last4 else brand


class PeachProvider:
    name = 'peach'
    label = 'Peach Payments'
    hosted = True
    methods = ('card',)

    def _configured(self):
        if not (settings.PEACH_ENTITY_ID and settings.PEACH_ACCESS_TOKEN):
            raise ValidationError('Peach Payments isn’t set up: add PEACH_ENTITY_ID and PEACH_ACCESS_TOKEN to the app’s settings.')

    def _call(self, method, path, data=None, *, recurring=False):
        """Peach's answer as JSON. A decline is an answer (HTTP 400 with its result code), not an error."""
        self._configured()
        entity = settings.PEACH_RECURRING_ENTITY_ID if recurring else settings.PEACH_ENTITY_ID
        token = settings.PEACH_RECURRING_ACCESS_TOKEN if recurring else settings.PEACH_ACCESS_TOKEN
        headers = {'Authorization': f'Bearer {token}'}
        url = f'{settings.PEACH_API_URL}{path}'
        if method in ('GET', 'DELETE'):
            url += f'?{urlencode({"entityId": entity, **(data or {})})}'
            _, response = http_call(method, url, headers=headers)
        else:
            _, response = http_call(method, url, headers=headers, form={'entityId': entity, **(data or {})})
        if not isinstance(response, dict) or 'result' not in response:
            raise ProviderUnavailable('Peach Payments sent an answer without a result.')
        return response

    # The card page: Peach's form, on the app's page

    def card_form(self, kind, record):
        """Prepare Peach's card form for a debit order (keep the card) or a payment (pay once), and say how the app's
        card page shows it. The checkout is kept on the record until the patient comes back."""
        if kind == 'mandate':
            # Peach's integrity hash is for the payment form's script, not the card-only (/registration) one, so a
            # browser would refuse that script with it: the card-only form goes without.
            data = {'createRegistration': 'true'}
        else:
            data = {'integrity': 'true', 'amount': f'{Decimal(record.amount):.2f}', 'currency': CURRENCY, 'paymentType': 'DB',
                    'merchantTransactionId': payment_reference(record)}
        try:
            response = self._call('POST', '/v1/checkouts', data)
        except ProviderUnavailable as error:
            raise ValidationError(f'Peach Payments couldn’t be reached. Try again in a minute. ({error})') from None
        checkout = response.get('id')
        if not checkout or outcome(response['result'].get('code')) == 'failed':
            raise ValidationError(f'Peach Payments couldn’t open the card form: {_reason(response)}')
        record.provider_reference = f'{CHECKOUT_PREFIX}{checkout}'
        record.save(update_fields=('provider_reference', 'updated_at'))
        return {
            'script': f'{settings.PEACH_API_URL}/v1/paymentWidgets.js?checkoutId={checkout}'
                      + ('/registration' if kind == 'mandate' else ''),
            'integrity': response.get('integrity', ''),
            'action': return_url(kind, record),
            'brands': BRANDS,
        }

    def _checkout(self, record):
        reference = record.provider_reference
        if not reference.startswith(CHECKOUT_PREFIX):
            raise ValidationError('This card form has expired. Start again.')
        return reference.removeprefix(CHECKOUT_PREFIX)

    # Debit orders (a kept card)

    def start_mandate(self, mandate):
        if mandate.method != 'card':
            raise ValidationError('Peach Payments takes cards only. Choose a card.')
        return reverse('portal:patient-card-form', args=['mandate', mandate.pk])

    def finish_mandate(self, mandate, params):
        try:
            response = self._call('GET', f'/v1/checkouts/{self._checkout(mandate)}/registration')
        except ProviderUnavailable:
            return Approval('pending')
        state = outcome(response['result'].get('code'))
        if state == 'paid' and response.get('id'):
            return Approval('approved', reference=response['id'], account_label=_label(response))
        if state == 'pending':
            return Approval('pending')
        return Approval('declined', reason=_reason(response))

    def collect(self, payment, mandate):
        """Charge the kept card. Its first charge is the patient's own (they have just entered the card); every later
        one is the practice's, on Peach's recurring entity."""
        from .models import Payment

        first = not Payment.objects.filter(provider=self.name, subscription_cycle__mandate_id=mandate.pk, checkout=False,
                                           status=Payment.Status.PAID).exists()
        data = {
            'amount': f'{Decimal(payment.amount):.2f}', 'currency': CURRENCY, 'paymentType': 'DB',
            'merchantTransactionId': payment_reference(payment),
            'standingInstruction.type': 'RECURRING', 'standingInstruction.recurringType': 'STANDING_ORDER',
            'standingInstruction.mode': 'INITIAL' if first else 'REPEATED',
            'standingInstruction.source': 'CIT' if first else 'MIT',
        }
        try:
            response = self._call('POST', f'/v1/registrations/{mandate.provider_reference}/payments', data, recurring=not first)
        except ProviderUnavailable:
            # Not known whether the card was charged: Peach's webhook settles it.
            return Collection('pending', reason='Peach Payments didn’t answer; waiting for its confirmation.')
        state = outcome(response['result'].get('code'))
        return Collection(state, response.get('id', ''), '' if state != 'failed' else _reason(response))

    def check_payment(self, payment):
        """What happened to a charge Peach didn't answer at the time (it timed out): Peach's transaction search by
        the payment's reference, on both entities. Nothing found means it never reached Peach, so it wasn't taken."""
        records = []
        for recurring in (True, False):
            response = self._call('GET', '/v3/query', {'merchantTransactionId': payment_reference(payment)}, recurring=recurring)
            records += [record for record in response.get('records') or [] if record.get('paymentType', 'DB') == 'DB']
            if settings.PEACH_RECURRING_ENTITY_ID == settings.PEACH_ENTITY_ID:
                break
        states = [(outcome((record.get('result') or {}).get('code')), record) for record in records]
        for wanted in ('paid', 'pending'):
            match = next((record for state, record in states if state == wanted), None)
            if match is not None:
                return Collection(wanted, match.get('id', ''))
        if states:
            return Collection('failed', states[-1][1].get('id', ''), _reason(states[-1][1]))
        return Collection('failed', reason='Peach has no record of this charge, so it wasn’t taken.')

    def cancel_mandate(self, mandate):
        """Ask Peach to forget the card."""
        if mandate.provider_reference and not mandate.provider_reference.startswith(CHECKOUT_PREFIX):
            try:
                self._call('DELETE', f'/v1/registrations/{mandate.provider_reference}')
            except (ProviderUnavailable, ValidationError):
                pass  # The debit order is cancelled here either way; the card can't be charged without it.

    # Once-off card payments

    def start_checkout(self, payment):
        return reverse('portal:patient-card-form', args=['payment', payment.pk])

    def finish_checkout(self, payment, params):
        try:
            response = self._call('GET', f'/v1/checkouts/{self._checkout(payment)}/payment')
        except ProviderUnavailable:
            return Collection('pending')
        state = outcome(response['result'].get('code'))
        return Collection(state, response.get('id', ''), '' if state != 'failed' else _reason(response))

    # Webhooks

    def read_webhook(self, request):
        """Decrypt a webhook (AES-256-GCM with the webhook secret; the IV and tag in headers, the body in hex, maybe
        wrapped as {"encryptedBody": ...}). A PAYMENT one settles the payment it names; others are ignored."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        secret = webhook_secret(self.name)
        if not secret:
            raise ValidationError('No webhook secret is set.')
        body = request.body.decode(errors='replace').strip()
        try:
            if body.startswith('{'):
                body = json.loads(body)['encryptedBody']
            key, iv = bytes.fromhex(secret), bytes.fromhex(request.headers.get('X-Initialization-Vector', ''))
            tag = bytes.fromhex(request.headers.get('X-Authentication-Tag', ''))
            event = json.loads(AESGCM(key).decrypt(iv, bytes.fromhex(body) + tag, None))
        except (ValueError, KeyError, TypeError, InvalidTag):
            raise ValidationError('The webhook couldn’t be decrypted.') from None
        if not isinstance(event, dict) or event.get('type') != 'PAYMENT':
            return []
        payload = event.get('payload') or {}
        payment_id = record_id(payload.get('merchantTransactionId'), 'MHP')
        if payment_id is None:
            return []
        state = outcome((payload.get('result') or {}).get('code'))
        return [Notice(Collection(state, str(payload.get('id', '')), '' if state != 'failed' else _reason(payload)),
                       payment_id=payment_id)]
