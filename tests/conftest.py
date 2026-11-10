import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Each test session gets a throw-away data directory.
_tmp = tempfile.mkdtemp(prefix="rdr-test-")
os.environ["RDR_DATA_DIR"] = _tmp


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, follow_redirects=False) as c:
        yield c
