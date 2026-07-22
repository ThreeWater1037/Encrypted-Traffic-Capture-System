"""真实浏览器网页抓取与加密流量采集入口。

脚本使用 Selenium 驱动 Chrome、Edge、Firefox 或 Safari。每次访问都创建临时
浏览器配置并关闭缓存；可在浏览器启动前拉起 TShark/tcpdump 保存 PCAP，同时为
支持的浏览器导出 TLS 会话密钥。

用法：
    python3 wiki_fetcher.py <wiki_url> [--output-dir ./fetch_output]
    python3 wiki_fetcher.py <wiki_url> --browsers chrome edge firefox safari
    python3 wiki_fetcher.py <wiki_url> --pcap
"""

import os
import time
import signal
import hashlib
import secrets
import logging
import argparse
import tempfile
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.safari.service import Service as SafariService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.core.driver_cache import DriverCacheManager

from browser_discovery import discover_browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Packet capture
# ---------------------------------------------------------------------------

class PacketCapture:
    """
    Wraps tshark (preferred) or tcpdump to capture traffic on all interfaces.
    Covers DNS (port 53), TCP handshake, TLS handshake, and HTTP(S) data.

    Capture timeline per browser:
        start() → browser launch → driver.get() → page loaded → stop()

    The pcap is flushed and closed only after stop(), so all phases
    (DNS query, TCP SYN, TLS ClientHello, HTTP response) are included.
    """

    def __init__(self, pcap_path: Path, capture_filter: str = ""):
        self.pcap_path = pcap_path
        self.capture_filter = capture_filter  # optional BPF filter
        self._proc: Optional[subprocess.Popen] = None
        self._tool: Optional[str] = None

    # ------------------------------------------------------------------
    # Tool detection
    # ------------------------------------------------------------------

    @staticmethod
    def _find_tool() -> Optional[tuple[str, str]]:
        """Return (tool_name, binary_path) for tshark or tcpdump."""
        for name, candidates in [
            ("tshark", [
                "D:/software/Wireshark/tshark.exe",
                "C:/Program Files/Wireshark/tshark.exe",
                "C:/Program Files (x86)/Wireshark/tshark.exe",
                "/Applications/Wireshark.app/Contents/MacOS/tshark",
                "/usr/local/bin/tshark",
                "/usr/bin/tshark",
            ]),
            ("tcpdump", [
                "/usr/sbin/tcpdump",
                "/usr/bin/tcpdump",
            ]),
        ]:
            for path in candidates:
                if os.path.isfile(path):
                    return name, path
            found = shutil.which(name)
            if found:
                return name, found
        return None

    # ------------------------------------------------------------------
    # Build capture command
    # ------------------------------------------------------------------

    def _build_cmd(self, tool: str, binary: str) -> list[str]:
        """
        Capture on every available interface simultaneously.
        tshark supports multiple -i flags; tcpdump falls back to the
        default interface (usually the primary uplink — covers DNS & HTTP).
        """
        if tool == "tshark":
            # Collect all up, non-loopback interfaces + loopback explicitly
            ifaces = self._list_interfaces_tshark(binary)
            cmd = [binary, "-q"]
            for iface in ifaces:
                cmd += ["-i", iface]
            cmd += ["-w", str(self.pcap_path)]
            if self.capture_filter:
                cmd += ["-f", self.capture_filter]
        else:  # tcpdump
            # -i any not always supported on macOS; use default interface
            # (covers outbound DNS/TCP). lo0 added for mDNS/local resolver.
            cmd = [binary, "-U",           # flush each packet immediately
                   "-w", str(self.pcap_path)]
            iface = self._default_iface_tcpdump()
            if iface:
                cmd += ["-i", iface]
            if self.capture_filter:
                cmd += [self.capture_filter]
        return cmd

    # Interfaces to skip — virtual/tunnel/pseudo devices that add noise
    _SKIP_IFACE_PREFIXES = (
        "utun", "awdl", "llw", "anpi", "gif", "stf", "bridge",
        "ap1", "en13", "en18", r"\\.\USBPcap",
    )
    # tshark pseudo-sources that are not real interfaces
    _SKIP_IFACE_NAMES = {
        "ciscodump", "etwdump", "randpkt", "sshdump", "udpdump", "wifidump",
    }

    @classmethod
    def _list_interfaces_tshark(cls, binary: str) -> list[str]:
        """
        Return interface names for tshark.
        Keeps Wi-Fi (en0), Ethernet (en*), and Loopback (lo0).
        Skips tunnel/virtual adapters that can't be opened without root.
        """
        try:
            out = subprocess.check_output(
                [binary, "-D"],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            ifaces = []
            for line in out.splitlines():
                # Format: "2. en0 (Wi-Fi)" or "22. lo0 (Loopback)"
                rest = line.strip().split(".", 1)[-1].strip()
                # Extract bare name (before the first space or '(')
                name = rest.split()[0].split("(")[0].strip() if rest else ""
                if not name:
                    continue
                if name in cls._SKIP_IFACE_NAMES:
                    continue
                if any(name.startswith(p) for p in cls._SKIP_IFACE_PREFIXES):
                    continue
                ifaces.append(name)
            return ifaces or ["en0", "lo0"]
        except Exception:
            return ["en0", "lo0"]

    @staticmethod
    def _default_iface_tcpdump() -> Optional[str]:
        """Detect the primary outbound interface on macOS via netstat."""
        try:
            out = subprocess.check_output(
                ["route", "-n", "get", "default"], stderr=subprocess.DEVNULL, text=True
            )
            for line in out.splitlines():
                if "interface:" in line:
                    return line.split(":")[-1].strip()
        except Exception:
            pass
        return "en0"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """选择可用抓包工具并在浏览器启动前开始捕获。"""
        result = self._find_tool()
        if result is None:
            log.warning("Neither tshark nor tcpdump found — pcap skipped.")
            return

        self._tool, binary = result
        cmd = self._build_cmd(self._tool, binary)

        log.info("  pcap capture starting (%s) → %s", self._tool, self.pcap_path)
        log.debug("  cmd: %s", " ".join(cmd))

        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(cmd, **popen_kwargs)
        # Wait for sniffer to open BPF handles and start capturing
        time.sleep(1.2)

    def stop(self) -> Optional[Path]:
        """刷新并可靠关闭抓包进程，返回非空 PCAP 路径。"""
        if self._proc is None:
            return None

        # Brief pause so the last packets (TCP FIN, TLS close_notify) are written
        time.sleep(0.5)
        # Ask the sniffer to flush write buffers, then guarantee termination.
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        except Exception:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            except Exception:
                if self._proc.poll() is None:
                    self._proc.kill()
                    self._proc.wait()

        self._proc = None

        if self.pcap_path.exists() and self.pcap_path.stat().st_size > 0:
            log.info("  pcap saved → %s (%d bytes)",
                     self.pcap_path, self.pcap_path.stat().st_size)
            return self.pcap_path
        else:
            log.warning("  pcap empty or missing.")
            return None


# ---------------------------------------------------------------------------
# Session record
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """记录一次 URL 与浏览器组合的抓取结果和产物路径。"""
    browser: str
    url: str
    timestamp: str
    final_url: str
    page_title: str
    html_length: int
    response_time_ms: float
    content_hash: str
    cookies: list[dict]
    key_log_path: Optional[str]
    pcap_path: Optional[str]
    request_id: str = field(default_factory=lambda: secrets.token_hex(8))
    error: Optional[str] = None

    def summary(self) -> str:
        """生成便于写入 report.txt 的人类可读摘要。"""
        cookie_names = ", ".join(c["name"] for c in self.cookies) or "(none)"
        lines = [
            f"  Browser        : {self.browser}",
            f"  Request-ID     : {self.request_id}",
            f"  Timestamp      : {self.timestamp}",
            f"  Final URL      : {self.final_url}",
            f"  Page title     : {self.page_title}",
            f"  HTML length    : {self.html_length} bytes",
            f"  Response time  : {self.response_time_ms:.0f} ms",
            f"  Body SHA-256   : {self.content_hash}",
            f"  Cookies        : {cookie_names}",
            f"  TLS key log    : {self.key_log_path or 'N/A'}",
            f"  pcap           : {self.pcap_path or 'N/A'}",
        ]
        if self.error:
            lines.append(f"  ERROR          : {self.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Browser drivers
# ---------------------------------------------------------------------------

class BrowserDriver:
    """不同浏览器构建逻辑的统一接口。"""
    name: str

    def build(self, key_log_path: Path, profile_dir: Path) -> webdriver.Remote:
        """根据临时配置目录和密钥路径构造浏览器实例。"""
        raise NotImplementedError


def _driver_cache_manager() -> DriverCacheManager:
    """把驱动依赖缓存定向到 Worker 数据目录，而不是实验结果目录。"""
    root_dir = os.getenv("WDM_CACHE_DIR")
    if root_dir:
        return DriverCacheManager(root_dir=root_dir)
    return DriverCacheManager()


class ChromeDriver(BrowserDriver):
    """创建全新、无缓存并开启 TLS key log 的 Chrome 实例。"""
    name = "Chrome (Blink)"

    @staticmethod
    def _find_binary() -> Optional[str]:
        """使用与 Worker 能力探测相同的规则定位 Chrome。"""
        return discover_browser("chrome")

    def build(self, key_log_path: Path, profile_dir: Path) -> webdriver.Chrome:
        """配置 Chrome 启动参数、驱动服务和 CDP 无缓存设置。"""
        opts = ChromeOptions()
        binary = self._find_binary()
        if not binary:
            raise RuntimeError("Google Chrome/Chromium not found")
        opts.binary_location = binary

        # Fresh profile — no persistent cache or cookies
        opts.add_argument(f"--user-data-dir={profile_dir}")

        # Disable all caching mechanisms
        opts.add_argument("--disable-application-cache")
        opts.add_argument("--disable-cache")
        opts.add_argument("--disk-cache-size=0")
        opts.add_argument("--media-cache-size=0")
        opts.add_argument("--disable-offline-load-stale-cache")

        # TLS session key capture
        opts.add_argument(f"--ssl-key-log-file={key_log_path}")

        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        service = ChromeService(
            ChromeDriverManager(cache_manager=_driver_cache_manager()).install()
        )
        driver = webdriver.Chrome(service=service, options=opts)

        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        driver.execute_cdp_cmd("Network.enable", {})
        return driver


class EdgeDriver(BrowserDriver):
    """创建与 Chrome 采用相同无缓存策略的 Chromium Edge 实例。"""
    name = "Microsoft Edge (Blink)"

    @staticmethod
    def _find_binary() -> Optional[str]:
        """使用覆盖变量、PATH、注册表和安装目录定位 Edge。"""
        return discover_browser("edge")

    def build(self, key_log_path: Path, profile_dir: Path) -> webdriver.Edge:
        """配置 Edge 启动参数、驱动服务和 CDP 无缓存设置。"""
        binary = self._find_binary()
        if not binary:
            raise RuntimeError("Microsoft Edge not found")

        opts = EdgeOptions()
        opts.binary_location = binary
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--disable-application-cache")
        opts.add_argument("--disable-cache")
        opts.add_argument("--disk-cache-size=0")
        opts.add_argument("--media-cache-size=0")
        opts.add_argument("--disable-offline-load-stale-cache")
        opts.add_argument(f"--ssl-key-log-file={key_log_path}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        service = EdgeService(
            EdgeChromiumDriverManager(
                cache_manager=_driver_cache_manager()
            ).install()
        )
        driver = webdriver.Edge(service=service, options=opts)
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        driver.execute_cdp_cmd("Network.enable", {})
        return driver


class FirefoxDriver(BrowserDriver):
    """创建禁用磁盘、内存和 HTTP 缓存的 Firefox 实例。"""
    name = "Firefox (Gecko)"

    @staticmethod
    def _find_binary() -> Optional[str]:
        """使用覆盖变量、PATH、注册表和安装目录定位 Firefox。"""
        return discover_browser("firefox")

    def build(self, key_log_path: Path, profile_dir: Path) -> webdriver.Firefox:
        """配置 Firefox Profile、缓存首选项和 TLS key log 环境变量。"""
        binary = self._find_binary()
        if not binary:
            raise RuntimeError(
                "Firefox not found. Install from https://www.mozilla.org/firefox/"
            )

        opts = FirefoxOptions()
        opts.binary_location = binary
        os.environ["SSLKEYLOGFILE"] = str(key_log_path)

        # 不传 -profile：GeckoDriver 会为每次会话创建全新临时 Profile，并把
        # 下方首选项写入该 Profile。Firefox 153 在接收一个已经存在的自定义
        # -profile 目录时会返回 Failed to set preferences。

        # 浏览器级缓存
        opts.set_preference("browser.cache.disk.enable", False)
        opts.set_preference("browser.cache.memory.enable", False)
        opts.set_preference("browser.cache.offline.enable", False)
        # HTTP 协议级缓存（原先遗漏）
        opts.set_preference("network.http.use-cache", False)
        opts.set_preference("network.http.cache.disk.enable", False)
        opts.set_preference("network.http.cache.memory.enable", False)

        opts.set_preference("browser.shell.checkDefaultBrowser", False)
        opts.add_argument("--headless")

        service = FirefoxService(
            GeckoDriverManager(cache_manager=_driver_cache_manager()).install()
        )
        return webdriver.Firefox(service=service, options=opts)


class SafariDriver(BrowserDriver):
    """创建 Safari 实例；Safari 不支持导出 TLS 会话密钥。"""
    name = "Safari (WebKit)"

    def build(self, key_log_path: Path, profile_dir: Path) -> webdriver.Safari:
        """调用系统 safaridriver，并把未启用远程自动化转换为可读错误。"""
        # Requires:
        #   1. Safari → 设置 → 高级 → 勾选「在菜单栏中显示"开发"菜单」
        #   2. 开发菜单 → 允许远程自动化（Allow Remote Automation）
        #   3. sudo safaridriver --enable  （仅需运行一次）
        # Safari 使用 Apple Security Framework，不暴露 SSLKEYLOGFILE 接口，
        # 无法导出 TLS session key，pcap 只能用于 infer_packets.py 密文推测。
        log.warning(
            "Safari 不支持 TLS key 导出，capture_safari_flows/ 将无法生成"
            "（pcap 仅可用于 infer_packets.py）"
        )
        try:
            return webdriver.Safari(service=SafariService())
        except Exception as exc:
            msg = str(exc)
            if "Allow remote automation" in msg or "remote automation" in msg.lower():
                raise RuntimeError(
                    "Safari WebDriver 未启用。请完成以下步骤：\n"
                    "  1. Safari -> 设置 -> 高级 -> 勾选「在菜单栏中显示开发菜单」\n"
                    "  2. 开发(Develop)菜单 -> 勾选「允许远程自动化」\n"
                    "  3. 终端运行：sudo safaridriver --enable"
                ) from exc
            raise


AVAILABLE_DRIVERS = {
    "chrome":  ChromeDriver(),
    "edge":    EdgeDriver(),
    "firefox": FirefoxDriver(),
    "safari":  SafariDriver(),
}


# ---------------------------------------------------------------------------
# Configurable interval between URL requests (seconds)
# Change this value to control the pause between consecutive URL fetches.
# ---------------------------------------------------------------------------
INTERVAL_BETWEEN_URLS: float = 3.0   # seconds


# ---------------------------------------------------------------------------
# URL entry — carries ID + name parsed from urls.txt
# ---------------------------------------------------------------------------

@dataclass
class UrlEntry:
    """从输入 TSV 读取的单个词条标识、名称和完整 URL。"""
    id: str          # 词条 ID（字符串，保留原始值）
    name: str        # 词条中文名
    url: str         # 完整 URL


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def _entry_slug(entry: "UrlEntry") -> str:
    """
    目录名格式：{ID}-wiki-{词条名}
    对文件系统不安全的字符做最小替换。
    """
    # 替换路径分隔符及其他不安全字符
    safe_name = entry.name.replace("/", "／").replace("\\", "＼").replace(":", "：")
    safe_name = safe_name.replace("*", "＊").replace("?", "？").replace('"', "＂")
    safe_name = safe_name.replace("<", "＜").replace(">", "＞").replace("|", "｜")
    safe_name = safe_name[:80]   # 防止超长路径
    return f"{entry.id}-wiki-{safe_name}"


class WikiFetcher:
    """按 URL 和浏览器顺序执行抓取，并维护汇总报告。"""
    def __init__(self, output_dir: Path, browsers: list[str], capture_pcap: bool):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.browsers = browsers
        self.capture_pcap = capture_pcap
        self.all_records: list[SessionRecord] = []   # flat list across all URLs

    # ------------------------------------------------------------------
    # Per-URL output directory
    # ------------------------------------------------------------------

    def _url_dir(self, slug: str) -> Path:
        d = self.output_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _key_log_path(self, url_dir: Path, browser_key: str) -> Path:
        return url_dir / f"tls_keys_{browser_key}.log"

    def _pcap_path(self, url_dir: Path, browser_key: str) -> Path:
        return url_dir / f"capture_{browser_key}.pcap"

    # ------------------------------------------------------------------
    # Single browser fetch
    # ------------------------------------------------------------------

    def _fetch_with(self, url: str, driver_key: str, url_dir: Path) -> SessionRecord:
        """完成单个 URL/浏览器的抓包、访问、落盘与资源清理。"""
        bd = AVAILABLE_DRIVERS[driver_key]
        timestamp = datetime.now().isoformat()

        profile_dir = Path(tempfile.mkdtemp(prefix=f"wb_{driver_key}_"))
        key_log = self._key_log_path(url_dir, driver_key)
        # Chrome on Windows does not reliably write --ssl-key-log-file to
        # non-ASCII paths. Write inside the ASCII temp profile first, then copy
        # the result back to the per-URL output directory.
        browser_key_log = profile_dir / f"tls_keys_{driver_key}.log"

        # Packet capture starts before browser launch
        capture = None
        actual_pcap_path: Optional[str] = None
        if self.capture_pcap:
            capture = PacketCapture(pcap_path=self._pcap_path(url_dir, driver_key))
            capture.start()

        driver = None
        error_msg = None
        final_url = url
        page_title = ""
        html = ""
        cookies = []
        response_time_ms = 0.0

        try:
            log.info("    Starting %s ...", bd.name)
            driver = bd.build(browser_key_log, profile_dir)

            t0 = time.perf_counter()

            if isinstance(driver, (webdriver.Chrome, webdriver.Edge)):
                driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                    "headers": {
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                    }
                })

            driver.get(url)

            # Stage 1: wait for document.readyState == "complete"
            # (Selenium's default page load strategy already does this,
            #  but we make it explicit and observable)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )

            # Stage 2: wait for network to go idle — no new requests for
            # NETWORK_IDLE_THRESHOLD consecutive seconds.
            # This covers async JS, lazy-loaded images, XHR/fetch calls, etc.
            NETWORK_IDLE_THRESHOLD = 2.0   # seconds of silence = "done"
            NETWORK_IDLE_TIMEOUT   = 15.0  # give up after this long regardless

            if isinstance(driver, (webdriver.Chrome, webdriver.Edge)):
                # Track in-flight request count via CDP Network events (Chromium only)
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_script("""
                    window.__inflight = 0;
                    window.__lastActivity = Date.now();
                    const orig_fetch = window.fetch;
                    window.fetch = function(...args) {
                        window.__inflight++;
                        window.__lastActivity = Date.now();
                        return orig_fetch.apply(this, args).finally(() => {
                            window.__inflight = Math.max(0, window.__inflight - 1);
                            window.__lastActivity = Date.now();
                        });
                    };
                """)
                idle_deadline = time.perf_counter() + NETWORK_IDLE_TIMEOUT
                while time.perf_counter() < idle_deadline:
                    time.sleep(0.3)
                    idle_ms = driver.execute_script(
                        "return Date.now() - (window.__lastActivity || 0);"
                    )
                    if idle_ms >= NETWORK_IDLE_THRESHOLD * 1000:
                        break
            else:
                # Firefox: no CDP — fall back to a fixed settle delay
                time.sleep(NETWORK_IDLE_THRESHOLD)

            response_time_ms = (time.perf_counter() - t0) * 1000

            final_url = driver.current_url
            page_title = driver.title
            html = driver.page_source
            cookies = driver.get_cookies()

            # Stage 3: let the browser flush any remaining in-flight responses
            # before we call quit() and terminate all connections.
            time.sleep(0.5)

        except Exception as exc:
            error_msg = str(exc)
            log.warning("    Error: %s", exc)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if browser_key_log.exists():
                shutil.copy2(browser_key_log, key_log)
            shutil.rmtree(profile_dir, ignore_errors=True)
            os.environ.pop("SSLKEYLOGFILE", None)

            if capture:
                saved = capture.stop()
                actual_pcap_path = str(saved) if saved else None

        content_hash = hashlib.sha256(html.encode()).hexdigest() if html else ""
        html_length = len(html.encode())

        if html:
            body_path = url_dir / f"body_{driver_key}.html"
            body_path.write_text(html, encoding="utf-8")
            log.info("    Body → %s (%d bytes)", body_path, html_length)

        return SessionRecord(
            browser=bd.name,
            url=url,
            timestamp=timestamp,
            final_url=final_url,
            page_title=page_title,
            html_length=html_length,
            response_time_ms=response_time_ms,
            content_hash=content_hash,
            cookies=cookies,
            key_log_path=str(key_log) if key_log.exists() else None,
            pcap_path=actual_pcap_path,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # Single entry point
    # ------------------------------------------------------------------

    def _run_entry(self, entry: "UrlEntry", position: int, total: int):
        """依次运行当前词条选择的浏览器，并写入词条级 report.txt。"""
        slug = _entry_slug(entry)
        url_dir = self._url_dir(slug)
        url_records: list[SessionRecord] = []

        for key in self.browsers:
            if key not in AVAILABLE_DRIVERS:
                log.warning("  Unknown browser '%s', skipping.", key)
                continue

            # 断点恢复：若该浏览器的输出文件已完整存在则跳过
            body_path = url_dir / f"body_{key}.html"
            if body_path.exists() and body_path.stat().st_size > 0:
                log.info("  → %s [已完成，跳过]", AVAILABLE_DRIVERS[key].name)
                continue

            log.info("  → %s", AVAILABLE_DRIVERS[key].name)
            record = self._fetch_with(entry.url, key, url_dir)
            url_records.append(record)
            self.all_records.append(record)
            print(record.summary())
            print()

        if url_records:
            report_path = url_dir / "report.txt"
            lines = [
                f"ID   : {entry.id}",
                f"词条 : {entry.name}",
                f"URL  : {entry.url}",
                "=" * 60, "",
            ]
            for r in url_records:
                lines += [f"[{r.browser}]", r.summary(), ""]
            report_path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------

    def run(self, entries: list["UrlEntry"]):
        """执行完整批次；URL 之间保留固定间隔以减少相互干扰。"""
        total = len(entries)
        log.info("词条数 : %d", total)
        log.info("Output : %s", self.output_dir)
        log.info("pcap   : %s", "enabled" if self.capture_pcap else "disabled")
        log.info("Interval between URLs: %.1f s", INTERVAL_BETWEEN_URLS)
        log.info("=" * 60)

        for pos, entry in enumerate(entries, start=1):
            log.info("[%d/%d] ID=%s  %s", pos, total, entry.id, entry.name)
            log.info("  %s", entry.url)

            self._run_entry(entry, pos, total)

            if pos < total:
                log.info("  Waiting %.1f s ...", INTERVAL_BETWEEN_URLS)
                time.sleep(INTERVAL_BETWEEN_URLS)

        self._write_summary()

    # ------------------------------------------------------------------
    # Global summary report
    # ------------------------------------------------------------------

    def _write_summary(self):
        """把本批次所有浏览器会话写入根目录 summary.txt。"""
        path = self.output_dir / "summary.txt"
        lines = [
            "Wiki Fetch Summary",
            "=" * 60,
            f"Generated : {datetime.now().isoformat()}",
            f"Total     : {len(self.all_records)} requests",
            "",
        ]
        for r in self.all_records:
            lines += [f"[{r.browser}]  {r.url}", r.summary(), ""]

        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("Summary → %s", path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_entries(txt_path: str,
                  id_start: Optional[int] = None,
                  id_end: Optional[int] = None) -> list[UrlEntry]:
    """
    读取 urls.txt（制表符分隔：ID\\t词条名\\t完整URL）。
    跳过注释行（#）和空行。
    若指定 id_start / id_end，只返回 ID 在 [id_start, id_end] 区间内的词条。
    """
    entries: list[UrlEntry] = []
    skipped_range = 0

    with open(txt_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            entry_id, name, url = parts[0], parts[1], parts[2]

            # ID 范围过滤
            try:
                numeric_id = int(entry_id)
            except ValueError:
                numeric_id = None

            if numeric_id is not None:
                if id_start is not None and numeric_id < id_start:
                    skipped_range += 1
                    continue
                if id_end is not None and numeric_id > id_end:
                    skipped_range += 1
                    continue

            entries.append(UrlEntry(id=entry_id, name=name, url=url))

    if skipped_range:
        log.info("ID 范围过滤：跳过 %d 条（范围外）", skipped_range)
    return entries


def main():
    """解析命令行参数并启动单 URL 或 TSV 批量抓取。"""
    parser = argparse.ArgumentParser(
        description=(
            "Fetch wiki URLs using real browser engines via Selenium.\n"
            "Accepts a single URL or a structured text file (--input).\n"
            "TLS session keys are saved for Chrome and Firefox.\n"
            "Network traffic can optionally be saved as pcap (--pcap).\n\n"
            f"Interval between URLs: INTERVAL_BETWEEN_URLS = {INTERVAL_BETWEEN_URLS} s (修改脚本顶部)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # URL 来源：单个 URL 或 txt 文件，二选一
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("url", nargs="?", help="单个 wiki URL")
    src.add_argument(
        "--input", "-i", metavar="FILE",
        help=(
            "urls.txt 文件路径（制表符分隔：ID\\t词条名\\t完整URL）。"
            "# 开头为注释，空行忽略。"
        ),
    )

    parser.add_argument(
        "--id-start", type=int, default=None, metavar="N",
        help="只处理 ID >= N 的词条（断点续传起始 ID）",
    )
    parser.add_argument(
        "--id-end", type=int, default=None, metavar="N",
        help="只处理 ID <= N 的词条（断点续传结束 ID）",
    )
    parser.add_argument(
        "--output-dir", default="./fetch_output",
        help="输出根目录（默认：./fetch_output）。每个词条生成子目录：{ID}-wiki-{词条名}",
    )
    parser.add_argument(
        "--browsers", nargs="+",
        choices=list(AVAILABLE_DRIVERS),
        default=["chrome", "firefox"],
        help="使用的浏览器（默认：chrome firefox；可选 edge）。Safari 需先运行 safaridriver --enable。",
    )
    parser.add_argument(
        "--pcap", action="store_true",
        help=(
            "抓取每个浏览器访问的完整网络流量（含 DNS、TCP、TLS），保存为 pcap。"
            "需要 tshark（Wireshark）或 tcpdump 且有 BPF 读取权限。"
        ),
    )
    args = parser.parse_args()

    # 构建 UrlEntry 列表
    if args.input:
        entries = _load_entries(args.input, id_start=args.id_start, id_end=args.id_end)
        if not entries:
            parser.error(f"{args.input} 中未找到符合条件的词条")
        log.info("加载词条：%d 条（来自 %s）", len(entries), args.input)
        if args.id_start or args.id_end:
            log.info("ID 范围：[%s, %s]",
                     args.id_start if args.id_start else "起始",
                     args.id_end   if args.id_end   else "结束")
    else:
        # 单个 URL：ID 和名称留空
        entries = [UrlEntry(id="0", name="manual", url=args.url)]

    fetcher = WikiFetcher(
        output_dir=Path(args.output_dir),
        browsers=args.browsers,
        capture_pcap=args.pcap,
    )
    fetcher.run(entries)


if __name__ == "__main__":
    main()
