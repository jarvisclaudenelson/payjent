from payjent.main import app


def test_c3po_self_setup_markdown_is_agent_readable(client):
    response = client.get("/docs/c3po-payjent-self-setup.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    text = response.text
    assert "# C3PO: Teach Yourself to Use Payjent" in text
    assert "do not ask them for code snippets, environment variables, or raw payment tokens" in text
    assert "I can use Payjent, but I am not execution-ready yet" in text
    assert "https://payjent.vercel.app" in text
    assert "grant_*" in text
    assert "Payjent does not execute pay.sh for you" in text
