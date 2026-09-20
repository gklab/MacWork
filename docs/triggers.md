# 让它自己醒过来

引擎一直能被"叫去做事"，也能被恢复，但**从来没有东西唤醒过它**。daemon 就在那儿
跑着，而每一个任务都是因为有人开的头——这让它整个是一件工具，而不是住在这台 Mac
里的东西。

缺的不是调度器（macOS 有，`launchd` 就是在跑这个）。缺的是另一半：**"发生这件事的
时候，去做那件事"**。

```
macwork watch          # 一直等着
macwork watch --check  # 只说会监听什么，然后退出
```

规则写在 `~/.config/macwork/triggers.yaml`，**不随项目发布**。哪个 app、哪个文件夹
对你重要，正是这个项目不该预先知道的东西。

## 形状

```yaml
triggers:
  - name: 下载的发票归档
    when: {event: file.changed, path: ~/Downloads/*.pdf}
    do: 把这个 PDF 按发票日期重命名，放进"发票"文件夹
    debounce_s: 30

  - name: 解锁后看一眼日历
    when: {event: screen.unlocked}
    do: 看看今天还有哪些会议没开，告诉我下一个是几点

  - name: 某个 app 一起来就静音
    when: {event: app.launched, bundle_id: com.example.thing}
    do: 把系统音量调到静音
    debounce_s: 300
```

`when` 里每一条都要成立。`event` 之外都按通配符匹配（也接受正则），所以 `app.*`
或一整棵目录树不需要它自己发明一套语法。

| 键 | 匹配什么 |
|---|---|
| `event` | 事件种类，见下表 |
| `app` / `bundle_id` | 哪个 app（app 类事件才有） |
| `path` | 哪个文件（文件和卷事件才有） |
| `text` | 事件里任意字段 —— 兜底用 |

其余的键：`do`（必需，交给引擎的目标）、`inputs`、`debounce_s`（默认 30）、
`enabled`、`pass_event`（默认 true：把事件本身作为 `inputs.event` 传进任务）。

## 能监听什么

不是我列的，是 helper 自己报的（`events.watch` 的回复里有 `kinds`）：

```
app.launched   app.quit     app.activated   app.hidden
screen.woke    screen.slept screen.locked   screen.unlocked
volume.mounted volume.unmounted
file.changed   clipboard.changed
```

每一条都是**系统在announce它自己的事**：NSWorkspace 的通知、
`com.apple.screenIsLocked`、FSEvents、粘贴板的 change count。没有一条需要知道任何
一个 app 是什么。

只订阅规则用得上的：某条规则要监听文件夹，不该顺带把剪贴板也轮询起来。

## 它为什么不是一个 provider

触发器是**调用方**，和人、和 MCP 客户端是同一类东西。它从外面启动主回路，因此
自动继承安全底线、facts、脱敏和预算——这几样一个都不需要重新实现。
`macwork/watch.py` 里没有任何一行驱动这台 Mac；它只调 `engine.submit()`。

这正是 [extending.md](extending.md) 里第 ① 个问题的答案："这是一个动作，还是一个
循环？"——都不是。它是**回路的起点**。

## 一直开着

```bash
macwork daemon install     # launchd 里的 MCP 服务
```

daemon 目前跑的是 `macwork serve`。要让触发器常驻，把 plist 里的
`ProgramArguments` 改成 `watch`，或者两个都装（各自一个 Label）。

## 注意

* **防抖是默认行为不是可选项。** 往一个文件夹里写东西会一个文件触发一次；回应它的
  规则不该一个文件跑一次。默认 30 秒。
* **任务排队，不并发。** 一台 Mac 一个键盘；同时触发的规则按先后排队
  （`macwork doctor` 能看到队列）。
* **剪贴板事件只报形状，不报内容**（change count 和类型列表）。密码管理器把真东西
  放在那儿，而这个会一直开着。
* **错过的事件会说出来。** helper 的环形缓冲区有上限；离开太久回来时，日志里会讲
  漏了多少，而不是假装什么都没发生。
