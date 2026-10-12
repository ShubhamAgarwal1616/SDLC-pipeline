# Clients respect rate limits

## Problem
notes-api now rate-limits each client (429 with Retry-After and RateLimit-* headers), but our client
library ignores this: callers get raw failures, and well-behaved apps can't see how much quota is left.

## Users
Developers who use the notes-client library to talk to notes-api.

## Goals
The client library should handle being rate limited gracefully and let callers see their remaining quota.
