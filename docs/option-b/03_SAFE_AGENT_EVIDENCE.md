# Phase G3 Evidence: Safe AI Agent with Tools & Human-In-The-Loop (HITL) Gate

**Product:** Agentic Support Operations (`SupportDesk`)  
**Capability:** Tool Calling + Safe Agent Orchestration with Human-In-The-Loop Gate  
**Branch:** `feature/option-b-safe-agent`  
**Date:** 2026-10-24  
**Evidence Status:** `UNIT_VERIFIED` + `INTEGRATION_VERIFIED`  

---

## 1. Business Problem & Safety Model

In an enterprise customer support operations platform, an autonomous agent cannot be permitted to execute high-impact side effects without verification:
- **Financial Risk:** An LLM hallucinates an order refund or processes compensation without verification.
- **Operational Risk:** An LLM prematurely escalates normal tickets or closes unresolved critical tickets.
- **Reputational Risk:** An unvetted LLM response is sent directly to customers without human review.

### Strict Safety Invariants
1. **Read-Only Inspection Tools:** Factual queries (`search_knowledge_base`, `get_customer_history`, `lookup_order`, `get_ticket_history`) are safe to execute autonomously.
2. **Action Proposals, Not Autonomous Execution:** High-impact tools (`propose_refund`, `propose_escalation`, `propose_external_reply`) create an immutable `ActionProposal` record in `pending_approval` status. **No money is moved, no ticket priority is modified, and no customer message is sent.**
3. **Human-In-The-Loop Authorization:** Only authenticated operators with `agent` or `admin` roles can call `/approve` or `/reject`. Customer role attempts receive `403 Forbidden`.
4. **State Monotonicity:** Proposals transition strictly from `pending_approval` → `approved` OR `rejected`. Re-approval or re-rejection attempts receive `409 Conflict`.
5. **Durable Audit Trail:** Every proposal creation, approval execution, and rejection halting is recorded in the `audit_events` table in the same database transaction.

---

## 2. Technical Implementation

```
               +-------------------------------------------------------+
               |                  Incoming Ticket                      |
               +---------------------------+---------------------------+
                                           |
                                           v
                             +---------------------------+
                             |     ToolCallingAgent      |
                             +-------------+-------------+
                                           |
                    +----------------------+----------------------+
                    |                                             |
                    v                                             v
     [Read-Only Allowlist Tools]                     [Sensitive Proposal Tools]
     - search_knowledge_base                         - propose_refund
     - get_customer_history                          - propose_escalation
     - lookup_order                                  - propose_external_reply
     - get_ticket_history                                         |
                    |                                             v
                    |                                +-------------------------+
                    |                                |     ActionProposal      |
                    |                                | status=pending_approval |
                    |                                +------------+------------+
                    |                                             |
                    v                                             v
     +-----------------------------+                 +-------------------------+
     | LLM Generates Context Draft |                 | Human-In-The-Loop Review|
     +-----------------------------+                 | POST .../approve|reject |
                                                     +------------+------------+
                                                                  |
                                           +----------------------+----------------------+
                                           |                                             |
                                           v                                             v
                                      [Approved]                                    [Rejected]
                            - Action executed (refund logged/             - Action halted immediately
                              escalation set/message posted)              - State remains untouched
                            - Audit log: proposal_approved                - Audit log: proposal_rejected
```

### Components Added:
- **Data Model:** `app/models/proposal.py` (`ActionProposal` table).
- **Service:** `app/services/hitl_service.py` (`create_proposal`, `approve_proposal`, `reject_proposal`).
- **Tools:**
  - `CustomerHistoryTool`: aggregates customer past ticket counts, resolution status, and SLA tier (`standard` vs `vip`).
  - `ProposeRefundTool`: creates pending refund proposal with amount and rationale.
  - `ProposeEscalationTool`: creates pending escalation proposal.
  - `ProposeExternalReplyTool`: queues outbound reply for approval.
- **REST Endpoints:** `app/api/proposals.py`
  - `GET /api/tickets/{ticket_id}/proposals`
  - `GET /api/tickets/{ticket_id}/proposals/{proposal_id}`
  - `POST /api/tickets/{ticket_id}/proposals/{proposal_id}/approve`
  - `POST /api/tickets/{ticket_id}/proposals/{proposal_id}/reject`

---

## 3. Measured Test Evidence

### A. Dedicated Safe Agent & HITL Test Suite
Command:
```bash
AI_PROVIDER=stub .venv/bin/pytest backend/tests/test_safe_agent_hitl.py -v
```
Output:
```text
============================= test session starts ==============================
backend/tests/test_safe_agent_hitl.py::TestReadOnlyTools::test_customer_history_tool PASSED
backend/tests/test_safe_agent_hitl.py::TestReadOnlyTools::test_knowledge_search_tool_read_only PASSED
backend/tests/test_safe_agent_hitl.py::TestReadOnlyTools::test_order_lookup_tool_read_only PASSED
backend/tests/test_safe_agent_hitl.py::TestProposalCreationAndSafety::test_propose_refund_creates_pending_proposal PASSED
backend/tests/test_safe_agent_hitl.py::TestProposalCreationAndSafety::test_propose_escalation_does_not_mutate_ticket_state PASSED
backend/tests/test_safe_agent_hitl.py::TestHITLApprovalAndRejectionFlow::test_approval_executes_escalation_and_records_audit PASSED
backend/tests/test_safe_agent_hitl.py::TestHITLApprovalAndRejectionFlow::test_rejection_halts_action_and_leaves_state_intact PASSED
backend/tests/test_safe_agent_hitl.py::TestAPIEndpointsAndAuthorization::test_api_proposals_list_and_approval PASSED
backend/tests/test_safe_agent_hitl.py::TestAPIEndpointsAndAuthorization::test_customer_cannot_approve_or_reject_proposal PASSED
backend/tests/test_safe_agent_hitl.py::TestEndToEndAgentHITL::test_agent_run_proposes_refund_and_waits_for_approval PASSED

============================== 10 passed in 8.56s ==============================
```

### B. Full SupportDesk Test Suite Regression Check
Command:
```bash
AI_PROVIDER=stub .venv/bin/pytest backend/tests/ -q
```
Output:
```text
435 passed, 2 skipped in 71.93s
```
Zero regressions across all existing features (tickets, auth, SLA, temporal, graph workflow, MCP server, vector store).

### C. Offline Labeled Evaluation Suite
Command:
```bash
.venv/bin/python evaluation/eval_suite.py --check
```
Output:
```text
Dataset: 92 labeled tickets | provider: stub | model: n/a

Accuracy                  93.5%
Macro-F1                  0.94
Provider errors           0
Schema validity           92/92 (100.0%)
Guardrail blocks          20/92 (21.7%)
Latency p50 (ms)          0.80
Latency p95 (ms)          1.22
No regression vs baseline.
```

---

## 4. Interview Talking Points (Backend + AI)

1. **Why an Agent must NOT have direct execution privileges:**
   - In autonomous architectures, giving an LLM write access to a payments or database gateway turns an unvalidated generation problem into a financial loss or compliance breach.
   - We decoupled **Intent Generation** (`ProposeRefundTool`) from **Intent Execution** (`hitl_service.approve_proposal`). The LLM is restricted to creating an `ActionProposal` with reasoning and parameters.
2. **Auditing and Explainability:**
   - Every proposal persists the exact structured payload and model reasoning.
   - When an agent approves or rejects, the operator's identity, decision timestamp, and optional override reason are committed atomically to `audit_events`.
3. **Role-Based Security Boundary:**
   - Endpoints `/approve` and `/reject` are protected by `require_agent` dependencies, preventing unauthorized customers from manipulating the decision queue.
