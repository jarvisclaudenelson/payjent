import pytest

from payjent.providers.paysh import build_execution_envelope


def test_obsolete_paysponge_fal_catalog_target_is_remediated_to_current_mpp_url():
    envelope = build_execution_envelope(
        service_fqn="paysponge/fal",
        resource="fal-ai/fast-sdxl",
        body={"prompt": "Lisbon at sunset"},
    )

    assert envelope["service_url"] == "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl"
    assert envelope["service_fqn"] == "paysponge/fal"
    assert envelope["x402_runtime"] == "sponge"
    assert envelope["settlement"] == "external_x402_runtime"
    assert envelope["command_preview"].startswith("npx spongewallet pay fetch")
    assert "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl" in envelope["command_preview"]
    assert "paysponge/fal:fal-ai/fast-sdxl" not in envelope["command_preview"]
    assert "paycurl" not in envelope["command_preview"]


def test_obsolete_paysponge_fal_with_stale_url_is_rejected_with_actionable_guidance():
    with pytest.raises(ValueError) as excinfo:
        build_execution_envelope(
            service_url="https://fal.x402.paysponge.com/fal-ai/fast-sdxl",
            service_fqn="paysponge/fal",
            resource="fal-ai/fast-sdxl",
            body={"prompt": "Lisbon at sunset"},
        )

    message = str(excinfo.value)
    assert "paysponge/fal is obsolete" in message
    assert "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl" in message
    assert "npx spongewallet pay discover fal" in message


def test_current_fal_mpp_tempo_url_uses_spongewallet_fetch_preview():
    envelope = build_execution_envelope(
        service_url="https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl",
        resource="fal-ai/fast-sdxl",
        body={"prompt": "Lisbon at sunset"},
    )

    assert envelope["service_url"] == "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl"
    assert envelope["x402_runtime"] == "sponge"
    assert envelope["agent_runtime_requirements"]["credential"] == "SPONGE_API_KEY in the agent runtime only"
    assert envelope["command_preview"].startswith("npx spongewallet pay fetch")
    assert "--url https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl" in envelope["command_preview"]
    assert "paycurl" not in envelope["command_preview"]
