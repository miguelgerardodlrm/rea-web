"""CLI: map an API/web page into a client, docs and evidence."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from . import capture as capture_mod
from .gen import _args, client_name, write_outputs
from .spec import auth_note, discover_spec, load_spec, normalize_spec
from .util import class_name, host_of, now_iso, registrable


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _is_url(value):
    return bool(re.match(r"^https?://", value))


def cmd_map(args):
    src = args.source
    if not _is_url(src) and not Path(src).exists():
        raise SystemExit(f"not a URL or file: {src}")
    if args.mode in ("auto", "spec"):
        spec = load_spec(src) if not _is_url(src) else discover_spec(src)
        if spec is None and args.mode == "spec":
            raise SystemExit(f"no OpenAPI/Swagger document found at {src}")
    else:
        spec = None

    capture_doc = None
    if spec:
        _log(f"[spec] {spec['source']}")
        norm = normalize_spec(spec["doc"], spec["source"])
        endpoints = norm["endpoints"]
        meta = {
            "source": spec["source"],
            "mode": "spec",
            "title": norm["title"],
            "version": norm.get("version"),
            "description": norm.get("description"),
            "base_url": norm["base_url"],
            "auth": auth_note(norm["auth"]),
            "limitations": ["Behavior beyond the declared OpenAPI document is not verified."],
        }
        if not endpoints:
            raise SystemExit("the spec declares no endpoints")
    else:
        if not _is_url(src):
            raise SystemExit("web mode needs an http(s) URL")
        _log(f"[capture] {src} (chromium)")
        capture_doc = capture_mod.capture(
            src,
            timeout_ms=args.timeout * 1000,
            wait_ms=args.wait,
            scroll=not args.no_scroll,
            include_external=args.include_external,
            max_pages=args.crawl,
            scroll_passes=args.scroll_passes,
            full_evidence=args.full_evidence,
            clicks=args.click or (),
        )
        endpoints, primary = capture_mod.infer_endpoints(capture_doc, args.include_external)
        if not endpoints:
            raise SystemExit(
                "no XHR/fetch traffic captured. The page may be server-rendered or block headless browsers."
            )
        auths = sorted({t["auth"] for t in capture_doc["transactions"] if t.get("auth")})
        meta = {
            "source": src,
            "mode": "web",
            "title": (capture_doc.get("title") or primary or "API")[:80],
            "version": None,
            "description": f"Endpoints observed while loading {capture_doc.get('final_url') or src}.",
            "base_url": next((e["base_url"] for e in endpoints if not e.get("external")), ""),
            "auth": "Bearer token observed" if "bearer" in auths else (", ".join(auths) or ""),
            "limitations": [
                "Only traffic from loading/scrolling the page was observed; writes and interactions are missing.",
                "Schemas are inferred from samples and may omit optional fields.",
            ],
        }
        _log(f"[capture] {len(capture_doc.get('pages', []))} page(s), "
             f"{len(capture_doc['transactions'])} transactions -> {len(endpoints)} endpoints")

    static = None
    if capture_doc and not args.no_static:
        scripts = sorted({script for page in (capture_doc.get("site") or {}).get("pages", [])
                          for script in page.get("scripts", []) if isinstance(script, str)})
        if scripts:
            from .static import analyze_bundles

            _log(f"[static] {len(scripts)} script(s)")
            static = analyze_bundles(scripts, endpoints,
                                     registrable(host_of(capture_doc.get("final_url") or src)))
    meta["generated_at"] = now_iso()
    _log(f"[generate] {len([e for e in endpoints if not e.get('external')])} endpoints -> {args.out}")
    summary = write_outputs(args.out, endpoints, meta, spec=spec, capture_doc=capture_doc, static=static)

    smoke = None
    if args.smoke:
        smoke = run_smoke(Path(args.out), token=args.token, base_url=None)
        summary["smoke"] = smoke
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"{summary['title']} · {summary['mode']} · {summary['endpoints']} endpoints")
        print(f"  client   {Path(summary['outdir']) / 'client.py'}")
        print(f"  docs     {Path(summary['outdir']) / 'README.md'}")
        print(f"  map      {Path(summary['outdir']) / 'map.json'}")
        if smoke:
            for row in smoke["results"]:
                mark = "ok " if row["ok"] else "err"
                print(f"  smoke {mark} {row['endpoint']} {row.get('detail', '')}")
    return 0


def _load_client(outdir):
    path = outdir / "client.py"
    module_spec = importlib.util.spec_from_file_location("reaweb_generated_client", path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"cannot import {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def run_smoke(outdir, token=None, base_url=None):
    """Call generated GET methods that need no required params, against the live API."""
    mapdoc = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    module = _load_client(outdir)
    client_cls = getattr(module, client_name(mapdoc) + "Client")
    client = client_cls(
        base_url=base_url or mapdoc.get("base_url") or None,
        token=token or os.environ.get("REAWEB_TOKEN"),
    )
    results, skipped = [], 0
    for endpoint in mapdoc.get("endpoints", []):
        if endpoint.get("external") or endpoint["method"] != "GET":
            continue
        if any(p["in"] == "path" and p["required"] for p in endpoint["params"]):
            continue
        if endpoint.get("body") and endpoint["body"].get("required"):
            continue
        if any(p["required"] and p.get("example") is None for p in endpoint["params"]):
            skipped += 1
            continue
        _, kwargs, body_arg = _args(endpoint)
        call = {arg: p["example"] for p, arg in kwargs if p.get("example") is not None}
        if body_arg:
            continue
        try:
            data = getattr(client, endpoint["id"])(**call)
            size = len(json.dumps(data, default=str))
            results.append({"endpoint": f"{endpoint['method']} {endpoint['path']}", "ok": True,
                            "detail": f"{size} bytes"})
        except Exception as exc:  # network/auth failures are part of the report, not a crash
            results.append({"endpoint": f"{endpoint['method']} {endpoint['path']}", "ok": False,
                            "detail": str(exc)[:200]})
    return {"base_url": base_url or mapdoc.get("base_url"), "results": results, "skipped": skipped,
            "ok": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


def cmd_smoke(args):
    summary = run_smoke(Path(args.outdir), token=args.token, base_url=args.base_url)
    for row in summary["results"]:
        print(("ok  " if row["ok"] else "err ") + row["endpoint"] + "  " + row.get("detail", ""))
    print(f"{summary['ok']} ok, {summary['failed']} failed")
    return 0 if summary["ok"] else 1


def build_parser():
    parser = argparse.ArgumentParser(prog="rea-web", description="Map an API or website into a client + docs + evidence.")
    sub = parser.add_subparsers(dest="command", required=True)

    mapping = sub.add_parser("map", help="discover a spec or capture a site, then generate outputs")
    mapping.add_argument("source", help="URL or local OpenAPI/Swagger file")
    mapping.add_argument("-o", "--out", default="out")
    mapping.add_argument("--mode", choices=["auto", "spec", "web"], default="auto")
    mapping.add_argument("--timeout", type=int, default=60, help="browser capture budget in seconds")
    mapping.add_argument("--wait", type=int, default=4000, help="ms to wait after load")
    mapping.add_argument("--no-scroll", action="store_true")
    mapping.add_argument("--crawl", type=int, default=1, help="same-site pages to visit (1 = only the given URL)")
    mapping.add_argument("--scroll-passes", type=int, default=3, help="infinite-scroll passes per page")
    mapping.add_argument("--full-evidence", action="store_true",
                         help="keep full captured bodies in evidence (default: samples of 3 array items)")
    mapping.add_argument("--no-static", action="store_true", help="skip JS bundle static analysis")
    mapping.add_argument("--click", action="append", default=[], metavar="SELECTOR",
                         help="click this selector after each page load (repeatable; may trigger writes)")
    mapping.add_argument("--include-external", action="store_true", help="keep third-party hosts in map.json")
    mapping.add_argument("--smoke", action="store_true", help="call safe GET endpoints after generating")
    mapping.add_argument("--token", help="bearer token used by --smoke")
    mapping.add_argument("--json", action="store_true")
    mapping.set_defaults(func=cmd_map)

    smoking = sub.add_parser("smoke", help="exercise an already generated client")
    smoking.add_argument("outdir", nargs="?", default="out")
    smoking.add_argument("--token")
    smoking.add_argument("--base-url")
    smoking.set_defaults(func=cmd_smoke)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        _log(f"error: {exc}")
        return 1
