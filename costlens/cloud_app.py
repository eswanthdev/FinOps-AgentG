"""Application boundary to the configured IAM-protected AgentCore runtime.

Cloud failures never fall back to a different local dataset. No credentials or
AWS endpoint choices are accepted from browser requests.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid


class CloudUnavailable(RuntimeError):
    pass


def enabled():
    return os.environ.get("COSTLENS_BACKEND", "local").strip().lower() == "cloud"


def mode():
    return {"mode":"model","label":"AI-assisted · Cloud","detail":"AgentCore chat and live Athena dashboards over synthetic S3 evidence."}


class Client:
    def __init__(self, sdk=None):
        self.arn = os.environ.get("COSTLENS_AGENTCORE_RUNTIME_ARN", "")
        match = re.fullmatch(r"arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):runtime/([A-Za-z0-9_-]+)",self.arn)
        if not match:
            raise CloudUnavailable("Cloud runtime is not configured")
        self.region = match[1]
        if sdk is None:
            import boto3
            from botocore.config import Config
            profile = os.environ.get("COSTLENS_AWS_PROFILE") or None
            sdk = boto3.Session(profile_name=profile,region_name=self.region).client("bedrock-agentcore",
                config=Config(connect_timeout=4,read_timeout=175,retries={"max_attempts":0}))
        self.sdk = sdk

    def invoke(self, operation, **arguments):
        try:
            response = self.sdk.invoke_agent_runtime(agentRuntimeArn=self.arn,runtimeSessionId=str(uuid.uuid4()),
                contentType="application/json",accept="application/json",payload=json.dumps({"operation":operation,**arguments}).encode())
            body = response["response"].read(1_048_577)
            if len(body)>1_048_576:
                raise ValueError("cloud result too large")
            result = json.loads(body)
            if result.get("status")!="ok":
                raise CloudUnavailable(result.get("message","Cloud service did not return a verified result"))
            return result if operation=="ask" else result["result"]
        except CloudUnavailable:
            raise
        except Exception as exc:
            raise CloudUnavailable("Cloud backend is unavailable. No local data was substituted.") from exc


def query(raw):
    from .query.model import CostQuery
    validated = CostQuery.from_dict(raw)
    result = Client().invoke("query",query=validated.to_dict())
    if result.get("status") not in ("ok","insufficient_data"):
        raise CloudUnavailable(result.get("reason","Cloud query was rejected"))
    return result


def dimensions():
    result = Client().invoke("dimensions")
    if result.get("status")!="ok":
        raise CloudUnavailable("Cloud dimensions are unavailable")
    return result["dimensions"]


def status():
    result = Client().invoke("status")
    if result.get("status")!="ok":
        raise CloudUnavailable("Cloud data status is unavailable")
    return result["data_status"]


def opportunities():
    result = Client().invoke("opportunities",account="")
    if result.get("status")!="ok":
        raise CloudUnavailable("Cloud opportunities are unavailable")
    return result


def optimization():
    result=Client().invoke("optimization")
    if result.get("status")!="ok":
        raise CloudUnavailable("Cloud optimization analysis is unavailable")
    return result["summary"]


def ask(conn,question,previous,user_id,conversation,past,on_event=None):
    from .agent import session
    if session.tokens_this_month(conn)>=session.monthly_token_cap():
        raise CloudUnavailable("The monthly model limit has been reached. No model request was made.")
    notes = [m["text"] for m in session.list_memories(conn,user_id)]
    if on_event:
        on_event({"type":"step","text":"Investigating cloud evidence through AgentCore…"})
    started = time.monotonic()
    response = Client().invoke("ask",question=question,previous=previous,history=past,notes=notes)
    recorded = {**response,"answer":response["message"],"turns":len(response.get("trace",[]))+1}
    run_id = session.record_run(conn,user_id,conversation,"AgentCore scoped investigator",question,recorded,int((time.monotonic()-started)*1000))
    response.update(run_id=run_id,memories_used=len(notes),result=None,
                    backend="cloud",
                    interpretation={"status":"ok","module":"investigator","understood":[],"assumptions":[],"query":None})
    return response
