"""
audit.py — a tamper-evident audit ledger.

A plain append-only JSON log can still be silently edited by anything
with filesystem access to it — including the same process whose actions
it's supposed to record. This module borrows MOVA's pattern (episode
store + signed audit receipts): every entry is chained to the previous
one's hash and HMAC-signed with a key the executor doesn't need to
operate normally, so a retroactive edit to any entry breaks the chain
from that point forward and is detectable by verify_chain(), not just
assumed absent.

This does not make tampering impossible (whoever holds the signing key
can still forge a consistent chain) — it makes tampering by the
*executor* (which should never hold the signing key) detectable, and it
makes tampering by anyone else evident rather than silent.
"""

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Protocol
from urllib import request
from .secrets import default_resolver, redact, register_secret


GENESIS_HASH = "0" * 64
_APPEND_LOCK = threading.RLock()


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sign(payload: str, key: bytes) -> str:
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class AuditSigner(Protocol):
    """Boundary implemented by a local adapter or a remote signing service."""

    def sign(self, payload: str) -> str: ...
    def verify(self, payload: str, signature: str) -> bool: ...


class HMACSigner:
    def __init__(self, key: bytes):
        self._key = key

    def sign(self, payload: str) -> str:
        return _sign(payload, self._key)

    def verify(self, payload: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)


class HTTPAuditSigner:
    """Client for an out-of-process signer; the executor never receives its key."""

    def __init__(self, endpoint: str, bearer_token: str, timeout: int = 5, opener=None):
        if not endpoint.startswith("https://"):
            raise ValueError("external audit signer endpoint must use HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.bearer_token = register_secret(bearer_token)
        self.timeout = timeout
        self._open = opener or request.urlopen

    def _call(self, operation: str, body: dict) -> dict:
        req = request.Request(
            f"{self.endpoint}/{operation}",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Authorization": f"Bearer {self.bearer_token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def sign(self, payload: str) -> str:
        signature = self._call("sign", {"payload": payload}).get("signature")
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("external audit signer returned no signature")
        return signature

    def verify(self, payload: str, signature: str) -> bool:
        return self._call("verify", {"payload": payload, "signature": signature}).get("valid") is True


@contextmanager
def _file_lock(path: Path):
    """Cross-process exclusive lock using a separate, persistent lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class AuditLedger:
    def __init__(
        self, path: str, signing_key: Optional[bytes] = None,
        ephemeral_ok: bool = False, signer: Optional[AuditSigner] = None,
        run_id: Optional[str] = None,
    ):
        """
        v3 fix: this used to silently generate `os.urandom(32)` whenever
        no key was supplied — which meant a fresh process (a restart, or
        a separate verifier process, as opposed to the single-process
        example_run.py) got a DIFFERENT random key, so verify_chain()
        would report signature_invalid on every entry even with zero
        tampering. That's not a hypothetical edge case, it's the normal
        shape of real deployment (write now, verify later, in a
        different process).

        The key must now be supplied explicitly (constructor arg or
        AUDIT_SIGNING_KEY env var) and be generated OUTSIDE the executor
        process — e.g. a CI secret or a dedicated verifier service's
        secret store — so the executor being fully compromised doesn't
        also hand over the ability to forge a consistent chain. Pass
        `ephemeral_ok=True` only for tests/demos where cross-process
        verification isn't the point.
        """
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.run_id = run_id or str(uuid.uuid4())
        self.path.parent.mkdir(parents=True, exist_ok=True)

        key_value = default_resolver.get(
            "audit_signing_key", ("AUDIT_SIGNING_KEY",), required=False,
        )
        if signer is not None and (signing_key is not None or key_value is not None):
            raise ValueError("provide signer or signing_key, not both")
        if signer is not None:
            self.signer = signer
            self.signing_key = None
        elif signing_key is not None:
            self.signing_key = signing_key
            self.signer = HMACSigner(signing_key)
        elif key_value is not None:
            self.signing_key = key_value.encode()
            self.signer = HMACSigner(self.signing_key)
        elif ephemeral_ok:
            self.signing_key = os.urandom(32)
            self.signer = HMACSigner(self.signing_key)
        else:
            raise ValueError(
                "AuditLedger requires a signing_key (or AUDIT_SIGNING_KEY "
                "env var) generated OUTSIDE the executor process. Pass "
                "ephemeral_ok=True only for single-process tests/demos "
                "where cross-process chain verification isn't the point."
            )

        if not self.path.exists():
            self.path.touch()

    def _last_hash(self) -> str:
        last = GENESIS_HASH
        if self.path.stat().st_size == 0:
            return last
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                last = entry["entry_hash"]
        return last

    def append(self, event_type: str, details: dict, severity: str = "tier0") -> dict:
        """
        severity: 'tier0' (background record only), 'tier1' (aggregated
        summary, surfaced at natural checkpoints), 'tier2' (blocking
        escalation — caller is expected to halt and wait for human
        response; this ledger only records the event, it does not itself
        enforce the halt).
        """
        with _APPEND_LOCK, _file_lock(self.lock_path):
            prev_hash = self._last_hash()
            record = {
                "ts": time.time(), "run_id": self.run_id,
                "event_type": event_type, "severity": severity,
                "details": redact(details), "prev_hash": prev_hash,
            }
            entry_hash = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            record["entry_hash"] = entry_hash
            record["signature"] = self.signer.sign(entry_hash)

            with open(self.path, "a", encoding="utf-8") as f:
                f.write(_canonical(record) + "\n")
                f.flush()
                os.fsync(f.fileno())

        return record

    def verify_chain(self) -> dict:
        """Returns {"ok": True} or {"ok": False, "reason": ..., "at_line": N}."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {"ok": True, "entries": 0}

        prev_hash = GENESIS_HASH
        line_no = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line_no += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "reason": "malformed_entry", "at_line": line_no}

                if entry["prev_hash"] != prev_hash:
                    return {"ok": False, "reason": "chain_broken", "at_line": line_no}

                claimed_hash = entry["entry_hash"]
                claimed_sig = entry["signature"]

                recomputed = dict(entry)
                del recomputed["entry_hash"]
                del recomputed["signature"]
                recomputed_hash = hashlib.sha256(_canonical(recomputed).encode("utf-8")).hexdigest()

                if recomputed_hash != claimed_hash:
                    return {"ok": False, "reason": "entry_hash_mismatch", "at_line": line_no}

                if not self.signer.verify(claimed_hash, claimed_sig):
                    return {"ok": False, "reason": "signature_invalid", "at_line": line_no}

                prev_hash = claimed_hash

        return {"ok": True, "entries": line_no}
