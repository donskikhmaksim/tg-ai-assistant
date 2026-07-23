"""Audit / restore plane (Phase 0 — foundation).

A durable, append-only `audit_log` records every change to the owner's
TickTick/Google state richly enough to answer "who changed what, when" and to
restore almost anything, whether the change came from our automation via MCP
(in-band) or from the owner/collaborator editing directly (out-of-band, caught
by a delta poller).

Public surface:
  log.record_mutation / log.finalize_mutation — the two-phase writers.
  poller.run_ticktick_audit_poll             — the out-of-band TickTick job.
  reconcile.*                                — pure attribution/diff helpers.

Everything here is FAIL-OPEN: when auditing is disabled or misconfigured the
writers and the poller no-op and never raise into the pipeline (mirrors the
qwen/transcribe pattern).
"""
