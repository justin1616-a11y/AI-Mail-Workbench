/* 数据层：所有后端调用集中在这里，组件不直接碰 fetch。
   两个数据源在此汇合：
     /api/messages  -> IMAP（Web 邮箱：未读、附件、文件夹）
     /api/search    -> Foxmail 本地索引（6646 封毫秒级检索）
*/

async function req(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    /* 连不上服务 —— 这是「点了没反应」最常见的成因：
       页面（可能来自缓存）照常显示，但所有操作都发不出去。
       必须用醒目的红条说出来，并直接给出修复动作；
       只弹一个 2 秒的轻提示，用户根本注意不到。 */
    if (window.__fatal) {
      window.__fatal(
        '连不上本地服务（127.0.0.1:8080）—— 页面虽然还显示着，但任何操作都不会生效。\n' +
        '修复：双击 mail-workbench\\启动V2工作台.cmd（或重新登录一次 Windows）。\n' +
        '原始错误：' + e.message
      );
    }
    throw new Error('连不上邮件工作台服务');
  }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const data = await res.json();

  // 服务恢复后，把之前那条「连不上」的红条撤掉
  const box = document.getElementById('fatal');
  if (box && box.textContent.indexOf('连不上本地服务') >= 0) box.remove();
  return data;
}

function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') u.append(k, v);
  }
  const s = u.toString();
  return s ? '?' + s : '';
}

export const api = {
  bootstrap: () => req('/api/bootstrap'),

  /* folder / limit / before —— before 是翻页游标（uid）：
     只取比它更早的邮件。列表一次拉 60 封，滚到底再拉下一批。 */
  messages: (folder, limit = 60, before = 0) =>
    req('/api/messages' + qs({ folder, limit, before })),

  /* 单封正文 + 附件清单（列表里只有元数据，正文要点开才取） */
  message: (folder, uid) =>
    req('/api/message' + qs({ folder, uid })),

  search: (q, limit = 50) =>
    req('/api/search' + qs({ q, limit })),

  /** 本地索引命中后，按主题回服务器取正文（索引只有元数据） */
  find: (subject) =>
    req('/api/find' + qs({ q: subject })),

  /* —— 标记 ——
     op 四种：read / unread（\Seen）、important / unimportant（\Flagged）。
     「重要」走 IMAP 标准标记，不是本地记一笔 —— 所以 Foxmail 里
     看到的是同一个星标，跟「已读」同理。 */
  flags: (folder, uids, op) =>
    req('/api/flags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uids, op }),
    }),

  /* —— 移到「已删除邮件」——
     界面上叫「已删除邮件」，但服务器上那个文件夹实际叫 Trash（IMAP 原名）。
     confirm 由前端弹过二次确认后才带上；后端还会再验一次。 */
  trash: (folder, uids) =>
    req('/api/trash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uids, confirm: true }),
    }),

  /* —— 撤销「移到已删除邮件」——
     把刚挪走的那几封搬回原文件夹。只能撤销最近一次：
     再往前的邮件可能已经被别处动过，凭旧编号去搬有搬错的风险。 */
  untash: (payload) =>
    req('/api/untash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  draft: (to, subject, body) =>
    req('/api/draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to, subject, body }),
    }),

  /* —— 发送 ——
     唯一不可撤销的操作。必须显式 confirm:true 才会真投递；
     传 dryRun:true 只验证 SMTP 连接与登录，不发信。 */
  send: (payload) =>
    req('/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  /* —— 实时同步 ——
     check：极轻量的兜底对账（只回数量 / uidNext / 未读数），SSE 断了时每 30 秒用。
     真正的推送走 EventSource('/api/events?folder=xxx')，那是长连接不走这里；
     服务端接到这条连接时会把 IDLE 监听切到该文件夹。 */
  check: (folder) => req('/api/check' + qs({ folder })),

  /* —— 附件 ——
     取下来存到本地；文档/图片类会用系统默认程序打开，
     可执行类只保存不打开（服务端会说明原因）。 */
  attachment: (payload) =>
    req('/api/attachment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  /* —— 回复起草队列 ——
     应用不自己生成草稿（实测调 WorkBuddy CLI 冷启动要 2 分半，做不成同步）。
     改成异步交接：把请求写进队列，WorkBuddy 那一侧起草后写回，这边轮询取。 */
  replyRequest: (payload) =>
    req('/api/reply-request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  replyQueue: () => req('/api/reply-queue'),

  /* 撤回：把还没起草的请求从队列里拿掉（误点/改主意了） */
  replyCancel: (payload) =>
    req('/api/reply-cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  /* 重试：把失败的那条重新排回队列（attempts 保留，看得出重试了几次） */
  replyRetry: (payload) =>
    req('/api/reply-retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  /* 取走一封已就绪的草稿。不给 id = 取第一封（用户点「点开修改」）；
     给 id = 只取那一封（自动回传用，避免错拿成队列里别的邮件）。
     轮询接口刻意不下发草稿内容，防止旧页面实例把它吃掉。 */
  replyDraft: (id) =>
    req('/api/reply-draft' + (id ? '?id=' + encodeURIComponent(id) : '')),
};
