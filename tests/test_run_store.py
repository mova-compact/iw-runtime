import uuid

import pytest

from runtime.run_store import RunStateError, RunStore
from runtime.secrets import register_secret


def test_run_lifecycle_is_durable_across_store_instances(tmp_path):
    store = RunStore(str(tmp_path))
    started = store.begin(metadata={"owner": "alice"})
    store.checkpoint(started["run_id"], {"round": 1, "revision": 2})

    restarted = RunStore(str(tmp_path))
    recovered = restarted.recover_incomplete()
    assert [item["run_id"] for item in recovered] == [started["run_id"]]
    assert recovered[0]["checkpoint"] == {"round": 1, "revision": 2}

    restarted.finish(started["run_id"], "completed", {"artifact": "ok"})
    assert restarted.recover_incomplete() == []


def test_terminal_run_cannot_be_reopened_or_checkpointed(tmp_path):
    store = RunStore(str(tmp_path))
    run = store.begin()
    store.finish(run["run_id"], "failed", {"reason": "test"})
    with pytest.raises(RunStateError):
        store.checkpoint(run["run_id"], {"round": 2})
    with pytest.raises(RunStateError):
        store.begin(run["run_id"])


def test_run_id_must_be_uuid(tmp_path):
    with pytest.raises(RunStateError):
        RunStore(str(tmp_path)).begin("../escape")


def test_explicit_run_id_is_preserved(tmp_path):
    run_id = str(uuid.uuid4())
    assert RunStore(str(tmp_path)).begin(run_id)["run_id"] == run_id


def test_begin_or_resume_reuses_incomplete_run(tmp_path):
    store = RunStore(str(tmp_path))
    run = store.begin()
    store.checkpoint(run["run_id"], {"round": 3})
    resumed = RunStore(str(tmp_path)).begin_or_resume(run["run_id"])
    assert resumed["run_id"] == run["run_id"]
    assert resumed["checkpoint"] == {"round": 3}


def test_run_state_never_persists_registered_secret(tmp_path):
    secret = "run-store-leak-secret"
    register_secret(secret)
    store = RunStore(str(tmp_path))
    run = store.begin(metadata={"authorization": secret, "note": secret})
    store.checkpoint(run["run_id"], {"stderr": f"failed with {secret}"})
    assert secret not in next(tmp_path.glob("*.json")).read_text()
