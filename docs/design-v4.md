# macwork v4 — 从"能操作一个 app"到"接管一台 Mac"

v3 给了引擎三个缺失的概念：谁握着这台 Mac（hands）、一个值从哪来（facts）、一步承诺了什么（contract）。
它们让引擎在**一个 app 里**变得可靠。v4 要回答的是下一个问题：为什么它还接管不了**整台 Mac**。

症状还是各自被打过补丁：安全底线在德语界面上形同虚设；菜单栏右侧、别的桌面、装在子目录里的 app、
app 自己声明的能力，引擎统统看不见；一次 helper 超时之后，它读到的每一帧屏幕都错开一位而毫不知情。

这又是同一件事的三个面：**引擎在三个地方用自己写的东西，代替了系统本来能告诉它的事实。**
用词表代替判断，用固定清单代替系统枚举，用"发出去了就假设收到对的"代替应答校验。

---

## 四个概念

### 1. 底线是判断，不是词表

`policy.yaml` 的 `confirm.categories` 设计本身是对的：正则只找候选，判定交给 decider，且判定时不给屏幕文本，
所以页面内容说不动它。问题在于**前提是正则先命中**。11 个类别 × 中英两套，约 100 个词。
德语 `Löschen`、日语 `削除`、法语 `Supprimer`、俄语 `Удалить` 一个都不命中 →
`_floor_hits` 返回 `[]` → `_needs_confirm` 直接放行 → 不可逆动作**不问用户就执行**。

更糟的是 `_risky()`（`policy.py:76`）—— 纯词表、不经 decider —— 被用在四条**完全没有 decider 参与**的路径上：
`mac_act` 步级调用（`engine.py:163`）、routine 重放（`loop.py:523`）、`learn` 探索（`learn.py:63,101`）、
tidy 回退（`tidy.py:211`）。在非中英界面上，这四条路径的安全保护等于零。

**改法**：floor 不再以"正则命中"为前提。每个将要执行的动作都经 decider 分类一次，
按 `bundle|label|context` 缓存（机制已存在，`cache["floor.verdicts"]`）。
正则降级为**预热召回**——命中的在 `_floor_questions` 批量随行分类，省掉延迟，但不再是唯一入口。
没有分类结果时保守要求确认，而不是放行。

### 2. 能力来自系统，不来自仓库

凡是 Mac 自己能列出来的东西——它装了哪些 app、一个元素支持哪些动作、一个 app 声明了什么能力、
屏幕上有几个窗口、键盘上有哪些键——都必须问系统，不能写在这个仓库里。

现状与之相反的地方，以及代价：

| 写死的东西 | 代价 |
|---|---|
| `observe.apps.dirs` 四个目录（`config.yaml:58`，`System.swift:331` 又写了一遍） | 这台机器上目录扫描见 147 个 app，Spotlight 见 461 个。**`/System/Library/CoreServices/Finder.app` 都不在范围内。** |
| `action_labels` 九个 AX 动作，表外的丢弃（`observe.py:269`） | `AXRaise`/`AXCancel`/`AXDelete`/`AXZoomWindow` 和**所有 app 自定义 AX 动作**对引擎不存在 |
| `text_roles`/`select_roles`/`submit_roles`/`range_roles` 角色白名单 | 角色不在表里就不能输入、不能选中。`contenteditable` 组、非标准角色的 Web 输入框全部失效 |
| `keyCodes` 70 条 US-ANSI **位置**表（`System.swift:56`） | 德/法/JIS 键盘上 `cmd+[` 按的是另一个物理键；F13–F20、小键盘、媒体键缺失 |
| `url_schemes`/`document_types` 采集了但只当文字喂 planner（`appmodel.py:82`） | app 主动声明的 API 完全浪费；planner 的回复 schema（`planner.py:33`）连"打开 things:///add"都表达不了 |
| App Intents / NSServices / 菜单栏状态项 / 跨 Space 窗口 / 剪贴板 | 五片能力面**一行代码都没有** |
| `skip_first_menu: true` 按位置删掉 Apple 菜单 | 这就是"手写的 macOS 操作知识"，顺手删掉了"关于本机""系统设置""最近使用" |

### 3. 每个回答对得上它的问题

好几个地方，两样东西被**假设**是对应的，而没有任何校验：

| 地方 | 假设 | 出错时 |
|---|---|---|
| `Helper.call`（`helper.py:133-154`） | 读回来的这行就是刚发出去那个请求的响应 | 超时后连接不重置、迟到响应留在 buffer、**从不校验 `reply["id"]`** → 此后每次调用都错开一位，静默 |
| `Engine._last`（`engine.py:67`） | `act` 拿到的 id 出自最近那次 `observe` | MCP 是最多 40 个 worker 线程并发；两个 caller 交错 observe 后，A 的 `act` 作用在 B 的观察上 |
| `task.started`（`model.py:145`） | 恢复的任务和原任务共用一个预算时钟 | 用户回答 `need_confirm` 超过 90 秒，恢复后第一轮直接判超预算失败 |
| floor 侧线程（`loop.py:217`） | 侧线程和主线程可以共用一个 `Redactor` | 伪名表无锁并发写，可能 `RuntimeError: dictionary changed size during iteration`；join 超时后线程泄漏到下一步 |
| `input.idle`（`System.swift:264`） | HID 空闲时间 = 用户没在操作 | helper 自己注入的键鼠同样进 HID 流 → 引擎分不清"用户回来了"和"我刚打完字"，v3 的 hands 概念实际没落地 |
| `contract.kept`（`contract.py:52`） | 读不回字段 = 无法核验 | Secure Input 下 CGEvent 被静默丢弃，记成 "unverified" 而非失败 → 容易误报 done |
| `facts.source_of`（`facts.py:14`） | 文本能被切成 `[数字\|拉丁字母\|CJK]` | 纯西里尔/阿拉伯/韩文文本切出空列表 → 返回 `"no value"`（真值）→ **反幻觉护栏被绕过** |

### 4. 任务要能活过一次重启

`Engine.tasks` 是内存 dict，30 分钟 TTL，且 `_gc` 只在 `do()` 里调用。进程一重启，
所有 pending 任务、所有 `task.desktop` 记录（tidy 靠它知道该关什么）全部消失。
`_run` 全程持有一把 RLock，所以同时只能跑一个任务，而且 `mac_observe` 会被阻塞整个任务时长。
`_replay` 完全绕过步数预算、时间预算和取消检查。没有任何货币成本上限——一"步"可能发出七次决策往返。

---

## 工作流

五个批次，每批一组可独立评审的提交。**不跑评测**（见"验证"一节）。

### 批次 1 — 对账（概念 3）

不修这些，后面所有开发都建立在可能错位的反馈上。

| # | 改动 | 位置 |
|---|---|---|
| 0.1 | JSON-RPC 请求/响应 id 校验；超时也走 `_reset()` 重连，不再保留错位的流 | `helper.py:133-154` |
| 0.2 | helper 记录自身注入时刻，`input.idle` 返回 `{idle_s, since_synthetic_s}`；`take_hands` 用真实用户 idle | `System.swift:83-267`，`engine.py:243-260` |
| 0.3 | `IsSecureEventInputEnabled()` 前置检查，输入前明确拒绝；`contract.kept` 对 secure 目标返回 False 而非 None | `System.swift`，`act.py:180-211`，`contract.py:52` |
| 0.4 | `pieces()` 改用 Unicode 类别；`source_of` 空切分返回 `None` 而非 `"no value"` | `facts.py:14,60` |
| 0.5 | `Redactor` 加锁；floor 侧线程 join 超时后丢弃结果，不读半成品 | `privacy.py`，`loop.py:217-232` |
| 0.6 | `_replay` 纳入步数/时间预算与取消检查 | `loop.py:514-540` |
| 0.7 | `resume` 重置本轮预算窗口（改为"每轮预算"而非从 `task.started` 起算） | `model.py:145`，`loop.py:71` |
| 0.8 | `_last` 换成带 observation id 的句柄；`act` 校验 id 并重建 `Ctx`，不再用陈旧的 app/running/ref | `engine.py:67,145-172` |
| 0.9 | 脱敏失败终止任务，不再静默把全部文本变 `[withheld]` 继续跑 | `privacy.py:_learn` |
| 0.10 | audit 文件 0600 + 按大小轮转 | `privacy.py:197-202` |
| 0.11 | `_cancelled` 随任务 GC 清理；`_gc` 不再只在 `do()` 里触发 | `engine.py:225-237` |
| 0.12 | helper 重连后清空所有依赖 AX ref 的缓存（`menu.snap`/`ambient`/`vision.ocr`） | `helper.py:_reset` → engine 回调 |
| 0.13 | 每步决策调用数上限 + 单任务成本熔断（目前一步最多七次往返，无任何货币上限） | `engine.py`，`decider.py` |
| 0.14 | 清死代码：`Outcome.final`（无生产者）、`Affordance.score`（声明为"相关性排序"却从未使用，与项目原则相悖）、`ax.focused`/`Observation.focused`、`permissions.request`（接进 doctor） | 多处 |
| 0.15 | 三处 code/YAML 默认值不一致：`planner.context_chars` 800/600、`context_actions` 150/120、`settle_ms` 250/150 | `consult.py:44,47`，`effects.py:59` |

### 批次 2 — 安全底线去语言化（概念 1）

| # | 改动 | 位置 |
|---|---|---|
| 1.1 | `_floor` 去掉"正则未命中即 return []"短路；所有将要执行的动作都分类 | `policy.py:130-158` |
| 1.2 | `_floor_questions` 的批量预热覆盖决策器排名靠前的候选，使 `_pick` 时通常已命中缓存（延迟不变） | `policy.py:96-125` |
| 1.3 | `_risky()` 四条无 decider 路径：无分类结果时保守要求确认 | `policy.py:76-78` |
| 1.4 | fail 方向统一 fail-closed：`_goal_calls_for`/`_serves_goal` 的 `except DeciderError: return True` 改为要求确认；`_floor` 的失败判定不再永久缓存 | `policy.py:151,192,207` |
| 1.5 | `_nondescript` 不再匹配英文字面量 `"(no label)"` / `"in front of the app"`，改为结构判断 | `policy.py:84` |
| 1.6 | floor 判定缓存加 TTL 与 app 版本键 | `policy.py:87` |

### 批次 3 — 能力来自系统：新增能力面（概念 2，纯增量）

| # | 能力面 | 做法 |
|---|---|---|
| 2.1 | **App Intents 目录** | 解析 `Contents/Resources/Metadata.appintents/extract.actionsdata`（纯 JSON，本机 47 个 app 有）进 `appmodel.static_model`；经 `AppModels.brief` 喂 planner，作为 sdef 之外的第二套"app 自述能力"。**本批只做目录，不做调用** |
| 2.2 | **URL schemes 成为真动作** | 新 provider `schemes`，产出"用 ⟨app⟩ 打开一个 ⟨scheme⟩:// URL"带 url slot；planner 的 `TRY_ITEM` 增加 `open_url`；走 floor |
| 2.3 | **Services（NSServices）** | helper 新增 `services.list`（扫各 app bundle 的 `NSServices`）与 `services.perform`（`NSPerformService`，公开 AppKit API）；配套 provider + channel。这是 app 之间系统级的"把内容送过去"总线 |
| 2.4 | **菜单栏状态项** | `AX.swift` 增加 scope `extras_menubar`（`kAXExtrasMenuBarAttribute`）。解锁 WiFi/蓝牙/音量/输入法/所有第三方菜单栏 app |
| 2.5 | **跨 Space / 全窗口** | `screen.windows` 增加 `all` 参数（`.optionAll`）并标注是否在当前屏；windows provider 提供"切到另一个桌面的窗口" |
| 2.6 | **多显示器缩放** | 取窗口中心点所在屏幕，而非"第一个相交的屏幕" | `Vision.swift:35` |
| 2.7 | **剪贴板一等通道** | helper `clipboard.read`/`clipboard.write`；provider 报告当前剪贴板类型与摘要（脱敏后作 evidence），channel 可写入。目前剪贴板只在 `pasteRestoring` 内部用 |
| 2.8 | **系统 UI 可操作** | 把目标从"前台 app"放宽到任意可 AX 进程（Dock、控制中心、通知中心） |
| 2.9 | **文件内容进 facts** | `file` channel 增加 `read`；文本/PDF 文本层成为 `Facts` 的来源。目前 facts 只来自屏幕 |
| 2.10 | **拖放成为一等 affordance** | 基于 AX frame 产出"把 X 拖到 Y"。`input.drag` 早已实现，但只有 planner 猜标签名这一条路 |
| 2.11 | **AX 属性扩展** | `batchAttrs` 增加 `AXURL`/`AXSelectedText`/`AXExpanded`/`AXDisclosing`/`AXNumberOfCharacters` | `AX.swift:7` |

### 批次 4 — 去硬编码（概念 2，会改变行为）

**落地策略：新行为默认开启，每项在 config 留一个回退开关。**

| # | 改动 | 开关 |
|---|---|---|
| 3.1 | `apps.installed` 改走 LaunchServices：Spotlight 枚举 app bundle + `NSWorkspace.urlForApplication(withBundleIdentifier:)` 回环校验"LaunchServices 确实会启动这个路径"。不用任何路径词表 | `observe.apps.source: launchservices\|dirs` |
| 3.2 | AX 动作不再白名单：用 `AXUIElementCopyActionDescription`（系统返回本地化描述）给任意动作命名 | `observe.window.unknown_actions: offer\|skip` |
| 3.3 | 角色白名单 → 能力探测：`AXUIElementIsAttributeSettable(AXValue)` 判可输入、`AXSelected` 可设判可选 | `observe.window.by_capability` |
| 3.4 | 键盘：复用已有的 `asciiKeyMap()` 按当前布局反查，替掉 US-ANSI 位置表；删掉 `engine.key_pattern` 白名单，改为 helper 能解析即接受 | `input.keymap: layout\|ansi` |
| 3.5 | locale 全部取自系统：OCR 语言从 `AppleLanguages` + 屏幕文本脚本检测；web `locale`/UA 从系统与真实 Chromium 版本；`usesLanguageCorrection = false`（它会把 UI 标签"纠正"成配置语言）。统一现在**三处互相矛盾**的 OCR 语言默认值 | `observe.vision.languages: auto` |
| 3.6 | 隐私：`NSDataDetector`（系统自带，多语言多地区）替代手写电话/地址正则；`_NAMEISH`/`_RUNS`/`_CLAUSE`/`_short_name_like`/`_CARRIERS` 改用 Unicode script 属性 | `privacy.detector: system\|regex` |
| 3.7 | Apple 菜单不再按位置跳过；"关机"这类由 floor 分类拦截 | `observe.menu.skip_first_menu` |
| 3.8 | sdef 放开：枚举参数变成 choice slot（固定取值正是 Jev 的理想输入）；list/record 参数至少可选提供 | `observe.sdef.strict_types` |
| 3.9 | OCR 阅读顺序支持 RTL 与竖排 | `observe.py:514` |
| 3.10 | `arrange` 不再按组大小排序；改为 provider 顺序 + 明确告知 decider "这里被截断了" | `observe.py:684` |
| 3.11 | `observe.keys` 扩到 helper 支持的全部物理键（方向键、空格、delete、home/end） | `config.yaml:77` |
| 3.12 | 阈值收敛：10 个只存在于 Python 字面量的 config key 补进 YAML；标注 `safety`（不可自动标定）/ `performance`（可标定）两类 | 多处 |
| 3.13 | `group_of` 未知 channel 用 channel 名，不再叫 `"general"` | `observe.py:661` |
| 3.14 | 去掉 selftest 的中文 app 名硬编码、planner probe 的 `"Calculator"` | `selftest.py:64,76`，`planner.py:297` |

> 注意一个连带后果：`appmodel.signature()` 与 `Skills.find()` 都以标签文本为键，而标签是"英文脚手架 + 本地化 app 标题"。
> 所以**改一次 macOS 界面语言，所有学到的 app 模型和 routine 全部失效**。3.2/3.3 会让标签更依赖系统自述（本地化），
> 这个问题会更明显，需要在 signature 里把语言无关的部分（角色、动作名、层级位置）与标签文本分开。

### 批次 5 — 持久与常驻（概念 4）

| # | 改动 |
|---|---|
| 4.1 | 任务持久化：`sqlite3`（stdlib，无新依赖）落到 `~/Library/Application Support/macwork/tasks.db`；`Task` 可序列化；重启后能 resume，tidy 也能补做 |
| 4.2 | 状态分层：`self.cache` 拆成真全局（appmodels/skills/host.bundles/installed/privacy.vocab/floor.verdicts）与 per-task（vision.wanted/ambient/menu.snap/actions_done）。目前"某个任务要求 OCR 这个窗口"会永久影响之后所有任务 |
| 4.3 | 解开单锁：`_run` 不再全程持锁；helper 的串行由 `Helper._lock` 保证即可。`mac_observe` 不再被任务阻塞整个时长 |
| 4.4 | 预算分层：step / sub-goal / task 三层，长任务靠 checkpoint 续跑，而不是把 `max_steps` 调大 |
| 4.5 | 常驻：Developer ID 签名 + 稳定 bundle id + `SMAppService` 注册 LaunchAgent + 崩溃拉起。TCC 授权一次到位，不再每次 rebuild 失效 |
| 4.6 | 生命周期收尾：`atexit`/信号处理关闭 helper 子进程与 Playwright；`observe`/`web`/`learn` 三个永久 redactor 的伪名表加上限（目前进程存活期内无限增长） |

---

## 验证（不跑评测）

现在不占用这台机器跑 eval suite。替代手段：

1. **现有 85 个单测保持绿**，每个工作流补单测。`tests/conftest.py` 已有的 fake-helper 模式覆盖得到批次 1、2、4 的绝大部分。
2. **Swift 侧从零建测试 target**（现在一个测试都没有，CI 只 `swift build`）：`helper/Tests/` 覆盖 id 对账、Secure Input 检测、键盘布局反查、fingerprint、多屏缩放。CI 加 `swift test`。
3. **新增 `macwork surfaces` 只读命令**：不执行任何动作，打印当前前台 app 下每个 provider 看到了什么，以及新增能力面（App Intents / Services / 菜单栏状态项 / 跨 Space 窗口 / 剪贴板）各枚举到多少条。这是在不跑评测的前提下，肉眼确认"深度集成"收益的方式，也是批次 3、4 的验收口径。
4. **`macwork privacy-check` 语料扩到 8 种语言**。现在它只测中英——而 redactor 也只为中英写——所以它报的低泄漏率是自证的。
5. **`macwork doctor` 扩展**：逐项报告能力面可用性（AX / 屏幕录制 / 当前 Secure Input 状态 / LaunchServices / Spotlight / 检测到多少个带 App Intents 的 app）。
6. 修掉 `tests/test_engine.py::test_mcp_server_exposes_three_levels`——当前环境 `mcp` 版本没有 `mcp.server.mcpserver`，`server.py:13` 需要兼容或在 pyproject 里收紧下限。

评测留到机器空下来时再跑，那时批次 4 的每个开关都能直接做 A/B 对比。

---

## 顺序

批次 1 → 2 → 3 → 4 → 5。

1 和 2 是地基（对账 + 安全），必须先做完：在反馈可能错位、安全底线可能失效的情况下开发新能力，是在给自己造噪声。
3 是纯增量，风险最低、收益最直观。4 会改变行为，放在 `surfaces` 命令能给出对比之后。5 依赖 Developer ID，独立于前面四批。

---

## 进度

| 批次 | 状态 |
|---|---|
| 1 — 对账 | 完成（0.14 的死代码只清了 `Affordance.score`；`Outcome.final` 保留，它是第三方 channel 契约的一部分） |
| 2 — 安全底线去语言化 | 完成。计划里的 1.2 做了又撤回（预热非命中动作每步 +1 次请求，命中被选中动作的概率约 4%）；1.4 大部分是误判——`_goal_calls_for`/`_backs_out` 出错时本来就走安全方向，只有 `_floor` 缓存失败判定那一处需要改 |
| 3 — 新增能力面 | 完成。2.10 的做法与计划不同：macOS 没有"这个元素可拖"的属性，按元素提供拖放就得靠猜，所以改成**一个**按名字取两端的动作 |
| 4 — 去硬编码 | 未开始 |
| 5 — 持久与常驻 | 未开始 |

批次 3 在真机上核对过的数字：菜单栏状态项 20 个（9 个进程）、窗口 22 可见 / 118 全部、`NSPerformService` 用 Info.plist 的原始英文名在中文 macOS 上可调用、`file.read_text` 正确拒绝二进制而接受未注册扩展名的纯文本。
