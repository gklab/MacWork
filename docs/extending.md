# 加一样新东西的时候

这个引擎能操作一个它从没见过的 app，靠的不是我们提前知道那个 app。靠的是两件事：

* **app 自己声明的功能** —— App Intents、脚本字典、NSServices、URL scheme、Shortcuts
* **UI 自己的设计逻辑** —— Accessibility 树、菜单、角色、每个元素声明支持的动作

所以规则只有一条：

> **不要把关于外面世界的知识写进这个仓库。**
> 尤其不要写用户可能装什么、可能用什么 —— 你永远不知道。

下面是怎么执行它。

---

## 三个问题，按顺序问

### ① 这是一个动作，还是一个循环？

这是最容易栽的地方，而且栽下去的代价最大。

一样新东西如果需要"做一步、看结果、再决定下一步"，**它不是一个 provider，它是一串
动作**。把它拆开，交给主回路去驱动；**绝不要再写一个循环。**

网上查资料曾经就栽在这里。它被写成一个子系统，于是它有了：自己的浏览器、自己的
身份、自己的步数预算、自己的 decider 调用，以及 —— 因为它绕开了主回路 —— 没有
facts、没有安全底线、没有 contract、没有隐私脱敏。拆开之后它是四个已经存在的动作：

```
打开浏览器        apps provider
搜索              浏览器自己的脚本命令，或地址栏（AX 里就是个文本框）
点开一条结果      window provider 的 press
读这一页          readall / vision
够了吗            主回路的 verified / done —— 它本来就在问这个
```

一行都不需要知道搜索引擎叫什么。用户换成任何浏览器都自动可用。

### ② Mac 自己描述了它吗？

有就写个 provider 去读，**代码里不放任何知识**。已经这么做的：

| 读什么 | 从哪读 |
|---|---|
| app 能做什么 | `Metadata.appintents/extract.actionsdata`、脚本字典、`NSServices`、`CFBundleURLTypes` |
| 界面有什么 | AX 树、菜单、`AXActionDescription`（系统给的、本地化的动作名） |
| 这个元素能干什么 | `AXUIElementIsAttributeSettable`，而不是"它的角色在不在我的表里" |
| 装了哪些 app | Spotlight + LaunchServices 回环，而不是四个写死的目录 |
| 这个文件有文字吗 | UTType 层级，而不是扩展名清单 |
| 这段文字里有电话吗 | `NSDataDetector`，而不是手写正则 |
| 屏幕该用什么语言读 | `Locale.preferredLanguages` ∩ 识别器支持的语言 |
| 按哪个键 | 当前键盘布局反查，而不是 US-ANSI 位置表 |

每一条都在 60 行以内。它们的共同点是：**换一台 Mac、换一种语言、换一个 app，
代码不用改。**

### ③ Mac 完全没描述它？

先确认这是真的 —— 我们错过一次。第一次断言"Chrome 的页面能通过 AX 读"，其实那
767 个 affordance 绝大部分是菜单项；真正量下来 Chrome 只暴露 41 个节点，全是
工具栏，两个 accessibility 标志都置位、等 5 秒也一样。

确实没有的时候，唯一的出路是看渲染结果（端上 OCR），并且**要把代价说清楚**，
而不是引入一个外来运行时把它绕过去。

---

## 判断一样东西是不是"带入了知识"

问：**用户明天装一个我们没听说过的东西，这行代码会不会错？**

```
❌  "duckduckgo" / "bing"                两个搜索引擎的名字
❌  'class="b_algo"'                     它们 HTML 的结构 —— 对方改版就静默失效
❌  '#mw-content-text'                   维基百科的正文 div，写在"通用"页面读取器里
❌  ["AXTextField", "AXTextArea", …]     角色不在表里的控件就不能输入
❌  [/Applications, /System/Applications] 装在别处的 app 等于不存在
❌  '删除|移除|抹掉'（作为唯一判据）      德语 Löschen 命中不了，安全底线就没了

✅  kAXPressAction / NSServices / CFBundleURLTypes    Apple 的词汇，不是谁的 app
✅  UTType.conforms(to: .text)                        问类型层级，不列格式
✅  AXUIElementIsAttributeSettable(AXValue)           问元素，不查角色表
✅  Locale.characterDirection(forLanguage:)           问系统，不判断语言
```

区别不在"有没有字符串常量"，在于**这个常量描述的是系统的词汇，还是外面的世界**。

一条重要的例外：**安全底线的词表可以留**，但它只能用来决定"先分类谁"，不能用来
决定"这个动作安不安全"。词表漏掉一个词的后果必须是"慢一点"，不能是"不问就做"。

---

## 落地形状

```toml
[project.entry-points."macwork.providers"]   # 发现能做什么   (ctx, observation) -> None
[project.entry-points."macwork.channels"]    # 执行它         (ctx, affordance, params) -> Outcome
[project.entry-points."macwork.deciders"]    # 决定做哪个      decide(state, questions) -> answers
```

新东西应该落成这三者之一，**再加零个新概念**。如果它装不进去，先回到问题 ①：
它多半是个循环，不是一个动作。

写完检查这几样是不是自动继承了 —— 如果没有，说明你绕过了主回路：

* 安全底线会分类它吗（`policy.py`，每个将要执行的动作都会）
* 它产出的值会进 facts 吗（没见过的值不许被写出去）
* 它发出去的文字会经过脱敏吗（`Gate` 是通往 decider 的唯一一扇门）
* 它算进任务的步数和成本预算吗
* 它做完之后有没有人核对它真的做到了（`contract.py`）

---

## 加完之后要量

这个项目里每一个"应该更好"的判断都被数据改过一次以上：

* 能力探测本来打算**替代**角色白名单 —— 量完发现只能取并集：AX 说"不可设"不等于
  "不能用"，引擎还有点击和击键两条兜底。
* "表外的 AX 动作都提供" —— 量完发现 `AXScrollToVisible` 出现在 441 个元素里的
  428 个上，而它们全都另有 `AXPress`。
* "保留滚动出视野的列表行" —— 量完发现那是每次观察 3 秒。
* scripting 命令的类型表本来以为是硬编码 —— 量完发现 32 条里 31 条卡在
  `specifier`，而 specifier 是代码，那是安全边界不是词表。

所以：**先量，再改；改完再量一次。** 用 `macwork surfaces`（只读，不执行任何动作）
看每片能力面现在能看到什么、花多久。
