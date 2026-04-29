def test_create_valid_quote(client, quote_payload):
    r = client.post("/api/v1/quotes", json=quote_payload)
    assert r.status_code == 200
    data = r.json()
    assert data["id"].startswith("quote_")
    assert data["status"] == "quoted"
    assert data["amount_minor"] == 250
    assert data["quote_hash"]

    got = client.get(f"/api/v1/quotes/{data['id']}")
    assert got.status_code == 200
    assert got.json()["quote_hash"] == data["quote_hash"]


def test_reject_amount_breakdown_mismatch(client, quote_payload):
    quote_payload["cost_breakdown"][0]["amount_minor"] = 199
    r = client.post("/api/v1/quotes", json=quote_payload)
    assert r.status_code == 422
    assert "cost_breakdown" in r.text
