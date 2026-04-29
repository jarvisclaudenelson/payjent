def test_checkout_creation(client, quote_payload):
    q = client.post("/api/v1/quotes", json=quote_payload).json()
    r = client.post(f"/api/v1/quotes/{q['id']}/checkout")
    assert r.status_code == 200
    ps = r.json()
    assert ps["quote_id"] == q["id"]
    assert ps["status"] == "checkout_created"
    assert client.get(f"/api/v1/payment-sessions/{ps['id']}").json()["id"] == ps["id"]


def test_mock_payment_issues_receipt_and_grant(client, quote_payload):
    q = client.post("/api/v1/quotes", json=quote_payload).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout").json()
    r = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay")
    assert r.status_code == 200
    data = r.json()
    assert data["payment_session"]["status"] == "paid"
    assert data["receipt"]["signature"]
    grant = data["grant"]
    assert grant["signature"]
    assert grant["payload"]["quote_id"] == q["id"]
    assert grant["payload"]["amount_minor"] == 250
    assert client.get(f"/api/v1/quotes/{q['id']}").json()["status"] == "paid"


def test_grant_verify_rejects_wrong_presented_fields(client, paid_grant):
    _q, _ps, grant = paid_grant
    gid = grant["id"]
    ok = {"bot_id":"bot-1","external_user_id":"user-1","request_hash":"hash-1"}
    assert client.post(f"/api/v1/grants/{gid}/verify", json=ok).status_code == 200
    for bad in ({"bot_id":"bad"}, {"external_user_id":"bad"}, {"request_hash":"bad"}):
        r = client.post(f"/api/v1/grants/{gid}/verify", json=bad)
        assert r.status_code == 403


def test_consume_replay_rejected(client, paid_grant):
    _q, _ps, grant = paid_grant
    gid = grant["id"]
    presentation = {"bot_id":"bot-1","external_user_id":"user-1","request_hash":"hash-1"}
    r1 = client.post(f"/api/v1/grants/{gid}/consume", json=presentation)
    assert r1.status_code == 200
    assert r1.json()["consumed"] is True
    r2 = client.post(f"/api/v1/grants/{gid}/consume", json=presentation)
    assert r2.status_code == 409
