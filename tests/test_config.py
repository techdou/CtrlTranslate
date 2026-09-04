from pathlib import Path

from app.config import DEFAULT_CONFIG, PROVIDER_PRESETS, deep_merge, load_config, save_config


def test_deep_merge_nested_override():
    merged = deep_merge(DEFAULT_CONFIG, {"popup": {"theme": "light"}})
    assert merged["popup"]["theme"] == "light"
    assert merged["popup"]["font_size"] == DEFAULT_CONFIG["popup"]["font_size"]
    assert merged["tts"] == DEFAULT_CONFIG["tts"]  # 未触碰的段保持原样


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"b": 1}}
    deep_merge(base, {"a": {"b": 2}})
    assert base["a"]["b"] == 1


def test_save_load_roundtrip(tmp_path: Path):
    p = tmp_path / "config.json"
    save_config({"provider": {"api_key": "sk-test"}}, p)
    cfg = load_config(p)
    assert cfg["provider"]["api_key"] == "sk-test"
    assert cfg["trigger"]["interval_ms"] == DEFAULT_CONFIG["trigger"]["interval_ms"]


def test_load_corrupt_json_falls_back(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{ not valid json !", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == DEFAULT_CONFIG


def test_load_missing_file(tmp_path: Path):
    assert load_config(tmp_path / "none.json") == DEFAULT_CONFIG


def test_provider_presets_shape():
    for name, preset in PROVIDER_PRESETS.items():
        assert preset["base_url"] is not None and preset["label"], name
        if name != "custom":
            assert preset["model"], f"{name} missing default model"
