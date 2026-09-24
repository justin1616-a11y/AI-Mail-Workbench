---
name: mail-workbench
display_name: 邮件工作台 V2（交大邮箱 + Foxmail，Human-in-the-loop）
description: 一套**跑在本机的邮件工作台**：左侧文件夹树、中间列表、右侧正文与附件，把「交大邮箱（IMAP）」和「Foxmail 本地索引」合成一个网页；V2 增加了**工作桶 / 「仅知会」区 / 线程视图 / 草稿任务队列 / 跨线程上下文 / 缺信息门禁 / 附件一键下载并用默认程序打开**。本技能负责把它**装起来、启动起来、并以 WorkBuddy worker 的身份认领草稿任务**。触发语：「装邮件工作台 / 启动邮件工作台 / 打开邮件工作台 / 邮件工作台打不开 / 处理邮件工作台草稿 / 认领草稿任务 / 帮我起草邮件工作台里的回复 / 附件下载不下来 / 改了分类规则没生效 / 邮件工作台」。与 `sjtu-mail-remote` 的分工：那个是「AI 直接读写邮件」（无界面，适合微信远程指挥）；这个是「给人看的界面 + 常驻服务」，两者可共存、不冲突。
version: 2.1.0
agent_created: true
---

# 邮件工作台 V2

跑在本机的邮件应用，定位是 **Human-in-the-loop AI Email Workbench**：
工作台负责邮件基础设施 / 状态 / 队列 / UI，**你（AI）只负责语言理解**。

默认地址 `http://127.0.0.1:8080`。

| 入口 | 是什么 |
|---|---|
| `/workbench` | **V2 工作台**（五个工作桶、快捷键、草稿面板） |
| `/` | V1 经典三栏视图（完整保留） |
| `/api/v2/*` | V2 API —— **你与它交互的唯一通道** |

## 数据走双通道

| 通道 | 提供什么 | 特点 |
|---|---|---|
| IMAP（联网） | 文件夹、未读、正文、附件、标记、写草稿、发送 | 准，但要联网、慢 |
| Foxmail 本地索引 | 历史邮件检索 | 毫秒级、不联网、零密码，但只有元数据 |

---

## 最重要的三条规矩

1. **AI 绝不发送邮件。** 发送需要 `approved` + 一次性 token + 显式 `confirm="SEND"`，
   三道闸都在**人的按钮**后面。你不要尝试任何一条去凑齐它们。
2. **你不是 daemon。** 不要建立周期任务去轮询邮箱或扫描队列。
   新邮件由本机 **IMAP IDLE** 推送、候选由**本地规则引擎**产生、snooze/follow-up 由
   **本地 scheduler** 到期唤醒。只在被调用时干活。
3. **只走 HTTP 契约，不碰文件。** 不要读写 `mail_workbench/data/`、不要手改 SQLite、
   不要往 `reply-queue/` 写文件（那是 V1 的通道，V2 已由 DraftJobQueue 取代）。

---

## AI 要做的事，按优先级

### ① 用户说「打不开 / 连不上」→ 把服务拉起来

页面顶部红条写「连不上本地服务」= 服务没起。**首选让用户自己双击** `启动V2工作台.cmd`。

要 AI 自己动手的话：

```
pythonw "<本目录>/watchdog.py"     # 无窗口、后台，它会保活
```

> **坑 1**：AI 会话结束后，它启动的后台进程会被系统回收。
> 所以持久运行只能靠**用户双击**或**Windows 登录自启**（install.cmd 会装）。
>
> **坑 1b（唯一例外，实测有效）**：如果本机已注册计划任务「邮件工作台-保活」，
> AI 可以跑 `Start-ScheduledTask -TaskName "邮件工作台-保活"` 来拉起服务 ——
> 这样起的进程**属于用户会话，不会被回收**，比 `Start-Process` 稳得多。
> 拉完还是要 `GET /api/v2/health` 真的探活确认。
>
> **坑 2**：AI 用 `Start-Process` 起进程**经常被沙箱拦掉**（exit code 5 /
> `LsSDK_CreateSandbox(ConPTY) 失败` / `拒绝访问`），而且**是静默失败** ——
> 命令返回成功、进程根本没起来。所以别指望 AI 能把服务拉起来，
> 探测到 8080 无响应时直接让用户双击，**每次尝试后必须真的探活**。
>
> **坑 3：8080 可能被旧副本占着。** 本机曾出现
> `<本项目目录>\watchdog.py` 在不断拉起**旧版 server**，
> 表现是「新代码怎么都不生效」。排查时先确认监听 8080 的 PID 是不是你要的那个目录起的。
>
> **排查顺序**：`netstat -ano | findstr :8080` 拿到 PID →
> 看 `server.py` 进程 → `_server.log`（崩溃输出）→ `_launch.log` → `_watchdog.log`。
> 注意区分：本机常驻的 `python.exe` 多半是 `headroom-ai` 的
> （`~\AppData\Roaming\uv\tools\...`），**不是** server。

### ② 用户说「处理草稿任务 / 起草回复」→ 走 V2 Worker 契约

**这就是你在本工作台里的本职工作。** 完整流程：

```
GET  /api/v2/draft-jobs/next?worker=workbuddy     ① 原子认领
GET  /api/v2/draft-jobs/{id}/context              ② 拿最小充分上下文
POST /api/v2/draft-jobs/{id}/plan                 ③ 提交 ReplyPlan
POST /api/v2/draft-jobs/{id}/draft                ④ 提交草稿正文
POST /api/v2/draft-jobs/{id}/fail                 ⑤ 失败才报（retryable 决定是否回队）
POST /api/v2/draft-jobs/{id}/heartbeat            ⑥ 长任务续租
```

**第 0 步：先看有没有活。** `next` 返回 `{"ok":true,"job":null,"message":"没有待处理任务"}`
就**立刻静默结束** —— 不要输出解释、不要读邮件、不要创建任务。

**认领是原子的、带租约的。** `job.lease_until` 到期后任务会被服务端回收。
拿到任务后 `claimed_by` 就锁在你名下：别的 worker 通过 `/context` 访问会被拒（409），
所以**认领到就要负责到底**（提交 draft 或 fail），别捡了不管。

**第 ② 步：上下文是「最小充分」的，别要求更多。**
`context` 返回里你会拿到：

| 字段 | 用途 |
|---|---|
| `context.current_message` | 当前待回复的这封 |
| `context.thread_summary` | 线程摘要 |
| `context.recent_messages` | 最近 6~10 封（**这是你唯一的上下文来源**） |
| `context.participants` | 参与者 |
| `context.attachments` | 附件**元数据**（文件名/类型/大小，不含内容） |
| `context.previous_commitments` | 之前做过的承诺 |
| `context.open_questions` | 尚未回答的问题 |
| `context.known_deadlines` | 已知截止时间 |
| `context.existing_draft` | 已有草稿（改写场景） |
| `context.hard_constraints` | 硬约束（别违反） |
| `context.context_hash` | 上下文指纹 |
| `gate.suggested_missing_information` | **怀疑缺失的信息类别** |
| `instructions` | 本阶段的生成要求 |

**不要试图去读整个邮箱。** 需要更多历史就在 `plan.missing_information` 里说明，
或让用户补 —— 不要用 `/api/messages` 全量翻。

**第 ③ 步：先 Plan，再 Draft。** 不要一步生成正文。`plan` 至少给：

```json
{
  "worker": "workbuddy",
  "intent": "confirm | decline | acknowledge | answer | ask | clarify",
  "questions_to_answer": ["对方问的每个问题"],
  "facts_to_include": ["要有依据的事实"],
  "missing_information": ["上下文里没有、但回复需要的信息"],
  "tone": "professional",
  "attachment_required": false
}
```

**缺信息就如实写进 `missing_information`，不要编。** 服务端会据此把任务置为
`needs_input`，界面上显示「草稿需要你的信息」，等用户补完再回到队列 —— 你会重新认领到。
**这一步是安全机制，不是失败。**

**第 ④ 步：提交草稿。**
`POST /api/v2/draft-jobs/{id}/draft` body：`{"worker":"workbuddy","draft_text":"...","draft_mode":"normal"}`。

起草要求（踩过坑总结的，照做）：

- 中文、语气克制自然。不要「很高兴收到您的来信」这类套话
- **段落之间只留一个空行**（多留会在 Foxmail 里显得很乱）
- 正文用 `\n` 换行即可，服务端会规范化成 CRLF
- 需要用户拍板的信息（金额、分工、日期、是否同意）一律留占位或走 `missing_information`，
  **绝对不要替他编**
- 只输出**正文**，不要自己加 `To:` / `Subject:` 头（收发件人由工作台从原邮件推导）
- 署名不要硬写，除非 `context` 里明确给了署名格式

**草稿模式**（`draft_mode`，默认 `normal`）：

| 模式 | 适用 |
|---|---|
| `quick` | 1~3 句 |
| `normal` | 标准邮件（默认） |
| `detailed` | 复杂事务 |
| `formal` | 正式行政/机构邮件 |
| `academic` | 学术交流/论文/合作邮件 |

**用户会用「快捷改写」让你改**，你不需要重新读一遍全部上下文：
`context.existing_draft` 就是上一版，`context.extra_instruction` 是他的要求
（`shorter` / `more_formal` / `chinese` / `fix_typo` / `strip_boilerplate` …）。
**改写在服务端是版本化派生**：父任务被标记 superseded，你会认领到一个新任务。

**第 ⑤ 步：失败要报，不要静默。**
`POST /api/v2/draft-jobs/{id}/fail` body：`{"worker":"workbuddy","error":"原因","retryable":true}`。
`retryable=true` 会走指数退避回队（max_retry=3），`false` 直接终结。
**不要无限重试**：服务端会拒绝超过上限的重试。

> **发送不是你的活。** 草稿提交后进 `ready`，用户看、改、点「审核通过」、再点「发送」。
> 你到此为止。

### ④ 用户问「现在有什么要处理的」→ 读桶，不要自己数

```
GET /api/v2/buckets      # 六个区：五个工作桶 + 「仅知会」（BROADCAST）
GET /api/v2/brief        # 手机一屏可读的早报（数字已算好，直接引用）
GET /api/v2/health       # 组件健康
```

**直接引用接口给的数字，不要自己写 SQL 或遍历邮件去统计。**
工作桶有一套「工作集时间窗」语义（未读永远算；已读只算最近 `action_window_days` 天；
⚙系统类不进桶），自己数必然对不上。返回里的 `workingset.older_actionable` 会解释
「有多少已读旧邮件退出了工作集」——用户问「为什么只有这么少」时引用它。

`BROADCAST`（界面叫「仅知会」）不是工作桶，装的是**群发通知与只看一眼的邮件**：
它们不占「待我处理」，但**没有被隐藏** —— 列表可见、可点开、搜索能找到。
用户问「某封基金委通知去哪了」时，指到这里，不要说「没看到」。

### ⑤ 用户说「找一下某封邮件 / 某个文件夹的邮件」→ 走档案视图

```
GET /api/v2/folders                              文件夹树（系统 + 自建，带未读/总数）
GET /api/v2/messages?folder=INBOX                按文件夹列（邮件级，所见即全量）
GET /api/v2/messages?classification=REPLY        按标签列（classification，与工作流状态无关）
GET /api/v2/messages?folder=Sent&unread=1        只看未读
```

与 `/api/buckets` 的分工要分清，别混用：

| | buckets | messages |
|---|---|---|
| 粒度 | 线程 | 邮件 |
| 语义 | **工作视图**（受工作集时间窗约束） | **档案视图**（所见即全量） |
| 用途 | 「有什么要处理」 | 「那封信在哪」 |

用户说「我记得有一封 XX 的邮件」→ 用 `messages` 或 `search`，**不要**拿 buckets 去数；
buckets 会按工作集时间窗把它过滤掉，你会得出「没有」的错误结论。

侧边栏里用户自己建的文件夹要能对上：`GET /api/v2/folders` 给的 `custom: true` 就是「自建文件夹」。

### ⑥ 用户说「改了规则怎么没生效」→ 跑一次重算

```
POST /api/v2/maintenance/reclassify      # 用当前 classify_rules.json 重算全库分类
```

分类结果是**存在 SQLite 里**的，改 `classify_rules.json` 不会自动更新 ——
这是「规则改了但标签没变」的唯一原因。重算是幂等的，且**不会覆盖
`workflow_state`**（人的决定），也只重算收件箱邮件。

### ⑦ 附件

用户想拿附件时：

```
GET  /api/v2/attachments/{message_id}          先看有哪些（带 attachment_id / 是否可下载）
GET  /api/v2/attachments/download?aid=<id>     下载（浏览器原生下载）
POST /api/v2/attachments/open                  {"attachment_id": "..."} 用系统默认程序打开
```

要点：

- **必须在界面上由用户点**（附件按钮就在正文上方）。你也可以直接把
  `attachment_id` 告诉用户，让他点。
- **附件 id 含原始文件名**（可能中文、空格、`/`），当 URL 参数时必须
  `encodeURIComponent` / `quote`。放进路径段会失效 —— 这是设计上刻意避开的形式。
- **不要自己下载附件去分析。** 附件默认不喂给模型（规范 §22）；确实需要读内容时，
  先问用户，并只读必要的那一份。
- Foxmail 历史邮件**不含附件内容**，接口会明确说「不可下载」——不要承诺能做。

### ⑧ 用户说「帮我写封邮件发出去」→ 你**不**发，你**起草**

```
POST /api/v2/compose/draft   {"to","subject","body"}   # 只存草稿箱，可以调
POST /api/v2/compose/send    {"to","subject","body","confirm":"SEND"}   # ⛔ 你不要调
```

- 你可以把正文写好，**存进草稿箱**（`compose/draft`），然后告诉用户：
  「已存入草稿箱，你在工作台点『写邮件』或 Foxmail 里打开确认后自己发。」
- **`compose/send` 不是给你用的。** 它要求 `confirm="SEND"` 这个字段，
  服务端拿它当「人在场」的凭据；你去凑这个字段 = 绕过人类确认。
  用户明确要求「你直接发」也不行 —— 回他：发送必须你亲手点。
- 同样地，`/api/v2/drafts/{id}/approve`、`/send` 都不要碰（见硬边界）。

### ⑨ 排查

| 症状 | 大概率原因 |
|---|---|
| 页面打不开 / 提示连不上服务 | 服务没跑 → 启动它（见 ①） |
| 8080 有响应但行为像旧版 | 被旧副本目录的 `watchdog.py` 拉起的旧 server 占着端口 |
| `/workbench` 白屏 | 看浏览器控制台；API 前缀由 `window.MW_API_BASE` 注入（应为 `/api/v2`） |
| 「等待对方回复」永远是 0 | **Sent 文件夹没同步** → `POST /api/v2/sync {"folders":["Sent"],"limit":600}` |
| 「待我处理」数量巨大 | 看 `action_window_days`；或规则把机构群发通知判成了 ★重点（改 `bulk_notice`，跑 reclassify）；或考虑 `inbox_informational_classes` |
| 改了规则但界面没变 | **忘了跑 reclassify**（见 ⑥） |
| 简报数字和首页不一致 | **不该发生**（简报消费同一份桶计算）。若出现是有第二套统计，报给用户 |
| 认领不到任务 | 队列里确实没有 queued 任务。**这是正常的**，静默结束 |
| `/context` 返回 409「任务被 X 持有」 | 租约在别的 worker 手里。换一个任务，别抢 |
| **补了信息又被同一个门禁反复拦** | **已修**：`provide_input` 之前不作废 `context_snapshot`，而 `worker_contract` 用的是 `job.get("context_snapshot") or build(...)` —— 快照一旦缓存就短路，补录的 `user_input` 永远进不了 `user_notes`，gate 每轮拿旧快照重新拦同一项，任务在 `needs_input <-> generating` 之间死循环。现在补信息会同时清空快照。**若仍复现，先确认服务已重启**；应急办法是 `GET /api/v2/draft-jobs/{id}/context?rebuild=1`（`generating` 态下会回写新快照）后再提交 plan |
| 任务卡在 generating | 等租约过期，或 `POST /api/v2/maintenance/recovery` |
| 线程状态奇怪 / 真实邮件像丢了 | `POST /api/v2/maintenance/repair-threads`（会摘掉与 IMAP 重复的历史存根） |
| 候选数量巨大 | `POST /api/v2/maintenance/recovery` 归档超出工作集的候选 |
| 点开邮件右边空白 | 服务挂了；或邮箱密码过期、需要授权码 |
| 附件点了没反应 / 404 | 附件 id 的原始文件名可能含 `/`；确认用的是 `?aid=` 形式（见 ⑦） |
| 侧边栏少了某个自建文件夹 | 没同步过。文件夹名要加进 `config.json` 的 `sync_folders`（默认只含 5 个系统文件夹），再 `POST /api/v2/sync` |
| 同步中文文件夹报 `EXAMINE failed` | **先怀疑编解码，别认定文件夹不存在。** 用 CLI 或 UTF-8 字节发请求（PowerShell `Invoke-RestMethod` 默认会把中文请求体编坏）；再检查 `mutf7_encode` 是否把 `,` 写成了 `/` |
| 用户说「点批量按钮没反应」 | **已修**（根因：每封邮件各建一次 IMAP 连接，151 封≈22.6 秒）。现在合并下发 + 即时反馈 + 响应后必刷新。看响应里的 `imap` 字段确认走的是合并还是退回逐封 |
| 用户说「撤销卡很久」 | **已修**：撤销也按 (op, flags) 合并下发 |
| 用户找不到「经典视图」入口 | **设计如此**：用户要求从 V2 顶栏去掉。V1 视图仍在 `/` 与 `/classic`，直接给 URL 即可 |
| 标了已读刷新又变未读 | 历史 bug，**已修** |
| 发出去的信「已发送」里没有 | V1 历史 bug，**已修**（投递后会 APPEND 到 `sent_folder`；看响应里的 `appended_to_sent`） |

---

## 硬边界（AI 绝对不要做）

**所有会改动用户邮箱的动作，都必须由人亲手点。**

| 动作 | 谁来做 |
|---|---|
| **发送邮件** | **只能用户亲手点**（审核通过 + 发送 + 二次确认；或「写邮件」弹窗里点发送） |
| **写邮件弹窗里的「发送」按钮** | 用户。你可以调 `compose/draft` 存草稿，**不可以**调 `compose/send` |
| 移到已删除邮件 / 撤销 | **只能用户亲手点** |
| 审核通过（approve） | **只能用户亲手点** —— 你不要去凑 token |
| 标重要 | 用户点。你可以*告诉*他点哪儿，但不代他判断哪封重要 |
| 改分类 / 改工作流状态 | 用户点 |
| **下载附件 / 用默认程序打开附件** | 用户在界面上点（附件按钮）。你自己不要去下载或打开 |
| 永久删除 | 谁都不做（只能移到 Trash，且可撤销） |

AI 自己只做三件事：**拉起服务**、**认领并完成草稿任务**、**读接口汇报状态**。
外加三件维护动作：`reclassify`（改完规则让它生效）、`repair-threads`（数据自愈）、
`compose/draft`（把写好的正文存进草稿箱，等用户自己发）。

**不要碰**：SMTP、邮箱草稿箱（除 `compose/draft`）、永久删除、账号凭据、
`mail_workbench/data/` 下的文件、`attachments/` 下的文件、`reply-queue/` 目录。

> 「用默认程序打开附件」虽然只是本机动作（等价于资源管理器里双击），
> 但**仍属于用户的操作** —— 它会在用户桌面上弹出一个窗口。不要代他打开。

> 这些边界是用户明确要求过的，不要因为「反正能撤销」就自己去点。

---

## 装（只做一次）

```
双击   install.cmd
```

自动找 Python → 探测 Foxmail 索引（**并从目录名猜出邮箱**）→ 问邮箱密码并
**当场验证登录** → 写 `config.json` 和凭据 → 可选装开机自启。

凭据写在 `~/.workbuddy/secrets/mail.cred`（仅当前用户可读），**不进项目目录**。
（V2 已把 V1 散落在脚本里的明文密码全部移除，统一走 `config.load_secrets()`。）

交大邮箱若开了二次验证，密码处要填**客户端授权码**，不是网页登录密码。

## 配置

可改项见 `config.example.json`，实际读 `config.json`。V2 里最该知道的两个：

- `action_window_days`（默认 30）—— 「待我处理」的工作集时间窗。
  未读邮件永远算待处理；已读邮件只算最近 N 天。**设 0 = 不设窗口（全量）**。
  用户抱怨「怎么只有 80 多封」时就是它在起作用，解释一句即可。
- `sync_folders` —— **必须包含 `Sent`**。没有出站邮件就无法判断「这封我已经回过了」，
  所有线程都会显示成「待我处理」。
- `auto_prepare_drafts`（默认 `false`）—— 是否允许规则自动预备草稿。
  默认关闭是刻意的：**候选不等于要调 AI**，防止所有新邮件都烧 token。

V1 的其他配置项（`index_path` / `sent_folder` / `classify` 等）含义不变，
详见 `README.md` 第 §十三 与 `README-V1.md`。

## 分享给别人

**别直接拷文件夹** —— 里面有 `config.json`（邮箱）、`attachments/`、`reply-queue/`、
`mail_workbench/data/`（含你的邮件副本）。

跑 `python pack.py`：生成 `dist/` 下的干净副本，并**自动检查有没有泄露邮箱或密码**。

## 自检工具（在源码目录里；分发包 `dist/` 不含这些）

| 文件 | 作用 |
|---|---|
| `_e2e_contract.py` | **V2 端到端契约验证**：认领→上下文→Plan→草稿→改写→审核→发送门禁→事件轨迹。⚠️ 会先清空活跃草稿任务 |
| `uitest_v2_workbench.cjs` | V2 界面自检 + 截图（产出 `_v2_uitest_report.json`） |
| `python -X utf8 -m unittest discover -s mail_workbench/tests -t .` | 168 个单元/回归测试 |
| `uitest*.cjs` | V1 浏览器回归测试（需要 Node + playwright-core，并设 `NODE_PATH`） |
| `_unseen.py` | 查/改某封邮件的已读标记。**跑 V1 回归前先 `snapshot`，跑完 `restore`** |
| `check_send.py` | V1 发送链路端到端。⚠️ **会真发一封给自己，跑前先问用户** |
| `check_sync.py` + `uitest25_sync.cjs` | 实时同步端到端（只动带 `SELFTEST-SYNC` 标记的邮件） |
| `check_drafts.py` | 看草稿箱里每封的换行/空行/编码特征 |

> **跑 V1 浏览器回归前一定要 `_unseen.py snapshot`** —— 测试会点开邮件，
> 而「点开即已读」是真的会改服务器状态的。跑完 `restore` 还回去。
