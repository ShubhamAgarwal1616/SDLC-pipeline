import { createServer } from "node:http";
import { routes } from "./adapters/http/routes.js";

const handle = routes();
const port = Number(process.env.PORT ?? 8080);

createServer((req, res) => {
  const chunks: Buffer[] = [];
  req.on("data", (chunk: Buffer) => chunks.push(chunk));
  req.on("end", () => {
    const url = new URL(req.url ?? "/", "http://localhost");
    const response = handle({ method: req.method ?? "GET", path: url.pathname, body: Buffer.concat(chunks).toString() });
    res.writeHead(response.status, response.headers);
    res.end(response.body);
  });
}).listen(port, () => console.log(`{{name}} listening on :${port}`));
