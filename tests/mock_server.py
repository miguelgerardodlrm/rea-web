"""Tiny mock API + HTML app used by the end-to-end tests."""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ITEMS = [
    {"id": 1, "name": "alpha", "sku": "SKU-10000001", "tags": ["a", "b"], "owner": {"name": "Ana", "email": "ana@example.test"}},
    {"id": 2, "name": "beta", "sku": "SKU-10000002", "tags": [], "owner": {"name": "Bo", "email": "bo@example.test"}},
    {"id": 3, "name": "gamma", "sku": "SKU-10000003", "tags": ["c"], "owner": {"name": "Cy", "email": "cy@example.test"}},
]

JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + base64.urlsafe_b64encode(
    json.dumps({"sub": "mock", "exp": int(time.time()) + 3600}).encode()
).rstrip(b"=").decode() + ".signature"

APP_JS = b"""const list = fetch('/api/items');
const unused = '/api/secret-unused';
const external = 'https://example.com/api/nope';
const op = 'query GetItems';
const templated = `/api/items/${id}`;
// fetch('/api/old-comment')
//# sourceMappingURL=/app.js.map
window.__bundleLoaded = true;
"""

APP_MAP = json.dumps({
    "version": 3,
    "sources": ["app.ts"],
    "sourcesContent": ["fetch('/api/from-map');"],
}).encode()

APP_HTML = f"""<!doctype html><html><head><title>Mock app</title><script src="/app.js"></script></head><body>
<h1>Mock app</h1>
<form action="/login" method="post">
  <input name="username" placeholder="User">
  <input type="password" name="password" placeholder="Password">
  <button type="submit">Sign in</button>
</form>
<button id="load-more">Load product</button>
<script>
const token = '{JWT}';
fetch('/api/items?limit=2&page=1', {{headers: {{Authorization: 'Bearer ' + token}}}}).then(r => r.json());
fetch('/api/items/42').then(r => r.json());
fetch('/api/missing').then(r => r.json());
fetch('/graphql', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
  body: JSON.stringify({{query: 'query GetItems($limit: Int) {{ items(limit: $limit) {{ id name }} }}',
                         variables: {{limit: 1}}}})}}).then(r => r.json());
fetch('https://analytics.example.test/collect?token=' + token).catch(() => {{}});
document.getElementById('load-more').addEventListener('click', () => fetch('/api/product?sku=SKU-10000001'));
setTimeout(() => document.getElementById('load-more').click(), 600);
</script><a href="/page2">More</a></body></html>""".encode()

PAGE2_HTML = b"""<!doctype html><html><head><title>Mock reviews</title></head><body>
<script>fetch('/api/reviews?limit=5').then(r => r.json());</script></body></html>"""

REVIEWS = [{"id": i, "stars": i % 5 + 1, "text": f"review {i}"} for i in range(1, 6)]

DOCS_HTML = b"""<!doctype html><html><body><div id="swagger-ui"></div>
<script>SwaggerUIBundle({url: "/openapi.json", dom_id: "#swagger-ui"});</script></body></html>"""


def spec(base):
    return {
        "openapi": "3.0.3",
        "info": {"title": "Mock Store", "version": "1.0.0"},
        "servers": [{"url": base}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "Owner": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
                },
                "Item": {
                    "type": "object",
                    "required": ["id", "name"],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "owner": {"$ref": "#/components/schemas/Owner"},
                    },
                },
            },
        },
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "summary": "List items",
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "example": 2}}],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {
                                "type": "array", "items": {"$ref": "#/components/schemas/Item"}}}},
                        }
                    },
                },
                "post": {
                    "operationId": "createItem",
                    "summary": "Create item",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
                        }
                    },
                },
            },
            "/items/{itemId}": {
                "get": {
                    "operationId": "getItem",
                    "summary": "Get one item",
                    "parameters": [{
                        "name": "itemId", "in": "path", "required": True,
                        "schema": {"type": "integer", "example": 1},
                    }],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}},
                        }
                    },
                }
            },
        },
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/openapi.json":
            self._send(200, spec(self.server.base_url))
        elif url.path == "/app":
            self._send(200, APP_HTML, "text/html")
        elif url.path == "/page2":
            self._send(200, PAGE2_HTML, "text/html")
        elif url.path == "/api/reviews":
            self._send(200, REVIEWS)
        elif url.path == "/api/product":
            sku = parse_qs(url.query).get("sku", [""])[0]
            self._send(200, {"id": 1001, "sku": sku, "name": "product"})
        elif url.path == "/api/missing":
            self._send(404, {"error": "not found", "code": "MISSING"})
        elif url.path == "/docs":
            self._send(200, DOCS_HTML, "text/html")
        elif url.path == "/app.js":
            self._send(200, APP_JS, "application/javascript")
        elif url.path == "/app.js.map":
            self._send(200, APP_MAP)
        elif url.path in ("/items", "/api/items"):
            limit = parse_qs(url.query).get("limit", [None])[0]
            items = ITEMS[: int(limit)] if limit and limit.isdigit() else ITEMS
            self._send(200, items)
        elif url.path.startswith(("/items/", "/api/items/")):
            tail = url.path.rsplit("/", 1)[-1]
            found = next((item for item in ITEMS if str(item["id"]) == tail), None)
            if found is None and tail.isdigit():
                found = {"id": int(tail), "name": f"item-{tail}", "tags": []}
            self._send(200 if found else 404, found or {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if urlsplit(self.path).path == "/graphql":
            self._send(200, {"data": {"items": [ITEMS[0]]}})
            return
        self._send(201, {**payload, "id": 99})


def make_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    srv = make_server()
    print(srv.base_url, flush=True)
    threading.Event().wait()
