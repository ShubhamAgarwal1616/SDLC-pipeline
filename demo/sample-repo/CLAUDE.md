# notes-api

Small Kotlin/Http4k service used as the SDLC pipeline's demo target.

- Architecture and coding rules: the `kotlin-service-standards` skill (DDD + hexagonal: `domain/`, `services/`, `adapters/`; wiring only in `Dependencies.kt` / `Application.kt`).
- Build and test (offline in the sandbox): `./gradlew --offline test`
- Errors use the standard envelope in `adapters/input/http/ErrorEnvelope.kt`.
- API keys: `X-API-Key` header; `ApiKeys.internal` keys belong to internal service callers.
- Domain glossary: `CONTEXT.md`. Specs: `openspec/`.
