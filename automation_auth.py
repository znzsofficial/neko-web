"""Bounded capability packets; never trust an identity supplied without proof."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid


class AuthError(ValueError):
    pass


def canonical(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > 12000:
        raise AuthError("invalid_packet")
    return raw


def read_keys(path):
    path = Path(path)
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 8192:
        raise AuthError("unsafe_key_file")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise AuthError("unsafe_key_permissions")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or len(value) > 8:
            raise ValueError
        for client, key in value.items():
            if (
                not re.fullmatch(r"[a-z0-9_.-]{1,64}", client)
                or not isinstance(key, str)
                or not re.fullmatch(r"[a-f0-9]{64}", key)
            ):
                raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError):
        raise AuthError("invalid_key_file") from None


def sign(key, client, audience, operation, body, *, now=None, nonce=None):
    packet = {
        "v": 1,
        "client": client,
        "audience": audience,
        "issued": int(time.time() if now is None else now),
        "nonce": nonce or uuid.uuid4().hex,
        "operation": operation,
        "body": body,
    }
    packet["signature"] = hmac.new(
        bytes.fromhex(key), canonical(packet), hashlib.sha256
    ).hexdigest()
    return packet


def verify(packet, keys, audience, *, now=None):
    try:
        if not isinstance(packet, dict) or set(packet) != {
            "v",
            "client",
            "audience",
            "issued",
            "nonce",
            "operation",
            "body",
            "signature",
        }:
            raise AuthError("unauthorized")
        unsigned = {k: v for k, v in packet.items() if k != "signature"}
        raw = canonical(unsigned)
        client = packet["client"]
        key = keys.get(client) if isinstance(client, str) else None
        if (
            type(packet["v"]) is not int
            or packet["v"] != 1
            or packet["audience"] != audience
            or not key
            or type(packet["issued"]) is not int
            or abs((time.time() if now is None else now) - packet["issued"]) > 120
            or not isinstance(packet["nonce"], str)
            or not re.fullmatch("[a-f0-9]{32}", packet["nonce"])
            or not isinstance(packet["body"], dict)
            or not isinstance(packet["operation"], str)
            or not isinstance(packet["signature"], str)
            or not re.fullmatch("[a-f0-9]{64}", packet["signature"])
        ):
            raise AuthError("unauthorized")
        expected = hmac.new(bytes.fromhex(key), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, packet["signature"]):
            raise AuthError("unauthorized")
        return client, packet["operation"], packet["body"], packet["nonce"]
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise AuthError("unauthorized") from None
