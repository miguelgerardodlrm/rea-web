# rea-web

Mapea una web o una API como la ve el navegador y devuelve **cliente + OpenAPI + docs + evidencia**.
Sin LLM: el mismo input produce el mismo mapa, así que un agente barato puede correrlo por centavos.

## Cumplimiento de los 7 puntos

| # | Requisito | Estado |
|---|---|---|
| 1 | Mapear el sitio como lo ve el navegador | ✅ páginas visitadas, SPA/SSR/estática, framework, formularios, inputs, links y flujos login/search/cart. No reconstruye el árbol de componentes React |
| 2 | APIs escondidas | ✅ fetch/XHR, GraphQL (operación, query, variables) y WebSockets (URL + frames), con método, headers, query, body y respuesta |
| 3 | Entender el flujo | ✅ `graph.json` con nodos, aristas por **procedencia de valores** (`$.id` → `query.id`), triggers DOM (clic/submit/Enter), huérfanos marcados, auth + expiración JWT, paginación y errores. La atribución es por ventana temporal (`inferred`) |
| 4 | Estático vs vivo | ✅ `static.json`: descarga bundles, busca fetch/axios/XHR/WS/GraphQL, resuelve template literals y source maps, y clasifica **observed / static_only / false_positive** |
| 5 | Evidencia por hallazgo | ✅ request/response real, headers, status y ref (`#/paths/...`, `#/transactions/N`, `#/websockets/N`), secretos redactados |
| 6 | Recrear, no copiar | ✅ `client.py` usable + `openapi.json` + un ejemplo por endpoint. Cliente Python; OpenAPI permite generar para otros stacks |
| 7 | Agente barato | ✅ CLI determinista + **servidor MCP stdio** (`mcp/server.py`) con `map_site`, `list_endpoints`, `get_evidence`, `get_client`, `get_graph` |

## Salida

```
out/
├── client.py          # cliente Python (stdlib) con un método por endpoint
├── openapi.json       # OpenAPI 3.0 con schemas, parámetros y evidencia (x-reaweb-*)
├── README.md          # site map, flujos, grafo, estático, endpoints y evidencia
├── map.json           # inventario normalizado + site + stats de grafo/estático
├── graph.json         # nodos, aristas por valores, triggers y huérfanos
├── static.json        # candidatos de bundles clasificados
└── evidence/
    ├── spec.json      # snapshot del OpenAPI/Swagger, si existe
    ├── network.json   # transacciones + frames WebSocket (secretos redactados)
    ├── events.json    # clics/submits/Enter observados
    └── site.json      # páginas, formularios, flujos y señales de rendering
```

## Cómo funciona

1. **Descubrir** — si la URL es un spec, lo carga (JSON/YAML). Si es una web, busca el spec en el HTML,
   config de Swagger UI/Redoc y rutas habituales.
2. **Mapear el sitio** — Chromium sin JS obtiene el HTML del servidor; luego el navegador real carga,
   hace scroll y (`--crawl N`) recorre enlaces internos. Se registran páginas, formularios, inputs y flujos.
3. **Capturar runtime** — XHR/fetch, GraphQL y frames WebSocket; además registra clics/submits/Enter con
   selector y texto (`--click` permite disparar un selector), y hashea los valores que salen en respuestas
   y entran en requests (nunca guarda el token en claro).
4. **Entender el flujo** — atribuye cada request al último evento DOM dentro de 2 s (o navegación, u
   huérfano) y crea aristas A → B cuando un valor de la respuesta A reaparece en el request B. Filtra
   fechas, strings < 8 y números < 1000 para no inventar dependencias.
5. **Analizar estático** — baja bundles same-site/CDN, busca literales de fetch/axios/XHR/WS/GraphQL,
   template literals y source maps; compara con lo observado y clasifica cada candidato.
6. **Generar** — `client.py`, `openapi.json`, `graph.json`, `static.json`, `README.md` y evidencia.
   `--smoke` ejecuta GETs seguros contra la API real.

Secretos (Authorization, cookies, tokens en query/body/frames) se redactan antes de escribir la evidencia.

## Uso

```bash
pip install -e .
playwright install chromium

# spec declarado
rea-web map https://petstore3.swagger.io/api/v3/openapi.json -o out/petstore --smoke

# web sin spec: 4 páginas, scroll profundo, evidencia completa
rea-web map https://www.binance.com/en/markets -o out/binance \
  --mode web --crawl 4 --scroll-passes 5 --full-evidence --smoke

# archivo local / YAML / página de docs con Swagger UI
rea-web map ./openapi.yaml -o out/local
rea-web map https://servicio/docs -o out/auto

rea-web smoke out/binance
```

`map` acepta `--mode auto|spec|web`, `--wait` (ms), `--crawl` (páginas), `--scroll-passes`,
`--full-evidence`, `--no-static`, `--click SELECTOR` (repetible; puede disparar escrituras),
`--no-scroll`, `--include-external`, `--token`, `--timeout`, `--json`.

```bash
# disparar un botón "cargar más" y atribuir sus requests al clic
rea-web map https://sitio/lista -o out/lista --mode web --click "#load-more"
```

## Demo real

| Objetivo | Modo | Resultado |
|---|---|---|
| Petstore 3 | spec | 19 endpoints, cliente ejecutado, smoke OK, `openapi.json` de 13 paths |
| Petstore Swagger 2 | spec | 20 endpoints, basePath `/v2` OK |
| **Binance markets** | web, 2 páginas, ~13 s | **26 endpoints (24 HTTP + 2 WebSockets)**, rendering `spa`, flujos search, paginación en banners, OpenAPI de 24 paths, smoke 17 OK / 2 fallos |
| Open Banking UK | spec | 29 endpoints de banco (sin smoke: requiere sandbox/OAuth) |
| Hacker News (Algolia) | web, 1 página | 1 endpoint runtime + **75 candidatos estáticos (22 static-only)**, incluyendo `/api/v1/items/:id` no disparado |
| Mercado Libre | web | 0: catálogo SSR + redirect anti-bot; requiere grabar clics/formularios |

## MCP (agente barato)

`mcp/server.py` es un wrapper stdio sin lógica propia: traduce tools MCP a `python -m reaweb ... --json`
y a lecturas de `map.json`/`graph.json`/evidencia. En Pi, apuntá tu `mcp.json` a:

```json
{
  "mcpServers": {
    "rea-web": {
      "command": "python3",
      "args": ["/ruta/a/rea-web/mcp/server.py"]
    }
  }
}
```

Tools: `map_site`, `list_endpoints`, `get_evidence`, `get_client`, `get_graph`. No hay LLM ni claves:
los secretos siguen redactados por el CLI.

## Evidencia

Cada endpoint apunta a su fuente:

- `evidence/spec.json#/paths/~1pet~1{petId}/get` — declarado en OpenAPI.
- `evidence/network.json#/transactions/12` — request/response observado.
- `evidence/network.json#/websockets/2` — frames observados.
- `evidence/site.json#/pages/0` — página/flujo observado.

Los tests verifican que un JWT enviado por la app no aparece en la evidencia.

## Tests

```bash
python -m unittest discover -s tests -v
```

6 tests contra un servidor local: OpenAPI → cliente ejecutado, captura web con
path/query/tipos/paginación/errores, JWT + redacción, GraphQL, site map con login,
crawl, OpenAPI, grafo (clic → `/api/product`, arista `sku`), estático (observed/static-only/
false-positive + source map) y handshake MCP con evidencia.

## Límites y próximos pasos

- No ejecuta clics/formularios/login por defecto: los flujos se detectan; `--click` dispara selectores sueltos
  y el grafo atribuye por ventana temporal. Grabar secuencias completas (`fill` + submit + login) sigue pendiente.
- La procedencia de valores es heurística: filtra cortos/fechas y marca `inferred`; un ID repetido puede
  coincidir por casualidad. Las aristas incluyen `from_field`/`to_field` y hash para auditar.
- El estático no resuelve concatenaciones dinámicas (`base + "/users"`) ni ejecuta código; los candidatos
  `static_only` son no confirmados, no endpoints garantizados.
- WebSockets: se guardan URL y frames; los binarios se cuentan pero no se decodifican, y el cliente no abre sockets.
- Cliente generado en Python; para TypeScript/Java/etc. usar `openapi.json` con `openapi-generator`.
- El wrapper MCP expone solo map/read: no ejecuta tools de escritura ni mantiene estado entre llamadas.
