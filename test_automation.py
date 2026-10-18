"""Offline capability and background-job regression tests; no providers or SDK."""

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from automation_auth import AuthError, read_keys, sign, verify
from automation_jobs import JobError, JobService

KEY = "12" * 32
OTHER = "34" * 32


class CapabilityTests(unittest.TestCase):
    def packet(self):
        return sign(
            KEY, "neko.life", "neko.web", "submit", {"request_id": "x"}, now=1000
        )

    def test_valid_identity_requires_key_proof(self):
        packet = self.packet()
        client, operation, body, nonce = verify(
            packet, {"neko.life": KEY}, "neko.web", now=1001
        )
        self.assertEqual(
            (client, operation, body), ("neko.life", "submit", {"request_id": "x"})
        )
        self.assertEqual(len(nonce), 32)

    def test_mutations_fail_closed(self):
        for field, value in [
            ("client", "other"),
            ("audience", "neko.life"),
            ("operation", "cancel"),
            ("body", {"request_id": "evil"}),
            ("v", 2),
            ("issued", 1100),
            ("nonce", "0" * 32),
        ]:
            with self.subTest(field=field):
                packet = self.packet()
                packet[field] = value
                with self.assertRaises(AuthError):
                    verify(
                        packet, {"neko.life": KEY, "other": OTHER}, "neko.web", now=1001
                    )

    def test_wrong_key_expiry_extra_fields_and_bad_types(self):
        for packet, keys, now in [
            (self.packet(), {"neko.life": OTHER}, 1001),
            (self.packet(), {}, 1001),
            (self.packet(), {"neko.life": KEY}, 1121),
            (
                {**self.packet(), "caller_plugin_id": "neko.life"},
                {"neko.life": KEY},
                1001,
            ),
            ([], {"neko.life": KEY}, 1001),
        ]:
            with self.assertRaises(AuthError):
                verify(packet, keys, "neko.web", now=now)

    def test_key_files_private_bounded_and_never_created_implicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "keys.json"
            self.assertEqual(read_keys(p), {})
            self.assertFalse(p.exists())
            p.write_text(json.dumps({"neko.life": KEY}), encoding="utf-8")
            p.chmod(0o600)
            self.assertEqual(read_keys(p), {"neko.life": KEY})
            p.write_text("{broken", encoding="utf-8")
            with self.assertRaises(AuthError):
                read_keys(p)
            self.assertEqual(p.read_text(), "{broken")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_world_readable_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "keys.json"
            p.write_text(json.dumps({"neko.life": KEY}), encoding="utf-8")
            p.chmod(0o644)
            with self.assertRaises(AuthError):
                read_keys(p)

    def test_packet_size_and_nan_rejected(self):
        for body in [{"text": "x" * 13000}, {"number": float("nan")}]:
            with self.assertRaises((AuthError, ValueError)):
                sign(KEY, "neko.life", "neko.web", "submit", body)


class JobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1000000000.0]
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

        async def execute(kind, body):
            self.calls.append((kind, copy.deepcopy(body)))
            self.entered.set()
            await self.release.wait()
            return {"success": True, "status": "ok", "results": []}

        self.service = JobService(self.temp.name, execute, clock=lambda: self.now[0])
        await self.service.open()
        self.addAsyncCleanup(self.shutdown)

    async def shutdown(self):
        if not self.service.closed:
            await self.service.close()

    def body(self, request_id="id1"):
        return {
            "request_id": request_id,
            "kind": "search",
            "arguments": {"query": "synthetic public topic"},
        }

    async def submit(self, request_id="id1", client="neko.life"):
        return await self.service.submit(client, self.body(request_id))

    async def test_submit_immediate_idempotent_and_conflicts_rejected(self):
        first = await asyncio.wait_for(self.submit(), 1)
        second = await self.submit()
        self.assertEqual(first["job_id"], second["job_id"])
        changed = self.body()
        changed["arguments"]["query"] = "different"
        with self.assertRaisesRegex(JobError, "idempotency_conflict"):
            await self.service.submit("neko.life", changed)
        await self.entered.wait()
        self.assertEqual(len(self.calls), 1)

    async def test_result_and_cancel_are_owner_scoped(self):
        first = await self.submit()
        with self.assertRaisesRegex(JobError, "not_found"):
            await self.service.dispatch(
                "other", "cancel", {"job_id": first["job_id"]}, "nonce1"
            )
        self.assertIn(first["job_id"], self.service.tasks)
        await self.entered.wait()
        result = await self.service.dispatch(
            "neko.life", "cancel", {"job_id": first["job_id"]}, "nonce2"
        )
        self.assertEqual(result["status"], "unknown")

    async def test_replay_survives_reopen(self):
        await self.service.store.call("nonce", "neko.life", "nonce")
        await self.service.close()
        self.service = JobService(
            self.temp.name, self.service.execute, clock=lambda: self.now[0]
        )
        await self.service.open()
        with self.assertRaisesRegex(JobError, "replay"):
            await self.service.store.call("nonce", "neko.life", "nonce")

    async def test_completed_result_durable_and_no_repeat(self):
        first = await self.submit()
        await self.entered.wait()
        task = self.service.tasks[first["job_id"]]
        self.release.set()
        await task
        result = await self.service.store.call("get", "neko.life", first["job_id"])
        self.assertEqual(result["status"], "succeeded")
        await self.service.close()
        self.service = JobService(
            self.temp.name, self.service.execute, clock=lambda: self.now[0]
        )
        await self.service.open()
        self.assertEqual((await self.submit())["status"], "succeeded")
        self.assertFalse(self.service.tasks)

    async def test_daily_budget_persisted_failures_count_and_manual_not_involved(self):
        self.release.set()
        for i in range(6):
            result = await self.submit(str(i))
            task = self.service.tasks.get(result["job_id"])
            if task:
                await task
        with self.assertRaisesRegex(JobError, "budget_exceeded"):
            await self.submit("seventh")
        self.now[0] += 86400
        await self.submit("tomorrow")

    async def test_queue_capacity_and_single_background_slot(self):
        for i in range(4):
            await self.submit(str(i))
        with self.assertRaisesRegex(JobError, "busy"):
            await self.submit("fifth")
        await self.entered.wait()
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_schema_and_corrupt_database_preserved(self):
        await self.service.close()
        path = Path(self.temp.name) / "automation.db"
        path.write_bytes(b"not a sqlite database")
        before = path.read_bytes()
        self.service = JobService(self.temp.name, self.service.execute)
        with self.assertRaises(Exception):
            await self.service.open()
        self.assertEqual(path.read_bytes(), before)

    async def test_stale_running_recovered_unknown_never_replayed(self):
        await self.submit()
        await self.entered.wait()
        await self.service.close()
        self.service = JobService(
            self.temp.name, self.service.execute, clock=lambda: self.now[0]
        )
        await self.service.open()
        self.assertEqual((await self.submit())["status"], "unknown")
        self.assertFalse(self.service.tasks)

    async def test_output_size_is_bounded(self):
        result = await self.submit()
        await self.entered.wait()
        await self.service.store.call(
            "update", result["job_id"], "succeeded", {"text": "中" * 11000}
        )
        record = await self.service.store.call("get", "neko.life", result["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"]["status"], "response_too_large")

    async def test_invalid_parameters_before_budget_or_queue(self):
        for body in [
            {},
            {**self.body(), "extra": 1},
            {**self.body(), "arguments": {"query": "x" * 301}},
            {**self.body(), "kind": "shell"},
            {**self.body(), "arguments": {"query": 5}},
        ]:
            with self.assertRaises(JobError):
                await self.service.submit("neko.life", body)
        self.assertFalse(self.calls)
