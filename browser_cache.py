"""Apply Chromium network-cache policy before each attached target can run."""

from collections import deque
import json
import queue
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket


class CachePolicyError(RuntimeError):
    """A browser target could not be covered by the required cache policy."""


class ChromiumCachePolicy:
    """Own an independent CDP connection covering pages, OOPIFs, and workers.

    A receive thread dispatches protocol replies while a separate initializer
    configures paused new targets. This avoids waiting for a reply inside the
    only thread able to receive it. The caller owns start/check/close lifecycle.
    """

    NETWORK_TARGETS = {"page", "iframe", "worker", "shared_worker", "service_worker"}
    TARGET_FILTER = [{"type": kind, "exclude": False} for kind in sorted(NETWORK_TARGETS)] + [
        {"exclude": True}
    ]

    def __init__(self, driver, *, timeout=5.0):
        self.driver = driver
        self.timeout = timeout
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = {}
        self._next_id = 0
        self._targets = {}
        self._errors = []
        self._events = deque()
        self._event_count = 0
        self._request_urls = {}
        self._cache_hits = []
        self._initializations = queue.Queue()
        self._socket = None
        self._receiver = None
        self._initializer = None
        self._connected = False
        self._started = False
        self._closing = False
        self._closed = False

    def _debugger_url(self):
        capabilities = self.driver.capabilities
        address = next((capabilities.get(name, {}).get("debuggerAddress")
                        for name in ("ms:edgeOptions", "goog:chromeOptions")
                        if capabilities.get(name, {}).get("debuggerAddress")), None)
        if not address:
            raise CachePolicyError("Chromium did not expose its local debugger address")
        parsed = urlsplit("http://" + address)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port:
            raise CachePolicyError("Chromium cache policy requires a loopback debugger address")
        # Avoid proxy environment variables and localhost IPv6 fallback delays.
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{parsed.port}/json/version", timeout=self.timeout) as response:
            debugger_url = json.load(response)["webSocketDebuggerUrl"]
        endpoint = urlsplit(debugger_url)
        if endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CachePolicyError("Unexpected non-loopback browser debugger WebSocket")
        return f"ws://127.0.0.1:{endpoint.port}{endpoint.path}"

    def start(self):
        if self._started:
            self.check()
            return self
        if self._closed:
            raise CachePolicyError("A closed Chromium cache policy cannot restart")
        try:
            self._socket = websocket.create_connection(
                self._debugger_url(), timeout=self.timeout, suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
            )
            self._socket.settimeout(0.25)
            self._connected = True
            self._receiver = threading.Thread(target=self._receive_loop, name="capture-cache-cdp", daemon=True)
            self._initializer = threading.Thread(target=self._initialize_loop, name="capture-cache-targets", daemon=True)
            self._receiver.start()
            self._initializer.start()
            self._request("Target.setAutoAttach", self._auto_attach(True))
            existing = self._request("Target.getTargets", {})["targetInfos"]
            required = {item["targetId"] for item in existing if item["type"] in self.NETWORK_TARGETS}
            deadline = time.monotonic() + self.timeout
            with self._condition:
                while required - {item["target_id"] for item in self._targets.values()
                                  if item["initialized"] or item["detached"]}:
                    self._raise_errors()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CachePolicyError("Timed out covering existing browser targets")
                    self._condition.wait(min(remaining, 0.1))
            self._started = True
            self.check()
            return self
        except Exception:
            self.close()
            raise

    def _auto_attach(self, enabled):
        return {"autoAttach": enabled, "waitForDebuggerOnStart": enabled,
                "flatten": True, "filter": self.TARGET_FILTER}

    def _request(self, method, params, session_id=None, *, timeout=None):
        with self._lock:
            if not self._connected:
                raise CachePolicyError("Browser debugger connection is closed")
            self._next_id += 1
            message_id = self._next_id
            item = {"event": threading.Event(), "reply": None}
            self._pending[message_id] = item
        message = {"id": message_id, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        try:
            with self._send_lock:
                self._socket.send(json.dumps(message))
            if not item["event"].wait(self.timeout if timeout is None else timeout):
                raise CachePolicyError(f"CDP {method} timed out")
            reply = item["reply"]
            if "error" in reply:
                raise CachePolicyError(f"CDP {method}: {reply['error']}")
            return reply.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(message_id, None)

    def _record_error(self, message):
        with self._condition:
            if not self._closing:
                self._errors.append(str(message))
            self._condition.notify_all()

    def _receive_loop(self):
        failure = "Browser debugger connection closed"
        try:
            while not self._closing:
                try:
                    raw = self._socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                message = json.loads(raw)
                if "id" in message:
                    with self._lock:
                        item = self._pending.get(message["id"])
                        if item:
                            item["reply"] = message
                            item["event"].set()
                    continue
                self._handle_event(message)
        except Exception as exc:
            failure = f"Browser debugger receive failed: {exc}"
        finally:
            with self._condition:
                self._connected = False
                for item in self._pending.values():
                    item["reply"] = {"error": failure}
                    item["event"].set()
                if not self._closing:
                    self._errors.append(failure)
                self._condition.notify_all()

    def _handle_event(self, message):
        method = message.get("method", "")
        params = message.get("params", {})
        if method == "Target.attachedToTarget":
            session_id = params["sessionId"]
            info = params["targetInfo"]
            with self._condition:
                if session_id not in self._targets:
                    self._targets[session_id] = {
                        "session_id": session_id, "target_id": info["targetId"],
                        "type": info["type"], "url": info.get("url", ""),
                        "initialized": False, "detached": False,
                        "paused": params.get("waitingForDebugger", False), "error": None,
                    }
                    self._initializations.put(session_id)
                self._condition.notify_all()
        elif method == "Target.detachedFromTarget":
            with self._condition:
                target = self._targets.get(params.get("sessionId"))
                if target:
                    target["detached"] = True
                    target["paused"] = False
                self._condition.notify_all()
        elif method.startswith("Page."):
            with self._lock:
                self._events.append(message)
        elif method.startswith("Network."):
            session_id = message.get("sessionId")
            request_id = params.get("requestId")
            key = (session_id, request_id)
            with self._lock:
                self._events.append(message)
                self._event_count += 1
                if method == "Network.requestWillBeSent":
                    self._request_urls[key] = params["request"]["url"]
                response = params.get("response") or params.get("redirectResponse") or {}
                url = response.get("url") or self._request_urls.get(key, "")
                reasons = []
                if method == "Network.requestServedFromCache":
                    reasons.append("requestServedFromCache")
                if method in {"Network.responseReceived", "Network.requestWillBeSent"}:
                    reasons.extend(name for name in ("fromDiskCache", "fromPrefetchCache", "fromServiceWorker")
                                   if response.get(name))
                if method == "Network.responseReceivedExtraInfo" and params.get("statusCode") == 304:
                    reasons.append("HTTP304")
                if reasons and (not url or url.startswith(("http://", "https://"))):
                    self._cache_hits.append({"session_id": session_id, "request_id": request_id,
                                             "url": url, "reasons": reasons})

    def _initialize_loop(self):
        while True:
            session_id = self._initializations.get()
            if session_id is None:
                return
            with self._lock:
                target = self._targets[session_id]
                if target["detached"] or self._closing:
                    continue
            error = None
            try:
                if target["type"] in {"page", "iframe"}:
                    self._request("Page.enable", {}, session_id)
                self._request("Network.enable", {}, session_id)
                self._request("Network.setCacheDisabled", {"cacheDisabled": True}, session_id)
                self._request("Network.setBypassServiceWorker", {"bypass": True}, session_id)
                self._request("Target.setAutoAttach", self._auto_attach(True), session_id)
            except Exception as exc:
                error = str(exc)
            finally:
                # A policy failure must never leave a target indefinitely
                # paused. The caller will reject this URL through check().
                try:
                    if not target["detached"] and self._connected:
                        self._request("Runtime.runIfWaitingForDebugger", {}, session_id)
                except Exception as exc:
                    error = error or str(exc)
                with self._condition:
                    target["paused"] = False
                    target["initialized"] = error is None
                    if error and not target["detached"] and not self._closing:
                        target["error"] = error
                        self._errors.append(f"{target['type']} {target['target_id']}: {error}")
                    self._condition.notify_all()

    def _raise_errors(self):
        if self._errors:
            raise CachePolicyError("; ".join(self._errors))
        if not self._connected:
            raise CachePolicyError("Browser debugger connection is closed")

    def check(self):
        """Wait for target initialization; fail when coverage or cache purity failed."""
        # A protocol round trip also drains events queued before this check.
        self._request("Target.getTargets", {})
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while True:
                self._raise_errors()
                pending = [item for item in self._targets.values()
                           if not item["initialized"] and not item["detached"]]
                if not pending:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CachePolicyError("Timed out initializing new browser targets")
                self._condition.wait(min(remaining, 0.1))
            if self._cache_hits:
                raise CachePolicyError(f"Browser cache policy observed {len(self._cache_hits)} cache response event(s)")

    def reset_observation(self):
        """Clear startup-page observations without clearing policy failures."""
        with self._lock:
            self._raise_errors()
            self._events.clear()
            self._event_count = 0
            self._cache_hits.clear()
            self._request_urls.clear()

    def drain_network_events(self):
        """Compatibility alias: this stream contains Page and Network events."""
        return self.drain_events()

    def drain_events(self):
        """Return raw Page/Network messages with sessionId since the last drain."""
        with self._lock:
            result = list(self._events)
            self._events.clear()
            return result

    def snapshot(self):
        with self._lock:
            targets = [dict(item) for item in self._targets.values()]
            return {"enabled": self._started, "connected": self._connected,
                    "target_count": len({item["target_id"] for item in targets}),
                    "session_count": len(targets), "targets": targets,
                    "pending_target_count": sum(not item["initialized"] and not item["detached"] for item in targets),
                    "errors": list(self._errors), "cache_hits": list(self._cache_hits),
                    "network_event_count": self._event_count}

    def close(self):
        """Release owned sessions and stop threads; safe after browser exit."""
        if self._closed:
            return
        # Keep the receiver running until detach replies have been processed.
        if self._connected:
            for session_id, target in list(self._targets.items()):
                if target["paused"] and not target["detached"]:
                    try:
                        self._request("Runtime.runIfWaitingForDebugger", {}, session_id, timeout=0.5)
                    except Exception:
                        pass
            try:
                self._request("Target.setAutoAttach", self._auto_attach(False), timeout=0.5)
            except Exception:
                pass
        self._closing = True
        self._closed = True
        self._initializations.put(None)
        if self._socket:
            try:
                self._socket.close(timeout=0.2)
            except Exception:
                pass
        for thread in (self._receiver, self._initializer):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._connected = False
