"""Reversible hosted-UI deployment. Preserves /data and the previous container.

Requires an already built/pushed image and the existing scoped pilot state.
Only grants the server permission to invoke that runtime and its DEFAULT endpoint.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import boto3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--profile",default="costlens-demo")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    state=json.loads((root/"var/scoped-pilot-deployment.json").read_text())
    account,region=state["account"],state["region"]
    if not re.fullmatch(re.escape(account)+r"\.dkr\.ecr\."+re.escape(region)+r"\.amazonaws\.com/costlens:[a-zA-Z0-9_.-]+",args.image):
        raise ValueError("image must be an immutable tag in the existing CostLens repository")
    session=boto3.Session(profile_name=args.profile,region_name=region)
    if session.client("sts").get_caller_identity()["Account"]!=account:
        raise ValueError("wrong AWS account")
    cf=session.client("cloudformation")
    outputs={o["OutputKey"]:o["OutputValue"] for o in cf.describe_stacks(StackName="costlens-demo")["Stacks"][0]["Outputs"]}
    instance=outputs["ServerId"]
    role=cf.describe_stack_resource(StackName="costlens-demo",LogicalResourceId="ServerRole")["StackResourceDetail"]["PhysicalResourceId"]
    session.client("iam").put_role_policy(RoleName=role,PolicyName="costlens-scoped-ui",PolicyDocument=json.dumps({
        "Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime",
        "Resource":[state["runtime_arn"],state["runtime_arn"]+"/runtime-endpoint/DEFAULT"]}]}))
    stamp=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    script="""set -euo pipefail
IMAGE='__IMAGE__'
BACKUP='costlens-rollback-__STAMP__'
DEPLOY_DIR='/var/lib/costlens-deployments/__STAMP__'
install -d -m 700 "$DEPLOY_DIR"
aws ecr get-login-password --region __REGION__ | docker login --username AWS --password-stdin __REGISTRY__ >/dev/null
docker pull "$IMAGE"
docker inspect costlens --format '{{.Config.Image}}' > "$DEPLOY_DIR/previous-image.txt"
docker inspect costlens > "$DEPLOY_DIR/previous-container.json"
chmod 600 "$DEPLOY_DIR/previous-container.json"
docker inspect costlens --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -v -E '^(COSTLENS_BACKEND|COSTLENS_AGENTCORE_RUNTIME_ARN|COSTLENS_AWS_PROFILE)=' > "$DEPLOY_DIR/cloud.env"
printf '%s\\n' 'COSTLENS_BACKEND=cloud' 'COSTLENS_AGENTCORE_RUNTIME_ARN=__RUNTIME__' >> "$DEPLOY_DIR/cloud.env"
chmod 600 "$DEPLOY_DIR/cloud.env"
docker stop costlens
docker rename costlens "$BACKUP"
rollback() {
  docker stop costlens >/dev/null 2>&1 || true
  docker rename costlens 'costlens-failed-__STAMP__' >/dev/null 2>&1 || true
  docker rename "$BACKUP" costlens
  docker start costlens
}
trap 'rollback' ERR
docker run -d --name costlens --restart unless-stopped -p 8765:8765 -v /data:/data --env-file "$DEPLOY_DIR/cloud.env" --log-driver awslogs --log-opt awslogs-region=__REGION__ --log-opt awslogs-group=/costlens/demo "$IMAGE"
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8765/healthz >/dev/null; then break; fi
  sleep 2
done
docker exec -i costlens python - <<'PY'
import json,urllib.request
headers={'Host':'costlens.cloudaip.com','x-amzn-oidc-identity':'deployment-check'}
request=urllib.request.Request('http://127.0.0.1:8765/api/status',headers=headers)
with urllib.request.urlopen(request,timeout=175) as response:
    status=json.load(response)
assert status['backend']=='cloud' and status['synthetic'] is True
print(json.dumps({'cloud_backend':status['backend'],'synthetic':status['synthetic'],'data_through':status['data_through']}))
PY
trap - ERR
printf 'Cloud UI healthy. Rollback container: %s\\n' "$BACKUP"
"""
    for key,value in {"__IMAGE__":args.image,"__STAMP__":stamp,"__REGION__":region,
                      "__REGISTRY__":args.image.split("/")[0],"__RUNTIME__":state["runtime_arn"]}.items():
        script=script.replace(key,value)
    result=session.client("ssm").send_command(InstanceIds=[instance],DocumentName="AWS-RunShellScript",
        Parameters={"commands":[script],"executionTimeout":["600"]},TimeoutSeconds=600,
        Comment="Connect CostLens UI to scoped AgentCore/Athena; preserve previous container")
    release={"command_id":result["Command"]["CommandId"],"instance_id":instance,"image":args.image,
             "rollback_container":"costlens-rollback-"+stamp,"runtime_arn":state["runtime_arn"]}
    (root/"var/cloud-ui-release.json").write_text(json.dumps(release,indent=2)+"\n")
    print(json.dumps(release,indent=2))


if __name__=="__main__":main()
