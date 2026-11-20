"""Suite 42 -- V2.5 hy4-flash-free single-model lock and live human-QA fixes.

The V2.5 pass integrates the operator-supplied ``opencode-g0-api.txt`` at
runtime ONLY, locks every AI dispatch surface to the single model
``hy4-flash-free``, and adds regressions for every interactive defect found by
the live human-tester Playwright pass.

Non-negotiable contracts asserted here:

* the single model id is ``hy4-flash-free`` everywhere; any other id is either
  coerced to it or rejected with the stable code ``MODEL_NOT_ALLOWED``;
* ``load_llm_api_config`` parses free-form prose (key + model hint), never
  leaks the raw key through ``repr``/``str``/serialisation, and masks it as
  ``***last4`` in every payload;
* offline defaults stay deterministic -- no live key, no live endpoint, no
  network probe is activated by importing the transport;
* the singular ``/api/v2/control/llm/probe`` alias exists alongside the
  historical plural route;
* reasoning-field (``reasoning_content``) responses are parsed without
  breaking JSON extraction or the numerical grounding verifier;
* the working tree contains no raw ``oc_sk_`` key material.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from core.llm_model_policy import (
    ALLOWED_MODEL,
    MODEL_NOT_ALLOWED,
    ModelNotAllowed,
    coerce_model,
    enforce_model,
    is_allowed_model,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE_NAME = "test_v2_5_hy4_flash_config_and_live_qa_fixes.py"

DEFAULT_API_FILE = Path(r"C:\Users\nahas\OneDrive\Desktop\opencode-g0-api.txt")

# Synthetic key -- NEVER a real credential.  Prefix matches the observed shape.
SYNTHETIC_KEY = "oc_sk_" + "A" * 44 + "wXyZ"
SYNTHETIC_FILE_TEXT = (
    "opencode g0 api key is " + SYNTHETIC_KEY + "\n\n\nuse this model -- hy4-flash-free\n"
)


# ---------------------------------------------------------------------------
# 1. Single-model lock
# ---------------------------------------------------------------------------


def test_allowed_model_is_exactly_hy4_flash_free() -> None:
    assert ALLOWED_MODEL == "hy4-flash-free"


def test_model_not_allowed_code_is_stable() -> None:
    assert MODEL_NOT_ALLOWED == "MODEL_NOT_ALLOWED"
    assert ModelNotAllowed.code == MODEL_NOT_ALLOWED


@pytest.mark.parametrize(
    "model",
    [
        "hy4-flash-free",
    ],
)
def test_is_allowed_model_true_for_target(model: str) -> None:
    assert is_allowed_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "hy4-preview",
        "deepseek-r1",
        "claude-sonnet-4-5",
        "claude-opus-4-5",
        "openrouter/auto",
        "qwen2.5:3b",
        "llama3.2:3b",
        "gemini-2.5-flash-lite",
        "groq/llama-3.3-70b-versatile",
        "",
        None,
    ],
)
def test_is_allowed_model_false_for_everything_else(model: Any) -> None:
    assert is_allowed_model(model) is False


def test_enforce_model_accepts_target() -> None:
    assert enforce_model("hy4-flash-free") == "hy4-flash-free"


@pytest.mark.parametrize(
    "model",
    ["hy4-preview", "deepseek-r1", "openrouter/auto", "jev-1.13-free"],
)
def test_enforce_model_rejects_other_models(model: str) -> None:
    with pytest.raises(ModelNotAllowed) as exc:
        enforce_model(model)
    assert exc.value.code == MODEL_NOT_ALLOWED
    assert model in str(exc.value)


@pytest.mark.parametrize(
    "model",
    ["hy4-preview", "deepseek-r1", "claude-opus-4-5", "", None],
)
def test_coerce_model_overrides_to_target(model: Any) -> None:
    assert coerce_model(model) == "hy4-flash-free"


def test_model_policy_error_never_contains_key_material() -> None:
    # The policy only ever sees a *model id*, never an API key.  Rejecting a
    # foreign model must not echo anything resembling key material.
    err = ModelNotAllowed("hy4-preview")
    assert "oc_sk_" not in str(err)
    assert "MODEL_NOT_ALLOWED" in str(err)


# ---------------------------------------------------------------------------
# 2. Runtime API-config loader
# ---------------------------------------------------------------------------


def _loader():
    from core.llm_api_config import load_llm_api_config

    return load_llm_api_config


def test_load_api_config_parses_free_form_prose(tmp_path: Path) -> None:
    cfg_file = tmp_path / "opencode-g0-api.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    cfg = _loader()(file_path=str(cfg_file))
    assert cfg.api_key == SYNTHETIC_KEY
    assert cfg.model == "hy4-flash-free"


def test_load_api_config_detects_model_hint(tmp_path: Path) -> None:
    cfg_file = tmp_path / "api.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    cfg = _loader()(file_path=str(cfg_file))
    assert cfg.model == "hy4-flash-free"


def test_load_api_config_default_model_when_no_hint(tmp_path: Path) -> None:
    cfg_file = tmp_path / "api.txt"
    cfg_file.write_text("key is " + SYNTHETIC_KEY + "\n", encoding="utf-8")
    cfg = _loader()(file_path=str(cfg_file))
    assert cfg.model == "hy4-flash-free"


def test_masked_key_uses_last4_only(tmp_path: Path) -> None:
    cfg_file = tmp_path / "api.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    cfg = _loader()(file_path=str(cfg_file))
    masked = cfg.masked_key
    assert masked.endswith("wXyZ")
    assert masked.startswith("***")
    assert SYNTHETIC_KEY not in masked
    assert "A" * 44 not in masked


def test_repr_and_str_never_leak_key(tmp_path: Path) -> None:
    cfg_file = tmp_path / "api.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    cfg = _loader()(file_path=str(cfg_file))
    for rendered in (repr(cfg), str(cfg), json.dumps(cfg.to_dict()) if hasattr(cfg, "to_dict") else "{}"):
        assert SYNTHETIC_KEY not in rendered
        assert "A" * 44 not in rendered


def test_env_path_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "custom.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    monkeypatch.setenv("NIOS_LLM_API_FILE", str(cfg_file))
    cfg = _loader()()
    assert cfg.api_key == SYNTHETIC_KEY


def test_missing_api_file_raises_clean_error(tmp_path: Path) -> None:
    with pytest.raises(Exception) as exc:
        _loader()(file_path=str(tmp_path / "missing.txt"))
    assert SYNTHETIC_KEY not in str(exc.value)


def test_real_default_file_constant_does_not_embed_key() -> None:
    from core import llm_api_config

    src = (Path(llm_api_config.__file__)).read_text(encoding="utf-8")
    assert "oc_sk_" + "A" not in src
    # No 40+ char high-entropy literal in source.
    assert not re.search(r"oc_sk_[A-Za-z0-9_-]{40,}", src)


# ---------------------------------------------------------------------------
# 3. Offline determinism / air-gap
# ---------------------------------------------------------------------------


def test_offline_default_provider_specs_unchanged() -> None:
    from core.llm_transport import default_provider_specs

    specs = default_provider_specs()
    names = {s.name for s in specs}
    assert names, "offline provider catalogue must remain populated"
    assert "qwen2.5:3b" in names or any("qwen" in n for n in names)


def test_live_flag_defaults_off() -> None:
    from core import llm_api_config

    assert llm_api_config.is_live_enabled() is False


def test_config_payload_never_carries_raw_key(tmp_path: Path) -> None:
    from core import llm_api_config

    cfg_file = tmp_path / "api.txt"
    cfg_file.write_text(SYNTHETIC_FILE_TEXT, encoding="utf-8")
    cfg = llm_api_config.load_llm_api_config(file_path=str(cfg_file))
    payload = llm_api_config.config_payload(cfg)
    assert payload is not None
    assert SYNTHETIC_KEY not in json.dumps(payload)
    assert payload["api_key_masked"] == "***wXyZ"
    assert "api_key" not in payload


# ---------------------------------------------------------------------------
# 4. Surface contracts (source-level, deterministic)
# ---------------------------------------------------------------------------


def test_singular_probe_alias_registered() -> None:
    cp = (ROOT / "ring3_cockpit" / "gateway" / "control_plane.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/v2/control/llm/probe"' in cp


def test_plural_probe_route_still_registered() -> None:
    cp = (ROOT / "ring3_cockpit" / "gateway" / "control_plane.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/v2/control/llms/probe"' in cp


def test_reasoning_content_response_parsing() -> None:
    from core.llm_transport import extract_completion_text

    payload = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "thinking step",
                    "content": '{"verdict": "ok"}',
                }
            }
        ]
    }
    assert extract_completion_text(payload) == '{"verdict": "ok"}'


def test_reasoning_only_response_parsing() -> None:
    from core.llm_transport import extract_completion_text

    payload = {"choices": [{"message": {"reasoning_content": "only reasoning"}}]}
    assert extract_completion_text(payload) == "only reasoning"


def test_content_list_block_response_parsing() -> None:
    from core.llm_transport import extract_completion_text

    payload = {
        "choices": [
            {
                "message": {
                    "content": [{"type": "text", "text": '{"x": 1}'}],
                }
            }
        ]
    }
    assert extract_completion_text(payload) == '{"x": 1}'


def test_json_extraction_survives_reasoning_field() -> None:
    from core.llm_transport import extract_completion_text

    payload = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "analysis",
                    "content": '```json\n{"net_bps": 3.2}\n```',
                }
            }
        ]
    }
    text = extract_completion_text(payload)
    assert "net_bps" in text


# ---------------------------------------------------------------------------
# 5. Secret hygiene
# ---------------------------------------------------------------------------


def test_no_raw_key_tokens_in_tracked_source() -> None:
    pattern = re.compile(r"oc_sk_[A-Za-z0-9_-]{40,}")
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(".git/") or rel.startswith("node_modules/"):
            continue
        if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".json", ".md", ".txt", ".toml", ".css"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(text):
            offenders.append(rel)
    assert offenders == [], f"raw key material in: {offenders}"


def test_api_file_lives_outside_repository() -> None:
    assert DEFAULT_API_FILE.parent != ROOT
    assert not str(DEFAULT_API_FILE).startswith(str(ROOT))


def test_suite_is_registered_as_number_42() -> None:
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    assert SUITE_NAME in runbook
    assert "42 suites" in runbook
