"""Tasks that survive a restart.

A task lived in a dict and nowhere else. Quit the engine — a crash, a logout, `macwork serve` restarted —
and every task waiting on the caller was gone, along with `desktop`, the record of what that task had
opened, which is the only thing that lets tidying touch its own doing and nothing else. A caller holding a
task id got "unknown or expired task", with no way to tell a real expiry from a restart.
"""

from macwork.engine import Engine
from macwork.model import Step, Task
from macwork.store import Store, dump, load
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def _rich(goal: str = "把这封邮件发出去") -> Task:
    from macwork.facts import Facts
    task = Task(goal=goal, inputs={"text": "hello"})
    task.memory.facts = Facts(inputs=task.inputs, goal=goal)
    task.steps = [Step(0, "menu 文件 ▸ 新建文稿", channel="menu", verb="press", key="ax|AXMenuItem|_new:")]
    task.status, task.reason = "need_confirm", "this action is irreversible"
    task.approved.add("menu 文件 ▸ 存储")
    task.memory.no_effect.add("sig|menu 文件 ▸ 导出")
    task.memory.declined.add("switch to app 访达")
    task.memory.facts.record("文本编辑", "未命名", "会议定在周四", 0)
    task.desktop.initial_pids = {1, 2}
    task.desktop.initial_windows = {1: {10, 11}}
    task.desktop.opened = {42: {"name": "计算器"}}
    task.pace.replans = 2
    task.spent_s = 12.5
    return task


def test_a_task_comes_back_whole(tmp_path):
    before = _rich()
    after = load(dump(before))

    assert after.goal == before.goal and after.status == "need_confirm"
    assert [s.action for s in after.steps] == [s.action for s in before.steps]
    assert after.steps[0].key == "ax|AXMenuItem|_new:", "the language-independent identity comes back too"
    assert after.approved == {"menu 文件 ▸ 存储"}
    assert after.memory.no_effect == {"sig|menu 文件 ▸ 导出"}
    assert after.memory.declined == {"switch to app 访达"}
    assert after.desktop.initial_pids == {1, 2}
    assert after.desktop.initial_windows == {1: {10, 11}}, "what tidying needs to touch only its own doing"
    assert after.desktop.opened == {42: {"name": "计算器"}}
    assert after.pace.replans == 2 and after.spent_s == 12.5


def test_what_the_task_saw_comes_back_as_facts(tmp_path):
    """Facts are what the task may write; losing them would let a resumed task invent values."""
    after = load(dump(_rich()))
    assert after.memory.facts.source_of("周四") == "文本编辑 (未命名)"
    assert after.memory.facts.source_of("从没见过的东西") is None


def test_live_handles_are_deliberately_left_behind(tmp_path):
    """Pids and element references described a machine that has moved on."""
    task = _rich()
    task.held = object()
    assert "held" not in dump(task)


def test_resuming_after_a_restart_is_not_an_expiry(tmp_path):
    conf = {"engine": {"store": str(tmp_path / "tasks.db")}}
    first = Engine(cfg(tmp_path, config=conf), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = _rich()
    first.tasks[task.id] = task
    first._finish(task, "need_confirm", "waiting on the caller")
    first.store.close()

    fresh = Engine(cfg(tmp_path, config=conf), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert task.id not in fresh.tasks, "nothing in memory: this is a new process"

    recalled = fresh._recall(task.id)
    assert recalled is not None and recalled.goal == task.goal
    assert recalled.outputs["resumed_after_restart"] is True
    assert fresh.cancel(task.id)["cancelled"] is True, "a known task, even after a restart"


def test_turning_it_off_keeps_everything_in_memory(tmp_path):
    store = Store(cfg(tmp_path, config={"engine": {"persist": False, "store": str(tmp_path / "off.db")}}))
    store.save(_rich())
    assert store.get(_rich().id) is None
    assert not (tmp_path / "off.db").exists()


def test_the_file_is_private(tmp_path):
    path = tmp_path / "tasks.db"
    Store(cfg(tmp_path, config={"engine": {"store": str(path)}}))
    assert path.stat().st_mode & 0o777 == 0o600, "it holds what the tasks saw"
