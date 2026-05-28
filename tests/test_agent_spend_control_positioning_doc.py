from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "agent-spend-control-positioning.md"
README_PATH = ROOT / "README.md"
DOC_URL = "/docs/agent-spend-control-positioning.md"


PROHIBITED_SECRET_PASTE_GUIDANCE = [
    "paste your api key",
    "paste api key",
    "paste bearer token",
    "paste payment token",
    "paste private grant",
    "paste one-time credential",
]


def test_agent_spend_control_positioning_route_and_index(client):
    response = client.get(DOC_URL)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "Payjent is not a generic tool search engine" in response.text
    assert "Zero-like discovery" in response.text
    assert "request-bound authorization checkpoint" in response.text

    index = client.get("/docs")
    assert index.status_code == 200
    assert DOC_URL in index.text
    assert "spend-control positioning" in index.text


def test_positioning_doc_and_readme_include_differentiators_without_secret_paste_guidance():
    doc_text = DOC_PATH.read_text()
    readme_text = README_PATH.read_text()
    combined = f"{doc_text}\n{readme_text}"
    combined_lower = combined.lower()

    for term in [
        "spend-control",
        "payment authorization layer",
        "exact provider quote",
        "human/user-funded budgets",
        "request-bound",
        "bounded grant",
        "spend ledger",
        "fulfillment, failure, receipt, and refund evidence",
        "agent-side runtime",
        "not a generic tool search engine",
    ]:
        assert term in combined_lower

    assert "docs/agent-spend-control-positioning.md" in readme_text
    assert "Public docs and chats must not contain provider secrets" in doc_text
    for phrase in PROHIBITED_SECRET_PASTE_GUIDANCE:
        assert phrase not in combined_lower
