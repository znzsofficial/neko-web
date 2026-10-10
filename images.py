"""下载公开网页上的图片。不访问内网、本机和云元数据。"""
from __future__ import annotations

import re
import asyncio
import io
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from math import isfinite
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from .public_http import (PublicFetchError as ImageFetchError, default_resolver,
                              download_public as _download, local_addresses as local_addresses, validate_url)
except ImportError:
    from public_http import (PublicFetchError as ImageFetchError, default_resolver,
                             download_public as _download, local_addresses as local_addresses, validate_url)

CANDIDATES_PER_PAGE = 48
PAGE_BYTES = 1_500_000
MAX_PIXELS = 20_000_000
MAX_FRAME_PIXELS = 40_000_000
MAX_PREVIEW_BYTES = 256 * 1024
_IMAGE_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix='neko-image')
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
    width: int = 0
    height: int = 0
    original_sha256: str = ''
    original_size: int = 0
    original_mime: str = ''

    def caption(self) -> str:
        parsed = urlparse(self.source)
        name = (parsed.path.rsplit("/", 1)[-1] or "image")[:40]
        size = f' {self.width}×{self.height}' if self.width and self.height else ''
        quality = '（低清候选）' if self.width and min(self.width, self.height) < 720 else ''
        return f"{parsed.hostname or '图片'} {name}{size}{quality}".strip()[:120]


def prepare_image(data: bytes, source: str, *, preview: bool = True) -> FetchedImage:
    """Validate real decoding and bound pixels/frames; never reencode originals."""
    import hashlib
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'GIF': 'image/gif', 'WEBP': 'image/webp'}.get(image.format)
                if not mime:
                    raise ImageFetchError('不是支持的图片')
                width, height = image.size
                frames = getattr(image, 'n_frames', 1)
                if width * height > MAX_PIXELS or width * height * frames > MAX_FRAME_PIXELS or frames > 100:
                    raise ImageFetchError('图片像素或动图帧数超过限制，请使用原链接')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                for index in range(frames):
                    image.seek(index)
                    if image.width * image.height > MAX_PIXELS:
                        raise ImageFetchError('图片像素超过限制')
                    image.load()
                image.seek(0)
                if preview:
                    oriented = ImageOps.exif_transpose(image)
                    width, height = oriented.size
                    try:
                        thumb = oriented.convert('RGB')
                        try:
                            thumb.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                            for quality in (85, 70, 55):
                                output = io.BytesIO()
                                thumb.save(output, format='JPEG', quality=quality, optimize=True)
                                if output.tell() <= MAX_PREVIEW_BYTES:
                                    break
                                thumb.thumbnail((max(1, thumb.width * 3 // 4), max(1, thumb.height * 3 // 4)))
                            else:
                                raise ImageFetchError('无法生成限量预览')
                            result = output.getvalue()
                        finally:
                            thumb.close()
                    finally:
                        oriented.close()
                else:
                    result = data
    except ImageFetchError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageFetchError('图片损坏、无法解码或像素超过限制') from None
    return FetchedImage(result, 'image/jpeg' if preview else mime, source, width, height,
                        hashlib.sha256(data).hexdigest(), len(data), mime)


async def decode_image(data, source, *, preview=True):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_IMAGE_WORKERS, lambda: prepare_image(data, source, preview=preview))


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


def normalize_image_url(raw: str, base_url: str = "") -> Optional[str]:
    """Resolve and validate a public URL, preserving its path and query identity."""
    if not isinstance(raw, str) or not raw.strip() or any(ord(c) < 32 for c in raw):
        return None
    try:
        absolute = urljoin(base_url, raw.strip())
        if validate_url(absolute):
            return None
        parsed = urlparse(absolute)
        return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), fragment="").geturl()
    except (ValueError, UnicodeError):
        return None


_UNSUPPORTED_SUFFIXES = (
    ".svg", ".ico", ".css", ".js", ".m3u8", ".mp4", ".webm",
    ".avif", ".heic", ".heif", ".bmp", ".tif", ".tiff", ".jxl",
)
_SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\((https?://[^)\s]+)\)", re.I)


def _srcset_candidates(value: str):
    """Yield ranked URL tokens without splitting commas inside URL tokens."""
    position = 0
    while position < len(value):
        while position < len(value) and (value[position].isspace() or value[position] == ","):
            position += 1
        match = re.match(r"\S+", value[position:])
        if not match:
            break
        raw = match.group()
        position += len(raw)
        descriptor = ""
        if raw.endswith(","):
            raw = raw.rstrip(",")
        else:
            end = value.find(",", position)
            if end == -1:
                end = len(value)
            descriptor = value[position:end].strip()
            position = end + 1
        if not descriptor:
            yield 1.0, raw
        elif re.fullmatch(r"(?:[0-9]+w|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x)", descriptor):
            rank = float(descriptor[:-1])
            if rank > 0 and isfinite(rank):
                yield rank, raw


class _ImageParser(HTMLParser):
    def __init__(self, base_url: str, limit: int):
        super().__init__(convert_charrefs=True)
        self.base_url, self.limit = base_url, limit
        self.meta: list[str] = []
        self.images: list[str] = []
        self.markdown: list[str] = []
        self.ignored: Optional[str] = None
        self.picture = False
        self.picture_source: Optional[str] = None
        self.picture_image: Optional[str] = None
        self.picture_hidden = False
        self.anchor_original: Optional[str] = None

    def candidate(self, raw: str) -> Optional[str]:
        url = normalize_image_url(raw, self.base_url)
        if url and not urlparse(url).path.lower().endswith(_UNSUPPORTED_SUFFIXES):
            return url
        return None

    def add(self, target: list[str], url: Optional[str]) -> None:
        if url and url not in target and len(target) < self.limit:
            target.append(url)

    def best(self, attrs: dict[str, str]) -> Optional[str]:
        # Only use original URLs actually supplied by the page. Do not strip
        # resize/signature parameters or invent CDN variants.
        for key in ('data-original', 'data-full', 'data-fullsize', 'data-large', 'data-zoom-image'):
            original = self.candidate(attrs.get(key, ''))
            if original:
                return original
        for key in ("data-srcset", "srcset"):
            best_url, best_rank = None, -1.0
            for rank, raw in _srcset_candidates(attrs.get(key, "")):
                url = self.candidate(raw)
                if url and rank > best_rank:
                    best_url, best_rank = url, rank
            if best_url:
                return best_url
        for key in ("data-src", "data-original", "data-lazy-src", "src"):
            url = self.candidate(attrs.get(key, ""))
            if url:
                return url
        return None

    def finish_picture(self) -> None:
        if self.picture and not self.picture_hidden:
            self.add(self.images, self.anchor_original or self.picture_source or self.picture_image)
        self.picture = False
        self.picture_source = self.picture_image = None
        self.picture_hidden = False

    def handle_starttag(self, tag, attributes):
        if self.ignored:
            return
        if tag in {"script", "style", "textarea", "title"}:
            self.ignored = tag
            return
        attrs = {key: value or "" for key, value in attributes}
        if tag == 'a':
            href = self.candidate(attrs.get('href', ''))
            self.anchor_original = href if href and urlparse(href).path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')) else None
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
                self.add(self.meta, self.candidate(attrs.get("content", "")))
        elif tag == "picture":
            self.finish_picture()
            self.picture = True
        elif tag == "source" and self.picture and not self.picture_source:
            mime = attrs.get("type", "").split(";", 1)[0].strip().lower()
            if not mime or mime in _SUPPORTED_TYPES:
                self.picture_source = self.best(attrs)
        elif tag == "img":
            hidden = attrs.get("width") in {"0", "1"} or attrs.get("height") in {"0", "1"}
            if self.picture:
                self.picture_hidden = self.picture_hidden or hidden
                if not self.picture_image and not hidden:
                    self.picture_image = self.best(attrs)
            elif not hidden:
                self.add(self.images, self.anchor_original or self.best(attrs))

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored:
                self.ignored = None
        elif tag == "picture":
            self.finish_picture()
        elif tag == 'a':
            self.anchor_original = None

    def handle_data(self, data):
        if not self.ignored:
            for match in _MARKDOWN_IMAGE.finditer(data):
                self.add(self.markdown, self.candidate(match.group(1)))


def image_urls_in_document(text: str, base_url: str, limit: int = 8) -> list[str]:
    if not text or limit <= 0:
        return []
    limit = min(limit, CANDIDATES_PER_PAGE)
    parser = _ImageParser(base_url, limit)
    parser.feed(text)
    parser.close()
    parser.finish_picture()
    return list(dict.fromkeys(parser.meta + parser.images + parser.markdown))[:limit]


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


async def load_image(client, url: str, policy: FetchPolicy, *, preview=True) -> FetchedImage:
    data, content_type, final = await download_public(client, url, policy=policy)
    mime = sniff_image(data)
    if mime:
        return await decode_image(data, final, preview=preview)
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
