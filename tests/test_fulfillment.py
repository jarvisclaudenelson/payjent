def test_fulfillment_state_transition(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    consumed = client.post(f"/api/v1/grants/{grant['id']}/consume", json={"bot_id": q["bot_id"]}, headers=bot_headers)
    assert consumed.status_code == 200
    r = client.post(f"/api/v1/quotes/{q['id']}/fulfillment", json={"status":"executing", "metadata":{"worker":"w1"}}, headers=bot_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["quote_id"] == q["id"]
    assert data["status"] == "executing"
    assert data["metadata"] == {"worker":"w1"}
    assert client.get(f"/api/v1/quotes/{q['id']}").json()["status"] == "executing"

    r2 = client.post(f"/api/v1/quotes/{q['id']}/fulfillment", json={"status":"fulfilled", "metadata":{"message_id":"m1"}}, headers=bot_headers)
    assert r2.status_code == 200
    assert client.get(f"/api/v1/quotes/{q['id']}").json()["status"] == "fulfilled"


def test_fulfillment_unknown_quote_rejected(client, bot_headers):
    r = client.post("/api/v1/quotes/quote_missing/fulfillment", json={"status":"fulfilled"}, headers=bot_headers)
    assert r.status_code == 404
