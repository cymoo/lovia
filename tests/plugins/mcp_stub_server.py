"""A minimal stdio MCP server speaking raw JSON-RPC — no SDK, so it serves
whichever mcp SDK major the client side runs. Tools span two pages so the
client's pagination is exercised, and one result per kind lovia reads fields
from (text, tool error, image)."""

from __future__ import annotations

import json
import sys
from typing import Any

PAGES: dict[str | None, tuple[list[dict[str, Any]], str | None]] = {
    None: (
        [
            {
                "name": "add",
                "description": "Add two integers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
            }
        ],
        "2",
    ),
    "2": (
        [
            {"name": "fail", "inputSchema": {"type": "object"}},
            {"name": "pic", "inputSchema": {"type": "object"}},
        ],
        None,
    ),
}


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "add":
        return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "boom"}], "isError": True}
    return {"content": [{"type": "image", "mimeType": "image/png", "data": "AAAA"}]}


def handle(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        tools, cursor = PAGES[params.get("cursor")]
        return {"tools": tools, **({"nextCursor": cursor} if cursor else {})}
    if method == "tools/call":
        return call(params["name"], params.get("arguments") or {})
    return None


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:  # a notification: nothing to answer
        continue
    result = handle(msg["method"], msg.get("params") or {})
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
    if result is None:
        reply["error"] = {"code": -32601, "message": f"unknown {msg['method']}"}
    else:
        reply["result"] = result
    print(json.dumps(reply), flush=True)
