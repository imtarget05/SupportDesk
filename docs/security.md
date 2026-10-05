# Security Architecture & Policies: SupportDesk

This document outlines the security controls, authentication patterns, infrastructure protections, and AI safety mechanisms implemented in SupportDesk.

---

## 1. Authentication & Identity Boundaries

SupportDesk enforces strict separation of concerns across its edge, gateway, and service layers:

```
[Client / Browser]
        │
        ▼ (HTTPS / TLS 1.3)
[Cloudflare Edge] (WAF, DDoS, Rate Limiting)
        │
        ▼
[TypeScript Gateway] (Fastify 5 + jose + Zod)
   • Method-aware public path matching (e.g. POST /api/tickets is public contact; GET /api/tickets is auth-gated)
   • JWT signature and expiry verification
   • Request payload validation & body size caps
        │
        ▼ (Internal Network / Mutual Auth)
[Backend AI Service] (FastAPI + SQLAlchemy)
   • RBAC role enforcement (CUSTOMER, AGENT, ADMIN)
   • Domain isolation and tenant boundaries
```

### Identity Principles
1. **Gateway Verifies, Backend Issues**: The gateway verifies cryptographic signatures (`RS256` or `HS256`) but does not mint tokens. The backend remains the single source of credential issuance.
2. **Method-Aware Public Routing**: Public endpoints are strictly matched by both HTTP method and URI path, preventing authentication bypasses where a state-mutating or listing endpoint was inadvertently exposed.
3. **Managed Identity on Azure**: In the `production` profile, compute workloads authenticate to Azure Service Bus, Key Vault, and Azure PostgreSQL using User-Assigned Managed Identity (`UserAssignedIdentity`), completely eliminating stored credentials or connection string secrets from container configuration.

---

## 2. Infrastructure & Cloud Security

### Network Isolation
- **Container Apps Environment**: All container revisions run in a dedicated Azure Container Apps Environment backed by Azure Log Analytics.
- **Storage & Bus Restrictions**: Service Bus and databases enforce TLS 1.2 minimum, disable unencrypted transports, and reject nested public access.
- **Dual Deployment Profile**:
  - `PROFILE=portfolio`: Runs on Render Free + Cloudflare Pages for zero-cost public demonstrations, with mock/stub secrets safely isolated.
  - `PROFILE=production`: Deployed via Terraform to Azure Container Apps with Private Endpoints and Managed Identity.

### Secrets Management
- No secrets, tokens, or private keys are committed to Git.
- Local development relies on `.env` (strictly gitignored).
- Production secrets are resolved dynamically from Azure Key Vault via Managed Identity.

---

## 3. AI Safety & Guardrails

The AI service layer executes deterministic safety guardrails before returning or applying model suggestions:

| Guardrail Layer | Threat Prevented | Enforcement Mechanism |
|---|---|---|
| **Prompt Injection Filter** | Adversarial user text manipulating system prompts | Regex pattern screening & semantic heuristics in `services/guardrails.py` |
| **Output Commitment Guard** | LLM promising unauthorized refunds, SLAs, or legal guarantees | Deterministic regex and classification scanner |
| **PII Redaction** | Leakage of customer sensitive data (emails, credit cards, phones) | Regex-based token replacement prior to provider dispatch |
| **Human-In-The-Loop (HITL)** | Autonomous execution of high-risk actions (refunds, escalations) | LangGraph checkpoint interrupts & approval gate |

---

## 4. Auditability & Non-Repudiation

- **Immutable Audit Trail**: Every ticket state transition (`NEW` → `IN_PROGRESS` → `RESOLVED` → `CLOSED`), assignment, and automated action generates an immutable audit record in PostgreSQL.
- **Transactional Outbox**: Events published to Azure Service Bus utilize the Transactional Outbox pattern with idempotency keys (`requires_duplicate_detection = true`), preventing lost updates and duplicate processing during retries.
