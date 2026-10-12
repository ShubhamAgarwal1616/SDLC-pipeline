from pathlib import Path

import pytest

from sdlc.config import ORG_CONFIG_DEFAULT, ConfigError, load_config, parse_overrides


def project(tmp_path: Path, yaml_text: str) -> Path:
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "pipeline.yaml").write_text(yaml_text)
    return tmp_path


def test_org_defaults_load_with_provenance():
    cfg = load_config()
    assert cfg.get("loops.verify_retries") == 3
    assert cfg.explain("loops.verify_retries") == [("loops.verify_retries", 3, "org")]


def test_project_and_run_layers_override_in_order(tmp_path):
    proj = project(tmp_path, "loops:\n  verify_retries: 2\n")
    cfg = load_config(proj, parse_overrides(["loops.verify_retries=1"]))
    assert cfg.get("loops.verify_retries") == 1
    assert cfg.explain("loops.verify_retries")[0][2] == "run"
    assert load_config(proj).explain("loops.verify_retries")[0][2].startswith("project")


def test_fixed_lock_cannot_be_turned_off(tmp_path):
    with pytest.raises(ConfigError, match="gates.mr_review is locked"):
        load_config(project(tmp_path, "gates:\n  mr_review: false\n"))


def test_max_lock_rejects_budget_above_ceiling():
    with pytest.raises(ConfigError, match="exceeds the org ceiling"):
        load_config(overrides={"budgets.per_run_usd": 50})
    assert load_config(overrides={"budgets.per_run_usd": 4.0}).get("budgets.per_run_usd") == 4.0


def test_one_of_lock_rejects_models_off_the_allow_list():
    with pytest.raises(ConfigError, match="not in llm.allowed_models"):
        load_config(overrides={"llm.stages.implement.model": "gpt-9"})


def test_append_only_list_merges_instead_of_replacing(tmp_path):
    cfg = load_config(project(tmp_path, "guardrails:\n  forbidden_paths: [migrations/**]\n"))
    paths = cfg.get("guardrails.forbidden_paths")
    assert "migrations/**" in paths and ".claude/**" in paths


def test_only_org_may_define_locks(tmp_path):
    with pytest.raises(ConfigError, match="only the org config may define locks"):
        load_config(project(tmp_path, "locks: []\n"))


def test_optional_gate_can_be_switched_on_by_a_run():
    assert load_config(overrides={"gates.plan": True}).get("gates.plan") is True


def test_org_file_exists():
    assert ORG_CONFIG_DEFAULT.exists()


def test_env_sets_stage_models_and_extends_the_allow_list():
    cfg = load_config(env={"SDLC_MODEL_DEFAULT": "claude-haiku-5-5", "SDLC_MODEL_REVIEW": "claude-fable-5-1"})
    assert cfg.get("llm.stages.grill.model") == "claude-haiku-5-5"
    assert cfg.get("llm.stages.review.model") == "claude-fable-5-1"
    assert "claude-fable-5-1" in cfg.get("llm.allowed_models")
    assert cfg.explain("llm.stages.review.model")[0][2] == "env (.env)"


def test_project_and_run_still_override_env_models_within_the_allow_list(tmp_path):
    env = {"SDLC_MODEL_IMPLEMENT": "claude-haiku-5-5"}
    proj = project(tmp_path, "llm:\n  stages:\n    implement: { model: claude-opus-5-5 }\n")
    assert load_config(proj, env=env).get("llm.stages.implement.model") == "claude-opus-5-5"
    with pytest.raises(ConfigError, match="not in llm.allowed_models"):
        load_config(overrides={"llm.stages.implement.model": "gpt-9"}, env=env)


def test_env_allow_list_replaces_org_list():
    cfg = load_config(env={"SDLC_ALLOWED_MODELS": "claude-sonnet-5-5, claude-opus-5-5"})
    assert cfg.get("llm.allowed_models") == ["claude-sonnet-5-5", "claude-opus-5-5"]
    with pytest.raises(ConfigError, match="not in llm.allowed_models"):
        load_config(overrides={"llm.stages.review.model": "claude-haiku-5-5"},
                    env={"SDLC_ALLOWED_MODELS": "claude-sonnet-5-5,claude-opus-5-5"})


def test_env_models_are_read_from_dotenv(monkeypatch):
    monkeypatch.setattr("sdlc.secrets._dotenv", lambda: {"SDLC_MODEL_GRILL": "claude-haiku-5-5"})
    assert load_config().get("llm.stages.grill.model") == "claude-haiku-5-5"


def test_resume_keeps_the_frozen_allow_list_when_env_changes():
    from sdlc.config import with_overrides
    frozen = load_config(env={"SDLC_MODEL_REVIEW": "claude-fable-5-1"})
    resumed = with_overrides(frozen, {"budgets.per_stage_usd.review": 5.0}, env={})
    assert resumed.get("llm.stages.review.model") == "claude-fable-5-1"


def test_dotenv_parser_strips_inline_comments_and_quotes(tmp_path, monkeypatch):
    from sdlc import secrets
    (tmp_path / ".env").write_text('A=   # blank\nB=claude-haiku-5-5  # note\nC="x # y"\nD=tok#en\n')
    monkeypatch.undo()  # drop conftest's _dotenv stub; test the real parser
    monkeypatch.setattr(secrets, "HOME", tmp_path)
    secrets._dotenv.cache_clear()
    try:
        assert secrets._dotenv() == {"A": "", "B": "claude-haiku-5-5", "C": "x # y", "D": "tok#en"}
    finally:
        secrets._dotenv.cache_clear()


def test_repo_instructions_are_loaded_per_stage(tmp_path):
    proj = project(tmp_path, "loops:\n  verify_retries: 2\n")
    (proj / ".sdlc" / "instructions").mkdir()
    (proj / ".sdlc" / "instructions" / "implement.md").write_text("Use the Result type for errors.\n")
    (proj / ".sdlc" / "instructions" / "unknown.md").write_text("ignored")
    cfg = load_config(proj, env={})
    assert cfg.get("instructions") == {"implement": "Use the Result type for errors."}
    assert cfg.explain("instructions.implement")[0][2].startswith("project")


def test_oversized_instructions_are_rejected(tmp_path):
    proj = project(tmp_path, "{}\n")
    (proj / ".sdlc" / "instructions").mkdir()
    (proj / ".sdlc" / "instructions" / "review.md").write_text("x" * 9000)
    with pytest.raises(ConfigError, match="keep stage instructions short"):
        load_config(proj, env={})
