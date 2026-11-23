'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
const $view = document.getElementById('view');
const $tabbar = document.getElementById('tabbar');
const $tip = document.getElementById('tip');
const $toast = document.getElementById('toast');

const PALETTE = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c', '#9085e9', '#e66767'],
};
const TONES = {
  positive: { label: 'Позитив', color: '#0ca30c' },
  negative: { label: 'Негатив', color: '#d03b3b' },
  mixed: { label: 'Смешанный', color: '#fab219' },
  neutral: { label: 'Нейтрально', color: '#8e8e93' },
};
const AUTO_PRESETS = [10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080];
const TOP_SOURCES = 8;
const FEED_PAGE = 50;

const S = {
  state: null,
  stateError: null,
  dash: null,
  dashError: null,
  dashLoading: false,
  dashAt: 0,
  dashStale: true,
  tab: 'overview',
  sub: null,
  feed: { kw: 'all', src: 'all', limit: FEED_PAGE },
  view: { sources: 'chart', trend: 'chart' },
  variants: {},
  variantsEnabled: true,
  lastView: '',
};


const nf = new Intl.NumberFormat('ru-RU');
const fmt = (n) => nf.format(n || 0);
const sum = (a) => a.reduce((x, y) => x + (y || 0), 0);

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (b === 1 && a !== 11) return one;
  if (b >= 2 && b <= 4 && (a < 12 || a > 14)) return few;
  return many;
}
const hoursText = (h) => `${h} ${plural(h, 'час', 'часа', 'часов')}`;
const mentions = (n) => plural(n, 'упоминание', 'упоминания', 'упоминаний');
const materialsIn = (n) => `в ${fmt(n)} ${plural(n, 'материале', 'материалах', 'материалах')}`;

function fmtTime(d) {
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}
function dayLabel(d) {
  const now = new Date();
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return 'Сегодня';
  if (sameDay(d, y)) return 'Вчера';
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}
function whenLabel(d) {
  return sameDay(d, new Date()) ? fmtTime(d) : `${d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })}, ${fmtTime(d)}`;
}
function humanEvery(min) {
  if (min % 1440 === 0) return `${min / 1440} сут`;
  if (min < 60) return `${min} мин`;
  if (min % 60 === 0) return `${min / 60} ч`;
  return `${Math.floor(min / 60)} ч ${min % 60} мин`;
}
function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}

const haptic = {
  sel() { try { tg.HapticFeedback.selectionChanged(); } catch {} },
  tap() { try { tg.HapticFeedback.impactOccurred('light'); } catch {} },
  ok() { try { tg.HapticFeedback.notificationOccurred('success'); } catch {} },
  err() { try { tg.HapticFeedback.notificationOccurred('error'); } catch {} },
};

function openLink(url) {
  if (!/^https?:\/\//i.test(url || '')) return;
  haptic.tap();
  if (tg && tg.openLink) tg.openLink(url);
  else window.open(url, '_blank', 'noopener');
}


const AUTHOR = { name: 'Ivan Zh', handle: 'o1928x' };
const WATERMARK = 'KeyScout';

function credit() {
  const url = `https://t.me/${AUTHOR.handle}`;
  return h('p', { class: 'credit' }, `© ${AUTHOR.name} (`,
    h('a', {
      href: url,
      onclick: (e) => {
        e.preventDefault();
        haptic.tap();
        if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
        else window.open(url, '_blank', 'noopener');
      },
    }, `@${AUTHOR.handle}`), ')');
}

const botUsername = () => (S.state && S.state.bot_username) || '';

function watermark(rows) {
  const size = Math.max(16, Math.min(30, Math.round(rows * 32 * 0.3)));
  const sub = rows >= 2 && botUsername();
  return h('div', { class: 'wm', 'aria-hidden': 'true', style: { fontSize: `${size}px` } },
    WATERMARK, sub ? h('small', {}, '@' + sub) : null);
}

let toastTimer;
function toast(text) {
  $toast.textContent = text;
  $toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.remove('show'), 2600);
}


function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
}
const SVGNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === 'style') Object.assign(el.style, v);
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}

const ICONS = {
  overview: '<svg width="26" height="26" viewBox="0 0 26 26" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M5 21V13"/><path d="M10.5 21V5"/><path d="M16 21v-9"/><path d="M21.5 21V8.5"/></svg>',
  feed: '<svg width="26" height="26" viewBox="0 0 26 26" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4.5" width="18" height="17" rx="3.5"/><path d="M8.5 10h9M8.5 14h9M8.5 18h5"/></svg>',
  settings: '<svg width="26" height="26" viewBox="0 0 26 26" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M5 8h9M19 8h2M5 18h2M12 18h9"/><circle cx="16.5" cy="8" r="2.5"/><circle cx="9.5" cy="18" r="2.5"/></svg>',
  refresh: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 10a6.5 6.5 0 1 1-1.9-4.6"/><path d="M15.5 2.5v3.6h-3.6"/></svg>',
  chevron: '<svg width="8" height="14" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 1.5 6.5 7l-5 5.5"/></svg>',
  updown: '<svg width="10" height="16" viewBox="0 0 10 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m2 6 3-3 3 3M2 10l3 3 3-3"/></svg>',
  remove: '<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="8.5"/><path d="M7.5 11h7"/></svg>',
  external: '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 1.5h6v6M9.5 1.5l-8 8"/></svg>',
  clock: '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="9" cy="9" r="7"/><path d="M9 5v4l2.5 1.5"/></svg>',
};
function icon(name) {
  const el = h('span', { class: 'icon', 'aria-hidden': 'true' });
  el.innerHTML = ICONS[name];
  return el;
}
function dot(color) {
  return h('span', { class: 'dot', style: { background: color || 'var(--hint)' }, 'aria-hidden': 'true' });
}
function toneMark(tone, size = 10) {
  const c = (TONES[tone] || TONES.neutral).color;
  const r = size / 2;
  const shapes = {
    positive: s('path', { d: `M${r} 0.5 ${size - 0.5} ${size - 1} 0.5 ${size - 1}Z` }),
    negative: s('path', { d: `M0.5 1 ${size - 0.5} 1 ${r} ${size - 0.5}Z` }),
    mixed: s('path', { d: `M${r} 0 ${size} ${r} ${r} ${size} 0 ${r}Z` }),
    neutral: s('circle', { cx: r, cy: r, r: r - 1 }),
  };
  return s('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true', style: { fill: c } },
    shapes[tone] || shapes.neutral);
}
function tonePill(tone) {
  const t = TONES[tone] || TONES.neutral;
  return h('span', { class: 'tone' }, toneMark(tone), t.label);
}
function deltaMark(up) {
  return s('svg', { width: 8, height: 8, viewBox: '0 0 8 8', 'aria-hidden': 'true', style: { fill: 'currentColor' } },
    s('path', { d: up ? 'M4 0.5 7.5 7H0.5Z' : 'M0.5 1h7L4 7.5Z' }));
}


async function api(path, body) {
  const opts = { headers: { Authorization: 'tma ' + tg.initData } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch('api/' + path, opts);
  } catch {
    throw new Error('Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.');
  }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error((data && data.error) || `Сервер ответил ошибкой ${res.status}.`);
  return data;
}

async function loadState() {
  try {
    S.state = await api('state');
    S.stateError = null;
  } catch (e) {
    S.stateError = e.message;
  }
  render({ background: true });
  loadVariants();
}

async function loadDashboard() {
  if (S.dashLoading) return;
  S.dashLoading = true;
  render({ background: true });
  try {
    S.dash = await api('dashboard');
    S.dashError = null;
    S.dashAt = Date.now();
    S.dashStale = false;
    if (S.dash.data) {
      for (const k of S.dash.data.keywords) S.variants[k.query] = k.variants;
    }
  } catch (e) {
    S.dashError = e.message;
  } finally {
    S.dashLoading = false;
    render({ background: true });
  }
}

async function loadVariants() {
  const kws = (S.state && S.state.settings.keywords) || [];
  const missing = kws.filter((k) => !S.variants[k]);
  if (!missing.length) return;
  try {
    const res = await api('expand', { keywords: missing });
    S.variantsEnabled = res.enabled;
    res.keywords.forEach((k, i) => { S.variants[missing[i]] = k.variants; });
    if (S.tab === 'settings' && !S.sub) render({ background: true });
  } catch {}
}

let saveChain = Promise.resolve();
function save(patch, { okText } = {}) {
  const run = async () => {
    try {
      const st = await api('settings', patch);
      const before = S.state && S.state.settings;
      S.state = st;
      if (!before || 'keywords' in patch || 'sites' in patch || 'add_sites' in patch || 'window_hours' in patch) {
        S.dashStale = true;
      }
      haptic.ok();
      if (okText) toast(typeof okText === 'function' ? okText(st) : okText);
      render();
      loadVariants();
      return st;
    } catch (e) {
      haptic.err();
      toast(e.message);
      await loadState();
      return null;
    }
  };
  saveChain = saveChain.then(run, run);
  return saveChain;
}


function header(title, sub, action) {
  return h('header', { class: 'top' },
    h('div', {}, h('h1', {}, title), sub ? h('p', {}, sub) : null),
    action || null);
}

function loading(text) {
  return h('div', { class: 'loading' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), text);
}

function emptyState(title, text, button, onclick) {
  return h('div', { class: 'empty' }, h('h2', {}, title), h('p', {}, text),
    button ? h('button', { class: 'btn', onclick }, button) : null);
}

function go(tab) {
  S.tab = tab;
  S.sub = null;
  hideTip();
  haptic.sel();
  if ((tab === 'overview' || tab === 'feed') && S.dashStale) loadDashboard();
  render();
}

const TABS = [['overview', 'Обзор'], ['feed', 'Лента'], ['settings', 'Настройки']];
function buildTabbar() {
  $tabbar.replaceChildren(...TABS.map(([key, label]) =>
    h('button', { 'data-tab': key, onclick: () => (S.tab === key && !S.sub ? window.scrollTo({ top: 0, behavior: 'smooth' }) : go(key)) },
      icon(key), label)));
  $tabbar.hidden = false;
}

const mounts = [];
function onMount(fn) { mounts.push(fn); }

function render({ background = false } = {}) {
  const active = document.activeElement;
  if (background && active && $view.contains(active) && /INPUT|SELECT/.test(active.tagName)) {
    active.addEventListener('blur', () => setTimeout(() => render({ background: true }), 0), { once: true });
    return;
  }
  const key = S.sub || S.tab;
  const y = window.scrollY;
  mounts.length = 0;
  let node;
  if (S.sub === 'sources') node = renderSources();
  else if (S.tab === 'feed') node = renderFeed();
  else if (S.tab === 'settings') node = renderSettings();
  else node = renderOverview();
  if (!node.querySelector('.loading')) node.append(credit());
  $view.replaceChildren(node);
  mounts.splice(0).forEach((fn) => fn());
  window.scrollTo(0, key === S.lastView ? y : 0);
  S.lastView = key;
  for (const b of $tabbar.querySelectorAll('button')) {
    if (b.dataset.tab === S.tab) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  }
  if (tg && tg.BackButton) S.sub ? tg.BackButton.show() : tg.BackButton.hide();
}


let tipOwner = null;
function tipRows(title, rows, total) {
  const kids = [h('div', { class: 'tip-title' }, title)];
  for (const r of rows) {
    kids.push(h('div', { class: 'tip-row' },
      h('span', { class: 'key-line', style: { background: r.color } }), h('b', {}, fmt(r.value)), h('span', {}, r.label)));
  }
  if (total != null) kids.push(h('div', { class: 'tip-total' }, `Всего ${fmt(total)}`));
  return kids;
}
function showTip(owner, kids, x, y) {
  tipOwner = owner;
  $tip.replaceChildren(...kids);
  $tip.hidden = false;
  const w = $tip.offsetWidth, hgt = $tip.offsetHeight;
  const left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8);
  let top = y - hgt - 14;
  if (top < 8) top = y + 18;
  $tip.style.left = left + 'px';
  $tip.style.top = top + 'px';
}
function hideTip() {
  if (tipOwner && tipOwner.classList) tipOwner.classList.remove('is-active');
  if (tipOwner && tipOwner.onHide) tipOwner.onHide();
  tipOwner = null;
  $tip.hidden = true;
}
document.addEventListener('pointerdown', (e) => {
  if (tipOwner && !(tipOwner.contains && tipOwner.contains(e.target))) hideTip();
});
window.addEventListener('scroll', () => { if (tipOwner) hideTip(); }, { passive: true });

function attachTip(el, content) {
  const show = (x, y) => {
    if (tipOwner && tipOwner !== el) hideTip();
    el.classList.add('is-active');
    showTip(el, content(), x, y);
  };
  el.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') show(e.clientX, e.clientY); });
  el.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && tipOwner === el) hideTip(); });
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    haptic.sel();
    const r = el.getBoundingClientRect();
    show(e.clientX, r.top);
  });
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); show(r.left + r.width / 2, r.top); });
  el.addEventListener('blur', () => { if (tipOwner === el) hideTip(); });
}


function palette() {
  return document.documentElement.dataset.theme === 'dark' ? PALETTE.dark : PALETTE.light;
}
function seriesOf(d) {
  const pal = palette();
  return d.keywords.map((k, i) => ({ label: k.label, query: k.query, color: i < pal.length ? pal[i] : '#8e8e93' }));
}
function toneOf(d, label) {
  const it = d.digest && d.digest.items.find((x) => x.keyword === label);
  return it ? it.tone : 'neutral';
}

function valueIn(totals, label) {
  if (!totals) return undefined;
  if (label in totals) return totals[label];
  const low = label.toLowerCase();
  const key = Object.keys(totals).find((k) => k.toLowerCase() === low);
  return key === undefined ? undefined : totals[key];
}
function lastCheckWith(history, labels) {
  for (let i = history.length - 1; i >= 0; i--) {
    if (labels.every((l) => valueIn(history[i].totals, l) !== undefined)) return history[i];
  }
  return null;
}

function deltaLine(now, prev, prevAt) {
  if (prev == null) return null;
  const diff = now - prev;
  const at = prevAt ? ` (${whenLabel(prevAt)})` : '';
  if (diff === 0) return h('div', { class: 'delta' }, `Без изменений с прошлой проверки${at}`);
  return h('div', { class: 'delta' }, deltaMark(diff > 0), `${fmt(Math.abs(diff))} с прошлой проверки${at}`);
}

function renderOverview() {
  const st = S.state, D = S.dash;
  const win = (st && st.settings.window_hours) || 24;
  const refresh = h('button', {
    class: 'round-btn' + (S.dashLoading ? ' is-spinning' : ''), 'aria-label': 'Обновить данные',
    onclick: () => { haptic.tap(); loadDashboard(); },
  }, icon('refresh'));
  const page = h('div', { class: 'page' });
  page.append(header('Обзор', `За последние ${hoursText(win)}`, D && D.state === 'ok' ? refresh : null));

  if (!D) {
    if (S.dashError) page.append(emptyState('Данные не загрузились', S.dashError, 'Повторить', () => loadDashboard()));
    else {
      const n = st ? st.settings.sites.length : 0;
      page.append(loading(n ? `Проверяю ${n} ${plural(n, 'источник', 'источника', 'источников')}…` : 'Загружаю…'));
    }
    return page;
  }
  if (D.state === 'no_keywords') {
    page.append(emptyState('Что будем искать?',
      'Добавьте слова — компании, людей или темы. Я соберу упоминания по новостным сайтам и оценю тон.',
      'Добавить слова', () => go('settings')));
    return page;
  }
  if (D.state === 'no_sites') {
    page.append(emptyState('Источники выключены', 'Включите хотя бы один сайт, чтобы начать поиск.',
      'Выбрать источники', () => { S.tab = 'settings'; S.sub = 'sources'; render(); }));
    return page;
  }
  if (S.dashError) toast(S.dashError), (S.dashError = null);

  const d = D.data;
  const series = seriesOf(d);
  const history = (D.history || []).filter((p) => p && p.totals);
  const body = h('div', { class: S.dashLoading ? 'is-refreshing' : '' });
  body.append(hero(d, history, series));
  if (series.length > 1) body.append(tiles(d, history, series));
  const notice = coverageNotice(d);
  if (notice) body.append(notice);
  body.append(sourcesCard(d, series));
  body.append(trendCard(d, history, series));
  body.append(digestSection(d, series));
  body.append(h('p', { class: 'footnote' }, `Данные на ${fmtTime(new Date(d.checked_at))}. Просмотрено ${fmt(d.n_materials)} ${plural(d.n_materials, 'материал', 'материала', 'материалов')}.`));
  page.append(body);
  return page;
}

function hero(d, history, series) {
  const prev = lastCheckWith(history, series.map((x) => x.label));
  const prevAt = prev ? new Date(prev.checked_at) : null;
  if (series.length === 1) {
    const k = series[0].label;
    const n = d.totals[k] || 0;
    return h('section', { class: 'hero' },
      h('div', { class: 'hero-kw' }, dot(series[0].color), k),
      h('div', { class: 'hero-num' }, fmt(n)),
      h('div', { class: 'hero-cap' }, `${mentions(n)} ${materialsIn(d.items[k] || 0)}`),
      deltaLine(n, prev && valueIn(prev.totals, k), prevAt),
      tonePill(toneOf(d, k)));
  }
  const total = sum(series.map((x) => d.totals[x.label]));
  const prevTotal = prev ? sum(series.map((x) => valueIn(prev.totals, x.label))) : null;
  return h('section', { class: 'hero' },
    h('div', { class: 'hero-num' }, fmt(total)),
    h('div', { class: 'hero-cap' }, `${mentions(total)} по ${series.length} ${plural(series.length, 'слову', 'словам', 'словам')}`),
    deltaLine(total, prevTotal, prevAt));
}

function tiles(d, history, series) {
  return h('div', { class: 'tiles' }, series.map((x) => {
    const n = d.totals[x.label] || 0;
    const prev = lastCheckWith(history, [x.label]);
    const p = prev ? valueIn(prev.totals, x.label) : null;
    let delta = null;
    if (p != null && n !== p) delta = h('div', { class: 'delta' }, deltaMark(n > p), fmt(Math.abs(n - p)));
    return h('button', {
      class: 'tile', 'aria-label': `${x.label}: ${fmt(n)} ${mentions(n)}. Открыть ленту`,
      onclick: () => { S.feed.kw = x.label; S.feed.limit = FEED_PAGE; go('feed'); },
    },
    h('div', { class: 'tile-body' },
      h('div', { class: 'tile-top' }, dot(x.color), h('span', {}, x.label)),
      h('div', { class: 'tile-num' }, fmt(n)),
      h('div', { class: 'tile-cap' }, materialsIn(d.items[x.label] || 0)),
      delta),
    tonePill(toneOf(d, x.label)));
  }));
}

function coverageNotice(d) {
  let text = '';
  if (d.busy && d.busy.length) {
    text = `Дособираю архив: ${d.busy.slice(0, 3).join(', ')}${d.busy.length > 3 ? ` и ещё ${d.busy.length - 3}` : ''}. Цифры уточнятся через пару минут — обновите обзор.`;
  } else {
    const partial = d.sites.filter((x) => x.ok && x.mode === 'rss' && x.coverage_hours < d.window_hours - 0.25);
    if (partial.length) {
      text = `Пока есть не все ${hoursText(d.window_hours)}: ` +
        partial.slice(0, 3).map((x) => `${x.name} — ${Math.round(x.coverage_hours)} ч`).join(', ') +
        (partial.length > 3 ? ` и ещё ${partial.length - 3}` : '') + '. Остальное соберётся само, пока бот работает.';
    }
  }
  return text ? h('div', { class: 'card notice' }, icon('clock'), h('span', {}, text)) : null;
}

function legend(series, kind) {
  if (series.length < 2) return null;
  return h('div', { class: 'legend' }, series.map((x) =>
    h('span', {}, h('i', { class: kind === 'line' ? 'key-line' : 'key-rect', style: { background: x.color } }), x.label)));
}

function chartCard(title, key, { series, kind, chart, table, note }) {
  const mode = S.view[key];
  const toggle = h('button', {
    class: 'link-btn', 'aria-pressed': mode === 'table' ? 'true' : 'false',
    onclick: () => { S.view[key] = mode === 'chart' ? 'table' : 'chart'; haptic.sel(); hideTip(); render(); },
  }, mode === 'chart' ? 'Таблица' : 'График');
  return h('section', { class: 'card chart-card' },
    h('div', { class: 'card-head' }, h('h3', {}, title), toggle),
    mode === 'chart' ? legend(series, kind) : null,
    mode === 'chart' ? chart() : table(),
    note ? h('p', { class: 'note' }, note) : null);
}

function dataTable(head, rows) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, head.map((x) => h('th', { scope: 'col' }, x)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c, i) => (i === 0 ? h('th', { scope: 'row' }, c) : h('td', {}, c))))))));
}

function sourcesCard(d, series) {
  const rows = d.sites.filter((x) => x.ok).map((x) => {
    const vals = series.map((k) => x.counts[k.label] || 0);
    return { name: x.name, vals, total: sum(vals) };
  });
  const hit = rows.filter((r) => r.total > 0).sort((a, b) => b.total - a.total);
  const zero = rows.length - hit.length;
  const shown = hit.slice(0, TOP_SOURCES);
  if (hit.length > TOP_SOURCES) {
    const rest = hit.slice(TOP_SOURCES);
    shown.push({
      name: `Другие (${rest.length})`, vals: series.map((_, i) => sum(rest.map((r) => r.vals[i]))),
      total: sum(rest.map((r) => r.total)),
    });
  }
  const failed = d.sites.filter((x) => !x.ok).map((x) => x.name);
  const notes = [];
  if (zero) notes.push(`Без упоминаний: ${zero} ${plural(zero, 'источник', 'источника', 'источников')}.`);
  if (failed.length) notes.push(`Не открылись: ${failed.join(', ')}.`);
  if (!hit.length) {
    return h('section', { class: 'card chart-card' }, h('div', { class: 'card-head' }, h('h3', {}, 'По источникам')),
      h('p', { class: 'muted' }, `За последние ${hoursText(d.window_hours)} упоминаний не нашлось.`),
      notes.length ? h('p', { class: 'note' }, notes.join(' ')) : null);
  }
  return chartCard('По источникам', 'sources', {
    series, kind: 'rect', note: notes.join(' '),
    chart: () => barsChart(shown, series),
    table: () => dataTable(['Источник', ...series.map((x) => x.label), ...(series.length > 1 ? ['Всего'] : [])],
      hit.map((r) => [r.name, ...r.vals.map(fmt), ...(series.length > 1 ? [fmt(r.total)] : [])])),
  });
}

function barsChart(rows, series) {
  const max = Math.max(1, ...rows.map((r) => r.total));
  return h('div', { class: 'bars' }, watermark(rows.length), rows.map((r) => {
    const bar = h('span', { class: 'bar', style: { width: `${(r.total / max) * 100}%` } },
      r.vals.map((v, i) => (v > 0 ? h('span', { class: 'seg', style: { flexGrow: v, background: series[i].color } }) : null)));
    const label = series.length > 1
      ? `${r.name}: ${series.map((x, i) => `${x.label} ${r.vals[i]}`).join(', ')}`
      : `${r.name}: ${r.total}`;
    const row = h('div', { class: 'bar-row', tabindex: '0', role: 'img', 'aria-label': label },
      h('span', { class: 'bar-label' }, r.name), h('span', { class: 'bar-track' }, bar), h('span', { class: 'bar-val' }, fmt(r.total)));
    attachTip(row, () => tipRows(r.name,
      series.map((x, i) => ({ color: x.color, label: x.label, value: r.vals[i] })).sort((a, b) => b.value - a.value),
      series.length > 1 ? r.total : null));
    return row;
  }));
}

function trendCard(d, history, series) {
  const norm = (totals) => Object.fromEntries(series.map((x) => [x.label, valueIn(totals, x.label)]).filter(([, v]) => v !== undefined));
  const pts = history.map((p) => ({ t: new Date(p.checked_at), totals: norm(p.totals) }))
    .filter((p) => !Number.isNaN(p.t.getTime()) && Object.keys(p.totals).length)
    .concat([{ t: new Date(d.checked_at), totals: norm(d.totals), now: true }])
    .sort((a, b) => a.t - b.t);
  if (pts.length < 2) {
    const auto = S.state && S.state.settings.auto;
    return h('section', { class: 'card chart-card' },
      h('div', { class: 'card-head' }, h('h3', {}, 'Динамика по проверкам')),
      h('p', { class: 'muted' }, auto
        ? 'График появится после следующей автопроверки.'
        : 'Здесь будет видно, как меняется число упоминаний от проверки к проверке. Включите автообновление — бот будет проверять сам.'),
      auto ? null : h('button', { class: 'link-btn', style: { marginTop: '8px' }, onclick: () => go('settings') }, 'Включить автообновление'));
  }
  return chartCard('Динамика по проверкам', 'trend', {
    series, kind: 'line',
    chart: () => {
      const host = h('div', { class: 'chart-host' });
      onMount(() => {
        let lastW = 0;
        const draw = () => {
          if (host.clientWidth && host.clientWidth !== lastW) { lastW = host.clientWidth; lineChart(host, pts, series); }
        };
        draw();
        if (window.ResizeObserver) new ResizeObserver(draw).observe(host);
      });
      return host;
    },
    table: () => dataTable(['Проверка', ...series.map((x) => x.label)],
      pts.slice().reverse().map((p) => [p.now ? 'Сейчас' : whenLabel(p.t), ...series.map((x) => (x.label in p.totals ? fmt(p.totals[x.label]) : '—'))])),
  });
}

function niceScale(max) {
  if (max <= 0) return { top: 4, step: 1 };
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  let step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  step = Math.max(1, Math.round(step));
  return { top: step * Math.ceil(max / step), step };
}

function lineChart(host, pts, series) {
  const W = host.clientWidth, H = 200;
  const values = pts.flatMap((p) => series.map((x) => p.totals[x.label]).filter((v) => v != null));
  const { top, step } = niceScale(Math.max(0, ...values));
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  const tickW = Math.max(...ticks.map((v) => fmt(v).length)) * 6.6;
  const m = { l: tickW + 10, r: 10, t: 10, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const pos = new Map(pts.map((p, i) => [p, i]));
  const X = (p) => m.l + (pts.length > 1 ? (pos.get(p) / (pts.length - 1)) * iw : iw / 2);
  const Y = (v) => m.t + ih - (v / top) * ih;
  const card = getComputedStyle(document.body).getPropertyValue('--card').trim() || '#fff';

  const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', tabindex: '0',
    'aria-label': 'Динамика упоминаний по проверкам. Подробности — в таблице.' });
  for (const v of ticks) {
    svg.append(s('line', { class: v === 0 ? 'baseline' : 'gridline', x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    svg.append(s('text', { x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, fmt(v)));
  }
  const wmX = m.l + iw / 2, wmY = m.t + ih / 2 - (botUsername() ? 7 : 0);
  svg.append(s('text', { class: 'wm-svg', x: wmX, y: wmY, 'text-anchor': 'middle', 'dominant-baseline': 'central' }, WATERMARK));
  if (botUsername()) {
    svg.append(s('text', { class: 'wm-svg wm-svg-sub', x: wmX, y: wmY + 23, 'text-anchor': 'middle', 'dominant-baseline': 'central' }, '@' + botUsername()));
  }
  const allSameDay = sameDay(pts[0].t, pts[pts.length - 1].t);
  const xl = (p) => (p.now ? 'сейчас' : allSameDay ? fmtTime(p.t) : p.t.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }));
  const xLabels = [[pts[0], 'start'], [pts[pts.length - 1], 'end']];
  if (pts.length > 3 && iw > 260) xLabels.splice(1, 0, [pts[Math.floor(pts.length / 2)], 'middle']);
  for (const [p, anchor] of xLabels) svg.append(s('text', { x: X(p), y: H - 6, 'text-anchor': anchor }, xl(p)));

  series.forEach((x) => {
    let dPath = '', pen = false;
    for (const p of pts) {
      const v = p.totals[x.label];
      if (v == null) { pen = false; continue; }
      dPath += `${pen ? 'L' : 'M'}${X(p).toFixed(1)} ${Y(v).toFixed(1)}`;
      pen = true;
    }
    if (series.length === 1 && dPath) {
      const first = pts.find((p) => p.totals[x.label] != null), last = [...pts].reverse().find((p) => p.totals[x.label] != null);
      svg.append(s('path', { d: `${dPath}L${X(last)} ${Y(0)}L${X(first)} ${Y(0)}Z`, style: { fill: x.color, opacity: 0.1 } }));
    }
    svg.append(s('path', { class: 'series-line', d: dPath, style: { stroke: x.color } }));
    const last = [...pts].reverse().find((p) => p.totals[x.label] != null);
    if (last) svg.append(s('circle', { cx: X(last), cy: Y(last.totals[x.label]), r: 4, style: { fill: x.color, stroke: card, strokeWidth: 2 } }));
  });

  const cross = s('line', { class: 'crosshair', y1: m.t, y2: m.t + ih, visibility: 'hidden' });
  const dots = series.map((x) => s('circle', { r: 4.5, visibility: 'hidden', style: { fill: x.color, stroke: card, strokeWidth: 2 } }));
  const hit = s('rect', { x: m.l - 12, y: 0, width: iw + 24, height: H, fill: 'transparent' });
  svg.append(cross, ...dots, hit);
  let idx = -1;
  const showAt = (i) => {
    idx = Math.max(0, Math.min(pts.length - 1, i));
    const p = pts[idx], x = X(p);
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    series.forEach((se, k) => {
      const v = p.totals[se.label];
      if (v == null) { dots[k].setAttribute('visibility', 'hidden'); return; }
      dots[k].setAttribute('cx', x); dots[k].setAttribute('cy', Y(v)); dots[k].setAttribute('visibility', 'visible');
    });
    const rows = series.filter((se) => p.totals[se.label] != null)
      .map((se) => ({ color: se.color, label: se.label, value: p.totals[se.label] }))
      .sort((a, b) => b.value - a.value);
    const r = svg.getBoundingClientRect();
    const topV = Math.max(0, ...rows.map((rr) => rr.value));
    svg.onHide = () => { cross.setAttribute('visibility', 'hidden'); dots.forEach((dd) => dd.setAttribute('visibility', 'hidden')); idx = -1; };
    showTip(svg, tipRows(p.now ? `Сейчас, ${fmtTime(p.t)}` : whenLabel(p.t), rows, null), r.left + x, r.top + Y(topV));
  };
  const nearest = (clientX) => {
    const px = clientX - svg.getBoundingClientRect().left;
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(X(p) - px) < Math.abs(X(pts[best]) - px)) best = i; });
    return best;
  };
  hit.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse' || e.buttons) showAt(nearest(e.clientX)); });
  hit.addEventListener('pointerdown', (e) => { haptic.sel(); showAt(nearest(e.clientX)); });
  hit.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && tipOwner === svg) hideTip(); });
  svg.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      showAt(idx < 0 ? pts.length - 1 : idx + (e.key === 'ArrowRight' ? 1 : -1));
    }
  });
  svg.addEventListener('blur', () => { if (tipOwner === svg) hideTip(); });
  host.replaceChildren(svg);
}


function digestSection(d, series) {
  const dg = d.digest;
  if (!dg || !dg.items || !dg.items.length) return null;
  const sec = h('section', {}, h('h2', { class: 'section-title' }, 'Выжимка'));
  for (const it of dg.items) {
    const se = series.find((x) => x.label === it.keyword);
    const card = h('article', { class: 'card digest-card' },
      h('header', { class: 'digest-head' },
        h('div', { class: 'digest-kw' }, series.length > 1 ? dot(se && se.color) : null, it.keyword), tonePill(it.tone)),
      h('p', { class: 'digest-summary' }, it.summary));
    if (it.events && it.events.length) {
      card.append(h('h4', {}, 'Ключевые события'), h('ul', { class: 'events' }, it.events.map(eventItem)));
    }
    if (it.factors && it.factors.length) {
      card.append(h('h4', {}, 'Что может влиять'), h('ul', { class: 'factors' }, it.factors.map((f) => h('li', {}, f))));
    }
    sec.append(card);
  }
  const note = [dg.note, 'Это обзор новостного фона, а не инвестиционная рекомендация.'].filter(Boolean).join(' ');
  sec.append(h('p', { class: 'footnote' }, note));
  return sec;
}

function eventItem(ev) {
  let text = ev.text, tone = null;
  const m = /^(Позитив|Негатив):\s*/.exec(text);
  if (m) { tone = m[1] === 'Позитив' ? 'positive' : 'negative'; text = text.slice(m[0].length); }
  const src = (ev.sources || []).join(', ');
  const link = ev.link && /^https?:\/\//i.test(ev.link)
    ? h('button', { class: 'ev-src', onclick: () => openLink(ev.link) }, src || hostOf(ev.link), icon('external'))
    : (src ? h('span', { class: 'ev-src plain' }, src) : null);
  return h('li', {},
    tone ? h('span', { class: 'ev-mark', 'aria-label': TONES[tone].label }, toneMark(tone, 9)) : null,
    h('div', {}, h('p', {}, text), link));
}


function feedItems(d) {
  const map = new Map();
  for (const m of d.materials) {
    const key = (m.link || '') + '|' + m.title;
    let it = map.get(key);
    if (!it) {
      it = { ...m, kws: new Set(), t: m.published ? new Date(m.published) : null };
      map.set(key, it);
    }
    it.kws.add(m.keyword);
  }
  return [...map.values()].sort((a, b) => (b.t ? b.t.getTime() : 0) - (a.t ? a.t.getTime() : 0));
}

function renderFeed() {
  const page = h('div', { class: 'page' });
  const D = S.dash;
  const win = (S.state && S.state.settings.window_hours) || 24;
  if (!D || D.state !== 'ok') {
    page.append(header('Лента', `За последние ${hoursText(win)}`));
    if (!D) page.append(S.dashError ? emptyState('Лента не загрузилась', S.dashError, 'Повторить', () => loadDashboard()) : loading('Собираю материалы…'));
    else page.append(emptyState('Пока пусто', 'Добавьте слова для поиска — здесь появятся найденные материалы со ссылками на оригиналы.', 'Добавить слова', () => go('settings')));
    return page;
  }
  const d = D.data;
  const series = seriesOf(d);
  const colorOf = Object.fromEntries(series.map((x) => [x.label, x.color]));
  const all = feedItems(d);
  if (S.feed.kw !== 'all' && !colorOf[S.feed.kw]) S.feed.kw = 'all';

  const byKw = S.feed.kw === 'all' ? all : all.filter((m) => m.kws.has(S.feed.kw));
  const srcCount = new Map();
  for (const m of byKw) srcCount.set(m.source, (srcCount.get(m.source) || 0) + 1);
  if (S.feed.src !== 'all' && !srcCount.has(S.feed.src)) S.feed.src = 'all';
  const list = S.feed.src === 'all' ? byKw : byKw.filter((m) => m.source === S.feed.src);

  page.append(header('Лента', `${fmt(list.length)} ${plural(list.length, 'материал', 'материала', 'материалов')} за ${hoursText(d.window_hours)}`));

  const setFilter = (patch) => { Object.assign(S.feed, patch, { limit: FEED_PAGE }); haptic.sel(); render(); window.scrollTo(0, 0); };
  const srcSelect = h('select', {
    'aria-label': 'Источник', onchange: (e) => setFilter({ src: e.target.value }),
  }, h('option', { value: 'all' }, 'Все источники'),
  [...srcCount.entries()].sort((a, b) => b[1] - a[1]).map(([name, n]) => h('option', { value: name, selected: name === S.feed.src }, `${name} (${n})`)));
  const filters = h('div', { class: 'filters', role: 'toolbar', 'aria-label': 'Фильтры' },
    h('label', { class: 'chip chip-select' + (S.feed.src !== 'all' ? ' is-on' : '') },
      S.feed.src === 'all' ? 'Все источники' : S.feed.src, icon('updown'), srcSelect),
    series.length > 1 ? h('button', { class: 'chip', 'aria-pressed': S.feed.kw === 'all' ? 'true' : 'false', onclick: () => setFilter({ kw: 'all' }) }, 'Все слова') : null,
    series.length > 1 ? series.map((x) => h('button', {
      class: 'chip', 'aria-pressed': S.feed.kw === x.label ? 'true' : 'false', onclick: () => setFilter({ kw: x.label }),
    }, dot(x.color), x.label)) : null);
  page.append(filters);

  if (!list.length) {
    page.append(emptyState('Ничего не нашлось', 'По выбранным фильтрам материалов за этот период нет.', null));
    return page;
  }

  let day = null, group = null;
  for (const m of list.slice(0, S.feed.limit)) {
    const label = m.t ? dayLabel(m.t) : 'Страницы без даты';
    if (label !== day) {
      day = label;
      group = h('div', { class: 'list' });
      page.append(h('h3', { class: 'day' }, label), group);
    }
    const dots = series.length > 1 ? h('span', { class: 'dots' }, [...m.kws].map((k) => dot(colorOf[k]))) : null;
    group.append(h('a', {
      class: 'feed-row', href: m.link || '#', target: '_blank', rel: 'noopener noreferrer',
      onclick: (e) => { e.preventDefault(); openLink(m.link); },
    },
    h('div', { class: 'feed-title' }, m.title || m.excerpt || 'Без заголовка'),
    h('div', { class: 'feed-meta' }, dots, m.t ? `${m.source}, ${fmtTime(m.t)}` : m.source)));
  }
  if (list.length > S.feed.limit) {
    page.append(h('button', { class: 'more-btn', onclick: () => { S.feed.limit += FEED_PAGE; haptic.tap(); render(); } },
      `Показать ещё ${Math.min(FEED_PAGE, list.length - S.feed.limit)}`));
  }
  const capped = series.some((x) => (d.items[x.label] || 0) > all.filter((m) => m.kws.has(x.label)).length);
  if (capped) page.append(h('p', { class: 'footnote' }, 'С каждого источника показаны 40 самых свежих материалов по каждому слову; в цифрах обзора учтены все.'));
  return page;
}


function group(head, foot, rows) {
  return [
    head ? h('h2', { class: 'group-head' }, head) : null,
    h('div', { class: 'list' }, rows),
    foot ? h('p', { class: 'group-foot' }, foot) : null,
  ].filter(Boolean);
}
function row(...kids) { return h('div', { class: 'row' }, kids); }

function switchEl(checked, label, onchange) {
  return h('input', {
    type: 'checkbox', role: 'switch', class: 'switch', checked, 'aria-label': label,
    onchange: (e) => { haptic.sel(); onchange(e.target.checked); },
  });
}

function addRow(placeholder, type, onAdd) {
  const input = h('input', {
    type, placeholder, enterkeyhint: 'done', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
    'aria-label': placeholder,
  });
  const btn = h('button', { class: 'add-btn', hidden: true }, 'Добавить');
  const submit = () => {
    const v = input.value.trim();
    if (!v) return;
    input.value = '';
    btn.hidden = true;
    input.blur();
    onAdd(v);
  };
  input.addEventListener('input', () => { btn.hidden = !input.value.trim(); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
  btn.addEventListener('pointerdown', (e) => e.preventDefault());
  btn.addEventListener('click', submit);
  return row(input, btn);
}

function variantsText(kw) {
  const v = S.variants[kw];
  if (!v || v.length < 2) return null;
  const rest = v.slice(1);
  const shown = rest.slice(0, 6).join(', ');
  return `Ищу также: ${shown}${rest.length > 6 ? ` и ещё ${rest.length - 6}` : ''}`;
}

function renderSettings() {
  const page = h('div', { class: 'page' });
  page.append(header('Настройки', 'Те же, что в меню бота'));
  const st = S.state;
  if (!st) {
    page.append(S.stateError ? emptyState('Настройки не загрузились', S.stateError, 'Повторить', () => loadState()) : loading('Загружаю…'));
    return page;
  }
  const set = st.settings, lim = st.limits;

  const kwRows = set.keywords.map((kw, i) => row(
    h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, kw),
      variantsText(kw) ? h('div', { class: 'row-sub' }, variantsText(kw)) : null),
    h('button', {
      class: 'icon-btn', 'aria-label': `Удалить «${kw}»`,
      onclick: () => { haptic.tap(); save({ keywords: set.keywords.filter((_, j) => j !== i) }, { okText: `«${kw}» больше не ищу` }); },
    }, icon('remove'))));
  if (set.keywords.length < lim.max_keywords) {
    kwRows.push(addRow(set.keywords.length ? 'Добавить слово' : 'Например: Сбер, клод', 'text', (v) => {
      const words = v.split(/[,;\n]+/).map((x) => x.trim()).filter(Boolean);
      save({ keywords: [...set.keywords, ...words] }, { okText: words.length > 1 ? 'Слова добавлены' : `Ищу «${words[0]}»` });
    }));
  }
  const kwFoot = (S.variantsEnabled
    ? 'Варианты написания и связанные названия подбираю сам. Свои — через «/», '
    : 'Варианты написания знаю для частых тем, для остальных впишите их через «/», ') +
    `например: клод / claude. До ${lim.max_keywords} слов.`;
  page.append(...group('Слова для поиска', kwFoot, kwRows));

  page.append(...group('Период поиска', 'После смены периода динамика по проверкам начнётся заново.', [row(
    h('div', { class: 'seg-ctl', role: 'group', 'aria-label': 'Период поиска' }, [[12, '12 часов'], [24, '24 часа']].map(([v, label]) =>
      h('button', {
        'aria-pressed': set.window_hours === v ? 'true' : 'false',
        onclick: () => { if (set.window_hours !== v) { haptic.sel(); save({ window_hours: v }, { okText: `Ищу за последние ${hoursText(v)}` }); } },
      }, label))))]));

  const presets = AUTO_PRESETS.includes(set.auto_every_min) ? AUTO_PRESETS : [...AUTO_PRESETS, set.auto_every_min].sort((a, b) => a - b);
  const autoRows = [row(h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, 'Искать автоматически')),
    switchEl(set.auto, 'Искать автоматически', (v) => save({ auto: v }, { okText: v ? `Буду искать раз в ${humanEvery(set.auto_every_min)}` : 'Автообновление выключено' })))];
  if (set.auto) {
    autoRows.push(row(h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, 'Как часто')),
      h('label', { class: 'select-wrap' }, h('span', { class: 'row-value' }, `раз в ${humanEvery(set.auto_every_min)}`), icon('updown'),
        h('select', {
          'aria-label': 'Как часто искать',
          onchange: (e) => save({ auto_every_min: Number(e.target.value) }, { okText: `Буду искать раз в ${humanEvery(Number(e.target.value))}` }),
        }, presets.map((m) => h('option', { value: String(m), selected: m === set.auto_every_min }, `раз в ${humanEvery(m)}`))))));
  }
  const autoFoot = set.auto
    ? `Отчёт придёт в чат с ботом${set.email ? ` и на ${set.email}` : ''}.`
    : 'Бот сам повторит поиск и пришлёт отчёт в чат.';
  page.append(...group('Автообновление', autoFoot, autoRows));

  const catalogUrls = new Set(st.catalog.map((c) => c.url));
  const on = set.sites.filter((u) => catalogUrls.has(u)).length;
  const custom = set.sites.length - on;
  page.append(...group('Источники', null, [h('button', {
    class: 'row row-nav', onclick: () => { haptic.sel(); S.sub = 'sources'; render(); },
  }, h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, 'Сайты для поиска')),
  h('span', { class: 'row-value' }, `${on} из ${st.catalog.length}${custom ? ` + ${custom}` : ''}`), icon('chevron'))]));

  const email = h('input', {
    type: 'email', inputmode: 'email', autocomplete: 'email', enterkeyhint: 'done', placeholder: 'name@example.com',
    value: set.email, 'aria-label': 'Почта для отчётов',
  });
  const saveEmail = () => {
    const v = email.value.trim();
    if (v === set.email) return;
    save({ email: v }, { okText: v ? 'Почта сохранена' : 'Почта удалена' });
  };
  email.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); email.blur(); } });
  email.addEventListener('change', saveEmail);
  const emailFoot = st.email_enabled
    ? 'Сюда придут автоотчёты и отчёты по кнопке «На почту».'
    : 'Адрес сохранится, но отправка писем на сервере пока не настроена.';
  page.append(...group('Почта для отчётов', emailFoot, [row(email)]));

  const report = async (withEmail) => {
    haptic.tap();
    try {
      await api('report', { email: withEmail });
      haptic.ok();
      toast(withEmail ? `Готовлю отчёт — отправлю на ${set.email} и в чат` : 'Готовлю отчёт — пришлю в чат');
    } catch (e) {
      haptic.err();
      toast(e.message);
    }
  };
  const canReport = set.keywords.length && set.sites.length;
  page.append(...group('Отчёт', 'Карточка с графиком и выжимкой — как по кнопке «Искать» в боте.', [
    h('button', { class: 'row row-action', disabled: !canReport, onclick: () => report(false) }, 'Прислать отчёт в чат'),
    h('button', { class: 'row row-action', disabled: !(canReport && set.email && st.email_enabled), onclick: () => report(true) }, 'Отправить на почту'),
  ]));
  return page;
}

function renderSources() {
  const page = h('div', { class: 'page' });
  const st = S.state;
  if (!st) { page.append(loading('Загружаю…')); return page; }
  const set = st.settings;
  const sites = new Set(set.sites);
  const on = st.catalog.filter((c) => sites.has(c.url)).length;
  page.append(header('Источники', `Включено ${on} из ${st.catalog.length}`));

  const toggle = (url, value) => {
    const next = value ? [...set.sites, url] : set.sites.filter((u) => u !== url);
    set.sites = next;
    save({ sites: next });
  };
  const groups = [];
  for (const c of st.catalog) {
    let g = groups.find((x) => x.name === c.group);
    if (!g) groups.push((g = { name: c.group, items: [] }));
    g.items.push(c);
  }
  for (const g of groups) {
    page.append(...group(g.name, null, g.items.map((c) => row(
      h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, c.name), h('div', { class: 'row-sub' }, hostOf(c.url))),
      switchEl(sites.has(c.url), c.name, (v) => toggle(c.url, v))))));
  }

  const catalogUrls = new Set(st.catalog.map((c) => c.url));
  const custom = set.sites.filter((u) => !catalogUrls.has(u));
  const customRows = custom.map((u) => row(
    h('div', { class: 'row-main' }, h('div', { class: 'row-title' }, hostOf(u)), h('div', { class: 'row-sub' }, u)),
    h('button', {
      class: 'icon-btn', 'aria-label': `Удалить ${hostOf(u)}`,
      onclick: () => { haptic.tap(); save({ sites: set.sites.filter((x) => x !== u) }, { okText: `${hostOf(u)} удалён` }); },
    }, icon('remove'))));
  if (custom.length < st.limits.max_custom_sites) {
    customRows.push(addRow('Адрес сайта или RSS', 'url', (v) => save({ add_sites: v }, {
      okText: (res) => (res.added && res.added.length ? `Добавлено: ${res.added.join(', ')}` : 'Этот источник уже включён'),
    })));
  }
  page.append(...group('Свои сайты', 'Можно вставить адрес сайта или RSS-ленты, несколько — через запятую. Если у сайта нет RSS, проверю его текущую страницу.', customRows));

  page.append(...group(null, null, [
    h('button', {
      class: 'row row-action', disabled: on === st.catalog.length,
      onclick: () => { haptic.tap(); save({ sites: [...set.sites, ...st.catalog.map((c) => c.url).filter((u) => !sites.has(u))] }, { okText: 'Все источники включены' }); },
    }, 'Включить все'),
    h('button', {
      class: 'row row-action danger', disabled: on === 0,
      onclick: () => { haptic.tap(); save({ sites: set.sites.filter((u) => !catalogUrls.has(u)) }, { okText: 'Источники из каталога выключены' }); },
    }, 'Выключить все из каталога'),
  ]));
  return page;
}


function applyTheme() {
  const dark = tg ? tg.colorScheme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  if (!tg) return;
  try {
    tg.setHeaderColor('secondary_bg_color');
    tg.setBackgroundColor('secondary_bg_color');
    if (tg.isVersionAtLeast && tg.isVersionAtLeast('7.10')) tg.setBottomBarColor('secondary_bg_color');
  } catch {}
}

function renderOutside() {
  applyTheme();
  $view.replaceChildren(h('div', { class: 'page' }, emptyState('Откройте в Telegram',
    'Это приложение бота KeyScout. Откройте чат с ботом и нажмите кнопку «Обзор» рядом с полем ввода.', null), credit()));
}

function boot() {
  if (!tg || !tg.initData) { renderOutside(); return; }
  tg.ready();
  tg.expand();
  try { if (tg.isVersionAtLeast('7.7')) tg.disableVerticalSwipes(); } catch {}
  applyTheme();
  tg.onEvent('themeChanged', () => { applyTheme(); render({ background: true }); });
  if (tg.BackButton) tg.BackButton.onClick(() => { S.sub = null; haptic.sel(); render(); });
  buildTabbar();
  render();
  loadState();
  loadDashboard();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && Date.now() - S.dashAt > 5 * 60 * 1000 && S.tab !== 'settings') loadDashboard();
  });
}

boot();
