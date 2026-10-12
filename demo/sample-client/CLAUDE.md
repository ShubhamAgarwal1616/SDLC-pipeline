# notes-client

Kotlin client library for notes-api (repo `Auranestt-candles/workflow-test`). Used as the consumer in the pipeline's multi-repo demo.

- Coding rules: the `kotlin-service-standards` skill. Results are sealed types (`domain/`); HTTP lives in `adapters/http/`.
- Build and test (offline in the sandbox): `./gradlew --offline test`
- The provider's API contract is notes-api's living specs (`openspec/specs/` in workflow-test). In pipeline runs they're mounted read-only at `/opt/sdlc/contracts/`.
