import shutil
from pathlib import Path

import pytest

from costlens import fixtures, store
from costlens.ingest.governance import load_governance
from costlens.ingest.loader import ingest_source
from costlens.ingest.optimization import ingest_optimization
from costlens.ingest.sources import LocalDirectorySource


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory) -> Path:
    return fixtures.generate(tmp_path_factory.mktemp("fx") / "deliveries")


@pytest.fixture()
def fresh_conn(tmp_path):
    conn = store.connect(tmp_path / "t.sqlite")
    yield conn
    conn.close()


def load_demo(conn, root: Path):
    outcomes = ingest_source(conn, LocalDirectorySource(root))
    ingest_optimization(conn, root / fixtures.OPT_FOLDER)
    load_governance(conn, root / fixtures.GOV_FOLDER)
    store.set_meta(conn, "data_mode", "synthetic")
    store.set_meta(conn, "reference_date", fixtures.REFERENCE_DATE.isoformat())
    store.set_meta(conn, "expected_start_period", fixtures.EXPECTED_START_PERIOD)
    return outcomes


@pytest.fixture()
def demo_conn(fresh_conn, fixture_root):
    load_demo(fresh_conn, fixture_root)
    return fresh_conn


@pytest.fixture()
def demo_db_path(tmp_path, fixture_root) -> Path:
    path = tmp_path / "demo.sqlite"
    conn = store.connect(path)
    load_demo(conn, fixture_root)
    conn.close()
    return path


def copy_deliveries(root: Path, dest: Path, names: list[str]) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        shutil.copytree(root / n, dest / f"{i:03d}-{n}")
    return dest


def total_nanos(conn, period: str | None = None) -> int:
    if period:
        return conn.execute("SELECT COALESCE(SUM(unblended_nanos),0) FROM line_items WHERE billing_period=?",
                            (period,)).fetchone()[0]
    return conn.execute("SELECT COALESCE(SUM(unblended_nanos),0) FROM line_items").fetchone()[0]
