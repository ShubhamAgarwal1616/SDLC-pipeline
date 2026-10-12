# {{name}}

Kotlin/Http4k service built with the SDLC pipeline.

- Architecture and coding rules: the `kotlin-service-standards` skill (DDD + hexagonal: `domain/`, `services/`, `adapters/`; wiring only in `Dependencies.kt` / `Application.kt`).
- Base package: `{{package}}`.
- Build and test (offline in the sandbox): `./gradlew --offline test`
- Dependencies: only those already in `build.gradle.kts`. Adding one is a human decision (a normal PR).
- Domain glossary: `CONTEXT.md`. Decisions: `docs/adr/`. Specs: `openspec/`.
