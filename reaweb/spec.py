"""OpenAPI/Swagger loading, normalization, and spec discovery on websites."""
from __future__ import annotations

import json
import re
import urllib.parse

import yaml

from .schema import ANY, fmt, from_openapi
from .util import escape_pointer, fetch, now_iso, origin, sanitize_ident

SPEC_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger/v1/swagger.json",
    "/api-docs",
    "/v2/api-docs",
    "/api/openapi.json",
    "/api/swagger.json",
    "/docs/openapi.json",
    "/.well-known/openapi.json",
]
SPEC_HINT = re.compile(
    r"""["']([^"']{1,400}?(?:openapi|swagger|api[-_]?docs)[^"']{0,400}?\.(?:json|ya?ml)(?:\?[^"']*)?)["']""",
    re.I,
)
CONFIG_HINT = re.compile(
    r"""(?:url|spec-url|data-url)\s*[:=]\s*["']([^"']{1,400}?(?:openapi|swagger|api[-_]?docs)[^"']{0,400}?)["']""",
    re.I,
)


def looks_like_spec(doc):
    return isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) and isinstance(doc.get("paths"), dict)


def parse_doc(raw, content_type=""):
    text = raw.decode("utf-8", "replace").lstrip("\ufeff")
    if "yaml" in content_type or "yml" in content_type:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None


def load_spec(src, timeout=20):
    """Load a local path or URL as OpenAPI/Swagger. Returns a dict with doc/raw/source/base_url."""
    raw, ctype = fetch(src, timeout=timeout)
    doc = parse_doc(raw, ctype if "yaml" in ctype or "json" in ctype else "")
    if not looks_like_spec(doc):
        raise ValueError(f"not an OpenAPI/Swagger document: {src}")
    return {
        "doc": doc,
        "raw": raw,
        "source": src,
        "fetched_at": now_iso(),
        "base_url": default_base_url(doc, src),
    }


def default_base_url(doc, source):
    servers = doc.get("servers") or []
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        url = servers[0]["url"]
        if re.match(r"^https?://", url):
            return url.rstrip("/")
        return urllib.parse.urljoin(origin(source) + "/", url).rstrip("/")
    host = doc.get("host")
    if host:
        scheme = (doc.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{doc.get('basePath', '')}".rstrip("/")
    return origin(source) if re.match(r"^https?://", source) else ""


def _resolve(node, root):
    seen = 0
    while isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/") and seen < 10:
        target = root
        for part in node["$ref"][2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        node = target
        seen += 1
    return node if isinstance(node, dict) else {}


def _param_type(param, root):
    schema = _resolve(param.get("schema") or {}, root)
    kind = schema.get("type") or param.get("type") or "string"
    return kind


def _param(param, root):
    param = _resolve(param, root)
    name = param.get("name", "param")
    where = param.get("in", "query")
    schema = _resolve(param.get("schema") or {}, root)
    example = param.get("example", param.get("x-example"))
    if example is None:
        example = schema.get("example", schema.get("default"))
    enum = schema.get("enum") or param.get("enum")
    if example is None and enum:
        example = enum[0]
    return {
        "name": name,
        "arg": sanitize_ident(name, "param"),
        "in": where,
        "type": _param_type(param, root),
        "required": bool(param.get("required")) or where == "path",
        "description": (param.get("description") or "").strip()[:300],
        "example": example,
        "enum": enum,
    }


def _response(doc, op, root):
    responses = op.get("responses") or {}
    keys = [k for k in ("200", "201", "202", "204", "default") if k in responses]
    keys += sorted(k for k in responses if str(k).startswith("2") and k not in keys)
    if not keys:
        return None
    node = _resolve(responses[keys[0]], root)
    content = node.get("content") or {}
    ctype = next((c for c in content if "json" in c), next(iter(content), None))
    schema = None
    if ctype:
        schema = (content[ctype] or {}).get("schema")
    elif node.get("schema"):
        schema = node["schema"]
        ctype = (doc.get("produces") or ["application/json"])[0]
    return {
        "status": keys[0],
        "content_type": ctype,
        "schema": from_openapi(schema, root) if schema else None,
    }


def _request_body(doc, op, params, root):
    body = op.get("requestBody")
    if body:
        body = _resolve(body, root)
        content = body.get("content") or {}
        ctype = next((c for c in content if "json" in c), next(iter(content), None))
        schema = (content.get(ctype) or {}).get("schema") if ctype else None
        return {
            "content_type": ctype or "application/json",
            "schema": from_openapi(schema, root) if schema else None,
            "required": bool(body.get("required")),
        }
    for p in params:
        if p.get("in") == "body":
            return {
                "content_type": (doc.get("consumes") or ["application/json"])[0],
                "schema": from_openapi(p.get("schema"), root),
                "required": bool(p.get("required")),
            }
    return None


def normalize_spec(doc, source):
    """Turn a loaded spec into the shared endpoint inventory."""
    root = doc
    base = default_base_url(doc, source)
    endpoints, used = [], set()
    schemes = (doc.get("components") or {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
    auth = []
    for name, raw in schemes.items():
        s = _resolve(raw, root)
        entry = {"name": name, "type": s.get("type")}
        if s.get("type") == "apiKey":
            entry.update({"in": s.get("in"), "key": s.get("name")})
        if s.get("scheme"):
            entry["scheme"] = s["scheme"]
        auth.append(entry)

    for path, item in (doc.get("paths") or {}).items():
        item = _resolve(item, root)
        path_params = [_param(p, root) for p in item.get("parameters") or []]
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            op = _resolve(op, root)
            merged = {p["name"]: p for p in path_params}
            for raw in op.get("parameters") or []:
                p = _param(raw, root)
                merged[p["name"]] = p
            params = [p for p in merged.values() if p["in"] in ("path", "query", "header")]
            base_id = sanitize_ident(op.get("operationId") or f"{method}_{path}")
            eid, n = base_id, 2
            while eid in used:
                eid, n = f"{base_id}_{n}", n + 1
            used.add(eid)
            endpoints.append({
                "id": eid,
                "method": method.upper(),
                "path": path,
                "base_url": base,
                "summary": (op.get("summary") or "").strip()[:300],
                "description": (op.get("description") or "").strip()[:1000],
                "tags": op.get("tags") or ["default"],
                "params": params,
                "body": _request_body(doc, op, op.get("parameters") or [], root),
                "response": _response(doc, op, root),
                "evidence": [{
                    "source": "openapi",
                    "url": source,
                    "ref": f"evidence/spec.json#/paths/{escape_pointer(path)}/{method.lower()}",
                    "confidence": "declared",
                }],
                "confidence": "declared",
            })
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return {
        "title": (doc.get("info") or {}).get("title") or "API",
        "version": (doc.get("info") or {}).get("version"),
        "description": ((doc.get("info") or {}).get("description") or "").strip()[:1000],
        "base_url": base,
        "auth": auth,
        "endpoints": endpoints,
    }


def _try_spec(url):
    try:
        return load_spec(url, timeout=10)
    except Exception:
        return None


def discover_spec(url):
    """Find an OpenAPI document from an API URL, a spec URL, or a website.
    Returns a dict from load_spec, or None."""
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith((".json", ".yaml", ".yml")):
        found = _try_spec(url)
        if found:
            return found
    try:
        raw, ctype = fetch(url, timeout=15)
    except Exception:
        raw, ctype = b"", ""
    if raw:
        doc = parse_doc(raw, ctype)
        if looks_like_spec(doc):
            return {
                "doc": doc,
                "raw": raw,
                "source": url,
                "fetched_at": now_iso(),
                "base_url": default_base_url(doc, url),
            }
        html = raw.decode("utf-8", "replace")
        candidates = [m.group(1) for m in SPEC_HINT.finditer(html)]
        candidates += [m.group(1) for m in CONFIG_HINT.finditer(html)]
    else:
        candidates = []
    candidates += [origin(url) + p for p in SPEC_PATHS]
    seen, tried = set(), 0
    for cand in candidates:
        cand = urllib.parse.urljoin(url, cand.strip())
        if cand in seen or not re.match(r"^https?://", cand):
            continue
        seen.add(cand)
        tried += 1
        if tried > 12:
            break
        found = _try_spec(cand)
        if found:
            return found
    return None


def auth_note(auth):
    parts = []
    for a in auth:
        if a.get("type") == "apiKey":
            parts.append(f"api key `{a.get('key')}` in {a.get('in')}")
        elif a.get("type") == "http":
            parts.append(f"http {a.get('scheme', 'auth')}")
        elif a.get("type") == "oauth2":
            parts.append("oauth2")
        else:
            parts.append(a.get("type") or "auth")
    return "; ".join(parts)


__all__ = ["load_spec", "discover_spec", "normalize_spec", "looks_like_spec", "parse_doc", "auth_note", "fmt", "ANY"]
