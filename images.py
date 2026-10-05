"""下载公开网页上的图片。不访问内网、本机和云元数据。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

try:
    from .public_http import (PublicFetchError as ImageFetchError, default_resolver,
                              download_public as _download, is_public_ip, local_addresses, validate_url)
except ImportError:
    from public_http import (PublicFetchError as ImageFetchError, default_resolver,
                             download_public as _download, is_public_ip, local_addresses, validate_url)

CANDIDATES_PER_PAGE = 6
PAGE_BYTES = 1_500_000
IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NekoWeb/1.0)",
    "Accept": "image/webp,image/png,image/jpeg,image/gif,text/html;q=0.9,*/*;q=0.5",
}
Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    mime: str
    source: str

    def caption(self) -> str:
        parsed = urlparse(self.source)
        name = (parsed.path.rsplit("/", 1)[-1] or "image")[:40]
        return f"{parsed.hostname or '图片'} {name}".strip()[:80]


def sniff_image(data: bytes) -> Optional[str]:
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


def image_urls_in_document(text: str, base_url: str, limit: int = 8) -> list[str]:
    if not text or limit <= 0:
        return []
    found: list[str] = []

    def add(raw: str) -> None:
        if len(found) >= limit or not raw:
            return
        try:
            absolute = urljoin(base_url, raw.strip())
            if validate_url(absolute) or absolute in found:
                return
            if urlparse(absolute).path.lower().endswith((".svg", ".ico", ".css", ".js", ".m3u8", ".mp4", ".webm")):
                return
        except (ValueError, UnicodeError):
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


@dataclass(frozen=True)
class FetchPolicy:
    resolver: Resolver
    blocked_ips: frozenset[str]
    max_images: int = 4
    max_image_bytes: int = 8 * 1024 * 1024


def make_policy(*, resolver=None, blocked_ips=None, max_images=4,
                max_image_bytes=8 * 1024 * 1024, allow_unresolved=False, check_peer=True) -> FetchPolicy:
    # Legacy flags cannot weaken the address policy, even through a proxy.
    return FetchPolicy(resolver or default_resolver, frozenset(blocked_ips or ()), max_images, max_image_bytes)


async def download_public(client, url, *, policy=None, **options):
    active = policy or make_policy(**options)
    return await _download(client, url, resolver=active.resolver, blocked_ips=active.blocked_ips,
                           max_image_bytes=active.max_image_bytes, page_bytes=PAGE_BYTES)


class HtmlPage(Exception):
    def __init__(self, data: bytes, final: str) -> None:
        self.data, self.final = data, final


def is_html(data: bytes, content_type: str) -> bool:
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared in {"text/html", "application/xhtml+xml"}:
        return True
    if declared.startswith("image/") or declared == "application/json":
        return False
    sample = data.lstrip()[:300].lower()
    return sample.startswith((b"<!doctype html", b"<html")) or b"<img" in sample or b"og:image" in sample


async def load_image(client, url: str, policy: FetchPolicy) -> FetchedImage:
    data, content_type, final = await download_public(client, url, policy=policy)
    mime = sniff_image(data)
    if mime:
        return FetchedImage(data, mime, final)
    if is_html(data, content_type):
        raise HtmlPage(data, final)
    raise ImageFetchError("不是支持的图片")


async def download_candidate_images(client, candidates, policy, images, notes) -> bool:
    found = False
    for candidate in candidates:
        if len(images) >= policy.max_images:
            break
        try:
            images.append(await load_image(client, candidate, policy))
            found = True
        except HtmlPage:
            notes.append("打开后仍是网页")
        except ImageFetchError as exc:
            notes.append(str(exc))
    return found


async def collect_images(client, urls, *, policy=None, **options):
    active = policy or make_policy(**options)
    images, notes = [], []
    for url in urls:
        if len(images) >= active.max_images:
            break
        try:
            images.append(await load_image(client, url, active))
        except HtmlPage as page:
            candidates = image_urls_in_document(page.data.decode("utf-8", "replace"), page.final, CANDIDATES_PER_PAGE)
            if not candidates:
                notes.append("页面没有可发送的图片")
            elif not await download_candidate_images(client, candidates, active, images, notes):
                notes.append("页面图片都没能下载")
        except ImageFetchError as exc:
            notes.append(str(exc))
    return images, notes
