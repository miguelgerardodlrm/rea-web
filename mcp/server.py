#!/usr/bin/env python3
"""Thin MCP stdio wrapper over the rea-web CLI. No business logic lives here."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "2024-11-05"
MAX_TEXT = 400_000

TOOLS = [
    {
        "name": "map_site",
        "description": "Map a URL (website or OpenAPI) into client.py, openapi.json, graph.json and evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "out": {"type": "string", "description": "Output directory (default out/mcp-last)"},
                "mode": {"type": "string", "enum": ["auto", "spec", "web"]},
                "crawl": {"type": "integer", "minimum": 1, "description": "Same-site pages to visit"},
                "wait": {"type": "integer", "description": "Milliseconds to wait after load"},
                "full_evidence": {"type": "boolean"},
                "smoke": {"type": "boolean", "description": "Call safe GET endpoints afterwards"},
                "no_static": {"type": "boolean", "description": "Skip JS bundle analysis"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_endpoints",
        "description": "List endpoints from a previously generated out/ directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outdir": {"type": "string", "description": "Default out"},
                "kind": {"type": "string", "enum": ["http", "graphql", "websocket"]},
            },
        },
    },
    {
        "name": "get_evidence",
        "description": "Resolve an evidence pointer, e.g. evidence/network.json#/transactions/3.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "outdir": {"type": "string", "description": "Default out"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "get_client",
        "description": "Return generated client.py and openapi.json.",
        "inputSchema": {
            "type": "object",
            "properties": {"outdir": {"type": "string", "description": "Default out"}},
        },
    },
    {
        "name": "get_graph",
        "description": "Return graph.json (nodes, value edges, triggers, orphans).",
        "inputSchema": {
            "type": "object",
            "properties": {"outdir": {"type": "string", "description": "Default out"}},
        },
    },
]


def _send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(request_id, code, message):
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _read_message():
    line = sys.stdin.readline()
    if not line:
        return None
    if line.lower().startswith("content-length:"):
        length = int(line.split(":", 1)[1].strip())
        while True:
            header = sys.stdin.readline()
            if not header or not header.strip():
                break
        return json.loads(sys.stdin.read(length))
    return json.loads(line)


def _text(value):
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n… (truncated)"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _fail(message):
    return {"content": [{"type": "text", "text": str(message)}], "isError": True}


def _base(arguments):
    outdir = arguments.get("outdir") or "out"
    base = Path(outdir).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    return base.resolve()


def _inside(base, path):
    try:
        path.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def _run_cli(arguments):
    out = arguments.get("out") or "out/mcp-last"
    command = [sys.executable, "-m", "reaweb", "map", arguments["url"], "-o", out, "--json"]
    if arguments.get("mode"):
        command += ["--mode", arguments["mode"]]
    if arguments.get("crawl"):
        command += ["--crawl", str(arguments["crawl"])]
    if arguments.get("wait"):
        command += ["--wait", str(arguments["wait"])]
    if arguments.get("full_evidence"):
        command.append("--full-evidence")
    if arguments.get("smoke"):
        command.append("--smoke")
    if arguments.get("no_static"):
        command.append("--no-static")
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    summary = json.loads(result.stdout)
    out_dir = Path(summary["outdir"]).resolve()
    return {
        "summary": summary,
        "outdir": str(out_dir),
        "map": json.loads((out_dir / "map.json").read_text(encoding="utf-8")),
    }


def _tool(name, arguments):
    if name == "map_site":
        if not arguments.get("url"):
            raise ValueError("url is required")
        return _run_cli(arguments)
    if name == "list_endpoints":
        base = _base(arguments)
        mapdoc = json.loads((base / "map.json").read_text(encoding="utf-8"))
        endpoints = []
        for endpoint in mapdoc.get("endpoints", []):
            if arguments.get("kind") and endpoint.get("kind") != arguments["kind"]:
                continue
            endpoints.append({
                "id": endpoint["id"],
                "kind": endpoint.get("kind"),
                "method": endpoint["method"],
                "path": endpoint["path"],
                "auth": endpoint.get("auth"),
                "evidence": [item["ref"] for item in endpoint.get("evidence", [])],
            })
        return {"outdir": str(base), "count": len(endpoints), "endpoints": endpoints}
    if name == "get_evidence":
        ref = arguments.get("ref", "")
        path_part, _, pointer = ref.partition("#")
        base = _base(arguments)
        target = (base / path_part).resolve()
        if not _inside(base, target):
            raise ValueError("ref escapes outdir")
        data = json.loads(target.read_text(encoding="utf-8"))
        if pointer:
            for part in pointer.strip("/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                data = data[int(part)] if isinstance(data, list) else data[part]
        return {"ref": ref, "data": data}
    if name == "get_client":
        base = _base(arguments)
        result = {"client": (base / "client.py").read_text(encoding="utf-8")}
        openapi = base / "openapi.json"
        if openapi.exists():
            result["openapi"] = json.loads(openapi.read_text(encoding="utf-8"))
        return result
    if name == "get_graph":
        base = _base(arguments)
        return json.loads((base / "graph.json").read_text(encoding="utf-8"))
    raise ValueError(f"unknown tool: {name}")


def main():
    while True:
        try:
            message = _read_message()
        except Exception as exc:
            _error(None, -32700, f"parse error: {exc}")
            continue
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": message.get("params", {}).get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rea-web", "version": "0.1.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                result = _tool(params.get("name"), params.get("arguments") or {})
                _send({"jsonrpc": "2.0", "id": request_id, "result": _text(result)})
            except Exception as exc:
                _send({"jsonrpc": "2.0", "id": request_id, "result": _fail(exc)})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method and method.startswith("notifications/"):
            continue
        else:
            _error(request_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    raise SystemExit(main())
