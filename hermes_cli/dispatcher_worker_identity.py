"""Fail-closed startup gate for dispatcher worker identity."""

from __future__ import annotations

import sys


def bind_dispatcher_worker_identity() -> None:
    """Bind a pending dispatcher handshake before any CLI command executes."""
    from agent import dispatcher_identity

    try:
        dispatcher_identity.bind_from_handshake()
    except dispatcher_identity.IdentityBindError as exc:
        print(f"dispatcher worker identity handshake failed: {exc}", file=sys.stderr)
        raise SystemExit(70) from exc
