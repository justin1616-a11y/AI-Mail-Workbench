/* ③ 中间列：工具条（搜索 / 筛选 / 批量）+ 邮件列表。
   列表项要素：未读圆点、复选框、发件人、主题、时间、附件标识、来源标签。 */

import { icons } from '../icons.js';
import { classify, splitFrom, TIERS } from '../classify.js';

const FILTERS = [
  { id: 'all', name: '全部' },
  { id: 'unread', name: '未读' },
  { id: 'attach', name: '带附件' },
];

export function renderList(state) {
  // 撤销条排在最上面：它是临时的、时间敏感的，不该被别的东西压下去
  return renderToolbar(state) + renderUndoBar(state) + renderDraftBar(state)
    + renderBulkbar(state) + renderItems(state) + renderToTop();
}

/* 「回到顶部」悬浮按钮 —— 滚过一屏才出现，由 main.js 的 syncToTop() 控制显隐。
   放在这里而不是常驻 DOM：列表整块重绘时它会被一起换掉，
   跟着视图走才不会出现「重绘后按钮卡在旧状态」。 */
function renderToTop() {
  return `<button class="to-top" data-action="to-top" title="回到列表最顶端">↑ 回到顶部</button>`;
}

/* 「已移到已删除邮件」之后的撤销条

   删除是这个应用里唯一会动用户数据的手动操作，所以给一次反悔的机会。
   只针对**最近一次** —— 再往前的邮件可能已经被别处（Foxmail / 网页邮箱）
   动过，凭旧编号去搬有搬错的风险，所以宁可不提供。

   放在列表顶部而不是用 2 秒就消失的轻提示：点错了得有个明确的地方能点回来。 */
function renderUndoBar(state) {
  const u = state.undoTrash;
  if (!u) return '';
  return `<div class="undo-bar">
    ${icons.trash}
    <span>已把 <b>${u.count || 0}</b> 封移到「已删除邮件」</span>
    <span class="spacer"></span>
    <button class="btn-mini" data-action="undo-trash">撤销</button>
    <button class="btn-mini" data-action="dismiss-undo" title="关掉这条提示">×</button>
  </div>`;
}

/* 草稿就绪提示条

   刻意做成「用户点了才取走」：草稿在磁盘上等着，不会因为页面刷没了、
   或者被另一个看不见的页面实例轮询到而消失。 */
function renderDraftBar(state) {
  if (!state.replyDone) return '';
  /* 点过「让 WorkBuddy 起草」的那些会**自动填进编辑器**（见 main.js 的自动回传），
     所以这里剩下的多半是每小时自动处理攒下来的 —— 明确写出来，免得用户纳闷
     「我点的那封怎么不在提示条里」。 */
  const autoN = (state.doneMeta || []).filter(
    (d) => String(d.uid) && (state.awaitingUids || []).map(String).includes(String(d.uid))).length;
  const label = autoN
    ? `WorkBuddy 已起草好 <b>${state.replyDone}</b> 封（你点的 <b>${autoN}</b> 封已自动填好）`
    : `WorkBuddy 已起草好 <b>${state.replyDone}</b> 封回复`;
  return `<div class="draft-bar">
    ${icons.draft}
    <span>${label}</span>
    <span class="spacer"></span>
    <button class="btn-mini" data-action="load-draft">点开修改</button>
  </div>`;
}

function renderToolbar(state) {
  const inSearch = state.view === 'search';
  const inTag = state.view === 'tag';
  const srcLabel = inSearch ? '本地索引' : 'IMAP';
  /* 「全部/未读/带附件」这三个筛选在标签视图里也用不了 ——
     索引里没有 seen / attach，筛了也不生效，不如置灰。 */
  const noFilter = inSearch || inTag;
  const chips = FILTERS.map((f) =>
    `<button class="chip${state.filter === f.id && !noFilter ? ' is-active' : ''}"
       data-filter="${f.id}" ${noFilter ? 'disabled style="opacity:.45"' : ''}>${f.name}</button>`).join('');

  /* 标签视图：说清筛的范围和命中数。
     不写清楚的话，「点 ★ 重点 只出来 3 封」会被当成坏了。 */
  const tagChip = inTag
    ? `<span class="chip" style="cursor:default"
         title="标签只筛当前文件夹里已加载的邮件（往下滚会自动续拉）。这样每一封都还能标已读 / 标重要 / 删除 —— 全库索引里的邮件做不到这些。"
         >已加载 ${(state.messages || []).length} 封 · 命中 <b>${tagHits(state)}</b></span>`
    : '';

  return `<div class="list-toolbar">
    <div class="search-box">
      ${icons.search}
      <input id="searchInput" placeholder="搜 8067 封历史邮件（本地索引，毫秒级）"
             value="${esc(state.query)}" />
      ${state.query ? `<span class="search-src">${srcLabel}</span>` : ''}
    </div>
    <div class="filter-row">
      ${chips}
      ${tagChip}
      ${state.replyPending ? `<span class="chip" style="cursor:default"
         title="同一封邮件重复点「让 WorkBuddy 起草」只会入队一次，不会让 WorkBuddy 重复劳动。手动点的插到队首优先处理，通常 30 分钟内出草稿（按钮上会显示已等多久）"
         >⏳ ${state.replyPending} 封等起草</span>` : ''}
      ${state.stalledCount ? `<span class="chip is-warn" style="cursor:default"
         title="这几封等得太久了（加急件超过 10 分钟、自动入队的超过 90 分钟还没被处理）。多半是 WorkBuddy 那侧的定时任务没跑起来 —— 在 WorkBuddy 对话里说一句「处理邮件回复队列」可以立刻处理它们"
         >⚠ ${state.stalledCount} 封处理超时</span>` : ''}
      ${state.failedCount ? `<span class="chip" style="cursor:default;color:var(--danger-text)"
         title="这几封起草失败了（原因写在阅读窗格的按钮上）。打开那封邮件，点「生成失败 · 重试」就能重新排队"
         >⚠ ${state.failedCount} 封起草失败</span>` : ''}
      ${inSearch ? `<button class="chip" data-action="exit-search">${icons.close} 退出搜索</button>` : ''}
      <span style="flex:1"></span>
      ${liveChip(state)}
      <button class="chip" data-action="refresh" title="重新拉取">${icons.refresh}</button>
    </div>
  </div>`;
}

/* 实时同步状态灯。
   为什么必须显式画出来：以前页面从不主动问服务器，新邮件不来、别处删的也不消失，
   用户根本分不清「今天没邮件」和「同步坏了」。有了这盏灯，
   live=正在被服务器推着、poll=退回轮询兜底、down=断了。
   点一下可以看详情，也知道上次同步是什么时候。 */
function liveChip(state) {
  const st = state.liveStatus || 'connecting';
  if (st === 'connecting') {
    return `<span class="chip" style="cursor:default" title="正在建立实时同步通道">◌ 连接中…</span>`;
  }
  if (st === 'down') {
    return `<span class="chip is-warn" style="cursor:default"
       title="实时通道断了（服务端连不上邮箱或进程被停了）。每 30 秒仍会重试一次；也可以点右边的刷新按钮手动拉">⚠ 同步中断</span>`;
  }
  const at = state.liveAt ? `上次同步 ${state.liveAt}` : '';
  if (st === 'poll') {
    return `<span class="chip" style="cursor:default"
       title="实时推送不可用，已退回每 30 秒对账一次${at ? '。' + at : ''}">⏱ 30 秒轮询${state.liveAt ? ' · ' + state.liveAt : ''}</span>`;
  }
  return `<span class="chip" style="cursor:default"
     title="服务端有一条只读 IMAP 连接挂在当前文件夹上（IDLE）：这里收到的新邮件、在 Foxmail / Zimbra 那边做的新增或删除，都会立刻同步过来${at ? '。' + at : ''}">⚡ 实时同步${state.liveAt ? ' · ' + state.liveAt : ''}</span>`;
}

function renderBulkbar(state) {
  const n = state.checked.size;
  if (!n) return '';
  const all = state.messages.filter(visible(state)).length;
  /* 不要 spacer 撑开：列表栏只有 380px，六个按钮本来就排不下。
     让它们老老实实按整颗换行（CSS .bulkbar 里 flex-wrap + nowrap），
     靠左密排两行；用 spacer 把按钮顶到第二行反而会空一大块。 */
  return `<div class="bulkbar">
    <span class="bulk-count">已选 ${n} 封</span>
    <button class="btn-mini" data-action="check-all">全选 ${all}</button>
    <button class="btn-mini" data-action="uncheck-all">取消</button>
    <button class="btn-mini" data-action="mark-read">标已读</button>
    <button class="btn-mini" data-action="mark-unread">标未读</button>
    <button class="btn-mini" data-action="mark-important"
            title="标为重要：左侧「★ 重点」里能找到它们，Foxmail 里也会显示星标">★ 标重要</button>
    <button class="btn-mini" data-action="trash-checked"
            title="移到「已删除邮件」文件夹 —— 不是彻底删除，之后还能找回来">移到已删除邮件</button>
  </div>`;
}

function visible(state) {
  return (m) => {
    if (state.filter === 'unread') return !m.seen;
    if (state.filter === 'attach') return m.attach;
    return true;
  };
}

function renderItems(state) {
  if (state.loadingList) {
    return `<div class="skeleton">${'<div class="sk-row"></div>'.repeat(7)}</div>`;
  }
  if (state.listError) {
    return `<div class="state err">${esc(state.listError)}</div>`;
  }

  // 先用筛选（全部/未读/带附件），再套标签分级。
  // 注意：本地索引结果没有 seen / attach 字段，不参与这两层过滤。
  let list = state.messages;
  if (state.source !== 'index') {
    list = list.filter(visible(state));
    if (state.view === 'tag') {
      list = list.filter((m) => classify(normalize(m)) === state.activeFolder);
    }
  }

  if (!list.length) {
    // 标签视图为空时不能笼统说「文件夹是空的」——用户点的是标签，
    // 容易误以为点坏了。要说清「在哪个文件夹里、找哪个标签、没找到」。
    // 筛没了和「文件夹是空的」是两回事 —— 前者是筛选条件太窄，
    // 说成后者会让人以为邮件丢了。
    let msg = '这个文件夹是空的';
    if (state.filter === 'unread') {
      msg = '已加载的这些邮件里没有未读的';
    } else if (state.filter === 'attach') {
      msg = '已加载的这些邮件里没有带附件的';
    } else if (state.view === 'search') {
      msg = '没有匹配的邮件';
    } else if (state.view === 'tag') {
      const tier = TIERS.find((t) => t.id === state.activeFolder);
      msg = `已加载的 ${state.messages.length} 封里没有「${tier ? tier.name : state.activeFolder}」的邮件，往下滚会续拉更早的`;
    }
    return `<div class="state">${esc(msg)}</div>`;
  }

  return `<div class="list-scroll">${list.map((m) => item(m, state)).join('')}${renderListFoot(state)}</div>`;
}

/* 列表末尾的翻页状态条。
   标签视图走全库索引、也要翻页；只有**搜索**是一次给全，不给翻页提示。 */
function renderListFoot(state) {
  if (state.loadingList || state.view === 'search') return '';
  if (state.loadingMore) return '<div class="list-more">正在加载更早的邮件…</div>';
  if (state.listHasMore) return '<div class="list-more">继续往下滚，自动加载更多</div>';
  return '<div class="list-more is-end">已经到底了</div>';
}

/* 当前这批复用的邮件里有多少封属于这个标签（工具栏要显示命中数） */
function tagHits(state) {
  return (state.messages || []).filter(
    (m) => !m.source && classify(normalize(m)) === state.activeFolder).length;
}

function normalize(m) {
  const p = splitFrom(m.from);
  // flagged 要一起带上：手动标的重要优先于自动分级规则
  return { from: p.addr, from_name: p.name, flagged: !!m.flagged };
}

function item(m, state) {
  const id = m.uid || m.id;
  const p = splitFrom(m.from);
  const name = p.name || p.addr || '(未知)';
  const unread = m.seen === false;
  const active = state.selectedId === id ? ' is-active' : '';
  const checked = state.checked.has(id) ? ' checked' : '';
  /* 「索引」标签按**这一条**的来源打，不能看整个列表的 source ——
     「★ 重点」里混了 IMAP 的手动标重要邮件（有 uid、能操作），
     它们不该被标成索引项。 */
  const isIdx = m.source ? m.source === 'index' : state.source === 'index';
  const tier = isIdx ? null : classify(normalize(m));
  // 手动标的重要用红色标签单独显示。
  // 这封邮件在分级上已经算「★重点」了，再打一个灰蓝色的「★重点」是重复的，
  // 所以手动标记存在时不再叠加自动分级的标签。
  const flagTag = m.flagged ? '<span class="mi-tag flag">★ 重要</span>' : '';
  const tierTag = !m.flagged && tier && tier !== 'read'
    ? `<span class="mi-tag">${{ vip: '★重点', reply: '要回', act: '系统', dump: '忽略' }[tier]}</span>`
    : '';
  const idxTag = isIdx ? `<span class="mi-tag idx">索引</span>` : '';

  return `<div class="mail-item${unread ? ' is-unread' : ''}${active}" data-id="${esc(id)}">
    <label class="mi-check" data-stop><input type="checkbox" data-check="${esc(id)}"${checked}></label>
    <span class="mi-dot"></span>
    <div class="mi-body">
      <div class="mi-line">
        <span class="mi-from">${esc(name)}</span>
        <span class="mi-time">${esc(shortTime(m.date))}</span>
      </div>
      <div class="mi-subject">${esc(m.subject || '(无主题)')}</div>
      <div class="mi-meta">
        ${m.attach ? `<span class="mi-attach">${icons.attach} 附件</span>` : ''}
        ${flagTag}${tierTag}${idxTag}
      </div>
    </div>
  </div>`;
}

function shortTime(d) {
  if (!d) return '';
  if (typeof d === 'string' && d.includes('-')) {
    const p = d.split(' ');
    return p[1] ? p[0].slice(5) + ' ' + p[1] : p[0].slice(5);
  }
  return d;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
