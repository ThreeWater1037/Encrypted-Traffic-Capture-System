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
import json
import platform
import time
import signal
import hashlib
import secrets
import logging
import argparse
import re
import tempfile
import shutil
import subprocess
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional
from urllib.parse import urlsplit

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
from browser_proxy import BrowserProxy, parse_browser_proxy
from browser_loading import HIT_LEGACY_HOSTS, wait_for_resources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _prepare_navigation(driver: webdriver.Remote, url: str) -> None:
    """设置导航兜底；今日哈工大资源等待在 DOM 就绪后单独处理。"""
    # Must be shorter than the WebDriver HTTP transport timeout (120s).
    driver.set_page_load_timeout(90)
    if (
        isinstance(driver, (webdriver.Chrome, webdriver.Edge))
        and urlsplit(url).hostname == "today.hit.edu.cn"
    ):
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [f"{scheme}://{host}/*"
                     for host in sorted(HIT_LEGACY_HOSTS) for scheme in ("http", "https")],
        })
        driver.get_log("performance")  # discard new-tab events before navigation


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
        "bluetooth-monitor",
        "ciscodump",
        "dbus-session",
        "dbus-system",
        "dpauxmon",
        "etwdump",
        "nflog",
        "nfqueue",
        "randpkt",
        "sdjournal",
        "sshdump",
        "udpdump",
        "wifidump",
    }

    @classmethod
    def _list_interfaces_tshark(cls, binary: str) -> list[str]:
        """
        Return interface names for tshark.
        Keeps Wi-Fi (en0), Ethernet (en*), and Loopback (lo0).
        Skips tunnel/virtual adapters that can't be opened without root.
        """
        system = platform.system().lower()

        # Linux 的特殊接口 any 已覆盖全部实体接口。WSL 镜像网络会额外暴露多个
        # eth*、nflog、DBus 和 extcap 伪接口；逐一传给 TShark 时，只要其中一个
        # 无法打开，整个抓包进程就可能立即退出并留下空 PCAP。
        if system == "linux":
            return ["any"]

        # Windows 首次启动 TShark/Npcap 可能较慢，因此延长超时并重试一次。
        # macOS 通常无需重试，同时仍保留 en0/lo0 作为该平台的安全回退。
        attempts = 2 if system == "windows" else 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                out = subprocess.check_output(
                    [binary, "-D"],
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "  tshark interface detection failed (%s/%s): %s",
                    attempt,
                    attempts,
                    last_error,
                )
                if attempt < attempts:
                    time.sleep(0.5)
                continue

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

            if ifaces:
                return ifaces

            last_error = "tshark -D returned no usable capture interfaces"
            log.warning(
                "  tshark interface detection failed (%s/%s): %s",
                attempt,
                attempts,
                last_error,
            )
            if attempt < attempts:
                time.sleep(0.5)

        if system == "darwin":
            log.warning(
                "  tshark interface detection failed on macOS; "
                "falling back to en0 and lo0."
            )
            return ["en0", "lo0"]

        # Windows 绝不能回退到 macOS 的 en0/lo0，否则会生成空 PCAP。
        raise RuntimeError(
            f"tshark interface detection failed on {platform.system()} "
            f"after {attempts} attempt(s): {last_error}"
        )

    @staticmethod
    def _default_iface_tcpdump() -> Optional[str]:
        """Detect the primary outbound interface on macOS via netstat."""
        if platform.system().lower() == "linux":
            return "any"
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
        try:
            cmd = self._build_cmd(self._tool, binary)
        except RuntimeError as exc:
            # 接口探测失败时保留网页抓取结果，但明确跳过 PCAP，任务会标记为部分成功。
            log.error("  pcap capture skipped: %s", exc)
            self._tool = None
            return

        log.info("  pcap capture starting (%s) → %s", self._tool, self.pcap_path)
        log.debug("  cmd: %s", " ".join(cmd))

        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            # 继承父进程的 stderr。Worker 已把父进程输出写入 worker.log，
            # 因此接口或权限错误会直接出现在任务日志中，且不会产生管道阻塞。
            "stderr": None,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(cmd, **popen_kwargs)
        # Wait for sniffer to open BPF handles and start capturing
        time.sleep(1.2)
        return_code = self._proc.poll()
        if return_code is not None:
            log.warning(
                "  %s exited before capture started (code=%s); see error output above.",
                self._tool,
                return_code,
            )

    def stop(self) -> Optional[Path]:
        """刷新并可靠关闭抓包进程，返回非空 PCAP 路径。"""
        if self._proc is None:
            return None

        # Brief pause so the last packets (TCP FIN, TLS close_notify) are written
        time.sleep(0.5)
        # Ask the sniffer to flush write buffers, then guarantee termination.
        try:
            return_code = self._proc.poll()
            if return_code is None:
                if os.name == "nt":
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self._proc.send_signal(signal.SIGINT)
                self._proc.wait(timeout=10)
            else:
                log.warning(
                    "  %s capture process exited early (code=%s).",
                    self._tool,
                    return_code,
                )
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
    skipped_resources: list[dict] = field(default_factory=list)

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
        if self.skipped_resources:
            lines.append(f"  Skipped stalled resources: {len(self.skipped_resources)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Browser drivers
# ---------------------------------------------------------------------------

class BrowserDriver:
    """不同浏览器构建逻辑的统一接口。"""
    name: str

    def build(
        self,
        key_log_path: Path,
        profile_dir: Path,
        proxy: BrowserProxy | None = None,
    ) -> webdriver.Remote:
        """根据临时配置目录和密钥路径构造浏览器实例。"""
        raise NotImplementedError


def _driver_cache_manager() -> DriverCacheManager:
    """把驱动依赖缓存定向到 Worker 数据目录，而不是实验结果目录。"""
    root_dir = os.getenv("WDM_CACHE_DIR")
    if root_dir:
        return DriverCacheManager(root_dir=root_dir)
    return DriverCacheManager()


def _apply_chromium_proxy(options: ChromeOptions | EdgeOptions, proxy: BrowserProxy | None) -> None:
    """把统一代理地址转换为 Chromium 启动参数。"""
    if proxy is not None:
        options.add_argument(f"--proxy-server={proxy.url}")


def _apply_firefox_proxy(options: FirefoxOptions, proxy: BrowserProxy | None) -> None:
    """把统一代理地址转换为 Firefox 临时 Profile 首选项。"""
    if proxy is None:
        return

    options.set_preference("network.proxy.type", 1)
    options.set_preference("network.proxy.no_proxies_on", "localhost, 127.0.0.1, ::1")
    if proxy.scheme == "http":
        # 同一个 HTTP CONNECT 代理同时处理 HTTP 和 HTTPS 目标。
        options.set_preference("network.proxy.http", proxy.host)
        options.set_preference("network.proxy.http_port", proxy.port)
        options.set_preference("network.proxy.ssl", proxy.host)
        options.set_preference("network.proxy.ssl_port", proxy.port)
        options.set_preference("network.proxy.share_proxy_settings", True)
    else:
        options.set_preference("network.proxy.socks", proxy.host)
        options.set_preference("network.proxy.socks_port", proxy.port)
        options.set_preference(
            "network.proxy.socks_version",
            4 if proxy.scheme == "socks4" else 5,
        )
        options.set_preference("network.proxy.socks_remote_dns", True)


def _detect_browser_error_page(final_url: str, html: str) -> str | None:
    """识别 Selenium 未抛异常但实际展示的浏览器网络错误页。"""
    normalized_url = final_url.lower()
    if normalized_url.startswith("about:neterror"):
        return f"Firefox network error page: {final_url}"
    if normalized_url.startswith("chrome-error://"):
        return f"Chromium network error page: {final_url}"

    lowered = html.lower()
    chrome_error_layout = any(
        marker in lowered
        for marker in (
            'id="main-frame-error"',
            'class="error-code"',
            "interstitial-wrapper",
        )
    )
    if chrome_error_layout:
        match = re.search(r"\bERR_[A-Z0-9_]+\b", html, flags=re.IGNORECASE)
        error_code = match.group(0).upper() if match else "unknown network error"
        return f"Chromium network error page: {error_code}"
    return None


class ChromeDriver(BrowserDriver):
    """创建全新、无缓存并开启 TLS key log 的 Chrome 实例。"""
    name = "Chrome (Blink)"

    @staticmethod
    def _find_binary() -> Optional[str]:
        """使用与 Worker 能力探测相同的规则定位 Chrome。"""
        return discover_browser("chrome")

    def build(
        self,
        key_log_path: Path,
        profile_dir: Path,
        proxy: BrowserProxy | None = None,
        *, skip_stalled_resources: bool = False,
    ) -> webdriver.Chrome:
        """配置 Chrome 启动参数、驱动服务和 CDP 无缓存设置。"""
        opts = ChromeOptions()
        if skip_stalled_resources:
            opts.page_load_strategy = "eager"
            opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
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
        _apply_chromium_proxy(opts, proxy)

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

    def build(
        self,
        key_log_path: Path,
        profile_dir: Path,
        proxy: BrowserProxy | None = None,
        *, skip_stalled_resources: bool = False,
    ) -> webdriver.Edge:
        """配置 Edge 启动参数、驱动服务和 CDP 无缓存设置。"""
        binary = self._find_binary()
        if not binary:
            raise RuntimeError("Microsoft Edge not found")

        opts = EdgeOptions()
        if skip_stalled_resources:
            opts.page_load_strategy = "eager"
            opts.set_capability("ms:loggingPrefs", {"performance": "ALL"})
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
        _apply_chromium_proxy(opts, proxy)

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

    def build(
        self,
        key_log_path: Path,
        profile_dir: Path,
        proxy: BrowserProxy | None = None,
    ) -> webdriver.Firefox:
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
        _apply_firefox_proxy(opts, proxy)
        opts.add_argument("--headless")

        service = FirefoxService(
            GeckoDriverManager(cache_manager=_driver_cache_manager()).install()
        )
        return webdriver.Firefox(service=service, options=opts)


class SafariDriver(BrowserDriver):
    """创建 Safari 实例；Safari 不支持导出 TLS 会话密钥。"""
    name = "Safari (WebKit)"

    def build(
        self,
        key_log_path: Path,
        profile_dir: Path,
        proxy: BrowserProxy | None = None,
    ) -> webdriver.Safari:
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
    # Windows 会在创建目录时静默裁掉末尾的点和空格。如果继续使用原始路径
    # 写 PCAP/keylog，后续 open() 会指向一个并不存在的目录。
    safe_name = safe_name[:80].rstrip(" .")   # 防止超长路径和 Windows 路径归一化
    if not safe_name:
        safe_name = "item"
    return f"{entry.id}-wiki-{safe_name}"


class WikiFetcher:
    """按 URL 和浏览器顺序抓取核心流量，并按需保存辅助产物。"""
    def __init__(
        self,
        output_dir: Path,
        browsers: list[str],
        capture_pcap: bool,
        *,
        save_html: bool = False,
        save_reports: bool = False,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.browsers = browsers
        self.capture_pcap = capture_pcap
        self.proxy = parse_browser_proxy(
            os.getenv("BROWSER_PROXY_URL"),
            name="BROWSER_PROXY_URL",
        )
        self.save_html = save_html
        self.save_reports = save_reports
        self.progress_run_id = uuid.uuid4().hex
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

    def _completion_marker_path(self, url_dir: Path, browser_key: str) -> Path:
        return url_dir / f"capture_{browser_key}.complete.json"

    def _expected_artifacts(self, url_dir: Path, browser_key: str) -> list[Path]:
        paths = [self._key_log_path(url_dir, browser_key)]
        if self.capture_pcap:
            paths.append(self._pcap_path(url_dir, browser_key))
        if self.save_html:
            paths.append(url_dir / f"body_{browser_key}.html")
        return paths

    def _checkpoint_valid(
        self, entry: "UrlEntry", url_dir: Path, browser_key: str
    ) -> bool:
        """仅在原子标记和全部预期文件均有效时跳过采集单元。"""
        marker = self._completion_marker_path(url_dir, browser_key)
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if payload.get("item_id") != entry.id or payload.get("url") != entry.url:
            return False
        if payload.get("browser") != browser_key:
            return False
        sizes = payload.get("artifacts")
        if not isinstance(sizes, dict):
            return False
        for path in self._expected_artifacts(url_dir, browser_key):
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            if sizes.get(path.name) != path.stat().st_size:
                return False
        return True

    def _clear_incomplete_artifacts(self, url_dir: Path, browser_key: str) -> None:
        """重抓前清理半截文件，防止旧非空 PCAP 被误判为新结果。"""
        paths = self._expected_artifacts(url_dir, browser_key)
        paths.append(self._completion_marker_path(url_dir, browser_key))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f"无法清理未完成产物 {path}: {exc}") from exc

    def _mark_complete(
        self,
        entry: "UrlEntry",
        url_dir: Path,
        browser_key: str,
        record: SessionRecord,
    ) -> bool:
        """采集和文件落盘全部成功后，原子提交 URL/浏览器检查点。"""
        expected = self._expected_artifacts(url_dir, browser_key)
        if record.error or not all(
            path.is_file() and path.stat().st_size > 0 for path in expected
        ):
            return False
        marker = self._completion_marker_path(url_dir, browser_key)
        temporary = marker.with_suffix(marker.suffix + ".tmp")
        payload = {
            "version": 2,
            "run_id": self.progress_run_id,
            "item_id": entry.id,
            "name": entry.name,
            "url": entry.url,
            "browser": browser_key,
            "completed_at": datetime.now().isoformat(),
            "artifacts": {path.name: path.stat().st_size for path in expected},
            "skipped_resources": record.skipped_resources,
            "needs_recapture": bool(record.skipped_resources),
            "resource_status": "partial" if record.skipped_resources else "complete",
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(marker)
        return True

    def _write_progress(
        self,
        *,
        position: int,
        total: int,
        entry: "UrlEntry",
        completed_units: int,
        skipped_units: int,
        incomplete_units: int,
        finished: bool = False,
    ) -> None:
        """原子更新批次进度；完成标记仍是续跑的最终依据。"""
        path = self.output_dir / "capture_progress.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": 1,
            "status": "finished" if finished else "running",
            "updated_at": datetime.now().isoformat(),
            "last_processed_position": position,
            "total_urls": total,
            "last_item": {"id": entry.id, "name": entry.name, "url": entry.url},
            "completed_units_this_run": completed_units,
            "skipped_units_from_checkpoint": skipped_units,
            "incomplete_units_this_run": incomplete_units,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

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
        error_html = ""
        skipped_resources = []
        skip_stalled = (
            driver_key in {"chrome", "edge"}
            and urlsplit(url).hostname == "today.hit.edu.cn"
        )

        try:
            log.info("    Starting %s ...", bd.name)
            build_options = {"skip_stalled_resources": True} if skip_stalled else {}
            driver = bd.build(browser_key_log, profile_dir, self.proxy, **build_options)

            t0 = time.perf_counter()

            if isinstance(driver, (webdriver.Chrome, webdriver.Edge)):
                driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                    "headers": {
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                    }
                })

            _prepare_navigation(driver, url)
            driver.get(url)

            if skip_stalled:
                wait_for_resources(driver, skipped_resources)
            else:
                self._wait_for_normal_page(driver)

            response_time_ms = (time.perf_counter() - t0) * 1000

            final_url = driver.current_url
            page_title = driver.title
            html = driver.page_source
            if self.save_reports:
                cookies = driver.get_cookies()

            detected_error = _detect_browser_error_page(final_url, html)
            if detected_error:
                error_msg = detected_error
                error_html = html
                html = ""
                cookies = []
                log.warning("    Error: %s", detected_error)

            time.sleep(0.5)

        except Exception as exc:
            error_msg = str(exc)
            log.warning("    Error: %s", exc)
        finally:
            # Stop recording before any potentially blocking browser cleanup.
            if capture:
                saved = capture.stop()
                actual_pcap_path = str(saved) if saved else None
            if driver:
                try:
                    client_config = getattr(driver.command_executor, "client_config", None)
                    if client_config is not None:
                        client_config.timeout = 5
                    driver.quit()
                except Exception:
                    pass
            if browser_key_log.exists():
                shutil.copy2(browser_key_log, key_log)
            shutil.rmtree(profile_dir, ignore_errors=True)
            os.environ.pop("SSLKEYLOGFILE", None)

        content_hash = hashlib.sha256(html.encode()).hexdigest() if html else ""
        html_length = len(html.encode())

        if skip_stalled:
            # Always overwrite the per-page assessment, including a clean re-run.
            # Also keep append-only history for later filtering or recapture.
            assessment = {
                "page_url": url, "final_url": final_url, "browser": driver_key,
                "run_id": self.progress_run_id,
                "recorded_at": datetime.now().astimezone().isoformat(),
                "needs_recapture": bool(skipped_resources) or bool(error_msg),
                "error": error_msg, "skipped_resources": skipped_resources,
                "resource_status": "failed" if error_msg else (
                    "partial" if skipped_resources else "complete"),
            }
            assessment_path = url_dir / f"resource_status_{driver_key}.json"
            temporary = assessment_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(assessment, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(assessment_path)
            if assessment["needs_recapture"]:
                assessment["artifact_dir"] = str(url_dir)
                with (self.output_dir / "pages_needing_recapture.jsonl").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(json.dumps(assessment, ensure_ascii=False) + "\n")
                log.warning("    Page marked for recapture: %s → %s", url, assessment_path)

        if self.save_html and error_html:
            error_body_path = url_dir / f"error_{driver_key}.html"
            error_body_path.write_text(error_html, encoding="utf-8")
            log.info("    Browser error page → %s (%d bytes)", error_body_path,
                     len(error_html.encode()))

        if self.save_html and html:
            body_path = url_dir / f"body_{driver_key}.html"
            body_path.write_text(html, encoding="utf-8")
            log.info("    Body → %s (%d bytes)", body_path, html_length)

        return SessionRecord(
            browser=bd.name, url=url, timestamp=timestamp, final_url=final_url,
            page_title=page_title, html_length=html_length,
            response_time_ms=response_time_ms, content_hash=content_hash,
            cookies=cookies, key_log_path=str(key_log) if key_log.exists() else None,
            pcap_path=actual_pcap_path, error=error_msg,
            skipped_resources=skipped_resources,
        )

    @staticmethod
    def _wait_for_normal_page(driver) -> None:
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


    # ------------------------------------------------------------------
    # Single entry point
    # ------------------------------------------------------------------

    def _run_entry(
        self, entry: "UrlEntry", position: int, total: int
    ) -> tuple[int, int, int]:
        """依次运行当前词条选择的浏览器，并按需写入辅助报告。"""
        slug = _entry_slug(entry)
        url_dir = self._url_dir(slug)
        url_records: list[SessionRecord] = []

        completed_browsers: set[str] = set()
        for key in self.browsers:
            if key not in AVAILABLE_DRIVERS:
                continue
            if self._checkpoint_valid(entry, url_dir, key):
                completed_browsers.add(key)

        report_path = url_dir / "report.txt"
        captured = 0
        skipped = 0
        incomplete = 0

        for key in self.browsers:
            if key not in AVAILABLE_DRIVERS:
                log.warning("  Unknown browser '%s', skipping.", key)
                continue

            if key in completed_browsers:
                log.info("  → %s [检查点已完成，跳过]", AVAILABLE_DRIVERS[key].name)
                skipped += 1
                continue

            log.info("  → %s", AVAILABLE_DRIVERS[key].name)
            self._clear_incomplete_artifacts(url_dir, key)
            record = self._fetch_with(entry.url, key, url_dir)
            url_records.append(record)
            if self.save_reports:
                self.all_records.append(record)
            if self._mark_complete(entry, url_dir, key, record):
                captured += 1
            else:
                incomplete += 1
                log.warning("    URL 检查点未提交；下次启动会重新抓取该单元")
            if self.save_reports:
                print(record.summary())
                print()
            else:
                log.info(
                    "    Core artifacts: tls_keylog=%s pcap=%s",
                    "saved" if record.key_log_path else "missing",
                    "saved" if record.pcap_path else "disabled/missing",
                )

        if self.save_reports and url_records:
            lines = [
                f"ID   : {entry.id}",
                f"词条 : {entry.name}",
                f"URL  : {entry.url}",
                "=" * 60, "",
            ]
            for r in url_records:
                lines += [f"[{r.browser}]", r.summary(), ""]
            report_path.write_text("\n".join(lines), encoding="utf-8")
        return captured, skipped, incomplete

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------

    def run(self, entries: Iterable["UrlEntry"], *, total: int) -> bool:
        """执行完整批次；URL 之间保留固定间隔以减少相互干扰。"""
        log.info("词条数 : %d", total)
        log.info("Output : %s", self.output_dir)
        log.info("pcap   : %s", "enabled" if self.capture_pcap else "disabled")
        log.info(
            "Browser proxy: %s",
            self.proxy.display_url if self.proxy else "disabled",
        )
        log.info("HTML   : %s", "enabled" if self.save_html else "disabled")
        log.info("reports: %s", "enabled" if self.save_reports else "disabled")
        log.info("Interval between URLs: %.1f s", INTERVAL_BETWEEN_URLS)
        log.info("=" * 60)

        completed_units = 0
        skipped_units = 0
        incomplete_units = 0
        last_entry: Optional[UrlEntry] = None
        for pos, entry in enumerate(entries, start=1):
            log.info("[%d/%d] ID=%s  %s", pos, total, entry.id, entry.name)
            log.info("  %s", entry.url)

            captured, skipped, incomplete = self._run_entry(entry, pos, total)
            completed_units += captured
            skipped_units += skipped
            incomplete_units += incomplete
            last_entry = entry
            if captured or incomplete or pos % 1000 == 0 or pos == total:
                self._write_progress(
                    position=pos,
                    total=total,
                    entry=entry,
                    completed_units=completed_units,
                    skipped_units=skipped_units,
                    incomplete_units=incomplete_units,
                )

            if pos < total and (captured or incomplete):
                log.info("  Waiting %.1f s ...", INTERVAL_BETWEEN_URLS)
                time.sleep(INTERVAL_BETWEEN_URLS)

        if self.save_reports:
            self._write_summary()
        if last_entry is not None:
            self._write_progress(
                position=total,
                total=total,
                entry=last_entry,
                completed_units=completed_units,
                skipped_units=skipped_units,
                incomplete_units=incomplete_units,
                finished=incomplete_units == 0,
            )
        return incomplete_units == 0

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

def _iter_entries(
    txt_path: str,
    id_start: Optional[int] = None,
    id_end: Optional[int] = None,
) -> Iterator[UrlEntry]:
    """
    读取 urls.txt（制表符分隔：ID\\t词条名\\t完整URL）。
    跳过注释行（#）和空行。
    若指定 id_start / id_end，只返回 ID 在 [id_start, id_end] 区间内的词条。
    """
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
                    continue
                if id_end is not None and numeric_id > id_end:
                    continue

            yield UrlEntry(id=entry_id, name=name, url=url)


def _load_entries(
    txt_path: str,
    id_start: Optional[int] = None,
    id_end: Optional[int] = None,
) -> list[UrlEntry]:
    """兼容旧调用；大批量入口使用 _iter_entries 流式读取。"""
    return list(_iter_entries(txt_path, id_start=id_start, id_end=id_end))


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
    parser.add_argument(
        "--save-html", action="store_true",
        help="可选：保存浏览器获取的完整 HTML 页面正文。",
    )
    parser.add_argument(
        "--save-reports", action="store_true",
        help="可选：保存逐 URL report.txt 和批次 summary.txt。",
    )
    args = parser.parse_args()

    # 构建 UrlEntry 列表
    if args.input:
        total = sum(
            1
            for _ in _iter_entries(
                args.input, id_start=args.id_start, id_end=args.id_end
            )
        )
        if total == 0:
            parser.error(f"{args.input} 中未找到符合条件的词条")
        entries: Iterable[UrlEntry] = _iter_entries(
            args.input, id_start=args.id_start, id_end=args.id_end
        )
        log.info("加载词条：%d 条（来自 %s）", total, args.input)
        if args.id_start or args.id_end:
            log.info("ID 范围：[%s, %s]",
                     args.id_start if args.id_start else "起始",
                     args.id_end   if args.id_end   else "结束")
    else:
        # 单个 URL：ID 和名称留空
        entries = [UrlEntry(id="0", name="manual", url=args.url)]
        total = 1

    fetcher = WikiFetcher(
        output_dir=Path(args.output_dir),
        browsers=args.browsers,
        capture_pcap=args.pcap,
        save_html=args.save_html,
        save_reports=args.save_reports,
    )
    if not fetcher.run(entries, total=total):
        log.error("仍有采集单元未完成；退出码 2 将触发 Worker 检查点重试")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
