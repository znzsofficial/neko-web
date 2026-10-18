"""Structured anonymous background retrieval, never delivery or browser actions."""

import asyncio
from datetime import datetime, timezone
import json

import httpx

from .page import page_text
from .providers import ProviderError, post_json
from .public_http import PublicClient, download_public, local_addresses, validate_url
from .retrieval import organize_results, search_sources


def timestamp():
    return datetime.now(timezone.utc).isoformat()


async def search_data(config, options, query):
    async with httpx.AsyncClient(timeout=config.timeout_seconds, **options) as client:
        if config.search_provider == "exa":
            sources, stats = await search_sources(client, config, query, 4, {})
        else:
            headers = {
                "Content-Type": "application/json",
                "X-Anysearch-Client": "neko-web/automation-v1",
            }
            if config.api_key.strip():
                headers["Authorization"] = "Bearer " + config.api_key.strip()
            data = await post_json(
                client,
                "https://api.anysearch.com/mcp",
                headers,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search",
                        "arguments": {"query": query, "max_results": 4},
                    },
                },
            )
            result = data.get("result")
            if (
                data.get("error")
                or not isinstance(result, dict)
                or result.get("isError")
            ):
                raise ProviderError("接口失败", "provider_error")
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                # JSON text is a supported wire representation, not a display-error parser.
                content = result.get("content")
                try:
                    text = next(
                        p["text"]
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                    structured = json.loads(text)
                except (StopIteration, TypeError, KeyError, ValueError):
                    raise ProviderError("未支持的结构化格式", "unsupported") from None
            if not isinstance(structured, dict) or not isinstance(
                structured.get("results"), list
            ):
                raise ProviderError("未支持的结构化格式", "unsupported")
            normalized = []
            for item in structured["results"][:100]:
                if isinstance(item, dict):
                    normalized.append(
                        {
                            **item,
                            "highlights": [
                                str(
                                    item.get("snippet") or item.get("description") or ""
                                )[:1000]
                            ],
                        }
                    )
            sources, stats = organize_results(normalized, 4)
            if normalized and not sources:
                raise ProviderError("结果无效", "invalid_response")
        return {
            "schema_version": 1,
            "success": True,
            "status": "partial" if stats["invalid"] else "ok",
            "results": [
                {
                    "title": x["title"],
                    "url": x["url"],
                    "snippet": x["snippet"],
                    "published_at": x["published"] or None,
                    "fetched_at": timestamp(),
                    "source_kind": "snippet",
                }
                for x in sources
            ],
            "documents": [],
            "truncated": bool(stats["omitted"]),
            "usage": None,
            "cost_known": False,
        }


async def read_data(config, options, url):
    if validate_url(url):
        raise ProviderError("目标无效", "blocked_target")
    blocked = await asyncio.wait_for(asyncio.to_thread(local_addresses), 10)
    async with PublicClient(
        timeout=min(config.timeout_seconds, 30),
        headers={"User-Agent": "neko-web/automation-v1"},
        **options,
    ) as client:
        raw, content_type, final_url = await download_public(
            client,
            url,
            blocked_ips=blocked,
            max_image_bytes=2 * 1024 * 1024,
            page_bytes=2 * 1024 * 1024,
        )
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared not in {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
    }:
        raise ProviderError("非文本网页", "unsupported")
    title, text = page_text(raw, content_type, 4000)
    if not text.strip():
        raise ProviderError("没有提取到正文")
    return {
        "schema_version": 1,
        "success": True,
        "status": "ok",
        "results": [],
        "documents": [
            {
                "url": final_url,
                "title": title[:300],
                "text": text[:4000],
                "source_kind": "body",
                "published_at": None,
                "fetched_at": timestamp(),
            }
        ],
        "truncated": len(text) >= 4000,
        "usage": None,
        "cost_known": False,
    }
