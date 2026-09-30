"""Route-contained Chromium proof for the first-party attribution bridge.

This helper is test-only.  It serves production frontend bytes from Playwright
routes and delegates API semantics to the caller; it never contacts AxonOS, X,
or another network service.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit


APP_ORIGIN = "https://app.example"
PAGE_PATH = "/vnc.html"
STATUS_PATH = "/api/x-attribution/status"
CONSENT_PATH = "/api/x-attribution/consent"
MODULE_PATH = "/app/x-attribution.js"

ApiHandler = Callable[
    [str, str, Mapping[str, str], str], Tuple[Mapping[str, Any], int]
]
GrantedCallback = Callable[[str, str], Any]

_ROOT = Path(__file__).resolve().parents[2]
_MARKER_RE = re.compile(r"^SYNTHETIC_APP_CAPTURE_[A-Za-z0-9_-]{8,180}$")
_CONTEXT_KEY = "axonos_x_attribution_context_v1"
_CAPABILITY_KEY = "axonos_x_attribution_revoke_capability_v1"
_PENDING_KEY = "axonos_x_attribution_revoke_pending_v1"
_OWNER_KEY = "axonos_x_attribution_tab_owner_v1"

# The trace contains booleans and counters only.  The temporary accessor turns
# into the same ordinary data property production creates as soon as it sees a
# non-empty assignment, so the production loader can synchronously consume and
# delete it.  No test global retains the assigned string.
_CAPTURE_TRACE_SCRIPT = r"""
(() => {
    const trace = {
        initialHrefHadOneTwclid: false,
        emptySetterCount: 0,
        nonEmptySetterCount: 0,
        setterMatchedHrefCandidate: false,
        replaceCount: 0,
        replaceSawCapturedCandidate: false,
        replaceTargetRemovedTwclid: false,
        replaceAfterClean: false,
    };
    const initial = new URL(window.location.href);
    trace.initialHrefHadOneTwclid =
        initial.searchParams.getAll('twclid').length === 1 &&
        !!initial.searchParams.get('twclid');
    Object.defineProperty(window, '__axonosCaptureTrace', {
        configurable: false, enumerable: false, writable: false, value: trace
    });

    Object.defineProperty(window, 'axonosPendingTwclid', {
        configurable: true,
        enumerable: true,
        get() { return ''; },
        set(candidate) {
            const value = String(candidate || '');
            if (!value) {
                trace.emptySetterCount += 1;
                return;
            }
            const current = new URL(window.location.href);
            trace.nonEmptySetterCount += 1;
            trace.setterMatchedHrefCandidate =
                current.searchParams.getAll('twclid').length === 1 &&
                current.searchParams.get('twclid') === value;
            Object.defineProperty(window, 'axonosPendingTwclid', {
                configurable: true, enumerable: true, writable: true, value
            });
        },
    });

    const replaceState = window.history.replaceState.bind(window.history);
    window.history.replaceState = function (state, title, target) {
        const before = new URL(window.location.href);
        const candidates = before.searchParams.getAll('twclid');
        const pending = String(window.axonosPendingTwclid || '');
        const afterTarget = new URL(String(target), before.href);
        trace.replaceCount += 1;
        trace.replaceSawCapturedCandidate =
            trace.replaceSawCapturedCandidate ||
            (candidates.length === 1 && !!candidates[0] && pending === candidates[0]);
        trace.replaceTargetRemovedTwclid =
            trace.replaceTargetRemovedTwclid ||
            afterTarget.searchParams.getAll('twclid').length === 0;
        const result = replaceState(state, title, target);
        trace.replaceAfterClean = trace.replaceAfterClean ||
            new URL(window.location.href).searchParams.getAll('twclid').length === 0;
        return result;
    };
})();
"""

_GPC_SCRIPT = r"""
Object.defineProperty(Navigator.prototype, 'globalPrivacyControl', {
    configurable: true,
    get() { return true; },
});
"""

_STORAGE_PROBE = r"""
async (needles) => {
    const contains = (value) => {
        let text = '';
        try {
            text = typeof value === 'string' ? value : JSON.stringify(value);
        } catch (_error) { return false; }
        return needles.some((needle) => needle && text.includes(needle));
    };
    const storageClean = (storage) => {
        for (let index = 0; index < storage.length; index += 1) {
            const key = storage.key(index);
            if (contains(key) || contains(storage.getItem(key))) return false;
        }
        return true;
    };
    if (typeof indexedDB.databases !== 'function' ||
        typeof caches === 'undefined' || typeof caches.keys !== 'function') {
        throw new Error('required browser storage inspection is unavailable');
    }
    const databases = await indexedDB.databases();
    const cacheNames = await caches.keys();
    let globalsClean = true;
    for (const name of Object.getOwnPropertyNames(window)) {
        try {
            if (typeof window[name] === 'string' && contains(window[name])) {
                globalsClean = false;
                break;
            }
        } catch (_error) { /* inaccessible browser globals are irrelevant */ }
    }
    return {
        localStorageClean: storageClean(localStorage),
        sessionStorageClean: storageClean(sessionStorage),
        documentCookieClean: !contains(document.cookie),
        indexedDbClean: databases.length === 0 && !contains(databases),
        indexedDbCount: databases.length,
        cacheStorageClean: cacheNames.length === 0 && !contains(cacheNames),
        cacheStorageCount: cacheNames.length,
        historyStateClean: !contains(history.state),
        windowNameClean: !contains(window.name),
        domClean: !contains(document.documentElement.outerHTML),
        globalsClean,
        pendingGlobalDeleted: typeof window.axonosPendingTwclid === 'undefined',
        urlClean: !contains(window.location.href) &&
            new URL(window.location.href).searchParams.getAll('twclid').length === 0,
        localStorageCount: localStorage.length,
        sessionStorageCount: sessionStorage.length,
        sessionStorageKeys: Array.from(
            {length: sessionStorage.length}, (_, index) => sessionStorage.key(index)
        ).sort(),
    };
}
"""


def _expiry(payload: Mapping[str, Any]) -> Optional[float]:
    raw = payload.get("expires_at", payload.get("attribution_expires_at"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _assert_clean(probe: Mapping[str, Any], label: str) -> None:
    for key in (
        "localStorageClean", "sessionStorageClean", "documentCookieClean",
        "indexedDbClean", "cacheStorageClean", "historyStateClean",
        "windowNameClean", "domClean", "globalsClean", "pendingGlobalDeleted",
        "urlClean",
    ):
        if probe.get(key) is not True:
            raise AssertionError(f"{label}: browser privacy probe failed: {key}")


def _assert_session_keys(
    probe: Mapping[str, Any], expected: set[str], label: str
) -> None:
    observed = set(probe.get("sessionStorageKeys") or ())
    if observed != expected:
        raise AssertionError(
            f"{label}: unexpected sessionStorage keys: {sorted(observed)}"
        )


def run_capture_browser(
    marker: str,
    api_handler: ApiHandler,
    on_granted: Optional[GrantedCallback] = None,
) -> Mapping[str, Any]:
    """Exercise capture, consent, first-touch, revoke, and initial GPC.

    ``api_handler`` receives ``(method, path, lowercase_headers, body_text)``
    and returns ``(response_mapping, http_status)``. ``on_granted`` runs once,
    after the actual UI has applied the granted response and before navigation
    or revocation. Returned evidence deliberately contains no marker, context,
    CSRF value, response body, or cookie; reported URLs must be query-free.
    """
    if not _MARKER_RE.fullmatch(str(marker)):
        raise ValueError("marker must be a bounded SYNTHETIC_APP_CAPTURE_ value")
    if not callable(api_handler):
        raise TypeError("api_handler must be callable")

    later_marker = f"{marker}_LATER"
    gpc_marker = f"{marker}_GPC"
    needles = (marker, later_marker, gpc_marker)
    page_source = (_ROOT / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
    bridge_source = (
        _ROOT / "novnc-theme" / "app" / "x-attribution.js"
    ).read_text(encoding="utf-8")
    boundary = "    <!-- Icons - Using AxonOS icon.png -->"
    if boundary not in page_source:
        raise AssertionError("production vnc head boundary was not found")
    production_head = page_source.split(boundary, 1)[0]
    document = production_head + """
        <script>window.axonosCoreBootReached = true;</script>
        </head><body><main id="capture-proof">capture proof</main></body></html>
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on test host
        if os.getenv("AXONOS_RUN_BROWSER_TESTS") == "1":
            raise
        raise unittest.SkipTest("Playwright is unavailable") from exc

    phase = {"name": "initial"}
    api_observations = []
    api_payloads = []
    route_errors = []
    unknown_request_count = 0
    x_request_count = 0
    unexpected_marker_transport_count = 0
    navigation_count = 0
    console_count = 0
    console_marker_count = 0
    page_error_count = 0
    page_error_marker_count = 0

    def contains_marker(value: Any) -> bool:
        text = str(value or "")
        return any(candidate in text for candidate in needles)

    def observe_console(message) -> None:
        nonlocal console_count, console_marker_count
        console_count += 1
        if contains_marker(message.text):
            console_marker_count += 1

    def observe_page_error(error) -> None:
        nonlocal page_error_count, page_error_marker_count
        page_error_count += 1
        if contains_marker(error):
            page_error_marker_count += 1

    def route_request(route) -> None:
        nonlocal unknown_request_count, x_request_count
        nonlocal unexpected_marker_transport_count, navigation_count
        request = route.request
        parsed = urlsplit(request.url)
        host = (parsed.hostname or "").lower()
        headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        body = request.post_data or ""

        if host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com"):
            x_request_count += 1
            route.abort()
            return
        if request.is_navigation_request() and host == "app.example" and parsed.path == PAGE_PATH:
            navigation_count += 1
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                body=document,
            )
            return

        landing_header = headers.get("x-axonos-landing-click", "")
        other_headers = {
            key: value for key, value in headers.items()
            if key != "x-axonos-landing-click"
        }
        if contains_marker(request.url) or contains_marker(body) or contains_marker(other_headers):
            unexpected_marker_transport_count += 1

        if host == "app.example" and parsed.path == MODULE_PATH and parsed.query == "v=5":
            route.fulfill(
                status=200,
                content_type="application/javascript; charset=utf-8",
                headers={"Cache-Control": "no-store"},
                body=bridge_source,
            )
            return
        if host == "app.example" and parsed.path in (STATUS_PATH, CONSENT_PATH):
            action = ""
            try:
                if body:
                    action = str((json.loads(body) or {}).get("action") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                action = "invalid-json"
            expected_marker = marker if phase["name"] in ("initial", "grant") else ""
            api_observations.append({
                "phase": phase["name"],
                "method": request.method,
                "path": parsed.path,
                "action": action,
                "has_landing": bool(landing_header),
                "landing_matches": bool(expected_marker) and landing_header == expected_marker,
                "has_context": bool(headers.get("x-axonos-attribution")),
                "frame_url_clean": not contains_marker(request.frame.url),
            })
            try:
                payload, status = api_handler(
                    request.method, parsed.path, headers, body
                )
                if not isinstance(payload, Mapping):
                    raise TypeError("api_handler payload must be a mapping")
                status = int(status)
                payload_copy = dict(payload)
                api_payloads.append((phase["name"], parsed.path, action, payload_copy))
                route.fulfill(
                    status=status,
                    content_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                    body=json.dumps(payload_copy, separators=(",", ":")),
                )
            except BaseException as exc:  # surface the adapter failure after routing
                route_errors.append(exc)
                route.fulfill(
                    status=500,
                    content_type="application/json; charset=utf-8",
                    body='{"ok":false}',
                )
            return

        unknown_request_count += 1
        route.abort()

    def matching_calls(*, call_phase: str, path: str, action: str = ""):
        return [
            item for item in api_observations
            if item["phase"] == call_phase and item["path"] == path and
            item["action"] == action
        ]

    def matching_payload(*, call_phase: str, path: str, action: str = ""):
        matches = [
            payload for seen_phase, seen_path, seen_action, payload in api_payloads
            if seen_phase == call_phase and seen_path == path and seen_action == action
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one {call_phase} {path} {action!r} response, got {len(matches)}"
            )
        return matches[0]

    def raise_route_error() -> None:
        if route_errors:
            raise AssertionError("api_handler failed during browser proof") from route_errors[0]

    def wait(page, expression: str, message: str) -> None:
        try:
            page.wait_for_function(expression, timeout=5000)
        except BaseException:
            raise_route_error()
            raise AssertionError(message)
        raise_route_error()

    def trace_for(page) -> Mapping[str, Any]:
        return page.evaluate("() => ({...window.__axonosCaptureTrace})")

    def privacy_probe(page, active_needles) -> Mapping[str, Any]:
        return page.evaluate(_STORAGE_PROBE, list(active_needles))

    def cookies_clean(context, active_needles) -> bool:
        return not any(
            any(candidate in json.dumps(cookie, sort_keys=True) for candidate in active_needles)
            for cookie in context.cookies()
        )

    callback_evidence = None
    probes = []
    traces = {}
    lifetime_copy_authoritative = False
    expiry_unchanged = False
    public_context_after_grant = False
    revoked_context_cleared = False
    initial_gpc_blocked = False
    restored_context_matches_grant = False
    visible_urls = {}
    session_keys = {}

    playwright = sync_playwright().start()
    browser = None
    try:
        try:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        except BaseException as exc:  # pragma: no cover - depends on test host
            if os.getenv("AXONOS_RUN_BROWSER_TESTS") == "1":
                raise
            raise unittest.SkipTest(f"Chromium is unavailable: {exc}") from exc

        context = browser.new_context(service_workers="block")
        try:
            context.add_init_script(script=_CAPTURE_TRACE_SCRIPT)
            context.route("**/*", route_request)
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on("console", observe_console)
            page.on("pageerror", observe_page_error)

            phase["name"] = "initial"
            page.goto(
                f"{APP_ORIGIN}{PAGE_PATH}?twclid={quote(marker, safe='')}",
                wait_until="domcontentloaded",
                timeout=5000,
            )
            wait(page, "window.axonosCoreBootReached === true", "core sentinel did not run")
            wait(
                page,
                "Array.from(document.querySelectorAll('button')).some("
                "button => button.textContent === 'Allow sharing')",
                "the actual unset consent UI did not render",
            )
            initial_calls = matching_calls(call_phase="initial", path=STATUS_PATH)
            if len(initial_calls) != 1 or not initial_calls[0]["landing_matches"]:
                raise AssertionError("initial status did not receive the exact captured marker")
            if not initial_calls[0]["frame_url_clean"]:
                raise AssertionError("initial status began before the visible URL was scrubbed")
            traces["initial"] = trace_for(page)
            if not all((
                traces["initial"].get("initialHrefHadOneTwclid"),
                traces["initial"].get("nonEmptySetterCount") == 1,
                traces["initial"].get("setterMatchedHrefCandidate"),
                traces["initial"].get("replaceSawCapturedCandidate"),
                traces["initial"].get("replaceTargetRemovedTwclid"),
                traces["initial"].get("replaceAfterClean"),
            )):
                raise AssertionError("capture-before-scrub ordering was not observed")
            if page.evaluate("window.axonosAttributionContext") != "":
                raise AssertionError("unset lifecycle exposed a business context")
            if page.evaluate(
                f"sessionStorage.getItem({json.dumps(_CONTEXT_KEY)}) !== null || "
                f"sessionStorage.getItem({json.dumps(_CAPABILITY_KEY)}) !== null"
            ):
                raise AssertionError("unset lifecycle was persisted before consent")
            initial_probe = privacy_probe(page, (marker,))
            _assert_clean(initial_probe, "initial unset")
            _assert_session_keys(initial_probe, {_OWNER_KEY}, "initial unset")
            probes.append(initial_probe)
            session_keys["initial_unset"] = initial_probe["sessionStorageKeys"]
            visible_urls["initial_unset"] = page.url
            if page.url != f"{APP_ORIGIN}{PAGE_PATH}":
                raise AssertionError("initial visible URL retained query state")
            if not cookies_clean(context, (marker,)):
                raise AssertionError("initial marker entered a browser cookie")

            initial_payload = matching_payload(call_phase="initial", path=STATUS_PATH)
            initial_expiry = _expiry(initial_payload)
            initial_ttl = initial_payload.get("attribution_ttl_days")
            copy_text = page.locator("#axonos-x-consent p").first.text_content() or ""
            if initial_expiry is not None:
                localized_expiry = page.evaluate(
                    "epoch => new Date(epoch * 1000).toLocaleString()", initial_expiry
                )
                lifetime_copy_authoritative = (
                    localized_expiry in copy_text and
                    "without extending that deadline" in copy_text
                )
            elif isinstance(initial_ttl, (int, float)) and initial_ttl > 0:
                lifetime_copy_authoritative = (
                    f"for at most {initial_ttl:g} day" in copy_text and
                    "without renewal" in copy_text
                )
            if not lifetime_copy_authoritative:
                raise AssertionError("consent UI did not render the server lifetime")

            phase["name"] = "grant"
            page.get_by_role("button", name="Allow sharing", exact=True).click()
            wait(
                page,
                "typeof window.axonosAttributionContext === 'string' && "
                "window.axonosAttributionContext.length >= 32",
                "grant did not publish the actual business context",
            )
            grant_calls = matching_calls(
                call_phase="grant", path=CONSENT_PATH, action="grant"
            )
            if len(grant_calls) != 1 or not grant_calls[0]["landing_matches"]:
                raise AssertionError("grant did not receive the exact captured marker")
            if not grant_calls[0]["frame_url_clean"]:
                raise AssertionError("grant request observed the pre-scrub URL")
            grant_payload = matching_payload(
                call_phase="grant", path=CONSENT_PATH, action="grant"
            )
            grant_context = str(grant_payload.get("context") or "")
            grant_csrf = str(grant_payload.get("csrf") or "")
            if len(grant_context) < 32 or len(grant_csrf) < 32:
                raise AssertionError("grant response omitted its opaque context or CSRF")
            public_context_after_grant = (
                page.evaluate("window.axonosAttributionContext") == grant_context
            )
            if not public_context_after_grant:
                raise AssertionError("browser published a context other than the grant result")
            grant_expiry = _expiry(grant_payload)
            if initial_expiry is not None and grant_expiry != initial_expiry:
                raise AssertionError("grant renewed the initial attribution expiry")
            if on_granted is not None:
                callback_evidence = on_granted(grant_context, grant_csrf)
            grant_probe = privacy_probe(page, (marker,))
            _assert_clean(grant_probe, "granted")
            _assert_session_keys(
                grant_probe,
                {_OWNER_KEY, _CONTEXT_KEY, _CAPABILITY_KEY},
                "granted",
            )
            probes.append(grant_probe)
            session_keys["granted"] = grant_probe["sessionStorageKeys"]
            visible_urls["granted"] = page.url
            if not cookies_clean(context, (marker,)):
                raise AssertionError("granted marker entered a browser cookie")

            phase["name"] = "reload"
            page.goto(
                f"{APP_ORIGIN}{PAGE_PATH}?twclid={quote(later_marker, safe='')}",
                wait_until="domcontentloaded",
                timeout=5000,
            )
            wait(
                page,
                "window.axonosAttributionReady === true && "
                "typeof window.axonosAttributionContext === 'string' && "
                "window.axonosAttributionContext.length >= 32",
                "granted lifecycle did not restore after same-tab navigation",
            )
            reload_calls = matching_calls(call_phase="reload", path=STATUS_PATH)
            if len(reload_calls) != 1:
                raise AssertionError("restored lifecycle did not perform exactly one status check")
            if reload_calls[0]["has_landing"] or not reload_calls[0]["has_context"]:
                raise AssertionError("later click replaced the first touch on restored status")
            if not reload_calls[0]["frame_url_clean"]:
                raise AssertionError("restored status began before the later URL was scrubbed")
            traces["reload"] = trace_for(page)
            if not all((
                traces["reload"].get("initialHrefHadOneTwclid"),
                traces["reload"].get("nonEmptySetterCount") == 1,
                traces["reload"].get("setterMatchedHrefCandidate"),
                traces["reload"].get("replaceSawCapturedCandidate"),
                traces["reload"].get("replaceAfterClean"),
            )):
                raise AssertionError("later click did not follow the canonical capture/scrub path")
            reload_payload = matching_payload(call_phase="reload", path=STATUS_PATH)
            reload_expiry = _expiry(reload_payload)
            restored_context_matches_grant = (
                page.evaluate("window.axonosAttributionContext") == grant_context and
                str(reload_payload.get("context") or "") == grant_context
            )
            if not restored_context_matches_grant:
                raise AssertionError("restored first touch changed its granted context")
            expiry_unchanged = (
                initial_expiry is not None and grant_expiry == initial_expiry and
                reload_expiry == initial_expiry
            )
            if not expiry_unchanged:
                raise AssertionError("same-tab navigation renewed attribution expiry")
            reload_probe = privacy_probe(page, (marker, later_marker))
            _assert_clean(reload_probe, "restored first touch")
            _assert_session_keys(
                reload_probe,
                {_OWNER_KEY, _CONTEXT_KEY, _CAPABILITY_KEY},
                "restored first touch",
            )
            probes.append(reload_probe)
            session_keys["restored_first_touch"] = reload_probe["sessionStorageKeys"]
            visible_urls["restored_first_touch"] = page.url
            if page.url != f"{APP_ORIGIN}{PAGE_PATH}":
                raise AssertionError("later-click visible URL retained query state")
            if not cookies_clean(context, (marker, later_marker)):
                raise AssertionError("later marker entered a browser cookie")

            phase["name"] = "revoke"
            page.get_by_role("button", name="Conversion sharing", exact=True).click()
            page.get_by_role("button", name="Revoke sharing", exact=True).click()
            wait(
                page,
                f"window.axonosAttributionContext === '' && "
                f"sessionStorage.getItem({json.dumps(_PENDING_KEY)}) === null",
                "confirmed UI revocation did not clear public/pending state",
            )
            revoke_calls = matching_calls(
                call_phase="revoke", path=CONSENT_PATH, action="revoke"
            )
            if len(revoke_calls) != 1 or revoke_calls[0]["has_landing"]:
                raise AssertionError("explicit revocation sent a landing identifier")
            revoked_context_cleared = (
                page.evaluate("window.axonosAttributionContext") == "" and
                page.evaluate(
                    f"sessionStorage.getItem({json.dumps(_CAPABILITY_KEY)})"
                ) is None
            )
            if not revoked_context_cleared:
                raise AssertionError("confirmed revoke retained public/revoke capability state")
            revoke_probe = privacy_probe(page, (marker, later_marker))
            _assert_clean(revoke_probe, "revoked")
            _assert_session_keys(
                revoke_probe, {_OWNER_KEY, _CONTEXT_KEY}, "revoked"
            )
            probes.append(revoke_probe)
            session_keys["revoked"] = revoke_probe["sessionStorageKeys"]
            visible_urls["revoked"] = page.url
        finally:
            context.close()

        api_count_before_gpc = len(api_observations)
        gpc_context = browser.new_context(service_workers="block")
        try:
            gpc_context.add_init_script(script=_CAPTURE_TRACE_SCRIPT)
            gpc_context.add_init_script(script=_GPC_SCRIPT)
            gpc_context.route("**/*", route_request)
            gpc_page = gpc_context.new_page()
            gpc_page.set_default_timeout(5000)
            gpc_page.on("console", observe_console)
            gpc_page.on("pageerror", observe_page_error)
            phase["name"] = "gpc"
            gpc_page.goto(
                f"{APP_ORIGIN}{PAGE_PATH}?twclid={quote(gpc_marker, safe='')}",
                wait_until="domcontentloaded",
                timeout=5000,
            )
            wait(
                gpc_page,
                "window.axonosCoreBootReached === true && "
                "window.axonosAttributionReady === true",
                "initial-GPC bridge did not complete locally",
            )
            traces["gpc"] = trace_for(gpc_page)
            initial_gpc_blocked = (
                traces["gpc"].get("initialHrefHadOneTwclid") is True and
                traces["gpc"].get("nonEmptySetterCount") == 0 and
                traces["gpc"].get("replaceAfterClean") is True and
                len(api_observations) == api_count_before_gpc and
                gpc_page.evaluate("window.axonosAttributionContext") == "" and
                gpc_page.get_by_role("button", name="Allow sharing", exact=True).count() == 0
            )
            if not initial_gpc_blocked:
                raise AssertionError("initial GPC did not prevent capture/consent/network state")
            gpc_probe = privacy_probe(gpc_page, (gpc_marker,))
            _assert_clean(gpc_probe, "initial GPC")
            _assert_session_keys(gpc_probe, set(), "initial GPC")
            probes.append(gpc_probe)
            session_keys["initial_gpc"] = gpc_probe["sessionStorageKeys"]
            visible_urls["initial_gpc"] = gpc_page.url
            if gpc_page.url != f"{APP_ORIGIN}{PAGE_PATH}":
                raise AssertionError("GPC visible URL retained query state")
            if not cookies_clean(gpc_context, (gpc_marker,)):
                raise AssertionError("GPC-blocked marker entered a browser cookie")
        finally:
            gpc_context.close()
    finally:
        if browser is not None:
            browser.close()
        playwright.stop()

    raise_route_error()
    if x_request_count or unknown_request_count or unexpected_marker_transport_count:
        raise AssertionError("browser proof attempted an unapproved or identifier-bearing request")
    if console_marker_count or page_error_marker_count:
        raise AssertionError("raw marker appeared in browser console/error output")

    counts = Counter(
        f"{item['phase']}:{item['method']}:{item['path']}:{item['action']}"
        for item in api_observations
    )
    return {
        "capture_before_scrub": True,
        "actual_module_received_marker": True,
        "unset_context_ephemeral": True,
        "public_context_after_grant": public_context_after_grant,
        "later_click_did_not_replace_first_touch": True,
        "restored_context_matches_grant": restored_context_matches_grant,
        "expiry_unchanged": expiry_unchanged,
        "explicit_revoke_cleared_context": revoked_context_cleared,
        "initial_gpc_blocked": initial_gpc_blocked,
        "storage_marker_clean": all(
            all(
                probe.get(key) is True for key in (
                    "localStorageClean", "sessionStorageClean", "documentCookieClean",
                    "indexedDbClean", "cacheStorageClean", "historyStateClean",
                    "windowNameClean", "domClean", "globalsClean",
                )
            ) for probe in probes
        ),
        "clean_urls": all(probe.get("urlClean") is True for probe in probes),
        "visible_urls": dict(sorted(visible_urls.items())),
        "session_storage_keys": dict(sorted(session_keys.items())),
        "lifetime_copy_authoritative": lifetime_copy_authoritative,
        "api_call_counts": dict(sorted(counts.items())),
        "navigation_count": navigation_count,
        "unknown_request_count": unknown_request_count,
        "x_request_count": x_request_count,
        "console_count": console_count,
        "console_marker_count": console_marker_count,
        "page_error_count": page_error_count,
        "page_error_marker_count": page_error_marker_count,
        "callback_evidence": callback_evidence,
    }
