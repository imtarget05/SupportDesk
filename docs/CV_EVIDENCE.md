# CV Evidence Matrix — SupportDesk

Date: 2026-10-05. Status: VERIFIED = reproducible here; PARTIAL = real but
bounded; REMOVE = do not claim.

| CV claim | Evidence (file:line / artifact) | Reproduce command | Status |
|---|---|---|---|
| Ticket assignment persists assignee | `backend/app/models/ticket.py` (`assignee_id` FK, nullable) + migrations `20261005_add_ticket_assignee.py`; `ticket_service.assign_ticket` (target must exist + role=agent); route `POST /api/tickets/{id}/assign` returns persisted `assignee` in `TicketOut` | `python -m pytest tests/test_ticket_endpoint_gaps.py -q` (14 passed: persist, reassign, 404/422/403, reload) | VERIFIED |
| Durable audit trail (survives restart) | `backend/app/models/audit.py` (`audit_events` table) + migration `20261005_add_audit_events.py`; `app/services/audit.py` (`log_event/list_events`, secret-scrubbed details, chronological order); written for create/assign/transition/automation | Same gap suite (`test_audit_survives_session_reload_and_orders_chronologically`, `test_audit_details_never_store_secrets`) | VERIFIED |
| Transactional Outbox Pattern | `backend/app/models/outbox.py` (`outbox_events` table with composite index `[status, created_at]`), `app/services/outbox.py` (`record_outbox_event`), `ticket_service.py` emits `TicketCreated`, `TicketAssigned`, `TicketStatusChanged`, `TicketResolved` atomically in the DB transaction | `python -m pytest tests/test_outbox_kafka_sla.py -k outbox -q` (3 passed: atomic insert, dispatch, failure handling) | VERIFIED |
| Idempotent Event Consumer + DLQ | `backend/app/models/outbox.py` (`processed_events` table with `[event_id, consumer_group]` unique constraint), `app/workers/event_consumer.py` (idempotency check, handler dispatch, exponential retry, DLQ routing after 3 failed attempts) | `python -m pytest tests/test_outbox_kafka_sla.py -k "idempotency or dlq" -q` (3 passed: duplicate suppression, DLQ routing) | VERIFIED |
| Priority-based SLA Engine | `backend/app/services/sla_service.py` (Urgent 1h/4h, High 4h/8h, Normal 8h/24h, Low 24h/48h), 80% warning alerts, breach detection with auto-escalation, live metrics queried directly from DB (`GET /api/sla/metrics`) | `python -m pytest tests/test_outbox_kafka_sla.py -k sla -q` (4 passed: target calculation, warning, breach, live metrics) | VERIFIED |
| Synchronous automation, honestly labeled | `backend/app/api/automation.py::run_automation_job` docstring states synchronous execution; audit `details.execution=synchronous`; no fake PENDING/RUNNING lifecycle | `python -m pytest tests/test_ticket_endpoint_gaps.py::test_automation_jobs -q` | VERIFIED (as sync execution — never claim "async job queue") |
| JWT auth + RBAC (customer/agent isolation, no self-escalation) | `app/security.py`, `app/deps.py:require_agent` (agent-only), `ensure_ticket_visible` (customer sees own); gateway re-verifies (`gateway/src/`) | Backend `361 passed, 1 skipped`; gateway `56 passed` | VERIFIED |
| Illegal transition → 409; state machine | `app/services/state_machine.py`; guarded conditional UPDATE in `update_ticket` (lost-race → conflict) | `test_transition_ticket_invalid_conflict`, `test_ticket_concurrency.py` | VERIFIED |
| AI eval: 92 labeled tickets, stub 93.5% | `evaluation/tickets.json` (92 records, counted by code); `evaluation/baseline.json` (stub accuracy 0.9348); README table labeled "offline stub provider" | `AI_PROVIDER=stub python evaluation/eval_suite.py --check` ("No regression vs baseline") | VERIFIED as stated; REMOVE any "100+ tickets" or "30% misclassification reduction" wording (no such baseline exists) |
| Test suites | Backend 361 passed/1 skipped; frontend 15 passed; gateway 56 passed (2026-10-06) | `python -m pytest tests/ -q` (backend); `npm test` (frontend, gateway) | VERIFIED |

Known limitations: SPA token in `localStorage` (documented tradeoff in README
Security Model — do not call it best-practice); multi-tenancy (out of scope per `docs/spec.md`).

