"""Measure redaction instead of trusting it: a synthetic corpus of personal data (no real person's data), mixed
into ordinary UI strings, goes through the same Redactor and on-device tagger the engine uses. Reported: how much
leaks (per kind) and how often an app name on this Mac is hidden by mistake.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable

from .config import Config
from .privacy import Redactor

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
    n = len(items)
    return {"texts": n, "leaked": sum(v[0] for v in per_kind.values()),
            "leak_rate": round(sum(v[0] for v in per_kind.values()) / n, 3),
            "by_kind": {k: f"{v[0]}/{v[1]} leaked" for k, v in per_kind.items()},
            "app_names_checked": min(len(app_names), 80) * 3, "app_names_hidden_by_mistake": len(over),
            "ui_words_checked": len(UI_WORDS), "ui_words_hidden_by_mistake": len(ui_over),
            "examples": {"leaks": leaks[:8], "over_redacted": (over + ui_over)[:8]}}
