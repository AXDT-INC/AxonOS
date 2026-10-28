# Cryptographic and byte-level contract

This is a synthetic engineering candidate, not production configuration. Protocol identity is `axonos-first-party-handoff-v2`; the ASCII MAC domain is exactly `AXONOS-X-HANDOFF-V2`. Public fixture keys are never deployment keys.

## Parser and canonical bytes

All new wire JSON has UTF-8 encoding restricted to ASCII by the closed schemas. Reject BOM, invalid UTF-8, duplicate object keys at any depth, unknown fields, NaN/Infinity, numeric booleans where integers are required, and numeric tokens using a decimal point or exponent. Integers use ordinary minimal decimal spelling (no leading zero, plus sign or negative zero). New wire times are positive integer milliseconds, bounded by 9007199254740991; policy epochs are 1..2147483647. Existing decrypted v4 ticket seconds are a distinct compatibility format and are not parsed with this integer-only wire rule.

CanonicalJSON recursively sorts object keys by ASCII byte order, preserves array order, uses compact separators `,` and `:`, minimal integer decimal output, `true`, `false`, `null`, and JSON ASCII string escaping. No spaces or trailing newline are included in canonical JSON bytes. Identifier strings reject control characters, including terminal line feeds; schema regexes use an absolute-end negative lookahead because a bare dollar anchor can match before a final newline. This is a restricted local canonical format, not a claim of complete RFC 8785 support. Service request/response bodies and decoded receipt payloads MUST equal their re-encoded canonical bytes. Browser JSON need not arrive in canonical key order, but must satisfy the same strict parser and schema before the source client constructs canonical service bytes.

Base64url uses RFC 4648 URL-safe alphabet without `=` padding or whitespace. Decode with reconstructed padding then re-encode and require exact equality; reject alternate unused pad bits. Token43 means exactly 32 decoded bytes. Hex64 means exactly 32 bytes represented as lowercase hexadecimal. Key IDs are case-sensitive `[A-Za-z0-9_-]{1,32}` and are not file paths. Unknown or duplicate key IDs fail closed. Hashing inputs use exact canonical values, never trimmed or case-normalized tokens.

Verify fixed-length MAC bytes using a constant-time comparison after bounded parsing and decoding. A schema match is never proof of a valid MAC, receipt, cookie or F2 authority.

## Service request MAC

Method is exactly `POST`; path is exactly `/api/x-attribution/handoff/source`. Reject query, trailing-slash redirects, percent-encoded aliases and alternate authorities. The authenticated transport destination is fixed `https://app.axonos.io`; do not follow redirects.

Use HMAC-SHA256 over the following ASCII fields joined by LF, including a final LF:

```text
AXONOS-X-HANDOFF-V2
request
POST
/api/x-attribution/handoff/source
<kid>
<epoch as decimal>
<generation UUID>
<request_id UUID>
<lowercase SHA256 of exact canonical request bytes>
```

The header `X-AxonOS-Handoff-MAC` is the unpadded base64url MAC. `X-AxonOS-Handoff-Key-Id` selects the service key. Both are single-valued; duplicate instances fail. The source endpoint rejects browser Origin, Cookie and Authorization headers. Source authentication permits only the enumerated source operations; it cannot create app consent, import ownership, wallet credentials or F2 proofs.

MACs do not provide confidentiality. All network hops carrying requests need independently verified authenticated encryption and trusted termination. The gate may relay exact bounded request bytes without possessing the service key or any CAPI DB credential. Worker performs the authoritative authentication.

No MAC timestamp window is used: immutable operation IDs, state checks, fixed deadlines, retired generations and tombstones provide replay safety. Replaying a valid old message must never reopen a source or refresh a deadline. Unknown-source closure is privacy-improving and idempotent. Operation records cannot be garbage-collected while a replay could recreate authority.

## Service response MAC

Use the same service key ID as the verified request and the separate `response` purpose. Join these exact ASCII fields by LF, including a final LF:

```text
AXONOS-X-HANDOFF-V2
response
<SHA256 of exact request bytes>
<HTTP status decimal>
<kid>
<epoch decimal>
<generation UUID>
<request_id UUID>
<SHA256 of exact canonical response bytes>
```

Response headers use the same key-ID and MAC names. A client must bind the response to its own request bytes, status, operation identity and generation, not merely trust the response's echo. Authenticated requests receive authenticated success/error responses; malformed unauthenticated requests may receive bounded unsigned generic errors, which can never acknowledge closure. Only an authenticated `200` revoke response with matching operation ID and `state=withdrawn` can acknowledge an obligation. A signed status of `none`, an HTTP timeout or a generic `410` is not such an acknowledgment.

## Observation receipt

The landing pre-render adapter samples server time when it observes the initial request, before consent and before Next receives the sanitized request. Browser volatile click memory is separate and supplies only the exact candidate on later consented capture. Browser time is never accepted as observation time.

The click commitment is lowercase HEX HMAC-SHA256 using the landing-only receipt key over these exact bytes:

```text
click\n<observation_id>\n<exact validated click>\n
```

Observation is the closed `observation.schema.json` object. Its payload is unpadded base64url of CanonicalJSON(Observation). Receipt MAC is HMAC-SHA256 over:

```text
AXONOS-X-HANDOFF-V2\nreceipt\n<kid>\n<payload>\n
```

Receipt consists of exactly `kid`, `payload`, `mac`. The commitment is hex; the MAC and payload are base64url. Do not interchange them. Receipt fields must be validated after authentication too: exact origin/policy/version/epoch/generation, temporal order, observation ID and click commitment. MAC validity does not prove a source is current or an X click is genuine.

The candidate synthetic clock profile requires each participating host to establish error <=5000 ms from UTC. Receipt deadline is observed+110000 ms; original source deadline is observed+604790000 ms. The 10000-ms safety margin conservatively covers opposite clock errors. Acceptance uses strict `< deadline`, not `<=`; no request, retry or key rotation renews either. Capture requires observed <= landing consent and a still-current receipt window at landing and worker; uncertain clocks reject positive operations. Privacy closures remain available without positive clock assurance. An import is deferred/dropped if worker time is behind the accepted observation/consent times; it must not invent a future import time.

The receipt is generated without recording pre-consent raw identifiers in durable state. It authenticates a server observation claim, not X provenance. Fixture timestamp 1791936000000 is a deterministic test value, not a reported real observation.

## Ownership and capability hashing

L, A and H are independently generated CSPRNG 32-byte values. Store only domain-separated hashes of their exact canonical base64url strings:

```text
landing_owner_hash = SHA256("AXONOS-X-HANDOFF-V2\nlanding-owner\n" + L)
app_owner_hash     = SHA256("AXONOS-X-HANDOFF-V2\napp-owner\n" + A)
handoff_hash       = SHA256("AXONOS-X-HANDOFF-V2\nhandoff\n" + H)
```

Database representations may be 32-byte binary values; wire hashes are lowercase hex. Generation and epoch are additionally bound by database keys and authenticated messages. The public gate computes app owner hash from the strict cookie parser only; browser-supplied hashes have no authority. Cookie secrets never enter a producer envelope or DB.

H remains hash-only after the first response is emitted. A committed begin retry therefore returns `409 RESULT_UNAVAILABLE`, not a fabricated identical H. Recovery of a redeemed context uses the retained encrypted result, not reconstruction of H from its hash.

## Existing v4 context and F2 compatibility

Preserve the exact 14-field encrypted v4 plaintext shape: `v,handle,csrf,state,policy_version,policy_epoch,mode_scope,audience_scope,issued_at,lifecycle_expires_at,landing_commitment,consented_at,expires_at,twclid`. Its timestamps are finite seconds; the authoritative new DB milliseconds are divided by 1000.0 once for sealing. Never recover authoritative milliseconds by truncating floating-point ticket fields. Seal issued_at from observed_at_ms and consented_at from import_cutoff_ms; the latter is effective import activation, not either UI choice or physical commit time. Plaintext <=1024 bytes, opaque wire <=2048 characters; generated handle/CSRF are 43 characters within existing 32..128 bounds.

The v4 `landing_commitment` remains the existing HMAC-SHA256, keyed by handle ASCII, over `b"AxonOS X landing commitment v1\x00" + twclid`. It is distinct from the observation receipt commitment. The imported ticket's effective lifecycle expiry is min(original source expiry, app-owner deadline); granted expires_at equals that lifecycle expiry. Source's original deadline remains immutable in its source record. The ticket policy is `x-capi-handoff-v2`; separate landing/app policy evidence stays in their records. Existing ciphertext includes the click; app browser storage contains ciphertext, never plaintext. Worker stores the same sealed result for bounded callback recovery; it does not reseal on retry.

The complete positive ingestion datagram remains bounded to the existing 4096 bytes, even when individually valid field maxima would exceed it. The new local source RPC frame is bounded to 12288 bytes including the 8192-byte HTTP body and its authenticated framing. Positive imported envelopes use `v=2` and include server-captured `browser_owner_hash`. Existing `exact-bearer-v1`, purpose `AxonOS X CAPI independently authenticated wallet v1`, primary-key requirement and 30000-ms authority validity remain unchanged. Envelope digest is SHA256 of sorted compact ASCII JSON of all envelope fields except `wallet_authority`. Binding normalization must preserve the verified owner scope. New policy epoch/audience scope pins handoff v2 and owner-bound ingestion; no positive legacy fallback. Existing suppression/revoke compatibility remains, including missing A or old decrypt-only context keys.

Proof verification still attests exact independently accepted bearer authority at event production; it is not indefinite login validity or payment-signature authentication. Outer schema validation does not replace wallet eligibility, context mapping, immutable source, original event time, binding decision cutoff, current consent or privacy-fence checks.

## New pending-source encryption and rotation

Encrypt only the new pending-source click with worker-only AES-256-GCM, independent random 96-bit nonce per encryption under a key, and 128-bit authentication tag. Store key ID, nonce and ciphertext+tag. AAD is CanonicalJSON of `{v:2,epoch,generation,owner_hash,observation_id,purpose:"pending-source"}`. No plaintext click in landing DB. Existing context/outbox plaintext columns require separately verified storage/backup encryption; this contract does not claim they are field-encrypted.

Service/receipt/data/context keys are distinct in deployment. Test vectors deliberately reuse a public key for reproducibility only. Service rotation distributes verification keys before signing switches; retries preserve logical IDs and canonical bodies while using the current key. Receipt key overlap covers the fixed receipt window. Source decryption keys remain available for retained data/backups. Existing worker hash key is immutable while retained rows exist. Never read or substitute actual deployment keys during artifact validation.
