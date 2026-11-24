"""End-to-end tests: mock API + browser capture, generated client, smoke, spec discovery."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_server import JWT, make_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "reaweb", *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )


def load_client(outdir):
    source = importlib.util.spec_from_file_location("generated_client", Path(outdir) / "client.py")
    module = importlib.util.module_from_spec(source)
    source.loader.exec_module(module)
    return module


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_spec_generates_working_client_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "spec"
            result = run("map", f"{self.base}/openapi.json", "--mode", "spec", "-o", out, "--smoke", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["mode"], "spec")
            self.assertEqual(summary["endpoints"], 3)
            self.assertGreaterEqual(summary["smoke"]["ok"], 1, summary["smoke"])

            mapdoc = json.loads((out / "map.json").read_text())
            ids = {ep["id"] for ep in mapdoc["endpoints"]}
            self.assertEqual(ids, {"list_items", "create_item", "get_item"})
            get_item = next(ep for ep in mapdoc["endpoints"] if ep["id"] == "get_item")
            self.assertIn("#/paths/~1items~1{itemId}/get", get_item["evidence"][0]["ref"])

            module = load_client(out)
            client = module.MockStoreClient()
            items = client.list_items(limit=2)
            self.assertEqual([item["name"] for item in items], ["alpha", "beta"])
            self.assertEqual(client.get_item(1)["name"], "alpha")
            self.assertEqual(client.create_item(body={"name": "new"})["id"], 99)

            readme = (out / "README.md").read_text()
            self.assertIn("Evidence:", readme)
            self.assertIn("`GET /items/{itemId}`", readme)
            self.assertTrue((out / "evidence" / "spec.json").exists())
            openapi = json.loads((out / "openapi.json").read_text())
            self.assertEqual(openapi["openapi"], "3.0.3")
            self.assertEqual(len(openapi["paths"]), 2)
            self.assertIn("get", openapi["paths"]["/items/{itemId}"])

    def test_web_capture_infers_endpoints_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "web"
            result = run("map", f"{self.base}/app", "--mode", "web", "-o", out, "--wait", "1500")
            self.assertEqual(result.returncode, 0, result.stderr)

            mapdoc = json.loads((out / "map.json").read_text())
            self.assertEqual(mapdoc["mode"], "web")
            paths = {(ep["method"], ep["path"]) for ep in mapdoc["endpoints"]}
            self.assertIn(("GET", "/api/items"), paths)
            self.assertIn(("GET", "/api/items/{id}"), paths)
            self.assertEqual(mapdoc["base_url"], self.base)

            listing = next(ep for ep in mapdoc["endpoints"] if ep["path"] == "/api/items")
            limit = next(p for p in listing["params"] if p["name"] == "limit")
            self.assertEqual((limit["type"], limit["example"]), ("integer", 2))
            self.assertEqual(listing["response"]["schema"]["type"], "array")
            self.assertEqual(listing["response"]["schema"]["items"]["type"], "object")
            self.assertEqual(listing["pagination"]["params"], ["limit", "page"])
            self.assertEqual(listing["auth"], "bearer")
            self.assertGreater(listing["auth_detail"]["ttl_seconds"], 0)

            missing = next(ep for ep in mapdoc["endpoints"] if ep["path"] == "/api/missing")
            self.assertEqual(missing["errors"][0]["status"], "404")
            self.assertIn("error", missing["errors"][0]["schema"]["fields"])

            gql = next(ep for ep in mapdoc["endpoints"] if ep.get("kind") == "graphql")
            self.assertEqual(gql["graphql"]["operation"], "GetItems")
            self.assertEqual(gql["graphql"]["query"][:10], "query GetI")

            site = mapdoc["site"]
            self.assertIn(site["rendering"], {"spa", "ssr", "static", "hybrid"})
            self.assertTrue(any(flow["kind"] == "login" for flow in site["flows"]), site["flows"])

            evidence = (out / "evidence" / "network.json").read_text()
            self.assertNotIn(JWT, evidence)
            self.assertNotIn("analytics.example.test", evidence)

            network = json.loads(evidence)
            product = next(t for t in network["transactions"] if "/api/product" in t["url"])
            self.assertEqual(product["trigger"]["kind"], "click")
            self.assertEqual(product["trigger"]["selector"], "#load-more")
            self.assertTrue((out / "evidence" / "events.json").exists())

            static = json.loads((out / "static.json").read_text())
            by_value = {item["value"]: item for item in static["candidates"]}
            self.assertEqual(by_value["/api/items"]["classification"], "observed")
            self.assertEqual(by_value["/api/secret-unused"]["classification"], "static_only")
            self.assertEqual(by_value["/api/old-comment"]["classification"], "false_positive")
            self.assertEqual(by_value["https://example.com/api/nope"]["classification"], "false_positive")
            self.assertEqual(by_value["/api/from-map"]["classification"], "static_only")
            self.assertIn("{param}", by_value["/api/items/{param}"]["value"])
            self.assertEqual(static["stats"]["bundles"], 1)
            self.assertEqual(static["bundles"][0]["source_map"], f"{self.base}/app.js.map")

            graph = json.loads((out / "graph.json").read_text())
            self.assertGreaterEqual(graph["stats"]["nodes"], 4)
            edge = next(e for e in graph["edges"]
                        if e["from"] == "get_api_items" and e["to"] == "get_api_product")
            self.assertIn("sku", edge["from_field"])
            self.assertEqual(edge["to_field"], "query.sku")
            self.assertEqual(edge["confidence"], "strong")
            self.assertIn("value_hash", edge)

            module = load_client(out)
            client = module.MockAppClient()
            self.assertEqual(len(client.get_api_items(limit=2)), 2)
            self.assertEqual(client.get_api_items_id(42)["id"], 42)
            self.assertEqual(client.graphql_get_items(variables={"limit": 1})["data"]["items"][0]["name"], "alpha")

            openapi = json.loads((out / "openapi.json").read_text())
            self.assertIn("/api/items", openapi["paths"])
            self.assertIn("post", openapi["paths"]["/graphql"])
            self.assertEqual(openapi["paths"]["/graphql"]["post"]["x-graphql-operation"]["name"], "GetItems")

            readme = (out / "README.md").read_text()
            self.assertIn("Evidence:", readme)
            self.assertIn("observed", readme)
            self.assertIn("Site map", readme)
            self.assertIn("GraphQL", readme)

    def test_crawl_reaches_more_pages_and_full_evidence_keeps_all_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "crawl"
            result = run("map", f"{self.base}/app", "--mode", "web", "-o", out,
                         "--crawl", "2", "--wait", "800", "--full-evidence")
            self.assertEqual(result.returncode, 0, result.stderr)
            mapdoc = json.loads((out / "map.json").read_text())
            paths = {(ep["method"], ep["path"]) for ep in mapdoc["endpoints"]}
            self.assertIn(("GET", "/api/reviews"), paths)

            network = json.loads((out / "evidence" / "network.json").read_text())
            self.assertEqual(len(network["pages"]), 2)
            reviews = next(t for t in network["transactions"] if t["url"].endswith("/api/reviews?limit=5"))
            self.assertEqual(len(reviews["response_body"]), 5)

    def test_auto_discovers_spec_from_docs_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "auto"
            result = run("map", f"{self.base}/docs", "-o", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            mapdoc = json.loads((out / "map.json").read_text())
            self.assertEqual(mapdoc["mode"], "spec")
            self.assertEqual(len(mapdoc["endpoints"]), 3)


class SchemaModel(unittest.TestCase):
    def test_merge_marks_missing_keys_optional_and_widens_numbers(self):
        from reaweb.schema import fmt, infer, merge

        schema = merge(infer({"id": 1, "name": "a"}), infer({"id": 1.5, "extra": True}))
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["fields"]["id"]["type"], "number")
        self.assertEqual(set(schema["optional"]), {"name", "extra"})
        self.assertIn("name?: string", fmt(schema))


if __name__ == "__main__":
    unittest.main(verbosity=2)
