# Local Operations demo

The local app has an isolated set of ten fictional patients, two doctors, three demo medication products,
a non-prescription product, stock receipts, subscriptions, invoices/payments and linked shipments.
The scenario records use `MH-OPS-DEMO-` record numbers, `Demo` patient names and `.demo@example.test` logins.

## Sign in

Open <http://127.0.0.1:8000/accounts/login/>. All these local demo accounts use `MeridianDemo!2026`.

| Role | Email |
| --- | --- |
| Practice administrator | lindiwe.mahlangu@meridianhealth.co.za |
| Doctor Maya | dr.maya.demo@example.test |
| Doctor Lebo | dr.lebo.demo@example.test |
| Patient Emma | emma.demo@example.test |

Other patient logins follow the same pattern: `liam.demo@example.test`, `zola.demo@example.test`,
`sara.demo@example.test`, `noah.demo@example.test`, `ava.demo@example.test`, `leo.demo@example.test`,
`mia.demo@example.test`, `nia.demo@example.test`, and `ben.demo@example.test`.
Each doctor has five assigned demo patients. Existing accounts and records have been kept.

## Scenarios seeded on 2 November 2026

Open [Weekly shipping for 2–8 November](http://127.0.0.1:8000/shipping/?week=2026-11-02).
The practice's shipping day is Tuesday 3 November.

The workflow tabs filter the table:

- **Pending**: draft, held and previously prepared parcels awaiting confirmation. Select parcels and use
  **Confirm & lock** to check prescription, active subscription, medication/dose/allowance, delivery details
  and required payment together. Passing parcels reserve stock; a shortage warns but does not prevent confirmation.
  The information icon beside the action contains this checklist.
- **Confirmed & Locked**: stock is reserved, but courier booking is still needed. **Book courier** uses
  the configured courier connection. A waybill or tracking reference moves a parcel into Tracking.
- **Tracking**: booked parcels awaiting collection, dispatched parcels and delivered parcels. Tracking
  shows the latest courier update in its **Shipping status** column, with the reference in **Waybill**. **Record collection**
  remains available for booked parcels to record physical dispatch and deduct stock once; a negative stock balance
  at dispatch requires an explicit override and reason.

**Due date** is the planned shipping date, followed by **Waybill** in both Customers and Medication views.
Pending and Confirmed & Locked omit the redundant workflow Status column. Pending shows an amber warning icon
beside the patient name when prescription, address, payment or held-parcel issues need attention; hover, focus or
click to read them. Tracking has a dedicated **Shipping status** column. All shipments retains the workflow Status. Use **All shipments** beside Filters to see the whole week. Product, Doctor, Payment and
Status filters combine with the selected stage and carry across views. Viewing a tab never changes parcel data.

| Patient | Starting scenario | What to try |
| --- | --- | --- |
| Emma | Paid prescription parcel in Draft | Select it and Confirm & lock. |
| Liam | Paid Ready prescription parcel; paid creatine request in Draft | Compare Customers and Medication views; confirm and lock the creatine parcel. |
| Zola | Paid, locked parcel with a synthetic collection reference | Record collection; the medication stock reduces by one. |
| Sara and Noah | Paid, locked Tirzepatide parcels | Inspect reservations and shortage; receive stock in the Inventory modal. |
| Ava | Ready parcel with a failed sandbox collection and a retry | Check the payment block or settle the test payment from Billing. |
| Leo | Paid Ready parcel with an incomplete delivery contact | Add the delivery contact number, then Confirm & lock. |
| Mia | Paused subscription and Held future parcels | Open My Treatment as Mia to resume, then review and confirm the held parcel. |
| Nia | Delivered history and future scheduled parcels | Inspect supply progress and Dispatch history. |
| Ben | In-transit history and future scheduled parcels | Open Dispatch history and inspect courier tracking. |

Stock starts at 20 Semaglutide 1.0 mg demo pens, 8 Semaglutide 2.0 mg demo pens, 1 Tirzepatide demo pen
and 25 creatine boxes. Tirzepatide has two reservations, so its available balance starts at **−1**.
Emma also has an unpaid non-prescription creatine request, and Liam has a paid accepted one.
Prescription supplies recur every four weeks, so later weeks have future parcels to inspect.

Zola's synthetic waybill is intended for testing Record collection. Holding or cancelling a courier-booked
parcel invokes the real booking-cancellation protection; the synthetic reference is not a real courier booking.

## Purchasing, Inventory and Operations

**Purchasing**, **Inventory** and **Operations** are separate navigation sections.

Purchasing has **To order**, **On order** and **Purchase history** tabs. To order shows active products using
`max(0, ceil(AWC × lead time in weeks + safety stock − on hand − on order))`. It uses physical on-hand stock;
customer reservations are not incoming supplier stock. Search, category/order-status filters, 25-row pagination
and Excel downloads share the same results. Products missing lead time or safety stock link to Inventory to
complete their settings. Outstanding units on real open purchase orders are subtracted from the suggestion.

Super Admin has a **Suppliers** page under Purchasing. Add contact details and link the products each supplier
provides, with that supplier's product code. Suppliers can be searched, edited, deleted and exported to Excel.
Deleting a supplier leaves the products, stock and saved purchase-order details intact.

The current local database has two fictional suppliers for testing: **Demo Medical Supplies** supplies all
Semaglutide products and Tirzepatide; **Demo Wellness Wholesale** supplies Creatine and the demo Semaglutide
1.0 mg and 2.0 mg products. Their email addresses are non-deliverable examples. Creatine and Tirzepatide have
no common supplier, so selecting both demonstrates the restriction.

Select products and click **Create Purchase**. The review popup requires a supplier, allows quantity changes
and accepts an optional ETA. Only suppliers that supply every selected product can be chosen; the server
also checks each product link before creating the order.
**Create purchase order** saves a numbered order in **On order**, where expanding it shows its products,
ordered/received/outstanding quantities, supplier product codes and receipt progress. The supplier name,
email and product codes are saved with the order so later supplier edits do not change its record.
Legacy orders without a supplier show **Unassigned**. Placing an order does not add physical stock.

Use **Receive stock** for a partial or full delivery. Enter quantities, batches and expiry dates; use zero for a
product that has not arrived. **Receive all remaining** fills outstanding quantities. Saving records positive
Inventory stock movements with the purchase order reference. A fully received order moves to **Purchase history**;
a partial order stays **On order**. History can be viewed by **Purchase orders** or **Products**, each expandable.
Both views retain search, filters, 25-row pagination and Excel downloads.

Every saved purchase order has **Download Excel**, **Download PDF** and **Email purchase order** controls.
The email popup defaults to the saved supplier email, requires a recipient and subject, accepts an optional
message and attaches the PDF. Sending requires the explicit **Send** action. It uses the
configured Django email backend: local development logs messages to the server console when SMTP is not configured.
Signed submissions prevent retries from creating duplicate orders, receipts or known successful email sends.

Inventory keeps live on-hand, available and reserved balances, the Add stock modal and reservation popups.
**AWC** uses a starting value when one is set. Only super admins can edit or clear that value using the AWC pencil.
Clearing it restores average weekly dispatched units over the previous eight completed calendar weeks, including
weeks with zero dispatches. The same effective AWC is used in Inventory, To order and their Excel downloads.
Its info icon explains the calculation. Use the pencil in **Lead time (weeks)** to enter an optional delivery lead time, including fractional
weeks, and **Safety stock** to save a minimum buffer per product. Only Super Admin can edit these settings;
Practice Admin sees their values without editing controls. Zero and unset values are distinct.
Stock movements start collapsed, load when opened, and offer pagination and an Excel download. Inventory and Stock
movements show **Product** and **Category**, matching Catalogue. Inventory and the three Catalogue sections can be
collapsed or expanded; their current expanded defaults are preserved. Its **Catalogue**
tab keeps products, prescribed doses and starting plans together, with their record breadcrumbs returning there.

The **Consumption** tab shows actual dispatched units by product and Monday–Sunday week. Choose 4, 8 or 12 weeks,
search by product/strength/category, or filter by category. The default is the last eight completed weeks; selecting
the current week marks it as in progress. Click any quantity to load its contributing dispatch lines in a paginated
popup, including patient, doctor, actual dispatch date, units and shipment/waybill. The source is dispatched and
delivered shipment lines, counted once by actual dispatch date; stock receipts and reservations do not contribute.

**Averages** compares the last eight completed weeks, this year and all time for every product. Zero-consumption
weeks count. The year average uses units from 1 January through the last completed Sunday divided by elapsed
days / 7; all time starts at each product's first recorded dispatch week. These historical figures do not use the
editable starting AWC used for purchasing. The current incomplete week is excluded from all three averages.
The Excel icon downloads weekly totals, averages with their calculation inputs, and every contributing dispatch
line in the selected range. Each weekly popup also has its own complete Excel download. Pagination never limits exports.

Operations covers subscriptions, weekly shipping, dispatch history and billing. Dispatch history is a paginated
list with 25 rows per page, numbered navigation and optional inclusive **From month** and **To month** filters.
Each Operations, Inventory and To order table offers a **Download Excel** icon beside its count. Downloads contain every
matching row across pages, retain the current search/filters, and preserve typed dates and quantities.

## Creating this dataset again in a fresh local database

For a client walkthrough, prefer the **Demo data** button beside the Super Admin profile. It is available in
development and production and explains the records before creating them. The managed set uses dedicated demo
products and patients without login accounts, preserves existing records, and supports **Delete demo data**
and **Rebuild demo data**. It includes an eight-week consumption history and open/partial/received purchases.
Existing clinicians are reused; without one, prescription examples are skipped. Notifications, financial
transactions and real courier calls are disabled for these managed records. Use **Simulate demo courier**
to move only the managed parcels through the shipping flow. Demo order downloads are labelled as demonstrations.

The older command below creates a different local-only fixture, including sandbox billing and demo accounts.
The header controls deliberately do not delete or replace that older dataset.

After the base Meridian practice and practice administrator exist, run:

```sh
.venv/bin/python manage.py seed_operations_demo
```

This command requires development mode and the sandbox payment provider. It pauses email notifications,
records demo/manual payments and creates synthetic courier references. It does not book a real courier.
A second run leaves the existing dataset and your testing progress unchanged.
