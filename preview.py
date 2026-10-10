"""Bounded, chat-scoped candidate queues. Never send messages or retain image bytes."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import secrets
import time

try:
    from .images import (CANDIDATES_PER_PAGE, HtmlPage, ImageFetchError,
                         image_urls_in_document, load_image, normalize_image_url)
except ImportError:
    from images import (CANDIDATES_PER_PAGE, HtmlPage, ImageFetchError,
                        image_urls_in_document, load_image, normalize_image_url)


@dataclass
class Queue:
    scope: str
    expires: float
    pending: deque = field(default_factory=deque)
    seen: set = field(default_factory=set)
    final_urls: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    number: int = 0
    limited: bool = False


class PreviewPager:
    TTL = 600
    MAX_QUEUES = 64
    MAX_PER_CHAT = 4
    MAX_CANDIDATES = 192
    MAX_ATTEMPTS = 12
    BATCH_SECONDS = 45

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.queues = {}
        self.active = {}
        self.generation = 0

    def clear(self):
        self.queues.clear()
        self.generation += 1

    def _prune(self):
        now = self.clock()
        for token, state in list(self.queues.items()):
            if state.expires <= now:
                del self.queues[token]

    def _enqueue(self, state, urls, expand):
        for url in urls:
            normalized = normalize_image_url(url) or url.strip()
            if normalized in state.seen:
                continue
            if len(state.seen) >= self.MAX_CANDIDATES:
                state.limited = True
                break
            state.seen.add(normalized)
            state.pending.append((normalized, expand))

    def create(self, scope, urls, *, expand=True):
        self._prune()
        same = [key for key, value in self.queues.items() if value.scope == scope]
        inflight = sum(state.scope == scope for state in self.active.values())
        if inflight >= self.MAX_PER_CHAT or len(self.active) >= self.MAX_QUEUES:
            raise ImageFetchError('图片预览任务繁忙，请稍后再试')
        while len(same) + inflight >= self.MAX_PER_CHAT:
            del self.queues[same.pop(0)]
        while len(self.queues) + len(self.active) >= self.MAX_QUEUES:
            del self.queues[next(iter(self.queues))]
        state = Queue(scope=scope, expires=self.clock() + self.TTL)
        self._enqueue(state, urls, expand)
        token = secrets.token_urlsafe(24)
        self.queues[token] = state
        return token

    async def batch(self, client, scope, token, policy, *, initial_images=()):
        self._prune()
        state = self.queues.get(token)
        if state is None or state.scope != scope:
            raise ImageFetchError("预览游标无效、已用过或已过期，请重新打开原链接。")
        # Consume before awaiting: concurrent calls cannot download the same batch.
        del self.queues[token]
        self.active[token] = state
        generation = self.generation
        try:
            return await self._batch(client, scope, state, policy, initial_images=initial_images, generation=generation)
        finally:
            self.active.pop(token, None)

    async def _batch(self, client, scope, state, policy, *, initial_images, generation):
        images, details = [], []

        def accept(image, source, number):
            final = normalize_image_url(image.source)
            digest = image.original_sha256 or hashlib.sha256(image.data).hexdigest()
            if final in state.final_urls or digest in state.hashes:
                details.append({"candidate": number, "url": source, "status": "duplicate"})
                return
            state.final_urls.add(final)
            state.hashes.add(digest)
            images.append(image)
            details.append({"candidate": number, "url": source, "status": "ready",
                            "media_item": len(images), "final_url": image.source,
                            "width": image.width, "height": image.height})

        for image in initial_images:
            state.number += 1
            accept(image, image.source, state.number)

        attempts = 0
        deadline = self.clock() + self.BATCH_SECONDS
        while state.pending and len(images) < policy.max_images and attempts < self.MAX_ATTEMPTS:
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            url, expand = state.pending.popleft()
            state.number += 1
            number = state.number
            attempts += 1
            if url in state.final_urls:
                details.append({"candidate": number, "url": url, "status": "duplicate"})
                continue
            try:
                image = await asyncio.wait_for(load_image(client, url, policy), timeout=remaining)
                accept(image, url, number)
            except HtmlPage as page:
                if not expand:
                    details.append({"candidate": number, "url": url, "status": "failed",
                                    "reason": "图片地址返回网页"})
                    continue
                candidates = image_urls_in_document(page.data.decode('utf-8', 'replace'),
                                                    page.final, CANDIDATES_PER_PAGE + 1)
                if len(candidates) >= CANDIDATES_PER_PAGE:
                    state.limited = True
                self._enqueue(state, candidates[:CANDIDATES_PER_PAGE], False)
                details.append({"candidate": number, "url": url, "status": "page",
                                "discovered": min(len(candidates), CANDIDATES_PER_PAGE)})
            except (ImageFetchError, TimeoutError) as exc:
                details.append({"candidate": number, "url": url, "status": "failed",
                                "reason": str(exc) or "下载超时"})
            except Exception:
                details.append({"candidate": number, "url": url, "status": "failed",
                                "reason": "下载未完成"})

        next_cursor = ""
        if state.pending and state.expires > self.clock() and generation == self.generation:
            next_cursor = secrets.token_urlsafe(24)
            while len(self.queues) + len(self.active) > self.MAX_QUEUES and self.queues:
                del self.queues[next(iter(self.queues))]
            self.queues[next_cursor] = state
        images.sort(key=lambda image: image.width * image.height, reverse=True)
        for item in details:
            if item.get('status') == 'ready':
                source_order = [image.source for image in images]
                # Candidate URL may redirect; retain the exact accepted source.
                source = item.get('final_url')
                if source in source_order:
                    item['media_item'] = source_order.index(source) + 1
        return images, details, next_cursor, len(state.pending), state.limited
