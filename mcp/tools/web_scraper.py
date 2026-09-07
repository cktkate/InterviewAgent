"""Safe webpage text extraction for JD and other public web pages."""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


class WebScraperError(RuntimeError):
    """Raised when a page cannot be fetched or safely parsed."""


DEFAULT_TIMEOUT_SECONDS = 12.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 30_000

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 InterviewAgent/1.0"
)
_REMOVED_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "nav",
    "footer",
    "header",
    "form",
    "button",
)


def is_web_url(value: str) -> bool:
    """Return whether the whole input is one HTTP(S) URL."""
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _validate_public_url(url: str) -> str:
    """Reject malformed URLs and destinations that resolve to private networks."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebScraperError("只支持有效的 http/https 网页地址")
    if parsed.username or parsed.password:
        raise WebScraperError("网页地址不能包含用户名或密码")

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise WebScraperError("网页端口不合法") from exc

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, port, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise WebScraperError("无法解析网页域名") from exc

    if not addresses:
        raise WebScraperError("网页域名没有可用地址")

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise WebScraperError("出于安全原因，不允许访问本机或内网地址")

    return parsed.geturl()


def extract_page_text(html: str, url: str = "") -> Dict[str, str]:
    """Extract a readable title and main text from an HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    for tag in soup.find_all(_REMOVED_TAGS):
        tag.decompose()

    root = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    raw_text = root.get_text("\n", strip=True)
    lines = []
    previous = ""
    for line in raw_text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned and cleaned != previous:
            lines.append(cleaned)
            previous = cleaned

    text = "\n".join(lines)[:MAX_TEXT_CHARS]
    if len(text) < 80:
        raise WebScraperError(
            "网页正文过少，页面可能依赖JavaScript动态渲染或限制了访问"
        )

    return {"url": url, "title": title[:300], "text": text}


def _read_limited_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise WebScraperError("网页内容超过2MB限制")
        except ValueError:
            pass

    chunks = bytearray()
    for chunk in response.iter_bytes():
        chunks.extend(chunk)
        if len(chunks) > MAX_RESPONSE_BYTES:
            raise WebScraperError("网页内容超过2MB限制")
    return bytes(chunks)


def fetch_webpage(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Fetch a public HTML page and return structured readable text.

    Redirects are followed manually so every destination receives the same
    public-network validation.
    """
    current_url = _validate_public_url(url)
    owns_client = client is None
    http = client or httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )

    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                with http.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebScraperError("网页重定向缺少目标地址")
                        if redirect_count >= MAX_REDIRECTS:
                            raise WebScraperError("网页重定向次数过多")
                        current_url = _validate_public_url(
                            urljoin(current_url, location)
                        )
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(
                        allowed in content_type
                        for allowed in ("text/html", "application/xhtml+xml", "text/plain")
                    ):
                        raise WebScraperError(
                            f"不支持的网页内容类型: {content_type or 'unknown'}"
                        )
                    body = _read_limited_response(response)
                    encoding = response.encoding or "utf-8" 
                    html = body.decode(encoding, errors="replace")
                    result = extract_page_text(html, current_url)
                    result.update(
                        {
                            "status": "success",
                            "content_type": content_type,
                            "text_length": len(result["text"]),
                        }
                    )
                    return result
            except httpx.TimeoutException as exc:
                raise WebScraperError("网页请求超时") from exc
            except httpx.HTTPStatusError as exc:
                raise WebScraperError(
                    f"网页请求失败，HTTP状态码: {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                raise WebScraperError(f"网页请求失败: {exc}") from exc

        raise WebScraperError("网页重定向次数过多")
    finally:
        if owns_client:
            http.close()
