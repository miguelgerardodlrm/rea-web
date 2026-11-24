"""Minimal JSON-schema subset: infer from samples, merge, render."""
from __future__ import annotations

ANY = {"type": "any"}


def infer(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        items = None
        for v in value:
            items = merge(items, infer(v))
        return {"type": "array", "items": items or ANY}
    if isinstance(value, dict):
        return {"type": "object", "fields": {k: infer(v) for k, v in value.items()}, "optional": []}
    return ANY


def merge(a, b):
    if a is None:
        return b
    if b is None:
        return a
    ta, tb = a.get("type"), b.get("type")
    if ta == tb:
        if ta == "object":
            fa, fb = a.get("fields", {}), b.get("fields", {})
            fields = {}
            for key in fa.keys() & fb.keys():
                fields[key] = merge(fa[key], fb[key])
            for key in fa.keys() - fb.keys():
                fields[key] = fa[key]
            for key in fb.keys() - fa.keys():
                fields[key] = fb[key]
            optional = set(a.get("optional", [])) | set(b.get("optional", []))
            optional |= fa.keys() ^ fb.keys()
            return {"type": "object", "fields": dict(sorted(fields.items())), "optional": sorted(optional)}
        if ta == "array":
            return {"type": "array", "items": merge(a.get("items"), b.get("items"))}
        return dict(a)
    if {ta, tb} <= {"integer", "number"}:
        return {"type": "number"}
    if "any" in (ta, tb):
        return dict(ANY)
    if "null" in (ta, tb):
        # Nullable sample: keep the informative side rather than polluting the type.
        return dict(b if ta == "null" else a)
    return dict(ANY)


def from_openapi(node, root, depth=0, seen=frozenset()):
    """Convert an OpenAPI/Swagger schema into the subset above. Local $refs only."""
    if not node or node is True or depth > 8:
        return dict(ANY)
    if isinstance(node, list):
        out = None
        for item in node:
            out = merge(out, from_openapi(item, root, depth, seen))
        return out or dict(ANY)
    if not isinstance(node, dict):
        return dict(ANY)
    ref = node.get("$ref")
    if ref and isinstance(ref, str) and ref.startswith("#/"):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {"type": "object", "name": name, "fields": {}, "optional": []}
        target = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part, {}) if isinstance(target, dict) else {}
        out = from_openapi(target, root, depth + 1, seen | {name})
        if out.get("type") == "object":
            out.setdefault("name", name)
        return out
    if "allOf" in node:
        out = None
        for part in node["allOf"]:
            out = merge(out, from_openapi(part, root, depth, seen))
        return out or dict(ANY)
    for key in ("oneOf", "anyOf"):
        if node.get(key):
            out = from_openapi(node[key][0], root, depth, seen)
            if len(node[key]) > 1:
                out["variants"] = len(node[key])
            return out
    kind = node.get("type")
    if isinstance(kind, list):
        node = {**node, "type": next((k for k in kind if k != "null"), "any")}
        kind = node["type"]
    if kind is None:
        kind = "object" if "properties" in node else ("array" if "items" in node else "string")
    if kind == "object":
        fields = {k: from_openapi(v, root, depth + 1, seen) for k, v in (node.get("properties") or {}).items()}
        out = {
            "type": "object",
            "fields": fields,
            "optional": [k for k in fields if k not in (node.get("required") or [])],
        }
        if isinstance(node.get("additionalProperties"), dict):
            out["extra"] = from_openapi(node["additionalProperties"], root, depth + 1, seen)
        return out
    if kind == "array":
        return {"type": "array", "items": from_openapi(node.get("items"), root, depth + 1, seen)}
    out = {"type": kind if kind in ("string", "integer", "number", "boolean") else "any"}
    for key in ("format", "enum", "example", "default"):
        if key in node:
            out[key] = node[key]
    return out


def is_optional(schema, key):
    return key in (schema or {}).get("optional", [])


def fmt(schema, depth=0, max_depth=3):
    """Compact one-line rendering: object {id: integer, tags?: array<string>}."""
    if not schema:
        return "any"
    kind = schema.get("type", "any")
    if depth > max_depth:
        return kind
    if kind == "object":
        fields = schema.get("fields", {})
        shown = list(fields.items())[:12]
        inner = ", ".join(f"{k}{'?' if is_optional(schema, k) else ''}: {fmt(v, depth + 1, max_depth)}" for k, v in shown)
        if len(fields) > len(shown):
            inner += f", … (+{len(fields) - len(shown)})"
        if schema.get("extra"):
            inner += f", *: {fmt(schema['extra'], depth + 1, max_depth)}"
        return "object {" + inner + "}"
    if kind == "array":
        return f"array<{fmt(schema.get('items'), depth + 1, max_depth)}>"
    if schema.get("enum"):
        return f"{kind} enum({', '.join(map(str, schema['enum'][:8]))})"
    return kind


def to_openapi(schema, depth=0):
    """Convert the internal subset to an inline JSON Schema for OpenAPI 3.0."""
    if not schema or depth > 12:
        return {}
    kind = schema.get("type")
    if kind == "object":
        properties = {k: to_openapi(v, depth + 1) for k, v in schema.get("fields", {}).items()}
        out = {"type": "object", "properties": properties}
        required = [k for k in properties if k not in schema.get("optional", [])]
        if required:
            out["required"] = required
        if schema.get("extra"):
            out["additionalProperties"] = to_openapi(schema["extra"], depth + 1)
        return out
    if kind == "array":
        return {"type": "array", "items": to_openapi(schema.get("items"), depth + 1)}
    if kind in (None, "any"):
        return {}
    if kind == "null":
        return {"nullable": True}
    out = {"type": kind if kind in ("string", "integer", "number", "boolean") else "string"}
    for key in ("format", "enum"):
        if schema.get(key):
            out[key] = schema[key]
    return out
