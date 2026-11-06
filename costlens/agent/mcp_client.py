"""IAM-signed MCP client for one fixed AgentCore Gateway. No bearer secrets."""
from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from .cloud_tools import NAMES


class GatewayTools:
    name = "scoped_athena"

    def __init__(self, url: str, region: str, *, session=None, target="finops", transport=None):
        if not re.fullmatch(r"https://[a-z0-9-]+\.gateway\.bedrock-agentcore\." + re.escape(region) + r"\.amazonaws\.com/mcp", url):
            raise ValueError("Gateway must be the configured regional AWS HTTPS endpoint")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,50}", target):
            raise ValueError("invalid configured target")
        self.url, self.region, self.target, self.session = url, region, target, session
        self.transport = transport or self._http
        self.session_id = None
        self.counter = 0

    def _http(self, payload):
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        session = self.session or boto3.Session(region_name=self.region)
        credentials = session.get_credentials().get_frozen_credentials()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-03-26"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        body = json.dumps(payload).encode()
        signed = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        SigV4Auth(credentials, "bedrock-agentcore", self.region).add_auth(signed)
        with urlopen(Request(self.url, data=body, headers=dict(signed.headers), method="POST"), timeout=60) as response:
            self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("MCP response exceeds its byte limit")
            text = raw.decode()
            if response.headers.get("Content-Type", "").startswith("text/event-stream"):
                messages = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]
                return next(message for message in messages if message.get("id") == payload["id"])
            return json.loads(text)

    def rpc(self, method, params):
        self.counter += 1
        response = self.transport({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params})
        if response.get("error"):
            raise ValueError("MCP request rejected")
        if response.get("id") != self.counter:
            raise ValueError("MCP response ID mismatch")
        return response["result"]

    def initialize(self):
        return self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "costlens", "version": "0.1.0"}})

    def discover(self):
        result = self.rpc("tools/list", {})
        expected = {self.target + "___" + name for name in NAMES}
        if not expected <= {tool["name"] for tool in result.get("tools", [])}:
            raise ValueError("Gateway is missing a required FinOps tool")
        return sorted(expected)

    def call(self, tool: str, **arguments):
        if tool not in NAMES:
            raise PermissionError("tool is outside the fixed allow-list")
        result = self.rpc("tools/call", {"name": self.target + "___" + tool, "arguments": arguments})
        if result.get("isError"):
            raise ValueError("Gateway tool failed")
        value = result.get("structuredContent")
        if value is None:
            texts = [block["text"] for block in result.get("content", []) if block.get("type") == "text"]
            value = json.loads("\n".join(texts))
        if not isinstance(value, dict):
            raise ValueError("tool response must be an object")
        return value
