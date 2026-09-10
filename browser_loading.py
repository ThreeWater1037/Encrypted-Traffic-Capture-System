"""Observe Chromium resource progress without imposing a five-second download limit."""

import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException

log = logging.getLogger("wiki_fetcher")
RESOURCE_STALL_SECONDS = 5.0
NETWORK_IDLE_SECONDS = 2.0
RESOURCE_WAIT_LIMIT = 90.0
HIT_LEGACY_HOSTS = {"today2.hit.edu.cn", "myweb.hit.edu.cn"}


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
