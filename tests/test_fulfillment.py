def test_fulfillment_state_transition(client, paid_grant):
    q, _ps, _grant = paid_grant
    r = client.post(f"/api/v1/quotes/{q['id']}/fulfillment", json={"status":"executing", "metadata":{"worker":"w1"}})
    assert r.status_code == 200
    data = r.json()
    assert data["quote_id"] == q["id"]
    assert data["status"] == "executing"
    assert data["metadata"] == {"worker":"w1"}
    assert client.get(f"/api/v1/quotes/{q['id']}").json()["status"] == "executing"

    r2 = client.post(f"/api/v1/quotes/{q['id']}/fulfillment", json={"status":"fulfilled", "metadata":{"message_id":"m1"}})
    assert r2.status_code == 200
    assert client.get(f"/api/v1/quotes/{q['id']}").json()["status"] == "fulfilled"


def test_fulfillment_unknown_quote_rejected(client):
    r = client.post("/api/v1/quotes/quote_missing/fulfillment", json={"status":"fulfilled"})
    assert r.status_code == 404
