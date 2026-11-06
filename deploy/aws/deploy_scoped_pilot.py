"""Provision the synthetic Athena -> MCP Gateway -> AgentCore pilot.

Run explicitly with the test AWS profile. Never creates or updates source exports.
Records deployment state under ignored var/. This does not update the existing app/runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from costlens.agent.cloud_tools import SPECS, gateway_schema
from costlens.ingest.parquet_snapshot import read_source
from costlens.query.athena import AthenaScope

ROOT = Path(__file__).resolve().parents[2]
TAGS = {"Project": "CostLens", "Component": "ScopedSyntheticPilot", "DataClass": "Synthetic"}


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def allow(actions, resources, **extra):
    return {"Effect": "Allow", "Action": actions, "Resource": resources, **extra}


def update_code(session, state, sdk_dir, data_root):
    """Update only the already registered pilot Lambda/target/runtime."""
    lamb, core, s3 = [session.client(service) for service in ("lambda","bedrock-agentcore-control","s3")]
    from costlens.parquet_fixtures import _extra_sources
    from costlens.ingest.optimization_report import build
    optimization_report=build(data_root)
    tools_zip = ROOT / "var/scoped-tools.zip"
    with zipfile.ZipFile(tools_zip,"w",zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT/"costlens").rglob("*.py"):
            archive.write(path,path.relative_to(ROOT).as_posix())
        archive.writestr("storage_fixture.json",json.dumps(_extra_sources()["storage-lens"]))
        archive.writestr("optimization_summary.json",json.dumps(optimization_report))
    lamb.update_function_code(FunctionName=state["lambda_arn"],ZipFile=tools_zip.read_bytes())
    lamb.get_waiter("function_updated_v2").wait(FunctionName=state["lambda_arn"])
    core.update_gateway_target(gatewayIdentifier=state["gateway_id"],targetId=state["target_id"],name="finops",
        targetConfiguration={"mcp":{"lambda":{"lambdaArn":state["lambda_arn"],"toolSchema":{"inlinePayload":[
            {"name":s["name"],"description":s["description"],"inputSchema":gateway_schema(s["input_schema"])} for s in SPECS]}}}},
        credentialProviderConfigurations=[{"credentialProviderType":"GATEWAY_IAM_ROLE"}])
    runtime_zip = ROOT / "var/scoped-runtime.zip"
    with zipfile.ZipFile(runtime_zip,"w",zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT/"costlens").rglob("*.py"):
            archive.write(path,path.relative_to(ROOT).as_posix())
        for path in sdk_dir.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path,path.relative_to(sdk_dir).as_posix())
        archive.writestr("cloud_entry.py","from costlens.agent.cloud_runtime import main\nmain()\n")
    import hashlib
    key = "artifacts/scoped-runtime-"+hashlib.sha256(runtime_zip.read_bytes()).hexdigest()[:16]+".zip"
    uploaded = s3.put_object(Bucket=state["results_bucket"],Key=key,Body=runtime_zip.read_bytes(),
                            ServerSideEncryption="AES256",ExpectedBucketOwner=state["account"])
    runtime = core.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    core.update_agent_runtime(agentRuntimeId=state["runtime_id"],roleArn=runtime["roleArn"],
        networkConfiguration=runtime["networkConfiguration"],protocolConfiguration=runtime["protocolConfiguration"],
        environmentVariables=runtime["environmentVariables"],agentRuntimeArtifact={"codeConfiguration":{
            "code":{"s3":{"bucket":state["results_bucket"],"prefix":key,"versionId":uploaded["VersionId"]}},
            "runtime":"PYTHON_3_12","entryPoint":["cloud_entry.py"]}})
    print("Updated scoped pilot code and tool schemas; no source or IAM changes",flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="costlens-demo")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--data-root", default="var/synthetic-parquet-20261105-v1")
    parser.add_argument("--sdk-dir", default="var/aws-tools")
    parser.add_argument("--code-only", action="store_true")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    region = args.region
    state_path = ROOT / "var/scoped-pilot-deployment.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    def save(**values):
        state.update(values)
        state_path.write_text(json.dumps(state, indent=2) + "\n")
    if state.get("account", account) != account or state.get("region", region) != region:
        raise ValueError("deployment state belongs to another AWS scope")
    if args.code_only:
        if not state.get("runtime_id"):
            raise ValueError("no existing scoped pilot deployment")
        update_code(session,state,ROOT/args.sdk_dir,ROOT/args.data_root)
        return
    save(account=account, region=region, synthetic=True)
    iam, s3, glue, athena, lamb, core = [session.client(name) for name in
        ("iam", "s3", "glue", "athena", "lambda", "bedrock-agentcore-control")]
    data_root = (ROOT / args.data_root).resolve()
    registrations = json.loads((data_root / "s3-destinations.json").read_text(encoding="utf-8-sig"))
    sources = {r["source"]: r for r in registrations}
    database, workgroup = "costlens_scoped_pilot", "costlens-scoped-pilot"
    results_bucket = "costlens-scoped-pilot-" + account
    scope = AthenaScope.from_dict({"region": region, "account_id": account, "workspace_id": "synthetic-pilot",
        "database": database, "workgroup": workgroup, "results_uri": f"s3://{results_bucket}/queries/",
        "accounts": ["210987654321", "310987654321", "410987654321", "510987654321"],
        "tables": {source: source.replace("-", "_") for source in sources},
        "locations": {source: f's3://{r["bucket"]}/{r["prefix"]}/' + ("" if source == "cur" else "snapshot_date=2026-11-02/")
                      for source, r in sources.items()}})
    scope_raw = dict(scope.__dict__)
    (ROOT / "var/scoped-athena.json").write_text(json.dumps(scope_raw, indent=2) + "\n")
    source_data = {source: read_source(data_root, source) for source in sources}
    from costlens.common import synchronize, export_parquet
    from costlens.store import connect
    common_db = connect(ROOT / "var/common-evidence.sqlite")
    try:
        common = synchronize(common_db,workspace_id=scope.workspace_id,
            sources={source:[row for _,rows in source_data[source][1] for row in rows] for source in sources if source != "cur"},
            allowed_accounts=scope.accounts,observed_at=source_data["cur"][0]["as_of"],complete=True)
        generation = common_db.execute("SELECT generation_id FROM common_generations WHERE workspace_id=? AND status='active'",(scope.workspace_id,)).fetchone()[0]
        common_path = ROOT / f"var/common-publication/{generation}/part-00001.parquet"
        if not common_path.exists():
            export_parquet(common_db,scope.workspace_id,common_path,synthetic=True)
    finally:
        common_db.close()
    common_prefix = f"curated/opportunities/generation={generation}/"
    scope = AthenaScope.from_dict({**scope_raw,"tables":{**scope.tables,"opportunities":"common_opportunities"},
                                  "locations":{**scope.locations,"opportunities":f"s3://{results_bucket}/{common_prefix}"}})
    scope_raw = dict(scope.__dict__)
    (ROOT / "var/scoped-athena.json").write_text(json.dumps(scope_raw,indent=2)+"\n")
    print("Validated local source manifests and fixed Athena configuration", flush=True)
    try:
        s3.create_bucket(Bucket=results_bucket, CreateBucketConfiguration={"LocationConstraint": region})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
    s3.put_public_access_block(Bucket=results_bucket, ExpectedBucketOwner=account, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=results_bucket, ExpectedBucketOwner=account, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_versioning(Bucket=results_bucket, ExpectedBucketOwner=account, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(Bucket=results_bucket, ExpectedBucketOwner=account, Tagging={"TagSet": [{"Key": k, "Value": v} for k,v in TAGS.items()]})
    s3.put_bucket_lifecycle_configuration(Bucket=results_bucket, ExpectedBucketOwner=account, LifecycleConfiguration={"Rules": [{
        "ID": "ExpireQueryResults", "Status": "Enabled", "Filter": {"Prefix": "queries/"},
        "Expiration": {"Days": 7}, "NoncurrentVersionExpiration": {"NoncurrentDays": 7}}]})
    s3.put_object(Bucket=results_bucket,Key=common_prefix+"part-00001.parquet",Body=common_path.read_bytes(),
                  ServerSideEncryption="AES256",ExpectedBucketOwner=account,Metadata={"synthetic":"true","contract":"common-opportunities-v1"})
    try:
        glue.create_database(CatalogId=account, DatabaseInput={"Name": database, "Description": "CostLens synthetic pilot only"})
    except glue.exceptions.AlreadyExistsException:
        pass
    import pyarrow as pa
    import pyarrow.parquet as pq
    for source in scope.tables:
        manifest = source_data[source][0] if source != "opportunities" else None
        table_name = scope.tables[source]
        schema = pq.ParquetFile(common_path if source == "opportunities" else data_root / source / manifest["files"][0]["key"]).schema_arrow
        columns = []
        for field in schema:
            dtype = (f"decimal({field.type.precision},{field.type.scale})" if pa.types.is_decimal(field.type) else
                     "boolean" if pa.types.is_boolean(field.type) else "bigint" if pa.types.is_integer(field.type) else "string")
            columns.append({"Name": field.name.lower(), "Type": dtype})
        descriptor = {"Columns": columns, "Location": scope.locations[source],
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"}}
        table = {"Name": table_name, "TableType": "EXTERNAL_TABLE", "StorageDescriptor": descriptor,
                 "Parameters": {"classification": "parquet", "EXTERNAL": "TRUE", "costlens.synthetic": "true"},
                 "PartitionKeys": [{"Name": "billing_period", "Type": "string"}] if source == "cur" else []}
        try:
            glue.create_table(CatalogId=account, DatabaseName=database, TableInput=table)
        except glue.exceptions.AlreadyExistsException:
            existing = glue.get_table(CatalogId=account, DatabaseName=database, Name=table_name)["Table"]
            if existing["StorageDescriptor"]["Location"] != descriptor["Location"]:
                raise ValueError("existing catalog location differs; refusing update")
        if source == "cur":
            for entry in manifest["files"]:
                partition_sd = {**descriptor, "Location": scope.locations[source] + f'billing_period={entry["billing_period"]}/'}
                try:
                    glue.create_partition(CatalogId=account, DatabaseName=database, TableName=table_name,
                        PartitionInput={"Values": [entry["billing_period"]], "StorageDescriptor": partition_sd})
                except glue.exceptions.AlreadyExistsException:
                    pass
    config = {"EnforceWorkGroupConfiguration": True, "BytesScannedCutoffPerQuery": scope.max_scan_bytes,
        "PublishCloudWatchMetricsEnabled": False, "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
        "ResultConfiguration": {"OutputLocation": scope.results_uri, "ExpectedBucketOwner": account,
                                "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}}}
    try:
        athena.create_work_group(Name=workgroup, Configuration=config, Description="CostLens bounded synthetic reads", Tags=[{"Key":k,"Value":v} for k,v in TAGS.items()])
    except athena.exceptions.InvalidRequestException:
        current = athena.get_work_group(WorkGroup=workgroup)["WorkGroup"]["Configuration"]
        if not current.get("EnforceWorkGroupConfiguration") or current["ResultConfiguration"]["OutputLocation"] != scope.results_uri:
            raise ValueError("existing workgroup differs; refusing update")
    print("Registered five Parquet tables and the enforced 10 MB Athena workgroup", flush=True)

    def role(name, service, statements, source_kind=None):
        trust = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
        if source_kind:
            trust["Condition"] = {"StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:{source_kind}/*"}}
        try:
            result = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({"Version":"2012-10-17","Statement":[trust]}),
                                     Tags=[{"Key":k,"Value":v} for k,v in TAGS.items()])
        except iam.exceptions.EntityAlreadyExistsException:
            result = iam.get_role(RoleName=name)
            tags = {t["Key"]:t["Value"] for t in result["Role"].get("Tags", [])}
            if tags.get("Component") != TAGS["Component"]:
                raise ValueError("existing IAM role is not owned by this pilot")
        iam.put_role_policy(RoleName=name, PolicyName="CostLensScopedPilot", PolicyDocument=json.dumps(policy(statements)))
        return result["Role"]["Arn"]

    function_name = "costlens-scoped-finops"
    function_arn = f"arn:aws:lambda:{region}:{account}:function:{function_name}"
    logs = session.client("logs")
    try:
        logs.create_log_group(logGroupName="/aws/lambda/" + function_name)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName="/aws/lambda/" + function_name, retentionInDays=7)
    read_policy = [
        allow(["athena:GetWorkGroup","athena:StartQueryExecution","athena:GetQueryExecution","athena:GetQueryResults","athena:StopQueryExecution"],
              f"arn:aws:athena:{region}:{account}:workgroup/{workgroup}"),
        allow(["glue:GetDatabase","glue:GetTable","glue:GetPartition","glue:GetPartitions","glue:BatchGetPartition"],
              [f"arn:aws:glue:{region}:{account}:catalog", f"arn:aws:glue:{region}:{account}:database/{database}",
               *[f"arn:aws:glue:{region}:{account}:table/{database}/{name}" for name in scope.tables.values()]]),
        allow(["s3:GetBucketLocation"], [f'arn:aws:s3:::{r["bucket"]}' for r in registrations] + [f"arn:aws:s3:::{results_bucket}"]),
        allow(["s3:GetObject"], [f'arn:aws:s3:::{r["bucket"]}/{r["prefix"]}/*' for r in registrations]+[f"arn:aws:s3:::{results_bucket}/{common_prefix}*"]),
        allow(["s3:PutObject","s3:GetObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"], f"arn:aws:s3:::{results_bucket}/queries/*"),
        allow(["s3:ListBucket"], f"arn:aws:s3:::{results_bucket}", Condition={"StringLike":{"s3:prefix":["queries/","queries/*",common_prefix,common_prefix+"*"]}}),
        allow(["logs:CreateLogStream","logs:PutLogEvents"], f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function_name}:*")]
    for r in registrations:
        read_policy.append(allow(["s3:ListBucket"], f'arn:aws:s3:::{r["bucket"]}',
            Condition={"StringLike":{"s3:prefix":[r["prefix"],r["prefix"]+"/",r["prefix"]+"/*"]}}))
    lambda_role = role("costlens-scoped-query", "lambda.amazonaws.com", read_policy)
    storage_rows = [row for _, rows in read_source(data_root, "storage-lens")[1] for row in rows]
    package = ROOT / "var/scoped-tools.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT / "costlens").rglob("*.py"):
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr("storage_fixture.json", json.dumps(storage_rows))
    environment = {"COSTLENS_ATHENA_SCOPE": json.dumps(scope_raw, separators=(",",":")),
                   "COSTLENS_GATEWAY_TARGET": "finops", "COSTLENS_STORAGE_FIXTURE_PATH": "/var/task/storage_fixture.json"}
    time.sleep(10)  # IAM propagation; bounded and only during deployment.
    try:
        lamb.create_function(FunctionName=function_name, Runtime="python3.12", Role=lambda_role,
            Handler="costlens.agent.cloud_tools.lambda_handler", Code={"ZipFile":package.read_bytes()},
            Timeout=75, MemorySize=256, Environment={"Variables":environment}, Tags=TAGS)
    except lamb.exceptions.ResourceConflictException:
        existing = lamb.get_function(FunctionName=function_name)
        if existing.get("Tags",{}).get("Component") != TAGS["Component"]:
            raise ValueError("Lambda is not owned by this pilot")
        lamb.update_function_code(FunctionName=function_name, ZipFile=package.read_bytes())
        lamb.get_waiter("function_updated").wait(FunctionName=function_name)
        lamb.update_function_configuration(FunctionName=function_name, Role=lambda_role, Environment={"Variables":environment})
    lamb.get_waiter("function_active_v2").wait(FunctionName=function_name)
    gateway_role = role("costlens-scoped-gateway", "bedrock-agentcore.amazonaws.com", [allow("lambda:InvokeFunction",function_arn)], "gateway")
    time.sleep(10)
    if not state.get("gateway_id"):
        gateway = core.create_gateway(name="costlens-scoped-pilot", roleArn=gateway_role, authorizerType="AWS_IAM", protocolType="MCP",
            description="Scoped read-only synthetic FinOps tools", tags=TAGS)
        save(gateway_id=gateway["gatewayId"], gateway_arn=gateway["gatewayArn"], gateway_url=gateway["gatewayUrl"])
    for _ in range(30):
        gateway = core.get_gateway(gatewayIdentifier=state["gateway_id"])
        if gateway["status"] == "READY":
            break
        if gateway["status"] in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            raise RuntimeError("Gateway failed: " + str(gateway.get("statusReasons")))
        time.sleep(2)
    else:
        raise TimeoutError("Gateway not ready")
    target_args = dict(gatewayIdentifier=state["gateway_id"], name="finops",
            targetConfiguration={"mcp":{"lambda":{"lambdaArn":function_arn, "toolSchema":{"inlinePayload":[
                {"name":s["name"],"description":s["description"],"inputSchema":gateway_schema(s["input_schema"])} for s in SPECS]}}}},
            credentialProviderConfigurations=[{"credentialProviderType":"GATEWAY_IAM_ROLE"}])
    if not state.get("target_id"):
        target = core.create_gateway_target(**target_args)
        save(target_id=target["targetId"])
    else:
        core.update_gateway_target(targetId=state["target_id"],**target_args)
    print("Connected IAM-authenticated Gateway to scoped Lambda tools", flush=True)
    runtime_role = role("costlens-scoped-runtime", "bedrock-agentcore.amazonaws.com", [
        allow("bedrock-agentcore:InvokeGateway", state["gateway_arn"]),
        allow(["s3:GetObject","s3:GetObjectVersion"], f"arn:aws:s3:::{results_bucket}/artifacts/*"),
        allow("bedrock:InvokeModel", [f"arn:aws:bedrock:{region}:{account}:inference-profile/eu.amazon.nova-lite-v1:0",
             *[f"arn:aws:bedrock:{r}::foundation-model/amazon.nova-lite-v1:0" for r in ("eu-central-1","eu-west-1","eu-west-3","eu-north-1")]]),
        allow(["logs:CreateLogGroup","logs:CreateLogStream","logs:DescribeLogStreams","logs:PutLogEvents"],
              f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*")], "runtime")
    runtime_zip = ROOT / "var/scoped-runtime.zip"
    with zipfile.ZipFile(runtime_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT/"costlens").rglob("*.py"):
            archive.write(path,path.relative_to(ROOT).as_posix())
        sdk = ROOT/args.sdk_dir
        for path in sdk.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path,path.relative_to(sdk).as_posix())
        archive.writestr("cloud_entry.py", "from costlens.agent.cloud_runtime import main\nmain()\n")
    import hashlib
    key = "artifacts/scoped-runtime-" + hashlib.sha256(runtime_zip.read_bytes()).hexdigest()[:16] + ".zip"
    uploaded = s3.put_object(Bucket=results_bucket,Key=key,Body=runtime_zip.read_bytes(),ServerSideEncryption="AES256",ExpectedBucketOwner=account)
    artifact = {"codeConfiguration":{"code":{"s3":{"bucket":results_bucket,"prefix":key,"versionId":uploaded["VersionId"]}},
                                     "runtime":"PYTHON_3_12","entryPoint":["cloud_entry.py"]}}
    runtime_env = {"COSTLENS_GATEWAY_URL":state["gateway_url"], "COSTLENS_MODEL_PROVIDER":"bedrock-nova",
                   "COSTLENS_BEDROCK_REGION":region,"COSTLENS_REFERENCE_DATE":scope.reference_date,"COSTLENS_SYNTHETIC":"true"}
    time.sleep(10)
    if state.get("runtime_id"):
        runtime = core.update_agent_runtime(agentRuntimeId=state["runtime_id"],agentRuntimeArtifact=artifact,roleArn=runtime_role,
            networkConfiguration={"networkMode":"PUBLIC"},protocolConfiguration={"serverProtocol":"HTTP"},environmentVariables=runtime_env)
    else:
        runtime = core.create_agent_runtime(agentRuntimeName="costlens_scoped_pilot",agentRuntimeArtifact=artifact,roleArn=runtime_role,
            networkConfiguration={"networkMode":"PUBLIC"},protocolConfiguration={"serverProtocol":"HTTP"},environmentVariables=runtime_env,
            lifecycleConfiguration={"idleRuntimeSessionTimeout":60,"maxLifetime":900},tags=TAGS,
            description="Synthetic FinOps pilot: IAM MCP Gateway and scoped Athena. No production data.")
    save(runtime_id=runtime["agentRuntimeId"],runtime_arn=runtime["agentRuntimeArn"],workgroup=workgroup,database=database,
         results_bucket=results_bucket,lambda_arn=function_arn,scope_file="var/scoped-athena.json")
    print(json.dumps(state,indent=2),flush=True)


if __name__ == "__main__":
    main()
