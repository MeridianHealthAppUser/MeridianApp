# Meridian application completion

Reference: local `meridian-health-app-mock-rev2.6__12_09_2026_.html` and the user's later corrections.

Scope confirmed 12 September 2026: complete the in-app functionality without approval pauses. Payment processing and email delivery are excluded. Every primary navigation destination is a separate Django page; the overview remains a summary. No live external services, deployments or transactions are implied by local status changes. Existing data must not be reset.

## Completed foundation

- [x] Two demo practices, shared login identities and practice-scoped roles/data.
- [x] Landing page and anonymous questionnaire → lead only; dummy checkout never creates a patient account.
- [x] Separate staff and patient workspaces; task table, general/patient tasks, assignment and arbitrary tags.
- [x] Secure conversations and mutually accepted appointment-change proposals.
- [x] Schedule calendar/table, doctor working hours, global clinician time off and clash checks.
- [x] Consultation drafts/signing, private PDF blood-test reports and requesting-doctor review tasks. Additive migration 0006; 379 tests passing at this checkpoint.

## Completed implementation phases

- [x] **Clinical and patient continuity** — explicit doctor authorisations/renewals; local subscription management; patient self-booking, stage-2 medical profile/history, Updates, progress chart and calendar export. Signed records are preserved; no automated prescribing or result interpretation.
- [x] **Pharmacy and operations** — editable catalogue; patient basket/order requests with explicit authorisation/quantity checks; stock receipt/ledger/quarantine; safe batch allocation, held/ready shipping, locking/manifest export, manual dispatch/delivery and immutable history.
- [x] **Administration and records** — Super Admin practices/users/roles, self password change, clinical review settings, lead follow-ups, dropout follow-ups, permitted multi-practice directory/timeline views, record exports and personal access history. No email invitations/recovery or automatic lead-to-account conversion.
- [x] **Reporting and remaining account features** — permission-scoped operational metrics/cohorts, activity statements without payment execution, patient consent/preferences/data requests, public policy/contact destinations, manual compounding tracking and explicit review reminders. Unavailable payment/cost inputs are not invented.
- [x] **Integration and verification** — separate desktop/mobile destinations, role/tenant/ownership tests, immutable history and PostgreSQL booking-race checks, end-to-end browser journeys on cloned databases, additive migration verification, Docker/GitHub CI/DigitalOcean configuration and user/deployment guides.
- [x] **Final visual refinement** — compact overview metric cards, naturally sized summary panels, clearer labels, grouped independently scrolling desktop navigation, consistent tables/forms/buttons and responsive layouts. Public landing and questionnaire styling is isolated from workspace refinements.
- [x] **Native video consultations** — private appointment-bound WebRTC rooms, explicit device joining, microphone/camera/screen controls, in-app invitations, reconnect handling, shared Redis presence and temporary TURN credentials. Real two-browser media and local relay-only calls verified; external HTTPS/Redis/TURN deployment remains separate.
- [x] **Shared account menu** — a single top-right avatar/name/role menu, a dedicated own-profile page, read-only practice access, password changes, personal history/preferences and CSRF-protected sign-out. Duplicate staff Account sidebar links are removed; patient care account details remain separate from shared login details.

The current route map and practical workflows are in [USER_GUIDE.md](USER_GUIDE.md). Deployment preparation and remaining live-use prerequisites are in [DEPLOYMENT.md](DEPLOYMENT.md). Verification results are recorded in [VERIFICATION.md](VERIFICATION.md).

## Working boundaries

- Preserve the three requested staff roles and two existing demo practices. A practice Super Admin is not Django's site-wide superuser.
- Practice Administrators retain Leads access as explicitly requested, despite the prototype's inconsistent role matrix.
- Cross-practice views may combine only records the signed-in user is authorised to read. Actions always target a specific practice and recheck permissions.
- A patient never gains an account by merely submitting a questionnaire. Since payment functionality is excluded, that existing checkout remains a preview; staff-user administration does not convert leads.
- Do not collect real card/bank details, mark money paid, send emails, dispense externally or contact couriers. Local orders/subscriptions/dispatch logs are auditable in-app records.
- Generated prescribing documents, automatic clinical decisions and live clinical deployment require separately validated governance. The prototype is not clinical or legal authority.

The implementation checklist covers local in-app workflows, not live integrations, medical approval or a completed external deployment.
