import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
import unittest

from test_support import TestPlugin, plugin
from automation_auth import sign

KEY = "45" * 32


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = TestPlugin()
        self.bot.ctx.paths = type("Paths", (), {"data_dir": self.temp.name})()
        await self.bot.on_load()
        self.addAsyncCleanup(self.bot.on_unload)

    def keyfile(self):
        p = Path(self.temp.name) / "automation-keys.json"
        p.write_text(json.dumps({"neko.life": KEY}), encoding="utf-8")
        p.chmod(0o600)

    def packet(self, op, body):
        return sign(KEY, "neko.life", "neko.web", op, body)

    async def test_disabled_by_default_no_database_or_network(self):
        result = await self.bot.automation_v1(packet=self.packet("capabilities", {}))
        self.assertEqual(result["status"], "disabled")
        self.assertFalse((Path(self.temp.name) / "automation.db").exists())

    async def test_no_keys_or_spoofed_identity_cannot_initialize_service(self):
        self.bot.config.automation.enabled = True
        for packet in [
            None,
            {"caller_plugin_id": "neko.life"},
            self.packet("capabilities", {}),
        ]:
            result = await self.bot.automation_v1(packet=packet)
            self.assertFalse(result["success"])
        self.assertIsNone(self.bot._automation)

    async def test_authenticated_capabilities_replay_and_async_job(self):
        self.bot.config.automation.enabled = True
        self.keyfile()
        request = self.packet("capabilities", {})
        self.assertTrue((await self.bot.automation_v1(packet=request))["success"])
        self.assertEqual(
            (await self.bot.automation_v1(packet=request))["status"], "replay"
        )
        released = asyncio.Event()

        async def execute(*_):
            await released.wait()
            return {"success": True, "status": "ok", "results": []}

        self.bot._automation.execute = execute
        result = await asyncio.wait_for(
            self.bot.automation_v1(
                packet=self.packet(
                    "submit",
                    {
                        "request_id": "r",
                        "kind": "search",
                        "arguments": {"query": "public topic"},
                    },
                )
            ),
            1,
        )
        self.assertTrue(result["success"])
        released.set()
        task = self.bot._automation.tasks.get(result["job_id"])
        if task:
            await task
        final = await self.bot.automation_v1(
            packet=self.packet("get", {"job_id": result["job_id"]})
        )
        self.assertEqual(final["status"], "succeeded")

    async def test_reload_cancels_inflight_and_preserves_job_as_unknown(self):
        self.bot.config.automation.enabled = True
        self.keyfile()
        await self.bot.automation_v1(packet=self.packet("capabilities", {}))
        entered = asyncio.Event()

        async def execute(*_):
            entered.set()
            await asyncio.Future()

        self.bot._automation.execute = execute
        result = await self.bot.automation_v1(
            packet=self.packet(
                "submit",
                {
                    "request_id": "r",
                    "kind": "search",
                    "arguments": {"query": "public topic"},
                },
            )
        )
        await entered.wait()
        await self.bot.on_config_update("self", {}, "1")
        final = await self.bot.automation_v1(
            packet=self.packet("get", {"job_id": result["job_id"]})
        )
        self.assertEqual(final["status"], "unknown")


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_exa_structured_output_does_not_parse_tool_display(self):
        from test_support import plugin
        from neko_web_test import automation_retrieval as ar

        config = plugin.NekoWebPluginConfig().web
        config.search_provider = "exa"
        sources = [
            {
                "title": "title",
                "url": "https://source.example/a",
                "snippet": "snippet",
                "published": "",
            }
        ]
        with patch.object(
            ar,
            "search_sources",
            AsyncMock(return_value=(sources, {"invalid": 0, "omitted": 0})),
        ):
            result = await ar.search_data(config, {"trust_env": False}, "topic")
        self.assertIsNone(result["results"][0]["published_at"])
        self.assertEqual(result["results"][0]["source_kind"], "snippet")
        self.assertIsNone(result["usage"])
        self.assertFalse(result["cost_known"])

    async def test_anysearch_unstructured_display_is_unsupported_not_success(self):
        from neko_web_test import automation_retrieval as ar

        config = plugin.NekoWebPluginConfig().web
        raw = {"result": {"content": [{"type": "text", "text": "搜索失败：一些文字"}]}}
        with patch.object(ar, "post_json", AsyncMock(return_value=raw)):
            with self.assertRaisesRegex(ar.ProviderError, "结构化"):
                await ar.search_data(config, {"trust_env": False}, "topic")

    async def test_read_rejects_private_url_before_request(self):
        from neko_web_test import automation_retrieval as ar

        config = plugin.NekoWebPluginConfig().web
        with patch.object(ar, "download_public", AsyncMock()) as download:
            with self.assertRaises(ar.ProviderError):
                await ar.read_data(
                    config, {"trust_env": False}, "http://127.0.0.1/private"
                )
            download.assert_not_awaited()
