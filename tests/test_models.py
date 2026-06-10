"""Tests for model discovery & pricing (M23). No network."""

from talos.models import estimate_cost, lookup, parse_models

DB = {
    "gpt-4o-mini": {
        "max_input_tokens": 128000,
        "input_cost_per_token": 0.15e-6,
        "output_cost_per_token": 0.6e-6,
        "supports_vision": True,
    },
    "claude-sonnet-4-5": {
        "max_input_tokens": 200000,
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "supports_vision": True,
    },
}


def test_lookup_strips_provider_prefix():
    assert lookup("anthropic/claude-sonnet-4-5", DB)["max_input_tokens"] == 200000
    assert lookup("nope/unknown", DB) == {}


def test_parse_minimal_openai_payload_enriched_from_db():
    payload = {"data": [{"id": "gpt-4o-mini", "object": "model"}]}
    (m,) = parse_models(payload, DB)
    assert m.context == 128000
    assert round(m.input_per_m, 2) == 0.15   # $/M tokens
    assert m.vision is True


def test_openrouter_fields_win_over_db():
    payload = {"data": [{
        "id": "gpt-4o-mini",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "context_length": 64000,
        "architecture": {"modality": "text+image->text"},
    }]}
    (m,) = parse_models(payload, DB)
    assert m.input_per_m == 1.0 and m.output_per_m == 2.0
    assert m.context == 64000 and m.vision is True


def test_estimate_cost(monkeypatch):
    import talos.models as models_mod

    monkeypatch.setattr(models_mod, "_price_db", lambda: DB)
    cost = estimate_cost("claude-sonnet-4-5", 1000, 100)
    assert abs(cost - (1000 * 3e-6 + 100 * 15e-6)) < 1e-9
    assert estimate_cost("unknown-model", 10, 10) is None
