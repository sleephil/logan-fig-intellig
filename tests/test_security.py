from fastapi.testclient import TestClient
from app import app
def test_health():
    assert TestClient(app).get("/health").status_code==200
def test_fail_closed():
    assert TestClient(app).get("/api/platforms").status_code==403
