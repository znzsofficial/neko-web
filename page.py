"""把公开网页收成标题和正文。不执行页面里的脚本。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx

try:
    from .images import (
        FetchedImage,
        ImageFetchError,
        download_public,
        image_urls_in_document,
        is_html,
        sniff_image,
    )
except ImportError:
    from images import (
        FetchedImage,
        ImageFetchError,
        download_public,
        image_urls_in_document,
        is_html,
        sniff_image,
    )


TEXT_CANDIDATES = 6


@dataclass(frozen=True)
class PageRead:
    """一次打开网页的结果。图片已经核对过格式，正文是纯文本。"""

    final_url: str
    title: str
    text: str
    images: list[FetchedImage]
    notes: list[str]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.description = ""
        self._parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "template"}:
            self._skip += 1
            return
        if name == "title":
            self._in_title = True
        values = {key.lower(): value or "" for key, value in attrs}
        meta = (values.get("property") or values.get("name") or "").lower()
        if name == "meta" and meta in {"description", "og:description"} and not self.description:
            self.description = values.get("content") or ""
        if name == "meta" and meta == "og:title" and not self.og_title:
            self.og_title = values.get("content") or ""
        if name in {"p", "br", "div", "h1", "h2", "h3", "li", "tr", "section", "article"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "template"} and self._skip:
            self._skip -= 1
        if name == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self._parts.append(text)


def _collapse(parts: list[str], limit: int) -> str:
    lines: list[str] = []
    for part in parts:
        if part == "\n":
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(part)
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（正文已截断）"


def page_text(data: bytes, content_type: str, limit: int) -> tuple[str, str]:
    """从 HTML、JSON 或纯文本里取出标题和正文。"""

    declared = content_type.split(";", 1)[0].strip().lower()
    charset = "utf-8"
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip(" \"'")
    try:
        text = data.decode(charset, "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    if declared == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
        return "", _collapse([text], limit)
    if not is_html(data, content_type) and declared.startswith("text/"):
        return "", _collapse(text.splitlines(), limit)
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    title = " ".join((parser.title or parser.og_title).split())
    description = " ".join(parser.description.split())
    body = _collapse(parser._parts, limit)
    if description and description not in body[:400]:
        body = _collapse([description, "\n", body], limit)
    return title[:200], body


async def read_public_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver=None,
    blocked_ips=None,
    allow_unresolved: bool = False,
    check_peer: bool = True,
    max_images: int = 4,
    max_image_bytes: int = 8 * 1024 * 1024,
    text_limit: int = 8000,
) -> PageRead:
    """打开一个公开地址，读取正文并下载其中的图片。"""

    options = dict(
        resolver=resolver,
        blocked_ips=blocked_ips,
        allow_unresolved=allow_unresolved,
        check_peer=check_peer,
        max_image_bytes=max_image_bytes,
    )
    data, content_type, final = await download_public(client, url, **options)
    mime = sniff_image(data)
    if mime:
        return PageRead(final, "", "", [FetchedImage(data, mime, final)], [])
    title, text = page_text(data, content_type, text_limit)
    images: list[FetchedImage] = []
    notes: list[str] = []
    if not is_html(data, content_type):
        return PageRead(final, title, text, images, notes)
    document = data.decode("utf-8", "replace")
    for candidate in image_urls_in_document(document, final, TEXT_CANDIDATES):
        if len(images) >= max_images:
            break
        try:
            image_data, image_type, image_url = await download_public(client, candidate, **options)
        except ImageFetchError as exc:
            notes.append(f"{urlparse(candidate).hostname or '图片'}：{exc}")
            continue
        image_mime = sniff_image(image_data)
        if not image_mime or is_html(image_data, image_type):
            notes.append(f"{urlparse(candidate).hostname or '图片'}：不是支持的图片")
            continue
        images.append(FetchedImage(image_data, image_mime, image_url))
    return PageRead(final, title, text, images, notes)
