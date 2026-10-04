"""下载公开网页上的图片。不访问内网、本机和云元数据。"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx


class ImageFetchError(ValueError):
    """图片地址不能用，或下载没有得到一张可发送的图片。"""


MAX_REDIRECTS = 3
CANDIDATES_PER_PAGE = 6
PAGE_BYTES = 1_500_000
IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MaiBotAnySearch/0.3)",
    "Accept": "image/webp,image/png,image/jpeg,image/gif,text/html;q=0.9,*/*;q=0.5",
}

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class FetchedImage:
    """一张已经核对过格式的图片。"""

    data: bytes
    mime: str
    source: str

    def caption(self) -> str:
        parsed = urlparse(self.source)
        name = (parsed.path.rsplit("/", 1)[-1] or "image")[:40]
        return f"{parsed.hostname or '图片'} {name}".strip()[:80]


def is_public_ip(raw: str) -> bool:
    """公网单播地址才返回 True。"""

    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if getattr(ip, "is_global", None) is not None:
        return bool(ip.is_global)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def local_addresses() -> set[str]:
    """本机地址。拿到这些地址时直接拒绝，避免经公网 IP 绕回自己。"""

    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            found.add(str(sock.getsockname()[0]))
    except OSError:
        pass
    return {ip for ip in found if ip}


def validate_url(url: str) -> Optional[str]:
    """返回拒绝原因；地址可以用时返回 None。"""

    if not isinstance(url, str):
        return "地址必须是字符串"
    candidate = url.strip()
    if not candidate or len(candidate) > 2000:
        return "地址为空或过长"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "只接受 http 或 https 地址"
    if parsed.username or parsed.password:
        return "地址里不能带账号或密码"
    if parsed.port not in {None, 80, 443}:
        return "只接受 80 或 443 端口"
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata.internal"}:
        return "不能获取本机或元数据地址"
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return "不能获取本机或内网域名"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_ip(str(literal)):
        return "不能获取内网或本机地址"
    return None


def default_resolver(host: str) -> list[str]:
    """解析域名。调用方负责判断这些地址是不是公网。"""

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ImageFetchError("无法解析域名") from exc
    addresses = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise ImageFetchError("无法解析域名")
    return addresses


def sniff_image(data: bytes) -> Optional[str]:
    """只认 JPEG、PNG、GIF、WebP。SVG 和其他内容一律拒绝。"""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _attrs(tag: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"([:\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, re.I):
        found[match.group(1).lower()] = match.group(2) if match.group(2) is not None else match.group(3)
    return found


def _skip_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".svg", ".ico", ".css", ".js", ".m3u8", ".mp4", ".webm"))


def image_urls_in_document(text: str, base_url: str, limit: int = 8) -> list[str]:
    """从 HTML 或 Markdown 里收集图片地址。优先 og:image。"""

    if not text or limit <= 0:
        return []
    found: list[str] = []

    def add(raw: str) -> None:
        if len(found) >= limit or not raw:
            return
        absolute = urljoin(base_url, raw.strip())
        if validate_url(absolute) or _skip_image_url(absolute) or absolute in found:
            return
        found.append(absolute)

    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        attrs = _attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            add(attrs.get("content") or "")
    for tag in re.findall(r"<img\b[^>]*>", text, re.I):
        attrs = _attrs(tag)
        if attrs.get("width") in {"0", "1"} or attrs.get("height") in {"0", "1"}:
            continue
        add(attrs.get("src") or "")
    for raw in re.findall(r"!\[[^\]]*]\((https?://[^)\s]+)\)", text, re.I):
        add(raw)
    return found


def _check_targets(host: str, ips: Iterable[str], blocked_ips: set[str]) -> None:
    usable = [str(ip) for ip in ips]
    if not usable:
        raise ImageFetchError("无法解析域名")
    for raw in usable:
        if raw in blocked_ips or not is_public_ip(raw):
            raise ImageFetchError("不能获取内网或本机地址")
        try:
            canonical = str(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise ImageFetchError("无法解析域名") from exc
        if canonical in blocked_ips:
            raise ImageFetchError("不能获取内网或本机地址")
    del host


def _body_limit(content_type: str, max_image_bytes: int) -> int:
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared.startswith("image/") or declared in {"", "application/octet-stream", "binary/octet-stream"}:
        return max_image_bytes
    return PAGE_BYTES


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ImageFetchError("文件超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


def _peer_is_blocked(response: httpx.Response, blocked_ips: set[str]) -> bool:
    stream = response.extensions.get("network_stream")
    getter = getattr(stream, "get_extra_info", None)
    if getter is None:
        return False
    addr = getter("server_addr")
    if not isinstance(addr, tuple) or not addr:
        return False
    peer = str(addr[0])
    return peer in blocked_ips or not is_public_ip(peer)


async def download_public(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    blocked_ips: Optional[set[str]] = None,
    allow_unresolved: bool = False,
    check_peer: bool = True,
    max_image_bytes: int = 8 * 1024 * 1024,
) -> tuple[bytes, str, str]:
    """下载一个公开地址。返回正文、Content-Type 和最终地址。"""

    return await _load(
        client,
        url,
        resolver=resolver or default_resolver,
        blocked_ips=blocked_ips or set(),
        allow_unresolved=allow_unresolved,
        check_peer=check_peer,
        max_image_bytes=max_image_bytes,
    )


async def _load(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Resolver,
    blocked_ips: set[str],
    allow_unresolved: bool,
    check_peer: bool,
    max_image_bytes: int,
) -> tuple[bytes, str, str]:
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        reason = validate_url(current)
        if reason:
            raise ImageFetchError(reason)
        host = urlparse(current).hostname or ""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            _check_targets(host, [str(literal)], blocked_ips)
        else:
            try:
                ips = resolver(host)
            except ImageFetchError:
                if not allow_unresolved:
                    raise
                ips = []
            except OSError as exc:
                if not allow_unresolved:
                    raise ImageFetchError("无法解析域名") from exc
                ips = []
            if ips:
                _check_targets(host, ips, blocked_ips)
            elif not allow_unresolved:
                raise ImageFetchError("无法解析域名")
        try:
            async with client.stream("GET", current) as response:
                if check_peer and _peer_is_blocked(response, blocked_ips):
                    raise ImageFetchError("不能获取内网或本机地址")
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location") or ""
                    if not location:
                        raise ImageFetchError("重定向没有目标")
                    current = urljoin(str(response.url), location)
                    continue
                if response.status_code != 200:
                    raise ImageFetchError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type") or ""
                data = await _read_limited(response, _body_limit(content_type, max_image_bytes))
                return data, content_type, str(response.url)
        except ImageFetchError:
            raise
        except httpx.TimeoutException as exc:
            raise ImageFetchError("下载超时") from exc
        except httpx.HTTPError as exc:
            raise ImageFetchError("下载失败") from exc
    raise ImageFetchError("重定向次数过多")


def is_html(data: bytes, content_type: str) -> bool:
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared in {"text/html", "application/xhtml+xml"}:
        return True
    if declared.startswith("image/") or declared == "application/json":
        return False
    sample = data.lstrip()[:300].lower()
    return sample.startswith((b"<!doctype html", b"<html")) or b"<img" in sample or b"og:image" in sample


async def collect_images(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    resolver: Optional[Resolver] = None,
    blocked_ips: Optional[set[str]] = None,
    allow_unresolved: bool = False,
    check_peer: bool = True,
    max_images: int = 4,
    max_image_bytes: int = 8 * 1024 * 1024,
) -> tuple[list[FetchedImage], list[str]]:
    """按顺序下载图片。网页只取 og:image 和少量 img，不再往下翻页。"""

    lookup = resolver or default_resolver
    blocked = blocked_ips or set()
    images: list[FetchedImage] = []
    notes: list[str] = []

    async def one(target: str) -> FetchedImage:
        data, content_type, final = await _load(
            client,
            target,
            resolver=lookup,
            blocked_ips=blocked,
            allow_unresolved=allow_unresolved,
            check_peer=check_peer,
            max_image_bytes=max_image_bytes,
        )
        mime = sniff_image(data)
        if mime:
            return FetchedImage(data, mime, final)
        if is_html(data, content_type):
            raise _HtmlPage(data, final)
        raise ImageFetchError("不是支持的图片")

    for url in urls:
        if len(images) >= max_images:
            break
        try:
            images.append(await one(url))
        except _HtmlPage as page:
            candidates = image_urls_in_document(
                page.data.decode("utf-8", "replace"), page.final, CANDIDATES_PER_PAGE
            )
            if not candidates:
                notes.append(f"{urlparse(url).hostname or '页面'}：没有可发送的图片")
                continue
            found = False
            for candidate in candidates:
                if len(images) >= max_images:
                    break
                try:
                    images.append(await one(candidate))
                    found = True
                except _HtmlPage:
                    notes.append(f"{urlparse(candidate).hostname or '图片'}：打开后仍是网页")
                except ImageFetchError as exc:
                    notes.append(f"{urlparse(candidate).hostname or '图片'}：{exc}")
            if not found:
                notes.append(f"{urlparse(page.final).hostname or '页面'}：图片都没能下载")
        except ImageFetchError as exc:
            notes.append(f"{urlparse(url).hostname or '地址'}：{exc}")
    return images, notes


class _HtmlPage(Exception):
    def __init__(self, data: bytes, final: str) -> None:
        self.data = data
        self.final = final
