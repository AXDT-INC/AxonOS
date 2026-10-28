# Browser requirements for worker-authoritative handoff v2

Status: normative contract candidate for synthetic implementation only. This document does not authorize implementation, deployment, production tests, configuration changes or live X delivery. Endpoint names and complete schemas are defined by the enclosing contract; this document does not introduce additional endpoints.

`MUST`, `MUST NOT` and `SHOULD` describe the proposed contract, not capabilities verified in the current repositories.

## 1. Origins, information and authority

- Landing origin is exactly `https://axonos.io`; app origin is exactly `https://app.axonos.io`. No trailing slash, alternate port, wildcard, suffix match, inferred forwarded origin or `www` alias is accepted as either authority.
- Attribution capture MUST NOT run on `www.axonos.io`. Its fallback goes to a fixed clean canonical destination with the query removed. A fallback MUST NOT forward a raw click, observation receipt, handoff or context in a URL.
- Raw `twclid` exists only transiently in the landing initial request and bounded landing-page memory before consent. A valid capture can persist it only after affirmative landing consent through the separate server contract.
- Raw `twclid` MUST NOT appear in app URLs, browser form bodies to the app, plaintext app browser storage, HTML/RSC payloads, browser diagnostics, analytics payloads, referrers or errors.
- The existing version-4 context is encrypted ciphertext whose decrypted payload contains `twclid`. Retaining that ciphertext is an explicit compatibility choice. It satisfies **no plaintext click in app browser storage**; it does not mean no click-bearing encrypted artifact exists in the browser. No decryption key is provided to the browser or landing service.
- L and A establish possession of origin-local attribution state. H identifies one registered app-consented transaction. None authenticates a wallet, proves physical identity or establishes authentic X click provenance.

## 2. Initial request and pre-Next boundary

A same-process request adapter MUST run before the Next router/server receives a sensitive initial request. It MUST:

1. Observe the original request target once in volatile memory, enforce bounded parsing, validate the exact candidate, and create the tamper-evident observation receipt described by the shared schema.
2. Construct a sanitized request target before handing the request to Next. For this initial-observation profile, always remove the entire query before rendering, including unrelated campaign parameters. Ambiguous parsing discards the candidate too. Use the exact conservative grammar below and the shared query fixtures.
3. Remove or overwrite caller-supplied original-URL, rewrite, internal-receipt and equivalent framework metadata. `Next-URL`, router-state values and other inputs that can carry a sensitive query MUST NOT reintroduce the original URL into rendering. Initial sensitive requests with unsupported routing metadata fail closed to a clean ordinary page.
4. Give the renderer only the sanitized URL and a trusted request-scoped receipt, never the original URL, the raw request object as an observation source, or raw click props. The receipt contains a commitment and metadata, not the click plaintext.
5. Prevent receipt cross-user caching. Either render dynamically or inject the receipt per response into a demonstrably clean cached template. The delivered initial response and all attribution/error responses are `Cache-Control: no-store, private`.
6. Supply a synchronous nonce/hash-authorized bootstrap before unrelated scripts/resources can observe the original browser URL. The bootstrap reads and validates its current URL, removes sensitive parameters with `history.replaceState`, and retains only an eligible candidate in bounded memory. If rewriting fails, discard it and navigate to a fixed clean destination without attribution.

Server sanitization does not itself change the browser address bar. Browser scrubbing does not retroactively sanitize server-rendered HTML. Both boundaries are mandatory.

The adapter MUST NOT log request targets, receipt contents, click values or thrown objects that can contain them. No raw candidate may be inserted into inline script, JSON script tags, RSC/Flight, hydration properties, metadata, preload URLs, response headers or HTML comments. Receipt injection uses only the exact safe encoding defined by its schema.

**Decision record:** using a same-process adapter preserves the landing product flow and adds no service. Its exact integration with Next 16 and the current standalone Docker launch remains UNVERIFIED. Session B must prove it with actual packaged-server, RSC, cache and browser tests. A presumed Next rewrite is not proof. If the adapter cannot meet this boundary, implementation is blocked until a preserving solution is reviewed; silently relaxing the raw-serialization rule is forbidden. This is an implementation prerequisite, not permission for an unreviewed consent interstitial or an additional service.

The observation request is `GET /` on the canonical landing host, for a top-level HTML document. Require `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`; Site may be `none`, `cross-site`, `same-site` or `same-origin`. Missing/ambiguous metadata leaves an ordinary clean page without a receipt. Do not issue receipts for RSC, prefetch or other background requests. The raw query is at most 8192 ASCII bytes and at most 32 ampersand-separated fields; each nonempty field has exactly one `=`. This conservative synthetic profile performs no percent or plus decoding: any percent sign, plus sign or semicolon anywhere in the query discards the candidate. Exactly one literal lowercase `twclid` name is allowed; any case-insensitive competing alias discards it. Its exact value must pass the shared click validator. Other unambiguous fields may be ignored but are removed from the renderer/browser URL. A browser fragment is removed and causes candidate discard; the server cannot observe fragments. This intentionally loses attribution for unsupported encoded queries instead of choosing an ambiguous interpretation. Golden query cases define matching adapter/bootstrap behavior. In off mode or observed GPC, scrub without minting positive receipt/owner state.

## 3. Observation, first touch and clocks

- The first successful consented capture for one established L owner is immutable. Later observations cannot replace its click, observation time or original expiry.
- This does not claim selection of the earliest unconsented navigation across unidentified tabs, global click uniqueness, or prevention of a fresh observation when a raw ad URL is deliberately loaded in another browser.
- Browser candidate memory lasts at most 120 seconds from initial observation. Denial, GPC, parsing/scrubbing failure or `pagehide` erases it earlier. Timers are not the sole control: every use compares the fixed deadline, including after suspension or BFCache restoration.
- The server receipt acceptance window is 110 seconds from the observation timestamp. This leaves the specified five-second margin on each side of the 120-second candidate window. The receipt clock policy and worker enforcement are defined by the shared server contract; browser clock values never establish original expiry.
- Original source expiry, and L's maximum source-associated lifetime, are observation time plus seven days minus ten seconds under this frozen profile. Retries, consent delay, handoff, refresh and wallet authentication never renew it.
- Receipt replay under an already captured owner cannot replace first touch. Receipt verification is not proof of browser ownership before the owner cookie exists, and is not X provenance.

## 4. Cookie preparation and confirmation

L and A are independent 32-byte CSPRNG values encoded as unpadded base64url. They use the contract's fixed `__Host-` cookie names with:

```http
Secure; HttpOnly; SameSite=Strict; Path=/
```

`Domain` is forbidden. Duplicate occurrences of an ownership-cookie name are rejected for attribution rather than resolved by first/last parser behavior. Cookie values supplied in request bodies, URL parameters or custom headers MUST NOT be treated as ownership cookies.

Landing prepare occurs only after affirmative landing choice. App prepare occurs only after affirmative independent app choice. The next contract call MUST demonstrate the cookie was accepted before capture or H creation. A prepare response alone is not cookie confirmation.

- Preserve an existing valid cookie. Never rotate it merely because another tab loaded, a request timed out, a response was lost, consent UI was reopened or a Strict cookie was absent on an initial cross-site GET.
- An X-to-landing GET can omit an existing Strict L. Resolve ownership with the subsequent same-origin call; do not overwrite the owner from SSR's missing-cookie observation.
- Serialize first prepare and cookie confirmation in a same-origin Web Lock. Concurrent tabs reuse the confirmed cookie. Where safe coordination is unavailable or ambiguous, drop attribution and continue ordinary navigation.
- A lost prepare acknowledgement is resolved by cookie confirmation/state through the existing contract call. With a valid existing prepared cookie, retry returns the same metadata without issuing a secret. If that creation request ID already committed but the cookie is absent, its hash-only retained state cannot reconstruct the cookie: return `RESULT_UNAVAILABLE`. No dependent durable capture/H is created. Do not silently issue a replacement owner as a retry policy; ordinary navigation remains available.
- A later explicit affirmative-choice attempt may use a new creation request ID under global bounded admission. It does not rotate an existing confirmed owner. Unconfirmed provisional rows contain no raw click and expire/are cleaned up after 120 seconds.
- A has a maximum seven-day server deadline fixed at creation; it may only be shortened, including to the source's original expiry at approval/redemption. Neither server validity nor cookie lifetime is extended by activity.
- Cookie deletion, blocking, expiry, owner mismatch or insufficient lifetime suppresses attribution. Never borrow the other origin's cookie, use a parent-domain cookie, or substitute IP/User-Agent matching.

Cookie issuance is exactly `__Host-axon_x_l=<L>` on landing prepare and `__Host-axon_x_a=<A>` on app prepare. On first creation, L's server deadline is the authenticated receipt's original source deadline; an existing L keeps its original deadline even when a newer receipt is presented; A's is creation plus 604800000 ms and can only shorten. A callback may repeat the same A value solely to shorten its expiry after redemption. No error, initial observation, ordinary entry or analytics response issues an ownership cookie. Set `Max-Age=floor(max(0,deadline_ms-response_now_ms)/1000)` and `Expires` to the deadline rounded down to a UTC whole second, plus the fixed attributes above. Refuse positive creation if this leaves no lifetime. No response may extend either server deadline. Relative Max-Age starts at browser receipt and can outlast the server deadline because of transit delay; the server deadline is always authoritative and cookie attributes are not an expiry proof. Closed state remains authoritative even if the client retains the cookie; withdrawal need not delete it. The prepare response's `state=prepared` means server bootstrap prepared, never proof of browser cookie acceptance, not a reset of an existing captured/imported/closed lifecycle; later status/use checks the actual lifecycle.

## 5. Browser request provenance

All new same-origin JSON handoff requests additionally require `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors` or `same-origin`, and `Sec-Fetch-Dest: empty`. Missing, duplicate or incompatible required metadata suppresses the optional operation. There is no legacy-browser positive fallback. Existing scoped privacy-capability closure remains independently available. All same-origin JSON handoff requests use exactly the frozen schema, `Content-Type: application/json`, `X-AxonOS-Handoff: 2`, and exact own `Origin`. They receive no cross-origin CORS permission. Duplicate keys, extra fields, ambiguous encodings and unsupported versions fail closed. Existing context CSRF capabilities remain required where the existing consent API requires them.

Cross-origin form navigation is the only browser exception. Each form has exactly two decoded fields: `v=2` and `handoff=<H>`. It uses the contract's fixed peer endpoint and `application/x-www-form-urlencoded`. Duplicate fields, aliases, unsupported version, arbitrary redirect parameters and extra fields are rejected.

- Landing authorize requires exact app Origin, confirmed L and the worker-registered H.
- App callback requires exact landing Origin, confirmed A and the matching H.
- `Origin: null`, missing Origin and multiple/confused Origin values are rejected on these POST paths.
- Peer form navigation requires `Sec-Fetch-Site: same-site`, `Sec-Fetch-Mode: navigate`, and `Sec-Fetch-Dest: document`. `Sec-Fetch-User` is optional (automatic form submission need not have activation); if present its only accepted value is `?1`. Metadata never substitutes for exact Origin or cookie ownership.
- Clean bridge documents use `Referrer-Policy: strict-origin`. Initial raw-query pages use `no-referrer`. There are no cross-origin redirect hops between the form source and accepting endpoint.
- Bridge CSP forbids framing and arbitrary form actions; no analytics or third-party scripts/resources run on bridges. No H appears in URL, path, page title, error, log or analytics event.

The Fetch Origin algorithm can produce `Origin: null` for navigation POST under `no-referrer`; accepting null to work around this is prohibited. Both canonical origins are same-site, so SameSite does not protect against a malicious sibling origin.

**No trusted-origin forwarding gadget:** the app renders only the H returned for the current A-bound consented begin operation. No URL/body/message input may ask the app to relay an arbitrary H in an app-origin form. Landing similarly renders only the H it just authorized for its current L. A signed attacker-owned transaction would not cure a violation of this requirement.

Bridge HTML uses `Content-Security-Policy: default-src 'none'; script-src 'nonce-<fresh-128-bit-or-greater-CSPRNG-base64>'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action <fixed-action>`. The fixed action is `https://axonos.io/attribution/authorize` for app entry, `https://app.axonos.io/attribution/callback` for successful landing authorize, and `'none'` for callback/error pages. Only reviewed first-party inline code receives the per-response nonce; no unsafe-inline/eval, third-party resource or caller-specified action is allowed. `Referrer-Policy: strict-origin` is required on all clean bridge documents, including app entry; raw-query landing pages remain `no-referrer`. The existing full marketing CSP needs compatible bootstrap nonce/hash integration and is not replaced by this bridge-only policy.

The source client has a 2000-ms whole-call deadline including connection/body/RPC wait; attribution browser waiting is bounded to 5000 ms before offering clean ordinary navigation. Neither deadline proves that a remote commit failed. Begin and hash-only prepare never automatically mint/retry on ambiguous results. Status, same-identity authorize/recovery and privacy retries follow durable state. Pending privacy retries use 1000-ms base exponential backoff capped at 30000 ms with 20% jitter and no attempt-count deletion; closed schema/auth failures alert and retain obligations. No service redirect is followed. These are attribution-only budgets and never wrap business requests.

## 6. H lifecycle and lost results

H is generated by the worker only after app cookie confirmation and independent app consent. Its deadline is fixed at creation to five minutes, capped further by A and source deadlines; authorization and retries do not extend it.

```text
app_granted -> source_approved -> redeemed
     |                |              |
     +-> cancelled/expired
```

Revocation remains terminal under the server state machine. A redeemed transaction never creates a second context; source closure and import state block its recovery.

- H is retained server-side only as its capability hash. A lost begin response cannot be reconstructed from that hash. The browser MUST NOT automatically retry begin. The orphan expires under admission limits; ordinary navigation continues. A later explicit user attempt is a new bounded attempt, never a renewal of the lost H.
- A repeat of an already committed hash-only begin creation whose original H cannot be reconstructed returns `RESULT_UNAVAILABLE`. This outcome applies to prepare/begin creation as specified above; it is not a replacement for the callback's required idempotent result. Never replace H, rotate the owner or mint another context to make a creation retry appear successful.
- An authorize retry is permitted only for identical H and the same immutable source owner. A different source cannot relink it.
- Callback recovery returns the original context/lifecycle under the frozen worker result-retention contract. It requires identical A and H, and fresh checks for revocation, source state, generation and expiry. An unavailable dependency returns the contract's unavailable response; closed state returns its applicable terminal result. Neither permits another context to be minted.
- H may be kept in app session storage only after app consent, until its fixed deadline. It is erased on completion, denial/GPC or terminal failure. It is never stored in a URL or copied into ordinary navigation links.
- Browser back/refresh or form resubmission cannot grant consent again, extend deadlines or cause additional context creation. Failure loses attribution only.

## 7. Context installation, owner proof and all-tab behavior

Every imported context has one stable nonsecret UUIDv4 `ownership_id` allocated and stored in the atomic import transaction. Independent callbacks MUST return that same identifier and MUST NOT invent different lock names for the same context.

Retain existing app Web Locks, private revoke capability, asynchronous-generation checks and wallet latch. On `pagehide`, invalidate asynchronous responses, clear publicly usable context and release ownership. A persisted `pageshow` must reacquire ownership and recheck state/GPC before exposing the context. Unsupported locking/storage, cloned-tab ambiguity or stale restoration leaves attribution suppressed; the private withdrawal path remains available.

The app checks browser GPC immediately before installing/exposing a returned context and before attribution use, in addition to server `Sec-GPC` handling. A newly observed signal latches suppression and withdrawal; a delayed response cannot reactivate it. A closed/offline browser cannot transmit a new GPC signal, and the contract does not claim otherwise.

**Post-redemption browser ownership is mandatory:** a version-4 encrypted bearer alone is insufficient. Trusted Flask/Websockify request adapters capture the domain-separated hash of the actual A cookie into immutable authenticated producer context. The new versioned F2 envelope covers that hash in its existing whole-envelope authority digest. The worker checks imported-context-to-owner mapping before every bind/event mutation. Imported contexts reject legacy envelopes lacking this proof; no fallback to ordinary bearer-only v4 handling is permitted.

Positive imported-context use must also have valid request provenance. If Origin is present, it is exactly the app origin. If it is absent on a legitimate same-origin GET, require `Sec-Fetch-Site: same-origin`, mode `cors` or `same-origin`, and destination `empty`; absent/ambiguous provenance loses attribution only. Same-origin Fetch can report mode `cors`, so a blanket `Sec-Fetch-Mode: same-origin` requirement is incorrect. Bridge requests intentionally use their separate peer-origin navigation rules.

The A hash comes only from the trusted cookie parser, not client JSON/header fields. Raw A is not sent to the worker or logged. This adds no database/RPC call to the business path. A missing/expired cookie or unsupported request path drops attribution without changing authentication, settlement or sessions.

Withdrawal remains authorized by the existing scoped revoke capability even when A is missing, because it can only suppress. Neither A, H nor browser provenance can establish wallet identity. Existing independent exact-bearer F2 authentication and first-wallet binding remain necessary.

## 8. Required synthetic acceptance evidence

Use isolated Chromium, Firefox and WebKit against the actual packaged landing/app adapters on two HTTPS same-site test origins, a malicious sibling and a separate browser profile. Use generated test values only; keep external delivery blocked.

Required cases include initial HTML/RSC/cache isolation; receipt-only rendering; query-parser confusing aliases; early analytics/referrer leakage; Strict-cookie cross-site entry; simultaneous prepare/lost prepare responses; blocked/deleted/duplicate cookies; wrong/null/missing Origin; malicious sibling forms; redirect taint; arbitrary-H forwarding rejection; H creation response loss; authorize/callback retry and `RESULT_UNAVAILABLE`; concurrent redemption; stable lock identity across duplicate callbacks; cloned tabs; BFCache and stale async responses; original expiry and A shortening; GPC during navigation/response handling; context-only theft using another authenticated wallet; imported-envelope downgrade rejection; and ordinary navigation/payment/session success when every optional dependency fails.

The current repositories establish none of these new browser flows as implemented. Existing app tests and privacy mechanisms are reusable evidence sources, not proof that the new packaged flow or public ingress has passed.
