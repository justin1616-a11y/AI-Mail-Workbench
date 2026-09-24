/* 图标：内联 SVG，stroke 风格，统一 20x20，currentColor 继承文字色。
   不用 emoji —— WorkBuddy 界面本身是线性图标风格。 */

const S = (inner) =>
  `<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;

export const icons = {
  chat:    S('<path d="M3 5.5A2.5 2.5 0 0 1 5.5 3h9A2.5 2.5 0 0 1 17 5.5v6a2.5 2.5 0 0 1-2.5 2.5H8l-4 3v-3h-.5A2.5 2.5 0 0 1 3 11.5z"/>'),
  mail:    S('<rect x="2.5" y="4.5" width="15" height="11" rx="2"/><path d="M3 6l7 5 7-5"/>'),
  task:    S('<rect x="3" y="3.5" width="14" height="13" rx="2"/><path d="M6.5 10l2.5 2.5 4.5-5"/>'),
  skill:   S('<path d="M11 2.5L5 11h4l-1 6.5L15 9h-4z"/>'),
  file:    S('<path d="M3 6a2 2 0 0 1 2-2h3l2 2h5a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  inbox:   S('<path d="M3 11l2-6a1.5 1.5 0 0 1 1.4-1h7.2A1.5 1.5 0 0 1 15 5l2 6v5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 16z"/><path d="M3 11h4l1 2h4l1-2h4"/>'),
  draft:   S('<path d="M14.5 3.5l2 2-9 9H5v-2.5z"/><path d="M12.5 5.5l2 2"/>'),
  send:    S('<path d="M17 3L9 11"/><path d="M17 3l-5 14-3-6-6-3z"/>'),
  trash:   S('<path d="M4 6h12"/><path d="M8 6V4.5h4V6"/><path d="M5.5 6l.7 10a1.5 1.5 0 0 0 1.5 1.4h4.6a1.5 1.5 0 0 0 1.5-1.4l.7-10"/>'),
  archive: S('<rect x="3" y="4" width="14" height="3.5" rx="1"/><path d="M4.5 7.5v7A1.5 1.5 0 0 0 6 16h8a1.5 1.5 0 0 0 1.5-1.5v-7"/><path d="M8.5 10.5h3"/>'),
  search:  S('<circle cx="9" cy="9" r="5.5"/><path d="M13 13l3.5 3.5"/>'),
  close:   S('<path d="M5.5 5.5l9 9"/><path d="M14.5 5.5l-9 9"/>'),
  attach:  S('<path d="M15 9.5l-5.2 5.2a3.2 3.2 0 0 1-4.5-4.5l5.5-5.5a2.2 2.2 0 0 1 3.1 3.1l-5.5 5.5a1.2 1.2 0 0 1-1.7-1.7l5-5"/>'),
  reply:   S('<path d="M8 5L3.5 9.5 8 14"/><path d="M3.5 9.5h8a5 5 0 0 1 5 5v.5"/>'),
  refresh: S('<path d="M16.5 10a6.5 6.5 0 1 1-2-4.7"/><path d="M16.8 3.5v3.2h-3.2"/>'),
  star:    S('<path d="M10 3l2.2 4.6 5 .7-3.6 3.5.9 5-4.5-2.4-4.5 2.4.9-5L3.8 8.3l5-.7z"/>'),
};
