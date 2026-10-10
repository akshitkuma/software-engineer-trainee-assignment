
from fastapi.testclient import TestClient
from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_attendance_rejects_invalid_pagination():
    with TestClient(app) as client:
        response = client.get("/attendance?page=0")

    assert response.status_code == 422


def test_attendance_rejects_invalid_page_size():
    with TestClient(app) as client:
        response = client.get("/attendance?page_size=101")

    assert response.status_code == 422
