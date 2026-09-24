/* 前端轻量分级：给列表邮件打标签（★重点 / ✉要回 / ⚙系统 / ○看一眼 / ×可忽略），
   供左侧「标签树」筛选。

   规则可在 config.json 的 classify 段里改（install.py 会引导填写）；
   没配就用下面这套「交大场景」的默认值。 */

const DEFAULTS = {
  vip_senders: ['gift-research@sjtu.edu.cn'],
  vip_domains: ['nsfc.gov.cn', 'catl.com', 'sunrayip.com'],
  act_senders: ['application.infoplus@sjtu.edu.cn'],
  act_domains: ['xcdsystem.com', 'easychair.org', 'editorialmanager.com',
                'elsevier.com', 'pro.nsfc.gov.cn'],
  dump_domains: [
    'researchgatemail.net', 'asianonlinejournals.com', 'onlinesciencepublishing.com',
    'onlineacademicpress.com', 'conscientiabeam.org', 'aessweb.org', 'esn-press.org',
    'ecsenet.com', 'ejvle.e-nig.com', 'cdkz.shop', 'dtgwfdj.cn', 'zbmiye.com',
  ],
  dump_tlds: ['shop', 'top', 'xyz', 'click', 'link', 'info', 'biz', 'work', 'live', 'icu'],
  trusted: ['sjtu.edu.cn', 'qq.com', 'gmail.com', '163.com', '126.com', 'outlook.com',
            'catl.com', 'nsfc.gov.cn', 'sunrayip.com', 'elsevier.com', 'springer.com',
            'mdpi.com', 'ieee.org', 'sae.org', 'jove.com', 'mathworks.com',
            'edu.cn', 'gov.cn', 'vip.163.com', 'sina.com'],
};

let R = DEFAULTS;

/** main.js 拿到后端配置后调用，覆盖默认规则（只覆盖你配了的字段） */
export function setClassifyRules(cfg) {
  if (cfg && typeof cfg === 'object') R = Object.assign({}, DEFAULTS, cfg);
}

export const TIERS = [
  { id: 'vip',   name: '★ 重点',   cls: 'vip' },
  { id: 'reply', name: '✉ 要回的', cls: 'reply' },
  { id: 'act',   name: '⚙ 系统处理', cls: 'act' },
  { id: 'read',  name: '○ 看一眼', cls: 'read' },
  { id: 'dump',  name: '× 可忽略', cls: 'dump' },
];

function addrOf(from) {
  const m = /<([^>]+)>/.exec(from || '');
  return (m ? m[1] : (from || '')).trim().toLowerCase();
}

function machineGenerated(msg) {
  const addr = addrOf(msg.from);
  const [local, dom] = addr.split('@');
  if (!dom) return true;
  const name = (msg.from_name || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
  if (name && name === (local || '').replace(/[^a-z0-9]/gi, '').toLowerCase()) return true;
  if ((local || '').length > 15) return true;
  if (/\d{6,}/.test(local || '')) return true;
  for (const lb of dom.split('.')) {
    if (['com', 'cn', 'net', 'org', 'edu', 'gov', 'info', 'io', 'co', 'ac'].includes(lb)) continue;
    if (lb.length >= 4 && /\d/.test(lb)) return true;
    if (/^[a-z]{6,14}$/.test(lb)) return true;
  }
  return false;
}

export function classify(msg) {
  const addr = addrOf(msg.from);
  const dom = addr.split('@')[1] || '';
  const tld = dom.split('.').pop();

  /* 用户手动标的重要，优先于任何自动规则。
     不这么写的话，「标重要」点下去会看不出任何效果 —— 那封邮件既进不了
     左侧的「★ 重点」，还可能因为域名规则被自动判成「× 可忽略」，
     等于用户的手动判断被机器覆盖掉了。 */
  if (msg.flagged) return 'vip';

  if (R.dump_domains.includes(dom) || R.dump_tlds.includes(tld)) return 'dump';
  if (R.act_senders.includes(addr) || R.act_domains.includes(dom)) return 'act';
  if (R.vip_senders.includes(addr) || R.vip_domains.includes(dom)) return 'vip';

  const trusted = R.trusted.some((t) => dom === t || dom.endsWith('.' + t));
  if (!trusted && machineGenerated(msg)) return 'dump';
  if (/no-?reply|notification|newsletter|mailer|digest/i.test(addr)) return 'read';
  if (/[\u4e00-\u9fff]/.test(msg.from_name || '')) return 'reply';
  return 'read';
}

/** 从邮件里拆出显示名与邮箱 */
export function splitFrom(from) {
  if (!from) return { name: '', addr: '' };
  const m = /^(.*?)\s*<([^>]+)>$/.exec(from);
  if (m) return { name: (m[1] || '').replace(/^["']|["']$/g, '').trim(), addr: m[2].trim() };
  return { name: '', addr: from.trim() };
}
