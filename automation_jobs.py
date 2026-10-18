"""Durable bounded background jobs. No SDK, network or provider dependencies."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from .automation_auth import canonical
except ImportError:
    from automation_auth import canonical


class JobError(ValueError):
    pass


class JobStore:
    def __init__(self, directory, clock=time.time):
        self.path = Path(directory) / "automation.db"
        self.clock = clock
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="neko-web-jobs"
        )
        self.conn = None
        self.pending = 0

    async def call(self, method, *args):
        if self.pending >= 32 and method != "close":
            raise JobError("storage_busy")
        self.pending += 1
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, getattr(self, "_" + method), *args
        )

        def finished(_):
            self.pending -= 1

        future.add_done_callback(finished)
        return await asyncio.shield(future)

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise JobError("unsafe_database")
        self.conn = sqlite3.connect(self.path)
        if self.conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise JobError("corrupt_database_preserved")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise JobError("unknown_schema_preserved")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, client TEXT, request_id TEXT,
            fingerprint TEXT, kind TEXT, status TEXT, body TEXT, result TEXT, created REAL,
            UNIQUE(client, request_id));
          CREATE TABLE IF NOT EXISTS nonces(client TEXT, nonce TEXT, created REAL, PRIMARY KEY(client,nonce));
          CREATE TABLE IF NOT EXISTS usage(client TEXT, day TEXT, kind TEXT, count INTEGER,
            PRIMARY KEY(client,day,kind)); PRAGMA user_version=1;
        """)
        self.path.chmod(0o600)
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='unknown' WHERE status IN ('queued','running')"
            )

    def _nonce(self, client, nonce):
        with self.conn:
            self.conn.execute(
                "DELETE FROM nonces WHERE created < ?", (self.clock() - 300,)
            )
            if self.conn.execute("SELECT count(*) FROM nonces").fetchone()[0] >= 4096:
                raise JobError("capacity")
            try:
                self.conn.execute(
                    "INSERT INTO nonces VALUES (?,?,?)", (client, nonce, self.clock())
                )
            except sqlite3.IntegrityError:
                raise JobError("replay") from None

    def _submit(self, client, request_id, kind, body):
        fingerprint = hashlib.sha256(
            canonical({"kind": kind, "body": body})
        ).hexdigest()
        existing = self.conn.execute(
            "SELECT id,fingerprint FROM jobs WHERE client=? AND request_id=?",
            (client, request_id),
        ).fetchone()
        if existing:
            if existing[1] != fingerprint:
                raise JobError("idempotency_conflict")
            return existing[0], False
        # Retain request IDs for seven days. Old terminal jobs only; never drop in-flight attribution.
        day = (
            datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
        with self.conn:
            self.conn.execute(
                "DELETE FROM jobs WHERE created<? AND status NOT IN ('queued','running')",
                (self.clock() - 7 * 86400,),
            )
            self.conn.execute(
                "DELETE FROM usage WHERE day < ?",
                (
                    datetime.fromtimestamp(
                        self.clock() - 30 * 86400, ZoneInfo("Asia/Shanghai")
                    )
                    .date()
                    .isoformat(),
                ),
            )
            if self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 128:
                raise JobError("capacity")
            if (
                self.conn.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                >= 4
            ):
                raise JobError("busy")
            count = self.conn.execute(
                "SELECT count FROM usage WHERE client=? AND day=? AND kind=?",
                (client, day, kind),
            ).fetchone()
            if count and count[0] >= 6:
                raise JobError("budget_exceeded")
            self.conn.execute(
                "INSERT INTO usage VALUES (?,?,?,1) ON CONFLICT(client,day,kind) DO UPDATE SET count=count+1",
                (client, day, kind),
            )
            key = uuid.uuid4().hex
            self.conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    client,
                    request_id,
                    fingerprint,
                    kind,
                    "queued",
                    json.dumps(body),
                    None,
                    self.clock(),
                ),
            )
        return key, True

    def _get(self, client, key):
        row = self.conn.execute(
            "SELECT status,result FROM jobs WHERE client=? AND id=?", (client, key)
        ).fetchone()
        if row is None:
            raise JobError("not_found")
        return {
            "schema_version": 1,
            "job_id": key,
            "status": row[0],
            "result": json.loads(row[1]) if row[1] else None,
        }

    def _update(self, key, status, result=None):
        body = (
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            if result is not None
            else None
        )
        if body is not None and len(body.encode("utf-8")) > 30000:
            status, body = (
                "failed",
                json.dumps({"success": False, "status": "response_too_large"}),
            )
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status=?,result=? WHERE id=?", (status, body, key)
            )

    def _close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    async def close(self):
        await self.call("close")
        self.executor.shutdown(wait=False)


class JobService:
    def __init__(self, directory, execute, clock=time.time):
        self.store = JobStore(directory, clock)
        self.execute = execute
        self.tasks = {}
        self.closed = False
        self.slot = asyncio.Semaphore(1)

    async def open(self):
        await self.store.call("open")

    async def submit(self, client, body):
        if self.closed:
            raise JobError("disabled")
        if set(body) != {"request_id", "kind", "arguments"}:
            raise JobError("invalid_request")
        request_id, kind, arguments = (
            body["request_id"],
            body["kind"],
            body["arguments"],
        )
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 96
            or kind not in {"search", "read"}
            or not isinstance(arguments, dict)
        ):
            raise JobError("invalid_request")
        if kind == "search":
            if (
                set(arguments) != {"query"}
                or not isinstance(arguments["query"], str)
                or not 1 <= len(arguments["query"].strip()) <= 300
            ):
                raise JobError("invalid_request")
        elif (
            set(arguments) != {"url"}
            or not isinstance(arguments["url"], str)
            or len(arguments["url"]) > 2000
        ):
            raise JobError("invalid_request")
        key, fresh = await self.store.call(
            "submit", client, request_id, kind, arguments
        )
        if fresh:
            self.tasks[key] = asyncio.create_task(self._run(key, kind, arguments))
        return await self.store.call("get", client, key)

    async def _run(self, key, kind, arguments):
        started = False
        try:
            async with asyncio.timeout(95):
                async with self.slot:
                    await self.store.call("update", key, "running")
                    started = True
                    result = await self.execute(kind, arguments)
                    status = (
                        "partial"
                        if result.get("status") == "partial"
                        else (
                            "succeeded" if result.get("success") is True else "failed"
                        )
                    )
                    await self.store.call("update", key, status, result)
        except (TimeoutError, asyncio.CancelledError):
            await self.store.call("update", key, "unknown" if started else "cancelled")
        except Exception:
            await self.store.call(
                "update", key, "failed", {"success": False, "status": "unavailable"}
            )
        finally:
            self.tasks.pop(key, None)

    async def dispatch(self, client, operation, body, nonce):
        await self.store.call("nonce", client, nonce)
        if operation == "submit":
            return await self.submit(client, body)
        if (
            operation in {"get", "cancel"}
            and set(body) == {"job_id"}
            and isinstance(body["job_id"], str)
            and len(body["job_id"]) == 32
        ):
            result = await self.store.call("get", client, body["job_id"])
            task = self.tasks.get(body["job_id"])
            if operation == "cancel" and task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                result = await self.store.call("get", client, body["job_id"])
                if result["status"] == "queued":
                    await self.store.call("update", body["job_id"], "cancelled")
                    result = await self.store.call("get", client, body["job_id"])
            return result
        raise JobError("invalid_request")

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()
