def test_login_missing_credentials(client):
    res = client.post("/auth/login", json={})
    assert res.status_code == 400


def test_login_invalid_credentials(client):
    res = client.post("/auth/login", json={"username": "nobody", "password": "wrong"})
    assert res.status_code == 401


def test_protected_route_without_token(client):
    res = client.get("/api/parking-logs")
    assert res.status_code == 401
