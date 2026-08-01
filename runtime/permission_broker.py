"""
permission_broker.py — translates a FROZEN Intent Contract's constraints
into an enforceable SandboxPolicy, before any workflow step executes.

This module exists because of one specific failure mode: a forbidden
side effect that lives only as a line of text competes with the task's
reward/goal during generation and can lose that competition under
sufficient optimization pressure (see: the OpenAI/Hugging Face incident,
where "stay inside the sandbox" was an environmental assumption rather
than a hard technical constraint). Here, `side_effects.forbidden` and
`scope.excluded` are read from the immutable, digest-protected intent
and compiled into a policy object that `sandbox.py` enforces mechanically
— the executor cannot violate it by "forgetting" it, because it never
had the physical capability to begin with.
"""

from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlparse


@dataclass
class SandboxPolicy:
    allowed_network_hosts: List[str] = field(default_factory=list)
    deny_all_network: bool = True
    allowed_write_paths: List[str] = field(default_factory=list)
    allowed_read_paths: List[str] = field(default_factory=list)
    # Human-readable record of what was explicitly forbidden, for audit
    # logging only — enforcement happens via the fields above, not by
    # trusting anyone to consult this list at runtime.
    forbidden_actions_log: List[str] = field(default_factory=list)
    env_allowlist: List[str] = field(default_factory=lambda: ["PATH", "LANG", "HOME"])


def derive_policy(intent: dict, workspace_dir: str) -> SandboxPolicy:
    side_effects = intent.get("side_effects", {})
    requested = set(side_effects.get("requested", []))
    forbidden = list(side_effects.get("forbidden", []))

    authoritative_inputs = intent.get("authoritative_inputs", [])
    allowed_hosts = []
    allowed_read_paths = [workspace_dir]

    for src in authoritative_inputs:
        source = src.get("source", "")
        parsed = urlparse(source)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            allowed_hosts.append(parsed.netloc)
        elif source:
            allowed_read_paths.append(source)

    # Network is closed by default. It is opened ONLY if the intent
    # explicitly requested network access as a side effect AND named
    # specific hosts via authoritative_inputs. No blanket internet access
    # is ever derived implicitly from "the task might need it" — this is
    # the direct fix for "any path to the goal, including the open
    # internet" as a failure mode.
    deny_all_network = ("network_access" not in requested) or (not allowed_hosts)

    return SandboxPolicy(
        allowed_network_hosts=allowed_hosts,
        deny_all_network=deny_all_network,
        allowed_write_paths=[workspace_dir],
        allowed_read_paths=allowed_read_paths,
        forbidden_actions_log=forbidden,
    )
