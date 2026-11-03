import pytest
from fastapi.testclient import TestClient

from hangar.main import app


@pytest.fixture
def client():
    # Host permitido por el middleware de Hangar (TestClient usa "testserver").
    return TestClient(app, base_url="http://127.0.0.1")
