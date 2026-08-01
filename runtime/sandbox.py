"""
sandbox.py — enforces a SandboxPolicy using container isolation.

This is the actual enforcement boundary the rest of the runtime relies
on. Everything upstream (resolve_intent, build_workflow) is soft LLM
judgment and is allowed to be wrong cheaply, because nothing it produces
can execute outside of what this module permits.

v3 fix: `deny_all_network=False` used to mean "give the container a
plain bridge network" — which is full outbound internet access,
regardless of how narrow `allowed_network_hosts` was. The host list was
data nobody enforced. Now a partially-open policy gets a dedicated
per-run bridge network with an iptables egress chain that DROPs
everything except the resolved IPs of the explicitly allowed hosts.

HONESTY NOTE, read before relying on this in anything real:
This is a reference implementation, not a hardened production sandbox.
IP-based allowlisting is defeated by a host that rotates IPs faster than
DNS TTL, by DNS-over-HTTPS resolving to an unexpected IP, or by an
allowed host itself proxying arbitrary traffic (SSRF-via-allowed-host).
For anything genuinely high-stakes, put a real filtering forward proxy
(e.g. squid with an explicit domain ACL) in front of the container
instead of relying on IP-level netfilter rules, and prefer a VM over a
container for the isolation boundary itself.
`run_step_local_unsafe` is provided ONLY for functional testing of the
workflow logic on machines without Docker — it is NOT a security
boundary and must not be used when the executor's trustworthiness is
in question. It now requires `allow_unsafe=True` explicitly so it can't
be reached by accident just because Docker happened to be unavailable.
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .permission_broker import SandboxPolicy

SANDBOX_IMAGE = "python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
SANDBOX_USER = "65534:65534"
MAX_OUTPUT_BYTES = 1024 * 1024


class SandboxUnavailable(RuntimeError):
    pass


class NetworkEnforcementError(RuntimeError):
    pass


def _run_bounded(
    args: List[str], timeout: int, max_output_bytes: int = MAX_OUTPUT_BYTES,
    container_name: Optional[str] = None,
) -> dict:
    """Run a process while bounding combined stdout/stderr in memory.

    The process is killed as soon as its combined output exceeds the quota.
    For Docker calls the named container is also force-removed, because killing
    only the Docker CLI does not reliably stop a detached engine-side process.
    """
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    output_exceeded = threading.Event()

    def read_stream(name: str, stream) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            with lock:
                used = len(chunks["stdout"]) + len(chunks["stderr"])
                remaining = max(0, max_output_bytes - used)
                chunks[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_exceeded.set()
                    process.kill()
                    break

    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
    finally:
        for reader in readers:
            reader.join(timeout=2)
        if (timed_out or output_exceeded.is_set()) and container_name:
            subprocess.run(
                ["docker", "rm", "-f", container_name], capture_output=True,
                timeout=10,
            )

    stderr = chunks["stderr"].decode("utf-8", errors="replace")
    if timed_out:
        stderr += "\n[sandbox: execution timed out]"
    if output_exceeded.is_set():
        stderr += "\n[sandbox: combined output limit exceeded]"
    exit_code = 124 if timed_out else 137 if output_exceeded.is_set() else process.returncode
    return {
        "exit_code": exit_code,
        "stdout": chunks["stdout"].decode("utf-8", errors="replace"),
        "stderr": stderr,
        "timed_out": timed_out,
        "output_truncated": output_exceeded.is_set(),
    }


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resolve_host_ips(host: str) -> List[str]:
    """Resolve a hostname to its current IPv4/IPv6 addresses. Isolated in
    its own function so callers can monkeypatch it in tests without
    needing real DNS access."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def build_egress_ruleset(policy: SandboxPolicy, chain: str, resolver=resolve_host_ips) -> List[List[str]]:
    """
    Builds the iptables rule list enforcing `policy.allowed_network_hosts`
    on a given chain name. Testable without Docker or real DNS by passing
    a fake `resolver`.

    Default-deny: every resolved IP of every allowed host gets an ACCEPT
    rule, and the chain ends in an unconditional DROP. A host that fails
    to resolve contributes no ACCEPT rule — it fails closed, not open.
    """
    if policy.deny_all_network:
        return []  # --network none handles this case entirely; no rules needed

    rules: List[List[str]] = []
    for host in policy.allowed_network_hosts:
        for ip in resolver(host):
            rules.append(["iptables", "-A", chain, "-d", ip, "-j", "ACCEPT"])
    rules.append(["iptables", "-A", chain, "-j", "DROP"])
    return rules


class _NetworkContext:
    """Creates an isolated Docker bridge network, applies an egress
    allowlist to it via iptables, and tears both down on exit. Used only
    for the partially-open (deny_all_network=False) case — the fully
    closed case just uses `--network none` and needs none of this."""

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        self.network_name = f"iw-egress-{secrets.token_hex(6)}"
        self.chain_name = f"IW_EGRESS_{secrets.token_hex(6).upper()}"
        self._bridge_iface: Optional[str] = None

    def __enter__(self) -> str:
        if os.name == "nt" or shutil.which("iptables") is None:
            raise NetworkEnforcementError(
                "partial network allowlisting is unavailable on this host; "
                "refusing unrestricted Docker bridge egress"
            )
        subprocess.run(
            ["docker", "network", "create", "--driver", "bridge", self.network_name],
            capture_output=True, check=True,
        )
        net_id = subprocess.run(
            ["docker", "network", "inspect", self.network_name, "-f", "{{.Id}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Docker auto-names the bridge iface br-<network id short> when no
        # explicit bridge.name option is set on network creation.
        self._bridge_iface = f"br-{net_id[:12]}"

        subprocess.run(["iptables", "-N", self.chain_name], check=True, capture_output=True)
        rules = build_egress_ruleset(self.policy, self.chain_name)
        for rule in rules:
            subprocess.run(rule, check=True, capture_output=True)
        subprocess.run(
            ["iptables", "-I", "FORWARD", "-i", self._bridge_iface, "-j", self.chain_name],
            check=True, capture_output=True,
        )
        return self.network_name

    def __exit__(self, *exc):
        subprocess.run(
            ["iptables", "-D", "FORWARD", "-i", self._bridge_iface or "", "-j", self.chain_name],
            capture_output=True,
        )
        subprocess.run(["iptables", "-F", self.chain_name], capture_output=True)
        subprocess.run(["iptables", "-X", self.chain_name], capture_output=True)
        subprocess.run(["docker", "network", "rm", self.network_name], capture_output=True)


class _ProxyNetworkContext:
    """Give the worker an internal-only network and egress via an ACL proxy."""

    proxy_host = "iw-egress-proxy"
    proxy_port = 8080

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        token = secrets.token_hex(6)
        self.network_name = f"iw-internal-{token}"
        self.proxy_name = f"iw-proxy-{token}"
        self._network_created = False
        self._proxy_created = False

    def __enter__(self) -> str:
        if not self.policy.allowed_network_hosts:
            raise NetworkEnforcementError("partial network policy has no allowed hosts")
        proxy_script = Path(__file__).with_name("egress_proxy.py").resolve()
        try:
            subprocess.run(
                ["docker", "network", "create", "--internal", self.network_name],
                capture_output=True, text=True, check=True,
            )
            self._network_created = True
            proxy_args = [
                "docker", "run", "-d", "--rm", "--name", self.proxy_name,
                "--network", "bridge", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--user", SANDBOX_USER, "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "32",
                "--memory", "128m", "--cpus", "0.5",
                "-v", f"{proxy_script}:/proxy.py:ro", SANDBOX_IMAGE,
                "python", "/proxy.py",
            ]
            for host in self.policy.allowed_network_hosts:
                proxy_args.extend(["--allow-host", host])
            subprocess.run(proxy_args, capture_output=True, text=True, check=True)
            self._proxy_created = True
            subprocess.run(
                ["docker", "network", "connect", "--alias", self.proxy_host,
                 self.network_name, self.proxy_name],
                capture_output=True, text=True, check=True,
            )
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.proxy_name],
                capture_output=True, text=True, check=True,
            )
            if inspect.stdout.strip() != "true":
                raise NetworkEnforcementError("egress proxy failed readiness check")
            return self.network_name
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.__exit__()
            raise NetworkEnforcementError(
                "could not establish the internal filtering-proxy network"
            ) from exc

    def __exit__(self, *exc) -> None:
        if self._proxy_created:
            subprocess.run(
                ["docker", "rm", "-f", self.proxy_name], capture_output=True,
            )
            self._proxy_created = False
        if self._network_created:
            subprocess.run(
                ["docker", "network", "rm", self.network_name], capture_output=True,
            )
            self._network_created = False

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_host}:{self.proxy_port}"


def run_step(command: List[str], policy: SandboxPolicy, workspace_dir: str, timeout: int = 120) -> dict:
    """Execute a step's command inside an isolated container configured
    from `policy`. Returns {"exit_code", "stdout", "stderr"}."""
    if not _docker_available():
        raise SandboxUnavailable(
            "Docker not found. Real enforcement requires container/VM "
            "isolation. Install Docker, or explicitly opt into "
            "run_step_local_unsafe(allow_unsafe=True) for non-security-"
            "critical testing."
        )

    Path(workspace_dir).mkdir(parents=True, exist_ok=True)

    container_name = f"iw-step-{secrets.token_hex(8)}"
    base_args = [
        "docker", "run", "--rm", "--name", container_name,
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "-v", f"{Path(workspace_dir).resolve()}:/workspace",
        "-w", "/workspace",
        "--user", SANDBOX_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "512m",
        "--cpus", "1.0",
        "--ulimit", "nofile=256:256",
    ]

    if policy.deny_all_network:
        docker_args = base_args + ["--network", "none", SANDBOX_IMAGE] + command
        return _run_bounded(docker_args, timeout, container_name=container_name)

    # Partially open: the worker has no direct egress. Only the sidecar proxy
    # is dual-homed, and it rejects destinations outside the exact host ACL.
    network = _ProxyNetworkContext(policy)
    with network as network_name:
        docker_args = base_args + [
            "--network", network_name,
            "-e", f"HTTP_PROXY={network.proxy_url}",
            "-e", f"HTTPS_PROXY={network.proxy_url}",
            "-e", f"http_proxy={network.proxy_url}",
            "-e", f"https_proxy={network.proxy_url}",
            "-e", "NO_PROXY=", "-e", "no_proxy=",
            SANDBOX_IMAGE,
        ] + command
        return _run_bounded(docker_args, timeout, container_name=container_name)


def run_verifier(
    command: List[str], policy: SandboxPolicy, workspace_dir: str,
    timeout: int = 60,
) -> dict:
    """Run trusted verification code with a read-only workspace and no network.

    Verification must observe artifacts, never mutate them. It therefore uses
    stricter settings than an execution step regardless of the intent's
    requested network policy.
    """
    if not _docker_available():
        raise SandboxUnavailable("Docker is required for trusted mechanical verification.")
    workspace = Path(workspace_dir).resolve()
    container_name = f"iw-verify-{secrets.token_hex(8)}"
    args = [
        "docker", "run", "--rm", "--name", container_name,
        "--read-only", "--network", "none",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "-v", f"{workspace}:/workspace:ro", "-w", "/workspace",
        "--user", SANDBOX_USER,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "256m", "--cpus", "1.0",
        "--ulimit", "nofile=128:128",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        SANDBOX_IMAGE,
    ] + command
    return _run_bounded(args, timeout, container_name=container_name)

def run_step_local_unsafe(
    command: List[str], policy: SandboxPolicy, workspace_dir: str,
    timeout: int = 120, allow_unsafe: bool = False,
) -> dict:
    """NOT a security boundary. Restricts cwd and env vars only. Does not
    prevent network access, filesystem access outside declared paths, or
    privilege escalation. Requires `allow_unsafe=True` explicitly so a
    caller can't reach this path by accident (e.g. Docker being
    temporarily unavailable in production silently falling back here)."""
    if not allow_unsafe:
        raise NetworkEnforcementError(
            "run_step_local_unsafe requires allow_unsafe=True — this is "
            "not a security boundary and must not be reached implicitly."
        )
    import os
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in policy.env_allowlist if k in os.environ}
    # Workflow commands are commonly generated for the Linux Docker image.
    # Keep the explicitly unsafe local test fallback portable on Windows.
    if os.name == "nt" and command and command[0] in {"python", "python3"}:
        command = [sys.executable, *command[1:]]
    result = subprocess.run(
        command, cwd=workspace_dir, env=env,
        capture_output=True, timeout=timeout, text=True,
    )
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


# ---------------------------------------------------------------------------
# Workspace snapshotting — for AC checks that need "no unexpected file
# changes" verified as a fact, not inferred from exit_code alone (finding
# #3: exit_code==0 doesn't prove stdout content or file-level side effects
# match what the criterion actually asked for).
# ---------------------------------------------------------------------------

def snapshot_workspace(workspace_dir: str) -> dict:
    """Returns {relative_path: sha256} for every file currently in the
    workspace. Call before and after a step to compute a real diff
    instead of trusting the model's claim about what it touched."""
    import hashlib
    base = Path(workspace_dir)
    snapshot = {}
    if not base.exists():
        return snapshot
    for p in base.rglob("*"):
        if p.is_file():
            snapshot[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


def diff_workspace(before: dict, after: dict) -> dict:
    added = {k: after[k] for k in after.keys() - before.keys()}
    removed = {k: before[k] for k in before.keys() - after.keys()}
    modified = {
        k: (before[k], after[k])
        for k in before.keys() & after.keys()
        if before[k] != after[k]
    }
    return {"added": added, "removed": removed, "modified": modified}
