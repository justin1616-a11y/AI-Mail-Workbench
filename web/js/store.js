/* 状态管理：单一状态源 + 订阅发布。
   原则：组件只读 state、只派发 action，不互相直接调用。
   任何一次 set 都会触发订阅者重渲染对应区域。
*/

/* state 字段名 → 需要重绘的区域。

   这张表是必须的：字段名（messages / selectedId / detail …）和区域名
   （list / reader / composer …）根本不是一套东西。早期版本直接拿
   Object.keys(patch) 当区域名用，结果只有 folders 这个同名键碰巧对上，
   其余列（列表、阅读窗格）永远收不到重绘通知 —— 表现就是「点了没反应」。
*/
const REGION_OF = {
  user:        ['rail'],
  folders:     ['rail', 'folders'],
  indexCount:  ['folders'],
  activeFolder:['folders', 'list'],
  view:        ['folders', 'list'],
  loadedFolder:['list'],          // 当前列表数据实际来自哪个文件夹（标签视图下与 activeFolder 不同）
  query:       ['list'],
  /* messages 也要通知阅读窗格重绘 —— 阅读窗格显示的这封邮件是**从 messages
     里查出来的**，它的 seen / flagged 都来自那里。只通知 list 的话，
     列表里变了、右边纹丝不动：
     点「标重要」请求发出去了、列表也打上红标了，但按钮还写着「标重要」。 */
  messages:    ['list', 'reader'],
  loadingList: ['list'],
  listError:   ['list'],
  /* 「后面还有更早的邮件吗」+「正在加载下一批吗」
     列表一次只拉 60 封，滚到底再拉下一批，靠这两个字段控制底栏指示。 */
  listHasMore: ['list'],
  loadingMore: ['list'],
  filter:      ['list'],
  checked:     ['list'],
  replyPending:['list', 'reader'], // 等待 WorkBuddy 起草的封数（reader 也要重绘：按钮三态）
  replyDone:   ['list'],          // 已起草好、等用户点开修改的封数
  /* 队列里正在等的邮件 uid 列表 + 已起草好的元信息。
     两者都影响阅读窗格：前者决定按钮是「起草」还是「已加入队列 · 撤回」，
     后者用来判断「我刚点的那封好了没」（好了就自动回传）。 */
  pendingIds:  ['list', 'reader'],
  /* 队列里每一条的状态：{uid, status, source, attempts, last_error}。
     阅读窗格的起草按钮靠它显示四态（待生成 / 生成中 / 失败 / 不在队列）。 */
  pendingMeta: ['list', 'reader'],
  failedCount: ['list', 'reader'],
  /* 等太久（超过阈值）还没被处理的封数 —— 工具栏要标黄提醒 */
  stalledCount:['list', 'reader'],
  /* 实时同步通道的状态：live（SSE 通）/ poll（退回轮询）/ down（断了）。
     工具栏显示一个小灯 —— 没有它，「新邮件怎么还没来」根本没法判断
     是没邮件、还是通道坏了。 */
  liveStatus:  ['list'],
  liveAt:      ['list'],
  doneMeta:    ['list', 'reader'],
  awaitingUids:['reader', 'list'],
  undoTrash:   ['list'],          // 「已移到已删除邮件」的撤销记录（最近一次）
  source:      ['list', 'reader'],
  selectedId:  ['list', 'reader'],
  detail:      ['reader'],
  loadingBody: ['reader'],
  composerOpen:['composer'],
  composerPrefill: ['composer'],
};

export function createStore(initial) {
  let state = initial;
  const subs = new Set();

  return {
    get: () => state,

    set(patch) {
      const next = typeof patch === 'function' ? patch(state) : patch;
      const keys = Object.keys(next);
      state = { ...state, ...next };

      const regions = new Set();
      for (const k of keys) {
        for (const r of REGION_OF[k] || []) regions.add(r);
      }
      if (!regions.size) regions.add('list');      // 兜底：至少重绘列表
      subs.forEach((fn) => fn(state, [...regions]));
    },

    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}

export const initialState = {
  // 账户与文件夹
  user: '',
  folders: [],
  indexCount: 0,

  // 当前视图
  activeFolder: 'INBOX',
  view: 'folder',            // 'folder' | 'search' | 'tag'
  loadedFolder: 'INBOX',     // 列表数据来源文件夹
  query: '',

  // 列表
  messages: [],
  loadingList: false,
  listError: '',
  listHasMore: false,        // 后面还有更早的邮件（滚到底可继续加载）
  loadingMore: false,        // 正在加载下一批

  // 筛选
  filter: 'all',             // all | unread | attach

  // 选中与批量
  selectedId: null,
  checked: new Set(),
  loadingBody: false,
  detail: null,

  // 写邮件
  composerOpen: false,
  composerPrefill: null,

  // 等待 WorkBuddy 起草回复的封数
  replyPending: 0,
  // 已起草好、等用户点开修改的封数
  replyDone: 0,
  // 队列里正在等的邮件 uid（用于按钮三态，防重复提交）
  pendingIds: [],
  // 队列里每条的状态（待生成 / 生成中 / 失败 + 手动还是自动）
  pendingMeta: [],
  failedCount: 0,
  // 排队或生成超过阈值还没动静的封数（界面标黄）
  stalledCount: 0,
  /* 实时同步通道状态：connecting / live / poll / down
     connecting 页面刚开还没连上；live 是 IMAP IDLE 推着；poll 是退回轮询兜底。 */
  liveStatus: 'connecting',
  // 上次实时刷新的时间（工具栏显示「刚刚同步」用），'' 表示还没同步过
  liveAt: '',
  // 已起草好的草稿元信息 [{id, uid, subject}]（用于自动回传配对）
  doneMeta: [],
  /* 用户主动点过「让 WorkBuddy 起草」的邮件 uid —— 这些是「他想立刻回」的，
     草稿一好就自动填进编辑器；没点过的（每小时自动处理攒下来的）只提示、不抢。 */
  awaitingUids: [],

  // 最近一次「移到已删除邮件」的记录，用来给一次反悔的机会。
  // 形如 { folder, trash, items:[{uid, trash_uid}], count }
  undoTrash: null,
};

export const store = createStore(initialState);

/* ---------- actions ---------- */

export const actions = {
  setActiveFolder(name) {
    store.set({
      activeFolder: name,
      view: 'folder',
      query: '',
      selectedId: null,
      checked: new Set(),
      detail: null,
    }, ['folders', 'list', 'reader']);
  },

  setFilter(f) {
    store.set({ filter: f }, ['list']);
  },

  setQuery(q) {
    store.set({ query: q }, ['toolbar']);
  },

  toggleCheck(id) {
    const checked = new Set(store.get().checked);
    checked.has(id) ? checked.delete(id) : checked.add(id);
    store.set({ checked }, ['list']);
  },

  clearChecks() {
    store.set({ checked: new Set() }, ['list']);
  },

  checkAll(ids) {
    store.set({ checked: new Set(ids) }, ['list']);
  },
};
