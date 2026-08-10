#!/usr/bin/env python3
"""Prove the app imports and serves with the network dropped.

A standalone product that cannot boot without reaching a host is not standalone,
and the way that regression arrives is always the same: somebody adds an import
that phones home at module scope, and it works on every developer machine.

Sockets are patched to raise before anything is imported. Then every module is
walk-imported, the app is booted, and `/healthz` and `/readyz` are called. The
loopback address stays open because a test client may use it.
"""

from __future__ import annotations

import pkgutil
import socket
import sys
from pathlib import Path

# Runnable from a bare checkout as well as from an editable install, because
# "does this boot without the network" should not itself require a build step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REAL_CONNECT = socket.socket.connect
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _blocked(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host in _LOOPBACK:
        return _REAL_CONNECT(self, address)  # type: ignore[arg-type,return-value]
    raise RuntimeError(f"the app tried to reach {address!r} during import or boot")


def main() -> int:
    socket.socket.connect = _blocked  # type: ignore[method-assign]

    import massingplan

    failures: list[str] = []
    for module in pkgutil.walk_packages(massingplan.__path__, "massingplan."):
        # The adoption kits are written against a *consumer's* package, which is
        # not installed here -- see massingplan/integrations/massing/README.md.
        # They are linted and type-checked; they are not importable, and that is
        # the point of them living upstream rather than in the consumer.
        if module.name.startswith("massingplan.integrations."):
            continue
        try:
            __import__(module.name)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            failures.append(f"{module.name}: {exc}")
    if failures:
        print("modules that could not be imported offline:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    from massingplan.app import create_app
    from massingplan.config import Settings

    # A throwaway key for a throwaway in-process app that never serves a request
    # to anyone. Named after not being a secret.
    app = create_app(Settings(env="testing", secret_key="offline-check"))  # noqa: S106
    client = app.test_client()
    for path, expected in (("/healthz", 200), ("/readyz", 200), ("/", 200), ("/demo", 200)):
        response = client.get(path)
        if response.status_code != expected:
            print(f"{path} returned {response.status_code}, expected {expected}", file=sys.stderr)
            return 1

    print("the app imports, boots and serves with the network dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
