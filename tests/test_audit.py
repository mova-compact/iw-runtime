import json
import tempfile
from pathlib import Path

import pytest

from runtime.audit import AuditLedger, HTTPAuditSigner
from runtime.secrets import register_secret


def test_ledger_requires_explicit_key_by_default():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            AuditLedger(str(Path(d) / "audit.jsonl"))


def test_ledger_accepts_explicit_ephemeral_opt_in():
    with tempfile.TemporaryDirectory() as d:
        ledger = AuditLedger(str(Path(d) / "audit.jsonl"), ephemeral_ok=True)
        ledger.append("event", {"x": 1})
        assert ledger.verify_chain()["ok"] is True


def test_ledger_reads_key_from_env_var(monkeypatch):
    monkeypatch.setenv("IW_SECRET_SOURCE", "environment")
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "env-provided-key")
    with tempfile.TemporaryDirectory() as d:
        ledger = AuditLedger(str(Path(d) / "audit.jsonl"))
        ledger.append("event", {"x": 1})
        assert ledger.verify_chain()["ok"] is True


def test_verification_survives_a_new_process_with_the_same_explicit_key():
    """
    This is the exact bug from finding #2: two separate AuditLedger
    instances (simulating two processes) must agree on signatures when
    given the SAME explicit key — which silently failed before when both
    fell back to independently-generated os.urandom(32) keys.
    """
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "audit.jsonl")
        key = b"shared-key-generated-outside-the-executor"

        writer = AuditLedger(path, signing_key=key)
        writer.append("event_a", {"x": 1})
        writer.append("event_b", {"x": 2})

        verifier = AuditLedger(path, signing_key=key)  # fresh instance, same key
        result = verifier.verify_chain()
        assert result["ok"] is True
        assert result["entries"] == 2


def test_two_ephemeral_ledgers_do_not_agree():
    """
    Documents the failure mode directly: without a shared explicit key,
    a second process's ledger instance cannot validate the first's
    entries, even with zero tampering — this is why ephemeral_ok is
    opt-in and loudly documented, not a silent default.
    """
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "audit.jsonl")
        writer = AuditLedger(path, ephemeral_ok=True)
        writer.append("event_a", {"x": 1})

        verifier = AuditLedger(path, ephemeral_ok=True)  # different random key
        result = verifier.verify_chain()
        assert result["ok"] is False
        assert result["reason"] == "signature_invalid"


def test_chain_valid_after_normal_appends():
    with tempfile.TemporaryDirectory() as d:
        ledger = AuditLedger(str(Path(d) / "audit.jsonl"), signing_key=b"test-key")
        ledger.append("event_a", {"x": 1})
        ledger.append("event_b", {"x": 2})
        ledger.append("event_c", {"x": 3})
        result = ledger.verify_chain()
        assert result["ok"] is True
        assert result["entries"] == 3


def test_entries_carry_stable_explicit_run_id():
    with tempfile.TemporaryDirectory() as d:
        run_id = "eb32f14f-31d5-4f15-8bb7-c90e361b6498"
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"test-key", run_id=run_id)
        first = ledger.append("event_a", {})
        second = ledger.append("event_b", {})
        assert first["run_id"] == second["run_id"] == run_id


def test_external_signer_boundary_is_used_for_sign_and_verify():
    class Signer:
        def __init__(self):
            self.signed = []

        def sign(self, payload):
            self.signed.append(payload)
            return "external:" + payload

        def verify(self, payload, signature):
            return signature == "external:" + payload

    with tempfile.TemporaryDirectory() as d:
        signer = Signer()
        ledger = AuditLedger(str(Path(d) / "audit.jsonl"), signer=signer)
        ledger.append("event", {"x": 1})
        assert signer.signed
        assert ledger.verify_chain()["ok"] is True


def test_http_signer_uses_external_sign_and_verify_operations():
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self):
            return json.dumps(self.payload).encode()

    def opener(req, timeout):
        body = json.loads(req.data)
        calls.append((req.full_url, body, timeout))
        if req.full_url.endswith("/sign"):
            return Response({"signature": "remote-signature"})
        return Response({"valid": body["signature"] == "remote-signature"})

    signer = HTTPAuditSigner("https://signer.internal", "token", opener=opener)
    assert signer.sign("digest") == "remote-signature"
    assert signer.verify("digest", "remote-signature") is True
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == ["sign", "verify"]


def test_http_signer_rejects_cleartext_endpoint():
    with pytest.raises(ValueError):
        HTTPAuditSigner("http://signer.internal", "token")


def test_malformed_trailing_entry_fails_closed():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"test-key")
        ledger.append("event", {})
        with path.open("a") as handle:
            handle.write('{"partial":')
        assert ledger.verify_chain()["reason"] == "malformed_entry"


def test_audit_file_never_persists_registered_secret():
    with tempfile.TemporaryDirectory() as d:
        secret = "audit-leak-test-secret"
        register_secret(secret)
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"test-key")
        record = ledger.append("failure", {"stderr": f"request used {secret}",
                                            "api_key": secret})
        assert secret not in path.read_text()
        assert secret not in json.dumps(record)


def test_tampering_with_an_entry_is_detected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"test-key")
        ledger.append("event_a", {"amount": 10})
        ledger.append("event_b", {"amount": 20})

        # Simulate the executor trying to retroactively rewrite its own
        # history — e.g. changing a recorded forbidden-side-effect event
        # into something innocuous.
        lines = path.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["details"]["amount"] = 999999  # tamper
        lines[0] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        result = ledger.verify_chain()
        assert result["ok"] is False
        assert result["reason"] in ("entry_hash_mismatch", "chain_broken")


def test_deleting_an_entry_breaks_the_chain():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"test-key")
        ledger.append("event_a", {"x": 1})
        ledger.append("event_b", {"x": 2})
        ledger.append("event_c", {"x": 3})

        lines = path.read_text().splitlines()
        del lines[1]  # remove the middle entry entirely
        path.write_text("\n".join(lines) + "\n")

        result = ledger.verify_chain()
        assert result["ok"] is False
        assert result["reason"] == "chain_broken"


def test_wrong_signing_key_cannot_forge_a_valid_chain():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        ledger = AuditLedger(str(path), signing_key=b"real-key")
        ledger.append("event_a", {"x": 1})

        # An attacker without the real key tries to append a forged entry
        # that continues the chain.
        forger = AuditLedger(str(path), signing_key=b"wrong-key")
        forger.append("forged_event", {"x": "malicious"})

        # Verifying with the REAL key must catch the forged signature.
        result = ledger.verify_chain()
        assert result["ok"] is False
        assert result["reason"] == "signature_invalid"
