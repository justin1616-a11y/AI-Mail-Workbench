/* ④ 右侧阅读窗格：标题 / 发件人 / 正文 / 附件 / 操作（回复、转发、写草稿）。 */

import { icons } from '../icons.js';
import { splitFrom } from '../classify.js';

/* 把「等了多久」说成人话。按钮上必须显示真实等待时长 ——
   只写「已加急」的话，用户等 10 分钟不知道是正常排队还是已经卡死。 */
function fmtWait(sec) {
  const s = Number(sec) || 0;
  if (s < 60) return s + ' 秒';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' 分钟';
  return Math.floor(m / 60) + ' 小时' + (m % 60 ? (m % 60) + ' 分' : '');
}

export function renderReader(state) {
  const m = state.messages.find((x) => (x.uid || x.id) === state.selectedId);
  if (!m) {
    return `<div class="reader-empty">
      <div style="text-align:center">
        <div style="font-size:28px;opacity:.35;margin-bottom:8px">${icons.mail}</div>
        <div>选择一封邮件开始阅读</div>
      </div>
    </div>`;
  }

  /* 只能看「这一条」有没有 uid，不能看整个列表的 source ——
     标签视图（★ 重点）里混着 IMAP 的手动标重要邮件，那些有 uid、能正常操作。 */
  const fromIndex = !m.uid;
  const p = splitFrom(m.from);
  const name = p.name || p.addr || '(未知)';
  const detail = state.detail;
  const body = detail ? detail.body || '(无正文)' : (fromIndex ? '' : '');
  const initials = (name[0] || '?').toUpperCase();

  const attachHtml = detail && detail.attachments && detail.attachments.length
    ? `<div class="attach-list">
         <span class="attach-label">附件 ${detail.attachments.length}</span>
         ${detail.attachments.map((a) =>
           `<button class="attach-chip" data-attach="${a.i}"
              title="点击：保存到本地并用默认程序打开（可执行文件只保存不打开）"
              >${icons.attach} ${esc(a.name)}${a.size ? `<span class="ac-size">${fmtSize(a.size)}</span>` : ''}</button>`
         ).join('')}
       </div>`
    : '';

  const actions = fromIndex
    ? `<button class="btn" data-action="find-body">${icons.search} 联网取正文</button>
       <span style="font-size:11.5px;color:var(--text-tertiary);align-self:center">
         本地索引仅存元数据（发件人 / 主题 / 时间）
       </span>`
    /* 起草按钮四态 —— 队列里每条都有自己的状态（见 reply_queue.py）：
         queued     待生成  → 「已加入队列 · 撤回」（手动点的显示「已加急」）
         generating 生成中  → 「生成中…」，不再提供撤回（AI 已经在写了）
         failed     失败    → 「生成失败 · 重试」，点一下重新排队
       不在队列里          → 「让 WorkBuddy 起草回复」
       手滑连点是最常见的浪费：以前点 3 下就往队列塞 3 份同一封邮件，
       WorkBuddy 要起草 3 遍，界面上还显示「3 封等起草」（其实只有 1 封）。 */
    : (() => {
        const uid = String(m.uid || '');
        const q = (state.pendingMeta || []).find((x) => String(x.uid) === uid);
        if (!q) {
          return `<button class="btn" data-action="ai-reply"
               title="点了就插到队首，WorkBuddy 会优先起草，通常 30 分钟内出草稿；草稿好了自动填进编辑器"
             >${icons.reply} 让 WorkBuddy 起草回复</button>`;
        }
        if (q.status === 'generating') {
          return `<button class="btn is-queued" disabled
               title="WorkBuddy 正在读往来线程并起草，已生成 ${fmtWait(q.ageSec)}。超过 15 分钟没动静会自动重新排队"
             >${icons.draft} 生成中… ${fmtWait(q.ageSec)}</button>`;
        }
        if (q.status === 'failed') {
          return `<button class="btn is-failed" data-action="retry-reply"
               title="${esc(q.last_error || '起草失败')}｜点这里重新排队"
             >${icons.draft} 生成失败 · 重试</button>`;
        }
        const manual = q.source === 'manual';
        /* 等太久要如实说出来。以前只显示「已加急 · 撤回」，用户等 10 分钟
           也不知道是正常排队还是已经卡死 —— 现在超过阈值就标黄并写清楚。 */
        const wait = fmtWait(q.ageSec);
        const late = !!q.stalled;
        const why = late
          ? `已经等了 ${wait}，比平常慢（阈值 ${fmtWait(q.stallAfter)}）。` +
            '可能是 WorkBuddy 那侧的定时任务没跑起来 —— 在 WorkBuddy 对话里说一句' +
            '「处理邮件回复队列」可以立刻处理它。'
          : (manual
            ? `已按加急插到队首，通常 30 分钟内出草稿（已等 ${wait}）。点这里撤回`
            : `这封是定时任务自动挑的，已等 ${wait}。点这里撤回`);
        return `<button class="btn is-queued${late ? ' is-stalled' : ''}" data-action="cancel-reply"
             title="${esc(why)}"
           >${icons.draft} ${manual ? '已加急' : '已入队'} · ${wait}${late ? ' ⚠' : ''} · 撤回</button>`;
      })()
      + `<button class="btn ghost" data-action="reply" title="自己写，只预填原文引用">自定义回复</button>
       <button class="btn ghost btn-important${m.flagged ? ' is-on' : ''}" data-action="toggle-important"
               title="${m.flagged
                 ? '取消「重要」标记（Foxmail 里的星标也会一起消失）'
                 : '标为重要：左侧「★ 重点」里能找到它，Foxmail 里也会显示星标'}">${icons.star} ${m.flagged ? '已标重要' : '标重要'}</button>
       <button class="btn ghost" data-action="trash"
               title="移到「已删除邮件」文件夹 —— 不是彻底删除，之后还能找回来">${icons.trash} 移到已删除邮件</button>`;

  return `
    <div class="reader-head">
      <button class="btn-mini reader-back" data-action="back-to-list" title="返回邮件列表">← 返回列表</button>
      <div class="reader-subject">${esc(m.subject || '(无主题)')}</div>
      <div class="reader-meta">
        <div class="reader-avatar">${esc(initials)}</div>
        <div class="reader-who">
          <div class="w-name">${esc(name)}</div>
          <div class="w-addr">${esc(p.addr || '')}</div>
        </div>
        <div class="reader-date">${esc(m.date || '')}</div>
      </div>
      <div class="reader-actions">${actions}</div>
    </div>
    <div class="reader-body">${
      state.loadingBody
        ? '<div class="state">正在取正文…</div>'
        : (body ? esc(body) : (fromIndex ? '<div class="state">点上方按钮从服务器取正文</div>' : '<div class="state">（空）</div>'))
    }</div>
    ${attachHtml}
  `;
}

function fmtSize(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
