/* 入口：订阅 store，分区渲染；所有交互走事件委托（data-* 属性）。
   渲染粒度：rail / folders / list / reader / composer 各自独立重绘，
   输入框不参与重绘，避免打字时丢焦点。 */

import { store, actions } from './store.js';
import { api } from './api.js';
import { renderRail } from './components/rail.js';
import { renderFolders } from './components/folders.js';
import { renderList } from './components/list.js';
import { renderReader } from './components/reader.js';
import { renderComposer } from './components/composer.js';
import { splitFrom, setClassifyRules } from './classify.js';

const el = {
  app: document.getElementById('app'),
  rail: document.getElementById('rail'),
  folders: document.getElementById('colFolders'),
  list: document.getElementById('colList'),
  reader: document.getElementById('colReader'),
  modal: document.getElementById('modalRoot'),
  toast: document.getElementById('toastRoot'),
};

/* ---------------- 渲染 ---------------- */

function paint(parts = ['rail', 'folders', 'list', 'reader', 'composer']) {
  const s = store.get();
  if (parts.includes('rail')) el.rail.innerHTML = renderRail(s);
  if (parts.includes('folders')) el.folders.innerHTML = renderFolders(s);
  if (parts.includes('list')) paintList(s);
  if (parts.includes('reader')) el.reader.innerHTML = renderReader(s);
  if (parts.includes('composer')) {
    // 关闭时把内容清空 —— 配合 css 的 .modal-root:empty{display:none}，
    // 保证这个全屏层在关闭状态下不可能被显示（显示出来就会拦掉整页点击）。
    el.modal.innerHTML = s.composerOpen ? renderComposer(s) : '';
    el.modal.classList.toggle('is-open', s.composerOpen);
    el.modal.hidden = !s.composerOpen;
    if (window.__guardOverlay) window.__guardOverlay();
  }
  // 窄屏两级导航用：选中邮件 = 进入阅读态（宽屏下这个类不起作用）
  if (el.app) el.app.classList.toggle('is-reading', !!s.selectedId);
}

/* 列表重绘：先记住滚动位置，重建 DOM 后再放回去。

   滚动容器 `.list-scroll` 是列表内部的一个 div，整列重绘会把它连内容一起
   换掉 —— scrollTop 随之归零，表现就是「往下翻了几屏、正看某封邮件时，
   列表自己弹回第一封」。列表会因为很多原因重绘（选中、标已读、5 秒一次的
   队列轮询……），所以放在这一层兜住，比逐个调用点去处理可靠。

   例外：换文件夹 / 换筛选 / 搜索这些**内容整体换掉**的场景应该回到顶部，
   由调用方先把 keepListScroll 设为 false。 */
let keepListScroll = true;

function paintList(s) {
  const prev = el.list.querySelector('.list-scroll');
  const top = keepListScroll && prev ? prev.scrollTop : 0;
  el.list.innerHTML = renderList(s);
  const next = el.list.querySelector('.list-scroll');
  if (next) next.scrollTop = top;   // 超出新高度时浏览器自己会夹到末尾
  keepListScroll = true;
  syncToTop();                      // 按钮是跟着重绘一起重建的，显隐要重算
}

// store 已把字段变化映射成区域名，这里直接用
store.subscribe((_s, regions) => paint(regions));

/* ---------------- 数据动作 ---------------- */

async function boot() {
  paint(['rail', 'folders', 'list', 'reader']);
  /* 先起实时通道再去拉数据：万一服务还没起来（刚开机 / 刚重启），
     bootstrap 会失败，但 fallbackCheck 每 30 秒会自己重试一遍 ——
     不这么做的话，页面会一直停在「连不上邮箱」，用户只能手动刷新。 */
  startRealtime();
  try {
    const r = await api.bootstrap();
    if (!r.ok) throw new Error(r.error || '未知错误');
    setClassifyRules(r.classify);   // 分级规则由 config.json 决定，没配就用内置默认
    store.set({ user: r.user, folders: r.folders || [], indexCount: r.index_count || 0 });
  } catch (e) {
    store.set({ listError: '连不上邮箱：' + e.message });
    toast('启动失败：' + e.message + '（每 30 秒自动重试）', 'err');
    return;
  }
  await loadFolder('INBOX');
}

async function loadFolder(raw) {
  keepListScroll = false;            // 换文件夹 = 内容整体换掉，回到顶部
  store.set({ loadingList: true, listError: '', selectedId: null, detail: null,
              source: 'imap', checked: new Set(), loadedFolder: raw,
              loadingMore: false, listHasMore: false });
  try {
    const r = await api.messages(raw, 60);
    if (!r.ok) throw new Error(r.error || '拉取失败');
    store.set({ messages: r.messages || [], loadingList: false, source: 'imap',
                listHasMore: !!r.hasMore, loadingMore: false });
    syncWatchFolder();               // 让服务端 IDLE 跟到这个文件夹
  } catch (e) {
    store.set({ messages: [], loadingList: false, listError: '加载失败：' + e.message });
  }
}

/* ---------------- 实时同步（IMAP IDLE → SSE） ----------------

   以前页面只在打开时拉一次数据，之后就再也不问了。后果是双向都不同步：
     · Foxmail / Zimbra 收到新邮件 → 这里看不到（除非手动点刷新）
     · 在别的客户端删掉 / 标已读 → 这里还堂而皇之地留着那封「已经不存在的邮件」，
       点开会报错，看着像我们删不掉

   现在服务端有一条专用的只读 IMAP 连接 IDLE 在当前文件夹上（服务器支持，
   实测能收到 EXISTS / EXPUNGE / FETCH FLAGS），一有变化就通过这条 SSE 推过来。
   收到之后**做 diff 而不是整体重拉**：新增的插到最前、消失的移除、标记变了就地更新，
   顺带保住滚动位置和正在看的那封。

   兜底：EventSource 在某些环境下会被代理/浏览器掐掉，所以另有两条保险 ——
     1. EventSource 自己断线会重连（浏览器行为）
     2. 每 30 秒调一次极轻的 /api/check（只回数量/uidNext）对账
   通道状态显示在工具栏上（live / poll / down），免得「没邮件」和「通道坏了」分不清。 */

const RT = { es: null, connected: false, timer: null, seen: null, lastAt: 0 };

const idOf = (m) => String(m.uid || m.id || '');
/* 一封邮件「长什么样」的指纹：uid + 已读 + 重要 + 主题。
   用它比对整批邮件，没变就一个 DOM 都不碰 —— 否则每次心跳都会重建列表。 */
const sigOf = (m) => [idOf(m), m.seen ? 1 : 0, m.flagged ? 1 : 0, m.subject || ''].join('|');
const sigOfAll = (arr) => (arr || []).map(sigOf).join(';');

function startRealtime() {
  syncWatchFolder();
  // 兜底对账：不管 SSE 通不通都跑，便宜（只回 3 个数字）
  if (RT.timer) clearInterval(RT.timer);
  RT.timer = setInterval(fallbackCheck, 30000);
}

/* 换了文件夹：重连 SSE，让服务端的 IDLE 监听跟过来。
   （SSE 连接上带的 folder 参数就是服务端切换监听的开关） */
function syncWatchFolder() {
  if (RT.es) {
    try { RT.es.close(); } catch (_) { /* 忽略 */ }
    RT.es = null;
  }
  RT.seen = null;                  // 换文件夹了，兜底对账的基准要重新记
  if (window.EventSource) connectSSE();
}

function connectSSE() {
  if (!window.EventSource) {
    store.set({ liveStatus: 'poll' });
    return;
  }
  const folder = store.get().loadedFolder || 'INBOX';
  try {
    const es = new EventSource('/api/events?folder=' + encodeURIComponent(folder));
    RT.es = es;
    es.onopen = () => {
      RT.connected = true;
      store.set({ liveStatus: 'live' });
    };
    es.onmessage = (ev) => {
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (!msg || msg.type === 'hello') return;
      if (msg.type === 'change' || msg.type === 'resync') scheduleRefresh(msg);
    };
    es.onerror = () => {
      RT.connected = false;
      // EventSource 会自己重连；这期间靠 fallbackCheck 兜着
      store.set({ liveStatus: 'poll' });
    };
  } catch (_) {
    store.set({ liveStatus: 'poll' });
  }
}

/* 事件可能连着来（一次收 5 封就是 5 个 EXISTS），攒 400ms 合成一次刷新 */
function scheduleRefresh(msg) {
  RT.lastAt = Date.now();
  clearTimeout(RT.pending);
  RT.pending = setTimeout(() => refreshCurrentFolder(msg), 400);
}

async function refreshCurrentFolder(msg) {
  const s = store.get();
  if (s.view !== 'folder' || s.loadingList) return;   // 搜索/标签视图不动
  const folder = s.loadedFolder;
  const cur = s.messages || [];
  /* 想拉多少：至少盖住当前显示的这批（上限 300，别把服务器拉爆）。
     一定要拉满「已显示的条数」，否则比不出来的旧邮件会被误判成「被删了」。 */
  const want = Math.max(60, Math.min(cur.length || 0, 300));
  let r;
  try {
    r = await api.messages(folder, want);
  } catch (_) {
    return;                                            // 网络抖一下，下轮再来
  }
  if (!r.ok) return;
  if (store.get().loadedFolder !== folder) return;     // 期间用户换文件夹了，作废

  const fresh = r.messages || [];
  const freshIds = new Set(fresh.map(idOf));
  const uids = fresh.map((m) => parseInt(m.uid, 10)).filter((n) => !Number.isNaN(n));
  const minFresh = uids.length ? Math.min(...uids) : Infinity;

  const curIds = new Set(cur.map(idOf));
  const added = fresh.filter((m) => !curIds.has(idOf(m)));
  /* 只在「拉到的范围内」判定消失 —— uid 是递增的，比 minFresh 更小的
     那些是更早的邮件，这次根本没拉，不能当成被删了。 */
  const removed = cur.filter((m) => {
    const u = parseInt(m.uid, 10);
    return !Number.isNaN(u) && u >= minFresh && !freshIds.has(idOf(m));
  });

  const tail = cur.filter((m) => {
    const u = parseInt(m.uid, 10);
    return Number.isNaN(u) || u < minFresh;
  });
  const next = fresh.concat(tail);

  const flagOnly = !added.length && !removed.length
    && sigOfAll(next) !== sigOfAll(cur);
  if (!added.length && !removed.length && !flagOnly) return;   // 没变，一个 DOM 都不碰

  /* 保住正在看的那封：新邮件是插在上面的，直接重绘会让内容整体往下跳。
     量一下重绘前后的高度差，把滚动位置补回去 —— 用户眼里就是「上面的邮件
     悄悄多了几封，我正在看的这封还在原处」。停在第 0 屏（scrollTop≈0）时
     不补，让他直接看到新邮件。 */
  const sc = el.list.querySelector('.list-scroll');
  const prevTop = sc ? sc.scrollTop : 0;
  const prevH = sc ? sc.scrollHeight : 0;

  store.set({ messages: next, listHasMore: !!r.hasMore, liveAt: timeNow() });

  const sc2 = el.list.querySelector('.list-scroll');
  if (sc2 && prevTop > 4 && sc2.scrollHeight !== prevH) {
    sc2.scrollTop = prevTop + (sc2.scrollHeight - prevH);
  }

  if (added.length) toast('收到 ' + added.length + ' 封新邮件');
  if (removed.length) toast('有 ' + removed.length + ' 封已在别处被删除或移走', 'warn');
  void flagOnly; void msg;
}

/* SSE 不通时的兜底：每 30 秒问一次「这文件夹现在多少封 / 下一个 uid 是几」，
   和上次记下的比，变了就做一次 diff 刷新。

   顺带当**自愈**用：服务重启、机器刚开机那会儿页面打不开邮箱，列表会是空的 +
   一条错误；页面自己不会重试，用户只能手动刷新。这里发现「有错且一封都没有」
   就重新走一遍 bootstrap + loadFolder。 */
async function fallbackCheck() {
  const s = store.get();
  if (!s.folders.length || (s.listError && !(s.messages || []).length)) {
    try {
      const b = await api.bootstrap();
      if (b.ok) {
        setClassifyRules(b.classify);
        store.set({ user: b.user, folders: b.folders || [],
                    indexCount: b.index_count || 0, listError: '' });
        await loadFolder(s.loadedFolder || 'INBOX');
        toast('已重新连上邮箱服务', 'ok');
      }
    } catch (_) { /* 下一次再试 */ }
    return;
  }
  if (RT.connected) return;                         // SSE 正常，不需要它对账
  if (s.view !== 'folder' || !s.loadedFolder) return;
  let r;
  try {
    r = await api.check(s.loadedFolder);
  } catch (_) {
    store.set({ liveStatus: 'down' });
    return;
  }
  if (!r.ok) { store.set({ liveStatus: 'down' }); return; }
  store.set({ liveStatus: 'poll' });
  const now = r.messages + '/' + r.uidNext;
  if (RT.seen === now) return;                      // 没变化
  const first = RT.seen === null;
  RT.seen = now;
  if (!first) refreshCurrentFolder({ type: 'poll' });
}

function timeNow() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/* 滚到底再拉一批（无限滚动）

   用**最小 uid 当游标**而不是「第几页」：翻页期间新邮件插到列表前面时，
   页码会整体错位（重复或漏掉），uid 游标不会。

   两个防护：
     1. 去重 —— 万一服务端把同一封又给回来，不会在列表里出现两份。
     2. 一批拉回来 0 条新增就停 —— 否则某些邮件取不下来时会无限重试。 */
async function loadMore() {
  const s = store.get();
  if (s.loadingList || s.loadingMore || !s.listHasMore) return;
  if (s.view === 'search') return;                // 搜索结果一次给全

  const uids = (s.messages || [])
    .map((m) => parseInt(m.uid, 10))
    .filter((n) => !Number.isNaN(n));
  if (!uids.length) return;

  store.set({ loadingMore: true });
  try {
    const r = await api.messages(s.loadedFolder, 60, Math.min(...uids));
    if (!r.ok) throw new Error(r.error || '加载失败');
    const cur = store.get().messages;
    const seen = new Set(cur.map((x) => String(x.uid || x.id)));
    const add = (r.messages || []).filter((x) => !seen.has(String(x.uid || x.id)));
    store.set({
      messages: cur.concat(add),
      listHasMore: !!r.hasMore && add.length > 0,
      loadingMore: false,
    });
    /* 这一批里可能全是「未读」筛选下看不见的邮件，显示高度没变、
       于是又停在底部 —— 这时应该继续拉，否则用户会觉得卡住了。
       上面 listHasMore 的 `add.length > 0` 已经挡住了空转。 */
    requestAnimationFrame(maybeLoadMore);
  } catch (e) {
    store.set({ loadingMore: false });
    toast('加载更多失败：' + e.message, 'err');
  }
}

/* ---- 一键回到顶部 ----

   列表能一次装 60 封、还能无限往下续，滚到几百封之后再想回开头，
   靠滚轮要滚很久。所以滚过一屏就浮出这个按钮。

   按钮每次列表重绘都会跟着重建（它在 el.list 里面），所以显隐不能只靠
   DOM 状态 —— paintList 之后要按当前的 scrollTop 重新算一次，
   否则「重绘一次按钮就消失」。 */
function syncToTop() {
  const sc = el.list.querySelector('.list-scroll');
  const btn = el.list.querySelector('.to-top');
  if (!sc || !btn) return;
  btn.classList.toggle('is-show', sc.scrollTop > TO_TOP_AFTER);
}

const TO_TOP_AFTER = 600;   // 滚过 600px（约一屏多）才显示

/* 距底部 400px 以内就提前拉下一批 —— 等真的滚到底再拉，用户会看到明显卡顿。 */
function maybeLoadMore() {
  const sc = el.list.querySelector('.list-scroll');
  if (!sc) return;
  if (sc.scrollTop + sc.clientHeight < sc.scrollHeight - 400) return;
  loadMore();
}

/* scroll 事件不冒泡，所以用**捕获阶段**挂在 document 上一次搞定 ——
   列表每次重绘都会换掉内部的滚动容器，逐个绑定会漏。 */
document.addEventListener('scroll', (ev) => {
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains('list-scroll')) return;
  maybeLoadMore();
  syncToTop();
}, true);

/* 标签视图 —— 只是**当前文件夹已加载的这批邮件**的一个筛选视图，不额外发请求。

   试过做成全库筛选（扫 Foxmail 索引 6600+ 封，V12），但索引里没有 uid：
   筛出来的邮件只能看，不能标已读 / 标重要 / 删除，而这几项恰恰是收件箱里
   最常用的操作。权衡下来，宁可只筛当前文件夹 —— 换来的是每一封都能操作。

   想看更多：往下滚会自动续拉（loadMore），标签跟着一起筛。 */
async function loadTag(tagId) {
  keepListScroll = false;                  // 换标签 = 换一批内容，回到顶部
  const s = store.get();
  store.set({
    view: 'tag', activeFolder: tagId,
    selectedId: null, detail: null, checked: new Set(),
  });
  // 上一个是搜索结果（本地索引，没有 uid）或还没加载过：回文件夹重新拉
  if (s.source === 'index' || !s.messages.length) {
    await loadFolder(s.loadedFolder || 'INBOX');
  }
}

async function selectMail(id) {
  const s = store.get();
  const m = s.messages.find((x) => (x.uid || x.id) === id);
  if (!m) return;
  store.set({ selectedId: id, detail: null, loadingBody: false });
  if (!m.uid) return;                      // 本地索引结果，没有正文

  /* 打开即已读 —— 标准邮件客户端的行为。
     乐观更新：先改本地状态（点下去立刻见效），请求后台发，失败再回滚。
     刻意不重拉列表 —— 那会把选中项和滚动位置一起冲掉，读到一半的邮件会跳回顶部。 */
  if (m.seen === false) {
    applySeen(m, true);
    api.flags(m.folder, [m.uid], 'read')
      .then((r) => { if (!r || !r.ok) applySeen(m, false); })
      .catch(() => applySeen(m, false));
  }

  store.set({ loadingBody: true });
  try {
    const r = await api.message(m.folder, m.uid);
    store.set({ detail: r.ok ? r : null, loadingBody: false });
  } catch (e) {
    store.set({ loadingBody: false, detail: { body: '取正文失败：' + e.message } });
  }
}

/* 只改本地状态：把某封标记为已读/未读，并同步所在文件夹的未读计数。
   不走 loadFolder —— 标记一封邮件不值得重新拉整个列表。 */
function applySeen(m, seen) {
  const s = store.get();
  const uid = String(m.uid);
  store.set({
    messages: (s.messages || []).map((x) =>
      (String(x.uid || x.id) === uid ? { ...x, seen } : x)),
    folders: (s.folders || []).map((f) =>
      (f.raw === m.folder
        ? { ...f, unseen: Math.max(0, (f.unseen || 0) + (seen ? -1 : 1)) }
        : f)),
  });
}

/* 只改本地状态：改掉某封的「重要」标记。
   跟 applySeen 同一个思路 —— 乐观更新（点下去立刻变红）、不重拉列表。
   这里不动 folders 的计数：重要与否不影响未读数。 */
function applyFlagged(m, flagged) {
  const s = store.get();
  const uid = String(m.uid);
  store.set({
    messages: (s.messages || []).map((x) =>
      (String(x.uid || x.id) === uid ? { ...x, flagged } : x)),
  });
}

/* ---------------- 「已移到已删除邮件」的撤销 ----------------

   删除是唯一会动用户数据的手动操作，所以给一次反悔的机会。

   两个刻意的取舍：
     1. 存活 20 秒（比轻提示的 2.6 秒长得多）—— 点错了得有足够时间反应，
        代价只是页面上多一条不碍事的窄条。
     2. 只记**最近一次**。再往前的邮件可能已经被别处（Foxmail / 网页邮箱）
        动过，凭旧编号去搬有搬错的风险，所以宁可不提供。 */

const UNDO_MS = 20000;
let undoTimer = null;

function armUndo(folder, trash, items) {
  if (undoTimer) clearTimeout(undoTimer);
  store.set({ undoTrash: { folder, trash, items, count: items.length } });
  undoTimer = setTimeout(() => {
    store.set({ undoTrash: null });
    undoTimer = null;
  }, UNDO_MS);
}

function disarmUndo() {
  if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
  store.set({ undoTrash: null });
}

async function undoTrash() {
  const u = store.get().undoTrash;
  if (!u) return;
  try {
    const r = await api.untash({ folder: u.folder, trash: u.trash, items: u.items });
    if (!r.ok) throw new Error(r.error || '撤销失败');
    disarmUndo();
    toast(`已撤销，${r.restored} 封邮件放回了「${u.folder}」`
      + (r.note ? '（' + r.note + '）' : ''), 'ok');
    await loadFolder(u.folder);
  } catch (e) {
    toast('撤销失败：' + e.message, 'err');
  }
}

/* 移到「已删除邮件」—— 整个应用里唯一的破坏性操作，所以刻意做得啰嗦：
     1. 一定弹二次确认（跟「发送」同一个做法）
     2. 文案只说「移到已删除邮件」，因为它确实还能捞回来
     3. 后端还会再验一次 confirm，并且**先复制成功才标记删除**，双保险
   注意：界面上叫「已删除邮件」，服务器上那个文件夹的 IMAP 原名是 Trash。 */
async function trashMails(folder, uids, label) {
  const n = uids.length;
  const txt = String(label || '');
  const ok = window.confirm(
    `把${n > 1 ? n + ' 封' : '这封'}邮件移到「已删除邮件」？\n\n` +
    `　${txt.slice(0, 40)}${txt.length > 40 ? '…' : ''}\n\n` +
    '不是彻底删除 —— 之后还能去「已删除邮件」里找回来。'
  );
  if (!ok) return;
  try {
    const r = await api.trash(folder, uids);
    if (!r.ok) throw new Error(r.error || '操作失败');
    const moved = (r.moved || []).length;
    const failed = (r.failed || []).length;
    toast(`已移到「已删除邮件」${moved} 封` + (failed ? `，${failed} 封失败` : ''),
      failed ? 'err' : 'ok');
    /* 服务端 EXPUNGE 失败要单独说：邮件被复制到了已删除邮件、但**原件还留在原文件夹**
       （只带了个删除标记），我们自己的列表刷新后就看不见了，
       可 Foxmail / Zimbra 不会自动清这种邮件 —— 那边照样显示。
       不说清楚，用户会以为是「我们删了、别人没同步」。 */
    if (r.expunged === false) {
      toast('注意：有邮件只被标记删除、没能从原文件夹彻底移除（' + (r.expungeError || '未知原因')
        + '）。Foxmail / Zimbra 那边可能还看得到，手动删一下即可', 'warn', 12000);
    }
    actions.clearChecks();
    // 记下这次移动，列表顶部就会出现一条可撤销的提示
    if (moved) armUndo(folder, r.trash || 'Trash', r.items || []);
    await loadFolder(folder);
  } catch (e) {
    toast('移到「已删除邮件」失败：' + e.message, 'err');
  }
}

async function runSearch(q) {
  if (!q.trim()) return;
  keepListScroll = false;            // 搜索出的是另一批邮件，回到顶部
  store.set({ loadingList: true, view: 'search', query: q, selectedId: null, detail: null, checked: new Set() });
  try {
    const r = await api.search(q, 50);
    // 本地索引结果一次给全，不做滚到底加载
    store.set({ messages: r.messages || [], source: 'index', loadingList: false,
                listHasMore: false, loadingMore: false });
  } catch (e) {
    store.set({ messages: [], loadingList: false, listError: '搜索失败：' + e.message });
  }
}

/* ---------------- 事件 ---------------- */

document.addEventListener('click', async (ev) => {
  const t = ev.target;

  // 复选框不冒泡成选中
  const chk = t.closest('[data-check]');
  if (chk) {
    ev.stopPropagation();
    actions.toggleCheck(chk.dataset.check);
    return;
  }

  const folderBtn = t.closest('[data-folder]');
  if (folderBtn) {
    actions.setActiveFolder(folderBtn.dataset.folder);
    await loadFolder(folderBtn.dataset.folder);
    return;
  }

  const tagBtn = t.closest('[data-tag]');
  if (tagBtn) { await loadTag(tagBtn.dataset.tag); return; }

  const filterBtn = t.closest('[data-filter]');
  if (filterBtn) {
    keepListScroll = false;          // 筛选条件变了，邮件条目随之变化
    actions.setFilter(filterBtn.dataset.filter);
    return;
  }

  // 附件：点一下就把文件取到本地（必要时打开）
  const attBtn = t.closest('[data-attach]');
  if (attBtn) { await openAttachment(attBtn.dataset.attach); return; }

  // 主导航条：Mails 就是当前空间；其余空间尚未接入，
  // 明确给一句提示，避免点了毫无反应。
  const navBtn = t.closest('[data-nav]');
  if (navBtn) {
    if (navBtn.dataset.nav !== 'mail') toast('「' + navBtn.textContent.trim().split(/\s+/)[0] + '」空间还没接进来');
    return;
  }

  const item = t.closest('.mail-item');
  if (item && !t.closest('[data-stop]')) { await selectMail(item.dataset.id); return; }

  const act = t.closest('[data-action]');
  if (!act) {
    // 点在了「看起来能点」的元素上，却没有绑任何操作。
    // 明确说出来 —— 不要再让用户面对「点了没反应」这种无从排查的沉默。
    if (t.closest('button, .tree-item, .chip, .rail-item, .rail-space')) toast('这个位置还没有绑定操作');
    return;
  }
  await handleAction(act.dataset.action);
});

async function openAttachment(i) {
  const s = store.get();
  const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
  if (!m) return;
  if (!m.uid) return toast('本地索引结果没有附件，先点「联网取正文」', 'err');

  const chips = document.querySelectorAll('[data-attach]');
  chips.forEach((c) => { c.disabled = true; });
  toast('正在取附件…');
  try {
    const r = await api.attachment({
      folder: m.folder || s.activeFolder,
      uid: m.uid,
      index: Number(i),
    });
    if (!r.ok) throw new Error(r.error || '取附件失败');
    toast(`${r.name}｜${r.note}`, r.opened ? 'ok' : '');
  } catch (e) {
    toast('取附件失败：' + e.message, 'err');
  } finally {
    chips.forEach((c) => { c.disabled = false; });
  }
}

async function handleAction(a) {
  const s = store.get();

  if (a === 'compose') { store.set({ composerOpen: true, composerPrefill: null }, ['composer']); return; }
  if (a === 'close-composer') { store.set({ composerOpen: false }, ['composer']); return; }

  if (a === 'save-draft') {
    const to = document.getElementById('cTo').value.trim();
    const subject = document.getElementById('cSubject').value.trim();
    const body = document.getElementById('cBody').value;
    if (!to) return toast('请填收件人', 'err');
    try {
      const r = await api.draft(to, subject, body);
      if (!r.ok) throw new Error(r.error);
      store.set({ composerOpen: false }, ['composer']);
      toast('已写入草稿箱，去 Foxmail 点发送', 'ok');
    } catch (e) { toast('写入失败：' + e.message, 'err'); }
    return;
  }

  if (a === 'load-draft') { await loadDraft(); return; }

  if (a === 'to-top') {
    const sc = el.list.querySelector('.list-scroll');
    if (sc) sc.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 窄屏两级导航：从阅读态返回列表
  if (a === 'back-to-list') {
    store.set({ selectedId: null, detail: null });
    return;
  }

  if (a === 'send') {
    // 不可撤销：先取内容 → 弹二次确认 → 带 confirm 才交给后端
    const to = document.getElementById('cTo').value.trim();
    const subject = document.getElementById('cSubject').value.trim();
    const body = document.getElementById('cBody').value;
    if (!to) return toast('请填收件人', 'err');
    if (!body.trim()) return toast('正文为空', 'err');

    const ok = window.confirm(
      '确认发送这封邮件？\n\n' +
      '收件人：' + to + '\n' +
      '主　题：' + (subject || '(无主题)') + '\n\n' +
      '发出后无法撤回。'
    );
    if (!ok) return;

    try {
      const r = await api.send({ to, subject, body, confirm: true });
      if (!r.ok) throw new Error(r.error || '发送失败');
      store.set({ composerOpen: false }, ['composer']);
      /* 副本有没有留成功要分开说 —— 信已经出去了，副本没留下是两个不同的问题，
         混成一句「已发送」会让用户过半天才发现「已发送邮件」里是空的。 */
      if (r.savedToSent === false) {
        toast('邮件已发出，但没能存进「' + (r.sentFolder || 'Sent') + '」：'
          + (r.sentError || '未知原因'), 'warn', 8000);
      } else {
        toast('已发送 → ' + (r.to || []).join('、')
          + (r.savedToSent ? '（已存入「' + (r.sentFolder || 'Sent') + '」）' : ''), 'ok');
      }
      // 正在看「已发送」就顺手刷新，不然刚发的信看不见
      const st = store.get();
      if (st.loadedFolder === (r.sentFolder || 'Sent')) loadFolder(st.loadedFolder);
    } catch (e) {
      toast('发送失败：' + e.message + '（已保留内容，可先存草稿）', 'err');
    }
    return;
  }

  if (a === 'mark-read' || a === 'mark-unread') {
    if (!s.checked.size) return toast('先勾选邮件');
    const r = await api.flags(s.activeFolder, [...s.checked], a === 'mark-read' ? 'read' : 'unread');
    toast(r.ok ? `已标记 ${r.updated} 封` : (r.error || '失败'), r.ok ? 'ok' : 'err');
    actions.clearChecks();
    await loadFolder(s.activeFolder);
    return;
  }

  if (a === 'mark-important') {
    if (!s.checked.size) return toast('先勾选邮件');
    const r = await api.flags(s.activeFolder, [...s.checked], 'important');
    toast(r.ok ? `已把 ${r.updated} 封标为重要` : (r.error || '失败'), r.ok ? 'ok' : 'err');
    actions.clearChecks();
    await loadFolder(s.activeFolder);
    return;
  }

  if (a === 'undo-trash') { await undoTrash(); return; }
  if (a === 'dismiss-undo') { disarmUndo(); return; }

  if (a === 'trash') {
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    if (!m || !m.uid) return toast('这封没有可操作的服务端邮件', 'err');
    await trashMails(m.folder, [m.uid], m.subject || '(无主题)');
    return;
  }

  if (a === 'trash-checked') {
    if (!s.checked.size) return toast('先勾选邮件');
    await trashMails(s.activeFolder, [...s.checked], `已勾选的 ${s.checked.size} 封`);
    return;
  }

  if (a === 'check-all') {
    actions.checkAll(s.messages.map((m) => m.uid || m.id));
    return;
  }
  if (a === 'uncheck-all') { actions.clearChecks(); return; }

  if (a === 'refresh') {
    await loadFolder(s.activeFolder);
    try {
      const r = await api.bootstrap();
      if (r.ok) store.set({ folders: r.folders || [] });
    } catch (_) {}
    toast('已刷新');
    return;
  }

  if (a === 'exit-search') {
    store.set({ view: 'folder', query: '' });
    await loadFolder(store.get().activeFolder);
    return;
  }

  if (a === 'ai-reply') {
    // 交给 WorkBuddy 起草：只把它登记进队列，不在这里等 AI。
    // 实测 WorkBuddy CLI 冷启动 2 分半没返回，同步等是不可行的。
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    if (!m) return;
    const uid = String(m.uid || '');
    /* 本地不再一棍子挡住：同一封重复点，服务端会走 promote（提到队首、按加急处理），
       不会新建第二份。挡住的话，失败的那封就再也没法「再点一次」了。 */
    try {
      const r = await api.replyRequest({
        folder: m.folder || s.activeFolder,
        uid,
        subject: m.subject || '',
        from: m.from || '',
        date: m.date || '',
        body: s.detail && s.detail.body ? s.detail.body : '',
      });
      if (!r.ok) throw new Error(r.error || '提交失败');

      const already = (s.pendingIds || []).map(String).includes(uid);
      if (r.duplicate) {
        toast(r.promoted
          ? '这封已在队列 —— 已提到最前面，按加急处理'
          : '这封已经在队列里了，WorkBuddy 只会起草一次', 'warn');
      } else {
        /* 立刻把计数和按钮状态改对，别等下一轮轮询 ——
           否则用户点完会看到数字半天不动，以为没点上，于是再点一次
           （这正是「误点 2 次 3 次」的来源）。 */
        store.set({
          replyPending: (s.replyPending || 0) + 1,
          pendingIds: (s.pendingIds || []).concat(uid ? [uid] : []),
          awaitingUids: (s.awaitingUids || []).concat(uid ? [uid] : []),
          pendingMeta: (s.pendingMeta || []).concat(uid ? [{
            id: r.id, uid, status: 'queued', source: 'manual', attempts: 0, last_error: '',
          }] : []),
        });
        toast('已加急：已插到队首，通常 30 分钟内出草稿（按钮上会显示已等多久）');
      }
      if (already && r.promoted) {
        // 按钮要立刻从「已入队」变成「已加急」
        store.set({
          pendingMeta: (store.get().pendingMeta || []).map(
            (x) => (String(x.uid) === uid ? { ...x, source: 'manual', status: 'queued' } : x)),
        });
      }
      pollReplyQueue();
    } catch (e) {
      toast('提交失败：' + e.message, 'err');
    }
    return;
  }

  /* 撤回：手滑点错、或者改主意了，把还没起草的请求从队列里拿掉。
     只删 pending，已经起草好的草稿不受影响。 */
  if (a === 'cancel-reply') {
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    if (!m) return;
    const uid = String(m.uid || '');
    try {
      const r = await api.replyCancel({
        folder: m.folder || s.activeFolder,
        uid,
        subject: m.subject || '',
        from: m.from || '',
      });
      if (!r.ok || !r.removed) throw new Error(r.error || '队列里没找到这封');
      store.set({
        replyPending: Math.max(0, (s.replyPending || 0) - 1),
        pendingIds: (s.pendingIds || []).map(String).filter((x) => x !== uid),
        awaitingUids: (s.awaitingUids || []).map(String).filter((x) => x !== uid),
      });
      toast('已从队列撤回，WorkBuddy 不会起草这封了', 'ok');
      pollReplyQueue();
    } catch (e) {
      toast('撤回失败：' + e.message, 'err');
    }
    return;
  }

  /* 重试：这条上次起草失败了（AI 侧会写失败原因），点一下重新排队。
     不新建记录 —— 还是原来那条 id，attempts 保留，看得出重试过几次。 */
  if (a === 'retry-reply') {
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    if (!m) return;
    const uid = String(m.uid || '');
    const meta = (s.pendingMeta || []).find((x) => String(x.uid) === uid);
    if (!meta) return;
    try {
      const r = await api.replyRetry({ id: meta.id });
      if (!r.ok || !r.retried) throw new Error(r.error || '重试失败');
      store.set({
        pendingMeta: (s.pendingMeta || []).map(
          (x) => (String(x.uid) === uid ? { ...x, status: 'queued', source: 'manual', last_error: '' } : x)),
        failedCount: Math.max(0, (s.failedCount || 0) - 1),
      });
      toast('已重新排队，这次按加急处理', 'ok');
      pollReplyQueue();
    } catch (e) {
      toast('重试失败：' + e.message, 'err');
    }
    return;
  }

  if (a === 'reply' || a === 'draft-from') {
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    const p = splitFrom(m?.from);
    const subject = (m?.subject || '').replace(/^Re:\s*/i, '');
    store.set({
      composerOpen: true,
      composerPrefill: {
        to: p.addr || '',
        subject: a === 'reply' ? 'Re: ' + subject : subject,
        body: a === 'reply' && s.detail?.body
          ? '\n\n----- 原邮件 -----\n' + s.detail.body.slice(0, 1500)
          : '',
      },
    }, ['composer']);
    return;
  }

  /* 标重要 / 取消重要 —— 写的是服务器上的 \Flagged 标记，
     所以 Foxmail 那边也会出现同一个星标（跟「已读」同理）。
     乐观更新：先改本地（点下去立刻变红），失败了再回滚。 */
  if (a === 'toggle-important') {
    const m = s.messages.find((x) => (x.uid || x.id) === s.selectedId);
    if (!m || !m.uid) return toast('这封没有可操作的服务端邮件', 'err');
    const next = !m.flagged;
    applyFlagged(m, next);
    try {
      const r = await api.flags(m.folder, [m.uid], next ? 'important' : 'unimportant');
      if (!r.ok) throw new Error(r.error || '标记失败');
      toast(next ? '已标为重要，左侧「★ 重点」里能找到它' : '已取消重要', 'ok');
    } catch (e) {
      applyFlagged(m, !next);
      toast('标记失败：' + e.message, 'err');
    }
    return;
  }

  if (a === 'find-body') {
    const m = s.messages.find((x) => (x.id || x.uid) === s.selectedId);
    if (!m) return;
    store.set({ loadingBody: true });
    try {
      const r = await api.find(m.subject);
      store.set({ loadingBody: false, detail: r.ok ? { body: r.body || '（服务器上没找到正文）' } : { body: r.error || '失败' } });
    } catch (e) { store.set({ loadingBody: false, detail: { body: '失败：' + e.message } }); }
    return;
  }
}

/* ---------------- 回复起草队列：只提示，不抢 ---------------- */

let pollBusy = false;

async function pollReplyQueue() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const r = await api.replyQueue();
    if (!r.ok) return;
    const s = store.get();
    const doneN = r.doneCount || 0;
    const pids = (r.pendingIds || []).map(String);
    const dmeta = r.donePreview || [];

    /* 只在**真的变了**的时候才 set。
       这些字段里有数组，每次请求都是新引用，直接 set 会让列表每 5 秒
       重建一次 DOM —— 既浪费，也会把正在滚动的用户打断（滚动位置现在
       靠 paintList 兜住了，但没必要让它每 5 秒忙一次）。 */
    /* 队列里已经不存在的 uid，不该再留在「等回传」名单里
       （比如 WorkBuddy 处理完了、或者用户在别处撤回了）。 */
    const awaiting = (s.awaitingUids || []).map(String)
      .filter((u) => pids.includes(u) || dmeta.some((d) => String(d.uid) === u));

    const sameIds = (a, b) => JSON.stringify(a || []) === JSON.stringify(b || []);
    const pidsChanged = !sameIds(
      (s.pendingIds || []).map(String),
      pids.map(String),
    );
    /* 每条的状态（待生成 / 生成中 / 失败 + 手动自动）也要跟着变 ——
       按钮四态靠它。同样要先比一次，避免每 5 秒重建阅读窗格。 */
    const pmeta = r.pendingMeta || [];
    /* 比较时要把 ageSec 粗粒度化（按 30 秒取整）—— 否则它每 5 秒都在变，
       metaChanged 永远为真，阅读窗格每 5 秒重建一次，正文选中的文字会被打断。
       状态、来源、失败原因、卡住标记这些还是一变就更新。 */
    const metaKey = (arr) => (arr || []).map((x) => [
      x.id, x.status, x.source, x.attempts, x.last_error, x.stalled ? 1 : 0,
      Math.floor((Number(x.ageSec) || 0) / 30),
    ].join('|')).join(';');
    const metaChanged = metaKey(s.pendingMeta || []) !== metaKey(pmeta);
    const doneChanged = !sameIds(
      (s.doneMeta || []).map((d) => String(d.id)),
      dmeta.map((d) => String(d.id)),
    );
    const awaitingChanged = !sameIds((s.awaitingUids || []).map(String), awaiting);

    if (s.replyPending === (r.pending || 0)
        && s.replyDone === doneN
        && s.failedCount === (r.failed || 0)
        && s.stalledCount === (r.stalled || 0)
        && !pidsChanged && !doneChanged && !awaitingChanged && !metaChanged) return;

    const patch = {
      replyPending: r.pending || 0,
      replyDone: doneN,
      failedCount: r.failed || 0,
      stalledCount: r.stalled || 0,
      pendingIds: pids,
      doneMeta: dmeta,
    };
    if (metaChanged) patch.pendingMeta = pmeta;
    if (awaitingChanged) patch.awaitingUids = awaiting;

    store.set(patch);

    /* 自动回传：用户**亲手点过**「让 WorkBuddy 起草」的那封，草稿一好就填进编辑器。
       只对这些邮件自动抢 —— 每小时自动处理攒下来的草稿仍然只提示、不打断，
       不然你正在写别的邮件时会被硬生生切走。 */
    const hit = dmeta.find((d) => String(d.uid) && awaiting.includes(String(d.uid)));
    if (hit && !s.composerOpen) await autoFillDraft(hit);

    /* 注意：这里**只更新数字、不取走草稿**。
       早期版本是「谁轮询到谁消费」—— 面板里如果同时开着多个页面实例
       （旧页面没关、或者开了两个预览），草稿会被那个看不见的实例吃掉，
       用户那边就永远等不到，还会以为没生成。
       现在必须由用户点「点开修改」、或上面的自动回传触发，草稿躺在磁盘上不会丢。 */
  } catch (_) {
    /* 服务器偶尔不在，忽略，下一轮再试 */
  } finally {
    pollBusy = false;
  }
}

/* 把某封已就绪的草稿直接填进编辑器（自动回传用） */
async function autoFillDraft(meta) {
  const s = store.get();
  if (s.composerOpen) return;                 // 编辑器占着，不抢
  try {
    const r = await api.replyDraft(meta.id);  // 指定 id，不会错拿成别的邮件
    if (!r.ok) return;
    const d = r.draft || {};
    store.set({
      composerOpen: true,
      composerPrefill: { to: d.to || '', subject: d.subject || '', body: d.body || '' },
      replyDone: r.doneCount || 0,
      awaitingUids: (s.awaitingUids || []).map(String)
        .filter((u) => u !== String(meta.uid)),
    });
    toast('WorkBuddy 起草好了，已自动填进编辑器', 'ok');
  } catch (_) { /* 下一轮再试 */ }
}

async function loadDraft() {
  const s = store.get();
  if (s.composerOpen) return toast('先把编辑器里这封处理完');
  try {
    /* 优先取「我亲手点过起草」的那封（如果有），否则取队列里第一封。
       这样点「点开修改」拿到的多半正是你刚才盯着的那封。 */
    const mine = (s.doneMeta || []).find(
      (d) => String(d.uid) && (s.awaitingUids || []).map(String).includes(String(d.uid)));
    const r = await api.replyDraft(mine ? mine.id : '');
    if (!r.ok) { toast(r.error || '暂时没有待处理的草稿'); return pollReplyQueue(); }
    const d = r.draft || {};
    store.set({
      composerOpen: true,
      composerPrefill: { to: d.to || '', subject: d.subject || '', body: d.body || '' },
      replyDone: r.doneCount || 0,
      awaitingUids: (s.awaitingUids || []).map(String)
        .filter((u) => u !== String(r.uid || '')),
    });
    toast('WorkBuddy 草稿已填入，改完可直接发送');
  } catch (e) {
    toast('取草稿失败：' + e.message, 'err');
  }
}

setInterval(pollReplyQueue, 5000);

/* 搜索：回车触发，不每次输入都重渲染（否则输入框失焦） */
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && ev.target.id === 'searchInput') {
    runSearch(ev.target.value);
  }
  if (ev.key === 'Escape' && store.get().composerOpen) {
    store.set({ composerOpen: false }, ['composer']);
  }
});

/* 提示条。ms 是可选的停留时长 —— 默认 2.6 秒够看「已保存」这种，
   但「邮件已发出、副本没存上」这种必须让人看清，得给长一点。 */
function toast(msg, kind = '', ms = 2600) {
  const d = document.createElement('div');
  d.className = 'toast ' + kind;
  d.textContent = msg;
  el.toast.appendChild(d);
  setTimeout(() => d.remove(), ms);
}

boot();
pollReplyQueue();
