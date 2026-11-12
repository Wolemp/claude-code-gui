"""Tests for the model catalog, launch-option spec, and argv builders.

These cover the parts a bad edit would break silently: a model id that the
CLI would reject, a launch flag that doesn't exist, a sanitizer that lets a
stale key through to argv, or a second `--add-dir` that drops the project path.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as m  # noqa: E402


# --- Model catalog -----------------------------------------------------------

def test_default_model_is_in_catalog():
    assert m.DEFAULT_MODEL in {mid for mid, _ in m.MODELS}


def test_catalog_has_no_duplicate_ids():
    ids = [mid for mid, _ in m.MODELS]
    assert len(ids) == len(set(ids)), "duplicate model id in catalog"


def test_catalog_excludes_retired_models():
    # These 404 at the API — a tab pinned to one cannot start.
    retired = {
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-20240620",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    }
    assert not (retired & {mid for mid, _ in m.MODELS})


def test_every_model_id_passes_the_exec_validator():
    """`opus[1m]` etc. must survive _validate_cmd_fields, which gates spawn."""
    for mid, _label in m.MODELS:
        tab = m.ChatTab(id="t", model=mid)
        assert m._validate_cmd_fields(tab), f"{mid} rejected by validator"


def test_model_groups_cover_the_flat_catalog():
    grouped = [mid for _gid, grp in m.MODEL_GROUPS for mid, _ in grp]
    assert sorted(grouped) == sorted(mid for mid, _ in m.MODELS)


# --- _resolve_model ----------------------------------------------------------

def test_resolve_model_exact_id():
    assert m._resolve_model("claude-opus-5-5") == "claude-opus-5-5"


def test_resolve_model_display_label():
    assert m._resolve_model("Opus 5.5") == "claude-opus-5-5"
    assert m._resolve_model("Sonnet 4.6") == "claude-sonnet-4-6"


def test_resolve_model_loose_spellings():
    for text in ("opus5.5", "OPUS 5-5", "opus_5_5", "  Opus5.5  "):
        assert m._resolve_model(text) == "claude-opus-5-5", text


def test_resolve_model_bare_family_maps_to_the_cli_alias():
    # The alias tracks whatever is newest, which beats pinning a version that
    # goes stale at the next release.
    for fam in ("opus", "sonnet", "haiku", "fable", "best", "default"):
        assert m._resolve_model(fam) == fam, fam


def test_resolve_model_bracket_aliases():
    assert m._resolve_model("opus[1m]") == "opus[1m]"
    assert m._resolve_model("opusplan") == "opusplan"


def test_resolve_model_rejects_unknown_and_empty():
    for text in ("", "   ", "gpt-4", "opus9000", "claude-3-opus-20240229"):
        assert m._resolve_model(text) == "", text


def test_resolve_model_output_is_always_settable():
    """Whatever it returns must pass set_tab_model's catalog check."""
    catalog = {mid for mid, _ in m.MODELS}
    for text in ("opus", "Opus 5.5", "claude-sonnet-5-5", "opus[1m]", "fable"):
        got = m._resolve_model(text)
        assert got in catalog, f"{text} -> {got} not in catalog"


# --- Effort / permission modes ----------------------------------------------

def test_effort_levels_match_cli_values():
    ids = {e[0] for e in m.EFFORT_LEVELS}
    # The five the --effort flag accepts, plus the GUI's "omit the flag" value.
    assert ids == {"low", "medium", "high", "xhigh", "max", "auto"}


def test_permission_modes_passed_to_cli_exclude_the_gui_only_one():
    assert "custom" not in m._CLI_PERMISSION_MODES
    assert m._CLI_PERMISSION_MODES == {
        "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions",
    }


# --- Launch option spec ------------------------------------------------------

def test_launch_option_keys_are_unique():
    keys = [o["key"] for o in m.LAUNCH_OPTIONS]
    assert len(keys) == len(set(keys))


def test_launch_option_kinds_are_known():
    for o in m.LAUNCH_OPTIONS:
        assert o["kind"] in {"bool", "text", "select", "list", "optional"}, o


def test_launch_flags_look_like_flags():
    for o in m.LAUNCH_OPTIONS:
        assert re.fullmatch(r"--[a-zA-Z][a-zA-Z-]*", o["flag"]), o


def test_defaults_cover_every_key():
    assert set(m._LAUNCH_DEFAULTS) == {o["key"] for o in m.LAUNCH_OPTIONS}


# --- sanitize_launch ---------------------------------------------------------

def test_sanitize_launch_fills_defaults_for_missing_keys():
    out = m.sanitize_launch({})
    assert out == m._LAUNCH_DEFAULTS
    assert out is not m._LAUNCH_DEFAULTS  # must be a copy


def test_sanitize_launch_drops_unknown_keys():
    out = m.sanitize_launch({"not_a_real_option": "boom", "ide": True})
    assert "not_a_real_option" not in out
    assert out["ide"] is True


def test_sanitize_launch_handles_garbage_input():
    for bad in (None, [], "string", 42):
        assert m.sanitize_launch(bad) == m._LAUNCH_DEFAULTS


def test_sanitize_launch_filters_unknown_tool_names():
    out = m.sanitize_launch({"tools": ["Read", "NotATool", "Bash"]})
    assert out["tools"] == ["Read", "Bash"]


def test_sanitize_launch_rejects_out_of_range_select_value():
    out = m.sanitize_launch({"system_prompt_snapshot": "maybe"})
    assert out["system_prompt_snapshot"] == ""
    assert m.sanitize_launch({"system_prompt_snapshot": "on"})["system_prompt_snapshot"] == "on"


def test_sanitize_launch_coerces_space_separated_string_to_list():
    out = m.sanitize_launch({"betas": "beta-a beta-b"})
    assert out["betas"] == ["beta-a", "beta-b"]


def test_sanitize_launch_caps_list_length():
    out = m.sanitize_launch({"betas": [f"b{i}" for i in range(500)]})
    assert len(out["betas"]) == m._LAUNCH_MAX_ITEMS


# --- _launch_flags -----------------------------------------------------------

def _tab(**launch):
    return m.ChatTab(id="t", launch=m.sanitize_launch(launch))


def test_launch_flags_empty_by_default():
    assert m._launch_flags(m.ChatTab(id="t"), interactive=True) == []


def test_launch_flags_bool_emits_bare_flag():
    argv = m._launch_flags(_tab(ide=True), interactive=True)
    assert argv == ["--ide"]


def test_launch_flags_text_emits_flag_and_value():
    argv = m._launch_flags(_tab(agent="reviewer"), interactive=True)
    assert argv == ["--agent", "reviewer"]


def test_launch_flags_list_emits_one_flag_then_values():
    argv = m._launch_flags(_tab(tools=["Read", "Bash"]), interactive=True)
    assert argv == ["--tools", "Read", "Bash"]


def test_launch_flags_optional_1_means_bare_flag():
    assert m._launch_flags(_tab(worktree="1"), interactive=True) == ["--worktree"]
    assert m._launch_flags(_tab(worktree="feat"), interactive=True) == ["--worktree", "feat"]


def test_launch_flags_hides_print_only_flags_from_the_pty_path():
    tab = _tab(max_budget_usd="5")
    assert "--max-budget-usd" not in m._launch_flags(tab, interactive=True)
    assert m._launch_flags(tab, interactive=False) == ["--max-budget-usd", "5"]


def test_launch_flags_never_emits_add_dir():
    """--add-dir is variadic; it must come only from _add_dir_args."""
    tab = _tab(extra_dirs=["/a", "/b"])
    assert "--add-dir" not in m._launch_flags(tab, interactive=True)


def test_launch_flags_never_emits_removed_max_turns():
    tab = m.ChatTab(id="t", max_turns=25)
    assert "--max-turns" not in m._launch_flags(tab, interactive=True)


# --- _permission_mode_args ---------------------------------------------------

def test_permission_mode_default_emits_nothing():
    """The CLI's choice list has no "default" — its spelling is "manual", and
    omitting the flag already selects it. Passing it exits with an error."""
    assert m._permission_mode_args(m.ChatTab(id="t", permission_mode="default")) == []


def test_permission_mode_custom_emits_nothing():
    # GUI-only mode: expressed via --allowedTools, not --permission-mode.
    assert m._permission_mode_args(m.ChatTab(id="t", permission_mode="custom")) == []


def test_permission_mode_real_modes_are_emitted():
    for mode in ("acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"):
        tab = m.ChatTab(id="t", permission_mode=mode)
        assert m._permission_mode_args(tab) == ["--permission-mode", mode]


def test_permission_mode_unknown_value_emits_nothing():
    assert m._permission_mode_args(m.ChatTab(id="t", permission_mode="bogus")) == []


def test_permission_mode_never_emits_a_value_the_cli_rejects():
    """Guards against regressing to `--permission-mode default`."""
    # The exact choice list reported by claude 2.1.310.
    cli_choices = {"acceptEdits", "auto", "bypassPermissions", "manual",
                   "dontAsk", "plan"}
    for mode, _label in m.PERMISSION_MODES:
        argv = m._permission_mode_args(m.ChatTab(id="t", permission_mode=mode))
        if argv:
            assert argv[1] in cli_choices, f"{mode} -> {argv[1]} not a CLI choice"


# --- _add_dir_args -----------------------------------------------------------

def test_add_dir_args_merges_project_and_extras_into_one_flag():
    tab = m.ChatTab(id="t", project_path="/proj",
                    launch=m.sanitize_launch({"extra_dirs": ["/x", "/y"]}))
    argv = m._add_dir_args(tab)
    assert argv == ["--add-dir", "/proj", "/x", "/y"]
    assert argv.count("--add-dir") == 1


def test_add_dir_args_dedupes_the_project_path():
    tab = m.ChatTab(id="t", project_path="/proj",
                    launch=m.sanitize_launch({"extra_dirs": ["/proj", "/x"]}))
    assert m._add_dir_args(tab) == ["--add-dir", "/proj", "/x"]


def test_add_dir_args_empty_when_nothing_to_add():
    assert m._add_dir_args(m.ChatTab(id="t")) == []


# --- Session toggles ---------------------------------------------------------

def test_session_toggles_off_by_default():
    assert m._session_toggle_cmds(m.ChatTab(id="t")) == []


def test_session_toggle_cmds_for_enabled_toggles():
    tab = m.ChatTab(id="t", toggles=m.sanitize_toggles(
        {"ultracode": True, "fast_mode": True, "output_style": "Concise"}))
    cmds = m._session_toggle_cmds(tab)
    assert "/effort ultracode on" in cmds
    assert "/fast on" in cmds
    assert "/output-style Concise" in cmds


def test_sanitize_toggles_rejects_unknown_output_style():
    out = m.sanitize_toggles({"output_style": "Shakespearean"})
    assert out["output_style"] == ""


def test_sanitize_toggles_drops_unknown_keys():
    out = m.sanitize_toggles({"nope": True, "fast_mode": True})
    assert "nope" not in out and out["fast_mode"] is True


# --- Tool catalog ------------------------------------------------------------

def test_tool_groups_have_no_duplicates():
    assert len(m.CLI_TOOLS) == len(set(m.CLI_TOOLS))


def test_tool_catalog_excludes_removed_tool():
    # TaskOutput was removed in v2.1.277.
    assert "TaskOutput" not in m._VALID_TOOLS


def test_core_tools_present():
    for name in ("Read", "Write", "Edit", "Bash", "Glob", "Grep",
                 "WebFetch", "WebSearch", "Agent", "Skill", "TodoWrite"):
        assert name in m._VALID_TOOLS, name


# --- ChatTab persistence round-trip -----------------------------------------

def test_chattab_roundtrip_preserves_launch_and_toggles():
    tab = m.ChatTab(id="t1", model="claude-opus-5-5",
                    launch=m.sanitize_launch({"ide": True, "agent": "rev"}),
                    toggles=m.sanitize_toggles({"fast_mode": True}),
                    remote_control=False)
    back = m.ChatTab.deserialize(tab.serialize())
    assert back.launch["ide"] is True
    assert back.launch["agent"] == "rev"
    assert back.toggles["fast_mode"] is True
    assert back.remote_control is False


def test_deserialize_repairs_a_retired_model():
    back = m.ChatTab.deserialize({"id": "t", "model": "claude-3-opus-20240229"})
    assert back.model == m.DEFAULT_MODEL


def test_deserialize_repairs_an_unknown_permission_mode():
    back = m.ChatTab.deserialize({"id": "t", "permission_mode": "whatever"})
    assert back.permission_mode == "default"


def test_deserialize_repairs_an_unknown_effort():
    back = m.ChatTab.deserialize({"id": "t", "effort": "turbo"})
    assert back.effort in m._VALID_EFFORTS


def test_deserialize_tolerates_a_legacy_tab_with_no_launch_keys():
    legacy = {"id": "t", "name": "x", "model": "claude-opus-4-8",
              "effort": "high", "max_turns": 10, "permission_mode": "plan"}
    back = m.ChatTab.deserialize(legacy)
    assert back.launch == m._LAUNCH_DEFAULTS
    assert back.toggles == m._TOGGLE_DEFAULTS
    assert back.model == "claude-opus-4-8"
    assert back.permission_mode == "plan"


# --- Version parsing ---------------------------------------------------------

def test_cli_version_regex_extracts_semver_from_cli_output():
    assert re.search(r"\d+\.\d+\.\d+", "2.1.310 (Claude Code)").group(0) == "2.1.310"
