# Proper Purchase Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Meridian POs real purchase orders: full supplier details, supplier prices with a reference-price fallback, bill-to/ship-to, a Bio-Swiss-style PDF and an ArcFlow-style email pop-up. Suppliers that insist on their own order form get no Meridian PDF.

**Architecture:** New fields on `Supplier`, `SupplierProduct` and `PracticeSettings` hold the details. `care.purchasing.create_purchase_order` copies prices, supplier details and bill-to/ship-to onto the PO (`PurchaseOrder` JSON snapshots plus `PurchaseOrderItem` price columns), so documents render only from the PO. `care.purchase_exports` renders the PDF and Excel from those snapshots. The email view gains multi-recipient fields, uploads and an inline PDF preview, and its dialog becomes a two-column pop-up driven by one new JS file.

**Tech Stack:** Django 5.2 · Python 3.13 · SQLite locally / PostgreSQL in production · reportlab (PDF) · openpyxl (Excel) · pypdf (tests) · vanilla JS registered through `window.MeridianPages.register`.

**Spec:** [docs/superpowers/specs/2026-11-23-purchase-order-documents-design.md](../specs/2026-11-23-purchase-order-documents-design.md). Mockups: [docs/superpowers/specs/2026-11-23-purchase-order-documents-mockups.html](../specs/2026-11-23-purchase-order-documents-mockups.html). Read both before starting any task.

## Global Constraints

- Run tests with `.venv/bin/python manage.py test <labels> --parallel 4` from the repo root. Baseline: the 6 purchasing and supplier modules pass (100 tests).
- Every query is scoped by `company`. Follow the existing guarantees exactly: `_lock_company`, signed `workflow_context` (`make_workflow_context` / `validate_workflow_context`), idempotent `submission_key`, `record_audit` before any SMTP send, and demo orders can't be emailed.
- One migration per task that changes models, numbered from `care/migrations/0059_…`. Generate with `.venv/bin/python manage.py makemigrations care -n <name>`.
- Money is `Decimal` with 2 places, rounded `ROUND_HALF_UP`. It's shown everywhere as `format_rand()` output (`R 1 600.00`, with space thousands separators), and JS uses the same format.
- VAT number format (supplier and practice): spaces removed, then `^4\d{9}$`. Error copy: `Enter a 10-digit VAT number starting with 4.`
- Copy follows the mockups word for word where they show it, for example `Reference price`, `always attached`, `Upload their order form`, and `{supplier} uses their own order form. No Meridian PDF is attached. Upload their completed form below. You can still send without one.`
- When a task changes a CSS or JS file, bump that file's `?v=` query string in the template that loads it (the codebase cache-busts this way).
- Work on branch `feature/purchase-order-documents`. Commit at the end of each task, with the message ending in the `Co-Authored-By` line from the session instructions.

## Review Focus

1. **Supplier or price edited after a PO exists:** the PO, its PDF and its email stay exactly as created. Test in Task 4 (`test_snapshot_ignores_later_supplier_settings_and_price_edits`).
2. **Supplier not VAT-registered:** VAT is 0, the PDF shows `VAT 0%`, and the total equals the subtotal. Tests in Tasks 4 and 6.
3. **PO created before this change (no prices or snapshots):** the PDF still renders, with `–` in the price columns and no totals block, and the email still works. Test in Task 6 (`test_legacy_order_without_prices_renders_dashes_and_no_totals`).
4. **Supplier-generated supplier, nothing uploaded:** the email sends with zero attachments, and the audit's `document_sha256` is `None`. Test in Task 7.
5. **Bad upload (`.exe`, a sixth file, or over 10 MB in total):** the dialog reopens with an error, and nothing is sent or audited. Test in Task 7.

---

### Task 1: Supplier details

**Files:**
- Modify: `care/models.py` (`Supplier`, ~line 2325), `care/suppliers.py`, `portal/supplier_forms.py`, `portal/supplier_views.py`, `templates/portal/includes/supplier_editor.html`, `templates/portal/suppliers.html`, `static/css/suppliers.css`
- Create: `care/migrations/0059_supplier_details.py`
- Test: `care/test_suppliers.py`, `portal/test_suppliers.py`

**Interfaces:**
- Produces:
  - `Supplier` fields: `registered_name` (255), `registration_number` (40), `vat_registered` (bool, default True), `vat_number` (20), `address_street` (200), `address_suburb` (120), `address_city` (120), `address_province` (3, `choices=PROVINCES`), `address_postal_code` (10), `accounts_email` (Email), `payment_terms` (120), `account_number` (40), `sahpra_licence_number` (40), `sahpra_licence_expiry` (Date, null), `uses_own_order_form` (bool, default False). All text fields are `blank=True`. The existing `email` gets verbose_name `'Orders email'`.
  - `Supplier.address_lines() -> list[str]`: street, suburb, then "city postal_code", skipping blanks (same shape as `DeliveryAddress.lines()`).
  - `care.suppliers.SUPPLIER_DETAIL_FIELDS: tuple[str, ...]`: the 15 new field names, plus `contact_name`, `email` and `phone`.
  - `care.suppliers.normalise_vat_number(value: str) -> str`: returns the 10 digits, or raises `ValidationError` with the copy from Global Constraints. Blank stays blank.
  - `save_supplier(..., **details)`: accepts every `SUPPLIER_DETAIL_FIELDS` name as a keyword with defaults (`''`, `True` for `vat_registered`, `False` for `uses_own_order_form`, `None` for the expiry). It validates type, length and format only (not required-ness). When `vat_registered` is False, `vat_number` is stored as `''`.
  - `care.suppliers.supplier_flags(supplier, today) -> dict`: `{'incomplete': bool, 'licence_expired': bool}`.

- [ ] **Step 1: Write failing service tests** in `care/test_suppliers.py`:

```python
def test_details_are_saved_normalised_and_audited(self):
    supplier = self.save(registered_name=' Bio-Swiss (Pty) Ltd ', vat_number='435 026 3226', address_street='1 Road',
                         address_city='Midrand', address_province='GP', address_postal_code='1685',
                         payment_terms='30 days from statement', uses_own_order_form=True,
                         sahpra_licence_expiry=self.today + timedelta(days=30))
    self.assertEqual((supplier.registered_name, supplier.vat_number, supplier.uses_own_order_form),
                     ('Bio-Swiss (Pty) Ltd', '4350263226', True))
    after = AuditEvent.objects.get(action='supplier.created').metadata['after']
    self.assertEqual(after['vat_number'], '4350263226')
    self.assertEqual(after['sahpra_licence_expiry'], (self.today + timedelta(days=30)).isoformat())

def test_invalid_vat_number_and_province_are_rejected(self):
    for values in ({'vat_number': '123'}, {'vat_number': '5350263226'}, {'address_province': 'XX'}):
        with self.subTest(values=values), self.assertRaises(ValidationError):
            self.save(**values)

def test_not_vat_registered_clears_the_vat_number(self):
    self.assertEqual(self.save(vat_registered=False, vat_number='4350263226').vat_number, '')

def test_flags_mark_incomplete_and_expired_licence(self):
    supplier = self.save()
    self.assertEqual(supplier_flags(supplier, self.today), {'incomplete': True, 'licence_expired': False})
    supplier.sahpra_licence_expiry = self.today - timedelta(days=1)
    self.assertTrue(supplier_flags(supplier, self.today)['licence_expired'])
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test care.test_suppliers`. Expect errors for unknown keyword arguments and the missing import.

- [ ] **Step 3: Implement the model fields, migration `0059_supplier_details`, `address_lines`, `SUPPLIER_DETAIL_FIELDS`, `normalise_vat_number`, `supplier_flags` and the extended `save_supplier`.** Include the new fields in `_snapshot()` (dates as ISO strings) and in the idempotency `payload`. "Incomplete" means any form-required field is blank: `registered_name`, `address_street`, `address_city`, `address_province`, `address_postal_code`, `email`, `payment_terms`, or `vat_number` while `vat_registered`.

- [ ] **Step 4: Write failing portal tests** in `portal/test_suppliers.py`. First extend the `supplier_data()` helper so existing tests stay valid. It should post `registered_name='New Supplier (Pty) Ltd'`, `vat_registered='on'`, `vat_number='4350263226'`, `address_street='1 Road'`, `address_city='Cape Town'`, `address_province='WC'`, `address_postal_code='8000'`, `payment_terms='30 days'`, `sahpra_licence_number='22C-1'`, `sahpra_licence_expiry=<today+365>` and `uses_own_order_form='false'`, and take `**extra` overrides. Then add:

```python
def test_required_details_are_enforced_on_the_form(self):
    response = self.post_supplier(self.supplier_data(registered_name='', payment_terms=''))
    self.assertEqual(response.status_code, 400)
    self.assertIn('registered_name', response.context['supplier_form'].errors)
    self.assertIn('payment_terms', response.context['supplier_form'].errors)

def test_licence_required_only_when_a_prescription_product_is_linked(self):
    data = self.supplier_data(sahpra_licence_number='', sahpra_licence_expiry='')
    response = self.post_supplier(data)
    self.assertEqual(response.status_code, 400)
    self.assertIn('sahpra_licence_number', response.context['supplier_form'].errors)
    ok = self.supplier_data(products=[(self.open_product, 'OPEN-1')], name='Open Only',
                            sahpra_licence_number='', sahpra_licence_expiry='')
    self.assertEqual(self.post_supplier(ok).status_code, 302)

def test_vat_number_required_only_when_vat_registered(self):
    response = self.post_supplier(self.supplier_data(vat_number=''))
    self.assertIn('vat_number', response.context['supplier_form'].errors)
    data = self.supplier_data(name='No VAT', vat_number='')
    del data['vat_registered']
    self.assertEqual(self.post_supplier(data).status_code, 302)

def test_order_form_choice_saves_and_edit_form_shows_saved_details(self):
    self.post_supplier(self.supplier_data(name='Own Form', uses_own_order_form='true'))
    supplier = Supplier.objects.get(name='Own Form')
    self.assertTrue(supplier.uses_own_order_form)
    form = self.listing(edit=supplier.pk).context['supplier_form']
    self.assertEqual(form.initial['registered_name'], 'New Supplier (Pty) Ltd')

def test_list_flags_incomplete_and_expired_suppliers(self):
    self.assertContains(self.listing(), 'Details incomplete')  # cls.supplier has no new details
    Supplier.objects.filter(pk=self.supplier.pk).update(sahpra_licence_expiry=self.today - timedelta(days=1))
    self.assertContains(self.listing(), 'Licence expired')

def test_export_includes_the_new_details(self):
    _, rows = self.export()
    for heading in ('Registered name', 'VAT number', 'Address', 'Payment terms', 'Our account number',
                    'SAHPRA licence', 'Licence expiry', 'PO PDF'):
        self.assertIn(heading, rows[0])
```

- [ ] **Step 5: Run and confirm they fail.** Run `.venv/bin/python manage.py test portal.test_suppliers`.

- [ ] **Step 6: Implement the form, view, editor, list and export.**
  - `SupplierForm` adds every detail field. The form requires these fields: `name`, `registered_name`, `address_street`, `address_city`, `address_province` (a select over `PROVINCES`), `address_postal_code`, `email` (label `Orders email`) and `payment_terms`.
  - `vat_registered` is a `BooleanField(required=False, initial=True)` labelled `VAT-registered`. `clean()` requires `vat_number` when it is ticked and normalises it with `normalise_vat_number`.
  - `uses_own_order_form` is a `TypedChoiceField` with radio buttons: `('false', 'Meridian-generated PDF')` and `('true', 'Supplier-generated PDF')`, coerced to bool.
  - Add `require_licence(form, formset) -> None` in `portal/supplier_forms.py`. It calls `form.add_error('sahpra_licence_number', 'Enter their SAHPRA licence number: they supply prescription medicines.')` and adds the matching expiry error when any selected product has `requires_authorisation` and either value is blank. The create and edit views call it after both forms validate, and continue only if the form is still valid.
  - The view builds `initial` from `SUPPLIER_DETAIL_FIELDS` and passes `**{f: form.cleaned_data[f] for f in SUPPLIER_DETAIL_FIELDS}` to `save_supplier`.
  - The editor groups fields under small headings: **Company** (name, registered name, registration number, VAT-registered, VAT number), **Address**, **Ordering** (orders email, accounts email, contact, phone, payment terms, our account number), **SAHPRA licence** and **Purchase order PDF** (the radio). Add a two-column grid in `suppliers.css` that stacks at ≤ 760px.
  - Supplier cards show `supplier_flags` as small amber badges, `Details incomplete` and `Licence expired`, next to the product count.
  - The export adds the headings from the test. Address is `', '.join(address_lines())`, and PO PDF is `Supplier` or `Meridian`.

- [ ] **Step 7: Run and confirm everything passes.** Run `.venv/bin/python manage.py test care.test_suppliers portal.test_suppliers care.test_purchase_orders portal.test_purchase_orders --parallel 4`. Expect OK.

- [ ] **Step 8: Commit.** Commit `feat(suppliers): capture registered, VAT, address, licence and PO PDF details`.

---

### Task 2: Supplier product prices

**Files:**
- Modify: `care/models.py` (`SupplierProduct`), `care/suppliers.py`, `portal/supplier_forms.py`, `portal/supplier_views.py`, `templates/portal/includes/supplier_product_row.html`, `templates/portal/suppliers.html`, `static/css/suppliers.css`
- Create: `care/migrations/0060_supplier_product_prices.py`
- Test: `care/test_suppliers.py`, `portal/test_suppliers.py`

**Interfaces:**
- Consumes: Task 1's `save_supplier`.
- Produces:
  - `SupplierProduct.description` (255, blank), `pack_size` (40, blank), and `unit_price` (`DecimalField(10, 2, null=True, blank=True)`). `unit_price` has a `CheckConstraint` named `supplier_product_price_not_negative` (`unit_price__isnull=True | unit_price__gte=0`).
  - The `save_supplier(products=[...])` entries accept the optional keys `description`, `pack_size` and `unit_price` (a Decimal or None). These keys are stored, included in `_snapshot()['products']` (price as a string) and included in the idempotency payload.
  - `care.purchasing.format_rand(value: Decimal) -> str` (Task 4 and later tasks use it): `R ` plus the amount rounded half-up to 2 places, with spaces between thousands.

- [ ] **Step 1: Write failing tests.** Add `test_format_rand` to `care/test_purchase_orders.py`, asserting `format_rand(Decimal('1600')) == 'R 1 600.00'`, `format_rand(Decimal('44200.5')) == 'R 44 200.50'` and `format_rand(Decimal('0')) == 'R 0.00'`. Add `test_link_saves_description_size_and_price` (care), which saves `{'product': self.product, 'supplier_code': 'T5/0.6/2.4 PFS', 'description': 'Tirzepatide 5mg/0,6ml PFS', 'pack_size': '2,4ml', 'unit_price': Decimal('1600.00')}` and asserts all three fields on the link and `audit['after']['products'][0]['unit_price'] == '1600.00'`. Add `test_negative_price_is_rejected` (care, `unit_price=Decimal('-1')` raises `ValidationError`). Add `test_editor_posts_and_shows_prices` (portal): post `products-0-unit_price='1600.00'`, `products-0-pack_size='2,4ml'` and `products-0-description='Tirzepatide 5mg/0,6ml PFS'`, then assert the saved link and that the listing contains `R 1 600.00`. A link without a price shows `Reference R 100.00` (from `self.product.price`).
- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test care.test_suppliers portal.test_suppliers`.
- [ ] **Step 3: Implement.**
  - Migration `0060_supplier_product_prices`.
  - `SupplierProductForm` gains `description`, `pack_size` and `unit_price` (`DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2)`, label `Unit price excl. VAT (R)`).
  - `supplier_product_forms` seeds those values in `initial`, and the views pass them into `products`.
  - The row template lays out product, code, description, size and price on one grid row, wrapping on narrow screens.
  - Add `format_rand` to `care/purchasing.py`.
  - The supplier card's product table adds Description, Size and Unit price columns. Price is `format_rand`, or `Reference {format_rand(product.price)}` in muted text.
  - The export adds the columns `Supplier description`, `Size` and `Unit price excl. VAT`.
- [ ] **Step 4: Run and confirm they pass.** Run the same command. Expect OK.
- [ ] **Step 5: Commit.** Commit `feat(suppliers): supplier description, pack size and unit price per product`.

---

### Task 3: Bill-to and ship-to in Practice settings

**Files:**
- Modify: `care/models.py` (`PracticeSettings`), `portal/practice_settings_views.py`, `templates/portal/practice_settings.html`
- Create: `care/migrations/0061_purchase_order_settings.py`, `portal/test_purchase_order_settings.py`

**Interfaces:**
- Consumes: `care.suppliers.normalise_vat_number` (Task 1).
- Produces:
  - `PracticeSettings` fields:
    - `po_bill_to_name` (150, `'Bill to (name of entity)'`)
    - `po_billing_street` / `_suburb` / `_city` / `_province` (`PROVINCES`) / `_postal_code`
    - `po_vat_number` (20, `'Practice VAT number'`, help `Leave blank if the practice isn't VAT-registered.`)
    - `po_pharmacy_number` (20, `'Pharmacy Y number'`)
    - `po_accounts_email` (Email, `'Accounts email'`)
    - `po_doctor` (FK `AUTH_USER_MODEL`, null, `SET_NULL`, `related_name='+'`, `'Doctor named on purchase orders'`)
    - `po_delivery_instructions` (300, `'Delivery instructions for suppliers'`)
    - All blank-able.
  - `PracticeSettings.po_billing_lines() -> list[str]`.

- [ ] **Step 1: Write failing tests** in the new `portal/test_purchase_order_settings.py`. Use `OperationsPortalFixture` and log in as `self.super_admin`. Give `self.doctor` `clinician_type='doctor'` on its Alpha membership in `setUpTestData`.

```python
def test_super_admin_saves_purchase_order_details_and_it_is_audited(self):
    response = self.client.post(reverse('portal:practice-settings'), {'section': 'purchasing',
        'po_bill_to_name': 'Dr Example', 'po_billing_street': '6 Harfield Rd', 'po_billing_city': 'Cape Town',
        'po_billing_province': 'WC', 'po_billing_postal_code': '7708', 'po_vat_number': '',
        'po_pharmacy_number': '', 'po_accounts_email': 'accounts@example.test', 'po_doctor': self.doctor.pk,
        'po_delivery_instructions': 'Hand to reception'})
    self.assertEqual(response.status_code, 302)
    row = PracticeSettings.objects.get(company=self.company)
    self.assertEqual((row.po_bill_to_name, row.po_doctor_id), ('Dr Example', self.doctor.pk))
    self.assertTrue(AuditEvent.objects.filter(action='practice.settings_updated', metadata__po_bill_to_name='Dr Example').exists())

def test_doctor_choices_are_this_practices_active_prescribers_only(self):
    form = self.client.get(reverse('portal:practice-settings')).context['purchasing_form']
    choices = set(form.fields['po_doctor'].queryset.values_list('pk', flat=True))
    self.assertEqual(choices, {self.doctor.pk})

def test_invalid_practice_vat_number_is_rejected(self):
    response = self.client.post(reverse('portal:practice-settings'), {'section': 'purchasing', 'po_vat_number': '12'})
    self.assertEqual(response.status_code, 400)
    self.assertIn('po_vat_number', response.context['purchasing_form'].errors)
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test portal.test_purchase_order_settings`.
- [ ] **Step 3: Implement.**
  - Migration `0061_purchase_order_settings`.
  - Add `PurchaseOrderSettingsForm(ModelForm)` with an `__init__(*args, company, **kwargs)` that limits `po_doctor` to users with an active `company_memberships` row in `company` with `clinician_type__in=CompanyMembership.PRESCRIBER_TYPES`. `clean_po_vat_number` uses `normalise_vat_number`.
  - Register it as `SECTION_FORMS['purchasing']`, and pass `company=self.company` when constructing it in `display()` and `post()` (only this form takes `company`).
  - Add a template section `Purchase orders`, with the intro `Printed as Bill to on every purchase order. Ship to is the courier collection address above.`, placed after Courier and reusing `practice-settings-form--grid`.
- [ ] **Step 4: Run and confirm they pass.** Run `.venv/bin/python manage.py test portal.test_purchase_order_settings portal.test_view_log_setting`. Expect OK.
- [ ] **Step 5: Commit.** Commit `feat(settings): purchase order bill-to details and doctor named on orders`.

---

### Task 4: Prices and snapshots saved on the PO

**Files:**
- Modify: `care/models.py` (`PurchaseOrder`, `PurchaseOrderItem`), `care/purchasing.py`
- Create: `care/migrations/0062_purchase_order_prices.py`
- Test: `care/test_purchase_orders.py`

**Interfaces:**
- Consumes: Tasks 1–3 fields.
- Produces (all in `care/purchasing.py` unless noted):
  - `PurchaseOrder.supplier_details` (`JSONField(default=dict, blank=True)`), `PurchaseOrder.buyer_details` (`JSONField(default=dict, blank=True)`) and `PurchaseOrder.vat_rate` (`DecimalField(4, 2, null=True, blank=True)`).
  - `PurchaseOrderItem.unit_price` (`DecimalField(10, 2, null=True)`), `price_source` (`CharField(10, choices=[('supplier','Supplier price'),('reference','Reference price')], blank=True)`), `description` (255, blank) and `pack_size` (40, blank).
  - `VAT_RATE = Decimal('15.00')`. `format_rand` already exists from Task 2.
  - `unit_price_for(link: SupplierProduct, product: MedicationProduct) -> tuple[Decimal, str]` returns `(link.unit_price, 'supplier')`, or `(product.price, 'reference')` when the link has no price.
  - `supplier_snapshot(supplier: Supplier) -> dict` with keys `name, registered_name, registration_number, vat_registered, vat_number, address (list[str]), email, accounts_email, contact_name, phone, payment_terms, account_number, sahpra_licence_number, sahpra_licence_expiry (ISO or None)`.
  - `buyer_snapshot(company: Company) -> dict` returns `{'bill_to': {name, practice, address (list), vat_number, pharmacy_number, accounts_email, doctor_name, doctor_hpcsa}, 'ship_to': {name, address (list), contact, phone, instructions}}`. `doctor_name` is `f'{title} {full name}'` stripped, from `po_doctor`, and `doctor_hpcsa` is `po_doctor.hpcsa_number`. `ship_to` comes from the `collection_*` fields, with `name` = `collection_business` or the company name. A missing `PracticeSettings` row gives empty strings and lists.
  - `order_amounts(order: PurchaseOrder) -> dict | None` returns `{'lines': {item_pk: Decimal line_total}, 'subtotal', 'vat_rate', 'vat', 'total'}`, or `None` if `order.vat_rate is None` or any item's `unit_price is None`.

- [ ] **Step 1: Write failing tests** in a new `class PurchaseOrderPricingTests(PurchaseOrderFixture)` in `care/test_purchase_orders.py`:

```python
def test_supplier_price_is_used_and_reference_price_fills_gaps(self):
    SupplierProduct.objects.filter(supplier=self.supplier, product=self.product).update(
        unit_price=Decimal('1600.00'), description='Tirzepatide 5mg/0,6ml PFS', pack_size='2,4ml')
    order = self.create_order(items=[{'product': self.product, 'quantity': 10}, {'product': self.open_product, 'quantity': 2}])
    priced = {item.product_id: (item.unit_price, item.price_source, item.description, item.pack_size) for item in order.items.all()}
    self.assertEqual(priced[self.product.pk], (Decimal('1600.00'), 'supplier', 'Tirzepatide 5mg/0,6ml PFS', '2,4ml'))
    self.assertEqual(priced[self.open_product.pk], (Decimal('40.00'), 'reference', '', ''))

def test_amounts_match_the_bio_swiss_example(self):
    # 10 × 1600 + 6 × 2500 + 4 × 3300 = 44 200; VAT 6 630; total 50 830
    ...  # build three priced lines (create extra products in the test), then:
    amounts = order_amounts(order)
    self.assertEqual((amounts['subtotal'], amounts['vat'], amounts['total']),
                     (Decimal('44200.00'), Decimal('6630.00'), Decimal('50830.00')))

def test_vat_rate_is_zero_for_a_supplier_that_is_not_vat_registered(self):
    Supplier.objects.filter(pk=self.supplier.pk).update(vat_registered=False)
    order = self.create_order()
    self.assertEqual(order.vat_rate, Decimal('0.00'))
    amounts = order_amounts(order)
    self.assertEqual((amounts['vat'], amounts['total']), (Decimal('0.00'), amounts['subtotal']))

def test_snapshot_ignores_later_supplier_settings_and_price_edits(self):
    order = self.create_order()
    before = (order.supplier_details, order.buyer_details, list(order.items.values_list('unit_price', flat=True)))
    Supplier.objects.filter(pk=self.supplier.pk).update(name='Renamed', payment_terms='COD')
    MedicationProduct.objects.filter(pk=self.product.pk).update(price=Decimal('999.00'))
    order.refresh_from_db()
    self.assertEqual((order.supplier_details, order.buyer_details,
                      list(order.items.values_list('unit_price', flat=True))), before)

def test_replay_after_a_price_change_returns_the_original_order(self):
    key = uuid4()
    first = self.create_order(key=key)
    SupplierProduct.objects.filter(supplier=self.supplier).update(unit_price=Decimal('5.00'))
    self.assertEqual(self.create_order(key=key).items.get().unit_price, first.items.get().unit_price)

def test_buyer_snapshot_uses_bill_to_settings_doctor_and_collection_address(self):
    # Set PracticeSettings po_* fields, po_doctor=self.doctor (hpcsa_number='MP0000000', title 'Dr'),
    # collection_* fields; assert order.buyer_details['bill_to']['doctor_hpcsa'] == 'MP0000000' and
    # order.buyer_details['ship_to']['address'] == ['1 Demo Road', 'Claremont', 'Cape Town 7708'].

def test_legacy_order_has_no_amounts(self):
    order = self.create_order()
    PurchaseOrder.objects.filter(pk=order.pk).update(vat_rate=None)
    order.refresh_from_db()
    self.assertIsNone(order_amounts(order))
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test care.test_purchase_orders`.
- [ ] **Step 3: Implement.**
  - Migration `0062_purchase_order_prices`.
  - `create_purchase_order` fetches the links (it already fetches codes; widen that to the full rows) and sets `supplier_details`, `buyer_details` and `vat_rate` (`VAT_RATE` if `supplier.vat_registered`, else `Decimal('0.00')`). Each item gets `unit_price`, `price_source`, `description` and `pack_size`.
  - Add `unit_price` and `price_source` to each audit `items` entry, as a string.
  - Replay identity stays products, quantities, supplier and ETA.
- [ ] **Step 4: Run and confirm they pass.** Run `.venv/bin/python manage.py test care.test_purchase_orders portal.test_purchase_orders --parallel 4`. Expect OK.
- [ ] **Step 5: Commit.** Commit `feat(purchasing): save prices, VAT rate, supplier and bill-to/ship-to on each PO`.

---

### Task 5: Prices in the Review order step

**Files:**
- Modify: `portal/purchasing_views.py` (the `purchase_supplier_products` context), `templates/portal/includes/purchasing_order_preview.html`, `templates/portal/includes/purchasing_order_line.html`, `static/js/purchasing_selection.js`, `static/css/purchasing.css`, `templates/portal/purchasing.html` (version bumps)
- Test: `portal/test_purchase_orders.py`

**Interfaces:**
- Consumes: `unit_price_for` and `VAT_RATE` (Task 4).
- Produces: `purchase_supplier_products` JSON with the shape `{"<supplier_id>": {"vat_rate": "15.00", "products": {"<product_id>": {"price": "1600.00", "reference": false}}}}`, with prices as 2-place strings.

- [ ] **Step 1: Write failing tests.** Add `test_review_data_carries_supplier_and_reference_prices`. Give one link a `unit_price` of `Decimal('1600.00')` and leave another link unpriced. Load `self.page('purchasing')`, read `response.context['purchase_supplier_products']`, and assert `data[str(supplier.pk)]['products'][str(product.pk)] == {'price': '1600.00', 'reference': False}`, the unpriced product equals `{'price': '40.00', 'reference': True}`, and `vat_rate == '15.00'`. Also assert `'0.00'` for a supplier with `vat_registered=False`. Add `test_server_bound_review_keeps_price_cells`: post an invalid create (quantity 0), then assert the HTML contains `data-order-unit-price` and `data-order-totals`.
- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test portal.test_purchase_orders`.
- [ ] **Step 3: Implement.**
  - Build the JSON in one query over `SupplierProduct` with `select_related('supplier', 'product')`.
  - Add the `Unit price` and `Line total` columns, with `<td data-order-unit-price>` and `<td data-order-line-total>`, to both the `<template>` row and `purchasing_order_line.html`. Add a `<tfoot data-order-totals>` with Subtotal (excl VAT), VAT {rate}% and Total (incl VAT).
  - In JS, eligibility uses `Object.keys(entry.products)`. On every `refreshSummary()`, for the selected supplier, fill each row's price and line total using integer cents, add `<span class="purchasing-price-tag">Reference price</span>` when `reference`, and fill the totals.
  - With no supplier chosen, the cells show `—` and the totals are hidden.
  - Add a JS `formatRand(cents)` that matches `format_rand`.
- [ ] **Step 4: Run and confirm they pass.** Run `.venv/bin/python manage.py test portal.test_purchase_orders`. Expect OK.
- [ ] **Step 5: Check it by hand in the browser.** Sign in as the demo Super Admin and open `/purchasing/`. Tick 2–3 products, click Order, and choose a supplier. Check that the prices, the Reference price tag and the totals match a hand calculation, and that changing a quantity updates them.
- [ ] **Step 6: Commit.** Commit `feat(purchasing): unit prices, reference tag and totals in Review order`.

---

### Task 6: PDF and Excel redesign

**Files:**
- Modify: `care/purchase_exports.py`
- Test: `care/test_purchase_exports.py`, `portal/test_purchase_documents.py` (update the expectations that relied on Received/Remaining in the PDF)

**Interfaces:**
- Consumes: the snapshots, `order_amounts` and `format_rand` (Task 4).
- Produces: the unchanged signatures `purchase_order_pdf(order) -> SpooledTemporaryFile` and `purchase_order_workbook(order) -> SpooledTemporaryFile`.

- [ ] **Step 1: Write failing tests** in `care/test_purchase_exports.py`. Use a helper that builds a priced order through `create_purchase_order`, with the Bio-Swiss example lines and filled supplier and practice details, and reads text with `PdfReader`.

```python
def test_pdf_has_header_parties_lines_and_totals(self):
    text = self.pdf_text(self.priced_order())
    for expected in ('PURCHASE ORDER', 'PO-00001', 'Bio-Swiss (Pty) Ltd', '4350263226', 'Bill to', 'Ship to',
                     'MP0000000', 'T5/0.6/2.4 PFS', 'Tirzepatide 5mg/0,6ml PFS', '2,4ml', 'R 1 600.00',
                     'Subtotal (excl VAT)', 'VAT 15%', 'R 50 830.00', 'Payment terms', 'Authorised by'):
        self.assertIn(expected, text)

def test_cold_chain_note_only_with_cold_chain_products(self):
    self.assertIn('Cold-chain products: deliver refrigerated', self.pdf_text(self.priced_order()))
    self.assertNotIn('Cold-chain', self.pdf_text(self.priced_order(products=[self.open_product])))

def test_non_vat_supplier_pdf_shows_vat_zero(self):
    self.assertIn('VAT 0%', self.pdf_text(self.priced_order(vat_registered=False)))

def test_legacy_order_without_prices_renders_dashes_and_no_totals(self):
    order = self.priced_order()
    PurchaseOrder.objects.filter(pk=order.pk).update(vat_rate=None, supplier_details={}, buyer_details={})
    order.items.update(unit_price=None)
    text = self.pdf_text(PurchaseOrder.objects.get(pk=order.pk))
    self.assertIn('–', text)
    self.assertNotIn('Total (incl VAT)', text)

def test_workbook_adds_prices_and_keeps_receipt_progress(self):
    # Header row includes 'Description', 'Size', 'Unit price (excl VAT)', 'Line total (excl VAT)', 'Received', 'Remaining';
    # a totals block has 'Subtotal (excl VAT)', 'VAT 15%', 'Total (incl VAT)' with numeric cells formatted '#,##0.00'.
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test care.test_purchase_exports`.
- [ ] **Step 3: Implement the PDF** to match mockup 1. Use reportlab `Table`s inside `SimpleDocTemplate`, keeping the existing `_fonts()`, `_text()`, demo label and footer pattern (`Meridian Health · PO-00001`, `Page N`).
  - **Header:** a navy band (`#1D4A5E`) with white bold `PURCHASE ORDER`, beside `static/images/meridian-logo.png` loaded through `django.contrib.staticfiles.finders.find`. If the logo is missing, print the company name instead.
  - **Facts row:** PO number, date of order, delivery requested (ETA or `–`), ordered by (`created_by` full name).
  - **Party boxes** (Supplier / Bill to / Ship to) render from the snapshot dicts, with `–` for blanks. A legacy order falls back to `supplier_name` and `supplier_email`.
  - **Lines table** (`repeatRows=1`): code (grey cell), description (`description` or `product_name + ' ' + strength`), size, unit price, qty, line total.
  - **Below the lines:** terms and notes on the left and the totals table on the right (navy grand-total row), then the Authorised by and Date boxes. The cold-chain note is red, printed when `any(item.product.requires_cold_chain)`.
- [ ] **Step 4: Implement the workbook changes.** Keep the existing structure. Add the four columns and a totals block, then update the widths and `auto_filter`.
- [ ] **Step 5: Run and confirm they pass.** Run `.venv/bin/python manage.py test care.test_purchase_exports portal.test_purchase_documents --parallel 4`. Expect OK. Then render one PDF to the scratchpad and **look at it** next to the mockup.
- [ ] **Step 6: Commit.** Commit `feat(purchasing): Bio-Swiss style purchase order PDF with prices and parties`.

---

### Task 7: Email backend: recipients, uploads, supplier-generated forms, inline preview

**Files:**
- Modify: `portal/purchase_document_views.py`, `portal/purchasing_views.py` (email context)
- Test: `portal/test_purchase_documents.py` (update `data()` to use `to` instead of `recipient`, and the multipart posts)

**Interfaces:**
- Consumes: `order_amounts` and `format_rand` (Task 4), and `Supplier.uses_own_order_form` (Task 1).
- Produces for Task 8:
  - `PurchaseOrderEmailForm` fields `to`, `cc`, `bcc` (`CharField`s holding comma-, semicolon- or newline-separated emails), `subject`, `message` and `attachments` (a multi-file field).
  - Context keys `email_own_form: bool`, `email_pdf_url: str` (`reverse('portal:purchase-order-pdf', ...) + '?inline=1'`), `email_amounts: dict | None` and `email_supplier` (the `Supplier`, or None).
  - Limits: `ATTACHMENT_EXTENSIONS = ('pdf','xlsx','xls','csv','docx','doc','png','jpg','jpeg')`, `MAX_ATTACHMENTS = 5` and `MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024` (the total, including the generated PDF).

- [ ] **Step 1: Write failing tests:**

```python
def test_multiple_recipients_cc_bcc_and_reply_to_the_sender(self):
    response = self.client.post(self.url('email'), self.data(to='a@example.test; b@example.test', cc='c@example.test', bcc='d@example.test'))
    self.assertEqual(response.status_code, 302)
    sent = mail.outbox[0]
    self.assertEqual((sent.to, sent.cc, sent.bcc, sent.reply_to),
                     (['a@example.test', 'b@example.test'], ['c@example.test'], ['d@example.test'], [self.admin.email]))

def test_one_invalid_address_names_it(self):
    response = self.client.post(self.url('email'), self.data(to='a@example.test, nope'))
    self.assert_email_error(response)
    self.assertIn('nope', str(response.context['email_form'].errors['to']))

def test_uploaded_files_are_attached_with_the_po_and_fingerprinted(self):
    upload = SimpleUploadedFile('their-form.pdf', b'%PDF-1.4 test', content_type='application/pdf')
    self.client.post(self.url('email'), {**self.data(), 'attachments': [upload]})
    names = [name for name, *_ in mail.outbox[0].attachments]
    self.assertEqual(names, [f'purchase-order-{self.order.reference}.pdf', 'their-form.pdf'])
    meta = self.sent_audits().get().metadata
    self.assertEqual(meta['attachments'][0]['name'], 'their-form.pdf')
    self.assertEqual(len(meta['attachments'][0]['sha256']), 64)

def test_supplier_generated_sends_without_meridian_pdf_even_with_nothing_attached(self):
    Supplier.objects.filter(pk=self.order.supplier_id).update(uses_own_order_form=True)
    self.assertEqual(self.client.post(self.url('email'), self.data()).status_code, 302)
    self.assertEqual(mail.outbox[0].attachments, [])
    self.assertIsNone(self.sent_audits().get().metadata['document_sha256'])

def test_bad_uploads_reopen_the_dialog_and_send_nothing(self):
    exe = SimpleUploadedFile('tool.exe', b'MZ', content_type='application/octet-stream')
    big = SimpleUploadedFile('big.pdf', b'%PDF' + b'0' * (10 * 1024 * 1024), content_type='application/pdf')
    six = [SimpleUploadedFile(f'f{i}.pdf', b'%PDF', content_type='application/pdf') for i in range(6)]
    for files in ([exe], [big], six):
        with self.subTest(count=len(files)):
            self.assert_email_error(self.client.post(self.url('email'), {**self.data(), 'attachments': files}))
    self.assertEqual(len(mail.outbox), 0)
    self.assertFalse(self.sent_audits().exists())

def test_dialog_defaults_to_supplier_orders_email_and_a_filled_message(self):
    form = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk}).context['email_form']
    self.assertEqual(form.initial['to'], 'saved-supplier@example.test')
    self.assertIn(self.order.reference, form.initial['message'])
    self.assertIn('Please confirm receipt', form.initial['message'])

def test_inline_pdf_is_framable_same_origin_and_not_audited(self):
    response = self.client.get(self.url('pdf') + '?inline=1')
    self.assertEqual(response['Content-Disposition'].split(';')[0], 'inline')
    self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
    self.assertFalse(AuditEvent.objects.filter(action='purchase_order.pdf_exported').exists())
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test portal.test_purchase_documents`.
- [ ] **Step 3: Implement the form.**
  - Add a `MultipleFileInput(ClearableFileInput)` with `allow_multiple_selected = True`, and a `MultipleFileField(FileField)` whose `clean` returns a list. This is Django's documented pattern.
  - `clean_to`, `clean_cc` and `clean_bcc` split on `[,;\n]`, validate each address with `validate_email` (the error names the bad address), de-duplicate, and cap at 10. `to` must have at least 1.
  - `clean()` checks the attachment extensions, the count, and the total bytes against `MAX_ATTACHMENT_BYTES`. The view adds the generated PDF's size before sending and fails with the same error if it goes over.
  - The form's initial message is:
    - `Dear {contact_name or f'{supplier name} team'},`
    - a blank line, then `Please find attached purchase order {ref}` plus `, for delivery by {Weekday d Month YYYY}` when there's an ETA, then `.`
    - for a supplier-generated supplier, the line is `Please find our completed order form attached` plus the same ETA clause instead
    - a blank line, then `Please confirm receipt of this order and let us know the delivery date and any tracking details.`
    - a blank line, then `Kind regards,` with the user's full name and the company name on separate lines.
- [ ] **Step 4: Implement the send.**
  - Change `_send_order_email` to take `files`. Build `to`, `cc`, `bcc` and `reply_to=[request.user.email]`. Attach the PDF unless the current supplier has `uses_own_order_form`, then attach the uploads (read once, with sha256).
  - Add `recipients` (to/cc/bcc), `attachments` (`[{name,size,sha256}]`), `uses_own_order_form` and `document_sha256` (None when no PDF) to the audit metadata.
  - The idempotency fingerprint covers the cleaned text fields plus the upload hashes.
  - The fallback body when the message is blank is `Please find purchase order {ref} attached.`, or `Please find our order attached.` for a supplier-generated supplier.
  - The view reads `request.FILES.getlist('attachments')`.
- [ ] **Step 5: Implement the inline preview.** In `_PurchaseOrderDocumentView.get`, when `request.GET.get('inline') == '1'` and it is the PDF view, return `FileResponse(..., as_attachment=False)` with `X-Frame-Options: SAMEORIGIN` (`django.views.decorators.clickjacking.xframe_options_sameorigin` semantics) and skip `record_audit`. Add `email_own_form`, `email_pdf_url`, `email_amounts` and `email_supplier` to the purchasing context where `email_order` is set.
- [ ] **Step 6: Run and confirm they pass.** Run `.venv/bin/python manage.py test portal.test_purchase_documents portal.test_purchase_orders --parallel 4`. Expect OK.
- [ ] **Step 7: Commit.** Commit `feat(purchasing): email POs to several recipients with uploads and supplier-generated forms`.

---

### Task 8: Email pop-up UI

**Files:**
- Modify: `templates/portal/includes/purchase_order_email_dialog.html` (rewrite), `templates/portal/purchasing.html` (load the new CSS and JS with versions)
- Create: `static/js/purchase_order_email.js`, `static/css/purchase_order_email.css`
- Test: `portal/test_purchase_documents.py`

**Interfaces:**
- Consumes: Task 7's context keys and form fields.

- [ ] **Step 1: Write failing render tests:**

```python
def test_meridian_supplier_dialog_locks_the_pdf_and_frames_the_preview(self):
    html = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk}).content.decode()
    self.assertIn('enctype="multipart/form-data"', html)
    self.assertIn(f'purchase-order-{self.order.reference}.pdf', html)
    self.assertIn('always attached', html)
    self.assertIn('?inline=1', html)

def test_supplier_generated_dialog_shows_the_notice_and_upload_zone(self):
    Supplier.objects.filter(pk=self.order.supplier_id).update(uses_own_order_form=True)
    html = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk}).content.decode()
    self.assertIn('Saved supplier name uses their own order form.', html)
    self.assertIn('Upload their order form', html)
    self.assertNotIn('always attached', html)
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python manage.py test portal.test_purchase_documents`.
- [ ] **Step 3: Implement the template and CSS** to match mockups 2 and 3.
  - **Dialog:** `app-dialog app-dialog--email purchasing-order-dialog`, max-width `min(66rem, calc(100vw - 2rem))`. Two columns: the form, and a `side` panel 20rem wide on `--console-canvas`.
  - **Left column:** To with CC and BCC pill buttons that reveal hidden CC and BCC rows, then Subject, Message (textarea, 9 rows), then Attachments, then Cancel and Send email.
  - **Attachments:** a locked PDF tile (name `purchase-order-{reference}.pdf`, the real attachment name, then `always attached`) and an `+ Add files` drop target, or the amber notice plus a dashed upload zone.
  - **Right column:** the Preview heading. Meridian-generated shows an `<iframe src="{{ email_pdf_url }}" title="Purchase order preview">` and an `Open PDF in new tab` link (`target="_blank" rel="noopener"`). Supplier-generated shows the empty-state copy from mockup 3. Below that, a details list: Supplier, Our account (`email_supplier.account_number` or `–`), Lines (`n products · m units`), and Total incl VAT (`format_rand`, or `–` when `email_amounts` is None).
  - Keep the existing hidden inputs (`workflow_context`, `view`, `group`, `q`).
  - **At ≤ 760px:** a single column, with the preview iframe hidden and only the "Open PDF in new tab" link shown.
- [ ] **Step 4: Implement `purchase_order_email.js`.** Register it with `MeridianPages.register('purchase_order_email', …)`, using the same `listen` helper as `suppliers.js`.
  - **Recipient chips:** each recipient field is a text input. Typing a comma, semicolon or Enter, or blurring the field, turns valid addresses into removable chips. The real `<input name="to|cc|bcc">` is a hidden input kept as a comma-joined list, so the form still works without JS (the hidden input is a plain text input until JS upgrades it).
  - **CC/BCC:** the toggles reveal their rows and focus them.
  - **Files:** chosen files go into a `DataTransfer`-backed `input[type=file][multiple]`. Each one shows as a removable tile. Enforce the 5-file and 10 MB limits on the client with the same messages.
  - **Preview:** the first uploaded PDF or image is previewed via `URL.createObjectURL` in the side panel (revoke it on change). For a Meridian-generated supplier, the side panel keeps the PO iframe.
  - **Closing:** on dialog close, strip `email` from the URL (as the existing dialog close handler does).
- [ ] **Step 5: Run and confirm they pass.** Run `.venv/bin/python manage.py test portal.test_purchase_documents`. Expect OK.
- [ ] **Step 6: Check it by hand in the browser.** Open an On order PO's email icon for each supplier setting. Compare against mockups 2 and 3: chips, CC/BCC, adding and removing a file, the preview, the mobile width at 390px, and a real send with the console backend that prints To/CC/BCC and the attachments.
- [ ] **Step 7: Commit.** Commit `feat(purchasing): two-column email pop-up with recipients, attachments and preview`.

---

### Task 9: Demo data and docs

**Files:**
- Modify: `care/demo_data.py` (~line 291), `docs/USER_GUIDE.md` (the purchasing/suppliers section), `README.md` (one line under the operations paragraph)
- Test: `care.test_demo_data`, `portal.test_demo_data`, `care.test_demo_safety`

- [ ] **Step 1: Fill in the demo supplier.** Give it a registered name, `vat_number='4000000000'`, an address in Midrand GP, payment terms `30 days from statement`, a SAHPRA licence `22C-DEMO` expiring in a year, and a `unit_price` on half of its links, so the demo shows both supplier and reference prices. Keep everything else the demo creates unchanged.
- [ ] **Step 2: Run the demo tests.** Run `.venv/bin/python manage.py test care.test_demo_data portal.test_demo_data care.test_demo_safety --parallel 4`. Expect OK, and update any assertion that lists supplier fields.
- [ ] **Step 3: Update the docs.** The user guide gets a short "Suppliers and purchase orders" section: the new supplier details, prices and the reference fallback, Practice settings → Purchase orders, the PDF, emailing, and supplier-generated forms. README.md gets one sentence pointing to it.
- [ ] **Step 4: Run the full suite.** Run `.venv/bin/python manage.py test --parallel 4`. Expect OK. Then run `.venv/bin/python manage.py migrate` on the local database.
- [ ] **Step 5: Commit.** Commit `docs(purchasing): demo supplier details and user guide for proper purchase orders`.
