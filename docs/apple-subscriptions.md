# Nudge Apple subscriptions: backend and Swift contract

Implemented against Apple App Store Server Library for Python 3.1.2. Apple is the source of truth. The backend never accepts an `is_subscribed` value as proof of access. All paths below are relative to the API origin. Authenticated requests require `Authorization: Bearer <access_token>`; refresh tokens are rejected as access tokens.

## Purchase identity and Swift flow

1. Create/sign in to the Nudge account first, including anonymous users (`POST /auth/anonymous`).
2. Read `user.app_account_token` from the auth response, or `app_account_token` from `GET /subscriptions/me`. It is a backend-generated UUID. Do not generate a new UUID per purchase.
3. Purchase using StoreKit 2's `.appAccountToken(token)` option:

```swift
let result = try await product.purchase(options: [.appAccountToken(accountToken)])
// For a verified result, send the VerificationResult's jwsRepresentation
// to the backend. The backend performs its own verification.
```

4. Send the transaction's signed JWS to `POST /subscriptions/apple/verify`.
5. Apply the returned `entitlement` to the UI. Persist/retry verification on transient failure, and handle StoreKit transaction updates, including purchases completed outside the immediate purchase sheet. Finish transactions after successful backend processing.
6. For Restore Purchases, use StoreKit sync when the user explicitly requests restore, enumerate current entitlements, and send each signed transaction to the **same verify endpoint**. Reconciliation alone cannot discover a purchase that has never been linked to this backend.

An original transaction ID has a database uniqueness constraint and one owner. Unlinked purchases must have a signed `appAccountToken` mapped to the authenticated account. Missing tokens on old, unlinked purchases need a separate support/migration policy; they are not assigned to whoever submits them first. An already linked transaction with no token may be restored by its existing owner. Family Sharing is not enabled by this implementation.

## Subscription endpoints

| Method and path | Auth | Request | Response |
| --- | --- | --- | --- |
| `GET /subscriptions/me` | Required | None | Subscription response below, cached verified state |
| `POST /subscriptions/apple/verify` | Required | `signed_transaction` | Same subscription response after Apple status refresh; used for purchases and restores |
| `POST /subscriptions/reconcile` | Required | No body | Same subscription response after refreshing all of this user's linked subscriptions |
| `POST /subscriptions/apple/notifications` | Apple signature | `signedPayload` | `{"status":"processed"}` or `{"status":"duplicate"}` |

Verify purchase/restore request:

```json
{
  "signed_transaction": "<StoreKit transaction JWS>"
}
```

Example subscription response (`200`):

```json
{
  "app_account_token": "5c54af77-c212-4e1e-90e5-295e62478211",
  "entitlement": {
    "is_subscribed": true,
    "status": "active",
    "product_id": "com.example.nudge.monthly",
    "expires_at": "2026-10-10T00:00:00Z",
    "auto_renew": false,
    "grace_period_expires_at": null,
    "revoked_at": null,
    "last_verified_at": "2026-09-10T00:00:00Z"
  },
  "free_ai_deck_available": true
}
```

The example has auto-renew canceled but retains paid access until expiration. A subscriber can still have an unused free allowance; paid deck creation does not consume it. All dates are ISO 8601 UTC, nullable as indicated. UUIDs are strings.

Entitlement states:

| `status` | Paid access |
| --- | --- |
| `none` | No verified subscription |
| `active` | Yes, strictly before `expires_at`, with sufficiently fresh verification |
| `grace_period` | Yes, strictly before Apple's `grace_period_expires_at`, with fresh verification |
| `billing_retry` | No; billing retry without a verified grace period does not grant access |
| `expired` | No; expiration is enforced against the current clock even if no notification arrived |
| `revoked` | No; revocations/refunds remove access when verified |
| `verification_required` | No; cached verification is older than the configured freshness limit |

`is_subscribed` is the access decision. Do not infer access from `auto_renew` or `product_id`. `expires_at` stays the paid-period expiration during grace; use `grace_period_expires_at` for the grace deadline. With multiple linked subscriptions, the response selects an entitled subscription when available, otherwise the most recent expiration. The entitlement itself is not stored as a client-editable flag.

## Auth and anonymous accounts

Every returned user now has the following fields:

```json
{
  "id": "68723efa-e2e6-412f-ad9e-18d17b6c19af",
  "name": "Alex",
  "email": "alex@example.com",
  "created_at": "2026-09-10T00:00:00Z",
  "is_anonymous": false,
  "app_account_token": "5c54af77-c212-4e1e-90e5-295e62478211",
  "free_ai_deck_available": false,
  "entitlement": {
    "is_subscribed": false,
    "status": "none",
    "product_id": null,
    "expires_at": null,
    "auto_renew": false,
    "grace_period_expires_at": null,
    "revoked_at": null,
    "last_verified_at": null
  }
}
```

`POST /auth/anonymous`, `/auth/login`, `/auth/refresh`, `/auth/upgrade`, and `/auth/merge` return:

```json
{
  "user": {
    "id": "68723efa-e2e6-412f-ad9e-18d17b6c19af",
    "name": "Alex",
    "email": "alex@example.com",
    "created_at": "2026-09-10T00:00:00Z",
    "is_anonymous": false,
    "app_account_token": "5c54af77-c212-4e1e-90e5-295e62478211",
    "free_ai_deck_available": false,
    "entitlement": {
      "is_subscribed": false,
      "status": "none",
      "product_id": null,
      "expires_at": null,
      "auto_renew": false,
      "grace_period_expires_at": null,
      "revoked_at": null,
      "last_verified_at": null
    }
  },
  "access_token": "<JWT>",
  "refresh_token": "<JWT>",
  "token_type": "bearer"
}
```

`POST /auth/register`, `GET /auth/me`, and `PUT /auth/me` return the user object directly. Login and refresh retain their existing token fields and now also include `user`.

**Upgrade a guest to a new permanent account:** authenticated `POST /auth/upgrade`:

```json
{
  "name": "Alex",
  "email": "alex@example.com",
  "password": "<at least 8 characters; at most 72 UTF-8 bytes>"
}
```

The user ID, purchase token, subscriptions, decks and usage history remain unchanged. Existing emails produce `409 email_already_registered`. `PUT /auth/me` cannot set an anonymous user's email; use upgrade to set credentials together.

**Explicitly merge a guest into an existing permanent account:** authenticate as the guest and `POST /auth/merge`:

```json
{
  "email": "existing@example.com",
  "password": "<destination account password>"
}
```

The bearer token proves control of the source guest; the password proves control of the destination. Decks, study materials, exam attempts/progression, subscriptions, and AI request history move atomically. The destination allowance is marked used if either account used it. The old purchase token remains a server-side alias for future Apple renewals/restores. New purchases use the destination's returned token. The guest is removed, so its old access/refresh tokens no longer work. Replace the Swift session with the returned destination tokens.

Ordinary `/auth/login` never merges or transfers anything. Permanent-to-permanent merging is unsupported. Present the explicit merge operation as a user decision; don't call it automatically on an ownership conflict. A failed merge rolls back all changes.

## First free AI deck and paid generation

`POST /ai/decks/generate` now **requires a UUID `Idempotency-Key` header**. Persist it for the logical creation request, including network retries. Use a new key only for a new intended deck. It is scoped to the backend account and retained across explicit merges.

```http
POST /ai/decks/generate
Authorization: Bearer <access_token>
Idempotency-Key: 44d6a0ee-3b79-4bde-aad9-14b3d8e645ab
Content-Type: application/json
```

```json
{
  "plan": {
    "title": "Cell Biology",
    "subject": "Biology",
    "education_level": "High school",
    "learning_language": "English",
    "chapters": [
      {
        "title": "Cell Structure",
        "description": "Learn the parts of a cell",
        "key_concepts": ["Nucleus", "Membrane"],
        "card_count": 10
      }
    ]
  },
  "study_purpose": "General Learning",
  "target_date": null
}
```

Successful response remains `200` with the existing deck/timeline structure:

```json
{
  "deck": {
    "id": "2390e608-a0f7-44e8-a5c7-373604ce1c09",
    "title": "Cell Biology",
    "subject": "Biology",
    "education_level": "High school",
    "learning_language": "English",
    "generation_status": "generating",
    "chapters": [
      {
        "id": "a8aa3310-8715-423a-b3dc-b9d224e10680",
        "title": "Cell Structure",
        "generation_status": "pending"
      }
    ]
  },
  "timeline": null
}
```

The allowance, admission receipt, parent/chapters, and job are committed together under an account row lock and a conditional allowance update. Same key/body returns the original response; use `GET /decks/{id}/generation-status` for current progress. Same key/different body returns `409`. Concurrent different keys can admit only one free deck. Deleting the deck never resets the allowance; repeating its key returns `410`.

Failure policy: validation or rollback before admission consumes nothing. Once a job is accepted, the allowance stays used even if generation fails or the deck is deleted. `POST /ai/decks/{deck_id}/retry` (no body) resumes the **same admitted job** without requiring a new subscription, including jobs accepted while subscribed. Pending/running/completed retries are no-ops; only failed jobs are requeued. Old jobs without admission receipts require a subscription for retry. Generation uses the saved plan. AI-managed `parent_deck_id`, `position`, `key_concepts`, `card_count`, and `generation_status` cannot be rewritten through deck CRUD, and extra chapter membership cannot be added to admitted decks (`409 generated_deck_structure_locked`). Rename/favorite remain available. Worker advisory locks prevent overlapping stale-job recovery from generating the same job twice.

New generation requests allow 1–20 chapters and each chapter requests 1–100 cards. Previously admitted jobs keep their saved plans.

Backend enforcement:

- `POST /ai/decks/plan`: subscription or unused free-deck allowance. Planning previews do not consume the deck allowance.
- `POST /ai/decks/generate`: subscription or atomic one-time allowance.
- `POST /ai/decks/{id}/retry`: admitted request ownership, otherwise subscription for legacy jobs.
- `POST /decks/{id}/exams/{exam_type}/generate`: subscription required before a new AI call. Already persisted exam questions can be returned without paying again.
- Existing study content, manual deck/card CRUD, exam status, exam submission, and study-material upload do not require a subscription.

Accepted jobs may finish after subscription expiration; they were authorized at admission. The worker does not independently grant new jobs. The free allowance is per backend account, not per device or Apple ID; anonymous-account creation abuse/rate limiting is a separate deployment concern.

## Errors

New subscription/access/account errors use:

```json
{
  "detail": {
    "code": "subscription_owned_by_another_account",
    "message": "This subscription belongs to another Nudge account."
  }
}
```

| HTTP | Code | Swift handling |
| --- | --- | --- |
| 400 | `invalid_apple_signature`, `invalid_apple_transaction`, `invalid_apple_renewal`, `invalid_apple_notification` | Reject invalid purchase data; never unlock locally |
| 400 | `unsupported_product`, `family_sharing_unsupported` | Product/configuration or unsupported purchase type |
| 401 | `invalid_credentials`, `account_unavailable` | Authenticate again; check destination credentials for merge |
| 402 | `subscription_required`, `free_deck_already_used` | Show subscription purchase flow |
| 409 | `subscription_owned_by_another_account` | Sign in to the owning Nudge account; no automatic reassignment |
| 409 | `app_account_token_mismatch` | Wrong/missing/unmapped purchase token; use the owning account or support |
| 409 | `email_already_registered`, `not_anonymous`, `upgrade_required` | Use the appropriate explicit account flow |
| 409 | `merge_idempotency_conflict` | Support intervention; merge did not run |
| 409 | `idempotency_conflict` | Retry with the original body, or use a new key for a new purchase-independent generation intent |
| 409 | `generated_deck_structure_locked` | Do not edit server-managed generation metadata |
| 410 | `generation_deleted` | Original admitted deck was deleted; allowance remains used |
| 422 | FastAPI validation array | Missing/invalid header or request fields; fix request |
| 503 | `apple_not_configured` | Backend setup incomplete |
| 503 | `apple_unavailable`, `apple_status_missing`, `invalid_apple_response` | Retry with backoff; no new access granted |

Existing auth errors still may have string `detail`, and request validation uses FastAPI's array format. Handle those alongside the structured new errors. Unknown fields are forbidden in purchase verification requests.

## Notifications and reconciliation operations

Configure App Store Server Notifications **V2**, HTTPS URL `/subscriptions/apple/notifications`. The envelope is:

```json
{"signedPayload":"<Apple notification JWS>"}
```

The official verifier validates the certificate chain, signature, bundle ID, app Apple ID in production, and configured environment. Nested transaction/renewal JWS are also verified and validated. For an owned subscription, every transaction-bearing notification retrieves current status from Apple's `get_all_subscription_statuses` API. This handles renewals, auto-renew changes, grace, billing retry, expiration, refunds, revocation, and refund reversals without trusting notification arrival order. Only a successfully processed UUID is committed; failures return non-2xx so Apple can retry. Duplicate UUIDs return `200` without repeating processing. Unsupported/non-transaction lifecycle events are acknowledged without granting access. Verified unlinked purchases with a recognized account token can be associated; an unknown token event is recorded without granting access, and later restore can retrieve current status.

Run `python -m app.reconcile_subscriptions` **at least hourly** in the deployment scheduler. It paginates known owners, refreshes all statuses per account, commits independently, logs failed account IDs without signed payloads/secrets, and exits nonzero if any fail. Monitor failures. No recurring desktop automation was created. Auth/user reads return cached state; paid generation reconciles stale state synchronously. Default freshness is 3600 seconds. If Apple is unavailable beyond that bound, paid requests fail closed with `503`; cached auth reads report `verification_required` and `is_subscribed: false`. Expiration/revocation never gets extended by an Apple outage.

## Required Apple/deployment configuration

Install dependencies (`pip install -r requirements.txt`) and run `alembic upgrade head` before deploying the new app and worker. Migration `e6a9b2c4d7f0` creates subscription/event/token-alias/admission tables and adds user identity/allowance fields. Existing users receive their user UUID as their initial purchase token. Existing anonymous-email patterns are backfilled, and any existing AI generation job counts as prior usage. Already deleted pre-migration jobs cannot be reconstructed; manually audit historical usage if needed.

Set these environment variables (list settings are JSON arrays):

```dotenv
APPLE_BUNDLE_ID=com.example.nudge
APPLE_APP_ID=1234567890
APPLE_ENVIRONMENT=production
APPLE_PRODUCT_IDS=["com.example.nudge.monthly","com.example.nudge.yearly"]
APPLE_ROOT_CERTIFICATE_PATHS=["/run/secrets/AppleRootCA-G3.cer","/run/secrets/AppleRootCA-G2.cer"]
APPLE_PRIVATE_KEY_PATH=/run/secrets/SubscriptionKey_XXXXXXXXXX.p8
APPLE_KEY_ID=XXXXXXXXXX
APPLE_ISSUER_ID=00000000-0000-0000-0000-000000000000
SUBSCRIPTION_MAX_STALENESS_SECONDS=3600
```

Values shown are placeholders, not actual Nudge configuration. Use the app's actual bundle identity, including the existing identity if the Memora-to-Nudge rename did not change it. Download the appropriate Apple root certificates as DER files from Apple PKI; do not accept roots from clients. Obtain the In-App Purchase key, issuer ID and key ID in App Store Connect. Mount secrets on the server; do not commit them. Online certificate checks are enabled and require outbound access to Apple's certificate services and App Store Server API.

`APPLE_ENVIRONMENT` accepts only `production` or `sandbox`. One deployment uses one environment; production never falls back to sandbox. Use a separate sandbox backend/database for StoreKit sandbox/TestFlight testing. `APPLE_APP_ID` is mandatory for production. Local Xcode StoreKit test signatures are deliberately rejected. Allowlisted product IDs must be auto-renewable subscriptions. Configure Apple billing grace period if desired; only a verified Apple grace period grants grace access.

Remaining product/deployment choices: real bundle/app/product identifiers and Apple credentials; grace-period duration in App Store Connect; confirmation of the paid exam-generation policy; preview rate limits/anonymous signup abuse controls; support process for legacy purchases without appAccountToken and permanent-account ownership disputes; actual scheduler/monitor deployment. The implemented failure-retention policy and 1–20 chapter/1–100 card limits are explicit defaults that product can revise.

## Validation

Offline suite: `.venv/bin/python -m unittest discover -s tests -v`.

PostgreSQL concurrency/backfill suite: set `TEST_DATABASE_URL` to a disposable PostgreSQL database and run `.venv/bin/python -m unittest discover -s tests -p test_subscription_postgres.py -v`. Tests use their own random schemas and remove only those schemas.

Apple network responses are simulated in automated tests; invalid JWS is tested through the real Apple verifier. A real signed sandbox purchase, restore, renewal, refund, notification test delivery, and production key/app identity smoke test still require your App Store Connect configuration. No live purchase or production database was modified during implementation.

Sources: [Apple server library and setup](https://github.com/apple/app-store-server-library-python), [SignedDataVerifier](https://apple.github.io/app-store-server-library-python/appstoreserverlibrary.signed_data_verifier.html), [App Store Server API client](https://apple.github.io/app-store-server-library-python/appstoreserverlibrary.api_client.html).

Implementation verification completed: all 32 tests passed, including four PostgreSQL tests (different-key concurrency, same-key concurrency, migration backfill, and overlapping-worker exclusion). The full migration chain also applied successfully to an isolated PostgreSQL database, and FastAPI OpenAPI generation passed.
