/* ① WorkBuddy 主导航条。

   布局意图：邮件入口不再混在一排图标里让人找 ——
   顶部单独辟出「空间」区，Mails 作为空间卡片常驻最上方，
   带图标 + 名称 + 未读徽标，一眼可见。下方才是其他导航项。
*/

import { icons } from '../icons.js';

const NAV = [
  { id: 'chat',  label: '会话', icon: icons.chat },
  { id: 'task',  label: '任务', icon: icons.task },
  { id: 'skill', label: '技能', icon: icons.skill },
  { id: 'file',  label: '文件', icon: icons.file },
];

export function renderRail(state) {
  const unread = (state.folders || []).reduce((n, f) => n + (f.unseen || 0), 0);
  const total = (state.folders || []).reduce((n, f) => n + (f.total || 0), 0);
  const badge = unread > 99 ? '99+' : unread;

  const others = NAV.map((n) => `
    <button class="rail-item" data-nav="${n.id}" title="${n.label}">
      <span class="ico">${n.icon}</span>
      <span class="lbl">${n.label}</span>
    </button>`).join('');

  return `
    <div class="rail-brand" title="WorkBuddy">W</div>

    <div class="rail-sep">空间</div>

    <button class="rail-space is-active" data-nav="mail" title="邮件管理 · Mails">
      <span class="rs-ico">${icons.mail}</span>
      <span class="rs-name">Mails</span>
      <span class="rs-badge${unread ? '' : ' is-zero'}">${badge}</span>
    </button>

    <div class="rail-line"></div>

    ${others}

    <span style="flex:1"></span>
    <div class="rail-foot" title="本地索引可检索邮件数">
      <span class="rf-num">${state.indexCount || 0}</span>
      <span class="rf-lbl">索引</span>
    </div>
    <div class="rail-foot" title="全部邮件数">
      <span class="rf-num">${total}</span>
      <span class="rf-lbl">邮件</span>
    </div>
  `;
}
