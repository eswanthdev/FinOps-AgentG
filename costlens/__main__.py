"""Command line: python -m costlens <command>

  demo               generate synthetic fixtures, ingest them and start the app
  generate-fixtures  write synthetic CUR 2.0 deliveries to a folder
  ingest             ingest deliveries from a local folder into a database
  status             print data coverage and delivery outcomes
  serve              start the local web app on an existing database
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import fixtures, hosting
from .agent.verify import load_savings_scenario
from .ingest.governance import load_governance
from .ingest.loader import ingest_source
from .ingest.optimization import ingest_optimization
from .ingest.sources import LocalDirectorySource, SourceUnavailable
from .server import serve
from .status import data_status
from .store import connect, purge_audit, set_meta


def _ingest(db: Path, source: Path, synthetic: bool) -> list:
    conn = connect(db)
    try:
        outcomes = ingest_source(conn, LocalDirectorySource(source))
        outcomes += ingest_optimization(conn, source / fixtures.OPT_FOLDER)
        outcomes += load_governance(conn, source / fixtures.GOV_FOLDER)
        if synthetic:
            set_meta(conn, "data_mode", "synthetic")
            set_meta(conn, "reference_date", fixtures.REFERENCE_DATE.isoformat())
            set_meta(conn, "expected_start_period", fixtures.EXPECTED_START_PERIOD)
        else:
            set_meta(conn, "data_mode", "customer-local")
        from .budgets import evaluate
        for n in evaluate(conn):
            print(f"  budget alert: {n['budget']} {n['threshold']} -> {n['status']}")
        return outcomes
    finally:
        conn.close()


def _print_outcomes(outcomes) -> None:
    for o in outcomes:
        print(f"  {o.status:<20} {o.billing_period:<8} {o.execution_id:<16} rows={o.row_count:<6} {o.detail}")


def _run_server(db: Path, host: str, port: int, label: str) -> None:
    conn = connect(db)
    removed = purge_audit(conn, int(os.environ.get("COSTLENS_AUDIT_RETENTION_DAYS", "90")))
    conn.close()
    if removed:
        print(f"Purged {removed} audit events past the retention window.")
    if not hosting.bind_allowed(host):
        print("Refusing to bind to a non-local address: the app has no login of its own. It needs "
              "COSTLENS_BEHIND_AUTH_PROXY=true and COSTLENS_ALLOWED_HOSTS. See deploy/aws/RUNBOOK.md.", file=sys.stderr)
        sys.exit(2)
    httpd = serve(str(db), host, port, label)
    print(f"CostLens running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=hosting.log_level(), format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="costlens")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="synthetic demo: fixtures + ingest + serve")
    d.add_argument("--data-dir", default="var")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--no-serve", action="store_true")
    d.add_argument("--reset-db", action="store_true", help="delete the demo database file first")
    d.add_argument("--scenario", choices=["savings"], help="savings: a separate demo with billing after recorded changes")

    g = sub.add_parser("generate-fixtures")
    g.add_argument("--out", required=True)

    i = sub.add_parser("ingest")
    i.add_argument("--source", required=True)
    i.add_argument("--db", required=True)
    i.add_argument("--synthetic", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("--db", required=True)

    sv = sub.add_parser("serve")
    sv.add_argument("--db", required=True)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)

    e = sub.add_parser("eval-agent", help="run the evaluation questions. Calls a paid model.")
    e.add_argument("--db", required=True)

    a = p.parse_args(argv)
    if a.cmd == "eval-agent":
        from .agent import agent_eval, investigator
        client = investigator.make_client()
        if client is None:
            print("No model is configured. Set COSTLENS_MODEL_PROVIDER=bedrock and install the bedrock extra.", file=sys.stderr)
            return 1
        conn = connect(a.db)
        report = agent_eval.run(conn, client)
        conn.close()
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] == report["total"] else 1
    if a.cmd == "generate-fixtures":
        out = fixtures.generate(a.out)
        print(f"Synthetic fixtures in {out}")
        return 0
    if a.cmd == "demo":
        data_dir = Path(a.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        db = data_dir / ("costlens-savings.sqlite" if a.scenario else "costlens-demo.sqlite")
        if a.reset_db:
            for suffix in ("", "-wal", "-shm"):
                Path(str(db) + suffix).unlink(missing_ok=True)
        fx = fixtures.generate(data_dir / "fixtures")
        print(f"Ingesting synthetic deliveries from {fx}")
        _print_outcomes(_ingest(db, fx, synthetic=True))
        if a.scenario:
            conn = connect(db)
            try:
                _print_outcomes(load_savings_scenario(conn, fx))
            finally:
                conn.close()
        if a.no_serve:
            return 0
        _run_server(db, a.host, a.port, "Synthetic fixtures (local folder)")
        return 0
    if a.cmd == "ingest":
        try:
            _print_outcomes(_ingest(Path(a.db), Path(a.source), a.synthetic))
        except SourceUnavailable as exc:
            print(f"Source unavailable: {exc}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "status":
        conn = connect(a.db)
        print(json.dumps(data_status(conn), indent=2, default=str))
        conn.close()
        return 0
    if a.cmd == "serve":
        _run_server(Path(a.db), a.host, a.port, "local folder")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
