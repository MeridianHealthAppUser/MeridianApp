"""The payment providers' adapters: Peach Payments (cards) and Stitch (DebiCheck, cards, once-off payments), with each
provider's HTTP calls stood in for. (care/peach.py was also run against Peach's public sandbox.)"""

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings
from django.utils import timezone

from . import stitch
from .billing import apply_notice, collect_cycle, run_debits, start_debit_order, start_order_payment
from .models import AuditEvent, ClinicalTask, DebitMandate, Payment, PharmacyOrder, SubscriptionCycle
from .payment_providers import ProviderUnavailable, current_provider
from .peach import PeachProvider, outcome
from .test_billing import BillingFixture

PEACH = dict(PAYMENT_PROVIDER='peach', PEACH_API_URL='https://peach.test', PEACH_ENTITY_ID='entity-3ds',
             PEACH_ACCESS_TOKEN='token-3ds', PEACH_RECURRING_ENTITY_ID='entity-recurring',
             PEACH_RECURRING_ACCESS_TOKEN='token-recurring', SITE_URL='https://meridian.test',
             PAYMENT_WEBHOOK_SECRET='00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff')
STITCH_SECRET = 'whsec_' + base64.b64encode(b'stitch-test-signing-key').decode()
STITCH = dict(PAYMENT_PROVIDER='stitch', STITCH_CLIENT_ID='test-client', STITCH_CLIENT_SECRET='secret',
              STITCH_FILES_CLIENT_ID='test-client', SITE_URL='https://meridian.test', PAYMENT_WEBHOOK_SECRET=STITCH_SECRET)


class FakePeach:
    """Stands in for Peach's card API: records each call and answers from a script."""

    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, method, url, *, headers=None, form=None, body=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'form': form or {}})
        for fragment, answer in self.answers.items():
            if fragment.replace('_', '/') in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return 200, {'result': {'code': '000.100.110', 'description': 'Request successfully processed'}, 'id': 'peach-payment'}


@override_settings(**PEACH)
class PeachTests(BillingFixture):
    def approve_card(self):
        mandate, url = start_debit_order(subscription=self.plan, actor=self.patient_user, method='card')
        self.assertEqual(url, f'/patient/billing/card/mandate/{mandate.pk}/')
        mandate.provider_reference = 'checkout:CHK1'
        mandate.save()
        registration = (200, {'id': 'reg-1', 'paymentBrand': 'VISA', 'card': {'last4Digits': '4242'},
                              'result': {'code': '000.100.110', 'description': 'ok'}})
        fake = FakePeach(checkouts_CHK1_registration=registration)
        with patch('care.peach.http_call', fake):
            approval = PeachProvider().finish_mandate(mandate, {})
            from .billing import settle_mandate

            settle_mandate(mandate=mandate, approval=approval, actor=self.patient_user)
        mandate.refresh_from_db()
        return mandate, fake

    def test_result_codes(self):
        for code, state in (('000.000.000', 'paid'), ('000.100.110', 'paid'), ('000.400.110', 'paid'), ('000.200.100', 'pending'),
                            ('800.400.500', 'pending'), ('800.100.152', 'failed'), ('100.396.101', 'failed'), ('', 'failed')):
            self.assertEqual(outcome(code), state, code)

    def test_peach_takes_cards_only(self):
        with self.assertRaisesMessage(ValidationError, 'card only'):
            start_debit_order(subscription=self.plan, actor=self.patient_user, method='debicheck')

    def test_the_card_form_keeps_a_card_or_pays_once_with_the_payment_reference(self):
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user, method='card')
        fake = FakePeach(checkouts=(200, {'id': 'CHK9', 'integrity': 'sha384-abc', 'result': {'code': '000.200.100'}}))
        with patch('care.peach.http_call', fake):
            form = PeachProvider().card_form('mandate', mandate)
        self.assertEqual(form['script'], 'https://peach.test/v1/paymentWidgets.js?checkoutId=CHK9/registration')
        self.assertEqual((form['integrity'], form['action']),
                         ('sha384-abc', f'https://meridian.test/patient/billing/return/mandate/{mandate.pk}/'))
        self.assertEqual(fake.calls[0]['form'], {'entityId': 'entity-3ds', 'createRegistration': 'true'})
        self.assertEqual(fake.calls[0]['headers']['Authorization'], 'Bearer token-3ds')
        mandate.refresh_from_db()
        self.assertEqual(mandate.provider_reference, 'checkout:CHK9')
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, subtotal=Decimal('250.00'),
                                             status=PharmacyOrder.Status.SUBMITTED)
        payment, url = start_order_payment(order=order, actor=self.patient_user)
        self.assertEqual((url, payment.checkout), (f'/patient/billing/card/payment/{payment.pk}/', True))
        with patch('care.peach.http_call', fake):
            form = PeachProvider().card_form('payment', payment)
        self.assertEqual(form['script'], 'https://peach.test/v1/paymentWidgets.js?checkoutId=CHK9')
        self.assertEqual({key: fake.calls[1]['form'][key] for key in ('amount', 'currency', 'paymentType', 'merchantTransactionId')},
                         {'amount': '250.00', 'currency': 'ZAR', 'paymentType': 'DB', 'merchantTransactionId': f'MHP{payment.pk:07d}'})

    def test_an_approved_card_is_charged_as_the_patients_first_then_by_the_practice(self):
        mandate, fake = self.approve_card()
        self.assertEqual((mandate.status, mandate.provider_reference, mandate.account_label), ('active', 'reg-1', 'Visa ···4242'))
        first = fake.calls[-1]
        self.assertEqual(first['url'], 'https://peach.test/v1/registrations/reg-1/payments')
        self.assertEqual({key: first['form'][key] for key in ('entityId', 'amount', 'standingInstruction.mode', 'standingInstruction.source',
                                                               'standingInstruction.type', 'standingInstruction.recurringType')},
                         {'entityId': 'entity-3ds', 'amount': '3295.00', 'standingInstruction.mode': 'INITIAL',
                          'standingInstruction.source': 'CIT', 'standingInstruction.type': 'RECURRING',
                          'standingInstruction.recurringType': 'STANDING_ORDER'})
        self.assertEqual(SubscriptionCycle.objects.get().status, 'paid')
        with patch('care.peach.http_call', fake):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
        later = fake.calls[-1]
        self.assertEqual((later['form']['standingInstruction.mode'], later['form']['standingInstruction.source'],
                          later['form']['entityId'], later['headers']['Authorization']),
                         ('REPEATED', 'MIT', 'entity-recurring', 'Bearer token-recurring'))

    def test_a_declined_charge_fails_with_peachs_reason_and_is_retried(self):
        self.approve_card()
        decline = (400, {'id': 'peach-x', 'result': {'code': '800.100.155', 'description': 'transaction declined (amount exceeds credit)'}})
        with patch('care.peach.http_call', FakePeach(registrations=decline)):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
        cycle = SubscriptionCycle.objects.get(cycle_number=2)
        self.assertEqual((cycle.status, cycle.attempts, cycle.next_attempt_on), ('pending', 1, cycle.due_on + timedelta(days=3)))
        self.assertIn('amount exceeds credit', cycle.failure_reason)

    def test_an_unanswered_charge_waits_for_the_webhook_and_isnt_sent_twice(self):
        self.approve_card()
        unreachable = FakePeach(registrations=ProviderUnavailable('timed out'))
        with patch('care.peach.http_call', unreachable):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
            run_debits(self.company, today=self.today + timedelta(weeks=4, days=1))
        self.assertEqual(len([call for call in unreachable.calls if 'registrations' in call['url']]), 1)
        payment = Payment.objects.get(subscription_cycle__cycle_number=2)
        self.assertEqual(payment.status, 'pending')
        # Peach's webhook, encrypted as Peach sends it, settles it.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        event = json.dumps({'type': 'PAYMENT', 'payload': {'id': 'peach-77', 'merchantTransactionId': f'MHP{payment.pk:07d}',
                                                           'result': {'code': '000.000.000', 'description': 'ok'}}}).encode()
        iv = bytes(12)
        sealed = AESGCM(bytes.fromhex(PEACH['PAYMENT_WEBHOOK_SECRET'])).encrypt(iv, event, None)
        body, tag = sealed[:-16].hex(), sealed[-16:].hex()
        request = RequestFactory().post('/billing/webhooks/peach/', data=json.dumps({'encryptedBody': body}),
                                        content_type='application/json', HTTP_X_INITIALIZATION_VECTOR=iv.hex(),
                                        HTTP_X_AUTHENTICATION_TAG=tag)
        provider = current_provider()
        for notice in provider.read_webhook(request):
            apply_notice(provider, notice)
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.provider_reference), ('paid', 'peach-77'))
        self.assertEqual(SubscriptionCycle.objects.get(cycle_number=2).status, 'paid')
        # A webhook that wasn't encrypted with the secret is refused.
        forged = RequestFactory().post('/billing/webhooks/peach/', data=body, content_type='text/plain',
                                       HTTP_X_INITIALIZATION_VECTOR=iv.hex(), HTTP_X_AUTHENTICATION_TAG='00' * 16)
        with self.assertRaises(ValidationError):
            provider.read_webhook(forged)

    def test_a_charge_peach_never_answered_is_looked_up_the_next_day(self):
        self.approve_card()
        with patch('care.peach.http_call', FakePeach(registrations=ProviderUnavailable('timed out'))):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
        payment = Payment.objects.get(subscription_cycle__cycle_number=2)
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=2))
        lookup = FakePeach(v3_query=(200, {'result': {'code': '000.000.100'}, 'records': []}))
        with patch('care.peach.http_call', lookup):
            run_debits(self.company, today=self.today + timedelta(weeks=4, days=1))
        self.assertIn(f'merchantTransactionId=MHP{payment.pk:07d}', lookup.calls[0]['url'])
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.failure_reason), ('failed', 'Peach has no record of this charge, so it wasn’t taken.'))
        cycle = SubscriptionCycle.objects.get(cycle_number=2)
        self.assertEqual((cycle.attempts, cycle.next_attempt_on), (1, cycle.due_on + timedelta(days=3)))

    def test_cancelling_the_subscription_asks_peach_to_forget_the_card(self):
        from .treatment import change_subscription_status

        mandate, _ = self.approve_card()
        fake = FakePeach()
        with patch('care.peach.http_call', fake), self.captureOnCommitCallbacks(execute=True):
            change_subscription_status(subscription=self.plan, actor=self.patient_user, action='cancel', confirm=True, reasons=['cost'])
        self.assertEqual((fake.calls[0]['method'], fake.calls[0]['url']), ('DELETE', 'https://peach.test/v1/registrations/reg-1?entityId=entity-3ds'))


class MovingProviderTests(BillingFixture):
    """New debit orders go to PAYMENT_PROVIDER; one already set up stays with the provider that holds it."""

    def test_a_card_peach_keeps_is_still_charged_by_peach_after_a_move_to_stitch(self):
        with override_settings(**PEACH):
            mandate, _ = PeachTests.approve_card(self)
        moved = {**PEACH, **STITCH, 'PAYMENT_WEBHOOK_SECRET': '', 'PEACH_WEBHOOK_SECRET': PEACH['PAYMENT_WEBHOOK_SECRET']}
        with override_settings(**moved):
            self.assertEqual(current_provider().name, 'stitch')
            fake = FakePeach()
            with patch('care.peach.http_call', fake):
                run_debits(self.company, today=self.today + timedelta(weeks=4))
            self.assertEqual(fake.calls[-1]['url'], 'https://peach.test/v1/registrations/reg-1/payments')
            payment = Payment.objects.get(subscription_cycle__cycle_number=2)
            self.assertEqual((payment.provider, payment.status), ('peach', 'paid'))
            # Peach's webhooks still have their address, and its own secret.
            response = self.client.post('/billing/webhooks/peach/', data='00', content_type='text/plain',
                                        HTTP_X_INITIALIZATION_VECTOR='00' * 12, HTTP_X_AUTHENTICATION_TAG='00' * 16)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.client.post('/billing/webhooks/sandbox/').status_code, 404)
            # A new debit order goes to Stitch.
            stitch._token.update(value='', expires=0)
            consent = FakeStitch(CreateConsent={'paymentConsentRequestCreate': {'id': 'consent-2', 'url': 'https://secure.stitch.money/v2/consent'}})
            with patch('care.stitch.http_call', consent):
                renewed, _ = start_debit_order(subscription=self.plan, actor=self.patient_user, method='debicheck')
            self.assertEqual(renewed.provider, 'stitch')

    def test_a_sandbox_debit_order_isnt_collected_once_real_money_is(self):
        with override_settings(PAYMENT_PROVIDER='sandbox'):
            mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user, method='card')
            from .billing import approve_mandate

            approve_mandate(mandate=mandate, actor=self.patient_user)
        with override_settings(**PEACH), patch('care.peach.http_call', FakePeach()):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
        cycle = SubscriptionCycle.objects.get(cycle_number=2)
        self.assertFalse(Payment.objects.filter(subscription_cycle=cycle).exists())
        self.assertEqual(cycle.failure_reason, 'The payment provider that holds this debit order isn’t connected.')


def signed(body, *, secret=STITCH_SECRET, stamp=None):
    stamp = str(int(stamp or time.time()))
    key = base64.b64decode(secret.removeprefix('whsec_'))
    signature = base64.b64encode(hmac.new(key, f'msg_1.{stamp}.'.encode() + body, hashlib.sha256).digest()).decode()
    return {'svix-id': 'msg_1', 'svix-timestamp': stamp, 'svix-signature': f'v1,bad v1,{signature}'}


class FakeStitch:
    """Stands in for Stitch: the token endpoint and the GraphQL API, answering by the operation's name."""

    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, method, url, *, headers=None, form=None, body=None):
        if url == stitch.TOKEN_URL:
            return 200, {'access_token': 'stitch-token', 'expires_in': 3600}
        self.calls.append({'headers': headers, 'body': body})
        name = body['query'].split('(')[0].split()[-1]
        answer = self.answers.get(name, {})
        if isinstance(answer, Exception):
            raise answer
        return 200, {'data': answer}


@override_settings(**STITCH)
class StitchTests(BillingFixture):
    def setUp(self):
        super().setUp()
        stitch._token.update(value='', expires=0)
        self.patient.id_number = '8001015009087'
        self.patient.phone = '082 555 0114'
        self.patient.save()

    def approve(self, method='debicheck', label_details=None, **answers):
        fake = FakeStitch(CreateConsent={'paymentConsentRequestCreate': {'id': 'consent-1', 'url': 'https://secure.stitch.money/v2/consent?requestId=r1&clientId=test-client'}})
        with patch('care.stitch.http_call', fake):
            mandate, url = start_debit_order(subscription=self.plan, actor=self.patient_user, method=method)
        granted = FakeStitch(Consent={'node': {'id': 'consent-1', 'status': {'__typename': 'PaymentConsentGranted'}}},
                             ConsentDetails={'node': {'consentDetails': label_details or {'customer': {'accountNumber': '62001234567', 'bankId': 'fnb'}}}},
                             **answers)
        with patch('care.stitch.http_call', granted):
            from .billing import settle_mandate

            settle_mandate(mandate=mandate, approval=current_provider().finish_mandate(mandate, {}), actor=self.patient_user)
        mandate.refresh_from_db()
        return mandate, url, fake

    def test_a_debicheck_mandate_is_weekly_on_its_collection_day_up_to_the_prescriptions_highest_price(self):
        mandate, url, fake = self.approve()
        self.assertEqual(url, 'https://secure.stitch.money/v2/consent?requestId=r1&clientId=test-client&returnUrl='
                              f'https%3A%2F%2Fmeridian.test%2Fpatient%2Fbilling%2Freturn%2Fmandate%2F{mandate.pk}%2F')
        sent = fake.calls[0]['body']['variables']['input']
        self.assertEqual(fake.calls[0]['headers']['Authorization'], 'Bearer stitch-token')
        self.assertEqual(sent['externalReference'], mandate.reference)
        self.assertEqual(sent['payerInformation']['identifyingDocument'], {'identityDocument': {'number': '8001015009087', 'country': 'ZA'}})
        details = sent['paymentMethods']['debicheck']['details']
        expected_day = (self.today + timedelta(days=3)).isoweekday()
        self.assertEqual(details['contractReference'], mandate.reference)
        self.assertEqual(details['collection'], {'collectionFrequency': 'weekly', 'collectionDay': expected_day,
                                                 'instalmentAmount': {'quantity': '3295.00', 'currency': 'ZAR'},
                                                 'maximumCollectionAmount': {'quantity': '3295.00', 'currency': 'ZAR'}})
        self.assertEqual((mandate.status, mandate.collection_day, mandate.provider_reference, mandate.account_label),
                         ('active', expected_day, 'consent-1', 'FNB ···4567'))

    def test_only_a_south_african_id_number_is_sent_and_a_passport_is_left_for_stitchs_page(self):
        self.patient.id_number = 'A01234567'
        self.patient.save()
        fake = FakeStitch(CreateConsent={'paymentConsentRequestCreate': {'id': 'consent-2', 'url': 'https://secure.stitch.money/v2/consent?requestId=r2'}})
        with patch('care.stitch.http_call', fake):
            start_debit_order(subscription=self.plan, actor=self.patient_user, method='debicheck')
        payer = fake.calls[0]['body']['variables']['input']['payerInformation']
        self.assertNotIn('identifyingDocument', payer)
        self.assertEqual((payer['fullName'], payer['email'], payer['mobileNumber']),
                         ('Nadia Mokoena', 'billing-patient@example.test', '082 555 0114'))

    def test_debicheck_debits_go_out_in_a_collection_file_and_the_output_file_settles_them(self):
        mandate, _, _ = self.approve()
        payment = Payment.objects.get()
        self.assertEqual((payment.status, payment.method, payment.provider_reference), ('pending', 'debicheck', ''))
        name, text, count = stitch.collection_file(self.company, actor=self.admin, today=self.today)
        self.assertEqual(count, 1)
        self.assertTrue(name.endswith('_0.csv'))
        lines = text.splitlines()
        self.assertEqual(lines[0], 'RECORD_TYPE,CLIENT_ID,PRODUCT,CHANNEL,FILE_TYPE')
        self.assertEqual(lines[1], 'P,test-client,COLLECTIONS,DEBICHECK,COLLECTION')
        self.assertEqual(lines[4], 'RECORD_TYPE,NONCE,CONSENT_ID,CONTRACT_REFERENCE,VALUE,EXTERNAL_COLLECTION_REFERENCE,COLLECTION_DATE,TRACKING_PERIOD')
        # The first debit is as soon as a DebiCheck collection can be, and it is collected on its due date.
        day = self.today + timedelta(days=3)
        self.assertEqual(SubscriptionCycle.objects.get().due_on, day)
        reference = f'MHP{payment.pk:07d}'
        self.assertEqual(lines[5], f'D,{reference},consent-1,{mandate.reference},3295.00,{reference},{day.isoformat()},')
        self.assertEqual(lines[7], 'T,1,3295.00,0,0.00')
        # Sent once only, and the next run doesn't try it again while it's with the bank.
        self.assertIsNone(stitch.collection_file(self.company, today=self.today))
        run_debits(self.company, today=self.today + timedelta(days=1))
        self.assertEqual(Payment.objects.count(), 1)
        output = ('RECORD_TYPE,CLIENT_ID,PRODUCT,CHANNEL,FILE_TYPE\nP,test-client,COLLECTIONS,DEBICHECK,OUTPUT\n'
                  'RECORD_TYPE,EXTERNAL_BATCH_REFERENCE,EXTERNAL_COLLECTION_REFERENCE,CONSENT_ID,CONTRACT_REFERENCE,COLLECTION_ID,'
                  'COLLECTION_DATE,VALUE,COLLECTION_STATUS,COLLECTION_REASON,SETTLEMENT_STATUS,SETTLEMENT_REFERENCE,TYPE,\n'
                  f'D,B1,{reference},consent-1,{mandate.reference},COL1,{day},3295.00,SUCCESS,PROCESSED,PENDING,,DC,\n'
                  'D,B1,MHP9999999,consent-x,X,COL2,2026-01-01,1.00,SUCCESS,PROCESSED,PENDING,,DC,\n'
                  'RECORD_TYPE,TOTAL_RECORDS\nT,2\n')
        self.assertEqual(stitch.apply_results_file(self.company, output), {'paid': 1, 'failed': 0, 'pending': 0, 'ignored': 1})
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.provider_reference), ('paid', 'COL1'))
        self.assertEqual(SubscriptionCycle.objects.get().status, 'paid')
        # Disputed later: refunded by the bank, the period is unpaid again and the practice gets a task.
        disputed = output.replace('SUCCESS,PROCESSED', 'DISPUTED,DISPUTED', 1)
        self.assertEqual(stitch.apply_results_file(self.company, disputed)['failed'], 1)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')
        self.assertEqual(SubscriptionCycle.objects.get().status, 'failed')
        self.assertTrue(ClinicalTask.objects.filter(title__startswith='Debit failed').exists())

    def test_a_returned_debicheck_collection_is_retried_on_the_mandates_weekday(self):
        mandate, _, _ = self.approve()
        payment = Payment.objects.get()
        stitch.collection_file(self.company, today=self.today)
        failed = ('RECORD_TYPE,CLIENT_ID,PRODUCT,CHANNEL,FILE_TYPE\nP,c,COLLECTIONS,DEBICHECK,OUTPUT\n'
                  'RECORD_TYPE,EXTERNAL_BATCH_REFERENCE,EXTERNAL_COLLECTION_REFERENCE,COLLECTION_ID,COLLECTION_STATUS,COLLECTION_REASON\n'
                  f'D,B1,MHP{payment.pk:07d},COL1,FAILED,INSUFFICIENT_FUNDS\n')
        stitch.apply_results_file(self.company, failed)
        cycle = SubscriptionCycle.objects.get()
        self.assertEqual((cycle.attempts, cycle.failure_reason), (1, 'Insufficient funds.'))
        run_debits(self.company, today=cycle.next_attempt_on)
        retry = Payment.objects.exclude(pk=payment.pk).get()
        name, text, _ = stitch.collection_file(self.company, today=cycle.next_attempt_on)
        retry.refresh_from_db()
        self.assertEqual(retry.due_on.isoweekday(), mandate.collection_day)
        self.assertGreaterEqual(retry.due_on, cycle.next_attempt_on + timedelta(days=3))

    def test_a_kept_card_is_charged_with_initiate_transaction(self):
        details = {'card': {'last4': '1111', 'network': 'Visa'}}
        success = {'initiateTransaction': {'id': 'txn-1', 'state': {'__typename': 'TransactionSuccess'}}}
        mandate, url, _ = self.approve('card', details, Charge=success)
        self.assertIn('&redirect_uri=', url)
        self.assertEqual(mandate.account_label, 'Visa ···1111')
        self.assertEqual(SubscriptionCycle.objects.get().status, 'paid')
        declined = {'initiateTransaction': {'id': 'txn-2', 'state': {'__typename': 'TransactionFailure', 'reason': 'insufficientFunds'}}}
        fake = FakeStitch(Charge=declined)
        with patch('care.stitch.http_call', fake):
            run_debits(self.company, today=self.today + timedelta(weeks=4))
        sent = fake.calls[-1]['body']['variables']['input']
        self.assertEqual((sent['token'], sent['paymentMethods']['card']['recurringPayment']['source']), ('consent-1', 'merchant'))
        self.assertEqual(fake.calls[-1]['headers']['Idempotency-Key'], sent['nonce'])
        self.assertEqual(SubscriptionCycle.objects.get(cycle_number=2).failure_reason, 'Insufficient funds.')

    def test_signed_webhooks_settle_consents_and_a_revoked_mandate_stops_collections(self):
        mandate, _, _ = self.approve()
        body = json.dumps({'type': 'payment-consent-request', 'data': {'id': 'consent-1', 'status': 'REVOKED',
                                                                       'externalReference': mandate.reference,
                                                                       'statusReason': 'PAYMENT_SUSPENDED'}}).encode()
        request = RequestFactory().post('/billing/webhooks/stitch/', data=body, content_type='application/json',
                                        headers=signed(body))
        provider = current_provider()
        for notice in provider.read_webhook(request):
            apply_notice(provider, notice)
        mandate.refresh_from_db()
        self.assertEqual((mandate.status, mandate.failure_reason), ('cancelled', 'Stopped by the account holder.'))
        self.assertTrue(AuditEvent.objects.filter(action='billing.mandate_revoked').exists())

    def test_webhook_signatures_are_checked(self):
        body = b'{"type": "transaction"}'
        stitch.verify_signature(body, signed(body), STITCH_SECRET)
        for headers in (signed(body, secret='whsec_' + base64.b64encode(b'another-key').decode()),
                        signed(body, stamp=time.time() - 600), {**signed(body), 'svix-signature': 'v1,nope'}):
            with self.assertRaises(ValidationError):
                stitch.verify_signature(body, headers, STITCH_SECRET)
        with self.assertRaises(ValidationError):
            stitch.verify_signature(b'{"type": "changed"}', signed(body), STITCH_SECRET)

    def test_a_once_off_payment_is_made_on_stitchs_page(self):
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, subtotal=Decimal('180.00'),
                                             status=PharmacyOrder.Status.SUBMITTED)
        created = {'clientPaymentInitiationRequestCreate': {'paymentInitiationRequest': {'id': 'pay-1', 'url': 'https://secure.stitch.money/connect/payment-request/pay-1'}}}
        fake = FakeStitch(Pay=created)
        with patch('care.stitch.http_call', fake):
            payment, url = start_order_payment(order=order, actor=self.patient_user)
        self.assertTrue(url.startswith('https://secure.stitch.money/connect/payment-request/pay-1?redirect_uri=https%3A%2F%2Fmeridian.test'))
        sent = fake.calls[0]['body']['variables']['input']
        self.assertEqual((sent['amount'], sent['externalReference']), ({'quantity': '180.00', 'currency': 'ZAR'}, f'MHP{payment.pk:07d}'))
        completed = FakeStitch(Payment={'node': {'id': 'pay-1', 'state': {'__typename': 'PaymentInitiationRequestCompleted'}}})
        with patch('care.stitch.http_call', completed):
            result = current_provider().finish_checkout(Payment.objects.get(pk=payment.pk), {})
        self.assertEqual(result.status, 'paid')
