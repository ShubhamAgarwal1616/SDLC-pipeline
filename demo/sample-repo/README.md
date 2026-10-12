# notes-api

A small notes API (Kotlin 2.4, Http4k 6, Kotest), used as the demo target for the SDLC pipeline.

| Endpoint | Auth | |
|---|---|---|
| `GET /health` | – | health check |
| `GET /notes` | – | list notes |
| `POST /notes` | `X-API-Key` | create `{"title": "...", "body": "..."}` |

```bash
./gradlew test
API_KEYS=key-1 INTERNAL_API_KEYS=internal-1 ./gradlew run
```
