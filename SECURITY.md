# Security

## Reporting

Email **security@massingcloud.com** with what you found and how to reproduce it.
Please do not open a public issue for anything exploitable.

You will get an acknowledgement within three working days and an assessment
within ten. If we disagree that something is a vulnerability we will say so and
why, rather than letting the report go quiet.

## Supported versions

`0.x` is alpha. Fixes land on `main`; there are no backports yet. When `1.0`
ships this section will name a supported window.

## What this software handles

Construction schedules. That sounds low-stakes until you consider what one
contains: a contractor's sequencing and production rates, procurement lead
times, subcontractor names, and — on a project in dispute — the evidential
record of who caused a delay and what it cost.

Two consequences shape the design:

- **A schedule is commercially sensitive between tenants on the same install.**
  One contractor learning a competitor's float is a real harm, not a
  hypothetical one.
- **A schedule may become evidence.** Baselines and audit rows are append-only,
  and a delay attribution that does not sum to the movement it explains is
  reported as broken rather than rounded into agreement.

## What is in place

**Tenant isolation.** Every domain row carries `organization_id`. Reads go
through `services/repository.scoped`, which returns an impossible filter rather
than every row when no organisation is active — it fails closed. A cross-tenant
read returns **404, not 403**: "this exists but is not yours" confirms an id is
real, and a sequential scan then maps a competitor's portfolio.

**Passwords.** argon2id, `time_cost=3`, `memory_cost=64MB`, `parallelism=2`.
Minimum twelve characters and no composition rules — forced symbols produce
`P@ssw0rd!` and a sticky note. Lock-out after eight consecutive failures.
Sign-in burns a hash on an unknown address so response time does not become an
account-enumeration oracle, and every failure returns the same message except a
lock-out, which says so.

**Sessions.** `HttpOnly`, `SameSite=Lax`, `Secure` in production, twelve-hour
lifetime, and the session id rotates on sign-in so a session fixed beforehand is
not valid afterwards.

**API keys.** 32 bytes of `secrets.token_urlsafe`, SHA-256 at rest, shown once.
A visible 12-character prefix identifies a key for revocation without anyone
pasting the secret back in. Revoked, never deleted.

**CSRF.** Global on the session-authenticated surface. The JSON API is exempt
because it authenticates with a bearer key, which a browser never sends
ambiently — a token there would protect nothing.

**Headers.** `default-src 'self'` with no CDN anywhere; `frame-ancestors 'none'`;
`nosniff`; `X-Frame-Options: DENY`; `Referrer-Policy: no-referrer`; HSTS in
production only, because sending it from a plain-HTTP dev server teaches the
browser to refuse that server afterwards.

**Errors.** An unhandled exception logs the cause and returns a request id. A
stack trace in a response tells an attacker the framework, the file layout and
often a query.

**Logs.** JSON, with `password`, `api_key`, `authorization`, `cookie`, `secret`,
`token` and `key_hash` redacted by key. A log carrying the credential whose use
it recorded is a second copy of the credential, in the place with the loosest
access controls.

**Audit.** Append-only, actor by id, and a test asserts no audit row ever
contains a usable key or a password.

**Supply chain.** Dependencies carry upper bounds. CI runs `pip-audit`, emits a
CycloneDX SBOM, and runs CodeQL with `security-and-quality`. The container base
image is pinned by digest, not by tag.

**Container.** Non-root (uid 10001), no compiler in the runtime stage, refuses
to boot in production without `MASSINGPLAN_SECRET_KEY`, and a CI step asserts
that refusal.

## What is not in place yet

Named honestly, because a security document that lists only strengths is a
marketing document.

- **No MFA.** Password plus session only.
- **No SSO.** The OIDC adapter seam exists; its implementation does not.
- **No rate limiting on the HTTP surface.** Account lock-out covers credential
  stuffing; it does not cover a scripted flood of expensive scheduling requests.
  Put a rate limit at your ingress until this lands.
- **No encryption at rest for schedule content** beyond whatever the database
  and the disk provide.
- **No signed webhooks in use.** The HMAC primitives exist in `security.py` and
  nothing calls them yet.
- **No penetration test.** Nobody outside the project has tried to break it.

## Out of scope

- Attacks needing an already-compromised host or database.
- Denial of service by uploading a pathologically large schedule. Bound the
  upload at your ingress; `MASSINGPLAN_MAX_UPLOAD_BYTES` defaults to 16MB.
- Social engineering of your users.
