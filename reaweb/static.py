"""Static analysis of loaded JS bundles: find candidate API calls without executing them."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse

from .capture import NOISE_RE, normalize_path
from .util import fetch, host_of, registrable

MAX_BUNDLES = 12
MAX_BYTES = 3_000_000
MAX_CANDIDATES = 300
STATIC_EXT = re.compile(r"\.(?:js|css|map|png|jpe?g|gif|svg|ico|woff2?|ttf|pdf|zip|mp4)$", re.I)
FALSE_HINTS = re.compile(r"(example\.(com|org)|/docs?/|/schema|/redoc|/swagger-ui|node_modules|sourceMappingURL)", re.I)

CALL_RE = re.compile(
    r"""(?:fetch|axios(?:\.(?:get|post|put|patch|delete|head))?|window\.fetch)\s*\(\s*"""
    r"""(?P<q>["'`])(?P<value>[^"'`\s]{2,200})(?P=q)""", re.I)
XHR_RE = re.compile(r"""\.open\s*\(\s*["'][A-Z]+["']\s*,\s*["'](?P<value>[^"']{2,200})["']""", re.I)
PATH_RE = re.compile(
    r"""(?P<q>["'`])(?P<value>/(?:api|bapi|v\d+|graphql|gql|rest|services?)[A-Za-z0-9_\-./{}:$]*)(?P=q)""",
    re.I)
URL_RE = re.compile(r"""(?P<q>["'`])(?P<value>https?://[^"'`\s]{8,220})(?P=q)""")
WS_RE = re.compile(r"""(?P<q>["'`])(?P<value>wss?://[^"'`\s]{8,220})(?P=q)""")
GQL_RE = re.compile(r"""(?P<q>["'`])(?:query|mutation|subscription)\s+(?P<value>[A-Za-z_]\w*)""")
SOURCE_MAP_RE = re.compile(r"//[#@]\s*sourceMappingURL=(\S+)")


def _in_comment(text, position):
    start = text.rfind("\n", 0, position) + 1
    segment = text[start:position]
    quote = None
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "/" and index + 1 < len(segment) and segment[index + 1] in "/*":
            return True
        index += 1
    return False


def _shape(path):
    template, _ = normalize_path(path)
    return re.sub(r"\{[^}]+\}", "{}", template)


def _candidate(value):
    value = value.strip()
    if "${" in value:
        value = re.sub(r"\$\{[^}]+\}", "{param}", value)
        templated = True
    else:
        templated = False
    if value.startswith(("http://", "https://", "ws://", "wss://")):
        parts = urllib.parse.urlsplit(value)
        path = parts.path or "/"
        return value, path, parts.netloc.lower(), templated
    if not value.startswith("/"):
        return None
    path = urllib.parse.urlsplit(value).path
    return value, path, "", templated


def analyze_bundles(script_urls, endpoints, base_domain):
    """Download up to MAX_BUNDLES scripts, classify endpoint candidates."""
    os_endpoints = [e for e in endpoints if not e.get("external")]
    http_shapes = {_shape(e["path"]): e for e in os_endpoints if e.get("kind") != "websocket"}
    ws_shapes = {_shape(e["path"]): e for e in os_endpoints if e.get("kind") == "websocket"}
    gql_ops = {(e.get("graphql") or {}).get("operation"): e
               for e in os_endpoints if e.get("kind") == "graphql"}
    bundles, candidates, seen = [], [], set()
    source_maps = []
    script_urls = sorted(set(script_urls), key=lambda url: (
        0 if registrable(host_of(url)) == base_domain else 1, "vendor" in url.lower(), url))

    for url in script_urls:
        if len(bundles) >= MAX_BUNDLES:
            break
        if not url.startswith(("http://", "https://")) or NOISE_RE.search(url):
            continue
        entry = {"url": url}
        try:
            raw, _ = fetch(url, timeout=15)
        except Exception as exc:
            entry["error"] = str(exc)[:200]
            bundles.append(entry)
            continue
        entry.update({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]})
        bundles.append(entry)
        text = raw[:MAX_BYTES].decode("utf-8", "replace")
        if len(raw) > MAX_BYTES:
            entry["truncated"] = True
        _scan(text, url, os_endpoints, http_shapes, ws_shapes, gql_ops,
              candidates, seen, base_domain)

        match = SOURCE_MAP_RE.search(text)
        if match:
            map_url = urllib.parse.urljoin(url, match.group(1))
            if map_url.startswith(("http://", "https://")):
                entry["source_map"] = map_url
                try:
                    raw_map, _ = fetch(map_url, timeout=15)
                    source_map = json.loads(raw_map[:5_000_000])
                    sources = source_map.get("sources") or []
                    entry["sources"] = len(sources)
                    source_maps.append({"bundle": url, "url": map_url, "sources": sources[:50]})
                    content = source_map.get("sourcesContent") or []
                    for name, source in zip(sources[:20], content[:20]):
                        if isinstance(source, str):
                            _scan(source, f"map:{name}", os_endpoints, http_shapes, ws_shapes,
                                  gql_ops, candidates, seen, base_domain)
                except Exception as exc:
                    entry["source_map_error"] = str(exc)[:200]

    candidates.sort(key=lambda item: (item["classification"] != "observed",
                                      item["classification"] != "static_only", item["value"]))
    candidates = candidates[:MAX_CANDIDATES]
    stats = {key: sum(1 for item in candidates if item["classification"] == key)
             for key in ("observed", "static_only", "false_positive")}
    return {
        "bundles": bundles,
        "source_maps": source_maps,
        "candidates": candidates,
        "stats": {"bundles": len(bundles), "candidates": len(candidates), **stats},
    }


def _scan(text, label, endpoints, http_shapes, ws_shapes, gql_ops, out, seen, base_domain):
    scans = [
        (CALL_RE, "call"), (XHR_RE, "call"), (PATH_RE, "path"),
        (URL_RE, "url"), (WS_RE, "websocket"), (GQL_RE, "graphql"),
    ]
    for pattern, kind in scans:
        for match in pattern.finditer(text):
            value = match.group("value")
            parsed = _candidate(value)
            if not parsed:
                continue
            original, path, host, templated = parsed
            key = (label, original)
            if key in seen:
                continue
            seen.add(key)
            result = {
                "value": original,
                "kind": kind,
                "bundle": label,
                "offset": match.start(),
                "templated": templated,
                "classification": "static_only",
                "confidence": "unconfirmed",
                "reason": "plausible call, not observed at runtime",
                "endpoint": None,
            }
            if _in_comment(text, match.start()):
                result.update({"classification": "false_positive", "reason": "inside a comment"})
            elif FALSE_HINTS.search(value) or STATIC_EXT.search(path):
                result.update({"classification": "false_positive", "reason": "docs/schema/asset/example"})
            elif kind == "graphql":
                endpoint = gql_ops.get(value)
                if endpoint:
                    result.update({"classification": "observed", "endpoint": endpoint["id"],
                                   "confidence": "observed", "reason": "GraphQL operation observed"})
            elif kind == "websocket" or original.startswith(("ws://", "wss://")):
                endpoint = ws_shapes.get(_shape(path))
                if endpoint:
                    result.update({"classification": "observed", "endpoint": endpoint["id"],
                                   "confidence": "observed", "reason": "WebSocket path observed"})
                elif host and registrable(host) != base_domain:
                    result.update({"classification": "false_positive", "reason": "third-party host"})
            elif host and registrable(host) != base_domain:
                result.update({"classification": "false_positive", "reason": "third-party host"})
            else:
                endpoint = http_shapes.get(_shape(path))
                endpoint_host = (endpoint or {}).get("host") or urllib.parse.urlsplit(
                    (endpoint or {}).get("base_url") or "").netloc
                if endpoint and host and endpoint_host and host != endpoint_host:
                    result.update({"reason": "same path on a different host; unconfirmed"})
                elif endpoint:
                    result.update({"classification": "observed", "endpoint": endpoint["id"],
                                   "confidence": "observed", "reason": "path observed at runtime"})
            out.append(result)
