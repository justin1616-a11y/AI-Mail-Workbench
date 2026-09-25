# 邮件工作台 · 全量审计报告

- 审计对象：`D:\WorkBuddy_Projects\mail-workbench-release`（发行包，81 个文件 / 2.1 MB）
- 审计时间：2026-09-25
- 运行环境：`http://127.0.0.1:8080`（真实邮箱 `jinyu_liu@sjtu.edu.cn`，库内 11k+ 封邮件）
- 结论（**两轮合计**）：
  - **81 个文件全部可用**；界面 **104 项**按钮/控件逐一实测，0 失败、零 console error
  - **修掉 15 个真实缺陷**（第一轮 8 个 + 第二轮 7 个，其中 5 个会让按钮「点了没反应」）
  - **补齐 15 项功能/可用性缺口**（第一轮 7 项 + 第二轮 8 项）
  - 移除 5 处死代码/死按钮；新增 1 套状态一致性审计（52 项）与 1 个后端回归测试
- 第二轮（状态同步）见文末 **「第二轮」** 一节 —— 那一轮由用户实测反馈触发。

> 审计用的是**发行包**（`mail-workbench-release`），不是开发工作区。
> 两者对「别人能用」的那部分文件逐字节一致（`diff -q` 全绿），
> 但开发工作区还带诊断脚本、截图、日志，不该发出去。

---

## 一、审计方法（可复现）

不做「看起来没问题」的结论，四项证据都留了脚本：

| # | 手段 | 脚本 | 结果 |
|---|---|---|---|
| 1 | **路由探活**：逐个打 V2 全部 67 条路由（31 GET + 36 POST），只读的真调、写接口发空参靠校验拒绝；破坏性的显式跳过 | `verification/_audit_endpoints.py` | 78 项探测，**0 项「路由缺失」** |
| 2 | **V1 端点探活**：`/api/*` 28 条 + 静态资源 + `/classic` | `verification/_audit_v1_endpoints.py` | 除已修的 `/classic` 外全部正常 |
| 3 | **字段契约核对**：把 UI 里每个 `r.data.x` / `r.xxx` 都拿去和真实响应比对（「路由通了但字段取错」是界面上「点了没反应」的头号成因） | `verification/_audit_contract.py` | 74 项字段**全部存在**；**抓到 5 处前端取错路径** |
| 4 | **界面全量按钮审计**（Playwright，真点击） | `verification/_audit_ui_buttons.cjs` | **104 通过 / 0 失败 / 0 跳过 / 零 console error** |
| 5 | **状态一致性审计**（第二轮新增，Playwright，全部可往返复原） | `verification/_audit_state_sync.cjs` | **52 通过 / 0 失败 / 0 跳过 / 零 console error** |

回归基线：`python -m unittest discover -s mail_workbench/tests -t .` → **241 passed**（原 236 + 新增 5）；
`uitest_v2_workbench.cjs` → **verdict: PASS**；`uitest2.cjs` → 全绿、遮挡异常 0、无 console error。

> 界面审计脚本的**安全原则**：会产生真实副作用的按钮（归档 / 删除 / 忽略 / 完成 / 标已读 /
> 加星标 / 发送 / 附件打开）**只验证「存在 + 可用 + 已绑定」，不真点**；
> 只读动作（简报 / 健康 / 帮助 / 外部客户端入口 / 搜索 / 取消选择 / 回到顶部）才真点。
> 唯一真写的是「快速草稿」，用来核对返回形状，跑完立刻 dismiss、净效果归零。

---

## 二、结论速览

```
                    第一轮    第二轮    合计
修掉的真实缺陷        8         7       15   （其中 5 个会让按钮「点了没反应」）
补齐的功能缺口        7         8       15
移除的死代码          5         -        5
建议移除（未动）      2         -        2   （mail.ico / 一批后端遗留端点，见 §六）
文件可用率         81 / 81
按钮可用率        104 / 104
一致性审计            -     52 / 52
单元测试          240 / 240  241 / 241
```

---

## 三、文件清单逐项判定（81 个）

### 3.1 文档（7）

| 文件 | 判定 | 说明 |
|---|---|---|
| `README.md` | ✅ 已修 | 使用者文档。**批量操作说明与实现不符**（写了「设为等待」但没有、漏了「标为未读 / 完成」）；**快捷键表只有 7 个，实际有 16 个**。已按实际更新，并补上 `?` 帮助与搜索语法入口 |
| `INSTALL.md` | ✅ 保留 | 安装向导说明，与 `install.py` 的步骤逐条对得上 |
| `SKILL.md` | ✅ 保留 | AI 侧技能清单（16 个子命令的说明与实际一致，已核对） |
| `LICENSE` | ✅ 保留 | MIT |
| `.gitignore` | ✅ 保留 | 隐私闸门写得很好（`config.json` / `data/` / `attachments/` / `*.cred` 全覆盖） |
| `docs/README.md` | ✅ 保留 | 371 B 的索引，指向下面两份 |
| `docs/ARCHITECTURE.md` | ✅ 已修 | 开发者文档。**含 3 个死链**（`V2-MIGRATION.md` / `README-V1.md` / `SKILL-V1.md` —— 只存在于开发仓库，发行包不含）；**数字过时**（「58 个端点」实为 67；「168 / 216 个测试」实为 240）。已全部修正并加注说明 |
| `docs/AGENT-CONTRACT.md` | ✅ 已修 | AI 协作契约。**开头残留了一段 skill frontmatter**（`name:` / `version: 2.1.0` / `agent_created: true`）—— 它是文档不是技能，那段元数据会误导；另有一处死链与过时测试数。已清理 |

### 3.2 入口脚本与安装（8）

| 文件 | 判定 | 说明 |
|---|---|---|
| `install.cmd` | ✅ 正常 | 找 Python → 起 `install.py`；找不到时给出 WorkBuddy 内置解释器的准确路径 |
| `install.py` | ✅ 正常 | 探测 Foxmail 索引、验证 IMAP 登录、写凭据到 `~/.workbuddy/secrets/`、建桌面图标 |
| `启动V2工作台.cmd` | ✅ 正常 | 主入口。幂等（已在跑就直接开浏览器）、等服务端口就绪（最多 40 s）、失败时列出三个日志的排查顺序。**实测重启路径可用** |
| `start.bat` | ✅ 保留 | 前台调试模式，日志直接打在窗口里。与主入口分工清楚，不是冗余 |
| `创建桌面图标.cmd` | ✅ 正常 | 用 PowerShell 取真实桌面路径（兼容 OneDrive 重定向） |
| `launch.py` | ✅ 正常 | 起 watchdog + server，幂等 |
| `watchdog.py` | ✅ 正常 | 15 s 探活、挂了拉起，带文件锁防重复守护。**本次就是靠它无损重启了服务** |
| `mail.ico` | ⚠️ **建议移除** | **全仓库零引用**（`grep -rn "mail.ico"` 只命中 git index）。`install.py:create_desktop_icon()` 建的是纯文本 `.cmd`，不带图标；`创建桌面图标.cmd` 同理。详见 §六 |

### 3.3 配置（1）

| 文件 | 判定 | 说明 |
|---|---|---|
| `config.example.json` | ✅ 正常 | 中性示例，**不含任何个人邮箱或自建文件夹名**（隐私自检已确认） |

### 3.4 后端（V1 入口 + V2 包，共 45）

| 文件 | 判定 | 说明 |
|---|---|---|
| `server.py` | ✅ 已修 | V1 服务入口（2100+ 行）。挂 V2 UI 到 `/workbench`、转发 `/api/v2/*`。**本次修 `/classic`（见 §四·缺陷 1）** |
| `reply_queue.py` | ✅ 保留 | V1 的 JSON 文件队列。V2 用 DraftJobQueue 取代，但**双轨并存**是有意设计（旧数据不丢、旧接口不改），不是死代码 |
| `mail_workbench/server.py` | ✅ 已修 | V2 路由 + App 装配。**67 条路由，探活 0 缺失**。本次改：`/constants` 补 4 个字段、`/draft-jobs/{id}/events` 补 404、`/classic` 指向真实文件名 |
| `mail_workbench/constants.py` | ✅ 已修 | 枚举唯一来源。新增 `REPLY_CONTROL_LABEL` / `REPLY_CONTROL_GROUP`（按钮文案，见 §五·F4） |
| `mail_workbench/config.py` | ✅ 保留 | 配置加载，凭据外置、支持环境变量优先 |
| `mail_workbench/util.py` / `metrics.py` | ✅ 保留 | 工具与本地指标（无 telemetry） |
| `mail_workbench/v1_bridge.py` | ✅ 保留 | 桥接层。含 3 个自有端点（`/api/v2/api/app`、`legacy/reply-queue`、`reply-queue/migrate`） |
| `mail_workbench/cli.py` | ✅ 保留 | **16 个子命令**，与 `SKILL.md` 的说明逐条一致 |
| `mail_workbench/classify_rules.default.json` | ✅ 保留 | 中性默认规则（用户自己的 `classify_rules.json` 永远优先） |
| `mail/`（6 个）`parser` `imap_client` `smtp_client` `sync_engine` `foxmail_index` `attachment_store` | ✅ 保留 | 基础设施层。IDLE 走裸 socket、modified UTF-7 往返、附件按 sha256 缓存 + 文件名消毒，都在 |
| `thread/`（2 个）`aggregator` `context_builder` | ✅ 保留 | 线程聚合 / 最小充分上下文 |
| `intelligence/`（3 个）`rule_engine` `fact_extractor` `reply_planner` | ✅ 保留 | 纯本地规则，不调模型 |
| `workflow/`（8 个） | ✅ 已修 | `actions.py` 本次改 `BATCH_ACTIONS` 补 `unread`；其余（state_machine / buckets / candidate_detector / snooze / followup / scheduler / brief）无问题 |
| `draft/`（4 个）`queue` `recovery` `templates` `worker_contract` | ✅ 保留 | DraftJobQueue 状态机 + 自愈 + 本地模板（8 类）+ Worker 契约 |
| `storage/`（3 个）`database` `repo` `search` | ✅ 保留 | SQLite WAL + 版本化迁移 + 搜索操作符解析 |
| `tests/`（3 个） | ✅ 已修+已扩充 | **240 个测试全过**（新增 4 个：`unread`、动作清单完整性、以及 2 个锁住「测试不许往项目目录写文件」）。`make_cfg` 缺 `attachments_dir` 导致跑测试会污染 `attachments/`，见缺陷 8 |

### 3.5 前端 V2 工作台（1）

| 文件 | 判定 | 说明 |
|---|---|---|
| `mail_workbench/ui/index.html` | ✅ 已修 | 单文件、原生 JS、无框架。本次改动最大：修 5 处取错字段 + 加 7 项功能。详见 §四、§五 |

### 3.6 前端 V1 经典视图（11）

`web/index.html` · `web/css/app.css` · `web/css/tokens.css` · `web/js/{main,api,store,classify,icons}.js` · `web/js/components/{rail,folders,list,reader,composer}.js`

| 判定 | 说明 |
|---|---|
| ✅ 可用，本轮做减法 | 11 个文件全部正常（`uitest2.cjs` 全绿、遮挡异常 0）。本次移除 3 处死代码：`api.js` 的 `replyConsume`（函数定义了但全仓库无人调用）、`main.js` 的 `draft-from` 分支（无任何渲染方）、`rail.js` 的 4 个无功能按钮（见 §五·R1）。`uitest_sidebar.log` 记录过它的完整功能，`/` 直接可访问，零回归 |

### 3.7 运行时占位目录（4 个 `.gitkeep`）

`attachments/` · `logs/` · `sent-log/` · `reply-queue/` —— ✅ 保留。`.gitkeep` 是必需的：这四个目录运行时必须存在，而内容被 `.gitignore` 排除，没有占位文件 git 不会保留空目录。

---

## 四、发现并修掉的 8 个真实缺陷

### 缺陷 1 — `/classic` 一直是 404，而文档承诺它可用 ⭐

**现象**：文档（`docs/ARCHITECTURE.md` 快速开始）写着
「`/` · `/classic` V1 经典三栏视图（**完整保留，直接敲 URL 就能到**）」。
实测 `GET /classic` → **404**。

**根因**：`mail_workbench/server.py` 去找 `inbox.html` —— 那个文件**从来不存在**，
V1 的页面叫 `web/index.html`。名字写错，于是这个入口从上线起就是死的。
（V1 的 `server.py` 里也根本没有 `/classic` 路由，`/` 才是它。）

**修复**：V2 侧按真实文件名找（并保留 `inbox.html` 作兼容回退）；
V1 侧补一条别名路由，把 `/classic` 重写成 `/index.html` 交给同一个静态处理器。
**实测**：`/classic` → **200**。

### 缺陷 2~6 — 5 处前端「取错返回路径」，按钮点了毫无反应 ⭐

**现象**：点「人工回复」不弹邮件客户端；点「外部客户端」没有任何提示；
点「延后」toast 不弹、**列表也不刷新**；点「快速草稿」提示永远是「已用『模板』生成草稿」。

**根因**：这两类端点的返回形状**不一样**，前端一律写成 `r.data.xxx`：

```
/api/actions      → 扁平返回 {ok, action, message, undo_id, snooze, mailto, hint, …}
/api/drafts/fast  → 统一信封 {ok, data:{job, template, …}}
```

取不到就抛 `TypeError`。因为是在 `async` 函数里抛的，**不会进 console**，
页面只是安静地什么都不做 —— 这正是「点了没反应」最难查的一种。

**实测证据**（`verification/_audit_contract.py` 打出的真实形状）：

```
action=open_external  顶层键=['action','hint','mailto','message_id','ok','subject']
   r.data 存在吗？= 不存在（所以 r.data.xxx 会抛 TypeError）
action=reply_manual   顶层键=['action','mailto','message','message_id','ok','undo_id']
   r.data 存在吗？= 不存在
/drafts/fast          顶层键=['data','ok']
   r.template = null   ← UI 读的是这个
   r.data.template.kind = "ack"   ← 真实位置
```

**修复**（5 处）：`snooze` 读 `r.snooze.wake_at`、`open_external` 读 `r.hint`、
`reply_manual` 读 `r.mailto`、`reply_fast` 与「换模板」读 `r.data.template`。

### 缺陷 7 — 明知会失败的动作，界面照样给点 ⭐（从线上日志里发现的）

**发现方式**：审计期间翻 `_server.log`，看到**真实浏览器会话**里连着三条

```
POST /api/v2/actions 502
POST /api/v2/actions 502
POST /api/v2/actions 502
```

夹在同一封 `<foxmail-6308@mail-workbench.local>` 的详情反复刷新之间 ——
是有人在 Foxmail 历史记录上依次点了「归档 / 星标 / 未读」这类按钮。

**根因**：Foxmail 历史记录**在服务器上不存在**（正文都在 Foxmail 里加密存放），
所以一切「改服务器标志」的动作注定返回
`该邮件没有服务器 UID（可能是 Foxmail 历史记录），无法改服务器状态`。
界面早就知道这一封是历史记录（详情里就印着「Foxmail 历史（仅元数据）」的标签），
却仍然把按钮亮着让人点。

**修复**：详情页对 `source === "foxmail"` 的邮件，直接把
**归档 / 星标 / 未读 / 删除**四个按钮 `disabled`，并把原因写进 `title`。
`完成 / 忽略 / 等对方` 不禁用 —— 它们会退化成「只改本地工作流状态」
并如实告知服务器标志没同步，本身是有意义的操作。

---

### 缺陷 8 — 跑一次测试，垃圾就进了发布目录的 `attachments/` ⭐（打包/隐私卫生）

**现象**：准备上传时发现 `mail-workbench-release/attachments/` 下多出两个文件：

```
1ad9615552126eb8_report.pdf
1ad9615552126eb8_简历.pdf      ← 文件名是「简历」，最敏感的那一类
```

**排查**：内容都是 7 字节的 `PDFDATA` —— 是**测试固件**，不是真人数据（所幸）。
但机制本身很危险：

**根因**：`test_bridge.py` 的附件端点测试会真的下载/打开附件，
而 `test_v2.py:make_cfg()` **没有设置 `attachments_dir`** →
`attachment_store` 回落到 `<project_root>/attachments`。
`project_root` 是按包路径算出来的 —— 于是在发布目录里跑测试，
固件就写进了发布目录；接着整目录复制上传，它们跟着走。

`.gitignore` 里 `attachments/*` 是拦住的，但**拦截发生在 git 那一层，
「打包 / 复制到上传目录」这一步没人管**。

**修复**（三层，缺一不可）：
1. `make_cfg()` 补 `attachments_dir = <db 同目录>/attachments` ——
   所有测试的落盘目录都跟着临时 db 走，与项目目录彻底隔离。
2. 新增 2 个测试把这条钉死：断言 `attachments_dir / logs_dir / data_dir`
   都在临时目录内，并且 `attachment_store.ensure_dir()/local_path()` 的实际
   落盘路径也在临时目录内。
3. **上传同步脚本加闸门**：按 `.gitignore` 的同一套规则过滤
   （附件内容、`*.db`、`*.log`、`__pycache__`、`config.json`、`*.cred`
   一律不带，只留 `.gitkeep` 占位）。

**验证**：修完后在发布目录里跑完整套测试（240 个），
`release/attachments/` 里**只剩 `.gitkeep`**；上传目录 82 个文件、
零附件内容、零数据库、零日志，与发布目录逐字节一致。

> 附带说一句：这个事故值得记下来，因为它是**「测试污染发布物」**这一类
> —— 平时完全看不出来，只在真的要发出去的那一刻才暴露，而那时候
> 泄出去的往往正是最不该泄的东西（附件目录里最常出现的就是简历、合同、发票）。

---

---

## 五、补齐的 7 项功能缺口（对照开源实现）

参照对象：**Roundcube** / **SnappyMail** / **Mailspring** / **Aerion** / Gmail / Spark。
比对的结论是：本工作台在「工作流层」上比它们都更专注（工作桶 / 草稿门禁 / 缺信息门禁，
这些是它们没有的），但在**几项被所有成熟客户端视为标配的能力上反而缺失**。

### F1 会话 / 线程视图 ⭐ 最大的一处缺口

Mailspring、Roundcube、SnappyMail（`IMAP THREAD`）、Gmail 全都有会话视图。
本工作台**按线程算工作桶**，右栏却只显示单封邮件 ——
用户处理第 4 轮回复时看不到前三轮说了什么。
后端 `/threads/{id}` 一直提供完整消息序列（含收发方向判定），**只是从来没人调用**。

→ 详情面板新增「🧵 本线程 N 封」可折叠条：`↘ 收到 / ↗ 我发出`、发件人、摘要、时间，
点任一条切过去；当前这封有主色竖线标记。只有 >1 封才出现，不占位置。

### F2 快捷键帮助浮层（`?`）

Gmail 的 `?`、Spark 的命令栏、Aerion 的快捷键页。本工作台原来只有底部一行
挤着的 13 个 kbd 标签，没有一份能查全的表。文档里也只列了 7 个键，而实际有 16 个。

→ 按 `?` 出完整按键表（16 条，含每条用途），底部状态栏补 `g` / `?` 提示。

### F3 搜索语法帮助

`SEARCH_HELP` 一直躺在后端常量里（`/constants → search_help`）**没有任何界面展示它**，
用户只能从输入框的 placeholder 猜。
→ 搜索框边加 `?` 按钮，弹出完整语法（9 个操作符 + 组合示例 + AND 语义），并可一键「去搜索」。

### F4 草稿模式 / 快捷改写按钮中文化 ⭐

后端 `REPLY_CONTROLS` 的**键是给 Worker 读的标识符**，值是给模型的一整句指令。
UI 却直接把键当按钮文字渲染 —— 界面上是一排：

```
quick  normal  detailed  formal  academic
shorter  longer  more_formal  more_friendly  more_direct  chinese  english
bilingual  regenerate  fix_typo  strip_boilerplate
```

→ 新增 `REPLY_CONTROL_LABEL` / `REPLY_CONTROL_GROUP` 常量（`/constants` 下发），
按钮变成中文（更短 / 更正式 / 换成中文 …），并按
**长度 / 语气 / 语言 / 其它** 分组（用户找的是「想让它变成什么样」，不是按字母序找标识符）。
模式按钮同理（快速回复 / 标准回复 / 详细回复 …），完整说明放 `title`。

### F5 批量「标为未读」（V1 有，V2 丢了）

`BATCH_ACTIONS` 缺 `unread`，于是 V2 批量条里根本没有这个按钮。
点开一封会自动标已读（标准行为），**点错了却退不回来** —— 而单个动作 `apply('unread')`
一直支持，缺的只是批处理入口。
→ `BATCH_ACTIONS` 补 `unread`、`_BATCH_FLAGS` 补 `("-", ["\\Seen"])`（与逐封路径**逐字一致**，
否则会出现「本地改了、服务器没改」的分叉）。同时把批量按钮清单改成**由后端下发**
（`/constants → batch_actions`），以后后端加动作界面自动出现，不会再各写一份各自演化。
顺带后端已支持的 `完成` / `等对方` 也在界面上出现了。

### F6 延后：从 `prompt()` 换成预设面板

原来弹一个 `window.prompt`，默认值 `"tomorrow"`，提示语是
`"延后到？（later_today / tomorrow / next_week / +3d / 2026-09-25）"` ——
用户得先知道我们的内部暗号才能用。而 `SNOOZE_PRESETS` 常量在代码里躺着**从未被使用**。
→ 改为预设面板：今天稍后 / 明天 / 下周 / 3 天后 / 2 周后 / 1 个月后 + `datetime-local` 自定义；
预设常量真正用起来了。Esc 可关。

### F7 写邮件：本机暂存 + 关闭确认

这个弹窗以前关掉就什么都没了 —— 写了两百字、手滑点「关闭」或误按 Esc，全部白写。
Gmail / Roundcube / Aerion 都会自动存草稿。
→ 打字即暂存到 `localStorage`（**不写邮箱草稿箱** —— 那要用户明确点「存入草稿箱」，
所以不会在别的客户端里凭空多出一封写了一半的信）；
点「写邮件」时自动恢复；存入草稿箱 / 发送成功后自动清掉；内容清空也自动清掉；
关闭前确认一次。

---

## 六、移除 / 建议移除清单

### 6.1 已移除（死代码与死按钮）

| 位置 | 是什么 | 为什么可以删 |
|---|---|---|
| `web/js/components/rail.js` | **左侧导航条上 4 个按钮**：会话 / 任务 / 技能 / 文件 | 这个项目里从未实现，点下去只弹「『会话』空间还没接进来」。四个按钮、四条死路，是纯噪音，还让人以为功能坏了。`data-nav` 语义标记保留（回归测试按它定位），行为改成「点当前空间 = 重新拉取当前文件夹」 |
| `web/js/main.js` | `draft-from` 动作分支 | `grep -ro 'data-action="draft-from"'` → **0 处渲染**，永远不会被触发 |
| `web/js/api.js` | `api.replyConsume()` | 全仓库（含 WorkBuddy 技能目录、文档）**零调用方**。后端 `/api/reply-consume` 仍在（那是 AI 侧契约，未动） |

### 6.2 建议移除（本轮**未动**，等你决定）

| 位置 | 是什么 | 证据 | 为什么我没直接删 |
|---|---|---|---|
| `mail.ico`（20 KB） | 应用图标 | `grep -rn "mail.ico"` → 只命中 `.git/index`，**零业务引用**。`install.py:create_desktop_icon()` 建的是纯文本 `.cmd`（注释说明是刻意避开 COM/.lnk 以免被安全策略拦），`创建桌面图标.cmd` 同理 | 两个选择：(a) 直接删；(b) **把它接上** —— 用 PowerShell 的 `WScript.Shell` 建带图标的 `.lnk`，失败则回退 `.cmd`。我倾向 (b)（仓库里已经有这个资源，桌面图标也确实该有），但那会改动**安装期**逻辑 —— 对别人来说装不上是硬故障，而我无法在这台机器上验证所有安装环境。所以留给你定 |
| `server.py` 的 V1 遗留端点（约 6 条） | `/api/tag`、`/api/reply-consume`、`/api/reply-status`、`/api/reply-complete`、`/api/reply-purge`、`/api/reply-autopick`、`/api/reply-trace` | 实测：**全部零 UI 调用方**；在 `~/.workbuddy/skills` 与 `connectors` 下 `grep` 也**零引用**；文档里也没写（`/api/tag` 连文档都没有 —— V1 的标签树后来改成前端本地筛选，注释里写明了原因） | 它们是**能跑的**（探活全部 200），不是坏代码。其中 `reply-*` 一组是 V1 时代 AI 侧的写回通道，`purge` 是「我现在就想看着队列变干净」的手动清理入口 —— 删掉是省 60 行，留着可能在排查时救一次急。这属于**你的偏好**，不是技术问题 |

---

## 七、按钮逐项判定（V2 工作台，104 项全通过）

自动化产出：`verification/_audit_ui_buttons.json`（`passed: 104 / failed: 0 / skipped: 0`）。

### 7.1 顶栏（7）

| 控件 | 实测结果 |
|---|---|
| 健康指示灯 | ✅ 存在；绿/黄/红三态，`title` 带问题明细与状态 |
| 账号 chip | ✅ 显示 `jinyu_liu@sjtu.edu.cn` |
| 搜索框 | ✅ 输入 + 回车 → 进入搜索视图，列表头显示条数 / 命中说明 / 「退出搜索」 |
| 搜索 `?` | ✅ **新增** 弹出完整搜索语法 |
| 今日简报 | ✅ 渲染简报 + **7 项 KPI** |
| 健康 | ✅ 渲染组件健康表 |
| 同步 | ✅ 真触发 `POST /api/v2/sync`，回显新增封数 |

### 7.2 侧边栏（全部真点）

| 控件 | 实测结果 |
|---|---|
| ✏ 写邮件 | ✅ 打开弹窗；含暂存 / 恢复 / 关闭确认（**新增**） |
| 工作桶 ×5（待我处理 / 等待对方回复 / AI 草稿 / 已延后 / 今天已完成） | ✅ **逐个点过**，视图都切过去 |
| · 超过 N 天没回音（沉底区） | ✅ 计数 >0 才出现；点击切视图 |
| 📢 仅知会 | ✅ 切视图 |
| 文件夹 ×N | ✅ 逐个点过（含中文自建文件夹），切视图正常 |
| 标签 ×5 | ✅ 逐个点过 |
| 底部「本地索引 N 封可检索 · 库内合计 M 封 · K 个线程」 | ✅ 数据来自两个来源的并集 |
| 草稿流水线（排队/缺信息/待审核/待发送） | ✅ 仅在真有活跃任务时出现，不常驻占位 |

### 7.3 列表区

| 控件 | 实测结果 |
|---|---|
| ☐ 全选（三态 + 取消全选） | ✅ 全选 / 部分（横杠）/ 未选；`title` 写明「全选当前列表（N 封）」 |
| 批量按钮 ×8 | ✅ **含新增的「标为未读」**；清单由后端下发；选中后全部渲染且可用；批量中统一禁用 |
| 取消选择 | ✅ 清空，选中数回到 0 |
| 仅未读 N / ↩ 显示全部 | ✅ 真点：筛选后列表内**全是未读** |
| 邮件行点击 | ✅ 右侧载入详情 |
| 行内复选框 | ✅ 只选中，不触发打开（不冒泡成选中） |
| ↑ 回到顶部 | ✅ 滚过 600 px 淡入；点击回顶且按钮隐去；**重绘后显隐正确重算** |
| 空态文案 | ✅ 按视图区分（工作桶说「没有要处理的」，文件夹说「空的或还没同步」） |

### 7.4 详情面板（12 个动作按钮）

| 按钮 | 实测结果 |
|---|---|
| ✎ AI 起草 | ✅ 存在可用（会写库，未真点）；`queued` 态专门解释「在等 Worker 认领」 |
| ⚡ 快速草稿 | ✅ **真点验证**：模板生成 → `r.data.template` 取到 → 返回形状正确，跑完 dismiss |
| 人工回复 | ✅ **真点**：成功打开 `mailto:`（修复前不弹） |
| 归档 / 延后 / 等对方 / 完成 / 忽略 / 星标 / 未读 / 删除 | ✅ 全部存在且已绑定后端动作（会改邮箱，未真点）；**在 Foxmail 历史记录上，归档/星标/未读/删除会自动禁用并写明原因**（见缺陷 7） |
| 外部客户端 | ✅ **真点**：弹出规范说明（修复前无任何反应） |
| 附件名 / ⤓ / 📂 | ✅ 三个控件齐备并已绑定（会打开本机文件，未真点）；历史邮件显示「不可下载」并说明原因 |
| 草稿面板（模式 ×5 / 改写 ×11 / 保存 / 复制 / 提交缺信息 / 确认发送 / 真正发送 / 撤回 / 放弃） | ✅ 全部渲染且已绑定；`sent` 后自动只读并隐藏「保存/改写」；按钮**中文化**（**新增**） |
| 🧵 本线程 N 封 | ✅ **新增** 展开列出线程全部邮件，点任一封切过去 |

### 7.5 快捷键（16 个，逐个实测）

`j` `k` `Enter` `r` `a` `s` `d` `w` `e` `x` `u` `c` `g` `b` `/` `?` `Esc`
—— `g`（刷新）、`b`（简报）、`?`（帮助）、`/`（搜索）、`Esc`（关弹窗/退输入框/退搜索）实测生效；
其余为动作键，已验证已绑定。底部状态栏的 14 个提示标签全部存在（实测逐项列出）。

### 7.6 V1 经典视图（`/` 与 `/classic`）

`uitest2.cjs` 实测：**12 项命中测试全绿、遮挡异常 0、真实点击 6 项全绿、`ERRORS = none`**。
写邮件弹窗、文件夹 / 标签切换、未读/带附件筛选、搜索、附件、标已读/未读/星标、
移到已删除邮件 + 20 秒撤销条、AI 起草队列四态、实时同步状态灯 —— 全部可用。
`/classic` 本轮补通（见缺陷 1）。

---

## 八、验证证据汇总

| 项目 | 命令 | 结果 |
|---|---|---|
| 单元 / 回归测试 | `python -X utf8 -m unittest discover -s mail_workbench/tests -t .` | **240 passed, OK** |
| 性能（5000 封） | 随测试附带 | 工作桶 38 ms / 列表 1.0 ms / 搜索 4.6 ms / 简报 75 ms（目标 < 200 ms） |
| V2 界面自检 | `node uitest_v2_workbench.cjs` | **verdict: PASS**，`failures: []`，`console_errors: []` |
| V1 界面回归 | `node uitest2.cjs` | 全绿，`ERRORS = none` |
| V1 多视口 | `node uitest3.cjs` | 仅剩**改动前就存在**的失败（见下） |
| 界面全量按钮审计 | `node verification/_audit_ui_buttons.cjs` | **104 / 0 / 0**，零 console error |
| 路由探活 | `python -X utf8 verification/_audit_endpoints.py` | 0 项「路由缺失」 |
| 字段契约核对 | `python -X utf8 verification/_audit_contract.py` | 74 项字段全部存在 |
| Python / JS 语法 | `compileall` + 内联脚本过 `node --check` | 通过（64 903 字符的内联脚本 OK） |

### 已知且**本次未处理**的问题（都不影响使用，但应当知道）

1. `uitest3.cjs` 有 6 项失败，**用改动前的代码跑同样失败**（已用 git 里的旧版文件做对照基线）：
   - 「文件夹 未来电池中心 不存在」—— 该自建文件夹当前没被同步（`sync_folders` 是个人配置）
   - 「列表最后一项 不存在」「文件夹 INBOX / 标签 ★重点 / 写邮件 尺寸0」—— 出现在 820×945 与
     900×700 两个窄视口，是 `uitest3.cjs` 自身的多视口滚动/尺寸断言问题，与本次改动无关
2. `web/`（V1 三栏视图）与 `mail_workbench/ui/`（V2 工作台）是**两套并行的前端**。
   发布包的使用者文档（`README.md`）只讲 V2，而开发者文档（`docs/ARCHITECTURE.md`）
   说 V1「完整保留」。这不是 bug，但长期是维护成本 —— 值得你决定要不要收成一套
3. 单账号单用户设计，没有「统一收件箱 / 多账号 / 深色模式 / 移动端适配」——
   与开源实现相比是明确的取舍（本地单人工具），不是缺失

---

## 九、本次改动清单（按文件）

```
mail_workbench/ui/index.html          修 5 处取错字段 + 7 项新功能 + 3 个审计抓出的缺陷
mail_workbench/workflow/actions.py    BATCH_ACTIONS 补 unread；_BATCH_FLAGS 补 ("-", \Seen)
mail_workbench/constants.py           新增 REPLY_CONTROL_LABEL / REPLY_CONTROL_GROUP
mail_workbench/server.py              /constants 补 4 字段；events 补 404；/classic 指向真实文件
mail_workbench/tests/test_v2.py       make_cfg 补 attachments_dir（修测试污染）+ 新增 4 个测试
server.py                             /classic 别名路由
web/js/components/rail.js             移除 4 个无功能按钮；Mails 卡片改为「刷新当前文件夹」
web/js/main.js                        移除 draft-from 死分支与 data-nav 死分支
web/js/api.js                         移除无调用方的 replyConsume
README.md                             批量操作与快捷键表按实际更新；补 ? 与搜索语法入口
docs/ARCHITECTURE.md                  修 3 个死链 + 过时数字（端点 58→67、测试 168/216→240）
docs/AGENT-CONTRACT.md                清理残留 skill frontmatter；修 1 个死链 + 过时测试数
```

新增（仅开发仓库，不进发行包）：`verification/_audit_endpoints.py`、
`verification/_audit_v1_endpoints.py`、`verification/_audit_contract.py`、
`verification/_audit_ui_buttons.cjs`。


---

# 第二轮（2026-09-25 晚）：状态同步修复 + 可用性提升

用户第二轮反馈三条：

1. **「我已阅读完邮件、点击『完成』等按钮后，左侧 Inbox 等导航栏的未读/数量计数应自动同步更新」**
2. **「中部邮件删除或标记已读后的颜色/状态变化」**
3. **「放弃草稿后又返回时界面毫无反应」**（附截图：状态已是「已放弃」，面板却仍摆着
   可编辑正文框、保存修改、模板、模式、快捷改写）

这三条表面上是三个 bug，实际是**同一个根因**：
**一处局部操作牵动了四处全局状态，而这四处各自为政、没有人负责收尾。**

---

## 一、第二轮修掉的 7 个真实缺陷

### 缺陷 9 — 单封操作后，左侧计数不更新 ⭐（用户报的第 1 条）

**现象**：点「完成」/「归档」/「标为已读」之后，左侧 Inbox 的未读数纹丝不动；
按 `g` 手动刷新才发现其实早就成功了。

**根因**：`afterAction()`（所有单封动作的收尾）**只调用了 `loadBuckets()`**，
没有 `loadFolders()` —— 而侧栏的文件夹未读/总数来自 `S.folders`。
于是它成了一个 45 秒才追平一次的陈旧数字（页面另有一个 45s 定时刷新兜底），
用户在那 45 秒里看到的是「操作没生效」。

**修复**：`afterAction()` 并行拉取工作桶与文件夹，一次性对齐。
另外给 SSE 补了 `action` / `batch` 两个监听：
**只刷新两侧计数、不动当前列表**（`refreshAll()` 会 `loadScope → select(0)`，
把用户正在看的那封换掉 —— 那比数字不更新更糟）。
这样连「另一个窗口做了操作」也能同步。

### 缺陷 10 — 标已读 / 删除之后，列表行不变（用户报的第 2 条）

**现象**：在文件夹视图里标已读，行上的未读小蓝点还亮着；删除之后那一行还赖在列表里
（服务端其实已经把它过滤掉了）。

**根因**：`afterAction()` 对**工作桶视图**是重建（`S.items = S.buckets[id]`），
对**文件夹/标签视图**什么都不做 —— `S.items` 还是那批旧对象。

**修复**：新增 `patchFolderItem()` —— 只向服务端要**这一封**的最新状态，就地打补丁：
未读/星标/分类/工作流状态同步过来；如果它已经**不该在这个列表里**
（`deleted=true`，或「仅未读」下已经不是未读），就把它移出去，
**判定规则与服务端 `/messages` 完全一致**（否则就会留下幽灵行）。
刻意不整批重拉：那会冲掉滚动位置和用户的选择。

### 缺陷 11 — 幽灵选中：一行没勾，批量条却写着「已选 3 封」

**根因**：动作把某一行移出列表后，`S.selected` 里还留着它的 id。
`renderList()` 按 `selected.size` 决定要不要显示批量条，于是出现
「没有任何勾选框是亮的，批量条却在，而且点下去会对已经不在这批列表里的邮件动手」。

**修复**：新增 `pruneSelection()`，在每次列表内容变化后按 `S.items` 收缩选中集合。
（跨视图切换本来就会清空选择 —— 那条路径是对的，漏的是「同一视图内成员变化」。）

### 缺陷 12 — 草稿终态仍可编辑，点了就报错 ⭐（用户报的第 3 条，有截图）

**现象**（用户截图）：任务状态已经是「已放弃」，面板上却照样是
可编辑的正文框 + 「保存修改」+ 快速草稿模板 + 模式切换 + 快捷改写。

**线上日志给出了同一场景的现场证据**：

```
POST /api/v2/drafts/fast                             ← 点了「快速草稿」
POST /api/v2/drafts/job_9c52ed9d3faa4c55/dismiss     ← 点了「放弃草稿」
POST /api/v2/drafts/job_9c52ed9d3faa4c55/edit  409   ← 回来又点「保存修改」→ 被拒
POST /api/v2/drafts/job_9c52ed9d3faa4c55/edit  409   ← 用户又点了一次
```

**根因**：`renderJobPanel` 里只判了一种终态 —— `const sentDone = (st==="sent")`。
`dismissed` / `expired` / `failed` 全都漏了，于是它们按「还能改」渲染。

**修复**：终态与失败一律**冻结**，并且按后端给的权威能力位渲染（不再自己拼状态字符串）：

| 状态 | 面板表现 |
|---|---|
| `sent` / `dismissed` / `expired` / `failed` | 正文只读、去掉「保存修改」「模板」「模式」「快捷改写」；顶部一条说明「这任务已结束 / 接下来能做什么」；**提供「重新起草」** |

「重新起草」走 `POST /draft-jobs`（幂等创建）：终态任务不占幂等键，所以会真的建新任务；
若这封邮件已有活跃任务，后端返回那一个，不会重复劳动。
**这条路径以前完全不存在** —— 用户放弃草稿之后，界面上没有任何出路，这才是
「界面毫无反应」的完整含义。

### 缺陷 13 — 假成功：失败也报「已放弃」「已撤回」

三处 `.then(()=>{ toast("成功") })` **完全不看 `r.ok`**：

| 位置 | 后果 |
|---|---|
| 放弃草稿 | `approved` 状态会被状态机拒绝（必须先撤回），界面却报「已放弃草稿」 |
| 撤回确认 | 撤回失败时报「已撤回」→ 用户以为令牌作废了，其实还停在「已确认待发送」，**下一封可能误发** |
| 切换模式 / 快捷改写 | 失败时既不提示也不改界面 → 又一个「点了没反应」 |

**修复**：全部改成「成功有说法、失败有原因、异常也有兜底」，
并且失败时**不再顺手刷新界面**（那会把界面刷成「看起来已经处理过了」，
而服务器其实没动 —— 这是最容易误导人的一种表现）。

### 缺陷 14 — 伪文件夹「(Foxmail 历史)」的过滤是空操作

**现象**：`GET /messages?folder=(Foxmail 历史)` 返回的是**全库最新的一批**
（`total=11115`，全是 IMAP 邮件），跟「Foxmail 历史」毫不相干。

**根因**：路由里写着「伪文件夹：改用 source 过滤」，实际却只把 `folder` 置空、
**忘了设 `source`** —— 于是等价于不带任何过滤。

**修复**：真的按 `source='foxmail'` 过滤（现在 `total=6696`）。
并补了一个单元测试把这条钉死 —— 这类 bug **不报错**，只能靠测试盯住。

### 缺陷 15 — 撤销只有一个快捷键入口，没人发现

**现象**：删除/归档/批量操作之后，界面上**没有**任何「撤销」按钮 ——
只有底部一行小字 `<kbd>u</kbd> 撤销`。V1 明明有一条常驻撤销条，V2 把它丢了。

**修复**：列表头下方加回**可见的撤销条**：「可撤销：**批量删除**（12 封）　[撤销] [×]」。
只对**新发生的**操作显示（进页面时把基线对齐到当前最后一条 undo，
否则一进来就挂着一条几小时前的旧记录，那不是后悔药，是噪音）。
批量操作同样会挂出来 —— 145 封误删没有出口是不可接受的。

---

## 二、第二轮顺带补齐的可用性

| 项 | 说明 |
|---|---|
| **单封「标为已读」** | 原来详情页**只有「未读」**（永远标未读），单封标已读只能勾选它再走批量。现在「已读 ⇄ 未读」是一对真开关，按钮文案跟随当前状态 |
| **星标也是真开关** | 原来「星标」永远加星，**已加星的没有取消入口**。现在显示「☆ 星标 / ★ 已星标」，再点即撤销 |
| **撤销条** | 见缺陷 15 |
| **失败的人话解释** | 在 Foxmail 历史记录上点 IMAP 类动作会拿到「没有服务器 UID」这种技术话术，现在补一句「这封在服务器上不存在，只能看、不能改标记」 |
| **面板可测性** | 草稿面板根节点加 `data-job="<job_id>"`，自检脚本能确认「面板显示的确实是这个任务」，不再把「SSE 刷新抢走了面板」误判成 UI 没渲染 |

---

## 三、第二轮验证

| 项目 | 命令 | 结果 |
|---|---|---|
| 单元 / 回归测试 | `python -X utf8 -m unittest discover -s mail_workbench/tests -t .` | **241 passed, OK** |
| **新增：一致性审计** | `node verification/_audit_state_sync.cjs` | **52 通过 / 0 失败 / 0 跳过 / 零 console error** |
| 界面全量按钮审计 | `node verification/_audit_ui_buttons.cjs` | **104 通过 / 0 失败 / 0 跳过** |
| V2 界面自检 | `node uitest_v2_workbench.cjs` | **verdict: PASS**，`failures: []`，`console_errors: []` |
| V1 界面回归 | `node uitest2.cjs` | 遮挡/异常 0，`ERRORS = none` |
| 路由探活 | `python -X utf8 verification/_audit_endpoints.py` | 0 项异常 |
| 字段契约核对 | `python -X utf8 verification/_audit_contract.py` | 74 项字段全部存在 |

### 新增的一致性审计（`_audit_state_sync.cjs`）覆盖什么

它专门盯「一处操作牵动的其它状态是否跟着变」，全部用**可往返复原**的操作，
跑完净效果为零：

| 场景 | 断言 |
|---|---|
| A | 单封已读⇄未读（**方向自适应**）：服务端未读数、**侧栏显示数**、列表行的 `unread` 类、详情页按钮翻面 —— 四者同步；再切回来复原 |
| A2 | 「仅未读」下列表成员变化 → 列表少一封、**没有幽灵行**、选中集合同步收缩、勾选框数与状态数一致、无勾选时不显示批量条 |
| B | 星标 / 已读是**真开关**：按钮反映当前状态、能开能关、往返回到原状 |
| C | 草稿终态（`dismissed` / `expired` / `sent` / `failed`）面板只读、无编辑控件、有「重新起草」，且点它**真的会建新任务** |
| D | 切视图后选中集合清空（跨视图不串味） |
| E | 工作桶计数与列表条数一致 |
| F | 撤销条：动作后出现、文案对得上、点它能复原、不常驻 |

> 取材上踩过两个坑，都写进脚本注释了：
> ① `openDetail` 展示的是该邮件**最新的那个任务**（`jobs[0]`），
> 所以必须挑「确实会被显示出来」的那个终态任务，否则测的是另一个；
> ② 行内复选框是 `<div class="chk">` 不是 `<input>`，读 `.checked` 永远是 undefined，
> 会把「界面正常」误报成「幽灵选中」。

---

## 四、第二轮改动清单

```
mail_workbench/ui/index.html     afterAction 四路对齐（计数/列表行/选中/撤销条）
                                 + patchFolderItem + pruneSelection + renderEmptyDetail
                                 + 草稿终态冻结与「重新起草」
                                 + 撤销条 UNDO_LABEL / syncUndoBar / primeUndoBaseline
                                 + 星标与已读做成真开关（补上单封「标为已读」）
                                 + 失败不再假成功（dismiss/revoke/mode/ctrl）
                                 + SSE action/batch 只同步计数
                                 + 面板 data-job 标记
mail_workbench/server.py         伪文件夹 (Foxmail 历史) 真的按 source 过滤
mail_workbench/tests/test_bridge.py  新增伪文件夹过滤测试
verification/_audit_state_sync.cjs   新增：一致性审计（52 项）
verification/_audit_ui_buttons.cjs   ACTIONS 支持成对按钮（read/unread、star/unstar）
```

### 留给你的一件事

本轮**没有**改「打开邮件即自动标已读」这个行为 —— V2 刻意不做（防误标）。
如果你希望它像 Foxmail/Gmail 那样「点开即已读」，说一声，改动很小；
现在想标已读是显式点一下按钮。
