/* 写邮件弹窗。硬边界：只写草稿箱，界面上不提供「发送」按钮。 */

import { icons } from '../icons.js';

export function renderComposer(state) {
  if (!state.composerOpen) return '';
  const p = state.composerPrefill || {};
  return `<div class="modal">
    <div class="modal-head">
      <span>${icons.draft} 写邮件</span>
      <span class="spacer"></span>
      <button class="btn-mini" data-action="close-composer">${icons.close}</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label>收件人</label>
        <input id="cTo" placeholder="name@sjtu.edu.cn" value="${esc(p.to || '')}">
      </div>
      <div class="field">
        <label>主题</label>
        <input id="cSubject" placeholder="（无主题）" value="${esc(p.subject || '')}">
      </div>
      <textarea id="cBody" placeholder="正文…">${esc(p.body || '')}</textarea>
    </div>
    <div class="modal-foot">
      <span class="hint">「发送」会立刻发出且无法撤回；「存入草稿箱」只存不发</span>
      <span class="spacer"></span>
      <button class="btn ghost" data-action="close-composer">取消</button>
      <button class="btn ghost" data-action="save-draft">存入草稿箱</button>
      <button class="btn" data-action="send">${icons.send} 发送</button>
    </div>
  </div>`;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
