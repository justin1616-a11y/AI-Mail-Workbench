/* ① 左侧窄导航条。

   这一条原来叫「WorkBuddy 主导航条」，里面除了 Mails 还挂着
   会话 / 任务 / 技能 / 文件 四个按钮 —— 但这个项目里它们**没有实现**，
   点下去只弹一句「『会话』空间还没接进来」。四个按钮、四个死路，
   对使用者是纯噪音（也让人以为功能坏了）。

   现在只留真正有意义的两个东西：
     · Mails 空间卡片（当前空间，点一下 = 重新拉取当前文件夹）
     · 底部两个计数（本地索引可检索数 / 全部邮件数）
   如果以后真的要把 WorkBuddy 的其他空间接进来，在 SPACES 里加一条即可。 */

import { icons } from '../icons.js';

/* 已接入的空间。目前只有邮件一个 —— 不写占位项：
   原来这里还挂着 会话 / 任务 / 技能 / 文件 四个按钮，但这个项目里
   它们**没有实现**，点下去只弹一句「『会话』空间还没接进来」。
   四个按钮、四个死路，对使用者是纯噪音（还让人以为功能坏了）。

   `data-nav` 保留着当**语义标记**（"这是一个空间入口"）——
   Playwright 回归测试按它定位，删掉等于白送一次假失败。
   `data-action` 才是真的行为：点当前空间 = 重新拉取当前文件夹。 */
const SPACES = [
  { id: 'mail', label: 'Mails', icon: icons.mail, active: true },
];

export function renderRail(state) {
  const unread = (state.folders || []).reduce((n, f) => n + (f.unseen || 0), 0);
  const total = (state.folders || []).reduce((n, f) => n + (f.total || 0), 0);
  const badge = unread > 99 ? '99+' : unread;

  const spaces = SPACES.map((s) => `
    <button class="rail-space${s.active ? ' is-active' : ''}"
            data-nav="${s.id}"
            data-action="${s.active ? 'refresh' : 'nav-' + s.id}"
            title="${s.label}${s.active ? ' · 点一下重新拉取当前文件夹' : ''}">
      <span class="rs-ico">${s.icon}</span>
      <span class="rs-name">${s.label}</span>
      <span class="rs-badge${unread && s.active ? '' : ' is-zero'}">${
        s.active ? badge : 0}</span>
    </button>`).join('');

  return `
    <div class="rail-brand" title="邮件工作台">W</div>

    <div class="rail-sep">空间</div>

    ${spaces}

    <span style="flex:1"></span>
    <div class="rail-foot" title="本地索引可检索邮件数（Foxmail 历史 + 已同步）">
      <span class="rf-num">${state.indexCount || 0}</span>
      <span class="rf-lbl">索引</span>
    </div>
    <div class="rail-foot" title="全部邮件数">
      <span class="rf-num">${total}</span>
      <span class="rf-lbl">邮件</span>
    </div>
  `;
}
