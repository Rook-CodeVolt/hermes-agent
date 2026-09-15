"""Narrow trusted in-process authority contexts for Kanban mutation authority
(design t_a1260456 section 8, inventory rows D1/D2/H1/M1; Phase B, t_c67e90a0).

Each context below is a plain boolean ``ContextVar`` -- unforgeable from
outside the process for the same reason ``agent.delegation_context``'s
``_DELEGATED_CHILD_CONTEXT`` is (nothing but this module's own code can set
a ContextVar in this interpreter). They must be entered ONLY by admitted,
already-trusted control-flow code, immediately around the narrow operation
they cover -- never derived from env/argv/TTY, and never left set for
longer than the trusted operation actually runs (every context manager
below resets on the way out, including on exception).

Call sites (see design section 8 / the Pass 2 inventory table, and
``kanban_invocation_authority.decide_mutation_authority`` which consults
these):

* :func:`dispatcher_authority` -- ONLY the embedded gateway dispatcher tick
  (``gateway.kanban_watchers_dispatcher._KanbanDispatcher.tick_once_for_board``).
  Deliberately NOT entered by ``hermes_cli.kanban_db_dispatch.dispatch_once``
  itself, nor by the standalone ``hermes kanban dispatch`` / ``daemon`` CLI
  commands that also call it directly -- per the inventory's D1 decision,
  standalone dispatch CLI stays denied unless started through an
  authenticated service launcher, which the embedded gateway watcher is and
  a bare CLI invocation is not.
* :func:`gateway_notifier_authority` -- ONLY the embedded gateway notifier's
  own claim/advance/rewind/unsub/GC writes
  (``gateway.kanban_watchers_notifier`` / ``gateway.kanban_watchers``).
* :func:`dashboard_request_authority` -- ONLY inside an already-authenticated
  dashboard REST request handler, after the app-level Host/session/OAuth
  gate has run (FastAPI middleware executes before route handlers, so this
  router's handlers are reached only post-auth); never derived from ambient
  worker env. Websocket routes (H2, read-only) never enter this.
* :func:`maintenance_authority` -- reserved for an admitted scheduler/
  service/broker running platform maintenance (GC/repair/migration).
  Deliberately NOT entered by the plain ``hermes kanban gc`` / ``repair``
  CLI commands -- per the inventory's M1 decision, "no generic local CLI
  exception" -- until such a broker exists, so those commands stay denied
  under Phase B enforcement exactly like standalone dispatch. This is a
  known, inventory-approved compatibility consequence, not an oversight;
  see the Phase A/B cutover report on t_c67e90a0.
* :func:`test_authority` -- ONLY pytest/eval fixtures against temporary
  Kanban DB roots (E1/CI1); never against a production/current board.
* :func:`schema_migration_authority` -- ONLY the codebase's own internal,
  idempotent, one-shot-per-process schema/column-migration/backfill writes
  performed transparently inside ``hermes_cli.kanban_db_connect.connect``'s
  ``_init_if_needed`` (design t_a1260456 §8/§9.13, cutover finding recorded
  on t_c67e90a0: NOT one of the inventory's original 13 rows -- discovered
  while writing the §9.13 test matrix). These writes are not requested by
  the calling process at all -- every FIRST connection to a given DB path
  in a process's lifetime transparently runs
  ``conn.executescript(SCHEMA_SQL)`` / ``_migrate_add_optional_columns`` /
  ``_backfill_legacy_inflight_runs`` before the caller's own code ever
  runs, entirely regardless of whether that caller has (or will ever use)
  any Kanban mutation authority. Gating this behind the ordinary
  worker/dispatcher/etc. authority classes would mean an unauthenticated
  read-only caller (the C1 "read verbs and export remain available" case
  the inventory explicitly preserves) fails on its very FIRST connection,
  before it ever reaches a read. This context is entered ONLY by
  ``kanban_db_connect.connect``'s own internal ``_init_if_needed`` closure
  -- never by anything a caller's board/env/argv can influence -- and
  covers ONLY that migration write, not any caller code that runs after
  ``connect()`` returns.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

_DISPATCHER_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_dispatcher_authority", default=False)
_GATEWAY_NOTIFIER_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_gateway_notifier_authority", default=False)
_DASHBOARD_REQUEST_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_dashboard_request_authority", default=False)
_MAINTENANCE_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_maintenance_authority", default=False)
_TEST_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_test_authority", default=False)
_SCHEMA_MIGRATION_AUTHORITY: ContextVar[bool] = ContextVar("hermes_kanban_schema_migration_authority", default=False)

# Ordered (label, ContextVar) so ``current_trusted_authority_class`` has one
# deterministic precedence order; in practice at most one is ever set at a
# time (these contexts are entered around narrow, non-overlapping trusted
# call sites), but a fixed order keeps the function total regardless.
_CONTEXTS: tuple[tuple[str, "ContextVar[bool]"], ...] = (
    ("dispatcher", _DISPATCHER_AUTHORITY),
    ("gateway_notifier", _GATEWAY_NOTIFIER_AUTHORITY),
    ("dashboard_request", _DASHBOARD_REQUEST_AUTHORITY),
    ("maintenance", _MAINTENANCE_AUTHORITY),
    ("test", _TEST_AUTHORITY),
    ("schema_migration", _SCHEMA_MIGRATION_AUTHORITY),
)


def _make_context(var: "ContextVar[bool]"):
    @contextmanager
    def _ctx() -> Iterator[None]:
        token: Token[bool] = var.set(True)
        try:
            yield
        finally:
            var.reset(token)

    return _ctx


dispatcher_authority = _make_context(_DISPATCHER_AUTHORITY)
gateway_notifier_authority = _make_context(_GATEWAY_NOTIFIER_AUTHORITY)
dashboard_request_authority = _make_context(_DASHBOARD_REQUEST_AUTHORITY)
maintenance_authority = _make_context(_MAINTENANCE_AUTHORITY)
test_authority = _make_context(_TEST_AUTHORITY)
schema_migration_authority = _make_context(_SCHEMA_MIGRATION_AUTHORITY)


def current_trusted_authority_class() -> Optional[str]:
    """The first (only, in legitimate use) trusted context label currently
    active in this process/task, or ``None``. Consulted by
    :func:`hermes_cli.kanban_invocation_authority.decide_mutation_authority`
    as evaluation-order step 2 (design section 6): an explicit trusted
    context authorizes only its own declared scope, checked BEFORE the
    PID-bound worker grant so a trusted service path never needs a token.
    """
    for label, var in _CONTEXTS:
        if var.get():
            return label
    return None


def board_pointer_authority_holder() -> Optional[str]:
    """Whether the current context may mutate the board POINTER itself
    (``set_current_board`` / ``clear_current_board``) -- design section 5:
    these have no target-board connection, so a worker grant (board-scoped
    by construction) is never sufficient regardless of validity. Only the
    two contexts that legitimately touch the pointer today qualify:
    dashboard board-switch/import routes (H1) and an admitted maintenance
    import/migration path (M1). Returns the authority label or ``None``.
    """
    if _DASHBOARD_REQUEST_AUTHORITY.get():
        return "dashboard_request"
    if _MAINTENANCE_AUTHORITY.get():
        return "maintenance"
    return None
