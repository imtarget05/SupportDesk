# Webhook Integration & Event Subscriptions

## 1. Supported Events
- ticket.created, ticket.assigned, ticket.status_changed, ticket.resolved.

## 2. HMAC Signature Verification
All webhook payloads include the header X-SupportDesk-Signature containing the HMAC-SHA256 hash computed with your webhook secret.
Always verify this signature before processing incoming events.
