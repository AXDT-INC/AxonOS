# Phase 2C correction and full standards validation

Artifact revision: `2.0.0-candidate.2`. Verdict: **READY_FOR_INDEPENDENT_RECHECK**. The KeyId correction and full Draft2020-12/OpenAPI3.1 validation are complete. This is an unapproved synthetic contract candidate, not authorization to implement, stage, deploy, process real clicks or enable CAPI.

## Correction and unchanged security contract

The source request header `X-AxonOS-Handoff-Key-Id` references `./schemas/common.schema.json#/definitions/KeyId` instead of repeating the weaker dollar-anchored pattern. The original terminal-LF acceptance was reproduced. The corrected request schema and all ten response KeyId schemas reject malformed values consistently with the shared schema and cryptographic-format rules.

All ten targeted `fixtures/golden.json:key_id_header_cases` pass: minimum/ordinary/maximum valid strings; empty, overlong, terminal LF, terminal CRLF, embedded LF, slash and non-ASCII invalid strings. All64 allowed alphabet characters also pass at lengths1 and32. Existing49 schema fixtures and all cryptographic vector bytes remain unchanged. Wire versions, epoch rules and normative PolicyB/custody semantics are unchanged.

Independent review reconfirmed PolicyB30s/10s, worker-created fixed BOOTTIME challenge deadlines, durable withdrawal and worker privacy fencing, central custody only after landing consent, independent app consent, continuing browser ownership, F2 wallet authority, single-use redemption, immutable expiry, no positive legacy downgrade and off/live restrictions. No additional contract defect was found by full validation. This completion changes only this report and manifest; the seven-file KeyId/revision correction from the earlier Phase2C step remains as recorded.

## Reviewed installation and source provenance

The operator explicitly approved narrowly scoped network access for validator dependencies. Before installation, all resolved package versions were reported, wheel METADATA/requirements were inspected, and each wheel's exact URL and SHA256 were checked against official PyPI JSON metadata over certificate/hostname-verified TLS. The sole index was `https://pypi.org/simple`; every wheel source was HTTPS on `files.pythonhosted.org`. No alternate index, trusted-host bypass, direct-URL dependency, source distribution or `.pth` file was accepted.

Installation used only local binary wheels with `--no-index --only-binary=:all: --require-hashes --no-deps --no-compile` and an exact19-package hash-pinned lock, targeting `/tmp/axonos-handoff-phase2c-full-etpgy_o6/venv/lib/python3.10/site-packages`. No downloaded build/install script was executed. The venv was created without system-site packages; its installed distribution set exactly matches the reviewed resolution. All24 active dependency requirements are satisfied. System/application dependency installations and repository dependency manifests were not changed.

| Package | Pinned version |
|---|---|
| `attrs` | `26.1.0` |
| `certifi` | `2026.7.22` |
| `charset-normalizer` | `3.5.2` |
| `idna` | `3.20` |
| `jsonschema` | `4.26.0` |
| `jsonschema-path` | `0.3.4` |
| `jsonschema-specifications` | `2025.9.1` |
| `lazy-object-proxy` | `1.12.0` |
| `openapi-schema-validator` | `0.6.3` |
| `openapi-spec-validator` | `0.7.2` |
| `pathable` | `0.4.4` |
| `PyYAML` | `6.0.3` |
| `referencing` | `0.36.2` |
| `requests` | `2.34.2` |
| `rfc3339-validator` | `0.1.4` |
| `rpds-py` | `0.30.0` |
| `six` | `1.17.0` |
| `typing_extensions` | `4.16.0` |
| `urllib3` | `2.8.0` |

`referencing0.36.2` is intentional: jsonschema-path0.3.4 requires `<0.37.0`. Exact wheel filenames, official source/metadata URLs, SHA256s, dependency declarations and declared entry points are retained in `manifest.json:validation_evidence.phase_2c.dependency_resolution`. Hashes of the temporary resolution, lock and verification scripts are recorded there too. Downloaded package authenticity/digest checks are not a claim that third-party packages received a full security audit.

## Executed full standards validation

- `jsonschema.Draft202012Validator` **4.26.0**: all36 schemas pass the Draft2020-12 metaschema; all49 existing positive/negative fixtures and ten targeted header fixtures have the expected result. All517 embedded OpenAPI Schema Objects also pass the metaschema checks.
- `openapi_spec_validator.OpenAPIV31SpecValidator` **0.7.2**: the complete OpenAPI3.1 document passes, including paths, operations, parameters, responses and security-scheme structure. The validator is explicitly version3.1; no older-dialect fallback is used.
- `openapi_schema_validator.OAS31Validator` **0.6.3**: all49 existing fixtures have the expected result and all517 embedded Schema Objects pass schema checks.
- All96 OpenAPI local references were explicitly resolved. Schema-local references were resolved through a contract-local registry. Every reference is checked against missing/escaping/remote targets; the existing whole-artifact reference suite also passes.
- Exact origins/path/operation/media mappings, request/response fixtures, error unions/statuses, required key-ID/MAC headers, security-scheme references, closed objects and supported validation keywords pass explicit checks. Recognized annotations and compatibility `definitions` containers are distinguished from validation keywords; none is silently substituted for an unsupported validator keyword.
- There are zero embedded `example`, `examples` or `externalValue` fields. Golden request/response fixtures provide the applicable example coverage. The check fails if new embedded examples appear without explicit validation.
- All ten KeyId cases pass against each of the ten response KeyId schemas, in addition to the corrected request/shared schema. Same spelling, parser requirements and strict absolute ending remain in force.

The full standards/HTTP script passed **3762 assertions**, emitted **zero warnings** with warnings enabled, and attempted **zero network operations**. Socket connect/name-resolution operations were blocked during validation; standard metaschemas were supplied by the installed validator packages. Network access was used only for the approved PyPI dependency acquisition/verification, not contract validation or production probes.

The existing offline contract suite passed **1776 assertions** on the updated artifacts. The independent cryptographic/adversarial suite passed **111 assertions**. Total: **5649 passing assertions across these overlapping suites**, not5649 distinct behavioral test cases. The24 dependency-requirement checks and environment/inventory scope checks are additional checks, not included in that suite total.

## Cryptography and independent review

A separate verifier used Python and OpenSSL3.0.2 to rebuild canonical JSON and request/response MAC preimages; verify request/response MACs, all11 supplied MAC-tamper cases, receipt MAC, click commitment, canonical base64url, strict malformed-input cases and domain-separated L/A/H hashes. Four F2 proofs were verified through OpenSSL HMAC/AES-CBC independently of the Fernet library, including owner-covered digests and owner-tampering rejection. All public vector results below remain unchanged. No real key, click, credential, payment or X request was used.

A separate reviewer reran the independent crypto checks, checked the KeyId request/response coverage and preserved security invariants, and independently repeated the full3762-assertion standards suite with warnings always enabled. That repeat also emitted zero warnings/errors and attempted zero network operations; package isolation was independently verified. Repeated runs are not added again to the suite total. Full standards validation found no further contract correction to make.

## Integrity, history and remaining limits

The original Phase2A manifest SHA256 was `23ec0e8d58df3fbec9d0019988b1e5d018dfbdb51528649d9a52d1bc1bebe98d`. The earlier Phase2C corrected-but-approval-blocked manifest was `8e30e394632b0947e9984769b5eea780081691ef2d16b1182260a17c986ec65c`. This completion supersedes that tooling block; it does not alter candidate2's protocol semantics.

All53 file paths remain unchanged. The manifest hashes all52 other files and excludes itself; its external SHA256 is reported after final copy. Exact inventory/digests, UTF-8/LF/trailing whitespace, regular nonexecutable files, Git whitespace and allowed-change scope are checked. All files remain unstaged and uncommitted at the same backend HEAD. Landing and GitOps are not modified.

No remaining standards-validation warning or blocker was found. Schema/OpenAPI conformance does not demonstrate implementation correctness: browser engines, PostgreSQL concurrency, proxy/privacy handling, financial/session regressions, deployment infrastructure and real public ingress were not exercised. Legal/privacy, implementation, staging/deployment, secret provisioning, real-click provenance and live-delivery approvals remain separate and absent. The40 test-matrix entries remain future acceptance obligations.

The historical Phase2A report below is retained as provenance; its old tooling limitations describe that earlier run and are superseded by the completed standards validation above.

---

# Historical Phase 2A offline validation and independent review

Scope: contract artifacts only, revision `2.0.0-candidate.1`. This is not implementation, browser, PostgreSQL, ingress or production acceptance evidence. The candidate remains unapproved for implementation/deployment and real identifiers. All inspected repository baselines are recorded in the manifest.

## Executed offline checks

Python 3.10.12, PyYAML 5.4.1, jsonschema 3.2.0, cryptography 48.0.0 and OpenSSL 3.0.2 were available locally. No packages were installed, no network request or database operation was made, and no actual credentials were read. Temporary authoring/validation tools remained outside all repositories.

- Parse every YAML/JSON/CSV artifact; reject duplicate mapping keys, malformed CSV shape, invalid UTF-8, symlinks, executable files, CRLF, missing final LF and trailing whitespace.
- Resolve all local OpenAPI/schema references without network access. Check exact origin/path/method/operation/media/form-field mappings, response MAC headers, bridge referrer policy, explicit Fetch Metadata, off defaults, policy/lease/expiry constants and error/status sets.
- Validate 36 schemas and 49 positive/negative value fixtures with the installed validator's compatible keyword subset. Check strict parser negatives separately, including duplicate keys and decimal/exponent integer spellings.
- Check 13 conservative query fixtures and the explicit fragment limitation. Check seven ordered/adversarial scenario fixtures, fixed lease arithmetic, terminal source closure, lost-begin no-remint, wrong owner, restored-queue quarantine and off-mode expected results. These are contract consistency checks, not executed service state transitions.
- Recompute canonical JSON, request/response SHA256 and HMAC-SHA256 with Python and independently with OpenSSL. All seven request and four response tamper vectors reject their unchanged supplied MAC.
- Recompute observation commitment, receipt serialization/MAC, immutable deadlines, tampered receipt/click and noncanonical base64url negatives.
- Decrypt the synthetic v4 context and four positive F2 envelopes (bind and all three milestones); verify the existing whole-envelope digest, owner coverage, proof lifetime and ticket bounds. A separate reviewer independently verified Fernet HMAC/AES-CBC using OpenSSL, including owner-tamper failure.
- Validate the exact manifest file inventory and SHA256 byte digests, excluding self-hashing. Its own SHA256 is reported separately to the operator. Run Git whitespace checks and a separate whole-directory whitespace check because untracked files are not covered by `git diff --check`.
- Conservative content scan rejects common real-secret shapes and private-key PEM. Configuration secret defaults are null; all vector key bytes/identifiers are documented public synthetic constants. This is a scoped review plus heuristic scan, not proof that an arbitrary string can never resemble an unknown credential.

The 40 entries in `test-matrix.csv` are future acceptance obligations. Their presence is not a claim that browser, database, financial/session regression or live-ingress tests ran in Phase 2A.

## Tooling limits

The installed jsonschema package lacks Draft202012Validator. All used schema keywords belong to the checked compatible Draft7 subset; fixtures were validated with that subset. Full Draft2020-12 meta-schema certification and a dedicated OpenAPI3.1 validator were unavailable. YAML parsing, local reference resolution and explicit OpenAPI structural/contract checks passed, but do not constitute full OpenAPI certification. Real Chromium/Firefox/WebKit behavior, Next16 packaged adapter integration, durable database races, privacy fences under load and proxy/network behavior remain unexecuted requirements.

## Independently recomputed public vectors

Only the public test key `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` is used in these MAC/receipt examples. Exact inputs and outputs are in the vector files.

| Value | Independently recomputed result |
|---|---|
| Request canonical-body SHA256 | `8a61369a9b3adec9fc1ac5a0c3704263230069d6e5c5d799225aace88dc8b464` |
| Request MAC | `9rE9RCzbaSjyJbXaqSeogoDHsvrZ904m_0I5lZ4mPko` |
| Response MAC | `KOMRAfuYflnpbJLOKVpy9nZzhYgIWFdUUC0wJ454iMQ` |
| Click commitment, lowercase hex | `5704d4b72f4a15526411ae54ac4f32268ca950f2a1b236a5e44115adb401f334` |
| Observation receipt MAC | `WWEuLV6zFGqVr8-d1G6xWcZ41Sna6VnQrpuwLErFKEo` |

The Phase1D quoted request and receipt MAC values matched independent recomputation; no numerical correction was necessary. The commitment is hex, not base64url. Timestamp `1791936000000` is an invented deterministic fixture time, not evidence of a real ad observation or provenance.

## Review findings resolved before artifact freeze

| Finding | Resolution and enforcing contract |
|---|---|
| Initial request might serialize raw click into Next SSR/RSC | Same-process pre-Next adapter sanitizes request and framework metadata; receipt only reaches renderer; browser independently scrubs before analytics. Actual integration remains a required future test. |
| Receipt/memory windows were inconsistent in one test row | Memory120s, server receipt110s, fixed uncertainty margin and original source deadline now agree. |
| Regex dollar anchor accepted a terminal newline | Absolute-end negative lookahead plus negative fixtures in all identifier schemas. |
| Source response authentication was missing from OpenAPI headers | Authenticated response headers and exact request-bound purpose-separated framing now explicit. |
| Clean POST bridge inherited no-referrer | All clean bridge pages require strict-origin; raw-query landing remains no-referrer. Exact browser-engine behavior remains a test gate. |
| Query ambiguity lacked matching fixtures | Explicit conservative no-decoding grammar and 13 cases; no guessed alias handling. |
| Hash-only H could not reproduce lost begin response | Same-ID committed retry returns RESULT_UNAVAILABLE; no automatic remint; bounded explicit new attempt only. Callback retains and returns the identical encrypted result. |
| Context-only theft could bypass browser ownership after import | Trusted cookie adapter supplies immutable owner hash; v2 F2 signs it; worker mapping checks it for every positive bind/event, including normalized bind envelopes. |
| V4 ticket version/bounds and source mapping differed | Keep unchanged14-field v4 format and bounds; authoritative millisecond fields live in import mapping; no positive v1 downgrade. |
| Wire withdrawal state confused with DB state | Wire `withdrawn` versus durable source `closed` explicitly distinguished. |
| Worker described as verifying landing receipt MAC | Landing alone verifies receipt key/MAC; worker verifies authenticated observation claim and independent time bounds. |
| Bridge HTML cap leaked into marketing page | Only bridge responses have65536-byte cap; marketing root retains privacy requirements without unsupported response-size assumption. |
| Binding state appeared as an import-table state | State matrix labels existing context wallet binding separately from active/closed/expired import state. |
| Fetch Metadata/cookie/CSP detail could diverge across sessions | Exact allowed metadata, literal empty destination, optional user activation, cookie issuance/shortening and nonce bridge CSP are specified. Prepared is server preparation; next request proves cookie acceptance. |
| Withdrawal could race delayed lease renewal or queue restoration | Same landing control lock; worker-created BOOTTIME deadline; boot-bound nonce; final exclusive dispatch check; restored/missing queue quarantines and requires reviewed generation recovery. |

Independent reviewers found no remaining material contract-level browser-owner, wallet-authority or replay bypass in this candidate. This is a bounded engineering review, not a security certification of future code.

## Residual trust and operational assumptions

Landing compromise can falsify landing observation/consent claims, but cannot establish app consent, app cookie authority or F2 wallet authentication. App/gate compromise remains a trusted-origin/proof-authority compromise. XSS, stolen full browser profiles and malicious browser extensions are not solved by cookies. Already authorized/in-flight external requests cannot be recalled; the lease guarantee ends at the final dispatch authorization boundary. Browser closure cannot transmit an unseen later GPC change.

Clock assurance, enforced transport/isolation, immutable deployment identity, durable queue supervision, safe logs/Umami headers, encrypted existing plaintext columns/backups and externally anchored recovery are prerequisites, not verified facilities. No restored empty queue is accepted as safety. Full-stack undetected rollback cannot be solved by a generation value stored only in the restored databases. These items remain explicit future approval and acceptance gates; no positive real-traffic mode is authorized.

No material `OPEN_DECISION` remains for the isolated synthetic artifact candidate. Legal/privacy, infrastructure, implementation, staging and activation approvals remain absent. A failure to meet an implementation prerequisite must stop that implementation path, not silently weaken this contract.

## Essential additional artifacts

`protocol.json` provides a machine-readable shared source for versions, origins and limits; `cryptographic-formats.md` specifies bytes and compatibility beyond JSON Schema; `browser-contract.md` records browser/provenance semantics that OpenAPI cannot express; this report records executed evidence and its limits. These additional files contain no executable runtime code or migrations.
