# X CAPI ingress activation runbook

Assessment date: 2026-09-29. Baseline: `e36629e1c05d41d0f90f29defd97c281cfbb3c3f`
on main `d6d9d72`. This is a proposed operator runbook, **not an applied change**.
No production probes, real clicks, X requests or production secrets were used.
Keep public production attribution **off**. Dry-run tests must be isolated and
synthetic: dry-run is not a classifier that distinguishes real clicks from fake
ones. This build's structural live block remains unchanged.

## Dedicated-token contract recheck

The X-owned [template repository](https://github.com/twitter/x-ads-conversion-api-gtm-template)
was rechecked before edits: current main `b54fdd1baac33fa2772e0081c6d5380dd85d54ab`
and metadata-pinned release `e637e1c4af64d3b4ff1a4da9b97e950d13ad5dc9`
have identical `template.tpl` bytes (SHA-256
`09a0dca138ddac75a5fa23b9e3f2d273e9bb2c29edcf436773c07f2732449fbd`).
The [payload and send implementation](https://github.com/twitter/x-ads-conversion-api-gtm-template/blob/e637e1c4af64d3b4ff1a4da9b97e950d13ad5dc9/template.tpl#L297)
still uses POST `https://ads-api.x.com/12/measurement/conversions/{pixel_id}`,
`Content-Type: application/json`, `X-Pixel-Token`, and `conversion_timestamp`
from Unix milliseconds. No material dedicated-token contract change was found.
The template's optional browser/PII fields are deliberately not adopted.
Its conversion-ID description addresses Pixel/CAPI deduplication, not a promise
of exactly-once CAPI retry handling. AxonOS's stricter response classifier,
retry/expiry policy and activation controls remain local decisions, not X
guarantees. This does not resolve the separate generic OAuth documentation for
other integration paths and does not authenticate a supplied click ID.

## Evidence and ownership

The supplied 2026-09-25 ingress report is production evidence supplied by the
operator, not a fresh runtime inspection from the GPU host. It reports Next.js
image `37513dc`, Umami `postgresql-v2.20.1`, TLS termination at Envoy, public and
private L4 OCI NLBs, mandatory PROXY v2, no CDN/WAF/proxy cache, retained default
JSON access logs, and two exact Umami routes. Production landing infrastructure
is separate from the local landing-source copy.

| Concern | Evidence | Owner / action |
| --- | --- | --- |
| High: initial query enters general logs | Supplied report lines 140–165: `%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%` | Gateway/GitOps and log-platform owners; redact at emission, then audit retention |
| High activation risk: analytics can persist click ID | Pinned Umami source below; actual records not inspected | Landing/analytics owners; prevent collection and verify storage, exports and deletion |
| Real-click handoff/provenance incomplete | Landing `components/Analytics.tsx:28`, `app/layout.tsx:49`, `app/page.tsx:9` at source `37513dc` | Landing/application owners; separate approved handoff project, not this transport patch |
| No namespace NetworkPolicy | Supplied report lines 231–242 | Infrastructure owner; separately scoped hardening; do not rely on forwarded headers as authentication |
| Unmanaged all-protocol `0.0.0.0/0` worker NSG rule | Supplied report lines 244–250 | Infrastructure owner; separately assess exposure and remove by approved change |

The report's blanket assertion that both hardening findings are inaccessible
from the Internet is not established by the NSG rule alone; routes, addresses,
listeners and other security controls must be checked. Do not infer landing
production reachability from the GPU host. Neither hardening issue was changed.

## 1. Discover deployed versions without reading secrets

Run on the landing infrastructure's authenticated administration host. Verify
the intended cluster before every change. The names below come from the report;
discover current pod/container names rather than guessing them.

```sh
kubectl config current-context
kubectl -n envoy-gateway-system get deployment,daemonset -o wide
kubectl -n envoy-gateway-system get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.containers[*]}{.name}{"="}{.image}{" "}{end}{"\n"}{end}'
kubectl -n envoy-gateway-system get envoyproxy,gateway -o yaml
kubectl get gatewayclass -o yaml
kubectl -n axonos-landing get httproute axonos-io www-axonos-io -o yaml
kubectl get httproute,clienttrafficpolicy,referencegrant -A
kubectl explain envoyproxy.spec.telemetry.accessLog --recursive
kubectl -n axonos-landing get deployment axonos-landing -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl -n umami get deployment -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}'
```

Record controller/proxy image digests, matching release documentation, CRD
schema, GatewayClass/Gateway `parametersRef` and both referenced EnvoyProxy
objects. Review only the relevant logging sections of Envoy's admin config
dump locally; do not publish full config dumps or secret/environment values.

## 2. Change log formatting, not requests or routing

Make an approved GitOps change to the existing resources in
`components/infra/overlays/mgmt-cluster/envoy-proxy-configs.yaml`.
Define an explicit `spec.telemetry.accessLog.settings` JSON format, preserving
the currently approved operational fields but replacing the path expression.
Do not add headers, cookies, bodies, full URLs or arbitrary metadata.

On versions supporting the current built-in operator, use:

```yaml
# A field within the existing explicit format.json map, NOT a full manifest:
x-envoy-origin-path: "%PATH(NQ:ORIG_OR_PATH)%"
```

`NQ` is essential: bare `%PATH%` defaults to including the query. If the installed
version lacks that operator, a version-supported equivalent is:

```yaml
x-envoy-origin-path: "%REQ_WITHOUT_QUERY(X-ENVOY-ORIGINAL-PATH?:PATH)%"
```

The latter needs the `envoy.formatter.req_without_query` extension. Current
Gateway source registers it when it sees the operator; verify this in the
**deployed release** and generated xDS rather than assuming support. The latest
Envoy documentation deprecates it in favor of `PATH`. Unsupported formatting
must block rollout; an approved literal omission of path is safer than falling
back to raw query logging.

These expressions preserve the existing original-path preference but omit its
query. They are query-free observations, not a cryptographic/canonical identity
or assurance about attacker-supplied path bytes. If operations explicitly want
the current normalized `:path` instead, select `%PATH(NQ:PATH)%` (or
`%REQ_WITHOUT_QUERY(:PATH)%`); the `/stats` rewrites then change the observed
path. Document that dashboard/parser change. Neither formatter modifies the
request passed to routing or the application.

Illustrative settings structure; merge the approved complete JSON field map,
do not replace entire EnvoyProxy resources with this example:

```yaml
spec:
  telemetry:
    accessLog:
      settings:
        - format:
            type: JSON
            json:
              start_time: "%START_TIME%"
              method: "%REQ(:METHOD)%"
              x-envoy-origin-path: "%PATH(NQ:ORIG_OR_PATH)%"
              response_code: "%RESPONSE_CODE%"
              route_name: "%ROUTE_NAME%"
          sinks:
            - type: File
              file:
                path: /dev/stdout
```

Verify no default/raw duplicate logger remains: custom settings replace the
default in current Gateway, but an additional empty setting can restore it.
Cover every replica, port 80 redirects, HTTPS, unmatched routes and listener
error logs, not just successful landing requests. Review other sinks and
sidecars separately. Gateway-wide logging affects observability for every
shared hostname, including `www.axonos.io` and `app.axonos.io`; obtain their
owners' approval and test dashboards/CrowdSec detection/parser compatibility.

Leave Gateway API routing, listeners, NLBs, TLS and PROXY protocol untouched.
Keep exactly `/stats/script.js` -> `/script.js` and `/stats/api/send` ->
`/api/send`, the root catch-all last, and the Umami ReferenceGrant unchanged.
Never broaden `/stats` to a prefix exposing admin/login. The log-only change
has no intended routing/cache effect, but xDS rejection or proxy replacement
can still disrupt traffic: require accepted/programmed status and staged
connectivity checks, including existing WebSocket sessions on shared hosts.
Do not use a direct plain curl to an Envoy listener as proof of health: it
requires PROXY v2. Test through the existing NLB chain; TCP health alone is
insufficient.

Primary references:
[Envoy PATH operator](https://www.envoyproxy.io/docs/envoy/latest/configuration/advanced/substitution_formatter#path-x-y-z),
[query-free formatter](https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/formatter/req_without_query/v3/req_without_query.proto),
[Gateway logging configuration](https://gateway.envoyproxy.io/docs/tasks/observability/proxy-accesslog/),
[Gateway formatter registration](https://github.com/envoyproxy/gateway/blob/main/internal/xds/translator/accesslog.go).

## 3. Audit and prevent Umami collection before real clicks

The pinned [2.20.1 tracker](https://github.com/umami-software/umami/blob/v2.20.1/src/tracker/index.js#L213)
initializes its current URL from the full browser `href` and sends URL/referrer
in analytics payloads. Its `excludeSearch` handling is in history navigation,
not initialization. A later history rewrite also copies the old URL into the
referrer variable. Therefore `data-exclude-search` or scrubbing after tracker
startup alone is insufficient for this version.

The [collector](https://github.com/umami-software/umami/blob/v2.20.1/src/app/api/send/route.ts#L136)
extracts URL query, referrer query and `twclid` separately. The
[PostgreSQL schema](https://github.com/umami-software/umami/blob/v2.20.1/db/postgresql/schema.prisma#L92)
persists `website_event.url_query`, `referrer_query` and a dedicated `twclid`
column. Custom event/session data are additional storage surfaces. This is a
source-backed risk, **not evidence that particular production rows contain
real clicks**. Check the actual served script/image; local modifications or
different builds can change behavior.

Required landing-side outcome: no tracker or unrelated script sees the raw
landing URL. Exclude capture/consent/handoff pages from analytics, or prove
scrubbing precedes tracker initialization and enforce a query/hash-free
URL/referrer plus an explicit analytics-property allowlist. Inspect titles,
event data, history transitions, errors and session data as well. Do not pass
attribution headers/tokens to Umami; the pinned collector does not automatically
map arbitrary headers into events, but wrappers/APM/custom events might.
Referrer-Policy cannot sanitize a full URL explicitly serialized into a body.

Verify the exact served JS digest, emitted POST bodies, production schema,
retention jobs/configuration, deletion permissions, replicas, exports,
dashboards and backup retention. Define approved retention; do not assume a
default TTL. If historical contamination exists, use an owner-approved scoped
purge/redaction procedure including backups/exports and record the completion
or remaining retention interval. This runbook does not authorize deletion.

For a synthetic test, a read-only PostgreSQL check (after verifying the schema)
can count matches without printing identifiers. Use a managed DB session, not
credentials in a shell argument:

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SELECT count(*) AS synthetic_marker_rows
FROM website_event
WHERE created_at >= now() - interval '1 hour'
  AND to_jsonb(website_event)::text LIKE '%' || :'privacy_marker' || '%';
SELECT count(*) AS synthetic_event_data_rows
FROM event_data
WHERE created_at >= now() - interval '1 hour'
  AND to_jsonb(event_data)::text LIKE '%' || :'privacy_marker' || '%';
SELECT count(*) AS synthetic_session_data_rows
FROM session_data
WHERE created_at >= now() - interval '1 hour'
  AND to_jsonb(session_data)::text LIKE '%' || :'privacy_marker' || '%';
COMMIT;
```

Bind `privacy_marker` with psql's `\set` to the synthetic marker only. Also check
session identity/custom stores and collector logs. Narrow by the test website
and time window on large datasets. Query timeouts/incomplete access are an
unverified result, never evidence of absence.

## 4. Synthetic verification matrix (operator-run, not executed here)

Use an isolated browser/profile and an approved test site. Block all X domains
and all third-party Pixel/tag requests. Keep production CAPI off, the dedicated
token unmounted, and worker egress disabled. Do not log into a real wallet.

Generate a non-secret marker, for example:

```sh
privacy_marker="SYNTHETIC_PRIVACY_TEST_$(openssl rand -hex 12)"
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "https://axonos.io/?twclid=${privacy_marker}"
```

The curl is a proposed approved operator probe through the actual ingress, not
a command run by this review. Repeat for `www`, HTTP redirect without automatic
following, HTTPS after redirect, 404s, both exact stats routes, duplicate keys,
mixed case, encoded names (`%74wclid`, `%2574wclid`) and malformed names. Exercise
allowed query delivery with a controlled test backend/browser assertion that
returns only a boolean/count, never echoes click values. On collector POSTs
use an approved test website; do not pollute real analytics deliberately.

Then use Chromium to test initial load, reload, history replacement, internal
navigation, app launch, consent accept/decline, GPC, revoke, cross-tab clone and
restore. Assert the address bar is scrubbed, no raw click persists in storage,
no browser request/referrer/analytics payload contains the marker except the
approved first-party capture exchange, and no X network request occurs.
This marker may intentionally fail the existing click validator: do not loosen
the provenance/shape gate to make it pass. It tests ingress leakage safely;
separate existing synthetic fixtures/mock transport test the CAPI state machine.
There is currently no implemented marketing handoff to claim as end-to-end.

For every test, record UTC window, hostname/path category and expected count.
Inspect counts of marker matches across **all** Envoy replicas, rotated stdout,
node/journald/container logs, forwarding buffers, indexed stores, object-store
archives, Next.js logs, Umami collector/DB, exception monitoring and any newly
enabled tracing/mirroring. Search marker plus percent/JSON-escaped variants.
Use restricted queries; do not export raw real-user log lines.

Zero matches alone is insufficient: prove each pipeline received contemporaneous
ordinary requests and that its retention/query window and shipping lag cover
the probes. Expected safe records should still show route/status/time, not the
query. Validate after log rotation/restart and after the maximum ingestion lag.
Run a controlled positive marker-detection check in an isolated test dataset,
never by deliberately restoring production raw logging.

Do not put the marker in User-Agent, XFF or request ID when testing query
redaction; the existing format intentionally logs those. An attacker can place
arbitrary text in logged headers/paths, so query stripping is not a universal
PII detector. Separate header/path abuse policy from the guarantee that the
normal X query is not propagated into unrelated observability.

## 5. Cache, handoff and activation conditions

The current root is Next.js-prerendered/cacheable for a year in the supplied
report. Any future capture/consent/handoff response must be dynamic and private
`no-store` in Next.js itself, with edge headers as defense in depth. An edge
header alone cannot undo a prerendered/shared response. Disable framework data
cache/revalidation for identifier-bearing processing; do not embed IDs in HTML,
cache keys, redirects, errors or tags. Keep `Referrer-Policy: no-referrer` on
sensitive pages/redirects. Capture at a controlled landing boundary is a separate
project requiring integrity-protected, consent-aware handoff approval. Neither
changing logs nor having a dedicated token proves X generated a supplied ID.
The existing application early scrub stays necessary and is not a substitute
for ingress logging controls. Do not add a Pixel to the application.

Moving later handoffs into bodies/headers cannot fix the first X-generated
query already observed by Envoy. Fix logging before any real campaign traffic.

Approval evidence must cover: query-free ingress and all downstream retention;
Umami payload/storage/retention; separately reviewed real-click provenance and
marketing handoff; consent/GPC policy; dedicated database/migration rehearsal
and least-privilege readiness; protected runtime token provisioning by an
operator; explicit canary authorization. Approval checklists are not independent
implemented environment switches: current code blocks live structurally even
with a token. Do not remove that block as part of this runbook.

Rollback: first stop campaign tests and keep public CAPI off. Apply off to the
actual worker process or stop the isolated worker; merely editing an env file
does not update its environment. Graceful stop can finish an entered iteration;
already dispatched/accepted requests cannot be recalled. A forced stop can
leave an ambiguously accepted leased job. Preserve IDs/fences/tombstones; never
promote old dry-run/off contexts, backfill, or restore mismatched DB/fence/key
snapshots. No down migration is needed for the pacing/frontend fixes. If a
logging rollout must be reverted, retain safe path omission or suspend traffic
before restoring a raw logger. Roll back landing capture independently without
changing auth, payment, session or WebRTC operation.
