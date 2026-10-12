---
name: kotlin-service-standards
description: >
  Activate this skill whenever you are writing, reviewing, refactoring, or designing any Kotlin code
  in this microservice codebase — even if the user has NOT mentioned "architecture" or "standards".
  This includes: writing a new feature, function, class, or file in Kotlin; reviewing Kotlin code for
  correctness or quality; refactoring existing Kotlin code; structuring a new feature across layers;
  designing a domain model, entity, value object, or aggregate; designing a use case; designing an
  adapter (GCP, HTTP, cache); designing or writing tests; and enforcing project conventions.
  Do NOT activate for: generic Kotlin language questions unrelated to this codebase; Gradle build or
  dependency version troubleshooting with no code involved; infrastructure, deployment, or CI/CD
  questions that contain no application code.
---

# Kotlin Microservice Coding Standards

## Purpose

This skill governs all Kotlin code in this microservice. The project follows **DDD + Hexagonal
Architecture (Ports & Adapters)** on Kotlin JVM 21 with Http4k 6.x, GCP adapters (Pub/Sub,
Firestore, Cloud Storage), and a Kotest/MockK test suite. Code is **synchronous by default**;
Kotlin Coroutines 1.10.x are introduced only where there is a concrete concurrency, scale, or
streaming benefit (see CORO rules).

---

## When to read reference files

| Situation | Read before acting |
|---|---|
| Creating any new file; structuring a feature; designing a domain object, service, or adapter; performing a code review | `references/architecture.md` |
| Writing or reviewing any GCP adapter (Pub/Sub, Firestore, GCS) | `references/gcp-adapters.md` |
| Writing any test | `references/testing.md` |
| Reviewing or refactoring existing code | `references/anti-patterns.md` |
| Writing routes, lenses, or Http4k filters | `references/http4k-patterns.md` |

The compact rules below are sufficient **only** for trivial, single-file edits in a layer you
already fully understand.

> **If a reference file is absent:** apply the compact rules below and explicitly flag the gap to
> the user so the missing file can be created.

---

## Hard rules — quick reference

### Architecture & layer dependency (ARCH) — severity: error

| ID | Rule |
|---|---|
| ARCH-01 | `domain/` imports stdlib and pure Kotlin only — no GCP, Http4k, Jackson, or kotlinx.coroutines types. |
| ARCH-02 | `services/` imports `domain/` only — never touches adapters directly; orchestrates via output ports. |
| ARCH-03 | `adapters/` is the only layer allowed to import GCP, Http4k, Jackson, Config4k, and Caffeine. |
| ARCH-04 | `adapters/output/` never depends on `services/` or on the HTTP layer; it only implements `domain/ports/output/`. |
| ARCH-05 | `Application.kt` and `Dependencies.kt` are the only files where all layers may meet. |
| ARCH-06 | Business logic never lives in route handlers, consumers, or GCP adapters — it belongs in `services/`. |
| ARCH-07 | Firestore/GCP models are never used directly as domain entities — map at the adapter boundary. |
| ARCH-08 | Jackson/serialization annotations never appear on domain classes — annotate only adapter models. |
| ARCH-09 | Mappers (e.g., `ProductDocument.toDomain()`) live in the adapter's own package, never in `domain/` or `services/`. |
| ARCH-10 | Caffeine caches live in `adapters/output/cache/` as decorator implementations of output port interfaces, wired in `Dependencies.kt`. |
| ARCH-11 | Services are verb-phrase classes with exactly one public method named `execute(...)`. Overloads are not permitted — model different inputs as sealed types. Define a `domain/ports/input/` interface only for event-driven consumers; HTTP routes call services directly. |
| ARCH-12 | Logging is only permitted in `adapters/` and `Application.kt` — never in `domain/` or `services/`. |

### Coroutine safety (CORO) — severity: error

| ID | Rule |
|---|---|
| CORO-00 | **Synchronous by default.** Prefer plain functions for ports, services, and adapters. Introduce `suspend`/coroutines only for a concrete benefit: fan-out concurrency, non-blocking IO under high concurrency, or streaming. Do not make signatures `suspend` reflexively. |
| CORO-01 | The `suspend` modifier is permitted on port interfaces in `domain/`, but no kotlinx.coroutines library types (`Flow`, `Deferred`, `Channel`, `CoroutineScope`, `Job`, dispatchers) may appear anywhere in `domain/`, including port signatures. |
| CORO-02 | When an adapter is `suspend`, blocking GCP SDK calls inside it must be wrapped in `withContext(Dispatchers.IO)`. |
| CORO-03 | `Thread.sleep()` is forbidden in coroutine contexts — use `delay()`. |
| CORO-04 | `runBlocking` must never appear inside suspend functions, services, or Http4k handlers — only at a non-coroutine SDK callback boundary (e.g. Pub/Sub `MessageReceiver`). |
| CORO-05 | `GlobalScope` is forbidden — use a lifecycle-bound `CoroutineScope` with `SupervisorJob()`. |
| CORO-06 | Adapters own all dispatcher and scope decisions. |

### Error handling (ERR) — severity: error

| ID | Rule |
|---|---|
| ERR-01 | Operations that can fail return a sealed result type defined in `domain/model/`. |
| ERR-02 | Arrow is not a project dependency — never introduce `Either` or suggest adding Arrow. |
| ERR-03 | `kotlin.Result` is acceptable only inside adapters as an implementation detail — never in port signatures. `runCatching` catches every `Throwable`: rethrow `CancellationException` rather than mapping it to a domain error — swallowing it in a `suspend` adapter breaks structured concurrency. |
| ERR-04 | Exceptions are never used to signal *expected* domain outcomes (not found, validation failed, insufficient stock) — model those as sealed result types. Precondition and programmer-error checks (`require`/`check`, e.g. invariants in a value object's `init` block) **do** throw — that is correct, and is not a domain error. |
| ERR-05 | Adapters catch SDK exceptions at the boundary and map them to sealed error types. |
| ERR-06 | `null` must never be returned to signal failure — use sealed error types. |
| ERR-07 | `when` on sealed types must be exhaustive — every branch must be handled. |
| ERR-08 | Adapters must catch SDK exceptions and either map to a sealed error type or rethrow as a typed unchecked exception with the original as cause. Never swallow exceptions silently. |

### Http4k usage (HTTP)

| ID | Severity | Rule |
|---|---|---|
| HTTP-01 | error | Raw string parsing from requests is forbidden — always use lenses. |
| HTTP-02 | error | Throwing exceptions from handlers is forbidden — return a typed `Response` with the appropriate status. |
| HTTP-03 | error | Side effects must not appear inside lens definitions — keep lenses pure, side effects in handlers. |
| HTTP-04 | style | Use functional composition (`then()`) for filter chains; avoid imperative middleware. |
| HTTP-05 | error | HTTP error responses must use the project's standard error envelope (see `references/http4k-patterns.md`). Do not invent ad-hoc JSON error shapes. |

### Idiomatic Kotlin (STYLE)

| ID | Severity | Rule |
|---|---|---|
| STYLE-01 | error | `!!` is forbidden — use `?.`, `?:`, `requireNotNull`/`checkNotNull` with a message, or sealed types. |
| STYLE-02 | style | Prefer `val` over `var`; use `var` only when mutability is genuinely required. Avoid mutable Java collections — use `listOf()`, `mapOf()`. |
| STYLE-03 | style | Prefer sealed classes over inheritance hierarchies; use exhaustive `when`. |
| STYLE-04 | style | Prefer top-level functions over companion-object utility dumping grounds. |
| STYLE-05 | style | Prefer expression bodies and scope functions (`let`/`also`/`run`/`with`/`apply`) over imperative temporaries and Java idioms. |
| STYLE-06 | style | No Java-isms: no manual loops where a collection operator fits (`map`/`filter`/`sumOf`), no getter/setter ceremony, no `Optional`, no statics-via-companion. |

### Naming conventions (NAMING)

| ID | Severity | Rule |
|---|---|---|
| NAMING-01 | style | Sealed result types are named `{Operation}Result` (e.g. `CreateOrderResult`). |
| NAMING-02 | style | Input ports are named `{Verb}{Noun}` (e.g. `ProcessOrder`). Output ports are named `{Noun}Repository` (data) or `{Noun}Gateway` (external system). |
| NAMING-03 | style | Value objects and sealed result types use `data class`/`data object` (or `value class` for single-field VOs). Entities are `data class` with an explicit identity value object as the first property (e.g. `val id: OrderId`) — that id carries the identity. |

### Testing (TEST)

| ID | Severity | Rule |
|---|---|---|
| TEST-01 | error | `domain/` tests are pure unit tests — zero dependencies, no mocks needed. |
| TEST-02 | error | `services/` tests use MockK (or hand-written fakes) for output ports; services tested in isolation. |
| TEST-03 | error | `adapters/` tests are integration tests per adapter, testing wiring/mapping only. |
| TEST-04 | error | `acceptance/` tests drive the fully wired app black-box (e.g. in-memory Http4k handler), asserting behaviour end-to-end. |
| TEST-05 | error | For `suspend` code, use `runTest` from kotlinx-coroutines-test — never `runBlocking` in tests. Synchronous code needs no coroutine test scaffolding. |
| TEST-06 | error | Every sealed result branch must have a test. |
| TEST-07 | error | Tests that rely on preloaded data (acceptance, integration/emulator) must be self-contained: seed the data they need before each case and remove it after. Test cases must be independent and order-insensitive — never depend on data left behind by another test. |
| TEST-08 | error | `Thread.sleep()` is forbidden in tests. Wait on a condition with Kotest primitives — `eventually` (and `continually`/`until`) — never a fixed sleep. |
| TEST-09 | style | Use DescribeSpec or ShouldSpec; describe behaviour, not implementation. |
| TEST-10 | style | Use `shouldBe`, `shouldBeInstanceOf<T>()`, and data class equality assertions. |

---

## Response behaviour

**Writing code:** Identify the correct layer first. Use only dependencies allowed in that layer.
Prefer the simplest design satisfying the constraints. Keep HTTP, GCP, and domain concerns separated.

**Reviewing code:** Produce the review output format below. Show a corrected version for every
error-severity violation.

**Refactoring:** Preserve behaviour. Improve boundaries. Avoid expanding scope unnecessarily.

**When uncertain:** Choose the most conservative architecture. Favor explicit code over clever
abstractions. Never relax a rule to make code compile — restructure instead.

---

## Review output format

Every code review must end with a violations table in exactly this shape, followed by corrected
code for each error-severity violation:

| Rule ID | Severity | Location | Violation | Fix summary |
|---------|----------|----------|-----------|-------------|
| ARCH-06 | error | OrderRoutes.kt:42 | Business logic in route handler | Move to `PlaceOrder` service |

If there are no violations, state explicitly: **"No violations found."** Do not invent nitpicks.

---

## Pre-delivery validation checklist

Run this checklist before delivering any code:

- [ ] Correct layer chosen for each new file or class
- [ ] No forbidden imports for the target layer (ARCH-01 through ARCH-05)
- [ ] Services are verb-phrase classes with `execute()`; input ports only for event-driven consumers (ARCH-11)
- [ ] Synchronous by default — `suspend`/coroutines added only where justified (CORO-00)
- [ ] No kotlinx.coroutines types in `domain/` (CORO-01)
- [ ] No blocking calls on coroutine threads when `suspend` is used (CORO-02, CORO-03)
- [ ] No `runBlocking` in handlers, services, or suspend functions (CORO-04)
- [ ] No `GlobalScope` usage (CORO-05)
- [ ] No `!!` anywhere (STYLE-01)
- [ ] Errors modelled as sealed types, not exceptions or nulls (ERR-01, ERR-04, ERR-06)
- [ ] HTTP/GCP models mapped at boundaries, not passed into domain (ARCH-07, ARCH-08)
- [ ] Every `when` on a sealed type is exhaustive (ERR-07)
- [ ] Tests cover every sealed result branch (TEST-06)
- [ ] Tests are self-contained: data seeded before and cleaned up after each case (TEST-07)
- [ ] No `Thread.sleep()` in tests — use `eventually` or Kotest condition primitives (TEST-08)
