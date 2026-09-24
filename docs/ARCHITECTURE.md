# Mail Workbench

**Human-in-the-loop AI Email Workbench** —— 跑在本机（127.0.0.1）的邮件工作台。

V2 的定位不是「更好的邮件客户端」，也不是「自动回信机器人」。它是你邮箱前的一道**工作流层**：
邮件基础设施、状态管理、队列、UI 由它负责；语言理解交给 WorkBuddy；
Foxmail 继续当成熟桌面客户端与历史邮件来源；**最终判断与发送永远由你亲手完成**。

> **AI NEVER SENDS EMAIL WITHOUT EXPLICIT HUMAN CONFIRMATION.**
> 这不是一句口号，而是状态机里的一条硬约束：只有 `approved` 能发送，
> 而 `approved` 只能由人点「审核通过」产生，并配一次性 token。

> 本文档面向**想改代码的人**：架构、模块地图、状态机、API 契约、安全边界、踩过的坑。
> 只想装来用 → 看 **[INSTALL.md](INSTALL.md)**。
> 让 AI 通过本工作台帮你操作邮件 → 看 **[SKILL.md](SKILL.md)**。
> **V1 → V2 做了什么、为什么这么做、验证结果** → 看 **[V2-MIGRATION.md](V2-MIGRATION.md)**。
> **V1 文档已归档**为 [README-V1.md](README-V1.md) / [SKILL-V1.md](SKILL-V1.md)。

---

## 快速开始

装（只做一次）：

```
双击  install.cmd
```

跑：

```
双击  启动V2工作台.cmd
```

浏览器打开 <http://127.0.0.1:8080/workbench>。

| 入口 | 是什么 |
|---|---|
| `/workbench` | **V2 工作台**：侧边栏（工作桶 + 文件夹树 + 标签树）、快捷键、批处理、草稿面板 |
| `/` · `/classic` | V1 经典三栏视图（**完整保留**，直接敲 URL 就能到） |
| `/api/v2/*` | V2 API（对 WorkBuddy worker 的稳定契约） |

> 「经典视图」在 V2 顶栏里的**入口已去掉**（用户要求，顶栏只留工作台自己的操作）。
> 页面本身没删 —— 需要时直接访问 `/` 或 `/classic`，V1 的零回归承诺不变。

服务启动后自动做三件事：挂上 IMAP IDLE 监听新邮件、启动本地 scheduler
（snooze / follow-up / recovery / 简报快照）、开始增量同步。

只用 Python 标准库 + 原生 ES Modules，无需 npm install、无需构建。

---

## 一、职责划分

| 角色 | 负责 |
|---|---|
| **Mail Workbench** | 邮件基础设施、状态管理、工作流、队列、UI |
| **WorkBuddy** | 邮件理解、推理、摘要、信息提取、回复规划、草稿生成 |
| **Foxmail** | 成熟桌面客户端、历史邮件来源、备用完整客户端 |
| **你** | 最终决策、草稿审核、修改、发送确认 |

---

## 二、架构与数据流

```
                    Mail Server (mail.sjtu.edu.cn)
                       IMAP IDLE / SMTP
                              │
                  ┌───────────▼───────────┐
                  │   Mail Sync Engine    │  事件驱动增量同步
                  │   imap_client         │  IDLE 不可用才降级 NOOP 轮询
                  └───────────┬───────────┘
                              │
                  ┌───────────▼───────────┐
                  │  Unified Mail Store   │  SQLite (WAL)
                  │  IMAP Mail            │  messages / attachments
                  │  Foxmail History      │  source='foxmail' 只读索引
                  │  Thread Index         │  threads
                  └───────────┬───────────┘
                              │
                  ┌───────────▼───────────┐
                  │  Mail Intelligence    │  纯本地、确定性、不调模型
                  │  rule_engine          │  五级分类
                  │  candidate_detector   │  候选（≠ 调 AI）
                  │  thread/aggregator    │  线程聚合
                  │  fact_extractor       │  日期/金额/敏感信息
                  └───────────┬───────────┘
                              │
                  ┌───────────▼───────────┐
                  │   Workflow Engine     │
                  │   state_machine       │  合法跳转校验
                  │   DraftJobQueue       │  草稿任务状态机
                  │   snooze / followup   │  本地调度器统一管理
                  │   recovery            │  队列自愈
                  └───────────┬───────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
        Web Workbench                  WorkBuddy Worker
        /workbench                     claim → context → plan → draft
                                               │
                                        Human Review（你）
                                               │
                                     approve + token + confirm
                                               │
                                        SMTP 发送 + Sent 副本
```

### 四条最高优先级原则

| 原则 | 落地 |
|---|---|
| **P0-1 EVENT DRIVEN** | IMAP IDLE 推事件 → 本地处理 → 生成候选。**不用 WorkBuddy automation 轮询邮件**。 |
| **P0-2 LOCAL FIRST** | 同步、状态、队列、搜索、线程、去重、retry、timeout、cleanup、snooze、follow-up、候选检测、规则分类全部本地完成。确定性任务不烧 token。 |
| **P0-3 AI ON DEMAND** | 只在需要语言理解时才找 WorkBuddy：摘要、歧义分类、动作抽取、线程理解、起草、改写、翻译、附件分析。 |
| **P0-4 HUMAN IN THE LOOP** | Draft → Review → Explicit Send。禁止 AI → SMTP。 |

---

## 三、模块地图

```
mail_workbench/
├── server.py            HTTP 路由 + SSE + App 装配（58 个端点）
├── cli.py               命令行入口（sync / serve / recovery / ...）
├── config.py            配置与凭据加载（凭据外置，绝不入库）
├── constants.py         枚举与中文标签的唯一来源
├── util.py              时间/文本/哈希/脱敏工具
├── metrics.py           本地指标（Time to Inbox Zero / 草稿接受率）
├── v1_bridge.py         把 V2 路由挂进 V1 server.py 的粘合层
├── mail/
│   ├── parser.py            MIME 解析、正文提取、附件元数据、线程键
│   ├── imap_client.py       IMAP 封装 + **裸 socket 的 IDLE 监听器**
│   ├── smtp_client.py       SMTP 发送（发送门禁之外的最后一道）
│   ├── sync_engine.py       事件驱动增量同步 + 分类 + 候选
│   └── foxmail_index.py     Foxmail 二进制索引解析（只读，不 GUI 自动化）
├── thread/
│   ├── aggregator.py        线程聚合 + 历史回填 + 历史线程回收
│   └── context_builder.py   MailContextPackage（最小充分上下文）
├── intelligence/
│   ├── rule_engine.py       五级分类（本地规则）
│   ├── fact_extractor.py    日期/金额/敏感信息
│   └── reply_planner.py     Plan 阶段 + Missing Information Gate
├── workflow/
│   ├── state_machine.py     状态机与合法跳转（唯一发送门禁定义点）
│   ├── candidate_detector.py 候选检测（Candidate ≠ DraftJob）
│   ├── buckets.py           五个工作桶（首页与简报的唯一真相来源）
│   ├── actions.py           动作层（归档/延后/完成/忽略/等待/删除/撤销）
│   ├── snooze_manager.py    延后（本地，不用 automation）
│   ├── followup_manager.py  跟进（本地，一个调度器管全部）
│   ├── scheduler.py         单一线程的本地调度器 + Daily Brief
│   └── brief.py             简报的读写封装
├── draft/
│   ├── queue.py             DraftJobQueue（状态机 + 幂等 + 租约 + 退避）
│   ├── worker_contract.py   WorkBuddy worker 契约实现
│   └── recovery.py          队列自愈（超时/孤儿/过期/回滚）
├── storage/
│   ├── database.py          SQLite（WAL + 版本化迁移）
│   ├── repo.py              数据访问层
│   └── search.py            搜索查询解析（sender:/subject:/after:/...）
├── ui/index.html            V2 工作台单页（原生 JS，无框架）
└── tests/                   168 个单元/回归测试
```

其他关键文件（项目根）：

| 文件 | 作用 |
|---|---|
| `server.py` | **V1 服务入口**，同时在 `/workbench` 挂载 V2 UI、在 `/api/v2/*` 转发 V2 API |
| `reply_queue.py` | V1 的回复队列（保留可用；V2 用 DraftJobQueue 取代，见 §八） |
| `watchdog.py` | 保活：每 15 秒探 `/api/health`，挂了拉起 |
| `uitest_v2_workbench.cjs` | V2 界面自检（Playwright），产出截图与 JSON 报告 |
| `_e2e_contract.py` | **端到端契约验证**：走完 claim→context→plan→draft→review→send gate |
| `README-V1.md` / `SKILL-V1.md` | V1 文档归档 |

---

## 四、五个工作桶

首页**不展示邮件总数**，展示你真正要推进的事：

| 桶 | 含义 |
|---|---|
| **待我处理** | 球在我这边 |
| **等待对方回复** | 我发出后还没回音的（近期） |
| **AI 草稿就绪** | 草稿已生成（含「缺你的信息」待补） |
| **已延后** | snooze 未到期 |
| **今天已完成** | 今日处理完毕 |

> 「等待对方回复」下面还会挂一行灰色小字「超过 N 天没回音 137」——
> 那是**沉底区**：不占工作桶计数，但点开仍可查。见下节。

### 工作集时间窗（重要）

邮箱里有 **6696 封 Foxmail 历史 + 约 4000 封当前邮件**。如果「待我处理」等于
「所有最新一封是来信的线程」，这个桶会有 **2883 条**，永远收敛不了，
「Time to Inbox Zero」也就失去意义。

因此规则是：

- **未读**的邮件永远是待处理（未读 = 还没看过）
- **已读**的邮件只在最近 `action_window_days`（默认 30 天）内仍算待处理，更早的退出工作集
- ⚙**系统类**（`SYSTEM`）不进任何工作桶 —— 它们要去对应的业务系统办理，不是回邮件
- ×**可忽略**（`DISMISS`）默认不进桶

退出工作集**不等于删除**：分类、线程、正文全部保留，搜索随时能找回。
`GET /api/buckets` 的 `workingset.older_actionable` 会如实报出被移出工作集的数量，
不静默丢弃。想要全量就把 `action_window_days` 设成 `0`。

#### 「等待对方回复」为什么**也**要时间窗

这条是用户实测反馈后补的，道理和「待我处理」正好相反，值得单独说：

- **待我处理** = 球在我这边。数字涨了，我可以做点什么把它压下去 —— 压力是建设性的
- **等待对方回复** = 球在对方那边。我**没有任何动作**能推进它，数字涨了我只能干看着

一个只增不减、又不由我控制的计数器，不是工作视图，是纯粹的焦虑源。

实测（2026-09，真实邮箱）：该桶 145 条里 **90 天以上 128 条、一年以上 102 条**，
最早的是 2023 年的「打印机权限设置」。它们不是「在等」，
而是「我最后说了一句话」的历史沉降 ——
一封 2023 年的通知我回了句「收到」，凭什么在 2026 年还挂在我的待办上？

规则改成：

- 只有我方最后一封（`last_outbound_ts`）在 `waiting_window_days`（默认 **14** 天）内
  发出的，才算「活跃等待」，进桶计数
- 更早的进**沉底区**（`STALE_WAIT`）：不占工作桶计数、不删除、不隐藏、可搜索、可点开
- **有 followup 的线程不受窗口限制** —— 那是用户主动说「这件事我要盯」，
  系统不该替他放弃。「等不等」的控制权回到用户手里，而不是由时间替他决定
- 对方回信后照常自动回到「待我处理」，这条出口不受影响

改完实测：**「等待对方回复」145 → 8，沉底 137**，留下的 8 条是真在等回音的事
（产学研申报、导师双选、读博名额）。`workingset.stale_waiting` 如实报出沉底数。
想要旧行为就把 `waiting_window_days` 设成 `0`。

实测（真实邮箱）：**0 待我处理 / 8 等对方 / 137 沉底 / 2277 已退出工作集 / 46 仅知会**，
首页计算 **< 150ms**。

> **计数与列表必须分开。** `counts` 永远是全量，`limit` 只截断返回的列表。
> 曾经用带 `LIMIT 600` 的查询结果去 `len()` 统计，把 2883 报成了 544。

### 分类 ≠ 工作流状态

`classification`（★重点 ✉要回 ○看一眼 ⚙系统 ×可忽略）只描述「这是什么邮件」；
`workflow_state`（待我处理 / 等对方 / 已延后 / 已完成 …）描述「这事走到哪了」。
两者自由组合，工作流动作**绝不改写分类**。

### 「仅知会」区：群发通知不占「待我处理」

科研办 / 基金委每天会群发若干封《…项目申报指南征求意见的通知》，机构群发是常态。
它们不是垃圾（要留档、日后会检索），但也不是要我动手的事 —— 让它们占着
「待我处理」会把真正该看的邮件淹掉。

判定在 `classify_rules.json` 的 **`bulk_notice`** 段，**白名单式生效**：

```json
"bulk_notice": {
  "senders": ["gift-research@sjtu.edu.cn"],
  "domains": ["nsfc.gov.cn", "pro.nsfc.gov.cn"],
  "subject_keywords": ["申报指南", "征求意见", "发布征集", "征集", "通告",
                       "立项建议", "转发", "Fwd:", "申报通知"],
  "except_keywords": ["人员信息核查", "限项", "答辩", "结题", "验收",
                      "中期检查", "邀您", "请您", "请于", "合作意向"]
}
```

两个开关**都要命中**才降级：发件人在 `senders`/`domains` 里 **且** 主题命中
`subject_keywords`。`except_keywords` 是「保命词」，命中一律不降级 ——
同样来自科研办的「人员信息核查」「结题验收」「答辩」「合作意向」那是真要动手的。

**CATL、专利代理等真人对人的往来完全不在作用域内**，一个字节都不会被误伤。

命中后：分类降为 `○ 看一眼`，并落到「仅知会」区（**不是删除**：列表可见、可点开、
搜索照常搜得到）。实测本机：★重点 从 15 降到 9，38 条群发通知从「待我处理」移出。

改完 `bulk_notice` 必须跑一次重算，否则库里存的还是旧判定：

```
POST /api/v2/maintenance/reclassify
```

> **这是「规则改了但界面没变」的唯一原因** —— 分类结果是**存在库里**的，
> 不会因为你改了 json 就自动更新。`reclassify` 只重算收件箱邮件（`source='imap'`），
> 我方发出的与 Foxmail 历史都不动，且**不改 `workflow_state`**（那是人的决定，幂等）。

### 再看一眼「○ 看一眼」该怎么算

`○ 看一眼` 里混着两类完全不同的东西，而且**没有结构性规则能干净分开**：

| 类别 | 例子 |
|---|---|
| 真要我过目的事务 | 师生互选通知、职称聘任预通知、面试报名、实验室安全检查、体检通知 |
| 期刊征稿 / 会议邀请 / 软件营销 | `Call for Papers`、SAE / MDPI / MathWorks / Zoom 营销 |

实测在本机 99 条桶内 `○ 看一眼` 里，这两类各占约一半，且成因交叉：
命中 `read` 名单的既可能是实验室安全通知（`giftlab@sjtu.edu.cn` 在 `read.senders` 里），
也可能是 SAE 营销；而「其它」类里既有职称聘任预通知，也有期刊推销。

所以**不替用户做这个政策判断**，而是给一个明确的开关：

```json
"inbox_informational_classes": []
```

- 默认 `[]` → 重要/要回/看一眼 都算「我的事」（保持原行为）
- 设成 `["READ"]` → 只看真正要办要回的；`○ 看一眼` 全部移到「仅知会」区，
  **仍然可见、可搜索、可点开**，不会消失

实测把 `action_window_days` 与这个开关一起用，可以把「待我处理」压到
**9 重点 + 46 要回 = 55 条**的量级。

---

## 五、DraftJobQueue 状态机

```
candidate ──→ queued ──→ generating ──→ ready ──→ reviewing ──→ approved ──→ sent
    │            │            │                                    │
    │            │            └──→ failed ──→ queued               └──→ failed
    │            │                 (指数退避，max_retry=3)
    │            ├──→ failed / expired / dismissed
    │            └──→ needs_input ──→ queued（补齐信息后继续）
    ├──→ dismissed / expired
    └──────────────── ready ──→ expired / dismissed
```

非法跳转会被状态机直接拒绝（`IllegalTransition`），每个跳转都写 `job_events` 审计。
`GET /api/state-machine` 可以拿到机器可读的完整跳转表。

**关键设计点：**

| 机制 | 说明 |
|---|---|
| **Candidate ≠ DraftJob** | 收到邮件只产生「候选」（这封可能要回），**不代表要调 AI**。只有你点「AI 起草」或规则明确允许才升级成 DraftJob。防止所有新邮件都烧 token。 |
| **幂等** | 同一 `message_id` 只有一个活跃任务（`idempotency_key = draft:{account}:{message_id}`，唯一索引保证）。重复点「AI 起草」返回同一个任务。 |
| **Lease / Claim** | `claimed_by` + `claim_time` + `lease_until`，claim 原子。worker 崩溃后租约过期 → Recovery 回滚成 queued。 |
| **超时与退避** | queued 超时 2h、generating 超时 15min、max_retry=3、指数退避（60s 起，上限 30min）。**禁止无限重试**。 |
| **改写是版本化派生** | 点「更短」不会原地覆盖草稿 —— 父任务标记 `expired`（superseded），派生新任务并记录 `revision_of`。旧草稿留作历史，同时保证「同一封邮件只有一个活跃任务」。 |
| **approved 不能直接 dismiss** | 已批准待发的草稿必须先 `revoke` 撤回到 `reviewing` 才能放弃 —— 避免误删已经过人工确认的内容。 |
| **NEEDS_INPUT** | 见 §七。 |

---

## 六、WorkBuddy Worker 契约

WorkBuddy **只通过 HTTP 契约**与工作台交互，**不碰内部文件结构**。

```
GET  /api/v2/draft-jobs/next?worker=<name>     # 原子认领（返回 job + lease_until）
GET  /api/v2/draft-jobs/{id}/context           # 拿最小充分上下文 + plan 阶段指令 + gate
POST /api/v2/draft-jobs/{id}/plan              # 提交 ReplyPlan
POST /api/v2/draft-jobs/{id}/draft             # 提交草稿正文
POST /api/v2/draft-jobs/{id}/fail              # 报告失败（retryable 决定是否回队）
POST /api/v2/draft-jobs/{id}/heartbeat         # 续租（长任务）
```

典型流程：`claim → context → plan → draft → complete`，状态由工作台负责。
`GET /api/contract` 会返回契约的自描述。

> `/api/draft-jobs/next` 按契约是**扁平响应**（`{ok, job, lease_until}`），
> 其余端点统一是 `{ok, data}`。写 worker 时用 `ok` 判断成败，别假设统一的 `data` 包装。

### 两步生成：Plan → Draft（不要一步出正文）

直接「邮件 → 生成回复」不可靠。先让模型理解并产出结构化的 `ReplyPlan`：

```json
{
  "intent": "confirm",
  "questions_to_answer": ["是否参加", "截止时间"],
  "facts_to_include": ["已收到通知"],
  "missing_information": ["可参加的日期"],
  "tone": "professional",
  "attachment_required": false
}
```

再 `ReplyPlan → Draft`。少了哪一环，回复就容易答非所问或漏答。

### 上下文是最小充分的，不是整个邮箱

`MailContextBuilder` 产出 `MailContextPackage`：

```
current_message / thread_summary / recent_messages(默认 6，上限 10) / participants
attachments / previous_commitments / open_questions / known_deadlines
user_notes / existing_draft / hard_constraints / memory_hints / context_hash
```

既省 token，又降低模型混淆。**禁止把整个邮箱交给 LLM。**

---

## 七、Missing Information Gate

如果邮件要求**具体日期、金额、承诺、人员名单、附件、实验数据、会议时间、项目决定、
正式意见**，而上下文里没有可靠答案 —— **禁止 AI 编造**。

此时任务进 `NEEDS_INPUT`，UI 显示「草稿需要你的信息」，列出待补项；
你填完 `POST /api/v2/drafts/{id}/input` → 任务回 `queued` 继续生成。

WorkBuddy 在 `context` 响应里会拿到 `gate.suggested_missing_information`，
必须把这些列入 `plan.missing_information` 或由你补全。

---

## 八、V1 → V2 增量升级记录

V1 **没有** `mail_workbench/` 包。V1 的真实形态是：`server.py`（88KB 单文件）
+ `reply_queue.py`（JSON 文件队列）+ `web/`（模块化前端）+ 17 个 Playwright 测试。
V2 是在它之上**增量长出**的，V1 一行功能都没丢。

| V1 | V2 | 迁移方式 |
|---|---|---|
| `server.py` 单文件路由 | `mail_workbench/` 分层包 | 保留 V1 `server.py` 作为入口，用 `v1_bridge.py` 把 V2 路由挂到 `/api/v2/*`、UI 挂到 `/workbench` |
| `reply_queue/` JSON 文件 | `DraftJobQueue`（SQLite 状态机） | **双轨并存**：`reply_queue.py` 仍可用，V2 不删旧数据、不改旧接口 |
| 4 个 WorkBuddy automation 轮询 | IMAP IDLE + 本地 scheduler | 见 §九 |
| 明文密码硬编码在 4 个脚本 | `~/.workbuddy/secrets/mail.cred` | 统一从 `config.load_secrets()` 读；明文已移除 |
| 零散 JSON 状态 | SQLite（WAL + 版本化迁移 v1→v3） | **不删旧 JSON**，先并存 |
| 按邮件处理 | 按**线程**处理 | `thread/aggregator.py`，历史邮件在**新邮件到达时**按主题回填进线程 |
| 分类标签当状态用 | `classification` ⊥ `workflow_state` | 严格分离，工作流动作不改分类 |

### 回归保障

| 验证 | 结果 |
|---|---|
| V2 单元/回归测试 | **168 个全部通过**（含状态机合法性、幂等、租约原子性、退避、gate、上下文最小性、线程聚合、规则分类、搜索操作符、Foxmail 索引解析、发送门禁、snooze/followup、工作桶、迁移、V1 功能回归、性能） |
| V1 Playwright UI 测试 | **全部通过、零控制台错误**（证明 V1 零回归） |
| V2 界面自检 | **verdict: PASS**（零 console error） |
| 端到端契约验证 | **全部通过**（见下） |
| 性能 | 工作桶 27ms / 列表 0.9ms / 搜索 4ms / 简报 62ms（目标 < 200ms） |

端到端契约验证覆盖：幂等（§25）、原子认领与防双跑（§27）、租约（§27）、
最小充分上下文（§7/§8）、Plan 两阶段（§9）、草稿进桶（§13）、
快捷改写与版本化派生（§12）、撤回审批（§5）、发送门禁（§33）、事件轨迹（§29）。
事件轨迹实测：

```
queued → generating → ready → reviewing → approved → reviewing → dismissed
```

---

## 九、自动化：从 4 个轮询任务到 1 个自愈任务

V1 有 4 个 ACTIVE automation，靠定时唤起 WorkBuddy 会话扫描邮件：

| V1 automation | 处置 |
|---|---|
| 邮件回复队列·加急通道（整点） | **停用** —— 改由 IMAP IDLE 事件驱动 |
| 邮件回复队列·加急通道（半点） | **停用** —— 同上 |
| 邮件回复队列·自动起草（每 6h） | **停用** —— 改由本地 candidate detector + 你点击 |
| 每日邮件早报（8:00） | 保留，但**只做投递**：数字由本地 `/api/brief` 算好，任务只取回并转发 |

新增的唯一周期任务：

| V2 automation | 作用 |
|---|---|
| **Mail Draft Recovery**（每 3 小时） | 只做一件事：`POST /api/maintenance/recovery`。发现并修复 queued 过久、generating 超时（worker 崩溃留下的过期租约）、孤儿认领、stale lock、可重试的 failed、ready 太久无人审核、candidate 过期。**无异常时完全静默退出**，不打扰你。 |

**正常邮件处理不依赖任何 automation。** 本地 scheduler（单线程，30 秒 tick）
负责 snooze 唤醒、follow-up 到期、周期性 recovery、简报快照预计算 —— 全是确定性任务，
不调模型。

> **snooze 与 follow-up 都不用 automation。** 全部落在本地 SQLite（`wake_at` / `due_at`），
> 由那一个 scheduler 统一到期恢复。禁止为每封邮件创建一个 automation。

---

## 十、安全边界

### AI 可以做什么

起草、改写、摘要、分类、抽取、构造上下文、更新队列状态。

### AI 绝对不可以做什么

| 动作 | 原因 |
|---|---|
| **发送邮件** | 只有你能发。发送需要 `state=approved` + 一次性 `approve_token` + 显式 `confirm="SEND"`，三重条件缺一即拒 |
| 永久删除邮件 | 只能移到 `Trash`，且可撤销 |
| 对外转发 | — |
| 改账号凭据 | — |
| 自己点「审核通过」 | 审核是人的动作 |

> 这些边界是用户明确要求过的，不要因为「反正能撤销」就自己动手。

### 隐私

- **LOCAL FIRST**：邮件正文、附件、联系人、草稿、日志默认全在本机 SQLite 与 `logs/`。
- WorkBuddy 只拿到**当前任务必要的上下文**（`MailContextPackage`），不是整个邮箱。
- 敏感信息（身份证、银行卡、手机号）在普通日志里**脱敏**（`util.redact`）。
- 凭据只在 `~/.workbuddy/secrets/mail.cred`，不进项目目录。
- Metrics 只存本机，不建任何 telemetry。

### Foxmail 的定位

Foxmail **不是自动化控制目标**：不 GUI 自动化点击、不模拟鼠标键盘发送、
不依赖它的窗口状态。它只承担三个角色：桌面客户端、历史邮件来源、备用客户端。
通信一律走标准协议（IMAP / SMTP）+ 只读解析其本地索引文件。

---

## 十一、API 一览

V2 UI 内部用 `/api/*`，外部经 `/api/v2/*` 访问（同一套路由）。

**只读 / 状态**

```
GET  /api/health              组件健康（imap/smtp/foxmail_index/draft_queue/workbuddy/scheduler/imap_idle/storage）
GET  /api/metrics             本地指标（含 Time to Inbox Zero、草稿接受率）
GET  /api/state               存储与队列总览
GET  /api/contract            Worker 契约说明（自描述）
GET  /api/state-machine       状态机图与合法跳转
GET  /api/constants           枚举与中文标签
GET  /api/buckets             五个工作桶
GET  /api/brief               Daily Brief（结构化 + 纯文本）
GET  /api/folders             文件夹与计数
GET  /api/messages            邮件列表（folder/unread/limit/offset）
GET  /api/messages/{id}       单封详情（含线程、候选、任务、附件、联系人）
GET  /api/messages/{id}/context   起草上下文预览
GET  /api/threads             线程列表
GET  /api/threads/{id}        线程详情（含消息序列）
GET  /api/search              搜索（sender:/subject:/after:/before:/has:attachment/status:/label:）
GET  /api/candidates          候选列表
GET  /api/snoozes /api/followups
GET  /api/contacts /api/draft-memory
GET  /api/events/recent       SSE 重连补发
```

**草稿任务（Worker 契约）**

```
GET  /api/draft-jobs                    任务列表
GET  /api/draft-jobs/next               ★ 原子认领
GET  /api/draft-jobs/{id}               任务详情
GET  /api/draft-jobs/{id}/context       ★ 最小充分上下文
GET  /api/draft-jobs/{id}/events        事件审计
POST /api/draft-jobs                    ★ 创建任务（幂等）
POST /api/draft-jobs/{id}/plan          ★ 提交 ReplyPlan
POST /api/draft-jobs/{id}/draft         ★ 提交草稿
POST /api/draft-jobs/{id}/fail          ★ 报告失败
POST /api/draft-jobs/{id}/heartbeat     续租
```

**人的动作**

```
POST /api/drafts/{id}/edit      编辑草稿
POST /api/drafts/{id}/mode      切换 Draft Mode
POST /api/drafts/{id}/input     补齐缺失信息 → 继续生成
POST /api/drafts/{id}/revise    快捷改写（shorter/longer/more_formal/...）
POST /api/drafts/{id}/approve   审核通过（产生一次性 token）
POST /api/drafts/{id}/revoke    撤回审批
POST /api/drafts/{id}/send      发送（需 approved + token + confirm="SEND"）
POST /api/drafts/{id}/dismiss   放弃（approved 需先 revoke）
POST /api/actions              单封动作（reply_ai/reply_manual/archive/snooze/waiting/done/ignore/star/read/trash）
POST /api/actions/batch        批处理
POST /api/undo                 撤销
POST /api/snooze               延后
POST /api/followups            安排跟进
```

**写邮件（人工起草）**

```
POST /api/compose/draft        {"to","subject","body"} → 存 IMAP 草稿箱（绝不发送）
POST /api/compose/send         {"to","subject","body","confirm":"SEND"} → 发送 + 写「已发送」副本
```

> ⚠️ `/api/compose/send` 缺 `confirm="SEND"` 时**直接拒发**（返回 400 + `policy`）。
> 这是服务端的闸门，不是前端交互效果 —— AI / 脚本不得调用本端点。

**浏览（侧边栏用）**

```
GET  /api/folders              文件夹树（系统 + 自建，带 total/unread + index_count）
GET  /api/messages?folder=X                按文件夹列邮件（邮件级档案视图）
GET  /api/messages?classification=REPLY    按标签列邮件
GET  /api/messages?folder=X&unread=1       只看未读
```

> `/api/messages` 与 `/api/buckets` 分工不同：buckets 是**工作视图**
> （线程级、带工作集时间窗），messages 是**档案视图**（邮件级、所见即全量）。
> 刻意不共用查询 —— 档案视图要的是准确与完整，不该被工作集时间窗过滤掉。
> 返回里的 `total` 是命中总数（**不受 `limit` 截断**），列表与计数共用同一套筛选条件。

**附件**

```
GET  /api/attachments/{message_id}          列出附件（带 attachment_id / 下载 URL / 是否已下载）
GET  /api/attachments/download?aid=<id>     下载（二进制；?inline=1 为内联预览）
POST /api/attachments/open                  {"attachment_id": "..."} 下载并用系统默认程序打开
POST /api/attachments/reveal                {"attachment_id": "..."} 下载并在文件管理器里定位
```

> 附件 id 为什么走 query / body 而不是路径段，见上面「附件」一节的说明。

**维护**

```
POST /api/sync                     手动增量同步
POST /api/classify                 重跑规则分类
POST /api/reclassify               用当前 classify_rules.json 重算全库分类与群发标记
POST /api/candidates/scan          候选检测
POST /api/maintenance/recovery     队列自愈
POST /api/maintenance/repair-threads  线程回收（重复历史存根 + 只剩历史邮件的空线程）
POST /api/maintenance/tick         手动触发一次 scheduler tick
POST /api/import/foxmail           导入 Foxmail 历史索引
GET  /api/events                   SSE 事件流
```

**响应约定**：成功 `{"ok": true, "data": ...}`，失败 `{"ok": false, "error": "..."}`
（配非 2xx 状态码）。例外：`/api/draft-jobs/next` 是扁平的 `{"ok", "job", "lease_until"}`。

---

## 十二、UI 与交互

### 侧边栏：工作桶 + 文件夹树 + 标签树

左栏刻意沿用 V1 的排布（**写邮件在最上 → 文件夹 → 自建文件夹 → 标签 → 底部本地索引**），
理由是用户的手已经记住了哪个位置是什么，换位置比换配色更伤习惯。

```
✏ 写邮件
工作      待我处理 0 / 等待对方回复 8 / AI 草稿就绪 / 已延后 / 今天已完成
          · 超过 14 天没回音 137          ← 沉底区：灰字、缩进、不占工作计数
📢 仅知会  46
文件夹    INBOX 219  Drafts 20  Sent 338  Junk 7  Trash 1
自建文件夹  专利代理 16  未来电池中心 11
标签      ★ 重点  ✉ 要回的  ⚙ 系统处理  ○ 看一眼  × 可忽略
📚 本地索引 6696 封可检索 · 库内合计 11189 封 · 3243 个线程
```

几条刻意的设计：

- **沉底入口视觉上刻意比工作桶轻**（缩进 24px、字号 11.5 对 13、数字不标红、高度 24 对 30）。
  它是可查的档案，不是待办 —— 目的是让人一眼看出「这些不占我的注意力」，
  而不是换一个地方继续焦虑。它在没沉底项时**完全不出现**，常驻一个 0 就是新的噪音。
- **计数规则照抄 V1**：有未读就显示未读（蓝底徽标），没有才显示总数（灰底）。
  看到蓝色就知道这个文件夹里有新东西 —— 这条规则用户已经很熟。
- **点文件夹 / 点标签，走的是同一个列表区**，行外观、快捷键、批处理、详情面板完全照旧。
  实现上只是换了数据来源（`bucket` → `/buckets`，`folder`/`tag` → `/messages`），
  量词也跟着换（工作桶是「个线程」，文件夹/标签是「封邮件」）。
- **文件夹列表 = 库里有的 ∪ 配置里要同步的**。空文件夹也要列出来（计数 0），
  否则「还没同步过」看起来就像「邮件不见了」。
- **标签名称与圆点色来自后端常量**（`CLASS_TREE_LABEL` / `CLASS_TREE_DOT`），
  UI 不硬编码，改一处就够。
- **底部「N 封可检索」** = Foxmail 历史条数。那个伪文件夹 `(Foxmail 历史)`
  不会出现在文件夹树里，它只折算成这个数字。

> **自建文件夹要写进 `config.json` 的 `sync_folders`**，否则不会被同步。
> 默认配置刻意只放 5 个系统文件夹（默认配置会随分发包给别人，
> 不该带上你自己的文件夹名）。

### 写邮件（人工起草，不是 AI 草稿）

侧栏顶部的「写邮件」（快捷键 `c`）打开一个弹窗：收件人 / 主题 / 正文 + 两个出口。

| 按钮 | 行为 | 服务端 |
|---|---|---|
| 存入草稿箱 | 只写 IMAP 草稿箱，**绝不发送** | `POST /api/compose/draft` |
| 发送 | 弹二次确认后才发 | `POST /api/compose/send`，**必须带 `confirm="SEND"`** |

这是与「AI 草稿」**两条不同的通道**：AI 草稿走 `DraftJobQueue`（approve → 一次性 token → send），
人工写邮件走这里。但两者共用同一批底层 helper
（`build_message` → `smtp_client.send` → `imap_client.append` 到「已发送」）——
刻意不另写一套 SMTP 逻辑，因为**两条发信路径迟早会分叉**，
而分叉的那一条往往就是漏掉「留副本」或「确认门禁」的那一条。

`confirm="SEND"` 是**服务端强制的第二道闸**，不是走过场：
缺了它直接拒发并返回 `policy: AI NEVER SENDS EMAIL WITHOUT EXPLICIT HUMAN CONFIRMATION.`。
WorkBuddy 不得调用这个端点（见 [SKILL.md](SKILL.md)）。

### 批量操作：点完必须「立刻有反应 + 列表自己刷新」

全选/勾选后，列表头出现：批量归档 / 批量忽略 / 标为已读 / 加星标 / 批量删除 / 取消选择。

**服务端**——同一批 UID 合并成一条 `UID STORE`：

原来逐封 `apply()` 会让**每封邮件各建一次 IMAP 连接**。本机实测单次连接 150ms，
全选 151 封 = **22.6 秒纯连接开销** —— 用户看到的就是「点了批量按钮，半天没反应」。
现在按 (文件夹, op, flags) 归组，一条 `UID STORE` 带一批 UID（分块，避免命令过长）：

| | 改前（估算） | 改后（实测） |
|---|---|---|
| 批量 60 封 | 9.0 s | **0.21 s** |
| 批量 30 封 | 4.5 s | **0.37 s** |
| 撤销 30 封（每封最多 2 次连接） | 9.0 s | **0.46 s** |

改动的三个点：

1. `imap_client.store_flags_many(uids, op, flags, chunk=150)`：一条命令打一批 UID。
2. `actions._prestore_flags()`：按文件夹分组合并下发；**任一分组失败就整批退回逐封** ——
   宁可慢，也不能让本地状态领先于服务器（那会造成「界面说改好了、邮箱其实没改」）。
3. 批处理**只写一条 undo 记录**：`apply(no_undo=True)`。否则 151 封会灌进 151 行 undo，
   还会把「整批撤销」挤下去（撤销时只撤到最后一封）。

撤销走同一套：`_undo_flag_ops()` 按 (op, flags) 归组后合并下发。

**前端**——先说话，再干活：

- 点下去**立刻**弹「正在批量归档，共 N 封…」，按钮同时禁用（防连点重复提交）。
- 响应后**无论成功失败都刷新**（失败时也可能已经改了一部分）。
- 「撤销」同样有即时反馈与忙标记。

> 排查用：批处理响应里的 `imap` 字段会说明这次是「服务器标志已合并下发 N 封」
> 还是「退回逐封（附失败原因）」。

### 列表上方：全选；列表下方：回到顶部

| 位置 | 控件 | 行为 |
|---|---|---|
| 列表最上方（贴着行内复选框那一列） | ☐ 全选 | 三态：全选 / 部分选中（横杠）/ 未选。全选时文案变「取消全选」，再点即清空。作用对象是**当前列表**（当前工作桶/文件夹/标签里已加载的邮件） |
| 列表右下角 | ↑ 回到顶部 | 滚过 600px 才淡入（同 V1）。点一下平滑回到顶端 |

两条实现上的注意：

- **全选不要做成「只选可见的前 N 条」而不说明。** 文件夹视图只加载前 200 封，
  列表头会写明「共 N 封，显示前 200」，避免用户以为全选就是全库。
- **回到顶部的显隐必须在每次列表重绘后重算**（`renderList()` 末尾调 `syncToTop()`）。
  按钮和列表内容一起被替换，只看 DOM 状态会出现「重绘一次按钮就不见了」——
  V1 踩过这个坑，这里沿用它的结论。
- 切换视图/文件夹时会把滚动位置重置到顶端，否则从滚到一半的旧列表切过来会停在新列表中间。

### 快捷键（Keyboard First）

| 键 | 动作 | 键 | 动作 |
|---|---|---|---|
| `j` / `k` | 下一封 / 上一封 | `d` | 完成 |
| `Enter` | 打开 | `w` | 等对方 |
| `r` | AI 起草 | `x` | 选中（批处理） |
| `a` | 归档 | `u` | 撤销 |
| `s` | 延后 | `e` | 外部客户端打开 |
| `c` | 写邮件 | `b` | 今日简报 |
| `/` | 搜索 | `Esc` | 关弹窗 / 退出输入框 |

目标：**一封邮件最多 1~2 次操作即可退出 Inbox。**

### 其他

- **批处理**：勾选多封 → 归档 / 忽略 / 标已读 / 加星标 / 删除。合并下发，速度见上一节。
- **邮件行的排布**：`[勾选] [主题/摘要] [发件人] [时间]`。发件人列**右对齐且只有 104px**，
  这样短名字紧贴时间（间距只有一个列间距 6px）；左对齐的宽列会让名字与时间之间
  空出一大片，看着像没对齐。
- **右侧正文列占 54%（上限 900px）**：正文与草稿面板都在这一列里排，太窄看长文很挤。
- **Draft Modes**：`quick` / `normal`（默认）/ `detailed` / `formal` / `academic`，一键切换。
- **Reply Controls**：`shorter` / `longer` / `more_formal` / `more_friendly` / `more_direct` /
  `chinese` / `english` / `bilingual` / `regenerate` / `fix_typo` / `strip_boilerplate`。不用重写 prompt。
- **草稿异步**：点「AI 起草」立即显示 `queued`，**页面绝不阻塞等模型**。
- **搜索操作符**：`sender:` `subject:` `after:` `before:` `has:attachment` `status:` `label:`。
- **Draft Memory**：记录你的修改习惯（常删的套话、偏好长度、中英文、署名），用于减少重复编辑。
  可在 `config.json` 关掉（`draft_memory_enabled: false`），也可 `POST /api/draft-memory/clear` 清空。
  它**不建人格模型**，目标只是少改几次。

### 附件：点一下就下载并用默认程序打开

附件默认**不下载、也不喂给模型**（规范 §22）：同步时只在本地记
`filename / type / size / sha256`。你点它的时候才从 IMAP 取，取完缓存在本地。

| 交互 | 行为 |
|---|---|
| 点附件名 | 下载（首次从 IMAP 取）→ **用系统默认程序打开** |
| 点 `⤓` | 只下载（走浏览器原生下载，落在浏览器下载目录） |
| 点 `📂` | 下载 + 在文件管理器里定位 |

实现要点：

- **按内容指纹缓存**：落盘路径是 `attachments/<sha256前16位>_<安全文件名>`，
  同一附件只下一份，重复点击直接命中本地缓存（不再联网）。
- **文件名必须消毒**：附件名来自外部邮件，可以包含 `../` 或 `C:\`。
  `safe_filename()` 只取基名、过滤非法字符、回避 Windows 保留名，
  且最终路径必须仍在 `attachments/` 之内（二次兜底）。
- **附件 id 不能放路径段**：它是 `<sha256>:<原始文件名>`，文件名可能含中文甚至 `/`。
  放进路径段后 `%2F` 会被整段解码成一个真正的 `/`，把一个路径段切成两段 → 404。
  所以下载走 `?aid=`（query），打开/定位走 JSON body。
- **中文文件名按 RFC 5987 编码**：响应带
  `Content-Disposition: attachment; filename*=UTF-8''%E6%B5%B7%E6%8A%A5.jpg`，
  浏览器保存下来不会变成乱码。
- **`用默认程序打开` 是本机动作**：等价于在资源管理器里双击，不发信、不改邮箱、
  不访问网络，因此不触碰「AI 绝不发信」那条边界。
- Foxmail 历史记录只有元数据、没有附件内容 → 附件显示为「不可下载」并说明原因，
  而不是让你点了才报错。
- 下载的附件落在项目根的 `attachments/`（可用 `attachments_dir` 改），
  `pack.py` 会把它排除在分发包之外。

---

## 十三、配置

见 `config.example.json`，实际读 `config.json`。V2 新增：

| 键 | 默认 | 作用 |
|---|---|---|
| `action_window_days` | 30 | 工作集时间窗（天）。**0 = 不设窗口（全量）** |
| `waiting_window_days` | 14 | 「等待对方回复」的活跃窗口（天）。超期的沉底。**0 = 退回旧行为（全部算活跃）** |
| `inbox_informational_classes` | `[]` | 哪些分类算「仅知会」而非「我的事」。设 `["READ"]` 可把 `○ 看一眼` 移出「待我处理」 |
| `attachments_dir` | `<项目根>/attachments` | 下载的附件落盘位置 |
| `default_draft_mode` | `normal` | 默认草稿模式 |
| `auto_prepare_drafts` | `false` | 是否允许规则自动预备草稿（默认关闭：候选不自动调 AI） |
| `scheduler_enabled` | `true` | 本地调度器开关 |
| `scheduler_tick_seconds` | 30 | 调度器 tick 间隔 |
| `max_retry` | 3 | 草稿任务最大重试 |
| `lease_seconds` | 300 | 认领租约时长 |
| `queued_timeout_seconds` | 7200 | queued 超时 |
| `generating_timeout_seconds` | 900 | generating 超时 |
| `ready_expire_hours` | 72 | ready 多久没人审就过期 |
| `retry_base_seconds` / `retry_max_seconds` | 60 / 1800 | 指数退避上下限 |
| `draft_context_recent_messages` / `_max_messages` | 6 / 10 | 上下文里带几封历史 |
| `sync_folders` | 5 个系统文件夹 | 同步哪些文件夹。**自建文件夹（含中文名）写在这里**，否则侧边栏里没有它们 |
| `sync_initial_limit` | 300 | 每文件夹首次拉多少封 |

> **`Sent` 必须同步。** 没有出站邮件就无法判断「这封我已经回过了」，
> 所有线程都会显示成「待我处理」。本机实测：不同步 Sent 时 WAITING_FOR_REPLY = 0，
> 同步后 = 126。
>
> **默认值刻意只放 5 个系统文件夹**：默认配置会随分发包给别人，
> 不该出现某个人自己的自建文件夹名。你的自建文件夹请写在**自己的 `config.json`** 里。
>
> ⚠️ **中文文件夹名踩过的坑（两次，方向相反）**
>
> 1. **「服务器上没有这个文件夹」是误判。** Zimbra 上中文文件夹以 modified UTF-7 存放
>    （`&ThNSKU7jdAY-` = 专利代理）。早先我用 PowerShell 发同步请求，而
>    `Invoke-RestMethod` 默认不按 UTF-8 编码请求体 → 中文名变成 `????` →
>    服务端报 `EXAMINE failed`。我当时据此把这两个文件夹从 `sync_folders` 里删了。
>    **它们一直都在**：正确编码后同步到专利代理 16 封、未来电池中心 11 封。
>    用 UTF-8 字节发请求（或干脆用 CLI）就能避开这个假象。
> 2. **`mutf7_encode` 本身也曾编错。** 它用了标准 base64，而 modified UTF-7 的
>    字母表把 `/` 换成 `,`。对「未来电池中心」正好编出 `...V/D-`（服务器上是 `...V,D-`），
>    于是 `status()` 去查一个不存在的文件夹。
>    症状很迷惑：`select`（走另一条路径）正常，所以像极了「文件夹不存在」。
>    已修，并有往返测试锁住（含 `&-` 是字面 `&` 这条边界）。
>
> 结论：看到 `EXAMINE failed` 处理中文文件夹时，**先怀疑名字编解码，再怀疑文件夹不存在**。

`action_window_days` 是「待我处理」唯一需要按习惯调的旋钮：
想收得更紧就调小（比如 14），想看全量就设 0。

`waiting_window_days` 管的是「等待对方回复」的活跃窗口，默认 14 天。
觉得 14 天太短（有些事确实要等更久）就调大，比如 30；
想要旧行为（全部算活跃、不沉底）就设 0。
如果只是**个别**几件事要盯久一点，不用动这个旋钮 ——
对那几件事单独建 followup 即可，它们不受窗口限制。

---

## 十四、排查

| 症状 | 原因 / 处理 |
|---|---|
| 页面打不开 | 服务没起 → 双击 `启动V2工作台.cmd`，或看 `_watchdog.log` |
| `/workbench` 白屏 | 看浏览器控制台；V2 UI 的 API 前缀由 `window.MW_API_BASE` 注入（应为 `/api/v2`） |
| 「等待对方回复」永远是 0 | **Sent 没同步** → `POST /api/v2/sync {"folders":["Sent"],"limit":600}` |
| 「等待对方回复」一直涨、清不掉 | 先看 `waiting_window_days` 是不是被设成了 `0`（那会退回旧行为：只增不减）。正常应 >0，超期项会沉底到它下面那行灰色入口；再想清掉就直接点进沉底区批量归档 |
| 「待我处理」数量巨大 | 检查 `action_window_days`；再看 ⚙系统类是否被算进去了（不该进桶）；再考虑 `inbox_informational_classes` |
| 改了 `classify_rules.json` 但界面没变 | **必须跑 `POST /api/v2/maintenance/reclassify`** —— 分类是存在库里的 |
| 某个附件点了下载没反应 / 404 | 附件 id 里的原始文件名可能含 `/` 或中文。确认走的是 `?aid=`（query）而不是路径段 |
| 附件下载下来文件名乱码 | 响应应带 `filename*=UTF-8''...`；若没有说明 Content-Disposition 没设上 |
| 历史邮件点附件提示不能下载 | **设计如此**：Foxmail 索引只有元数据，不含附件内容 |
| 某封邮件的附件「消失」了 | 可能是裸 UTF-8 中文文件名（不合规但常见）曾导致解析不到，**已修**（`parser.attachment_meta`） |
| 简报数字和首页不一致 | **不应该发生** —— 简报直接消费 `buckets.compute()`。若出现，说明有人又写了一套统计 SQL，去看 `scheduler.build_brief_payload` |
| AI 起草点了没反应 | `GET /api/v2/health` 看 `draft_queue`；若任务停在 queued 说明没有 worker 认领（正常：WorkBuddy 按需认领） |
| 任务卡在 generating | 等租约过期后 Recovery 回滚，或手动 `POST /api/v2/maintenance/recovery` |
| 候选数量巨大 | 首次全量同步曾为历史邮件造出上千候选。`/api/maintenance/recovery` 会归档超出工作集的候选 |
| 线程数异常膨胀 | `POST /api/v2/maintenance/repair-threads` 回收「只有历史邮件」的线程 |
| 线程状态随机漂移 / 列表显示错发件人 / 真实邮件被从桶里挤掉 | 历史存根与 IMAP 邮件**时间戳完全相同**，取「最新一封」不确定。**已修**：统一用 `repo.LATEST_ORDER`（同一时刻 IMAP 优先），并停掉重复存根 |
| `approved` 状态点「放弃」失败 | **设计如此**：先 `revoke` 撤回审批 |
| IDLE 反复断线 | 已修：imaplib 的 `readline` 走 socket 缓冲层，socket 超时后永久损坏（`cannot read from timed out object`）。V2 的 IDLE 改走**裸 socket 读取** |
| 点批量按钮「没反应」 | **已修**（根因是每封邮件各建一次 IMAP 连接，151 封≈22.6 秒）。现在合并下发 + 即时反馈 + 响应后必刷新。若仍慢，看响应里的 `imap` 字段是否显示「退回逐封」 |
| 点「撤销」卡很久 | **已修**：撤销同样按 (op, flags) 合并下发（30 封 9.0s → 0.46s） |
| 8080 被别的进程占着 | 历史遗留的旧副本目录可能还跑着 `watchdog.py` 在拉起旧 server。先杀掉再启（本机曾遇到 `<本项目目录>` 的残留进程） |
| 侧边栏里少了某个自建文件夹 | 它没被同步过。把文件夹名加进 `config.json` 的 `sync_folders`，再 `POST /api/v2/sync` |
| 同步中文文件夹报 `EXAMINE failed` | **先怀疑编解码，别急着认定文件夹不存在**。用 CLI 或 UTF-8 字节发请求（PowerShell 的 `Invoke-RestMethod` 默认会把中文请求体编坏）；再检查 `mutf7_encode` 有没有把 `,` 写成 `/` |
| 点了「写邮件 → 发送」没反应 | 看返回的 `error`。缺 `confirm="SEND"` 会被**服务端**拒发（这是设计） |
| 发的信在「已发送」里找不到 | 看响应里的 `appended_to_sent`。已修：投递后会 APPEND 一份到 `sent_folder`；APPEND 失败**不算发送失败**，会如实告知 |

---

## 十五、验证怎么做

```bash
# 1) 单元 / 回归测试（216 个）
python -X utf8 -m unittest discover -s mail_workbench/tests -t .

# 2) V1 UI 零回归（需要 Node + playwright-core）
node uitest3.cjs && node uitest2.cjs

# 3) V2 界面自检 + 截图（产出 _v2_workbench.png / _v2_sidebar.png / _v2_uitest_report.json）
node uitest_v2_workbench.cjs

# 4) 端到端契约验证（对运行中的服务，覆盖 §25/§27/§8/§9/§13/§12/§33/§29）
python -X utf8 _e2e_contract.py

# 5) 附件 + 分桶真机验证
python -X utf8 _verify_v2_2.py

# 6) 打包 + 隐私自检（会检查是否混入密码 / 真实邮箱 / 本机路径）
python -X utf8 pack.py
```

> `_e2e_contract.py` 会在开始前**清空活跃草稿任务**以保证可重复执行
> （会在输出里报告清掉了几个）。在真的有草稿要保留时不要跑它。
>
> 两个脚本（`_e2e_contract.py` / `uitest_v2_workbench.cjs`）都**不再写死「待我处理」桶** ——
> 它会随你的整理而变空，写死就会报一堆假失败。它们现在自己挑一个有内容的视图。
>
> **响应形状约定**：凡是返回草稿任务的端点，任务都在 `data.job` 下（不是 `data` 顶层）；
> 只有 `/api/draft-jobs/next` 是扁平的 `{ok, job, lease_until}`（Worker 契约）。

---

## 十六、已知限制

- 正文提取是本地规则 + 正则；极复杂的 HTML 邮件可能有排版噪声。
- Foxmail 历史邮件**只有元数据**（正文在 Foxmail 里加密存放），
  需要正文时按需经 IMAP 补取；历史邮件不产生候选、不进工作桶。
- `fact_extractor` 的中文日期识别覆盖常见写法，冷僻表述可能漏。
- 附件分析（`AttachmentSummaryCache`）只在你明确需要时才触发，默认不把附件喂给模型。
- 单用户本机应用：不引入 Kafka / Redis / K8s / Celery / 微服务。
  Python + SQLite + 文件系统 + 线程 + asyncio + SSE + IMAP IDLE 就是全部依赖。
- 工作桶的**分类权重**来自规则引擎，需要按自己的邮件习惯微调
  （`classify_rules.json`）。默认规则会把机构群发通知判成 ★重点，
  于是它们会占满「待我处理」—— 这是规则问题，不是桶的问题。
