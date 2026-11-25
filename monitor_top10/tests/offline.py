"""Run the standalone tests with socket creation, DNS and subprocesses denied."""
import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    attempts = []

    def deny(event):
        attempts.append(event)
        raise RuntimeError("network or subprocess prohibited during offline tests")

    def audit(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            deny(event)

    sys.addaudithook(audit)
    with ExitStack() as guard:
        for name in ("create_connection", "create_server", "getaddrinfo", "gethostbyname",
                     "gethostbyname_ex", "socket", "socketpair"):
            def denied(*args, operation=name, **kwargs):
                deny(operation)
            guard.enter_context(patch.object(socket, name, denied))
        suite = unittest.defaultTestLoader.discover(str(root / "monitor_top10/tests"), top_level_dir=str(root))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Offline tests: {result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)} passed, "
          f"{len(result.failures) + len(result.errors)} failed, {len(result.skipped)} skipped")
    print(f"Network or subprocess attempts: {len(attempts)}")
    return int(not result.wasSuccessful() or bool(attempts))


if __name__ == "__main__":
    raise SystemExit(main())
