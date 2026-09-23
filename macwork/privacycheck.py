"""Measure redaction instead of trusting it: a synthetic corpus of personal data (no real person's data), mixed
into ordinary UI strings, goes through the same Redactor and on-device tagger the engine uses. Reported: how much
leaks (per kind) and how often an app name on this Mac is hidden by mistake — text by text, and through one
redactor as one task sees many screens (`carried`). `replay` puts one recorded task through again.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config
from .privacy import Redactor, _TOKEN, _beside

# Synthetic values only: common names and reserved/test numbers, no real person's data.
#
# Eight scripts, not two. The corpus used to be Chinese and English — the same two languages the redactor was
# written for — so the low leak rate it reported was self-fulfilling: it never tested what the tagger and the
# hand-written patterns could not see.
PEOPLE_ZH = ["张伟", "王芳", "李娜", "刘洋", "陈静", "赵磊", "周敏", "黄晓明"]
PEOPLE_EN = ["Emily Johnson", "Michael Brown", "Sarah Miller", "David Wilson"]
PEOPLE_OTHER = ["Иван Петров", "김민준", "山田太郎", "Σοφία Παπαδοπούλου", "محمد العلي",
                "Müller Schneider", "José da Silva", "Nguyễn Văn An"]
EMAILS = ["emily.j@example.com", "wang.fang@example.org", "d.wilson+work@example.net"]
PHONES = ["13812345678", "+86 139 1234 5678", "(415) 555-0134",
          "+49 30 901820", "03-1234-5678", "+55 11 91234-5678", "+44 20 7946 0958", "+82 2 1234 5678"]
IDNUMS = ["11010519491231002X", "440524188001010014"]
CARDS = ["4111 1111 1111 1111", "5500-0000-0000-0004"]
ADDRESSES = ["北京市朝阳区建国路88号", "上海市浦东新区世纪大道100号", "1600 Amphitheatre Parkway, Mountain View",
             "Friedrichstraße 43, 10117 Berlin", "東京都千代田区丸の内1-1-1", "Rua Augusta 1500, São Paulo"]
IPS = ["192.168.1.23", "10.0.0.8"]

TEMPLATES = {
    "person": ["发送给 {v}", "{v} 的日程", "Meeting with {v} at 3pm", "回复 {v}：好的，明天见", "{v} shared a document with you",
               "Termin mit {v}", "Встреча с {v}", "{v} 님과의 회의", "Reunião com {v}"],
    "email": ["收件人: {v}", "Reply to {v}", "{v} 邀请你加入"],
    "phone": ["拨打 {v}", "Call {v}", "联系电话：{v}"],
    "idnum": ["身份证号 {v}", "ID: {v}"],
    "card": ["卡号 {v}", "Card ending in {v}"],
    "address": ["寄送地址：{v}", "Ship to {v}"],
    "ip": ["服务器 {v} 无响应", "Connected to {v}"],
}
VALUES = {"person": PEOPLE_ZH + PEOPLE_EN + PEOPLE_OTHER, "email": EMAILS, "phone": PHONES, "idnum": IDNUMS, "card": CARDS,
          "address": ADDRESSES, "ip": IPS}

# The same people again, glued into longer words — the way scripts without spaces write a name, and the way file
# names, possessives and plurals do. What was learned about a name in one text has to keep hiding it in the next;
# the forms a whole-word rule gives up are here on purpose, so that its price shows (`carried`). The first group
# is for every person; the others are for names written with capitals.
GLUED = {"glued": ["来自{v}的消息", "和{v}一起", "{v}的日程", "Re: {v}'s notes", "{v}_notes.txt"],
         "one_letter_ending": ["{v}s iPhone", "AirDrop: {v}s iPhone"],
         "camel_case": ["{v}Notes.txt"]}
# Given names alone, two letters of a script without capitals. The tagger calls some of them a name in one
# sentence and not in another, so a name glued into a message is hidden only as far as the rule carries what was
# learned about it. Alternately learned from a message's recipient and from a bare label, then seen glued.
GIVEN = ("一诺 一鸣 丹珍 之柔 之瑶 乐丹 乐枫 乐菱 乐萱 书翠 书雪 亦玉 从彤 以蕊 伟泽 伟诚 俊杰 元霜 冬儿 冰兰 冰夏 冰彤 冰露 凌萱 "
         "凝天 初夏 千兰 千琴 博文 博涛 向珊 听寒 嘉懿 夏菡 夏青 天佑 如萱 如霜 妙彤 子涵 子轩 宇轩 安静 宛儿 寒云 峻熙 平安 "
         "弘文 弘毅 忆柳 念薇 念露 怜梦 思源 惜雪 慕灵 慕青 懿轩 擎宇 新波 方圆 昊天 昊然 明杰 明轩 易梦 晓亦 晓霜 晨曦 智宸 "
         "曼文 梓涵 梓睿 梦曼 梦柏 梦琪 欣怡 正豪 沛菡 浩然 海云 海安 海莲 烨华 烨霖 煜城 煜祺 皓轩 盼儿 睿渊 碧彤 秋月 立诚 "
         "立轩 立辉 笑蓝 笑霜 紫寒 紫山 翠萱 芷蕾 若南 若烟 诗涵 语兰 语嫣 语琴 语蓉 雁玉 雅绿 雨寒 雪柳 青槐 靖柏 静曼 鸿涛 "
         "鸿煊").split()
GIVEN_LEARNED = {"given_from_a_message": "发送给 {v}", "given_from_a_label": "{v}"}
GIVEN_GLUED = ["给{v}发消息", "{v}邀请你加入", "来自{v}的消息"]


UI_WORDS = ["新建文稿", "打开", "存储", "显示器", "蓝牙", "通用", "下载", "桌面", "文稿", "应用程序", "前往", "窗口", "帮助",
            "格式", "字体", "粗体", "撤销", "重做", "全选", "查找", "替换", "偏好设置", "关于本机", "最近使用", "个人收藏", "共享",
            "New Window", "Open Recent", "Save As", "Page Setup", "Show Sidebar", "Bring All to Front", "Font Book",
            "Activity Monitor", "Disk Utility", "Keychain Access", "Home", "Library", "Music", "Pictures", "Movies",
            "General", "Bluetooth", "Displays", "Wallpaper", "Screen Time", "Control Center", "Login Items"]


def corpus() -> list[tuple[str, str, str]]:
    """(kind, value, text) — every value in every template of its kind."""
    return [(kind, v, t.format(v=v)) for kind, vals in VALUES.items() for v, t in itertools.product(vals, TEMPLATES[kind])]


def _leaked(value: str, redacted: str) -> bool:
    """Leaked when the value, or a telling part of it (a name's parts, 4+ digit runs), survives."""
    if value in redacted:
        return True
    parts = [p for p in value.replace("-", " ").replace("+", " ").replace("(", " ").replace(")", " ").split() if len(p) >= 4]
    return any(p in redacted for p in parts if not p.isdigit() or len(p) >= 6)


Found = Callable[[list[str]], list[list[dict[str, Any]]]]


def run(cfg: Config, entities: Found | None, app_names: list[str], detect: Found | None = None) -> dict[str, Any]:
    """Measured with the same parts the engine redacts with — name tagging *and* the system's data detector.
    Leaving one of them out here is how a measurement comes back reassuring about something untested."""
    items = corpus()
    per_kind: dict[str, list[int]] = {}
    leaks: list[dict[str, str]] = []
    for kind, value, text in items:
        r = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect)   # a fresh table per text: nothing learned across
        out = r.text(text)
        bad = _leaked(value, out)
        per_kind.setdefault(kind, [0, 0])
        per_kind[kind][0] += bad
        per_kind[kind][1] += 1
        if bad:
            leaks.append({"kind": kind, "text": text, "sent": out})
    over: list[dict[str, str]] = []
    for name in app_names[:80]:
        for t in ("打开 {v}", "Open {v}", "切换到 {v}"):
            text = t.format(v=name)
            out = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect).text(text)
            if name not in out:
                over.append({"app": name, "sent": out})
    ui_over: list[dict[str, str]] = []
    for word in UI_WORDS:                         # ordinary interface words must reach the decider as they are
        out = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect).text(word)
        if out != word:
            ui_over.append({"ui": word, "sent": out})
    door = _through_the_door(cfg, entities, app_names, detect, items)
    one_task = carried(cfg, entities, app_names, detect, items)
    n = len(items)
    return {"texts": n, "leaked": sum(v[0] for v in per_kind.values()),
            "leak_rate": round(sum(v[0] for v in per_kind.values()) / n, 3),
            "by_kind": {k: f"{v[0]}/{v[1]} leaked" for k, v in per_kind.items()},
            "app_names_checked": min(len(app_names), 80) * 3, "app_names_hidden_by_mistake": len(over),
            "ui_words_checked": len(UI_WORDS), "ui_words_hidden_by_mistake": len(ui_over),
            "through_the_gate": door,
            "one_task": one_task,
            "examples": {"leaks": leaks[:8], "over_redacted": (over + ui_over)[:8]}}


def _through_the_door(cfg: Config, entities: Found | None, app_names: list[str], detect: Found | None,
                      items: list[tuple[str, str, str]]) -> dict[str, Any]:
    """The same corpus again, but through `Gate.decide` — the door the engine actually sends by.

    This measurement existed and looked at the wrong thing. It ran the corpus through `Redactor`, which is
    one part of the door, and reported a leak rate for the whole. Meanwhile `Gate.decide` was redacting the
    state and the options and passing each question's *wording* through untouched — and the safety floor
    interpolates the action's own label into that wording, several times per step. A number that says 27%
    while the largest leak is not in the sample is worse than no number.

    So this puts each corpus text in all three places a real request has: the state, an option, and the
    question's own wording.

    Nothing of it is written to the audit log. Its requests are not the engine's, and they used to land there
    as `decide` and `answers` records of a task called '' — 237 of each per run, 4,266 of each on 09-23 alone —
    for every later reading of the log to filter out.
    """
    from .privacy import Audit, Gate

    class Capture:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
            self.sent.append(repr(state) + repr(questions))
            return {k: {"type": "choice", "choice": "a", "confidence": 1.0} for k in questions}

    seen = Capture()
    quiet = Config({**cfg.docs, "config": {**cfg.docs["config"], "audit": {**cfg.section("audit"), "enabled": False}}})
    gate = Gate(seen, Audit(quiet))
    leaked: list[dict[str, str]] = []
    for kind, value, text in items:
        r = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect)
        try:
            gate.decide(r, {"app": "Mail", "screen_text": text},
                        {"q": {"type": "choice", "instructions": f"what does 「{text}」 do?",
                               "criteria": {"a": text, "b": "nothing"}}})
        except Exception:                          # noqa: BLE001  (a refusal is not a leak)
            continue
        if _leaked(value, seen.sent[-1]):
            leaked.append({"kind": kind, "sent": seen.sent[-1][:200]})
    return {"requests": len(items), "leaked": len(leaked),
            "leak_rate": round(len(leaked) / (len(items) or 1), 3),
            "places_checked": ["state", "criteria", "instructions"],
            "examples": leaked[:5]}


def _as_substrings(r: Redactor, text: str) -> str:
    """What `r` would send if it replaced every value it has learned wherever its letters occur, longest first —
    the rule before whole words — measured on the same table, so only the rule differs."""
    for value in sorted(r.table, key=len, reverse=True):
        text = text.replace(value, r.table[value])
    return text


def carried(cfg: Config, entities: Found | None, app_names: list[str], detect: Found | None,
            items: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Everything through ONE Redactor, the way one task sees many screens.

    `run` gives each text a fresh Redactor, so nothing learned from one string could show up in another — and
    that is exactly where redaction went wrong on a real Mac: 「Saf」, learned from a label the engine had cut at
    40 characters, went out inside 「Safari」 in every later request of the task, and 「单元格」, learned from a
    tab, inside the goal. Here every corpus text is seen first, then the given names; then the same people glued
    into longer words (still hidden?), and this Mac's app names and ordinary interface words (still as they are?).

    Each count stands next to what replacing the same learned values as raw substrings would have sent
    (`as_substrings`): whole words cost some glued names (`by_probe` says which forms) and save the app names
    and the words that share a name's letters.
    """
    r = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect)
    for _, _, text in items:
        r.text(text)
    hows = list(GIVEN_LEARNED)
    given = {v: hows[n % len(hows)] for n, v in enumerate(GIVEN)}
    for v, how in given.items():
        r.text(GIVEN_LEARNED[how].format(v=v))
    probes = [(group, v, t.format(v=v)) for group, tmpls in GLUED.items() for v in VALUES["person"]
              if group == "glued" or v[:1].isupper() for t in tmpls]
    probes += [(how, v, t.format(v=v)) for v, how in given.items() for t in GIVEN_GLUED]
    by_probe: dict[str, dict[str, int]] = {}
    leaks: list[dict[str, str]] = []
    for group, v, text in probes:
        out = r.text(text)
        n = by_probe.setdefault(group, {"texts": 0, "leaked": 0, "leaked_as_substrings": 0})
        n["texts"] += 1
        n["leaked"] += _leaked(v, out)
        n["leaked_as_substrings"] += _leaked(v, _as_substrings(r, text))
        if _leaked(v, out):
            leaks.append({"text": text, "sent": out})
    over, over_old = [], 0
    for name in app_names[:80]:
        for t in ("打开 {v}", "Open {v}", "切换到 {v}"):
            text = t.format(v=name)
            out = r.text(text)
            if name not in out:
                over.append({"app": name, "sent": out})
            over_old += name not in _as_substrings(r, text)
    ui, ui_old = [], 0
    for word in UI_WORDS:
        out = r.text(word)
        if out != word:
            ui.append({"ui": word, "sent": out})
        ui_old += _as_substrings(r, word) != word
    texts = sum(n["texts"] for n in by_probe.values())
    leaked = sum(n["leaked"] for n in by_probe.values())
    leaked_old = sum(n["leaked_as_substrings"] for n in by_probe.values())
    return {"texts": texts, "leaked": leaked, "leak_rate": round(leaked / (texts or 1), 3),
            "app_names_hidden_by_mistake": len(over), "ui_words_hidden_by_mistake": len(ui),
            "as_substrings": {"leaked": leaked_old, "leak_rate": round(leaked_old / (texts or 1), 3),
                              "app_names_hidden_by_mistake": over_old, "ui_words_hidden_by_mistake": ui_old},
            "by_probe": by_probe,
            "examples": {"leaks": leaks[:8], "over_redacted": (over + ui)[:8]}}


def inside_words(sent: str, table: dict[str, str]) -> int:
    """How many pseudonyms in `sent` stand for a value that was glued into a longer word there — measured on the
    text with every value put back, by the redactor's own rule for where a word ends. Where the tagger found a
    name glued into running text this counts it too; tell those apart by reading the examples."""
    value_of = {token: v for v, token in table.items()}
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    at = last = 0
    for m in _TOKEN.finditer(sent):
        v = value_of.get(m.group(0))
        if v is None:
            continue
        parts.append(sent[last:m.start()])
        at += m.start() - last
        spans.append((at, at + len(v)))
        parts.append(v)
        at += len(v)
        last = m.end()
    whole = "".join(parts) + sent[last:]
    return sum(_beside(whole, i, j) != ("", "") for i, j in spans)


def recorded(path: Path, task: str) -> Iterator[dict[str, Any]]:
    """One task's step requests — the audit's `decide` records that carry an action question — from the audit
    log and its rotations (audit.3.jsonl … audit.jsonl), oldest first, read a line at a time: each file grows
    to 64 MB before it rotates."""
    def rotation(p: Path) -> int:
        middle = p.name[len(path.stem) + 1: -len(path.suffix)]
        return int(middle) if middle.isdigit() else -1
    older = sorted((p for p in path.parent.glob(f"{path.stem}.*{path.suffix}") if rotation(p) > 0), key=rotation,
                   reverse=True)
    for f in older + ([path] if path.exists() else []):
        with f.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                if task not in line or '"decide"' not in line[:80]:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") == "decide" and rec.get("task") == task and "action" in (rec.get("questions") or {}):
                    yield rec


def replay(cfg: Config, entities: Found | None, app_names: list[str], detect: Found | None,
           requests: Iterator[dict[str, Any]] | list[dict[str, Any]]) -> dict[str, Any]:
    """One recorded task's step requests again, in order, through one Redactor — as the engine sends them.

    The requests are the audit's `decide` records, redacted when they were sent. What was hidden then stays
    hidden, under a pseudonym this redactor cannot reuse (⟦SENT_PERSON_2⟧), so what is measured is what
    redaction does to the rest: pseudonyms put inside words, the goal or the app's name changed, options
    changed, and what was left in place (`kept`, as the audit records it). Recorded text that was already
    hidden cannot show a word cut — this undercounts; it is for checking that nothing got worse.
    """
    r = Redactor(cfg, entities=entities, protect=lambda: app_names, detect=detect)
    looks = glued_looks = inside = goal = app = options = changed = 0
    kept: dict[str, dict[str, int]] = {"glued": {}, "in_a_name": {}, "replaced_glued": {}}
    examples: list[str] = []

    def sent_before(v: Any) -> Any:
        if isinstance(v, str):
            return _TOKEN.sub(lambda m: "⟦SENT_" + m.group(0)[1:], v)
        if isinstance(v, dict):
            return {k: sent_before(x) for k, x in v.items()}
        return [sent_before(x) for x in v] if isinstance(v, list) else v

    for req in requests:
        state = sent_before(req.get("state") or {})
        crit = sent_before(((req.get("questions") or {}).get("action") or {}).get("criteria") or {})
        sent = r.value({"state": state, "criteria": crit}, tally=kept)
        looks += 1
        n = 0
        for k, before in crit.items():
            after = sent["criteria"].get(k, before)
            options += 1
            if after != before:
                changed += 1
                got = inside_words(str(after), r.table)
                n += got
                if got and len(examples) < 8:
                    examples.append(str(after)[:160])
        for field in ("goal", "app", "window"):
            before, after = str(state.get(field) or ""), str(sent["state"].get(field) or "")
            if after != before:
                goal += field == "goal"
                app += field == "app"
                n += inside_words(after, r.table)
        inside += n
        glued_looks += n > 0
    return {"looks": looks, "looks_with_a_pseudonym_inside_a_word": glued_looks, "inside_words": inside,
            "goal_changed": goal, "app_changed": app, "options": options, "options_changed": changed,
            "kept": kept, "examples": examples}
