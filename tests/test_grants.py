"""Standing grants: a confirmation the person chose to have remembered.

The failure this answers is not a bug in any one place. The floor stops what nobody can vouch for — a
classifier at 0.69 under a bar of 0.7 — the person says yes, and tomorrow the same action in the same app
stops again. Making it stop asking meant editing a threshold, and no threshold is right for every app
anyone will ever install. The decision is the person's, so it is kept: per app, per action, given only by
them, taken back in one place.
"""

import argparse
import json
import stat

import anyio
import pytest

from macwork.cli import cmd_grants
from macwork.engine import Engine
from macwork.grants import Grants, identity
from macwork.model import Affordance
from macwork.server import build
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

EXPORT = [{"pick": "PDF"}, {"pick": "done", "done": 0.95}]      # 文件 ▸ 导出 ▸ PDF: the words say "write"
DELETE = [{"pick": "删除文稿"}, {"pick": "done", "done": 0.95}]
APP = {"pid": 42, "name": "文本编辑", "bundle_id": "com.apple.TextEdit"}


def fresh(tmp_path, script, decider=ScriptedDecider, **over):
    """A new engine each time, as a new day would be: nothing carries over but what is on disk."""
    helper = FakeHelper()
    return Engine(cfg(tmp_path, **over), helper=helper, decider=decider(script)), helper


def audit(tmp_path, kind):
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["kind"] == kind]


# ----------------------------------------------------------------- the point of it
def test_the_same_scene_a_second_time_needs_nobody(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    asked = eng.do("导出成 PDF")
    assert asked["status"] == "need_confirm" and asked["pending"]["because"] == ["write"]
    assert asked["pending"]["remember"]["bundle_id"] == "com.apple.TextEdit"
    assert eng.resume(asked["task_id"], confirm=True, remember=True)["status"] == "done"

    again, helper = fresh(tmp_path, EXPORT)
    res = again.do("导出成 PDF")
    assert res["status"] == "done", "the person already answered this, for this app"
    assert helper.did("ax.perform"), "and it was really done, not skipped"
    released = [r for r in audit(tmp_path, "floor") if r.get("grant")]
    assert released and released[-1]["because"] == ["write"], "a release by grant is on the record as one"


def test_a_plain_yes_is_for_this_once(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    asked = eng.do("导出成 PDF")
    assert eng.resume(asked["task_id"], confirm=True)["status"] == "done"
    assert fresh(tmp_path, EXPORT)[0].do("导出成 PDF")["status"] == "need_confirm"
    assert Grants(eng.cfg).all() == []


def test_it_can_be_taken_back_from_another_process(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    asked = eng.do("导出成 PDF")
    eng.resume(asked["task_id"], confirm=True, remember=True)
    running, _ = fresh(tmp_path, EXPORT)
    assert running.grants.all()                       # a long-lived engine has read the file…
    assert Grants(eng.cfg).revoke(asked["pending"]["remember"]["id"][:6]) == 1     # …`macwork grants revoke`, elsewhere
    assert running.do("导出成 PDF")["status"] == "need_confirm"


# ----------------------------------------------------------------- what it never does
def test_deleting_asks_every_time_whatever_was_said_before(tmp_path):
    eng, _ = fresh(tmp_path, DELETE)
    asked = eng.do("把这个文稿删掉")
    assert asked["status"] == "need_confirm" and "remember" not in asked["pending"]
    assert eng.resume(asked["task_id"], confirm=True, remember=True)["status"] == "done"
    assert Grants(eng.cfg).all() == [], "remember=true on something that may not be remembered is not an error, and not a grant"

    # and a row for it in the file — written by hand, or from before the setting changed — counts for nothing
    menu = next(a for a in eng.observe()["affordances"] if "删除文稿" in a["label"])
    forged = Affordance(menu["id"], menu["channel"], menu["verb"], menu["label"], {}, context=menu.get("context", ""))
    permissive = Grants(cfg(tmp_path, config={"grants": {"never_for": []}}))
    permissive.add(permissive.offer(APP, forged, ["delete"]), "by hand")
    assert fresh(tmp_path, DELETE)[0].do("把这个文稿删掉")["status"] == "need_confirm"
    # (the row is the right one: with the setting emptied, that same row does release it)
    assert fresh(tmp_path, DELETE, config={"grants": {"never_for": []}})[0].do("把这个文稿删掉")["status"] == "done"


def test_a_grant_does_not_make_the_goal_ask_for_it(tmp_path):
    """It answers "may this go ahead without asking me", never "should this be done"."""
    eng, _ = fresh(tmp_path, EXPORT)
    asked = eng.do("导出成 PDF")
    eng.resume(asked["task_id"], confirm=True, remember=True)

    class Unasked(ScriptedDecider):
        calls_for = 0.0            # the goal gives no reason for this action

    other, helper = fresh(tmp_path, EXPORT, decider=Unasked)
    res = other.do("看看这个文稿")
    assert "PDF" not in " ".join(res["steps"]) and not helper.did("ax.perform")


def test_the_engine_never_gives_one_itself(tmp_path):
    for _ in range(3):             # however often the person says yes
        eng, _ = fresh(tmp_path, EXPORT)
        eng.resume(eng.do("导出成 PDF")["task_id"], confirm=True)
    assert Grants(eng.cfg).all() == []


def test_an_unreadable_record_grants_nothing_and_is_left_alone(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    eng.grants.path.parent.mkdir(parents=True, exist_ok=True)
    eng.grants.path.write_text("{ not json", encoding="utf-8")
    assert eng.do("导出成 PDF")["status"] == "need_confirm"
    assert eng.grants.path.read_text(encoding="utf-8") == "{ not json"


def test_it_can_be_switched_off(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    eng.resume(eng.do("导出成 PDF")["task_id"], confirm=True, remember=True)
    off, _ = fresh(tmp_path, EXPORT, config={"grants": {"enabled": False}})
    res = off.do("导出成 PDF")
    assert res["status"] == "need_confirm" and "remember" not in res["pending"]


def test_the_record_is_private(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    eng.resume(eng.do("导出成 PDF")["task_id"], confirm=True, remember=True)
    assert stat.S_IMODE(eng.grants.path.stat().st_mode) == 0o600


# ----------------------------------------------------------------- what a grant is for
def press(label, key="", context="文本编辑 ▸ 文件", channel="menu"):
    return Affordance("m1", channel, "press", label, {}, context=context, key=key)


def test_it_is_for_one_action_in_one_app():
    here = identity(APP, press("menu 文件 ▸ 导出 ▸ PDF"))
    assert here != identity({**APP, "bundle_id": "com.example.other"}, press("menu 文件 ▸ 导出 ▸ PDF"))
    assert here != identity(APP, press("menu 文件 ▸ 导出 ▸ Word"))
    assert here == identity({**APP, "pid": 9, "version": "2.0"}, press("menu 文件 ▸ 导出 ▸ PDF")), "it survives an update and a relaunch"


def test_it_survives_the_language_being_changed_where_the_mac_names_the_action():
    assert identity(APP, press("menu 文件 ▸ 导出 ▸ PDF", key="exportPDF:")) == \
        identity(APP, press("menu File ▸ Export ▸ PDF", key="exportPDF:", context="TextEdit ▸ File"))


def test_typing_one_thing_is_not_typing_anything():
    field = "type into 文本栏 「命令」 — typing 「{}」"
    a = Affordance("w1", "window", "type", field.format("ls"), {}, context="终端")
    b = Affordance("w1", "window", "type", field.format("rm -rf ~"), {}, context="终端")
    assert identity(APP, a) != identity(APP, b)


def test_what_a_field_holds_right_now_is_not_part_of_it():
    assert identity(APP, press("type into 文本栏 (now: hello)")) == identity(APP, press("type into 文本栏 (now: goodbye)"))


# ----------------------------------------------------------------- who may give one
def call(server, tool, **args):
    async def go():
        return (await server.call_tool(tool, args)).structured_content
    return anyio.run(go)


def test_a_model_calling_over_mcp_cannot_give_itself_one(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT)
    server = build(eng.cfg, eng)
    asked = call(server, "mac_do", goal="导出成 PDF")
    assert "macwork grants allow" in asked["pending"]["remember_how"]
    res = call(server, "mac_resume", task_id=asked["task_id"], confirm=True, remember=True)
    assert res["status"] == "done" and "remember_ignored" in res      # the one action: yes. From now on: not theirs
    assert Grants(eng.cfg).all() == []


def test_unless_the_person_turned_that_on(tmp_path):
    eng, _ = fresh(tmp_path, EXPORT, config={"grants": {"from_mcp": True}})
    server = build(eng.cfg, eng)
    asked = call(server, "mac_do", goal="导出成 PDF")
    res = call(server, "mac_resume", task_id=asked["task_id"], confirm=True, remember=True)
    assert res["outputs"]["remembered"]["ok"] and len(Grants(eng.cfg).all()) == 1


def test_the_person_gives_one_from_the_command_line(tmp_path, capsys):
    eng, _ = fresh(tmp_path, EXPORT)
    asked = eng.do("导出成 PDF")
    ns = lambda action, id=None: argparse.Namespace(action=action, id=id)   # noqa: E731

    assert cmd_grants(eng.cfg, ns("allow", asked["task_id"])) == 0
    assert cmd_grants(eng.cfg, ns("list")) == 0
    shown = capsys.readouterr().out
    assert "PDF" in shown and "because write" in shown and "via cli" in shown
    assert fresh(tmp_path, EXPORT)[0].do("导出成 PDF")["status"] == "done"
    assert [r["source"] for r in audit(tmp_path, "grant")] == ["cli"]

    assert cmd_grants(eng.cfg, ns("allow", "no-such-task")) == 1
    assert cmd_grants(eng.cfg, ns("clear")) == 0
    assert fresh(tmp_path, EXPORT)[0].do("导出成 PDF")["status"] == "need_confirm"


# ----------------------------------------------------------------- step level
def test_the_step_level_honours_and_offers_them_too(tmp_path):
    eng, _ = fresh(tmp_path, [])
    pdf = next(a for a in eng.observe()["affordances"] if "PDF" in a["label"])
    refused = eng.act(pdf["id"])
    assert refused["needs_confirm"] and refused["remember"]["because"] == ["write"]
    assert eng.act(pdf["id"], confirm=True, remember=True)["ok"]

    later, _ = fresh(tmp_path, [])
    pdf = next(a for a in later.observe()["affordances"] if "PDF" in a["label"])
    assert later.act(pdf["id"])["ok"] is True


@pytest.mark.parametrize("because", [["delete"], ["send"], ["pay"], ["the screen asks to confirm something"], []])
def test_what_may_never_be_remembered(tmp_path, because):
    assert Grants(cfg(tmp_path)).offer(APP, press("anything"), because) is None
