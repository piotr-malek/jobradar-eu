# Projects (Ana Ferreira, made up)

## Main projects
### Payment service for a ticket marketplace (2023 to 2026) [payments, python]
Built and ran the service that takes card payments for a ticket marketplace: about 40,000 orders a day across
Portugal and Spain. Python (FastAPI), PostgreSQL, Stripe and a local card acquirer. Refunds, chargebacks and payouts.

### Order events on Kafka (2025) [events, kafka]
Moved order processing from nightly cron jobs to Kafka consumers. Refunds and seat releases went from next-day to a
few minutes, and the support team stopped fixing orders by hand.

## Supporting projects
### Booking API for gyms (2021 to 2023) [python, django]
Django REST API for class bookings in 120 gyms: waiting lists, no-show fees, instructor shift changes.

## Small fixes (only for a question that asks for exactly this kind of story)
### Duplicate charges on retries
A payment retried after a timeout was charged twice. Added an idempotency key per order; duplicates stopped.
