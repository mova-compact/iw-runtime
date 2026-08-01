"""CI smoke test for the real Docker enforcement boundary."""

import os
import tempfile
from pathlib import Path

from runtime.permission_broker import SandboxPolicy
from runtime.sandbox import run_step


def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        if os.name != "nt":
            os.chmod(workspace, 0o777)
        result = run_step(
            ["python", "-c", "import os; open('probe.txt','w').write(str(os.getuid()))"],
            SandboxPolicy(), workspace, timeout=30,
        )
        assert result["exit_code"] == 0, result
        assert Path(workspace, "probe.txt").read_text() == "65534"
        network = run_step(
            ["python", "-c",
             "import socket; socket.create_connection(('1.1.1.1',80),2)"],
            SandboxPolicy(), workspace, timeout=15,
        )
        assert network["exit_code"] != 0, "deny-all container unexpectedly reached internet"
    print("Docker E2E passed: non-root write and deny-all network enforced")


if __name__ == "__main__":
    main()
