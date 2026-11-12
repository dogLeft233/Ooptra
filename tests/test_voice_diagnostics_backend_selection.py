"""Diagnostics must select the same backend aliases as the backend factory."""

import asyncio

import pytest
from test_voice_operations import make_agent

from app import diagnostics
from behavior.models import VoiceControlDecision


@pytest.mark.parametrize("backend", [" MIMO ", " MiMo_Cascade ", " CASCADE_MIMO "])
def test_network_diagnostics_accept_normalized_mimo_aliases(tmp_path, monkeypatch, backend):
    async def classify(self, text):
        return VoiceControlDecision()

    original_factory = diagnostics.create_backend

    def create(*args, **kwargs):
        backend = original_factory(*args, **kwargs)
        backend.detect_control_intent = lambda text: classify(backend, text)
        return backend

    monkeypatch.setattr(diagnostics, "create_backend", create)
    agent = make_agent(tmp_path)
    agent.settings.backend = backend
    agent.settings.mimo_api_key = "diagnostic-test"
    agent.settings.gemini_api_key = ""
    agent.settings.gemini_base_url = ""

    result = asyncio.run(diagnostics.diagnose(agent, network=True))

    checks = {item["id"]: item for item in result["checks"]}
    assert checks["credentials"]["state"] == "pass"
    assert checks["network"]["state"] == "pass"
    assert result["network_checked"]
