"""MCP stdio wrapper: protocol handshake and read-only tools over a generated output."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_server import make_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class McpServer(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "spec"
        subprocess.run(
            [sys.executable, "-m", "reaweb", "map", f"{self.server.base_url}/openapi.json",
             "--mode", "spec", "-o", str(self.out)],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=120,
        )
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp" / "server.py")],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def test_tools_over_stdio(self):
        init = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "rea-web")

        tools = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(names, {"map_site", "list_endpoints", "get_evidence", "get_client", "get_graph"})

        listed = self.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "list_endpoints", "arguments": {"outdir": str(self.out)}}})
        payload = json.loads(listed["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 3)

        evidence = self.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "get_evidence", "arguments": {
                                  "outdir": str(self.out),
                                  "ref": "evidence/spec.json#/paths/~1items/get"}}})
        data = json.loads(evidence["result"]["content"][0]["text"])
        self.assertEqual(data["data"]["operationId"], "listItems")

        mapped = self.call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "map_site", "arguments": {
                                "url": f"{self.server.base_url}/openapi.json",
                                "out": str(Path(self.tmp.name) / "mcp-map"), "mode": "spec"}}})
        mapped_data = json.loads(mapped["result"]["content"][0]["text"])
        self.assertEqual(mapped_data["summary"]["endpoints"], 3)

        client = self.call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "get_client", "arguments": {"outdir": str(self.out)}}})
        client_data = json.loads(client["result"]["content"][0]["text"])
        self.assertIn("class MockStoreClient", client_data["client"])
        self.assertIn("/items", client_data["openapi"]["paths"])

        escaped = self.call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                             "params": {"name": "get_evidence", "arguments": {
                                 "outdir": str(self.out), "ref": "../../etc/passwd"}}})
        self.assertTrue(escaped["result"]["isError"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
