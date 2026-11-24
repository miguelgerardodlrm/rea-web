"""Browser runtime mapping: pages, forms/flows, rendering, XHR/fetch, GraphQL, WebSockets."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse

from .schema import infer, merge
from .util import host_of, now_iso, registrable, sanitize_ident, trim_json, UA

NUM_RE = re.compile(r"^\d+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
NOISE_RE = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|fundingchoices|adtrafficquality|"
    r"sentry\.io|segment\.(io|com)|hotjar|facebook\.(com|net)|onetrust|cookielaw|"
    r"o11y|opentelemetry|/melidata/|/tracks|/rum\b|ping\.gif|/collect\?)",
    re.I,
)
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-auth-token"}
SENSITIVE_KEYS = re.compile(r"(password|passwd|secret|token|api[-_]?key|signature|credential|session)", re.I)
SKIP_HEADERS = {"content-length", "host", "connection", "accept-encoding", "user-agent",
                ":authority", ":method", ":path", ":scheme"}
PAGINATION_PARAMS = {"page", "offset", "limit", "cursor", "after", "before", "per_page",
                     "per-page", "page_size", "page-size", "pagesize", "skip", "start", "next", "top"}
PAGINATION_FIELDS = {"next", "nextpage", "next_page", "nextcursor", "next_cursor", "hasmore",
                     "has_more", "total", "totalcount", "total_count", "count", "cursor", "offset", "page"}
CART_RE = re.compile(r"(cart|carrito|basket|bag|checkout|comprar)", re.I)
SEARCH_NAMES = {"q", "query", "s", "search", "as_word", "keyword", "term", "buscar"}
GRAPHQL_HINT = re.compile(r"(/graphql\b|/gql\b|/api/graphql\b)", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")
PROV_KEY = os.urandom(16)

EVENT_SCRIPT = """(() => {
  if (window.__reawebEvents) return;
  window.__reawebEvents = [];
  const selector = (el) => {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + el.id;
    if (el.getAttribute && el.getAttribute('data-testid'))
      return '[data-testid="' + el.getAttribute('data-testid') + '"]';
    let part = el.tagName.toLowerCase();
    if (el.getAttribute && el.getAttribute('name'))
      part += '[name="' + el.getAttribute('name') + '"]';
    if (el.parentElement) {
      const same = Array.from(el.parentElement.children).filter(c => c.tagName === el.tagName);
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
    }
    return part;
  };
  const push = (type, target) => {
    if (window.__reawebEvents.length >= 200) return;
    const el = target && target.nodeType === 1 ? target : null;
    window.__reawebEvents.push({
      type: type,
      selector: el ? selector(el) : '',
      text: (el && (el.innerText || el.value || el.getAttribute('aria-label') || '') || '').trim().slice(0, 80),
      tag: el ? el.tagName.toLowerCase() : '',
      href: el && el.getAttribute ? (el.getAttribute('href') || '') : '',
      t: Date.now(),
      page: location.href
    });
  };
  document.addEventListener('click', (e) => push('click', e.target), true);
  document.addEventListener('submit', (e) => push('submit', e.target), true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter') push('keydown', e.target); }, true);
})();"""

CRAWL_SKIP = re.compile(
    r"(logout|log-out|signout|sign-out|delete|remove|unsubscribe|checkout|/cart|/admin|wp-admin)",
    re.I,
)
SKIP_EXT = {"pdf", "zip", "jpg", "jpeg", "png", "gif", "svg", "css", "js", "ico", "xml", "txt",
            "mp4", "mp3", "webp", "woff", "woff2"}

PAGE_PROBE = """() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  const fieldInfo = (i) => ({
    name: i.name || '',
    type: (i.type || i.tagName || '').toLowerCase(),
    required: !!i.required,
    placeholder: i.placeholder || '',
    selector: i.id ? ('#' + i.id) : (i.name ? (i.tagName.toLowerCase() + '[name="' + i.name + '"]') : '')
  });
  const fields = q('input,select,textarea').map(fieldInfo).slice(0, 40);
  const forms = q('form').map((f) => ({
    action: f.action || '',
    method: (f.method || 'get').toLowerCase(),
    fields: Array.from(f.querySelectorAll('input,select,textarea')).map(fieldInfo).slice(0, 20)
  })).slice(0, 20);
  const buttons = q('button,[role="button"],input[type="submit"]')
    .map((b) => (b.innerText || b.value || '').trim()).filter(Boolean).slice(0, 30);
  const scripts = q('script[src]').map((s) => s.src).slice(0, 30);
  const html = document.documentElement.outerHTML;
  const markers = [];
  if (window.__NEXT_DATA__ || html.includes('__NEXT_DATA__')) markers.push('next.js');
  if (window.__NUXT__ || html.includes('__NUXT__')) markers.push('nuxt');
  if (document.querySelector('[ng-version]')) markers.push('angular');
  if (document.querySelector('[data-reactroot]') || window.__APOLLO_STATE__) markers.push('react');
  if (html.includes('data-v-')) markers.push('vue');
  if (window.__SVELTE__) markers.push('svelte');
  if (document.querySelector('astro-island')) markers.push('astro');
  const root = document.querySelector('#root, #app, #__next, #__nuxt');
  return {
    url: location.href,
    title: document.title,
    html_bytes: html.length,
    text_bytes: (document.body ? document.body.innerText : '').length,
    links: q('a[href]').length,
    forms: forms,
    fields: fields,
    buttons: buttons,
    scripts: scripts,
    markers: markers,
    structure: {
      nav: q('nav').length, header: q('header').length, footer: q('footer').length,
      main: q('main').length, forms: q('form').length, tables: q('table').length,
      dialogs: q('[role="dialog"],dialog').length, buttons: q('button,[role="button"]').length,
      inputs: q('input,select,textarea').length, images: q('img').length
    },
    root_empty: !!root && !root.innerText.trim(),
    nav_links: q('a[href]').map((a) => ({
      text: (a.innerText || '').trim().slice(0, 60), href: a.href
    })).slice(0, 100)
  };
}"""


def _redact_json(value, depth=0):
    if depth > 8:
        return value
    if isinstance(value, dict):
        return {k: ("<redacted>" if SENSITIVE_KEYS.search(k) else _redact_json(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v, depth + 1) for v in value[:5]]
    return value


def _redact_url(url):
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    pairs = [(k, "<redacted>" if SENSITIVE_KEYS.search(k) else v) for k, v in pairs]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(pairs)))


def _jwt_expiry(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = data.get("exp")
    if isinstance(exp, int):
        return {"exp": exp, "ttl_seconds": max(0, exp - int(time.time()))}
    return None


def _auth_info(headers):
    raw = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if not raw:
        return None, None
    scheme, _, token = raw.partition(" ")
    detail = {"scheme": scheme.lower()}
    if scheme.lower() == "bearer" and token:
        expiry = _jwt_expiry(token)
        if expiry:
            detail.update(expiry)
    return scheme.lower(), detail


def _request_headers(headers):
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if low in SKIP_HEADERS:
            continue
        out[key] = "<redacted>" if low in SENSITIVE_HEADERS else str(value)[:300]
    return out


def _response_headers(headers):
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if low in ("set-cookie", "cookie"):
            out[key] = "<redacted>"
        elif (low in ("content-type", "cache-control", "etag", "location", "retry-after")
              or low.startswith(("x-ratelimit", "ratelimit", "x-rate"))):
            out[key] = str(value)[:300]
    return out


def _graphql(post, url):
    payload = None
    if post:
        try:
            payload = json.loads(post)
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("query", [None])[0]
        payload = {"query": query} if query else None
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        return None
    query = payload["query"]
    match = re.search(r"\b(query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)", query)
    kind = match.group(1) if match else "query"
    name = match.group(2) if match else (payload.get("operationName") or "anonymous")
    return {
        "type": kind,
        "operation": name,
        "variables": _redact_json(payload.get("variables") or {}),
        "query": query[:8000],
    }


def _prov_scalar(value):
    """Hash a scalar worthy of dependency tracking; short/common values are ignored."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        if abs(value) < 1000:
            return None
        text, kind = str(value), "number"
    elif isinstance(value, float):
        return None
    elif isinstance(value, str):
        text = value.strip()
        if len(text) < 8 or len(text) > 500:
            return None
        if text.lower() in ("application/json", "text/html", "true", "false", "null", "undefined"):
            return None
        if DATE_RE.match(text):
            return None
        kind = "string"
    else:
        return None
    digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8, key=PROV_KEY).hexdigest()
    return {"hash": digest, "type": kind, "len": len(text)}


def _prov_values(value, prefix="$", out=None, depth=0):
    if out is None:
        out = []
    if len(out) >= 120 or depth > 8:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            _prov_values(item, f"{prefix}.{key}", out, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value[:10]):
            _prov_values(item, f"{prefix}[{index}]", out, depth + 1)
    else:
        info = _prov_scalar(value)
        if info:
            out.append({"path": prefix, **info})
    return out


def _header_values(headers):
    out = []
    for key, value in headers.items():
        low = key.lower()
        if low == "authorization":
            info = _prov_scalar(value.partition(" ")[2] or value)
            if info:
                out.append({"path": "header.authorization", **info})
        elif low == "cookie":
            for pair in value.split(";"):
                name, _, cookie = pair.strip().partition("=")
                info = _prov_scalar(cookie)
                if info:
                    out.append({"path": f"cookie.{name}", **info})
    return out


def _safe_body(response, max_bytes=1_500_000):
    ctype = ""
    try:
        ctype = response.headers.get("content-type", "")
    except Exception:
        pass
    try:
        raw = response.body()
    except Exception:
        return ctype, 0, None
    if len(raw) > max_bytes:
        return ctype, len(raw), None
    if "json" in ctype or raw[:1] in (b"{", b"["):
        try:
            return ctype, len(raw), json.loads(raw)
        except ValueError:
            return ctype, len(raw), None
    if "text" in ctype or "xml" in ctype or "javascript" in ctype:
        return ctype, len(raw), raw.decode("utf-8", "replace")[:4000]
    return ctype, len(raw), None


def _cache_bodies(responses, body_limit, max_items, max_depth, max_str, cache):
    """Read/trim response bodies while the page that produced them is still open."""
    for _, _, response in responses:
        if id(response) in cache:
            continue
        ctype, size, body = _safe_body(response, body_limit)
        provenance = _prov_values(body) if body is not None else []
        if body is not None:
            body = trim_json(_redact_json(body), max_items=max_items,
                             max_depth=max_depth, max_str=max_str)
        cache[id(response)] = (ctype, size, body, provenance)


def _crawlable(href, base_domain):
    parts = urllib.parse.urlsplit(href)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.lower()
    if not (host == base_domain or host.endswith("." + base_domain) or registrable(host) == base_domain):
        return False
    if CRAWL_SKIP.search(parts.path or "/"):
        return False
    name = (parts.path or "/").rsplit("/", 1)[-1]
    return "." not in name or name.rsplit(".", 1)[-1].lower() not in SKIP_EXT


def _is_search_field(field):
    return (field.get("type") == "search"
            or field.get("name", "").lower() in SEARCH_NAMES
            or "search" in field.get("placeholder", "").lower()
            or "buscar" in field.get("placeholder", "").lower())


def _site_flows(pages):
    """Heuristic flows (login/search/cart/form) with page-level evidence."""
    flows = []
    for index, page in enumerate(pages):
        ref = f"evidence/site.json#/pages/{index}"
        for form in page.get("forms", []):
            kinds = []
            if any(f.get("type") == "password" for f in form.get("fields", [])):
                kinds.append("login")
            if any(_is_search_field(f) for f in form.get("fields", [])):
                kinds.append("search")
            for kind in kinds or ["form"]:
                flows.append({
                    "kind": kind,
                    "page": page["url"],
                    "action": form.get("action") or page["url"],
                    "method": form.get("method") or "get",
                    "fields": [f.get("name") for f in form.get("fields", []) if f.get("name")],
                    "evidence": ref,
                })
        if not any(_is_search_field(f) for form in page.get("forms", []) for f in form.get("fields", [])):
            for field in page.get("fields", []):
                if _is_search_field(field):
                    selector = field.get("selector") or (
                        f'input[type="{field.get("type")}"]' if field.get("type") in ("search", "text")
                        else "input")
                    flows.append({
                        "kind": "search", "page": page["url"],
                        "selector": selector,
                        "fields": [field.get("name")], "evidence": ref,
                    })
                    break
        for link in page.get("nav_links", []):
            if CART_RE.search(link.get("text", "") + " " + link.get("href", "")):
                flows.append({"kind": "cart", "page": page["url"], "selector": link["href"],
                              "fields": [], "evidence": ref})
                break
    seen, unique = set(), []
    for flow in flows:
        key = (flow["kind"], flow["page"], flow.get("action") or flow.get("selector"))
        if key not in seen:
            seen.add(key)
            unique.append(flow)
    return unique


def _rendering(signals):
    raw, dom, xhr = signals["server_text_bytes"], signals["dom_text_bytes"], signals["xhr_count"]
    markers = signals["framework"]
    if raw < 200 and dom >= 200:
        return "spa"
    if not xhr:
        return "static"
    if raw >= 200:
        return "ssr" if markers else "hybrid"
    return "spa"


def _text_len(html):
    stripped = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S | re.I)
    return len(re.sub(r"\s+", " ", stripped).strip())


def _markers_from_html(html):
    markers = []
    for marker, name in (("__NEXT_DATA__", "next.js"), ("__NUXT__", "nuxt"),
                         ("ng-version", "angular"), ("data-reactroot", "react"),
                         ("data-v-", "vue"), ("astro-island", "astro")):
        if marker in html:
            markers.append(name)
    return markers


def _nojs_signals(browser, url):
    """Server-rendered HTML with JavaScript disabled, to separate SSR/static from SPA."""
    try:
        context = browser.new_context(user_agent=UA, java_script_enabled=False, ignore_https_errors=True)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        html = page.content()
        context.close()
        return _text_len(html), _markers_from_html(html)
    except Exception:
        return 0, []


def capture(url, timeout_ms=45000, wait_ms=4000, scroll=True, include_external=False,
            max_pages=1, scroll_passes=3, full_evidence=False, clicks=()):
    """Load pages in Chromium and record pages, forms, XHR/fetch, GraphQL and WebSocket traffic."""
    from playwright.sync_api import sync_playwright

    responses, sockets, page_infos, pages_seen, events = [], [], [], [], []
    body_cache = {}
    nav_error = None
    final_url = url
    title = ""
    base_domain = registrable(host_of(url))
    body_limit = 8_000_000 if full_evidence else 1_500_000
    max_items = 100_000 if full_evidence else 3
    max_depth = 30 if full_evidence else 8
    max_str = 2_000_000 if full_evidence else 2000
    server_text_bytes, server_markers = 0, []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        server_text_bytes, server_markers = _nojs_signals(browser, url)
        context = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, ignore_https_errors=True)
        context.add_init_script(EVENT_SCRIPT)
        page = context.new_page()
        current = [url]

        def on_ws(ws):
            entry = {"url": _redact_url(ws.url), "page": current[0], "sent": [], "received": []}
            for bucket in ("sent", "received"):
                def handler(payload, bucket=bucket):
                    if len(entry[bucket]) >= 20:
                        return
                    text = payload if isinstance(payload, str) else f"<{len(payload)} bytes>"
                    entry[bucket].append(text[:4000])
                ws.on("framesent" if bucket == "sent" else "framereceived", handler)
            sockets.append(entry)

        page.on("response", lambda r: responses.append((current[0], int(time.time() * 1000), r))
                if r.request.resource_type in ("xhr", "fetch") else None)
        page.on("websocket", on_ws)
        queue = [url]
        deadline = time.monotonic() + max(5, timeout_ms / 1000)
        while queue and len(pages_seen) < max_pages and time.monotonic() < deadline:
            target = queue.pop(0).split("#")[0]
            if target in pages_seen:
                continue
            remaining_ms = int(max(1000, (deadline - time.monotonic()) * 1000))
            current[0] = target
            nav_ts = int(time.time() * 1000)
            page_label = target
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=min(remaining_ms, 25000))
                page.wait_for_timeout(wait_ms)
                if scroll:
                    for _ in range(scroll_passes):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(800)
                page.wait_for_timeout(500)
                _cache_bodies(responses, body_limit, max_items, max_depth, max_str, body_cache)
                final_url = page.url
                title = title or page.title()
                pages_seen.append(target)
                try:
                    info = page.evaluate(PAGE_PROBE)
                    info["requested_url"] = target
                    page_infos.append(info)
                    page_label = info.get("url") or target
                except Exception:
                    pass
                try:
                    for event in page.evaluate("window.__reawebEvents || []"):
                        event["_index"] = len(events)
                        events.append(event)
                    page.evaluate("window.__reawebEvents = []")
                except Exception:
                    pass
                for selector in clicks:
                    try:
                        page.locator(selector).first.click(timeout=2000)
                        page.wait_for_timeout(900)
                    except Exception:
                        continue
                if clicks:
                    try:
                        for event in page.evaluate("window.__reawebEvents || []"):
                            event["_index"] = len(events)
                            events.append(event)
                        page.evaluate("window.__reawebEvents = []")
                    except Exception:
                        pass
                    _cache_bodies(responses, body_limit, max_items, max_depth, max_str, body_cache)
                if len(pages_seen) < max_pages:
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    for href in hrefs:
                        href = href.split("#")[0]
                        if href not in pages_seen and href not in queue and _crawlable(href, base_domain):
                            queue.append(href)
            except Exception as exc:
                nav_error = str(exc)[:500]
                pages_seen.append(target)
            events.append({"type": "navigation", "page": page_label, "selector": "", "text": "",
                           "t": nav_ts, "_index": len(events)})
        _cache_bodies(responses, body_limit, max_items, max_depth, max_str, body_cache)
        txns = _build_transactions(responses, base_domain, include_external, body_cache)
        browser.close()

    for txn in txns:
        page_for = next((info for info in page_infos if info.get("requested_url") == txn["page"]), None)
        txn["page"] = page_for["url"] if page_for else txn["page"]
    for info in page_infos:
        info["requests"] = sum(1 for t in txns if t["page"] == info["url"])
    site = {
        "rendering": _rendering({
            "server_text_bytes": server_text_bytes,
            "dom_text_bytes": page_infos[0]["text_bytes"] if page_infos else 0,
            "xhr_count": len(txns),
            "framework": sorted(set((page_infos[0].get("markers", []) if page_infos else []) + server_markers)),
            "root_empty": page_infos[0].get("root_empty") if page_infos else None,
        }),
        "framework": sorted(set((page_infos[0].get("markers", []) if page_infos else []) + server_markers)),
        "signals": {
            "server_text_bytes": server_text_bytes,
            "dom_text_bytes": page_infos[0]["text_bytes"] if page_infos else 0,
            "xhr_count": len(txns),
            "root_empty": page_infos[0].get("root_empty") if page_infos else None,
        },
        "pages": page_infos,
        "flows": _site_flows(page_infos),
    }
    return {
        "page_url": url,
        "final_url": final_url,
        "pages": pages_seen,
        "title": title,
        "nav_error": nav_error,
        "include_external": include_external,
        "full_evidence": full_evidence,
        "site": site,
        "events": events,
        "transactions": txns,
        "websockets": sockets,
        "captured_at": now_iso(),
    }


def _build_transactions(responses, base_domain, include_external, body_cache):
    txns, seen_txns = [], set()
    for page_url, ts, response in responses[:400]:
        request = response.request
        req_url = request.url
        if NOISE_RE.search(req_url):
            continue
        host = host_of(req_url)
        external = not (host == base_domain or host.endswith("." + base_domain) or registrable(host) == base_domain)
        if external and not include_external:
            continue
        auth, auth_detail = _auth_info(request.headers)
        post = None
        try:
            post = request.post_data
        except Exception:
            pass
        request_body, raw_body = None, None
        if post:
            try:
                raw_body = json.loads(post)
            except ValueError:
                pairs = urllib.parse.parse_qsl(post, keep_blank_values=True)
                raw_body = dict(pairs) if pairs else None
            if raw_body is not None:
                request_body = _redact_json(raw_body)
        redacted = _redact_url(req_url)
        key = (request.method, redacted, response.status, str(request_body)[:300])
        if key in seen_txns:
            continue
        seen_txns.add(key)
        cached = body_cache.get(id(response))
        if cached:
            ctype, size, body, response_prov = cached
        else:
            ctype, size, body = _safe_body(response)
            response_prov = _prov_values(body) if body is not None else []
        request_prov = _prov_values(dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(req_url).query, keep_blank_values=True)), prefix="query")
        if raw_body is not None:
            request_prov += _prov_values(raw_body, prefix="body")
        request_prov += _header_values(request.headers)
        for index, segment in enumerate(urllib.parse.urlsplit(req_url).path.split("/")):
            info = _prov_scalar(segment)
            if info:
                request_prov.append({"path": f"path.{index}", **info})
        gql = _graphql(post, req_url) if (request_body is not None or GRAPHQL_HINT.search(req_url)) else None
        txns.append({
            "seq": len(txns),
            "ts": ts,
            "method": request.method,
            "url": redacted,
            "host": host,
            "page": page_url,
            "status": response.status,
            "resource_type": request.resource_type,
            "request_headers": _request_headers(request.headers),
            "response_headers": _response_headers(response.headers),
            "request_body": trim_json(request_body) if request_body is not None else None,
            "response_content_type": ctype,
            "response_bytes": size,
            "response_body": body,
            "response_omitted": f"body > {size} bytes, omitted" if body is None and size else None,
            "auth": auth,
            "auth_detail": auth_detail,
            "provenance": {"request": request_prov[:120], "response": response_prov[:120]},
            "graphql": gql,
            "external": external,
            "at": now_iso(),
        })
    return txns


def _classify(segment):
    if NUM_RE.match(segment):
        return "id", "integer"
    if UUID_RE.match(segment):
        return "uuid", "string"
    if HEX_RE.match(segment):
        return "hash", "string"
    return None, None


def normalize_path(path):
    template, params, seen = [], [], set()
    for segment in path.split("/"):
        kind, ptype = _classify(segment)
        if not kind:
            template.append(segment)
            continue
        name, n = kind, 2
        while name in seen:
            name, n = f"{kind}{n}", n + 1
        seen.add(name)
        params.append({
            "name": name,
            "arg": name,
            "in": "path",
            "type": ptype,
            "required": True,
            "description": "",
            "example": segment,
            "enum": None,
        })
        template.append("{" + name + "}")
    return "/".join(template) or "/", params


def _scalar(value):
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value or ""):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value or ""):
        return float(value)
    return value


def _query_params(urls):
    found = {}
    for url in urls:
        for key, value in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True):
            found.setdefault(key, []).append(_scalar(value))
    params = []
    for key, values in found.items():
        schema = None
        for value in values:
            schema = merge(schema, infer(value))
        example = values[0]
        if example == "<redacted>":
            example = None
        params.append({
            "name": key,
            "arg": sanitize_ident(key, "param"),
            "in": "query",
            "type": (schema or {}).get("type", "string"),
            "required": False,
            "description": "",
            "example": example,
            "enum": None,
        })
    return params


def _merge_bodies(txns):
    schema = None
    for txn in txns:
        if txn.get("request_body"):
            schema = merge(schema, infer(txn["request_body"]))
    return schema


def _merge_responses(txns):
    schema = None
    for txn in txns:
        if txn["status"] < 400 and "json" in (txn.get("response_content_type") or "") and txn.get("response_body") is not None:
            schema = merge(schema, infer(txn["response_body"]))
    return schema


def _error_schemas(txns):
    by_status = {}
    for txn in txns:
        if txn["status"] >= 400 and txn.get("response_body") is not None:
            status = str(txn["status"])
            by_status[status] = merge(by_status.get(status), infer(txn["response_body"]))
    return [{"status": status, "schema": schema} for status, schema in sorted(by_status.items())]


def _pagination(params, schema):
    query = sorted({p["name"] for p in params if p["name"].lower() in PAGINATION_PARAMS})
    fields = sorted(k for k in (schema or {}).get("fields", {}) if k.lower() in PAGINATION_FIELDS)
    return {"params": query, "response_fields": fields} if query or fields else None


def _rate_limits(txns):
    limits = {}
    for txn in txns:
        for key, value in (txn.get("response_headers") or {}).items():
            if re.search(r"(ratelimit|rate-limit|retry-after)", key, re.I):
                limits[key] = value
    return limits or None


def _observed_headers(txns):
    names = sorted({k.lower() for txn in txns for k in (txn.get("request_headers") or {})})
    return [name for name in names if name not in {"accept", "content-type", "origin", "referer",
                                                  "x-requested-with", "accept-language"}]


def _infer_websockets(sockets, base_domain, include_external=False):
    groups = {}
    for index, socket in enumerate(sockets):
        host = host_of(socket["url"])
        if not include_external and host != base_domain and not host.endswith("." + base_domain) \
                and registrable(host) != base_domain:
            continue
        path = urllib.parse.urlsplit(socket["url"]).path or "/"
        template, params = normalize_path(path)
        key = (host, template)
        group = groups.setdefault(key, {"indices": [], "sent": None, "received": None,
                                        "frames": {"sent": 0, "received": 0},
                                        "url": socket["url"], "page": socket["page"], "params": params})
        group["frames"]["sent"] += len(socket.get("sent", []))
        group["frames"]["received"] += len(socket.get("received", []))
        for bucket in ("sent", "received"):
            for frame in socket.get(bucket, []):
                try:
                    payload = json.loads(frame)
                except ValueError:
                    continue
                group[bucket] = merge(group[bucket], infer(payload))
        group["indices"].append(index)
    endpoints = []
    for (host, template), group in groups.items():
        base_id = sanitize_ident(f"ws_{template}")
        endpoints.append({
            "id": base_id,
            "kind": "websocket",
            "method": "WS",
            "path": template,
            "base_url": f"https://{host}",
            "host": host,
            "summary": "WebSocket",
            "description": "",
            "frames": group["frames"],
            "tags": ["websocket"],
            "params": group["params"],
            "body": {"content_type": "application/json", "schema": group["sent"], "required": False} if group["sent"] else None,
            "response": {"status": None, "content_type": "application/json", "schema": group["received"]} if group["received"] else None,
            "evidence": [{
                "source": "network",
                "url": group["url"],
                "ref": f"evidence/network.json#/websockets/{i}",
                "confidence": "observed",
            } for i in group["indices"]],
            "confidence": "observed",
        })
    return endpoints


def infer_endpoints(cap, include_external=False):
    """Group captured transactions into method+path-template endpoints (HTTP, GraphQL, WebSocket)."""
    groups = {}
    for index, txn in enumerate(cap.get("transactions") or []):
        if not txn.get("status"):
            continue
        if txn.get("external") and not include_external:
            continue
        split = urllib.parse.urlsplit(txn["url"])
        template, path_params = normalize_path(split.path or "/")
        gql = txn.get("graphql")
        key = (txn["host"], txn["method"], template, gql["operation"] if gql else None)
        group = groups.setdefault(key, {
            "txns": [], "indices": [], "path_params": path_params,
            "template": template, "graphql": gql,
        })
        group["txns"].append(txn)
        if len(group["indices"]) < 5:
            group["indices"].append(index)

    endpoints, used = [], set()
    for (host, method, template, gql_op), group in groups.items():
        txns = group["txns"]
        gql = group["graphql"]
        base_id = sanitize_ident(f"graphql_{gql['operation']}" if gql else f"{method}_{template}")
        eid, n = base_id[:80], 2
        while eid in used:
            eid, n = f"{base_id[:77]}_{n}", n + 1
        used.add(eid)
        params = group["path_params"] + _query_params([t["url"] for t in txns])
        response_schema = _merge_responses(txns)
        statuses = sorted({t["status"] for t in txns})
        auths = [t.get("auth") for t in txns if t.get("auth")]
        auth_detail = next((t.get("auth_detail") for t in txns if t.get("auth_detail")), None)
        body_schema = None if gql else _merge_bodies(txns)
        body_ctype = next((t["request_headers"].get("content-type") for t in txns if t.get("request_body")), None)
        endpoints.append({
            "id": eid,
            "kind": "graphql" if gql else "http",
            "method": method,
            "path": template,
            "base_url": f"{urllib.parse.urlsplit(txns[0]['url']).scheme}://{host}",
            "summary": "",
            "description": "",
            "tags": [gql["operation"]] if gql else [(template.strip("/").split("/") or ["root"])[0]],
            "params": params,
            "body": {"content_type": body_ctype or "application/json", "schema": body_schema, "required": False} if body_schema else None,
            "response": {"status": str(statuses[-1]) if statuses else None, "content_type": "application/json",
                         "schema": response_schema} if response_schema else None,
            "graphql": {"type": gql["type"], "operation": gql["operation"], "query": gql["query"],
                        "variables": gql.get("variables")} if gql else None,
            "auth": auths[0] if auths else None,
            "auth_detail": auth_detail,
            "observed": {
                "count": len(txns),
                "first_seq": min(t["seq"] for t in txns),
                "statuses": statuses,
                "pages": sorted({t["page"] for t in txns}),
            },
            "pagination": _pagination(params, response_schema),
            "errors": _error_schemas(txns),
            "rate_limit": _rate_limits(txns),
            "observed_headers": _observed_headers(txns),
            "host": host,
            "evidence": [{
                "source": "network",
                "url": txn["url"],
                "ref": f"evidence/network.json#/transactions/{index}",
                "confidence": "observed",
            } for index, txn in zip(group["indices"], txns)],
            "confidence": "observed",
        })

    endpoints += _infer_websockets(cap.get("websockets") or [],
                                   registrable(host_of(cap.get("final_url") or cap.get("page_url") or "")),
                                   include_external)

    counts = {}
    for endpoint in endpoints:
        counts[endpoint["host"]] = counts.get(endpoint["host"], 0) + 1
    primary = max(counts, key=counts.get) if counts else None
    primary_domain = registrable(primary) if primary else None
    for endpoint in endpoints:
        endpoint["external"] = registrable(endpoint["host"]) != primary_domain
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return endpoints, primary
