"""Running behind a login proxy: settings, host checks, health check and deploy files."""
import http.client
import json
import logging
import re
import threading
from pathlib import Path

import pytest

from costlens import __main__ as cli
from costlens import hosting
from costlens.server import serve

ROOT = Path(__file__).parent.parent
TEMPLATE = (ROOT / "deploy" / "aws" / "costlens-demo.yaml").read_text(encoding="utf-8")
DOMAIN = "costlens.example.com"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("COSTLENS_ALLOWED_HOSTS", "COSTLENS_BEHIND_AUTH_PROXY", "COSTLENS_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def start(db):
    httpd = serve(str(db), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def get(port, path, host, method="GET", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host, **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, data, r


@pytest.fixture()
def local_server(demo_db_path):
    httpd = start(demo_db_path)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def hosted_server(demo_db_path, monkeypatch):
    monkeypatch.setenv("COSTLENS_ALLOWED_HOSTS", DOMAIN)
    httpd = start(demo_db_path)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ settings

def test_defaults_stay_local_only():
    assert hosting.allowed_hosts() == frozenset()
    assert not hosting.behind_auth_proxy()
    assert hosting.bind_allowed("127.0.0.1") and not hosting.bind_allowed("0.0.0.0")


@pytest.mark.parametrize("proxy,hosts,allowed", [
    ("true", DOMAIN, True), ("true", "", False), ("", DOMAIN, False), ("false", DOMAIN, False), ("yes", DOMAIN, False),
    ("true", "*", False), ("true", "*.example.com", False), ("true", "nodots", False),
])
def test_non_local_bind_needs_both_settings(monkeypatch, proxy, hosts, allowed):
    monkeypatch.setenv("COSTLENS_BEHIND_AUTH_PROXY", proxy)
    monkeypatch.setenv("COSTLENS_ALLOWED_HOSTS", hosts)
    assert hosting.bind_allowed("0.0.0.0") is allowed


def test_invalid_host_names_are_ignored(monkeypatch):
    monkeypatch.setenv("COSTLENS_ALLOWED_HOSTS", f" {DOMAIN.upper()} ,*,evil host.com,http://x.com,")
    assert hosting.allowed_hosts() == {DOMAIN}


def test_cli_refuses_non_local_bind(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli._run_server(tmp_path / "x.sqlite", "0.0.0.0", 0, "x")
    assert e.value.code == 2
    assert "no login of its own" in capsys.readouterr().err


@pytest.mark.parametrize("value,level", [("INFO", logging.INFO), ("debug", logging.DEBUG), ("", logging.WARNING), ("LOUD", logging.WARNING)])
def test_log_level(monkeypatch, value, level):
    monkeypatch.setenv("COSTLENS_LOG_LEVEL", value)
    assert hosting.log_level() == level


# ------------------------------------------------------------------ host checks

def test_other_hosts_stay_blocked_by_default(local_server):
    assert get(local_server, "/api/status", DOMAIN)[0] == 421


def test_allowed_host_works(hosted_server):
    assert get(hosted_server, "/api/status", DOMAIN)[0] == 200
    assert get(hosted_server, "/api/status", DOMAIN + ":443")[0] == 200


def test_localhost_still_works_when_hosted(hosted_server):
    assert get(hosted_server, "/api/status", f"127.0.0.1:{hosted_server}")[0] == 200


@pytest.mark.parametrize("host", ["evil.example.com", "costlens.example.com.evil.com", "example.com", ""])
def test_unlisted_hosts_are_refused_when_hosted(hosted_server, host):
    assert get(hosted_server, "/api/status", host)[0] == 421


def test_same_origin_post_works_and_cross_origin_is_blocked(hosted_server):
    body = json.dumps({"question": "any anomalies?"})
    h = {"Content-Type": "application/json"}
    assert get(hosted_server, "/api/ask", DOMAIN, "POST", body, {**h, "Origin": f"https://{DOMAIN}"})[0] == 200
    assert get(hosted_server, "/api/ask", DOMAIN, "POST", body, {**h, "Origin": "https://evil.example.com"})[0] == 403


def test_https_header_only_when_hosted(local_server, hosted_server):
    assert get(local_server, "/api/status", f"127.0.0.1:{local_server}")[2].getheader("Strict-Transport-Security") is None
    assert "max-age" in get(hosted_server, "/api/status", DOMAIN)[2].getheader("Strict-Transport-Security")


# ------------------------------------------------------------------ health check

def test_healthz_answers_by_ip_and_shows_no_data(hosted_server):
    status, data, r = get(hosted_server, "/healthz", "10.60.1.25:8765")
    assert status == 200 and json.loads(data) == {"status": "ok"}
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")


def test_healthz_exception_does_not_open_other_paths(hosted_server):
    assert get(hosted_server, "/api/status", "10.60.1.25:8765")[0] == 421
    assert get(hosted_server, "/", "10.60.1.25:8765")[0] == 421


def test_healthz_reports_a_broken_database(tmp_path):
    httpd = serve(str(tmp_path / "missing-folder" / "x.sqlite"), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, data, _ = get(httpd.server_address[1], "/healthz", "127.0.0.1")
        assert status == 503 and b"sqlite" not in data.lower()
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ deploy files (text checks, not AWS validation)

def test_dockerfile_runs_as_non_root_with_no_installs():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER costlens" in text
    assert re.findall(r"pip install.*", text) == ['pip install --no-cache-dir "anthropic[bedrock]"; fi']  # only when asked for
    assert 'WITH_BEDROCK=false' in text
    assert text.index("USER costlens") < text.index("CMD [\"python\", \"-m\"")


def test_docker_context_leaves_out_everything_but_the_app():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert lines[:2] == ["*", "!costlens"]


def block(name: str) -> str:
    m = re.search(rf"^  {name}:\n((?:    .*\n|\n|  #.*\n)+)", TEMPLATE, re.M)
    assert m, name
    return m.group(1)


def test_server_accepts_traffic_only_from_the_load_balancer():
    sg = block("ServerSecurityGroup")
    assert "SourceSecurityGroupId: !Ref LoadBalancerSecurityGroup" in sg
    assert "CidrIp" not in sg and "FromPort: 22" not in TEMPLATE


def test_every_request_goes_through_login():
    listener = block("HttpsListener")
    assert listener.index("authenticate-cognito") < listener.index("Type: forward")
    assert "OnUnauthenticatedRequest: authenticate" in listener
    assert "Type: redirect" in block("HttpRedirect") and "Type: forward" not in block("HttpRedirect")


def test_template_hardening():
    assert "HttpTokens: required" in TEMPLATE
    assert "Encrypted: true" in TEMPLATE
    assert "AllowAdminCreateUserOnly: true" in TEMPLATE
    assert "RetentionInDays: 30" in TEMPLATE
    assert "AWS::Budgets::Budget" in TEMPLATE


def test_server_role_has_no_broad_or_billing_access():
    role = block("ServerRole")
    assert "AdministratorAccess" not in role and "PowerUser" not in role
    assert not re.search(r"(?<![\w:])(ce|cur|s3|organizations|iam|ec2|budgets):", role)  # action names, not ARNs
    assert role.count('Resource: "*"') == 1  # ecr:GetAuthorizationToken has no resource-level form
    assert "Action: ecr:GetAuthorizationToken\n                Resource: \"*\"" in role


def test_server_can_call_only_the_chosen_model():
    role = block("ServerRole")
    assert "Action: bedrock:InvokeModel" in role and "inference-profile/eu.amazon.nova-lite-v1:0" in role
    assert "foundation-model/amazon.nova-lite-v1:0" in role and "foundation-model/*" not in role
    assert "bedrock:*" not in role.replace("arn:aws:bedrock:*", "")
    assert "HttpPutResponseHopLimit: 2" in TEMPLATE
    assert "COSTLENS_MODEL_PROVIDER=bedrock-nova" in TEMPLATE


def test_load_balancer_waits_longer_than_the_investigator():
    from costlens.agent.investigator import MAX_SECONDS
    seconds = int(re.search(r'idle_timeout.timeout_seconds, Value: "(\d+)"', TEMPLATE).group(1))
    assert seconds > MAX_SECONDS


def test_snapshot_description_has_no_characters_aws_refuses():
    text = re.search(r"Description: (.*?)(?:  #.*)?$", block("DailySnapshots"), re.M).group(1)
    assert re.fullmatch(r"[0-9A-Za-z _-]+", text)


def test_runbook_uses_frankfurt():
    text = (ROOT / "deploy" / "aws" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "eu-central-1" in text and "ap-south-1" not in text


def test_template_passes_the_settings_the_app_needs():
    assert "COSTLENS_BEHIND_AUTH_PROXY=true" in TEMPLATE and "COSTLENS_ALLOWED_HOSTS=${DomainName}" in TEMPLATE
    assert "HealthCheckPath: /healthz" in TEMPLATE


def test_no_secrets_or_real_accounts_in_deploy_files():
    for name in ("costlens-demo.yaml", "costlens-ecr.yaml", "RUNBOOK.md"):
        text = (ROOT / "deploy" / "aws" / name).read_text(encoding="utf-8")
        assert not re.search(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b", text)
        assert not re.search(r"\b\d{12}\b", text)


def test_runbook_is_honest_about_status():
    text = (ROOT / "deploy" / "aws" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "Status: deployed" in text and "Synthetic data only" in text and "explicit approval" in text
