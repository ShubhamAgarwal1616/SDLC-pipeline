/** HTTP routes as a pure function: easy to test without a server. Wiring lives in src/main.ts. */
export type HttpRequest = { method: string; path: string; headers?: Record<string, string>; body?: string };
export type HttpResponse = { status: number; headers?: Record<string, string>; body?: string };
export type Handler = (request: HttpRequest) => HttpResponse;

const json = (status: number, value: unknown): HttpResponse => ({
  status,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(value),
});

export function routes(): Handler {
  return (request) => {
    if (request.method === "GET" && request.path === "/health") return json(200, { status: "ok" });
    return json(404, { error: { code: "not_found", message: `No route for ${request.method} ${request.path}` } });
  };
}
