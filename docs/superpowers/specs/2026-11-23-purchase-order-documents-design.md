# Proper purchase orders: supplier details, prices, PDF and email

Date: 23 Nov 2026 · Status: approved by Andre (mockups approved, "go ahead and build it")

## Why

Meridian's purchase orders are quantity lists. The PO a supplier receives has no prices, no VAT, no bill-to or ship-to, and none of the supplier details that make it a real order. Suppliers such as Bio-Swiss also insist on their own order form. Andre wants POs that look like a proper order form (modelled on the Bio-Swiss "Product Order Form"), and an email pop-up like ArcFlow's: To/CC/BCC, an editable message, the PDF attached, extra files and a preview.

## Goals

1. **Supplier record** captures what a PO needs: registered name, registration number, VAT, physical address, orders email, accounts email, payment terms, our account number with them, and a SAHPRA licence. It also has a switch: **PO PDF made by Meridian** or **by the supplier**.
2. **Product links** carry the supplier's own description, pack size and unit price excl. VAT, next to the existing supplier product code.
3. **Practice settings** gain a *Purchase orders* section with bill-to details. Ship-to is the courier collection address that already exists.
4. **Creating a PO**: the review step shows unit price, line total, subtotal, VAT and total. A line uses the supplier's price, or the catalogue **reference price** (`MedicationProduct.price`) with a "Reference price" tag when the supplier has no price.
5. **PO snapshot**: prices, supplier details and bill-to/ship-to are saved on the PO when it is created. Later edits never change a sent PO.
6. **PDF** follows the approved mockup: navy "PURCHASE ORDER" band with the Meridian logo, a facts row, Supplier / Bill to / Ship to boxes, a lines table, totals, payment terms, an "Authorised by" box and a cold-chain note.
7. **Email pop-up** follows the approved mockups, in Meridian's existing dialog style.
   - **Meridian-generated supplier:** the PO PDF is locked as an attachment.
   - **Supplier-generated supplier:** no PDF is attached. An amber notice asks you to upload their completed form, and Send stays available without it.

## Non-goals

- Filling in a supplier's own form automatically (option B was rejected).
- Banking details, B-BBEE, postal address, default lead time.
- Storing uploaded attachments. They are emailed and fingerprinted in the audit log only.
- Rich-text email, saved email templates, editing prices in the review step.
- Mobile purchasing screens.

## Anti-goals (a failure even if it works)

- A price, supplier detail or bill-to change silently altering a PO already created or sent.
- A PO PDF showing a price as the supplier's when it is a reference price. Inside Meridian the source is visible; the PDF just shows the unit price.
- Blocking work on existing suppliers that don't have the new details yet. POs can still be created; missing details print as "–".
- Breaking the codebase's guarantees: company scoping, signed workflow context, idempotent submissions, audit before SMTP, and demo orders that can't be emailed.

## Decisions (Claude's calls unless marked as Andre's)

| Area | Decision |
|---|---|
| PDF switch | Andre: boolean on the supplier. Stored as `uses_own_order_form`, default False (Meridian-generated). |
| Supplier-generated email | Andre: option A. You upload their form yourself, and Send is allowed without any attachment. |
| Price fallback | Andre: supplier price, else catalogue reference price. |
| Required supplier fields | Required on the supplier **form** (not in the service, so existing callers and tests keep working): supplier name, registered name, street, city, province, postal code, orders email, payment terms. A VAT number is required when "VAT-registered" is ticked (the default). The SAHPRA licence number and expiry are required when any linked product has `requires_authorisation`. Optional: registration number (sole proprietors), suburb, accounts email, our account number (a new supplier may not have one), contact, phone. |
| VAT number format | SARS format: 10 digits starting with 4, spaces ignored. Applies to the supplier and the practice. |
| VAT on the PO | 15% when the supplier is VAT-registered, otherwise 0%. Saved on the PO as `vat_rate`. VAT is rounded half-up to cents on the subtotal. |
| Reference prices | Treated as excl. VAT, like supplier prices. |
| Expired SAHPRA licence | Not blocked. The supplier card shows an "Licence expired" badge. |
| Incomplete suppliers | The supplier card shows a "Details incomplete" badge when a form-required field is blank. |
| Bill to | New practice settings: bill-to name (entity), billing address, practice VAT number (blank prints "N/A"), pharmacy Y number, accounts email, and **doctor named on orders** (a prescriber in the practice). Their HPCSA number comes from their staff profile. |
| Ship to | Courier collection address and contact (existing settings), plus a new "Delivery instructions for suppliers" field. |
| Authorised by | The doctor named on orders. There's no signature image, only a signature line. |
| PDF columns | Code, description, size, unit price (excl VAT), qty, line total. Received/remaining move out of the PDF (the supplier doesn't need them). The Excel export keeps them and gains price columns. |
| Legacy POs | Created before this change: no prices, so "–" in the price columns and no totals. Supplier box from `supplier_name`/`supplier_email`, and empty bill-to/ship-to boxes. |
| Cold-chain note | Printed only when a line's product `requires_cold_chain`: "Cold-chain products: deliver refrigerated (2–8 °C)." |
| Email recipients | To (1–10), CC (0–10), BCC (0–10). To defaults to the supplier's orders email. |
| Reply-To | The signed-in sender's email, so supplier replies reach a person. |
| Email message | Pre-filled and editable, plain text. If cleared, a one-line fallback is used. |
| Attachments | Up to 5 extra files: PDF, XLSX, XLS, CSV, DOCX, DOC, PNG, JPG/JPEG. At most 10 MB in total including the PO PDF. Not stored; the audit records name, size and SHA-256. After a validation error, uploads must be re-chosen (browsers can't keep them). |
| Preview | Meridian PDF shown inline in an iframe (`?inline=1`, same-origin framing allowed, not audited because the PO holds no patient data and the email audit records the document hash). Uploaded PDFs and images are previewed in the browser via object URLs. |
| Which switch applies | The supplier's **current** `uses_own_order_form` when emailing (a deleted supplier means Meridian PDF). |

## Data added

- `Supplier`: `registered_name`, `registration_number`, `vat_registered` (default True), `vat_number`, `address_street`, `address_suburb`, `address_city`, `address_province` (PROVINCES), `address_postal_code`, `accounts_email`, `payment_terms`, `account_number`, `sahpra_licence_number`, `sahpra_licence_expiry`, `uses_own_order_form`. The existing `email` is relabelled "Orders email".
- `SupplierProduct`: `description`, `pack_size`, `unit_price` (nullable, ≥ 0).
- `PracticeSettings`: `po_bill_to_name`, `po_billing_street/suburb/city/province/postal_code`, `po_vat_number`, `po_pharmacy_number`, `po_accounts_email`, `po_doctor` (FK user, SET_NULL), `po_delivery_instructions`.
- `PurchaseOrder`: `supplier_details` (JSON), `buyer_details` (JSON with `bill_to` and `ship_to`), `vat_rate` (nullable).
- `PurchaseOrderItem`: `unit_price` (nullable), `price_source` (`supplier` / `reference` / blank), `description`, `pack_size`.

## Reference

Approved mockups: [2026-11-23-purchase-order-documents-mockups.html](2026-11-23-purchase-order-documents-mockups.html). Open it in a browser. It shows the PDF, both email pop-up states and the review step. Personal details are placeholders.
