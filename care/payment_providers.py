"""Who takes the money: the payment provider in settings.PAYMENT_PROVIDER, behind one small interface so care.billing
doesn't change with the provider. PAYMENT_PROVIDER takes new debit orders and payments; one already set up stays with
the provider that holds it (its card or mandate is there), so the practice can move from Peach to Stitch, or back,
without asking every patient again, as long as the old provider's settings stay in place.

- "sandbox": for local testing. A debit order is approved, and a card paid, on a page inside the app; debits succeed
  unless the patient chose "approve, but fail the debits" there. No money moves.
- "peach": Peach Payments (care.peach). Cards only: Peach doesn't offer DebiCheck. The card is kept by Peach and
  charged each period; once-off payments are paid by card on Peach's form.
- "stitch": Stitch (care.stitch). DebiCheck debit orders, approved in the patient's banking app and collected with
  daily collection files, or a card kept by Stitch; once-off payments on Stitch's page (card or bank).
- "" (empty): no provider. Nothing can be collected, and staff record payments received some other way by hand.

A hosted provider (Peach, Stitch) sends the patient to its own page, then back to the app's return page, which asks the
provider how it went; its webhooks say the same later. Card and bank details never reach the app: only a label such as
"Visa ···4242" is kept.

The interface, for a hosted provider: methods (the DebitMandate methods it takes), start_mandate, finish_mandate,
collect, start_checkout, finish_checkout, cancel_mandate and read_webhook.
"""

import json
import logging
import secrets
from dataclasses import dataclass
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.urls import reverse

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 30
# The providers with their own pages and webhooks.
HOSTED = ('peach', 'stitch')


class ProviderUnavailable(Exception):
    """The provider couldn't be reached, or answered with something that isn't an answer. For a debit, what happened
    isn't known: it is left pending, for the provider's webhook to settle."""


def http_call(method, url, *, headers=None, form=None, body=None):
    """(HTTP status, parsed JSON) from a provider. A 4xx with a JSON body is returned, not raised: providers answer a
    declined card that way. Anything else that goes wrong raises ProviderUnavailable."""
    data = None
    headers = {'Accept': 'application/json', **(headers or {})}
    if form is not None:
        data = urlencode(form).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urlrequest.Request(url, data=data, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode() or '{}')
    except urlerror.HTTPError as error:
        text = error.read().decode(errors='replace')
        try:
            return error.code, json.loads(text)
        except ValueError:
            raise ProviderUnavailable(f'answered {error.code}: {text[:200] or error.reason}') from None
    except (urlerror.URLError, TimeoutError, ValueError) as error:
        raise ProviderUnavailable(str(error)) from None


@dataclass(frozen=True)
class Collection:
    """What a provider said about one collection: paid, failed (with the reason) or pending (it will tell us)."""

    status: str
    reference: str = ''
    reason: str = ''


@dataclass(frozen=True)
class Approval:
    """What a provider said about a debit order sent for approval: approved (with what it collects with, and a label
    such as "Visa ···4242"), declined (with the reason) or pending (the bank is still asking the patient)."""

    status: str
    reference: str = ''
    account_label: str = ''
    reason: str = ''


@dataclass(frozen=True)
class Notice:
    """A verified webhook: the debit order or payment it is about (by its id here) and what happened."""

    outcome: object
    mandate_id: int | None = None
    payment_id: int | None = None


def return_url(kind, record):
    """Where the provider's page sends the patient back: kind is 'mandate' or 'payment'."""
    return f"{settings.SITE_URL}{reverse('portal:patient-payment-return', args=[kind, record.pk])}"


def webhook_url(name):
    return f"{settings.SITE_URL}{reverse('portal:payment-webhook', args=[name])}"


def payment_reference(payment):
    """The reference a payment is sent to the provider with, and read back from its webhooks: letters and digits
    only, as some acquiring banks require."""
    return f'MHP{payment.pk:07d}'


def mandate_reference(mandate):
    return f'MHD{mandate.pk:07d}'


def record_id(reference, prefix):
    """The id in one of our references ('MHP0000012' with prefix 'MHP' is 12), or None."""
    reference = str(reference or '')
    digits = reference[len(prefix):] if reference.startswith(prefix) else ''
    return int(digits) if digits.isdigit() else None


class SandboxProvider:
    name = 'sandbox'
    label = 'Sandbox (no money moves)'
    methods = ('debicheck', 'card')
    # The sandbox's approval and payment pages are inside the app, so it has no return page or webhooks.
    hosted = False
    # A debit order approved with "fail the debits" carries this, so its collections fail.
    DECLINE_PREFIX = 'sandbox-decline-'

    def start_mandate(self, mandate):
        """Where the patient goes to approve the debit order: here, the sandbox approval page."""
        mandate.provider_reference = f'sandbox-{secrets.token_hex(6)}'
        return reverse('portal:patient-billing-sandbox-mandate', args=[mandate.pk])

    def collect(self, payment, mandate):
        if mandate.provider_reference.startswith(self.DECLINE_PREFIX):
            return Collection('failed', f'sandbox-{payment.pk}', 'Insufficient funds (sandbox).')
        return Collection('paid', f'sandbox-{payment.pk}')

    def start_checkout(self, payment):
        return reverse('portal:patient-billing-sandbox-checkout', args=[payment.pk])

    def cancel_mandate(self, mandate):
        return None


def current_provider():
    """The provider new debit orders and payments go to, or None when there isn't one."""
    name = settings.PAYMENT_PROVIDER
    if name and name not in HOSTED and name != SandboxProvider.name:
        raise ImproperlyConfigured(f'Unknown PAYMENT_PROVIDER "{name}". Use sandbox, peach, stitch or leave it empty.')
    return provider_named(name)


def provider_named(name):
    """The provider a debit order or payment was made with (its provider field), or None. The sandbox only while it is
    the current provider, so a sandbox debit order is never "collected" once real money is."""
    if name == SandboxProvider.name:
        return SandboxProvider() if settings.PAYMENT_PROVIDER == name else None
    if name == 'peach':
        from .peach import PeachProvider

        return PeachProvider()
    if name == 'stitch':
        from .stitch import StitchProvider

        return StitchProvider()
    return None


def webhook_secret(name):
    """A provider's webhook secret: its own setting, or PAYMENT_WEBHOOK_SECRET when it is the current provider."""
    own = getattr(settings, f'{name.upper()}_WEBHOOK_SECRET', '')
    return own or (settings.PAYMENT_WEBHOOK_SECRET if settings.PAYMENT_PROVIDER == name else '')


def require_provider():
    provider = current_provider()
    if provider is None:
        raise ValidationError('No payment provider is connected yet, so payments can’t be taken in the app. '
                              'The practice can record a payment it received another way.')
    return provider
