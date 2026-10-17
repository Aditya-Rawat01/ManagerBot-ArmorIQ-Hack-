"""Shared offline doubles for the request orchestrator (P3-04; P3-09 extends).

Test-only: never imported by ``apps/`` or ``services/`` (guardrail test).
Nothing here touches ArmorIQ, Slack, Anthropic, Neon or any network, and no
result produced with these doubles is gate evidence.

- :mod:`.gateway`: :class:`ScriptedGateway`, a call-counting ArmorIQ gateway
  double with per-tool step scripts (allow, not-found, block, tool-server
  rejection, unverified, HOLD that waits and resolves, HOLD returned
  pending, raise) and the E2 behaviour of ``attempt_off_plan`` (a planned
  name passes the SDK check and would execute).
- :mod:`.audit`: :class:`RecordingAudit`, the ``services.audit.store`` write
  functions as an in-memory recorder with failure injection; it applies the
  store's real event checks to ``append_event``.
- :mod:`.delivery`: :class:`FakeDelivery`, ``deliver`` / ``notify_hold``
  doubles bound to the real P3-06 signatures.
- :mod:`.llm`: planner model and responder generator doubles.
- :mod:`.slack`: :class:`RecordingSlackSender` (the P3-05 sender shape).
- :mod:`.scenario`: fake settings, the member table, trusted contexts,
  result builders and :func:`build_harness`, which wires an orchestrator
  from the doubles above.
"""
