import { test } from "node:test";
import assert from "node:assert/strict";
import { routes } from "../src/adapters/http/routes.js";

test("reports health", () => {
  const response = routes()({ method: "GET", path: "/health" });
  assert.equal(response.status, 200);
  assert.match(response.body ?? "", /ok/);
});

test("unknown routes are 404 with the error envelope", () => {
  const response = routes()({ method: "GET", path: "/nope" });
  assert.equal(response.status, 404);
  assert.match(response.body ?? "", /not_found/);
});
