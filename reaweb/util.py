"""Shared helpers: fetch, identifiers, evidence refs."""
from __future__ import annotations

import json
import keyword
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "rea-web/0.1 (API mapper)"
MAX_FETCH = 12 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url, timeout=20):
    """GET a URL (or read a local file). Returns (bytes, content_type)."""
    if not re.match(r"^https?://", url):
        with open(url, "rb") as fh:
            return fh.read(MAX_FETCH), ""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read(MAX_FETCH), res.headers.get("Content-Type", "")


def parse_body(raw, content_type=""):
    """Parse JSON when possible, else return None."""
    if "json" in content_type or raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def sanitize_ident(name, fallback="value"):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name or ""))
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    name = re.sub(r"_+", "_", name) or fallback
    if name[0].isdigit():
        name = "n_" + name
    if keyword.iskeyword(name):
        name += "_"
    return name


def class_name(title):
    parts = re.findall(r"[0-9A-Za-z]+", title or "Api")
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Api"
    if name[0].isdigit():
        name = "Api" + name
    return name[:60]


def origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def host_of(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def registrable(host):
    labels = [x for x in host.split(":")[0].split(".") if x]
    # shortcut: assumes the common two-label suffix, wrong for co.uk-style domains
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def escape_pointer(path):
    return path.replace("~", "~0").replace("/", "~1")


def trim_json(value, max_items=3, depth=0, max_depth=8, max_str=2000):
    """Shrink captured JSON for evidence files without ever emitting invalid JSON."""
    if depth > max_depth:
        return "…"
    if isinstance(value, dict):
        return {k: trim_json(v, max_items, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        kept = [trim_json(v, max_items, depth + 1, max_depth) for v in value[:max_items]]
        if len(value) > max_items:
            kept.append(f"… (+{len(value) - max_items} items)")
        return kept
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    return value
