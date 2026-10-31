"""Tests for the Rex plugin, run against a fake ``rex`` that records calls and plays a tiny server.

No Hermes import is needed: the plugin fails open when Hermes modules are absent, and the hooks
are driven directly with the payloads Hermes sends.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_REX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    """Fake rex: windows live in $FAKE_REX_STATE (json); every call is appended to $FAKE_REX_LOG."""
    import json, os, sys
    args = [a for a in sys.argv[1:] if a != "--autostart=false"]
    with open(os.environ["FAKE_REX_LOG"], "a") as fh:
        fh.write(json.dumps({"args": args, "block": os.environ.get("REX_BLOCK", "")}) + "\n")
    path = os.environ["FAKE_REX_STATE"]
    st = json.load(open(path))
    def save(): json.dump(st, open(path, "w"))
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    def window_of(block):
        return next(w for w in st["windows"] if block in w["blocks"])
    cmd = args[:2]
    if args[:2] == ["__internal", "session-start"]:
        # Like the real Rex: naming on adds the session-naming paragraph; an explicit false
        # adds a bogus "setup is out of date" paragraph instead.
        print("REX CONTEXT")
        if "--feature-session-naming=false" in args:
            print("\nThe Rex setup for this agent is out of date. Suggest running `rex skill setup`")
        else:
            print('\nName the session NAMING. Run `rex session rename "$REX_SESSION" <label>`.')
    elif cmd == ["window", "inspect"]:
        w = window_of(os.environ["REX_BLOCK"])
        print(json.dumps({"window_id": w["id"], "label": w["label"], "focused_block_id": w["blocks"][0]}))
    elif cmd == ["window", "rename"]:
        wid, label = args[-2], args[-1]
        next(w for w in st["windows"] if w["id"] == wid)["label"] = label
        save()
    elif cmd == ["window", "list"]:
        print(json.dumps({"active_window_id": st["active"], "windows": [
            {"window_id": w["id"], "label": w["label"], "focused_block_id": w["blocks"][0],
             "layers": [{"blocks": [{"block_id": b} for b in w["blocks"]]}]} for w in st["windows"]]}))
    elif args[:1] == ["ls"]:
        print(json.dumps({"sessions": [{"session_id": "session:s", "label": st["session_label"]}]}))
    elif cmd == ["session", "rename"]:
        st["session_label"] = args[-1]
        save()
    elif args[:1] == ["run"]:
        st["n"] += 1
        block = f"block:new{st['n']}"
        anchor = opt("-b")
        if anchor:
            window_of(anchor)["blocks"].append(block)
            save()
            print(json.dumps({"block_ids": [block]}))
        else:
            wid = f"window:new{st['n']}"
            st["windows"].append({"id": wid, "label": opt("--label"), "blocks": [block]})
            save()
            print(json.dumps({"window_id": wid, "block_ids": [block]}))
''').lstrip()


@pytest.fixture
def fake_rex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rex = bin_dir / "rex"
    rex.write_text(FAKE_REX.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    rex.chmod(rex.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"n": 0, "active": "window:a", "session_label": "tidal-bay", "windows": [
        {"id": "window:a", "label": "fix-login", "blocks": ["block:a"]}]}))
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("FAKE_REX_STATE", str(state))
    monkeypatch.setenv("FAKE_REX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REX_SESSION", "session:s")
    monkeypatch.setenv("REX_BLOCK", "block:a")

    class Fake:
        def calls(self):
            return [json.loads(line) for line in log.read_text().splitlines()]

        def windows(self):
            return {w["id"]: w for w in json.loads(state.read_text())["windows"]}

        def label(self, wid="window:a"):
            return self.windows()[wid]["label"]

        def state(self):
            return json.loads(state.read_text())

        def edit(self, fn):
            st = json.loads(state.read_text())
            fn(st)
            state.write_text(json.dumps(st))

    return Fake()


class FakeState:
    def __init__(self):
        self.data = {}
        self.data_dir = None

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.state = FakeState()
        self.hooks = {}
        self.sections = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_system_prompt_section(self, sid, fn):
        self.sections[sid] = fn

    def register_skill(self, *a, **k):
        pass


@pytest.fixture
def plugin(fake_rex):
    name = f"rexplugin_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    ctx = FakeCtx()
    mod.register(ctx)
    mod.ctx_for_test = ctx
    yield mod
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def drain(plugin):
    plugin._worker.drain()


# --- prompt section ---------------------------------------------------------------------------

def test_prompt_section_naming_on_includes_window_rename(plugin):
    text = plugin._prompt_section({"platform": "cli"})
    assert text.startswith("REX CONTEXT") and "rex session rename" in text
    assert "rex window rename <label>" in text
    assert 'skill_view(name="rex:rex")' in text
    assert "rex run -b \"$REX_BLOCK\"" in text


def _no_session_naming(text):
    # Rex's naming paragraph is dropped, and the bogus "out of date" paragraph that Rex prints for
    # an explicit --feature-session-naming=false never appears.
    return "rex session rename" not in text and "out of date" not in text and "rex skill setup" not in text


def test_prompt_section_naming_off_drops_session_and_window_naming(plugin, fake_rex):
    plugin.ctx_for_test.settings.update(session_naming=False, visible_commands=False, status_titles=False)
    text = plugin._prompt_section({"platform": "cli"})
    assert text.startswith("REX CONTEXT") and _no_session_naming(text)
    assert "rex window rename" not in text and "rex run" not in text
    starts = [c["args"] for c in fake_rex.calls() if c["args"][:2] == ["__internal", "session-start"]]
    assert starts and all("--feature-session-naming=false" not in a for a in starts)


def test_prompt_section_skips_messaging_platforms_and_non_rex(plugin, monkeypatch):
    assert plugin._prompt_section({"platform": "telegram"}) == ""
    monkeypatch.delenv("REX_SESSION")
    assert plugin._prompt_section({"platform": "cli"}) == ""


def test_prompt_section_shared_rex_session_names_window_only(plugin, fake_rex):
    fake_rex.edit(lambda st: st["windows"].append({"id": "window:b", "label": "other", "blocks": ["block:b"]}))
    text = plugin._prompt_section({"platform": "cli"})
    assert text.startswith("REX CONTEXT") and _no_session_naming(text)
    assert "rex window rename <label>" in text and "Don't rename the Rex session" in text


# --- the shared Rex session label ---------------------------------------------------------------

def test_session_label_restored_when_last_hermes_chat_ends(plugin, fake_rex, monkeypatch):
    fake_rex.edit(lambda st: st["windows"].append({"id": "window:b", "label": "other", "blocks": ["block:b"]}))
    plugin._pre_llm_call(session_id="s1", platform="cli")
    monkeypatch.setenv("REX_BLOCK", "block:b")
    plugin._pre_llm_call(session_id="s2", platform="cli")
    drain(plugin)
    fake_rex.edit(lambda st: st.update(session_label="fix-login"))  # a chat ran `rex session rename`
    monkeypatch.delenv("REX_SESSION")  # finalize runs outside the turn
    monkeypatch.delenv("REX_BLOCK")
    plugin._on_session_finalize(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.state()["session_label"] == "fix-login"  # s2 still lives in this Rex session
    plugin._on_session_finalize(session_id="s2", platform="cli")
    drain(plugin)
    assert fake_rex.state()["session_label"] == "tidal-bay"


def test_session_label_untouched_when_naming_off(plugin, fake_rex, monkeypatch):
    plugin.ctx_for_test.settings["session_naming"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    fake_rex.edit(lambda st: st.update(session_label="user-chosen"))
    plugin._on_session_finalize(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.state()["session_label"] == "user-chosen"
    assert not [c for c in fake_rex.calls() if c["args"][:1] == ["ls"]]


# --- status titles ----------------------------------------------------------------------------

def test_turn_lifecycle_sets_status_marks(plugin, fake_rex):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "● fix-login"
    plugin._on_human_input_request(kind="clarify", session_id="s1", platform="cli", prompt="q?")
    drain(plugin)
    assert fake_rex.label() == "? fix-login"
    plugin._on_human_input_resolved(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "● fix-login"
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "✓ fix-login"
    plugin._on_session_end(session_id="s1", platform="cli", completed=False, interrupted=True)
    drain(plugin)
    assert fake_rex.label() == "■ fix-login"


def test_status_off_never_renames(plugin, fake_rex):
    plugin.ctx_for_test.settings["status_titles"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "fix-login"
    assert not [c for c in fake_rex.calls() if c["args"][:2] == ["window", "rename"]]


def test_subagent_turns_do_not_touch_parent_title(plugin, fake_rex):
    plugin._pre_llm_call(session_id="child", platform="cli", parent_session_id="s1")
    plugin._on_session_end(session_id="child", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_done_mark_waits_for_background_subagents(plugin, fake_rex):
    plugin.ctx_for_test.settings["subagent_panes"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._subagent_start(parent_session_id="s1", child_session_id="c1", child_goal="a")
    plugin._subagent_start(parent_session_id="s1", child_session_id="c2", child_goal="b")
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)  # parent turn ends first
    drain(plugin)
    assert fake_rex.label() == "● fix-login"
    plugin._subagent_stop(parent_session_id="s1", child_session_id="c1")
    drain(plugin)
    assert fake_rex.label() == "● fix-login"  # c2 still running
    plugin._subagent_stop(parent_session_id="s1", child_session_id="c2")
    drain(plugin)
    assert fake_rex.label() == "✓ fix-login"


def test_sync_subagents_do_not_delay_done(plugin, fake_rex):
    plugin.ctx_for_test.settings["subagent_panes"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._subagent_start(parent_session_id="s1", child_session_id="c1", child_goal="a")
    plugin._subagent_stop(parent_session_id="s1", child_session_id="c1")  # finished inside the turn
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "✓ fix-login"


def test_interrupted_turn_is_not_held_for_subagents(plugin, fake_rex):
    plugin.ctx_for_test.settings["subagent_panes"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._subagent_start(parent_session_id="s1", child_session_id="c1", child_goal="a")
    plugin._on_session_end(session_id="s1", platform="cli", completed=False, interrupted=True)
    drain(plugin)
    assert fake_rex.label() == "■ fix-login"


def test_stale_child_stops_holding_done(plugin, fake_rex, monkeypatch):
    plugin.ctx_for_test.settings["subagent_panes"] = False
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._subagent_start(parent_session_id="s1", child_session_id="c1", child_goal="a")
    monkeypatch.setattr(plugin, "_CHILD_MAX_AGE_S", -1)  # its subagent_stop never came
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "✓ fix-login"


def test_model_rename_keeps_status_mark(plugin, fake_rex):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    # the model runs `rex window rename new-task`, which drops the mark
    fake_rex.windows()  # (state is file-backed; simulate the rename by hand)
    st = json.loads(Path(os.environ["FAKE_REX_STATE"]).read_text())
    st["windows"][0]["label"] = "new-task"
    Path(os.environ["FAKE_REX_STATE"]).write_text(json.dumps(st))
    plugin._post_tool_call(tool_name="terminal", args={"command": "rex window rename new-task"},
                           session_id="s1")
    drain(plugin)
    assert fake_rex.label() == "● new-task"


def test_session_finalize_restores_original_label(plugin, fake_rex, monkeypatch):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._pre_command(command="title", args_raw="new task", session_key="k", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "● new-task"
    # finalize fires outside the turn: no bound env, so the registry must supply the pane
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._on_session_finalize(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_title_command_relabels_and_keeps_mark(plugin, fake_rex, monkeypatch):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    # Like Hermes' CLI/TUI: slash commands fire outside the turn (no bound env), and session_key
    # carries the session id.
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._pre_command(command="title", args_raw="Fix the OAuth Redirect Loop!", session_key="s1",
                        platform="cli")
    drain(plugin)
    assert fake_rex.label() == "● fix-the-oauth-redirect-loop"


def test_resume_labels_window_after_old_session_restores(plugin, fake_rex, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_session",
                        lambda ref: ("old-sid", "Count .py files and read README first line #2"))
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    assert fake_rex.label() == "✓ fix-login"
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._pre_command(command="resume", args_raw="old-sid", session_key="s1", platform="cli")
    plugin._on_session_finalize(session_id="s1", platform="cli")  # Hermes ends s1 after the hook
    drain(plugin)
    assert fake_rex.label() == "count-py-files-and-read-readme"
    # quitting the resumed session before its first turn still finds the pane and restores
    plugin._on_session_finalize(session_id="old-sid", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_resume_reported_after_old_session_ended(plugin, fake_rex, monkeypatch):
    """Classic CLI / Ink run /resume themselves and report it after the old session ended."""
    monkeypatch.setattr(plugin, "_resolve_session",
                        lambda ref: ("old-sid", "Count .py files and read README first line #2"))
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._on_session_finalize(session_id="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"
    plugin._pre_command(command="resume", args_raw="old-sid", session_key="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "count-py-files-and-read-readme"
    plugin._on_session_finalize(session_id="old-sid", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_resume_when_old_session_ends_during_the_title_lookup(plugin, fake_rex, monkeypatch):
    """The race seen live: finalize lands while pre_command reads the session DB."""
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")

    def slow_lookup(ref):
        plugin._on_session_finalize(session_id="s1", platform="cli")  # the client ended s1 meanwhile
        return "old-sid", "Count .py files and read README first line #2"

    monkeypatch.setattr(plugin, "_resolve_session", slow_lookup)
    plugin._pre_command(command="resume", args_raw="old-sid", session_key="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "count-py-files-and-read-readme"


def test_quit_after_resume_without_a_turn_restores(plugin, fake_rex, monkeypatch):
    """The hook sequence a live trace showed: Hermes never finalizes a session resumed without a
    new turn, but /quit reaches pre_command with that session's id."""
    monkeypatch.setattr(plugin, "_resolve_session",
                        lambda ref: ("old-sid", "Count .py files and read README first line #2"))
    plugin._pre_llm_call(session_id="s1", platform="cli")
    plugin._on_session_end(session_id="s1", platform="cli", completed=True)
    drain(plugin)
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._pre_command(command="resume", args_raw="old-sid", session_key="s1", platform="cli")
    plugin._on_session_finalize(session_id="s1", platform="local")
    drain(plugin)
    assert fake_rex.label() == "count-py-files-and-read-readme"
    plugin._pre_command(command="quit", args_raw="", session_key="old-sid", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_new_restores_and_unknown_session_quit_is_ignored(plugin, fake_rex, monkeypatch):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._pre_command(command="quit", args_raw="", session_key="other", platform="cli")
    plugin._pre_command(command="new", args_raw="", session_key="s1", platform="telegram")
    drain(plugin)
    assert fake_rex.label() == "● fix-login"
    plugin._pre_command(command="new", args_raw="", session_key="s1", platform="cli")
    plugin._on_session_finalize(session_id="s1", platform="local")  # the normal finalize after it
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_title_after_session_ended_does_nothing(plugin, fake_rex, monkeypatch):
    plugin._pre_llm_call(session_id="s1", platform="cli")
    drain(plugin)
    monkeypatch.delenv("REX_SESSION")
    monkeypatch.delenv("REX_BLOCK")
    plugin._on_session_finalize(session_id="s1", platform="cli")
    plugin._pre_command(command="title", args_raw="late", session_key="s1", platform="cli")
    drain(plugin)
    assert fake_rex.label() == "fix-login"


def test_messaging_platform_hooks_are_ignored(plugin, fake_rex):
    plugin._pre_llm_call(session_id="s1", platform="telegram")
    drain(plugin)
    assert fake_rex.calls() == []


def test_debug_trace_records_hook_ids_only(plugin, fake_rex, tmp_path):
    ctx = plugin.ctx_for_test
    ctx.state.data_dir = tmp_path / "data"
    hook = ctx.hooks["pre_llm_call"]
    hook(session_id="s1", platform="cli", user_message="secret text", conversation_history=[{"x": 1}])
    assert not (ctx.state.data_dir / "trace.jsonl").exists()  # debug is off by default
    ctx.settings["debug"] = True
    hook(session_id="s1", platform="cli", user_message="secret text", conversation_history=[{"x": 1}])
    ctx.hooks["pre_command"](command="resume", args_raw="abc", session_key="s1", platform="cli")
    rows = [json.loads(line) for line in (ctx.state.data_dir / "trace.jsonl").read_text().splitlines()]
    assert [r["hook"] for r in rows] == ["pre_llm_call", "pre_command"]
    assert rows[0]["session_id"] == "s1" and rows[0]["bound_block"] == "block:a"
    assert rows[1]["command"] == "resume" and rows[1]["session_key"] == "s1"
    assert "secret text" not in (ctx.state.data_dir / "trace.jsonl").read_text()
    drain(plugin)


# --- alerts -----------------------------------------------------------------------------------

def test_alert_skipped_when_pane_in_view_sent_otherwise(plugin, monkeypatch):
    sent = []
    titles = sys.modules[plugin.__name__ + ".titles"]
    monkeypatch.setattr(titles, "notify", lambda *a: sent.append(a))
    monkeypatch.setattr(titles, "_front_app", lambda: "Rex Beta")
    plugin._on_human_input_request(kind="approval", session_id="s1", platform="cli", prompt="rm -rf x")
    drain(plugin)
    assert sent == []  # Rex in front, our block focused in the active window
    monkeypatch.setattr(titles, "_front_app", lambda: "Safari")
    plugin._on_human_input_request(kind="approval", session_id="s1", platform="cli", prompt="rm -rf x")
    drain(plugin)
    assert sent and sent[0][0] == "Hermes wants approval to run a command" and sent[0][2] == "rm -rf x"
    plugin.ctx_for_test.settings["alerts"] = "off"
    plugin._on_human_input_request(kind="approval", session_id="s1", platform="cli", prompt="y")
    drain(plugin)
    assert len(sent) == 1


# --- subagent / kanban panes ------------------------------------------------------------------

def _manifest(root: Path, goals):
    d = root / "deleg_abcd1234"
    d.mkdir(parents=True)
    tasks = []
    for i, g in enumerate(goals):
        log = d / f"task-{i}.log"
        log.write_text(f"kickoff {g}\n")
        tasks.append({"index": i, "goal": g, "log": str(log), "status": "running"})
    (d / "manifest.json").write_text(json.dumps({"tasks": tasks}))
    return d


def test_subagents_open_panes_in_one_window(plugin, fake_rex, tmp_path, monkeypatch):
    root = tmp_path / "live"
    _manifest(root, ["review the diff", "review the diff", "write tests"])
    panes = sys.modules[plugin.__name__ + ".panes"]
    monkeypatch.setattr(plugin, "live_root", lambda: root)
    for goal in ["review the diff", "review the diff", "write tests"]:
        plugin._subagent_start(parent_session_id="s1", child_session_id=f"c-{goal}", child_goal=goal)
    drain(plugin)
    runs = [c["args"] for c in fake_rex.calls() if c["args"][:1] == ["run"]]
    assert len(runs) == 3
    assert "-b" not in runs[0] and "--focus=false" in runs[0]  # first: new window, no focus steal
    assert all("-b" in r for r in runs[1:])                     # rest: split inside that window
    logs = [r[r.index("--log") + 1] for r in runs]
    assert len(set(logs)) == 3  # identical goals still map to distinct transcripts
    wins = [w for w in fake_rex.windows().values() if w["id"] != "window:a"]
    assert len(wins) == 1 and wins[0]["label"] == "subagents" and len(wins[0]["blocks"]) == 3
    assert fake_rex.label() == "fix-login"  # the parent's own window is untouched


def test_subagent_panes_off(plugin, fake_rex, tmp_path, monkeypatch):
    plugin.ctx_for_test.settings["subagent_panes"] = False
    monkeypatch.setattr(plugin, "live_root", lambda: _manifest(tmp_path / "live", ["g"]).parent)
    plugin._subagent_start(parent_session_id="s1", child_session_id="c", child_goal="g")
    drain(plugin)
    assert not [c for c in fake_rex.calls() if c["args"][:1] == ["run"]]


def test_kanban_worker_uses_last_active_pane(plugin, fake_rex, tmp_path, monkeypatch):
    plugin._kanban_log_path = None
    panes = sys.modules[plugin.__name__ + ".panes"]
    # no Hermes turn yet -> no known Rex pane -> nothing opens
    plugin._on_kanban_worker_spawned(task_id="t_1", board="b", assignee="dev", worker_pid=os.getpid())
    drain(plugin)
    assert not [c for c in fake_rex.calls() if c["args"][:1] == ["run"]]

    plugin._pre_llm_call(session_id="s1", platform="cli")  # records the last active pane
    monkeypatch.delenv("REX_SESSION")                        # dispatcher has no client env
    monkeypatch.delenv("REX_BLOCK")
    fake_mod = type(sys)("hermes_cli.kanban_db")
    fake_mod.worker_log_path = lambda task_id, board=None: tmp_path / f"{task_id}.log"
    monkeypatch.setitem(sys.modules, "hermes_cli", type(sys)("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_mod)
    plugin._on_kanban_worker_spawned(task_id="t_1", board="b", assignee="dev", worker_pid=os.getpid())
    drain(plugin)
    runs = [c for c in fake_rex.calls() if c["args"][:1] == ["run"]]
    assert len(runs) == 1
    args = runs[0]["args"]
    assert args[args.index("-s") + 1] == "session:s"
    assert args[args.index("--log") + 1] == str(tmp_path / "t_1.log")
    assert str(os.getpid()) in args
    assert any(w["label"] == "kanban" for w in fake_rex.windows().values())


# --- follow.py --------------------------------------------------------------------------------

def test_follow_streams_until_manifest_done(tmp_path):
    import subprocess
    d = _manifest(tmp_path, ["g"])
    log, manifest = d / "task-0.log", d / "manifest.json"
    proc = subprocess.Popen([sys.executable, str(ROOT / "follow.py"), "--log", str(log), "--manifest",
                             str(manifest), "--index", "0", "--linger", "0", "--title", "T"],
                            stdout=subprocess.PIPE, text=True)
    time.sleep(0.8)
    with log.open("a") as fh:
        fh.write("tool: terminal\n")
    data = json.loads(manifest.read_text())
    data["tasks"][0]["status"] = "completed"
    manifest.write_text(json.dumps(data))
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "kickoff g" in out and "tool: terminal" in out and "── completed ──" in out


def test_follow_stops_when_pid_exits(tmp_path):
    import subprocess
    log = tmp_path / "w.log"
    log.write_text("worker start\n")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    import threading
    threading.Thread(target=sleeper.wait, daemon=True).start()  # reap it, as Hermes' dispatcher does
    out = subprocess.run([sys.executable, str(ROOT / "follow.py"), "--log", str(log), "--pid",
                          str(sleeper.pid), "--linger", "0"], capture_output=True, text=True, timeout=15)
    assert "worker start" in out.stdout and "── exited ──" in out.stdout
