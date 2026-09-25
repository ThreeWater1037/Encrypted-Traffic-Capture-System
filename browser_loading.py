"""Shared Chrome/Edge/Firefox request completion and quiet-window tracking."""

import json
import logging
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException, WebDriverException
from browser_request_policy import blocked_request_reason

log = logging.getLogger("wiki_fetcher")
RESOURCE_STALL_SECONDS = 5.0
NETWORK_IDLE_SECONDS = 2.0
RESOURCE_WAIT_LIMIT = 90.0
HIT_LEGACY_HOSTS = {"today2.hit.edu.cn", "myweb.hit.edu.cn"}


def configure_uncached_network(driver) -> None:
    """Require network responses instead of Chromium HTTP or worker caches.

    Apply to the capture target before navigation. Keep failures visible so a
    browser that cannot enforce this policy does not silently run cached.
    """
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
    driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": True})


def _performance_events(driver):
    """Supplement ChromeDriver's log with out-of-process target events."""
    firefox = vars(driver).get("_capture_firefox_network")
    if firefox is not None:
        firefox.check()
        return [{"message": json.dumps({"message": event})} for event in firefox.network_events()]
    events = driver.get_log("performance")
    policy = vars(driver).get("_capture_cache_policy")
    if policy is not None:
        policy.check()
        target_types = {item["session_id"]: item["type"]
                        for item in policy.snapshot().get("targets", [])}
        for message in policy.drain_events():
            message["capture_target_type"] = target_types.get(message.get("sessionId"))
            events.append({"message": json.dumps({"message": message})})
    return events


class NetworkIdleTracker:
    """Keep one request ledger and deadline across pre-stop rechecks."""

    def __init__(self, driver, *, idle_seconds: float = 0.5, timeout: float = 90.0,
                 completion_policy: str = "all_requests", resource_timeout: float = 10.0,
                 resource_stall_seconds: float = 3.0, deadline: float | None = None):
        self._session = _network_idle_session(
            driver, idle_seconds=idle_seconds, timeout=timeout,
            completion_policy=completion_policy, resource_timeout=resource_timeout,
            resource_stall_seconds=resource_stall_seconds, deadline=deadline)

    def wait(self) -> dict:
        # Resuming drains fresh events before examining the saved quiet window.
        # No new events means an already satisfied window returns immediately.
        return next(self._session)


def wait_for_network_idle(driver, *, idle_seconds: float = 0.5,
                          timeout: float = 90.0) -> dict:
    """Observe one quiet window; use NetworkIdleTracker to recheck later."""
    return NetworkIdleTracker(driver, idle_seconds=idle_seconds, timeout=timeout).wait()


def _network_idle_session(driver, *, idle_seconds: float, timeout: float,
                          completion_policy: str, resource_timeout: float,
                          resource_stall_seconds: float, deadline: float | None):
    """Wait for zero pending page HTTP requests and a continuous quiet interval.

    CDP performance logging or Firefox BiDi must be enabled, and startup
    events drained *before* navigation, never between navigation and this
    call. Buffered events retain monotonic timestamps, so time
    already quiet during ``driver.get`` counts toward the interval. Downloads
    remain pending until loadingFinished/loadingFailed, including cache hits.
    WebSocket/EventSource streams are excluded; ordinary XHR/fetch are included.
    The default low-level mode requires full network idle. target_document mode
    requires a completed non-cache 2xx main document, then bounds supplementary
    resource waiting without pretending those requests completed.
    The returned request ledger is suitable for writing to a JSON audit file.
    """
    if completion_policy not in {"all_requests", "target_document"}:
        raise ValueError("Unknown completion policy")
    if not all(math.isfinite(value) and value > 0 for value in (
            idle_seconds, timeout, resource_timeout, resource_stall_seconds)):
        raise ValueError("idle_seconds and timeout must be finite and positive")

    started = time.monotonic()
    target_deadline = deadline if deadline is not None else started + timeout
    target_completed_at = None
    target_document = None
    completion_reason = None
    firefox = vars(driver).get("_capture_firefox_network")
    tree = firefox.frame_tree() if firefox is not None else driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]
    main_frame = tree["frame"]["id"]
    # A known frame's loader distinguishes this navigation from old new-tab
    # requests that can finish just after the pre-navigation log drain.
    frames = {}
    parents = {}

    def add_frame_tree(node, parent=None):
        frame = node["frame"]
        frames[frame["id"]] = frame.get("loaderId")
        parents[frame["id"]] = parent
        for child in node.get("childFrames", []):
            add_frame_tree(child, frame["id"])

    add_frame_tree(tree)
    clock_offset = 0.0 if firefox is not None else None
    try:
        if firefox is None:
            driver.execute_cdp_cmd("Performance.enable", {})
            metrics = driver.execute_cdp_cmd("Performance.getMetrics", {})
        else:
            metrics = {}
        metric_time = next((item["value"] for item in metrics.get("metrics", [])
                            if item["name"] == "Timestamp"), None)
        if metric_time is not None:
            # Anchor at command receipt, not dispatch: the small command delay
            # then makes the quiet interval conservative rather than too short.
            clock_offset = time.monotonic() - metric_time
    except WebDriverException:
        # Older Chromium builds may not expose Performance metrics. Logging
        # still provides real pending counts; receipt-time waiting is safe.
        pass

    pending = {}
    requests = []
    seen_request_events = set()
    failures = []
    detached = []
    ignored_count = 0
    finished_count = 0
    redirect_count = 0
    last_activity = None

    def event_time(params, now):
        stamp = params.get("timestamp")
        if clock_offset is not None and isinstance(stamp, (int, float)):
            return stamp + clock_offset
        return now

    def activity(params, now):
        nonlocal last_activity
        value = event_time(params, now)
        last_activity = value if last_activity is None else max(last_activity, value)

    def summary(now):
        return {
            "request_count": len(requests), "finished_count": finished_count,
            "redirect_count": redirect_count, "failed_requests": failures,
            "intentionally_blocked_requests": [item.copy() for item in failures
                                               if item.get("policy_reason")],
            "pending_count": len(pending), "pending_requests": list(pending.values()),
            "detached_requests": detached, "ignored_request_count": ignored_count,
            "requests": requests, "idle_seconds": idle_seconds,
            "same_document_image_reuses": [item.copy() for item in requests
                                            if item.get("same_document_image_reuse")],
            "cache_hit_requests": [item.copy() for item in requests if (any(
                item.get(key) for key in ("served_from_cache", "from_disk_cache",
                                         "from_service_worker", "from_prefetch_cache"))
                or item.get("status") == 304)
                and not item.get("same_document_image_reuse")],
            "observed_idle_seconds": max(0.0, now - last_activity)
                if last_activity is not None else 0.0,
            "wait_seconds": now - started,
            "decision_epoch": time.time(),
            "completion_policy": completion_policy,
            "completion_reason": completion_reason,
            "target_document": target_document.copy() if target_document else None,
            "network_complete": not pending,
            "resource_status": "partial" if (pending or failures or detached or any(
                isinstance(item.get("status"), (int, float)) and item["status"] >= 400
                for item in requests)) else "complete",
            "warnings": (["Supplementary requests did not finish; target document completed"]
                         if pending and completion_reason in {"resource_stall", "resource_timeout"} else []),
            "clock_source": "bidi_monotonic_receipt" if firefox is not None else (
                "cdp_monotonic" if clock_offset is not None else "log_receipt"),
        }

    while True:
        for entry in _performance_events(driver):
            message = json.loads(entry["message"])["message"]
            method = message["method"]
            params = message.get("params", {})
            request_id = params.get("requestId")
            now = time.monotonic()

            if method == "Page.frameAttached" and params.get("parentFrameId") in frames:
                frame_id = params["frameId"]
                frames.setdefault(frame_id, None)
                parents[frame_id] = params["parentFrameId"]
            elif method == "Page.frameNavigated":
                frame = params["frame"]
                if frame["id"] in frames or frame.get("parentId") in frames:
                    frames[frame["id"]] = frame.get("loaderId")
                    parents[frame["id"]] = frame.get("parentId")
            elif method == "Page.frameDetached" and params.get("reason") != "swap":
                # A removed iframe cannot finish its requests. A process swap
                # is different: the frame and its downloads still exist.
                removed = {params.get("frameId")}
                if main_frame in removed:
                    continue
                while True:
                    descendants = {frame_id for frame_id, parent in parents.items()
                                   if parent in removed}
                    if descendants <= removed:
                        break
                    removed.update(descendants)
                for frame_id in removed:
                    frames.pop(frame_id, None)
                    parents.pop(frame_id, None)
                for key, item in list(pending.items()):
                    if item["frame_id"] in removed:
                        item["state"] = "frame_detached"
                        detached.append(item.copy())
                        pending.pop(key)
                        activity(params, now)
            elif method == "Network.requestWillBeSent":
                url = params["request"]["url"]
                # Driver logs and the cache-policy CDP session can timestamp
                # the same blocked request differently. Its identity is stable
                # even after loadingFailed has removed it from pending. Keep
                # redirect timestamps to distinguish repeated hops to one URL.
                event_key = (request_id, params.get("loaderId"), url,
                             params.get("timestamp") if params.get("redirectResponse") else None)
                if event_key in seen_request_events:
                    continue
                frame_id = params.get("frameId")
                loader_id = params.get("loaderId")
                # A document request announces a new navigation before
                # Page.frameNavigated commits its loader. This also applies
                # when an existing iframe changes src during the quiet window.
                # Subresources of an old loader must still be filtered out.
                if (params.get("type") == "Document" and frame_id in frames
                        and loader_id and url.startswith(("http://", "https://"))):
                    frames[frame_id] = loader_id
                frame_matches = frame_id in frames and (
                    not frames[frame_id] or not loader_id or frames[frame_id] == loader_id)
                loader_matches = (not frame_id and loader_id and loader_id in frames.values())
                worker_matches = message.get("capture_target_type") in {
                    "worker", "shared_worker", "service_worker"}
                if not (frame_matches or loader_matches or worker_matches):
                    continue
                if not url.startswith(("http://", "https://")):
                    continue
                if params.get("type") in {"WebSocket", "EventSource"}:
                    ignored_count += 1
                    continue
                seen_request_events.add(event_key)
                previous = pending.pop(request_id, None)
                if previous is not None and params.get("redirectResponse"):
                    previous.update(state="redirected",
                                    status=params["redirectResponse"].get("status"),
                                    finished_timestamp=params.get("timestamp"))
                    finished_count += 1
                    redirect_count += 1
                elif previous is not None:
                    # Duplicate notifications are not additional requests.
                    pending[request_id] = previous
                    continue
                item = {
                    "request_id": request_id, "url": url,
                    "type": params.get("type", "Other"), "frame_id": frame_id,
                    "loader_id": loader_id, "state": "pending",
                    "started_timestamp": params.get("timestamp"),
                }
                requests.append(item)
                pending[request_id] = item
                activity(params, now)
            elif request_id in pending:
                item = pending[request_id]
                if method == "Network.responseReceived":
                    response = params.get("response", {})
                    item.update(status=response.get("status"),
                                mime_type=response.get("mimeType"),
                                from_disk_cache=response.get("fromDiskCache", False),
                                from_service_worker=response.get("fromServiceWorker", False),
                                from_prefetch_cache=response.get("fromPrefetchCache", False))
                    # Only Firefox's adapter can authorize this exception with
                    # evidence of a completed image download in this document.
                    if firefox is not None and response.get("sameDocumentImageReuse"):
                        item["same_document_image_reuse"] = response["sameDocumentImageReuse"]
                    if params.get("type") == "EventSource":
                        item["state"] = "ignored_long_lived"
                        pending.pop(request_id)
                        ignored_count += 1
                    activity(params, now)
                elif method in {"Network.dataReceived", "Network.requestServedFromCache"}:
                    # A cache notification does not imply the body has finished.
                    if method == "Network.requestServedFromCache":
                        item["served_from_cache"] = True
                    activity(params, now)
                elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                    pending.pop(request_id)
                    item["finished_timestamp"] = params.get("timestamp")
                    if method == "Network.loadingFinished":
                        item["state"] = "redirected" if params.get("redirected") else "finished"
                        if params.get("redirected"):
                            redirect_count += 1
                        item["encoded_data_length"] = params.get("encodedDataLength")
                        finished_count += 1
                    else:
                        item.update(state="failed", error=params.get("errorText", "loadingFailed"),
                                    canceled=params.get("canceled", False))
                        for key in ("blockedReason", "corsErrorStatus"):
                            if key in params:
                                item[key] = params[key]
                        if ((item.get("blockedReason") == "inspector"
                             or item.get("error") == "net::ERR_BLOCKED_BY_CLIENT")
                                and blocked_request_reason(item["url"], vars(driver).get("_capture_blocked_urls", []))):
                            item["policy_reason"] = blocked_request_reason(
                                item["url"], vars(driver).get("_capture_blocked_urls", []))
                        failures.append(item.copy())
                    activity(params, now)

        now = time.monotonic()
        if last_activity is None:
            # With no page events yet, observe a full interval from receipt.
            last_activity = now
        if completion_policy == "target_document":
            documents = [item for item in requests
                         if item["type"] == "Document" and item["frame_id"] == main_frame]
            latest = documents[-1] if documents else None
            if latest is not target_document:
                target_document = latest
                target_completed_at = None
            if target_document:
                status = target_document.get("status")
                if (target_document["state"] == "failed"
                        or any(target_document.get(key) for key in (
                            "served_from_cache", "from_disk_cache", "from_service_worker", "from_prefetch_cache"))
                        or status == 304
                        or (isinstance(status, (int, float)) and status >= 400)):
                    exc = WebDriverException(
                        f"Target document failed: {target_document['url']} "
                        f"({target_document.get('error') or status})")
                    exc.network_idle_summary = summary(now)
                    raise exc
                if (target_document["state"] == "finished"
                        and isinstance(status, (int, float)) and 200 <= status < 300):
                    if target_completed_at is None:
                        target_completed_at = event_time(
                            {"timestamp": target_document.get("finished_timestamp")}, now)
                    if not pending and now - last_activity >= idle_seconds:
                        completion_reason = "network_idle"
                    elif now - last_activity >= resource_stall_seconds:
                        completion_reason = "resource_stall"
                    elif now - target_completed_at >= resource_timeout:
                        completion_reason = "resource_timeout"
                    else:
                        completion_reason = None
                    if completion_reason:
                        yield summary(now)
                        continue
            if target_completed_at is None and now >= target_deadline:
                exc = TimeoutException("Target document did not complete within the navigation budget")
                exc.network_idle_summary = summary(now)
                raise exc
            time.sleep(0.05)
            continue
        if not pending and now - last_activity >= idle_seconds:
            if not requests and tree["frame"].get("url", "").startswith(("http://", "https://")):
                exc = TimeoutException(
                    "No target HTTP(S) requests were observed; network observation "
                    "must be enabled and startup events drained before navigation")
                exc.network_idle_summary = summary(now)
                raise exc
            completion_reason = "network_idle"
            yield summary(now)
            continue
        if now - started >= timeout:
            result = summary(now)
            exc = TimeoutException(
                f"Network did not become idle for {idle_seconds:g}s within {timeout:g}s; "
                f"{len(pending)} request(s) still pending")
            exc.network_idle_summary = result
            raise exc
        remaining_idle = idle_seconds - (now - last_activity) if not pending else 0.05
        time.sleep(min(0.05, max(0.001, remaining_idle), max(0.001, timeout - (now - started))))


def wait_for_resources(driver, skipped: list[dict] | None = None) -> list[dict]:
    """After DOMContentLoaded, stop only when all pending resources have stalled.

    A response or a received data chunk renews the wait. Performance logging
    must be enabled before launch and pageLoadStrategy must be eager. The main
    frame's loader excludes Chrome's new-tab/background traffic.
    """
    root = driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]
    if skipped is None:
        skipped = []
    loaders = set()

    def add_loaders(tree):
        loaders.add(tree["frame"]["loaderId"])
        frames.add(tree["frame"]["id"])
        for child in tree.get("childFrames", []):
            add_loaders(child)

    frames = set()
    add_loaders(root)
    pending = {}
    started = last_activity = time.monotonic()
    while True:
        now = time.monotonic()
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            method = message["method"]
            params = message.get("params", {})
            request_id = params.get("requestId")
            if method == "Page.frameAttached" and params.get("parentFrameId") in frames:
                frames.add(params["frameId"])
            elif method == "Page.frameNavigated":
                frame = params["frame"]
                if frame.get("parentId") in frames or frame["id"] in frames:
                    frames.add(frame["id"])
                    loaders.add(frame["loaderId"])
            elif method == "Network.requestWillBeSent":
                if params.get("loaderId") not in loaders and params.get("frameId") not in frames:
                    continue
                if params.get("type") in {"WebSocket", "EventSource"}:
                    continue
                url = params["request"]["url"]
                if not url.startswith(("http://", "https://")):
                    continue
                pending[request_id] = {
                    "url": url, "type": params.get("type", "Other"), "last_progress": now,
                }
                last_activity = now
                if urlsplit(url).hostname in HIT_LEGACY_HOSTS:
                    skipped.append({"url": url, "type": params.get("type", "Other"),
                                    "reason": "isolated_legacy_host",
                                    "recorded_at": datetime.now(timezone.utc).isoformat()})
                    log.warning("    Isolated legacy resource: %s", url)
            elif request_id in pending:
                if method in {"Network.responseReceived", "Network.dataReceived"}:
                    pending[request_id]["last_progress"] = now
                    last_activity = now
                    if params.get("type") == "EventSource":
                        pending.pop(request_id)
                elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                    pending.pop(request_id)
                    last_activity = now
        if pending and all(now - item["last_progress"] >= RESOURCE_STALL_SECONDS
                           for item in pending.values()):
            stalled = [{"url": item["url"], "type": item["type"],
                        "reason": "no_progress_for_5_seconds",
                        "recorded_at": datetime.now(timezone.utc).isoformat()}
                       for item in pending.values()]
            skipped.extend(stalled)
            for item in stalled:
                log.warning("    Skipping stalled %s (no progress for 5s): %s",
                            item["type"], item["url"])
            # No actively transferring resource remains. This also cancels
            # unreachable image/CSS connections without editing page URLs.
            driver.execute_cdp_cmd("Page.stopLoading", {})
            return skipped
        if not pending and now - last_activity >= NETWORK_IDLE_SECONDS:
            return skipped
        if now - started >= RESOURCE_WAIT_LIMIT:
            # Continuous traffic must not make a batch run unbounded. Do not
            # mislabel actively downloading resources as a successful skip.
            driver.execute_cdp_cmd("Page.stopLoading", {})
            raise TimeoutException("Resource loading exceeded the 90s safety limit")
        time.sleep(0.2)
