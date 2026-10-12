# {{name}}

Node.js/TypeScript service built with the SDLC pipeline.

- Layout (hexagonal): `src/domain/` (entities, ports; no I/O), `src/services/` (use cases), `src/adapters/` (HTTP, storage). Wiring only in `src/main.ts`.
- HTTP routes are a pure function (`src/adapters/http/routes.ts`): test them without a server.
- TypeScript strict mode, ES modules (`.js` extensions in relative imports). Tests: `node:test` + `node:assert/strict` in `test/**/*.test.ts`.
- Build and test (offline in the sandbox): `npm ci`, then `npm test`. `npm run lint` type-checks.
- Dependencies: only those already in `package.json`. Adding one is a human decision (a normal PR that updates the lockfile).
- Errors use the envelope `{"error": {"code", "message"}}`.
- Domain glossary: `CONTEXT.md`. Decisions: `docs/adr/`. Specs: `openspec/`.
