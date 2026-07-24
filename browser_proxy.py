"""浏览器代理配置的解析与校验。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrowserProxy:
    """经过校验、可同时用于 Chromium 和 Firefox 的代理地址。"""

    url: str
    scheme: str
    host: str
    port: int

    @property
    def display_url(self) -> str:
        """返回不包含敏感信息的日志展示地址。"""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def parse_browser_proxy(
    value: object,
    *,
    name: str = "network.proxy_url",
) -> BrowserProxy | None:
    """解析代理 URL；空值表示浏览器直连。"""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是代理 URL 字符串或留空")

    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} 不是有效的代理 URL：{exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "socks4", "socks5"}:
        raise ValueError(f"{name} 仅支持 http、socks4 或 socks5")
    if not parsed.hostname or port is None:
        raise ValueError(f"{name} 必须包含主机和端口，例如 http://127.0.0.1:7890")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} 暂不支持用户名或密码认证")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{name} 不能包含路径、查询参数或片段")

    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host else host
    normalized = f"{scheme}://{display_host}:{port}"
    return BrowserProxy(
        url=normalized,
        scheme=scheme,
        host=host,
        port=port,
    )
