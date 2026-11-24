"""Flow graph: DOM-event triggers and value provenance between requests."""
from __future__ import annotations

import re
import urllib.parse

from .capture import normalize_path
from .util import now_iso

EVENT_WINDOW_MS = 2000
NAV_WINDOW_MS = 8000
MAX_EDGES = 300


def attribute_triggers(txns, events):
    """Attach a trigger to each transaction: a DOM event, page navigation, or orphan."""
    by_page = {}
    for event in events:
        by_page.setdefault(event.get("page"), []).append(event)
    for page_events in by_page.values():
        page_events.sort(key=lambda event: event.get("t") or 0)

    for txn in txns:
        timestamp = txn.get("ts") or 0
        page_events = by_page.get(txn["page"], [])

        def recent(kind_filter, window):
            return [event for event in page_events
                    if kind_filter(event) and (event.get("t") or 0) <= timestamp
                    and timestamp - (event.get("t") or 0) <= window]

        dom_events = recent(lambda event: event.get("type") != "navigation", EVENT_WINDOW_MS)
        if dom_events:
            event = dom_events[-1]
            txn["trigger"] = {
                "kind": event.get("type"),
                "selector": event.get("selector"),
                "text": event.get("text"),
                "confidence": "inferred",
                "reason": f"last DOM event within {EVENT_WINDOW_MS}ms",
                "evidence": f"evidence/events.json#/events/{event['_index']}",
            }
            continue
        navigations = recent(lambda event: event.get("type") == "navigation", NAV_WINDOW_MS)
        if navigations:
            event = navigations[-1]
            txn["trigger"] = {
                "kind": "navigation",
                "selector": "",
                "text": event.get("text") or "",
                "confidence": "inferred",
                "reason": "no DOM event; attributed to page navigation",
                "evidence": f"evidence/events.json#/events/{event['_index']}",
            }
        else:
            txn["trigger"] = {
                "kind": "orphan",
                "selector": "",
                "text": "",
                "confidence": "inferred",
                "reason": "no DOM event or navigation inside the attribution window",
            }


def _endpoint_key(method, path, graphql=None):
    return (method, path, graphql)


def _txn_key(txn):
    path = urllib.parse.urlsplit(txn["url"]).path or "/"
    template, _ = normalize_path(path)
    graphql = (txn.get("graphql") or {}).get("operation")
    return _endpoint_key(txn["method"], template, graphql)


def _value_edges(txns, endpoint_for):
    appearances, request_counts = {}, {}
    for txn in txns:
        for value in (txn.get("provenance") or {}).get("response", []):
            appearances.setdefault(value["hash"], []).append((txn, value))
        for value in (txn.get("provenance") or {}).get("request", []):
            request_counts[value["hash"]] = request_counts.get(value["hash"], 0) + 1

    edges, seen = [], set()
    for txn in txns:
        target = endpoint_for.get(_txn_key(txn))
        if not target:
            continue
        for value in (txn.get("provenance") or {}).get("request", []):
            if request_counts.get(value["hash"], 0) > 5:
                continue
            sources = [(source, source_value) for source, source_value in appearances.get(value["hash"], [])
                       if source["seq"] < txn["seq"]]
            if not sources:
                continue
            source, source_value = min(sources, key=lambda item: item[0]["seq"])
            origin = endpoint_for.get(_txn_key(source))
            if not origin:
                continue
            key = (origin, target, source_value["path"], value["path"])
            if key in seen:
                continue
            seen.add(key)
            field_name = source_value["path"].rsplit(".", 1)[-1].split("[")[0]
            strong = bool(re.search(r"(id|token|key|code|uuid|session|cursor|secret|sku)",
                                    field_name, re.I)) or value["len"] >= 16
            edges.append({
                "from": origin,
                "to": target,
                "from_field": source_value["path"],
                "to_field": value["path"],
                "value_hash": value["hash"],
                "value_type": value["type"],
                "confidence": "strong" if strong else "inferred",
                "evidence": {
                    "source": f"evidence/network.json#/transactions/{source['seq']}",
                    "target": f"evidence/network.json#/transactions/{txn['seq']}",
                    "reason": f"{source_value['path']} -> {value['path']}",
                },
            })
            if len(edges) >= MAX_EDGES:
                return edges
    return edges


def build_graph(endpoints, txns, events):
    """Build nodes (endpoints), trigger attribution and value-provenance edges."""
    attribute_triggers(txns, events)
    endpoint_for = {}
    for endpoint in endpoints:
        if endpoint.get("kind") == "websocket":
            continue
        graphql = (endpoint.get("graphql") or {}).get("operation")
        endpoint_for[_endpoint_key(endpoint["method"], endpoint["path"], graphql)] = endpoint["id"]

    triggers, orphans = {}, []
    for txn in txns:
        endpoint_id = endpoint_for.get(_txn_key(txn))
        if not endpoint_id:
            continue
        trigger = txn.get("trigger") or {}
        if trigger.get("kind") == "orphan":
            orphans.append({
                "endpoint": endpoint_id,
                "method": txn["method"],
                "path": txn["url"],
                "seq": txn["seq"],
                "reason": trigger.get("reason"),
                "evidence": f"evidence/network.json#/transactions/{txn['seq']}",
            })
        key = (endpoint_id, trigger.get("kind"), trigger.get("selector") or "",
               trigger.get("text") or "", trigger.get("confidence"))
        entry = triggers.setdefault(key, {"kind": trigger.get("kind"), "selector": trigger.get("selector"),
                                          "text": trigger.get("text"), "confidence": trigger.get("confidence"),
                                          "count": 0, "evidence": []})
        entry["count"] += 1
        if trigger.get("evidence") and len(entry["evidence"]) < 3:
            entry["evidence"].append(trigger["evidence"])

    nodes = []
    for endpoint in endpoints:
        if endpoint.get("external"):
            continue
        node = {
            "id": endpoint["id"],
            "kind": endpoint.get("kind", "http"),
            "method": endpoint["method"],
            "path": endpoint["path"],
            "host": endpoint.get("host"),
            "auth": endpoint.get("auth"),
            "observed": endpoint.get("observed"),
            "triggers": sorted(
                (value for (node_id, *_), value in triggers.items() if node_id == endpoint["id"]),
                key=lambda item: -item["count"],
            ),
        }
        nodes.append(node)

    edges = _value_edges(txns, endpoint_for)
    return {
        "generated_at": now_iso(),
        "nodes": nodes,
        "edges": edges,
        "orphans": orphans,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "orphans": len(orphans),
            "triggers": sum(item["count"] for item in triggers.values()),
        },
    }
