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
from browser_request_policy import bidi_block_patterns


class FirefoxNetworkPolicy(ChromiumCachePolicy):
    protocol_name = "BiDi"
    EVENTS = ["network.beforeRequestSent", "network.responseStarted",
              "network.responseCompleted", "network.fetchError",
              "browsingContext.contextCreated", "browsingContext.contextDestroyed",
              "browsingContext.navigationStarted"]

    def __init__(self, driver, *, timeout=5.0):
        super().__init__(driver, timeout=timeout)
        self._intercepts = []
        self._url_intercept = None
        self._blocked = set()
        self._blocking = set()
        self._subscription = None
        self._documents = {}
        self._observed_requests = {}
        self._downloaded_images = {}
        self._image_reuses = {}

    def _observe_image_reuse(self, method, params):
        """Allow only images fully downloaded in this observed document.

        Keep raw fromCache evidence. A new navigation (including a reload of
        the same URL), observation reset, or browser has no reusable evidence.
        """
        context = params.get("context")
        if method in {"browsingContext.navigationStarted", "browsingContext.contextDestroyed"}:
            self._documents[context] = self._documents.get(context, 0) + 1
            self._downloaded_images = {key: value for key, value in self._downloaded_images.items()
                                       if key[0] != context}
            return None
        request, response = params.get("request", {}), params.get("response", {})
        request_id = (request.get("request"), params.get("redirectCount", 0))
        scope = (context, self._documents.get(context, 0))
        url = request.get("url", "")
        if method == "network.beforeRequestSent":
            self._observed_requests[request_id] = {
                "scope": scope, "url": url, "method": request.get("method"),
                "destination": request.get("destination"), "cached": False,
            }
            return None
        observed = self._observed_requests.get(request_id)
        if not observed or observed["scope"] != scope or observed["url"] != url:
            return None
        if method == "network.fetchError":
            observed["failed"] = True
            return None
        image_request = (context is not None and observed["method"] == "GET"
                         and observed["destination"] == "image"
                         and not observed.get("failed")
                         and url.startswith(("http://", "https://")))
        cache_key = (*scope, url)
        if response.get("fromCache") or response.get("status") == 304:
            observed["cached"] = True
            source = self._downloaded_images.get(cache_key)
            if (image_request and response.get("fromCache") is True
                    and response.get("status") == 200
                    and response.get("mimeType", "").startswith("image/")
                    and source and source["request_id"] != f"{request_id[0]}:{request_id[1]}"):
                reuse = {"request_id": f"{request_id[0]}:{request_id[1]}", "url": url,
                         "context": context, "document_generation": scope[1],
                         "source_request_id": source["request_id"],
                         "source_bytes_received": source["bytes_received"]}
                self._image_reuses[request_id] = reuse
                return reuse
        elif (method == "network.responseCompleted" and image_request
              and not observed["cached"] and response.get("fromCache") is False
              and response.get("status") == 200
              and response.get("mimeType", "").startswith("image/")
              and response.get("bytesReceived", 0) > 0):
            self._downloaded_images[cache_key] = {
                "request_id": f"{request_id[0]}:{request_id[1]}",
                "bytes_received": response["bytesReceived"],
            }
        return None

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

    def set_blocked_urls(self, urls):
        """Replace exact URL exclusions; keep legacy host intercepts separate."""
        if self._url_intercept is not None:
            self._request("network.removeIntercept", {"intercept": self._url_intercept})
            self._intercepts.remove(self._url_intercept)
            self._url_intercept = None
        if urls:
            result = self._request("network.addIntercept", {
                "phases": ["beforeRequestSent"],
                "urlPatterns": bidi_block_patterns(urls)})
            self._url_intercept = result["intercept"]
            self._intercepts.append(self._url_intercept)

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
            reuse = self._observe_image_reuse(method, params)
            if reuse:
                message["capture_same_document_image_reuse"] = reuse
            if method.startswith("network."):
                self._event_count += 1
                request = params.get("request", {})
                response = params.get("response", {})
                if (response.get("fromCache") or response.get("status") == 304) and not reuse:
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
            if self._cache_hits and getattr(self, "reject_cache_hits", True):
                # BiDi reports both responseStarted and responseCompleted. The
                # event count is not a count of distinct cached resources.
                urls = list(dict.fromkeys(hit.get("url") or "<unknown>" for hit in self._cache_hits))
                samples = ", ".join(url[:240] for url in urls[:3])
                raise CachePolicyError(
                    f"Firefox observed {len(self._cache_hits)} cached response event(s) "
                    f"for {len(urls)} URL(s); sample URLs: {samples}")

    def reset_observation(self):
        self.check()
        with self._lock:
            super().reset_observation()
            self._blocked.clear()
            self._documents.clear()
            self._observed_requests.clear()
            self._downloaded_images.clear()
            self._image_reuses.clear()

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
                                      "fromDiskCache": response.get("fromCache", False),
                                      "sameDocumentImageReuse": message.get("capture_same_document_image_reuse")},
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
                    "same_document_image_reuses": list(self._image_reuses.values()),
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
