"""Model registry: loading, validation and queries."""

import json

import pytest

from app.classifier.types import ComplexityLevel
from app.routing.registry import DEFAULT_REGISTRY_FILE, ModelRegistry, RegistryError, load_registry
from tests.conftest import model


def test_default_registry_loads_with_all_tiers():
    reg = load_registry(None)
    tiers = {m.tier for m in reg.enabled()}
    assert tiers == {ComplexityLevel.SIMPLE, ComplexityLevel.MEDIUM, ComplexityLevel.COMPLEX}


def test_default_registry_pricing_is_labelled_placeholder():
    reg = load_registry(None)
    assert all(m.pricing_is_placeholder for m in reg.all())
    assert "PLACEHOLDER" in json.loads(DEFAULT_REGISTRY_FILE.read_text())["_comment"]


def test_default_registry_has_a_disabled_model():
    reg = load_registry(None)
    legacy = reg.get("mock-legacy")
    assert legacy is not None and not legacy.enabled
    assert legacy not in reg.enabled()


def test_example_registry_is_valid_and_disabled_by_default(tmp_path):
    from app.core.config import PROJECT_ROOT

    reg = load_registry(PROJECT_ROOT / "backend" / "config" / "models.example.json")
    assert len(reg) >= 3
    assert reg.enabled() == []
    assert all(m.pricing_is_placeholder for m in reg.all())


def test_model_config_fields():
    m = model("x", "MEDIUM", capabilities=("chat", "coding"), priority=5, enabled=False)
    assert m.tier == ComplexityLevel.MEDIUM
    assert m.capabilities == frozenset({"chat", "coding"})
    assert m.blended_cost_per_million == 3.0
    assert not m.enabled


def test_enabled_filter_by_tier(three_tier_models):
    reg = ModelRegistry(three_tier_models)
    assert [m.id for m in reg.enabled(ComplexityLevel.MEDIUM)] == ["balanced"]
    assert "fast" in reg and "nope" not in reg


@pytest.mark.parametrize(
    "bad",
    [
        {"max_output_tokens": 9000},  # > max_context
        {"fallbacks": ("a",)},  # self fallback
        {"input_cost_per_million": -1},
        {"id": "bad id with spaces"},
        {"tier": "HUGE"},
        {"unknown_field": 1},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_model_definitions_rejected(bad):
    with pytest.raises(ValueError):
        model("a", **bad)


def test_duplicate_ids_rejected():
    with pytest.raises(RegistryError, match="duplicate"):
        ModelRegistry([model("a"), model("a")])


def test_unknown_fallback_rejected():
    with pytest.raises(RegistryError, match="unknown fallback"):
        ModelRegistry([model("a", fallbacks=("ghost",))])


def test_empty_registry_rejected():
    with pytest.raises(RegistryError):
        ModelRegistry([])


def test_from_file_errors(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        ModelRegistry.from_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RegistryError, match="not valid JSON"):
        ModelRegistry.from_file(bad)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"models": [{"id": "x"}]}))
    with pytest.raises(RegistryError, match="invalid model definition"):
        ModelRegistry.from_file(wrong)


def test_custom_registry_file_via_settings(tmp_path):
    from app.core.config import Settings
    from app.routing.factory import build_chat_service

    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [model("only-one").model_dump(mode="json")]}))
    service = build_chat_service(Settings(_env_file=None, llm_mock_mode=True, model_registry_file=path))
    assert [m.id for m in service.policy.registry.all()] == ["only-one"]
