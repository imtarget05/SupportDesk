# API Rate Limits & Throttling

## 1. Default Quotas
- Free Tier: 60 requests per minute per IP address.
- Pro Tier: 600 requests per minute per API token.
- Enterprise Tier: 3,000 requests per minute with dedicated bursting pool.

## 2. Handling HTTP 429
When exceeded, the API returns HTTP 429 Too Many Requests with a 'Retry-After' header indicating the wait duration in seconds.
Implement exponential backoff with jitter to handle rate limits gracefully.
