# Threat Model: SupportDesk

This document applies the STRIDE methodology to analyze potential security threats to the SupportDesk event-driven architecture and documents mitigation strategies.

---

## 1. System Overview & Assets

**Critical Assets:**
- Customer ticket data, contact information, and attachment contents.
- Audit trails and SLA compliance metrics.
- Internal AI prompts, system instructions, and knowledge base vector embeddings.
- Azure Service Bus topics, queues, and PostgreSQL database state.

---

## 2. STRIDE Analysis & Mitigations

### S - Spoofing Identity
* **Threat**: Malicious actor attempts to forge JWT claims or impersonate an Agent to view tickets.
* **Mitigation**:
  - JWT verification performed at Gateway using standard cryptographic validation (`jose`).
  - Algorithm confusion attacks prevented by pinning accepted algorithms (`RS256` / `HS256`).
  - Ticket access is strictly scoped to `owner_id` or requires `AGENT` / `ADMIN` role.

### T - Tampering
* **Threat**: An attacker modifies ticket parameters during transition or tampers with message payloads on the bus.
* **Mitigation**:
  - Request bodies validated with Zod schemas at Gateway and Pydantic models at Backend.
  - Azure Service Bus messages are signed and transmitted over TLS 1.2+ encrypted channels.
  - Transactional Outbox ensures database changes and message events are committed in a single atomic transaction.

### R - Repudiation
* **Threat**: An agent or user claims they did not trigger a ticket resolution, assignment, or refund.
* **Mitigation**:
  - Immutable audit logs capture actor ID, timestamp, prior state, new state, and reason for every transition.
  - AI traces record exact model responses, token usage, and prompts in `ai_call_traces`.

### I - Information Disclosure
* **Threat**: Sensitive customer data or API keys leak via logs, error responses, or model prompts.
* **Mitigation**:
  - PII redaction layer filters customer emails, credit cards, and phone numbers before sending to external LLMs.
  - Generic error messages returned to clients; detailed tracebacks suppressed in production.
  - Managed Identity replaces plaintext connection strings and tokens in Azure Container Apps.

### D - Denial of Service (DoS)
* **Threat**: Volumetric traffic spikes or malicious actors spamming large ticket bodies to overwhelm the AI service.
* **Mitigation**:
  - Cloudflare Edge provides DDoS mitigation and rate limiting.
  - Gateway enforces rigid request payload limits (100KB default).
  - Background workers utilize Azure Service Bus dead-letter queues (`max_delivery_count = 3` or `5`) to isolate poison-pill messages.
  - Azure Container Apps scale on HTTP concurrency and KEDA queue depth.

### E - Elevation of Privilege
* **Threat**: Unauthenticated or normal user exploits AI action tools to perform refunds or administrative escalations.
* **Mitigation**:
  - Role-based route guards in FastAPI dependencies (`app/deps.py`).
  - High-impact AI actions require Human-in-the-Loop approval via LangGraph interrupts; LLMs cannot autonomously finalize state transitions.
