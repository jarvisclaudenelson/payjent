def test_agent_self_setup_markdown_is_agent_readable(client):
    response = client.get("/docs/agent-payjent-self-setup.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    text = response.text
    assert "# Agent Self-Setup Guide: Teach Yourself to Use Payjent" in text
    assert "C3PO" not in text
    assert "do not ask them for code snippets, environment variables, Payjent agent ids" in text
    assert "I can use Payjent, but I am not connected yet" in text
    assert "register/authorize this agent" in text
    assert "do not invent variable names or manual shell steps" in text
    assert "Do not provide a manual `.env` recipe" in text
    assert "https://payjent.vercel.app/docs/agent-payjent-self-setup.md" in text
    assert "grant_*" in text
    assert "Payjent does not execute pay.sh for you" in text


def test_c3po_specific_doc_url_redirects_to_generic_agent_guide(client):
    response = client.get("/docs/c3po-payjent-self-setup.md", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/docs/agent-payjent-self-setup.md"
