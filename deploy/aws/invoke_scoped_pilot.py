"""Invoke the scoped AgentCore pilot; a question calls the paid configured model."""
import argparse
import json
import uuid
from pathlib import Path

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="costlens-demo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--question")
    group.add_argument("--health", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    state = json.loads((root / "var/scoped-pilot-deployment.json").read_text())
    client = boto3.Session(profile_name=args.profile,region_name=state["region"]).client("bedrock-agentcore",
        config=Config(connect_timeout=5,read_timeout=180,retries={"max_attempts":0}))
    payload = {"operation":"health"} if args.health else {"question":args.question}
    response = client.invoke_agent_runtime(agentRuntimeArn=state["runtime_arn"],runtimeSessionId=str(uuid.uuid4()),
        contentType="application/json",accept="application/json",payload=json.dumps(payload).encode())
    value = json.loads(response["response"].read())
    (root / "var/scoped-pilot-last-invocation.json").write_text(json.dumps(value,indent=2)+"\n")
    print(json.dumps(value,indent=2))
    if value.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
