"""Durable run lifecycle records used for restart/crash recovery."""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional
from .secrets import redact


class RunStateError(RuntimeError):
    pass


class RunStore:
    def __init__(self, directory: str):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def begin(self, run_id: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
        run_id = run_id or str(uuid.uuid4())
        path = self._path(run_id)
        if path.exists():
            raise RunStateError(f"run already exists: {run_id}")
        now = time.time()
        state = {
            "version": 1, "run_id": run_id, "status": "running",
            "created_at": now, "updated_at": now,
            "metadata": redact(metadata or {}), "checkpoint": None,
        }
        self._write(state)
        return state

    def checkpoint(self, run_id: str, checkpoint: dict) -> dict:
        state = self.get(run_id)
        if state["status"] != "running":
            raise RunStateError("only a running run can be checkpointed")
        state["checkpoint"] = redact(checkpoint)
        state["updated_at"] = time.time()
        self._write(state)
        return state

    def resume(self, run_id: str) -> dict:
        state = self.get(run_id)
        if state["status"] != "running":
            raise RunStateError("only an incomplete running run can be resumed")
        return state

    def begin_or_resume(self, run_id: str, metadata: Optional[dict] = None) -> dict:
        if self._path(run_id).exists():
            return self.resume(run_id)
        return self.begin(run_id, metadata)

    def finish(self, run_id: str, status: str, result: Optional[dict] = None) -> dict:
        if status not in ("completed", "failed"):
            raise ValueError("terminal status must be completed or failed")
        state = self.get(run_id)
        if state["status"] != "running":
            raise RunStateError("run is already terminal")
        state.update({"status": status, "result": redact(result or {}), "updated_at": time.time()})
        self._write(state)
        return state

    def get(self, run_id: str) -> dict:
        path = self._path(run_id)
        if not path.exists():
            raise RunStateError(f"unknown run: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def recover_incomplete(self) -> list[dict]:
        recovered = []
        for path in sorted(self.directory.glob("*.json")):
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("status") == "running":
                recovered.append(state)
        return recovered

    def _path(self, run_id: str) -> Path:
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as exc:
            raise RunStateError("run_id must be a UUID") from exc
        return self.directory / f"{parsed}.json"

    def _write(self, state: dict) -> None:
        path = self._path(state["run_id"])
        temp = path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
        payload = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        try:
            with open(temp, "x", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
