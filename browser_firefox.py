"""Firefox BiDi adapter for the shared capture request ledger.

The transport is shared with Chromium's policy; no CDP commands are sent to
Firefox. HTTP cache bypass is session-wide. Service Workers are disabled in
the fresh capture profile because BiDi has no Service Worker bypass command.
"""

import threading
import time
from urllib.parse import urlsplit, urlunsplit

import websocket

from browser_cache import CachePolicyError, ChromiumCachePolicy


class FirefoxNetworkPolicy(ChromiumCachePolicy):
    protocol_name = "BiDi"
    EVENTS = ["network.beforeRequestSent", "network.responseStarted",
              "network.responseCompleted", "network.fetchError",
              "browsingContext.contextCreated", "browsingContext.contextDestroyed"]

    def __init__(self, driver, *, timeout=5.0):
        super().__init__(driver, timeout=timeout)
        self._intercepts = []
        self._blocked = set()
        self._blocking = set()
        self._subscription = None

    def _debugger_url(self):
        endpoint = urlsplit(self.driver.capabilities.get("webSocketUrl", ""))
        if endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or not endpoint.port:
            raise CachePolicyError("Firefox requires a loopback WebDriver BiDi webSocketUrl")
        return urlunsplit(("ws", f"127.0.0.1:{endpoint.port}", endpoint.path, endpoint.query, ""))

    def start(self):
        if self._started:
            self.check()
            return self
        if self._closed:
            raise CachePolicyError("A closed Firefox network policy cannot restart")
        try:
            self._socket = websocket.create_connection(
                self._debugger_url(), timeout=self.timeout, suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"])
            self._socket.settimeout(0.25)
            self._connected = True
            self._receiver = threading.Thread(target=self._receive_loop, name="capture-firefox-bidi", daemon=True)
            self._initializer = threading.Thread(target=self._initialize_loop, name="capture-firefox-blocks", daemon=True)
            self._receiver.start()
            self._initializer.start()
            # Omit contexts to cover current/future frames and worker requests.
            self._subscription = self._request("session.subscribe", {"events": self.EVENTS})["subscription"]
            self._request("network.setCacheBehavior", {"cacheBehavior": "bypass"})
            self._started = True
            self.check()
            return self
        except Exception:
            self.close()
            raise

    def block_hosts(self, hosts):
        if not hosts:
            return
        result = self._request("network.addIntercept", {
            "phases": ["beforeRequestSent"],
            "urlPatterns": [{"type": "pattern", "protocol": scheme, "hostname": host}
                            for host in sorted(hosts) for scheme in ("http", "https")]})
        self._intercepts.append(result["intercept"])

    def _initialize_loop(self):
        # Never await protocol replies on the receiver thread.
        while True:
            request_id = self._initializations.get()
            if request_id is None:
                return
            try:
                self._request("network.failRequest", {"request": request_id})
            except Exception as exc:
                self._record_error(exc)
            finally:
                with self._condition:
                    self._blocking.discard(request_id)
                    self._condition.notify_all()

    def _handle_event(self, message):
        method, params = message.get("method", ""), message.get("params", {})
        # Receipt time is monotonic and conservative; a delayed event never
        # shortens the quiet window. Retain it across navigation and rechecks.
        message["capture_timestamp"] = time.monotonic()
        with self._condition:
            self._events.append(message)
            if method.startswith("network."):
                self._event_count += 1
                request = params.get("request", {})
                response = params.get("response", {})
                if response.get("fromCache") or response.get("status") == 304:
                    self._cache_hits.append({"request_id": request.get("request"),
                                             "url": request.get("url"),
                                             "reasons": ["fromCache" if response.get("fromCache") else "HTTP304"]})
                if method == "network.beforeRequestSent" and params.get("isBlocked"):
                    request_id = request["request"]
                    self._blocked.add((request_id, params.get("redirectCount", 0)))
                    self._blocking.add(request_id)
                    self._initializations.put(request_id)

    def check(self):
        self._request("session.status", {})
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while self._blocking:
                self._raise_errors()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CachePolicyError("Timed out rejecting intercepted Firefox requests")
                self._condition.wait(min(remaining, 0.1))
            self._raise_errors()
            if self._cache_hits:
                raise CachePolicyError(f"Firefox observed {len(self._cache_hits)} cached response event(s)")

    def reset_observation(self):
        self.check()
        super().reset_observation()
        with self._lock:
            self._blocked.clear()

    def frame_tree(self):
        result = self._request("browsingContext.getTree", {"root": self.driver.current_window_handle})

        def convert(context):
            return {"frame": {"id": context["context"], "url": context.get("url", "")},
                    "childFrames": [convert(child) for child in context.get("children") or []]}

        return convert(result["contexts"][0])

    def network_events(self):
        """Normalize BiDi into the shared ledger's event vocabulary."""
        result = []
        for message in self.drain_events():
            method, params = message.get("method"), message.get("params", {})
            stamp = message["capture_timestamp"]
            if method == "browsingContext.contextCreated":
                if params.get("parent"):
                    result.append({"method": "Page.frameAttached", "params": {
                        "frameId": params["context"], "parentFrameId": params["parent"]}})
                continue
            if method == "browsingContext.contextDestroyed":
                result.append({"method": "Page.frameDetached", "params": {
                    "frameId": params["context"], "reason": "remove"}})
                continue
            if not method.startswith("network."):
                continue
            request = params["request"]
            hop = params.get("redirectCount", 0)
            request_id = f"{request['request']}:{hop}"
            base = {"requestId": request_id, "timestamp": stamp}
            if method == "network.beforeRequestSent":
                kind = {"document": "Document", "iframe": "Document", "image": "Image",
                        "script": "Script", "style": "Stylesheet", "xmlhttprequest": "XHR",
                        "fetch": "Fetch"}.get(request.get("destination") or request.get("initiatorType"), "Other")
                base.update(frameId=params.get("context"), loaderId="", type=kind,
                            request={"url": request["url"]})
                normalized = "Network.requestWillBeSent"
            elif method == "network.responseStarted":
                response = params["response"]
                base.update(response={"status": response["status"], "mimeType": response.get("mimeType"),
                                      "fromDiskCache": response.get("fromCache", False)},
                            type="EventSource" if response.get("mimeType", "").split(";")[0] == "text/event-stream" else "Other")
                normalized = "Network.responseReceived"
            elif method == "network.responseCompleted":
                response = params["response"]
                base.update(encodedDataLength=response.get("bytesReceived", 0),
                            redirected=300 <= response["status"] < 400 and response["status"] != 304)
                normalized = "Network.loadingFinished"
            elif method == "network.fetchError":
                base["errorText"] = params.get("errorText", "fetchError")
                if (request["request"], hop) in self._blocked:
                    base["blockedReason"] = "inspector"
                normalized = "Network.loadingFailed"
            else:
                continue
            result.append({"method": normalized, "params": base,
                           "capture_target_type": "worker" if params.get("context") is None else "page"})
        return result

    def snapshot(self):
        with self._lock:
            return {"enabled": self._started, "connected": self._connected,
                    "protocol": "webdriver_bidi", "http_cache": "bypass",
                    "service_worker_policy": "disabled_in_profile",
                    "errors": list(self._errors), "cache_hits": list(self._cache_hits),
                    "network_event_count": self._event_count,
                    "pending_intercept_count": len(self._blocking)}

    def close(self):
        if self._closed:
            return
        # Release intercepts before closing the only connection that handles them.
        if self._connected:
            for intercept in self._intercepts:
                try:
                    self._request("network.removeIntercept", {"intercept": intercept}, timeout=0.5)
                except Exception as exc:
                    self._record_error(exc)
            if self._subscription:
                try:
                    self._request("session.unsubscribe", {"subscriptions": [self._subscription]}, timeout=0.5)
                except Exception as exc:
                    self._record_error(exc)
        self._closing = self._closed = True
        self._initializations.put(None)
        if self._socket:
            self._socket.close(timeout=0.2)
        for thread in (self._receiver, self._initializer):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._connected = False
