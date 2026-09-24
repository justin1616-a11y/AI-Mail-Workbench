/* ② 左侧列：文件夹树 + 标签树。
   文件夹来自 IMAP（含未读数），标签是前端分级视图（★/✉/⚙/○/×）。 */

import { icons } from '../icons.js';
import { TIERS } from '../classify.js';

const FOLDER_ICON = {
  INBOX: icons.inbox,
  Drafts: icons.draft,
  Sent: icons.send,
  Trash: icons.trash,
  Junk: icons.archive,
};
const SYSTEM_ORDER = ['INBOX', 'Drafts', 'Sent', 'Junk', 'Trash'];

export function renderFolders(state) {
  const folders = state.folders || [];
  const sys = folders.filter((f) => SYSTEM_ORDER.includes(f.raw));
  const custom = folders.filter((f) => !SYSTEM_ORDER.includes(f.raw));

  const item = (f) => {
    const active = state.view === 'folder' && state.activeFolder === f.raw ? ' is-active' : '';
    const cnt = f.unseen > 0
      ? `<span class="tree-count is-unread">${f.unseen}</span>`
      : (f.total ? `<span class="tree-count">${f.total}</span>` : '');
    return `<button class="tree-item${active}" data-folder="${escapeAttr(f.raw)}">
      <span class="ti-ico">${FOLDER_ICON[f.raw] || icons.file}</span>
      <span class="ti-name">${esc(f.name)}</span>
      ${cnt}
    </button>`;
  };

  const tagItem = (t) => {
    const active = state.view === 'tag' && state.activeFolder === t.id ? ' is-active' : '';
    return `<button class="tree-item${active}" data-tag="${t.id}">
      <span class="ti-ico"><span class="tag-dot" style="background:${dotColor(t.id)}"></span></span>
      <span class="ti-name">${t.name}</span>
    </button>`;
  };

  return `
    <div class="folders-head">
      <button class="compose-btn" data-action="compose">${icons.draft} 写邮件</button>
    </div>
    <div class="tree">
      <div class="tree-group-title">文件夹</div>
      ${sys.map(item).join('')}
      ${custom.length ? `<div class="tree-group-title">自建文件夹</div>${custom.map(item).join('')}` : ''}
      <div class="tree-group-title">标签</div>
      ${TIERS.map(tagItem).join('')}
      <div class="tree-group-title" style="display:flex;align-items:center;gap:4px">
        本地索引
      </div>
      <div class="tree-item" style="cursor:default">
        <span class="ti-ico">${icons.archive}</span>
        <span class="ti-name">${state.indexCount || 0} 封可检索</span>
      </div>
    </div>
  `;
}

function dotColor(id) {
  return { vip: '#d93a33', reply: '#3a66ac', act: '#2f86d4', read: '#8ba5cb', dump: '#c4daf5' }[id] || '#3a66ac';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function escapeAttr(s) { return esc(s); }
