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

**Rate limiting.** Fixed-window, keyed by the authenticated subject where there
is one and by client address otherwise. Sign-in is keyed by address rather than
by the submitted email on purpose: keying by email lets an attacker lock out an
account whose address they merely know. See the limitation below.

**Audit.** Append-only, actor by id, and a test asserts no audit row ever
contains a usable key or a password.

**Supply chain.** Dependencies carry upper bounds. CI runs `pip-audit`, emits a
CycloneDX SBOM, and runs CodeQL with `security-and-quality`. The container base
image is pinned by digest, not by tag.

**Container.** Non-root (uid 10001), no compiler in the runtime stage, refuses
to boot in production without `MASSINGPLAN_SECRET_KEY`, and a CI step asserts
that refusal.

**Exported XER.** Every field written goes through one sanitiser that removes
tabs and newlines. XER is tab-delimited with newline-terminated rows and has no
escape sequence, so a value carrying either does not produce a malformed file —
it produces a **well-formed file with different contents**. An activity named
`Dig\n%R\t99\t1\tEVIL` ended its own row and started another, and the schedule a
planner opened in P6 contained an activity nobody added. Reachable through the
API, where a JSON string carries a newline happily. There is nowhere else to fix
it: a reader cannot tell a forged row from a real one, because by then they are
the same thing.

The adversarial suite covered this and passed throughout, because its fixture
put the newline in the *uploaded file* — where it is a row separator before the
reader sees it, so no field ever held one. The test exercised the wrong layer,
and now exercises the writer directly.

**Uploaded XML.** Both XML readers go through `core/xmlsafe.py`, which refuses a
document type declaration defining entities before anything is parsed. This was
a real hole rather than a theoretical one: `xml.etree` does not resolve external
entities, but it expands internal ones, and a 448-byte document with four levels
of nested definitions was measured expanding to 30,000 characters — each further
level multiplying by ten. The reader previously carried a note saying the
application layer hardened untrusted uploads; it did not, and the only control
was a byte limit that a payload this small passes trivially. `defusedxml` is the
usual answer and `core` cannot have it, being pure standard library by contract,
so the check is built from `re` and applied to the prologue. Neither Primavera
nor MS Project writes an entity declaration, so it refuses attacks and not
files.

## What is not in place yet

Named honestly, because a security document that lists only strengths is a
marketing document.

- **MFA is opt-in and off by default**, and unavailable at all without
  `pip install 'massingplan[mfa]'` plus a `MASSINGPLAN_ENCRYPTION_KEY`. Where it
  is on: TOTP, secrets encrypted at rest with Fernet, hashed single-use recovery
  codes, no reachable state between the two factors, replay refused inside the
  drift window, and ten attempts per fifteen minutes. **API keys bypass it by
  design** — a CI job cannot type a code, so the factor protects the interactive
  session and not the machine credential.
- **SSO is OIDC authorization-code with PKCE, and off unless all four settings
  are present.** `MASSINGPLAN_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET` and
  `_REDIRECT_URI`; a partial configuration offers no button rather than a
  button that fails at the issuer. The id_token is verified against the
  issuer's JWKS by `kid` with **asymmetric algorithms only** — there is no HMAC
  code path to confuse with an RSA public key, and no path where the header's
  `alg` decides *whether* to verify. `iss`, `aud`, `azp` when there are several
  audiences, `exp`, `iat` and the session's `nonce` are all checked, and the
  discovery document must name the issuer it was fetched from.

  What it deliberately does not do: no refresh tokens, no userinfo call, no
  back-channel logout, and **no role or group mapping** — a `roles` claim is
  the IdP's opinion about a different system. A first sign-in provisions into a
  **new** organisation; joining an existing one is by invitation, for the same
  reason self-service registration was changed. Users are matched on
  `issuer#sub`, never on email, because an address at an IdP is reallocatable.

  Client authentication at the token endpoint follows the discovery document's
  `token_endpoint_auth_methods_supported` — `client_secret_basic` preferred,
  `client_secret_post` where that is all the issuer accepts, and an explicit
  refusal naming both lists when it accepts neither. `private_key_jwt` and
  `tls_client_auth` are not implemented.

  **It has not been tested against a commercial IdP,** and that gap is narrower
  than it was rather than closed. `tests/test_oidc_interop.py` builds the fake
  issuer into the other shapes the specification allows — POST-only client
  authentication, ES256 signing, a JWKS carrying both sides of a rotation, a
  JWKS carrying encryption keys — because those, not the cryptography, are what
  a first run against Entra or Okta usually breaks on. What remains untested is
  everything a specific vendor does that the specification does not describe.
- **Rate limiting is per-process by default, and need not be.** The in-memory
  store is correct for exactly one worker and wrong by a factor of N for N
  workers — a limiter that silently multiplies is worse than none, because it
  is believed. The app still logs a warning at startup when it detects more
  than one worker, and `massingplan check` still prints the scope.

  **`MASSINGPLAN_RATE_LIMIT_STORE=database` shares one counter** across every
  worker and replica pointed at the same database, which makes the configured
  limit the real one. It needs migration `0009` and a database that can take a
  small write per limited request. Three things make it actually shared rather
  than nominally: the increment is a single `INSERT ... ON CONFLICT DO UPDATE`
  so two workers cannot both read 19 and both write 20; the window is keyed on
  **wall-clock** seconds, because `time.monotonic()` has a per-process origin
  and workers using it would place one instant in different windows and keep
  separate counts; and the counter commits in its own transaction, so a failed
  sign-in still counts after its request rolls back.

  An ingress limit remains the better control where you have one — it stops the
  traffic before it reaches Python — but "put one at your ingress" is not an
  answer for a deployment that has no ingress to put it at.
- **No encryption at rest for schedule content** beyond whatever the database
  and the disk provide. Only TOTP secrets are encrypted, deliberately: an
  attacker who can read the database holds the key too, so column encryption
  buys nothing there. What it protects is the narrower and realer case of a
  leaked backup, where the key stays in the environment and does not travel with
  the dump — which is only true if you store the two apart.
- **Webhooks carry a residual DNS-rebinding window.** Every URL is vetted
  against its *resolved addresses* at subscribe time and again before each
  delivery — loopback, private, link-local (the cloud metadata service),
  carrier-grade NAT and reserved ranges are refused, on every address a name
  returns rather than the first; the scheme is an allow-list; credentials in the
  URL are refused; redirects are not followed; and the connection is pinned to
  the address that was vetted rather than resolving a second time. What remains
  is the gap between `getaddrinfo` and `connect` on that pinned address, which
  pinning narrows but does not close. Egress filtering at the network is the
  control that does close it.
- **Password hashing is bounded twice, and it needs to be.** `auth.sign_in`
  runs argon2id at 64MiB, which makes it the most expensive thing the app does
  per request — measured at 35 seconds on a memory-pressured machine. The rate
  limit bounds attempts *per fifteen minutes*; it does not bound attempts
  *arriving together*, and twenty simultaneous ones all pass a limit of twenty
  and then all allocate. `accounts.MAX_CONCURRENT_HASHES` (default 4,
  `MASSINGPLAN_MAX_CONCURRENT_HASHES`) bounds hashes in flight, so the excess
  queues instead of exhausting memory. Raise it only if you have sized the box:
  the ceiling is roughly that number times 64MiB.
- **Load tested, in one specific sense.** `tests/test_load.py` runs its own CI
  job against a real threaded WSGI server over real sockets: nothing 5xxs under
  eight concurrent clients, two tenants hammering the same endpoints never see
  each other's data, `/healthz` still answers while the app is busy, and
  password hashes stay bounded in flight. That is a *correctness-under-
  concurrency* test. It is **not** a capacity benchmark: it does not tell you
  requests per second, it was not run against your database or your hardware,
  and it asserts no latency threshold — a wall-clock ratio was tried, measured
  a noise floor around 4x, and was removed rather than shipped as a number that
  would either flap or catch nothing. Size your deployment by measuring it.
- **No penetration test.** Nobody outside the project has tried to break it.
  `tests/test_adversarial.py` runs as its own CI job and attacks the app from
  the outside — cross-tenant reads and writes, forged and revoked keys, session
  fixation, cookie tampering, open redirect, stored XSS, SQL-looking input, XER
  and HTTP header injection, mass assignment, XXE and entity expansion,
  path traversal, upload limits, and authorisation as distinct from
  authentication. It found three real bugs when it was written, which are fixed
  and listed in `CHANGELOG.md`. It is still not a pentest: it tests the attacks
  we thought of.

## Out of scope

- Attacks needing an already-compromised host or database.
- Denial of service by uploading a pathologically large schedule. Bound the
  upload at your ingress; `MASSINGPLAN_MAX_UPLOAD_BYTES` defaults to 16MB.

  The two location textareas are *not* left to that ceiling, because both do
  work proportional to the line count: the take-off builds an error string per
  bad line, and the breakdown inserts a row per entry inside one transaction.
  A 16MB body would be 2.7 million error strings or 8 million inserts from one
  authenticated `PROJECT_WRITE` user. Both are bounded at
  `services.projects.MAX_BREAKDOWN_LINES` (2000) and refused whole rather than
  truncated, since a take-off silently read down to line 2000 is a schedule
  missing everything after it.
- Social engineering of your users.
