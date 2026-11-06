"""Check the deployed app on its private host. --model opts into a paid chat check."""
import argparse
import json
from pathlib import Path

import boto3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",default="costlens-demo")
    parser.add_argument("--model",action="store_true")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    state=json.loads((root/"var/cloud-ui-release.json").read_text())
    source="""import json,urllib.request
base='http://127.0.0.1:8765'
headers={'Host':'costlens.cloudaip.com','x-amzn-oidc-identity':'deployment-check'}
def call(path,payload=None):
    request=urllib.request.Request(base+path,headers={**headers,'Content-Type':'application/json'},data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(request,timeout=175) as response:
        return json.load(response)
status=call('/api/status')
assert status['backend']=='cloud' and status['synthetic'] is True
query={'start':'2026-10-01','end':'2026-11-01','granularity':'total','group_by':'service'}
chart=call('/api/query',{'query':query})['result']
assert chart['total_display']=='$18,561.93' and len(chart['series'])==8
opportunities=call('/api/opportunities')
assert len(opportunities['opportunities'])==29
optimization=call('/api/optimize')
assert optimization['state']=='ok' and optimization['totals']['counted_display']=='$3,272.06'
assert optimization['source_snapshot_as_of'] and optimization['workflow_read_only'] is True
assert optimization['service_filter']=='all'
rds=call('/api/optimize?service=AmazonRDS')
assert len(rds['opportunities'])==1 and rds['service_label']=='RDS'
s3=call('/api/optimize?service=AmazonS3')
assert s3['opportunities']==[] and s3['totals']['counted_nanos']==0
report={'backend':status['backend'],'synthetic':status['synthetic'],'chart_total':chart['total_display'],'chart_series':len(chart['series']),'opportunities':len(opportunities['opportunities'])}
report.update(optimization_estimate=optimization['totals']['counted_display'],optimization_findings=len(optimization['opportunities']))
report.update(service_tabs_verified=True,rds_recommendations=len(rds['opportunities']),s3_recommendations=len(s3['opportunities']))
if __MODEL__:
    answer=call('/api/ask',{'question':'What was our total unblended AWS cost in October 2026? Use query_costs for start 2026-10-01, exclusive end 2026-11-01, total granularity, service grouping, and top_n 8. State that it is synthetic.'})
    assert '$18,561.93' in answer['message'], answer['message']
    assert answer['backend']=='cloud' and answer['evidence_queries']
    evidence=call('/api/query',{'query':answer['evidence_queries'][0]['query']})['result']
    assert evidence['total_display']=='$18,561.93'
    report.update(chat_answer=answer['message'],evidence_chart_total=evidence['total_display'],run_id=answer['run_id'])
print(json.dumps(report))
""".replace("__MODEL__",str(args.model))
    script="docker exec -i costlens python - <<'PY'\n"+source+"\nPY\n"
    command=boto3.Session(profile_name=args.profile,region_name="eu-central-1").client("ssm").send_command(
        InstanceIds=[state["instance_id"]],DocumentName="AWS-RunShellScript",Parameters={"commands":[script],"executionTimeout":["600"]},
        Comment="Verify hosted cloud chart/common opportunities"+(" and one paid chat investigation" if args.model else ""))
    record={"command_id":command["Command"]["CommandId"],"instance_id":state["instance_id"],"model_check":args.model}
    (root/"var/cloud-ui-check.json").write_text(json.dumps(record,indent=2)+"\n")
    print(json.dumps(record,indent=2))


if __name__=="__main__":main()
