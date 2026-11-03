# Courier Guy tracking webhook

The app accepts Courier Guy **Tracking event** callbacks at:

```text
POST https://meridianweightlossapp-ujlkh.ondigitalocean.app/courier/webhooks/<TCG_WEBHOOK_TOKEN>/
Content-Type: application/json
```

Set `TCG_WEBHOOK_TOKEN` in the app's environment to a long random secret. For example, generate one with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and save it as a secret environment variable in
DigitalOcean App Platform. Replace the placeholder in the URL with that exact value. Keep the full URL private;
rotate the secret and update the registered URL if it is exposed. The endpoint returns 404 when the secret is
missing or does not match. It does not require a staff login or browser CSRF token.

Deploy the code and run migrations, including `0044_shipment_courier_status_updated_at`. In The Courier Guy portal,
open **Integrations → Webhook subscriptions**, add the **Shipment tracking event** topic, paste the full delivery URL, and enable
the subscription. The tracking endpoint works independently of `TCG_API_KEY`; that key is needed to book waybills
from Meridian. Authentication is the secret in the delivery URL; this endpoint does not validate a separate Bearer
header. The provider's [webhook setup and example payload](https://thecourierguy.co.za/wp-content/uploads/2025/04/Wordpress-Plugin-V4.pdf)
are in its account settings guide.

## Request

A single object or an array of tracking events is accepted. The array format follows the provider's example:

```json
[
  {
    "shipment_id": 640079,
    "short_tracking_reference": "KPD7P",
    "status": "delivered",
    "shipment_time_modified": "2026-11-02T10:35:00Z",
    "shipment_delivered_date": "2026-11-02T10:30:00Z"
  }
]
```

At least one of `shipment_id` or `short_tracking_reference` is required. The app matches the stored Courier Guy
shipment ID or waybill. A manually dispatched parcel can also match its recorded tracking reference. If both
identifiers are supplied, they must agree with the stored booking. Ambiguous or conflicting matches are ignored
rather than choosing a patient. Valid events for other systems' parcels are acknowledged and ignored.

`status` is required; new provider codes are accepted as lowercase words separated by hyphens. Examples include
`collected`, `at-hub`, `in-transit`, `out-for-delivery`, `delivery-failed-attempt`, `delivered`, and
`returned-to-sender`. Other provider fields may be present and are ignored. Dates, when supplied, must be ISO 8601
datetimes; use UTC with `Z` or an explicit offset. A timestamp without an offset is interpreted as UTC. Legacy events without timestamps are accepted, but provide
`shipment_time_modified` so older events cannot overwrite newer tracking.

## What it updates

- The linked parcel's courier status appears in Weekly shipping, Dispatch history and the shipment record. Status
  changes are audited, with the provider's event time when available.
- `delivered` marks an **already dispatched** parcel Delivered, using `shipment_delivered_date`, then
  `shipment_time_modified`, or the receipt time as a fallback. It adds one delivery update to the patient's timeline.
- Repeated deliveries do not create duplicate delivery updates or change the original delivery time. Older events
  do not replace newer tracking. Undated events cannot regress a delivered parcel. A newer return event can update
  courier tracking while preserving the historical delivery record.
- A webhook never records dispatch, deducts stock, or changes the ongoing subscription's Active/Paused/Cancelled
  status. Staff still confirm physical collection through Weekly shipping. If a delivery event arrives before that,
  it records courier tracking; a repeated delivered callback after dispatch can complete the delivery state.

## Responses and verification

| HTTP status | Meaning |
| --- | --- |
| 200 | Accepted. Plain-text body is the number of matched events, e.g. `1`. Duplicate/older matched events are included. `0` means no matching parcel. |
| 400 | Invalid JSON, event shape, identifiers, status or date. The batch is not partially applied. |
| 404 | Secret is absent or incorrect. |
| 405 | Request method is not POST. |

To verify, send a tracking event for a test parcel with its exact stored waybill, then open its shipment record or
Dispatch history. Repeat a delivered event and confirm that its delivery time and stock balance remain unchanged.
Do not send a delivered test callback for a real patient's parcel unless that parcel was actually delivered.
