/* Piyasa Masası — single-page UI.
   Vanilla JS (ES2020), no build step, no external requests. Talks to the Flask JSON API under /api.
   Pure helpers/renderers are exported for node tests (module.exports) — the browser ignores that. */
(function () {
'use strict';

const HAS_DOM = typeof document !== 'undefined';

// ============================================================ constants

const TZ_ET = 'America/New_York';
const TZ_TR = 'Europe/Istanbul';
const NAV_KEYS = ['haberler', 'kim-ne-diyor', 'firsatlar', 'pano', 'takvim', 'brifingler', 'hisse', 'ayarlar', 'islem'];
// Seconds. Page polls live on each page object (news 30 s, market 60 s ...).
const POLL = { status: 15, alerts: 20, badges: 30, opps: 60, sources: 120, config: 300, meta: 600, clusters: 90 };

const SESSION_LABELS = { premarket: 'Piyasa öncesi', open: 'Açık', afterhours: 'Piyasa sonrası', closed: 'Kapalı' };
const SENT_LABELS = { pos: 'Olumlu', neg: 'Olumsuz', neu: 'Nötr' };
const STYPE_LABELS = { news: 'Haber', press: 'Basın bülteni', filing: 'Resmi başvuru (SEC)', official: 'Resmi kurum', social: 'Sosyal medya', api: 'Veri API' };
const GROUP_LABELS = { media: 'Ana akım medya', press: 'Basın bültenleri', official: 'Resmi kurumlar', aggregator: 'Toplayıcılar', broker: "Veri API'leri", social: 'Sosyal medya', data: 'Piyasa verisi ve takvim' };
const GROUP_ORDER = ['media', 'press', 'official', 'aggregator', 'broker', 'social', 'data'];
const QUOTE_GROUPS = [['vadeli', 'Vadeli işlemler'], ['endeks', 'Endeksler'], ['faiz', 'Faiz'], ['doviz', 'Döviz'], ['emtia', 'Emtia'], ['kripto', 'Kripto'], ['diger', 'Diğer']];
const BRIEF_KINDS = { sabah: 'Sabah', ogle: 'Öğle', aksam: 'Akşam', manual: 'Manuel' };
const AUTHORS = { kural: 'Kural', ollama: 'Ollama', claude: 'Claude' };
const DIRS = { long: 'Yükseliş', short: 'Düşüş', mixed: 'Karışık' };
const IMPACT = { high: 'Yüksek', medium: 'Orta', low: 'Düşük' };
const EARN_TIME = { bmo: 'Açılış öncesi', dmh: 'Seans içinde', amc: 'Kapanış sonrası', unknown: 'Saat belirsiz' };
// Alerts: stories published longer ago than this are marked seen silently (first-pass backlog, replay
// after the window was hidden). Toasts are also held back while the scheduler warms up.
const ALERT_MAX_AGE = 2 * 3600;
const ALERT_WARMUP = 180;
const FG_RATING = { 'extreme fear': 'Aşırı korku', fear: 'Korku', neutral: 'Nötr', greed: 'Açgözlülük', 'extreme greed': 'Aşırı açgözlülük' };
const MARKET_STATE = { PRE: 'Seans öncesi', PREPRE: 'Seans öncesi', REGULAR: 'Seans açık', POST: 'Seans sonrası', POSTPOST: 'Seans sonrası', CLOSED: 'Kapalı' };
const REQ_LABELS = { alpaca_key_id: 'Alpaca Key ID', alpaca_secret: 'Alpaca Secret', finnhub_key: 'Finnhub anahtarı', sec_user_agent: 'SEC kimliği (ad + e-posta)' };
const CFG_LABELS = Object.assign({}, REQ_LABELS, { ollama_url: 'Ollama adresi', ollama_model: 'Ollama modeli', use_finbert: 'FinBERT', watchlist: 'İzleme listesi', brief_times_et: 'Brifing saatleri', alert_min_importance: 'Uyarı eşiği' });
const KNOWN_CFG = new Set(['alpaca_key_id', 'alpaca_secret', 'finnhub_key', 'sec_user_agent', 'ollama_url', 'ollama_model', 'use_finbert', 'watchlist', 'disabled_sources', 'brief_times_et', 'alert_min_importance', 'port', 'data_dir', 'claude_in_dir', 'claude_out_dir']);
const SECRET_KEYS = ['alpaca_key_id', 'alpaca_secret', 'finnhub_key'];
const MONTHS = ['Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran', 'Temmuz', 'Ağustos', 'Eylül', 'Ekim', 'Kasım', 'Aralık'];
const DAYS = ['Pazar', 'Pazartesi', 'Salı', 'Çarşamba', 'Perşembe', 'Cuma', 'Cumartesi'];
const FG_BANDS = [[0, 25, '#ef4444', 'Aşırı korku'], [25, 45, '#f97316', 'Korku'], [45, 55, '#eab308', 'Nötr'], [55, 75, '#84cc16', 'Açgözlülük'], [75, 100, '#22c55e', 'Aşırı açgözlülük']];

const ICON = {
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4.5H15"/>',
  columns: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M9.2 5v14M14.8 5v14"/>',
  warn: '<path d="M12 4 21 20H3z"/><path d="M12 10v4.5M12 17.3v.2"/>',
  star: '<path d="m12 4 2.4 5 5.4.6-4 3.7 1.1 5.4L12 16l-4.9 2.7 1.1-5.4-4-3.7 5.4-.6z"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  bell: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  play: '<path d="M8 5.5v13l10-6.5z"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  check: '<path d="m5 12 4.5 4.5L19 7"/>',
  chevDown: '<path d="m6 9 6 6 6-6"/>',
  chevUp: '<path d="m6 15 6-6 6 6"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  power: '<path d="M12 3.5v8"/><path d="M6.6 6.9a7.5 7.5 0 1 0 10.8 0"/>',
};
function icon(n, cls) { return `<svg class="ico${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" aria-hidden="true">${ICON[n] || ''}</svg>`; }

// ============================================================ storage (per-viewer conveniences only)

const store = {
  get(k, d) { try { const v = localStorage.getItem('pm.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('pm.' + k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } },
};

// ============================================================ small helpers

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(v) { return v == null ? '' : String(v).replace(/[&<>"']/g, c => ESC_MAP[c]); }
function arr(v) { return Array.isArray(v) ? v : []; }
function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
function num(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string') {
    const t = v.replace(/[$,%\s]/g, '');
    if (t === '' || t === '-' || t === '.') return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}
function clamp(v, a, b) { return Math.min(b, Math.max(a, v)); }
function pad2(n) { return String(n).padStart(2, '0'); }
function trunc(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function safeUrl(u) { u = String(u || '').trim(); return /^https?:\/\/\S+$/i.test(u) ? u : ''; }
function safeColor(c) {
  c = String(c || '').trim();
  return /^(#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%/+-]+\)|var\(--[\w-]+\)|[a-z]{3,20})$/i.test(c) ? c : '';
}
function hue(s) { s = String(s || ''); let h = 7; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h % 360; }
function groupBy(list, fn) { const m = {}; for (const x of list) { const k = fn(x); (m[k] || (m[k] = [])).push(x); } return m; }
function pick(o, keys) { const r = {}; for (const k of keys) if (o && o[k] !== undefined) r[k] = o[k]; return r; }
function normSym(s) {
  s = String(s || '').trim().toUpperCase().replace(/^\$/, '');
  return /^[A-Z0-9][A-Z0-9.\-^=]{0,11}$/.test(s) ? s : '';
}
/** Case/diacritic-insensitive search key. Turkish lower-casing turns "I" into "ı", so fold ı → i
    (otherwise "inflation" never matches "Inflation"), then strip accents (İşten → isten). */
function fold(s) {
  return String(s == null ? '' : s).toLocaleLowerCase('tr').replace(/ı/g, 'i').normalize('NFD').replace(/[̀-ͯ]/g, '');
}
const _deb = {};
function debounce(key, fn, ms) { clearTimeout(_deb[key]); _deb[key] = setTimeout(fn, ms); }
function byId(id) { return HAS_DOM ? document.getElementById(id) : null; }
function setHtml(id, html) { const el = byId(id); if (el) el.innerHTML = html; return el; }
function setText(id, t) { const el = byId(id); if (el && el.textContent !== t) el.textContent = t; }

// ============================================================ number formatting (tr-TR: 1.234,56)

const _nf = {};
function nf(min, max, compact) {
  const k = min + '|' + max + '|' + (compact ? 'c' : '');
  if (!_nf[k]) {
    const o = { minimumFractionDigits: min, maximumFractionDigits: max };
    if (compact) o.notation = 'compact';
    _nf[k] = new Intl.NumberFormat('tr-TR', o);
  }
  return _nf[k];
}
function fmtNum(v, d = 2) { v = num(v); return v == null ? '—' : nf(d, d).format(v); }
function fmtInt(v) { v = num(v); return v == null ? '—' : nf(0, 0).format(Math.round(v)); }
function fmtPrice(v, dd) {
  v = num(v);
  if (v == null) return '—';
  const a = Math.abs(v);
  const d = dd != null ? dd : a === 0 || a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
  return nf(d, d).format(v);
}
/** FX pairs (EUR/USD 1,1204) need 4 decimals; indices such as DX-Y.NYB keep 2. */
function isFx(q) { return String(obj(q).instrument || '').toUpperCase() === 'CURRENCY'; }
function fmtSigned(v, d = 2) {
  v = num(v);
  if (v == null) return '—';
  return (v > 0 ? '+' : v < 0 ? '−' : '') + nf(d, d).format(Math.abs(v));
}
function fmtPct(v, d = 2) {
  v = num(v);
  if (v == null) return '—';
  return (v > 0 ? '+' : v < 0 ? '−' : '') + '%' + nf(d, d).format(Math.abs(v));
}
function fmtCompact(v) { v = num(v); return v == null ? '—' : nf(0, 1, true).format(v); }
function fmtCap(v) {
  const n = num(v);
  if (n == null) return v ? esc(String(v)) : '—';
  return '$' + fmtCompact(n);
}
function fmtEps(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? (v < 0 ? '−$' : '$') + nf(2, 2).format(Math.abs(v)) : '—';
  return v ? esc(String(v)) : '—';
}
function fmtInterval(s) {
  s = num(s);
  if (!s) return '—';
  if (s < 60) return `${Math.round(s)} sn`;
  if (s < 3600) return `${Math.round(s / 60)} dk`;
  return `${nf(0, 1).format(s / 3600)} sa`;
}
function dirClass(v) { v = num(v); return v == null || v === 0 ? 'flat' : v > 0 ? 'up' : 'down'; }

// ============================================================ time (TR display, ET secondary)

const S = {
  meta: { categories: {}, groups: {}, source_types: {}, sentiments: {} },
  metaLoaded: false,
  status: null, statusAt: 0, online: true, lastErr: '',
  config: null,
  sources: [], sourcesAt: 0,
  clusterCount: {}, clusterAt: 0,
  opps: null,
  market: null,
  skewMs: 0,
  lastData: 0,
  newsSeen: num(store.get('newsSeen', 0)) || 0,
  alertSince: 0,
  alertSeen: new Set(),
  alertClusters: new Set(),
  focusSec: false,
  gsearch: null, // {sym, text}: global-search word sent to the ticker page (news-search fallback when empty)
  prefs: Object.assign({ notify: false, beep: false }, obj(store.get('prefs', {}))),
};

function nowSec() { return (Date.now() + S.skewMs) / 1000; }
const _dtf = {};
function dtf(tz) {
  if (!_dtf[tz]) {
    _dtf[tz] = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short',
    });
  }
  return _dtf[tz];
}
const WD = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
function zpSlow(ts, tz) {
  const o = {};
  for (const p of dtf(tz).formatToParts(new Date(ts * 1000))) o[p.type] = p.value;
  return { y: +o.year, mo: +o.month, d: +o.day, h: (+o.hour) % 24, mi: +o.minute, s: +o.second, wd: WD[o.weekday] ?? 0 };
}
// UTC offset per (zone, UTC hour): ET/TR change offset only on hour boundaries, so this is exact and
// keeps 500-row renders cheap (formatToParts is slow).
const _offCache = {};
function tzOff(ts, tz) {
  const h0 = Math.floor(ts / 3600) * 3600;
  const m = _offCache[tz] || (_offCache[tz] = new Map());
  let v = m.get(h0);
  if (v === undefined) {
    const p = zpSlow(h0, tz);
    v = Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) / 1000 - h0;
    if (m.size > 20000) m.clear();
    m.set(h0, v);
  }
  return v;
}
/** Wall-clock parts of unix ts in a time zone. */
function zp(ts, tz) {
  const d = new Date((Math.floor(ts) + tzOff(ts, tz)) * 1000);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds(), wd: d.getUTCDay() };
}
function zOffset(ts, tz) { const p = zp(ts, tz); return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) / 1000 - Math.floor(ts); }
/** Unix ts of a wall-clock time in a time zone. */
function zTs(y, mo, d, h, mi, tz) {
  const g = Date.UTC(y, mo - 1, d, h, mi) / 1000;
  const t = g - zOffset(g, tz);
  return g - zOffset(t, tz);
}
function hm(ts, tz = TZ_TR) { const p = zp(ts, tz); return pad2(p.h) + ':' + pad2(p.mi); }
function hms(ts, tz = TZ_TR) { const p = zp(ts, tz); return pad2(p.h) + ':' + pad2(p.mi) + ':' + pad2(p.s); }
function dmy(ts, tz = TZ_TR) { const p = zp(ts, tz); return `${pad2(p.d)}.${pad2(p.mo)}.${p.y}`; }
function dayDiff(ts, tz = TZ_TR) {
  const a = zp(ts, tz), b = zp(nowSec(), tz);
  return Math.round((Date.UTC(a.y, a.mo - 1, a.d) - Date.UTC(b.y, b.mo - 1, b.d)) / 86400000);
}
function dayName(ts, tz = TZ_TR) { const p = zp(ts, tz); return `${p.d} ${MONTHS[p.mo - 1]} ${DAYS[p.wd]}`; }
function dayLabel(ts, tz = TZ_TR) {
  const dd = dayDiff(ts, tz), base = dayName(ts, tz);
  return dd === 0 ? 'Bugün · ' + base : dd === 1 ? 'Yarın · ' + base : dd === -1 ? 'Dün · ' + base : base;
}
/** Compact row time: "14:32" today, "Dün 14:32", "22.10 14:32". */
function whenShort(ts) {
  ts = num(ts);
  if (!ts) return '—';
  const dd = dayDiff(ts), t = hm(ts);
  if (dd === 0) return t;
  if (dd === -1) return 'Dün ' + t;
  const p = zp(ts, TZ_TR);
  return `${pad2(p.d)}.${pad2(p.mo)} ${t}`;
}
function tsTitle(ts) {
  ts = num(ts);
  return ts ? `TR ${dmy(ts)} ${hm(ts)} · ET ${dmy(ts, TZ_ET)} ${hm(ts, TZ_ET)}` : '';
}
function rel(ts) {
  ts = num(ts);
  if (!ts) return '—';
  const d = nowSec() - ts;
  if (d < -60) {
    const f = -d;
    return f < 3600 ? `${Math.max(1, Math.floor(f / 60))} dk sonra` : f < 86400 ? `${Math.floor(f / 3600)} sa sonra` : `${Math.floor(f / 86400)} gün sonra`;
  }
  if (d < 45) return 'az önce';
  if (d < 3600) return `${Math.max(1, Math.floor(d / 60))} dk önce`;
  if (d < 86400) return `${Math.floor(d / 3600)} sa önce`;
  return `${Math.floor(d / 86400)} gün önce`;
}
function agoMs(ms) {
  if (!ms) return '—';
  const s = (Date.now() - ms) / 1000;
  if (s < 5) return 'şimdi';
  if (s < 60) return `${Math.floor(s)} sn önce`;
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  return hm(ms / 1000);
}
function dur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d} gün ${h} sa`;
  if (h) return `${h} sa ${pad2(m)} dk`;
  if (m) return `${m} dk ${pad2(s)} sn`;
  return `${s} sn`;
}
function hoursLabel(h) { h = num(h) || 24; return h < 24 ? `${h} saat` : `${Math.round(h / 24)} gün`; }
/** "08:30" ET today → "15:30" TR. */
function etToTr(v) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(v || ''));
  if (!m) return '';
  const p = zp(nowSec(), TZ_ET);
  return hm(zTs(p.y, p.mo, p.d, +m[1], +m[2], TZ_ET)) + ' TR';
}

// ---- market session (server status is the truth; boundaries are ticked locally between polls)

function localSession(now) {
  const p = zp(now, TZ_ET), m = p.h * 60 + p.mi;
  if (p.wd === 0 || p.wd === 6) return 'closed';
  return m >= 240 && m < 570 ? 'premarket' : m >= 570 && m < 960 ? 'open' : m >= 960 && m < 1200 ? 'afterhours' : 'closed';
}
function effSession(now) {
  const st = S.status;
  if (!st || !st.session || Date.now() - S.statusAt > 180000) return localSession(now);
  const p = zp(now, TZ_ET), m = p.h * 60 + p.mi;
  let s = st.session;
  // session_end_ts: end of the current part as the server computed it (13:00 / 17:00 ET on early-close days).
  const end = num(st.session_end_ts);
  if (end && now >= end) s = { premarket: 'open', open: 'afterhours', afterhours: 'closed' }[s] || s;
  else if (!end) {
    if (s === 'premarket' && m >= 570 && m < 960) s = 'open';
    if (s === 'open' && m >= 960) s = 'afterhours';
    if (s === 'afterhours' && m >= 1200) s = 'closed';
  }
  return SESSION_LABELS[s] ? s : 'closed';
}
function sessLabel(s) {
  const st = S.status;
  return st && st.session === s && st.session_label ? String(st.session_label) : ml('sessions', s, SESSION_LABELS);
}
function guessNextOpen(now) {
  const p = zp(now, TZ_ET);
  for (let k = 0; k < 8; k++) {
    const day = new Date(Date.UTC(p.y, p.mo - 1, p.d + k));
    const wd = day.getUTCDay();
    if (wd === 0 || wd === 6) continue;
    const ts = zTs(day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), 9, 30, TZ_ET);
    if (ts > now) return ts;
  }
  return null;
}
/** Countdown targets for the session: [{label, ts}]. `sessionEnd`: server's end of the current part (status.session_end_ts). */
function sessionTargets(sess, now, nextOpen, nextClose, sessionEnd) {
  const p = zp(now, TZ_ET);
  const at = (h, m) => zTs(p.y, p.mo, p.d, h, m, TZ_ET);
  const nOpen = nextOpen && nextOpen > now ? nextOpen : guessNextOpen(now);
  const end = sessionEnd && sessionEnd > now ? sessionEnd : null;
  if (sess === 'premarket') { const o = end || at(9, 30); return [{ label: 'Açılışa', ts: o > now ? o : nOpen }]; }
  if (sess === 'open') { const c = at(16, 0); return [{ label: 'Kapanışa', ts: end && end <= c ? end : nextClose && nextClose > now && nextClose <= c ? nextClose : c }]; }
  if (sess === 'afterhours') return [{ label: 'Seans sonrası bitişine', ts: end || at(20, 0) }, { label: 'Açılışa', ts: nOpen }].filter(x => x.ts);
  return nOpen ? [{ label: 'Açılışa', ts: nOpen }] : [];
}

function curTargets(sess, now) {
  const st = obj(S.status);
  // session_end_ts belongs to the session the server reported; after a local roll-over it no longer applies.
  return sessionTargets(sess, now, num(st.next_open_ts), num(st.next_close_ts), st.session === sess ? num(st.session_end_ts) : null);
}

// ============================================================ markdown (small & safe: text is always escaped)

const RE_FENCE = /^\s{0,3}(```|~~~)/;
const RE_HEAD = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RE_HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const RE_LI = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const RE_QUOTE = /^\s{0,3}>/;

function mdHref(u) {
  const raw = String(u).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  if (/^(https?:\/\/|mailto:)\S+$/i.test(raw) || /^#[\w\-/.%]*$/.test(raw)) return esc(raw);
  return '';
}
function mdEmph(s) {
  return s
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?=[^\w]|$)/g, '$1<strong>$2</strong>')
    .replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])?\*(?=[^*\w]|$)/g, (m, a, b) => (b === undefined ? m : `${a}<em>${b}</em>`))
    .replace(/(^|[^\w])_(?=\S)([^_]*?\S)_(?=[^\w]|$)/g, '$1<em>$2</em>')
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
}
function mdInline(src) {
  const slots = [];
  const keep = html => '\u0000' + (slots.push(html) - 1) + '\u0000';
  let s = esc(src);
  s = s.replace(/`([^`\n]+)`/g, (m, c) => keep(`<code>${c}</code>`));
  s = s.replace(/\[([^\]\n]+)\]\(\s*([^)\s]+)(?:\s+&quot;[^\n]*?&quot;)?\s*\)/g, (m, text, url) => {
    const href = mdHref(url);
    if (!href) return text;
    const ext = href.charAt(0) !== '#';
    return keep(`<a href="${href}"${ext ? ' target="_blank" rel="noopener noreferrer"' : ''}>${mdEmph(text)}</a>`);
  });
  s = s.replace(/&lt;((?:https?:\/\/|mailto:)[^\s]+?)&gt;/g, (m, url) => {
    const href = mdHref(url);
    return href ? keep(`<a href="${href}" target="_blank" rel="noopener noreferrer">${url}</a>`) : m;
  });
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<\u0000]*[^\s<\u0000.,;:!?)\]'"*_])/g, (m, pre, url) => {
    const href = mdHref(url);
    return href ? pre + keep(`<a href="${href}" target="_blank" rel="noopener noreferrer">${url}</a>`) : m;
  });
  s = mdEmph(s);
  s = s.replace(/(^|[\s(>])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![\w])/g, '$1<button type="button" class="tk" data-tk="$2">$$$2</button>');
  for (let k = 0; k < 3 && s.indexOf('\u0000') >= 0; k++) s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => slots[+i] || '');
  return s;
}
function mdSplitRow(l) {
  let t = String(l).trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
  return t.split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, '|'));
}
function mdIsSep(l) {
  if (!l || l.indexOf('|') < 0 || l.indexOf('-') < 0) return false;
  const cells = mdSplitRow(l);
  return cells.length > 0 && cells.every(c => /^:?-+:?$/.test(c));
}
function mdTable(lines, i) {
  const head = mdSplitRow(lines[i]);
  const aligns = mdSplitRow(lines[i + 1]).map(c => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : ''));
  i += 2;
  const rows = [];
  while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) rows.push(mdSplitRow(lines[i++]));
  const al = k => (aligns[k] ? ` style="text-align:${aligns[k]}"` : '');
  const html = `<div class="md-table"><table><thead><tr>${head.map((c, k) => `<th${al(k)}>${mdInline(c)}</th>`).join('')}</tr></thead>`
    + `<tbody>${rows.map(r => `<tr>${head.map((_, k) => `<td${al(k)}>${mdInline(r[k] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  return { html, next: i };
}
function mdIndent(s) { return String(s || '').replace(/\t/g, '    ').length; }
function mdList(lines, i) {
  const first = lines[i].match(RE_LI);
  const base = mdIndent(first[1]);
  const ordered = /\d/.test(first[2]);
  const start = ordered ? parseInt(first[2], 10) : 1;
  const items = [];
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      const m2 = j < lines.length ? lines[j].match(RE_LI) : null;
      if (m2 && mdIndent(m2[1]) >= base && (mdIndent(m2[1]) > base + 1 || /\d/.test(m2[2]) === ordered)) { i = j; continue; }
      break;
    }
    const m = line.match(RE_LI);
    if (m) {
      const ind = mdIndent(m[1]);
      if (ind < base) break;
      if (ind <= base + 1) {
        if (/\d/.test(m[2]) !== ordered) break;
        items.push({ text: [m[3]], sub: '' });
        i++;
        continue;
      }
      const r = mdList(lines, i);
      if (!items.length) items.push({ text: [], sub: '' });
      items[items.length - 1].sub += r.html;
      i = r.next;
      continue;
    }
    if (!items.length) break;
    if (RE_HEAD.test(line) || RE_HR.test(line) || RE_FENCE.test(line) || RE_QUOTE.test(line) || (line.indexOf('|') >= 0 && mdIsSep(lines[i + 1]))) break;
    items[items.length - 1].text.push(line.trim());
    i++;
  }
  const tag = ordered ? 'ol' : 'ul';
  const html = `<${tag}${ordered && start !== 1 ? ` start="${start}"` : ''}>`
    + items.map(it => `<li>${mdInline(it.text.join(' '))}${it.sub}</li>`).join('') + `</${tag}>`;
  return { html, next: i };
}
/** Markdown → safe HTML: headings, bold/italic/strike, links (target=_blank), lists, tables, code, quotes, hr. */
function md(src) {
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [];
  const flush = () => {
    if (!para.length) return;
    const parts = para.map((l, k) => {
      const hard = /( {2,}|\\)$/.test(l);
      const text = mdInline(l.replace(/\\$/, '').trim());
      return text + (k < para.length - 1 ? (hard ? '<br>' : ' ') : '');
    });
    out.push('<p>' + parts.join('') + '</p>');
    para = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { flush(); i++; continue; }
    let m = line.match(RE_FENCE);
    if (m) {
      flush();
      const fence = m[1], buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if ((m = line.match(RE_HEAD))) { flush(); const lv = m[1].length; out.push(`<h${lv}>${mdInline(m[2])}</h${lv}>`); i++; continue; }
    if (RE_HR.test(line)) { flush(); out.push('<hr>'); i++; continue; }
    if (line.indexOf('|') >= 0 && mdIsSep(lines[i + 1])) { flush(); const r = mdTable(lines, i); out.push(r.html); i = r.next; continue; }
    if (RE_QUOTE.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && RE_QUOTE.test(lines[i])) buf.push(lines[i++].replace(/^\s{0,3}>\s?/, ''));
      out.push(`<blockquote>${md(buf.join('\n'))}</blockquote>`);
      continue;
    }
    if (RE_LI.test(line)) { flush(); const r = mdList(lines, i); out.push(r.html); i = Math.max(r.next, i + 1); continue; }
    para.push(line);
    i++;
  }
  flush();
  return out.join('\n');
}

// ============================================================ meta lookups

function catInfo(k) {
  const c = obj(S.meta.categories)[k];
  if (c && typeof c === 'object') return { label: String(c.label || k), color: safeColor(c.color) || 'var(--neu)', bias: num(c.bias) || 0 };
  if (typeof c === 'string') return { label: c, color: 'var(--neu)', bias: 0 };
  return { label: String(k), color: 'var(--neu)', bias: 0 };
}
function sentLabel(k) {
  const s = obj(S.meta.sentiments)[k];
  if (typeof s === 'string' && s) return s;
  if (s && typeof s === 'object' && s.label) return String(s.label);
  return SENT_LABELS[k] || SENT_LABELS.neu;
}
function labelOf(map, k, fallback) {
  const v = obj(map)[k];
  if (typeof v === 'string' && v) return v;
  if (v && typeof v === 'object' && v.label) return String(v.label);
  return fallback[k] || (k ? String(k) : '');
}
function groupLabel(k) { return labelOf(S.meta.groups, k, GROUP_LABELS) || 'Diğer'; }
function shortGroup(k) { return groupLabel(k).replace(/\s*\(.*\)\s*$/, ''); }
function stypeLabel(k) { return labelOf(S.meta.source_types, k, STYPE_LABELS); }
/** Label from an optional /api/meta map (directions, brief_kinds, impacts, …) with a local fallback. */
function ml(name, k, fallback) { return labelOf(S.meta[name], k, fallback); }
function mapOf(name, fallback) { const m = Object.assign({}, fallback); for (const k in obj(S.meta[name])) m[k] = ml(name, k, fallback); return m; }
function groupOrder(keys) {
  const known = Object.keys(obj(S.meta.groups));
  const order = [...GROUP_ORDER, ...known.filter(k => !GROUP_ORDER.includes(k))];
  return [...keys].sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || String(a).localeCompare(String(b));
  });
}
function wlSet() { return new Set(arr(obj(S.config).watchlist).map(s => String(s).toUpperCase())); }

// ============================================================ shared renderers (pure: data → HTML string)

function stateHtml(kind, msg, extra) {
  if (kind === 'loading') return `<div class="state"><span class="spin" aria-hidden="true"></span>${esc(msg || 'Yükleniyor…')}</div>`;
  if (kind === 'error') {
    return `<div class="state err" role="alert">${icon('warn')}<div><b>Veri alınamadı</b><div class="muted">${esc(msg || '')}</div></div>`
      + `<button type="button" class="btn sm" data-act="retry">Tekrar dene</button></div>`;
  }
  return `<div class="state empty">${esc(msg || 'Henüz veri yok — kaynaklar çekiliyor…')}${extra ? `<div class="state-x">${extra}</div>` : ''}</div>`;
}
function tkChip(sym, cls) {
  sym = String(sym || '');
  return sym ? `<button type="button" class="tk${cls ? ' ' + cls : ''}" data-tk="${esc(sym)}" title="${esc(sym)} hisse sayfası">${esc(sym)}</button>` : '';
}
function tkChips(tickers, max = 6) {
  const t = arr(tickers).filter(x => typeof x === 'string' && x);
  if (!t.length) return '';
  let h = t.slice(0, max).map(s => tkChip(s)).join('');
  if (t.length > max) h += `<span class="more" title="${esc(t.slice(max).join(', '))}">+${t.length - max}</span>`;
  return h;
}
function catTags(cats, max = 4) {
  const c = arr(cats).filter(Boolean);
  let h = c.slice(0, max).map(k => {
    const ci = catInfo(k);
    return `<button type="button" class="cat" style="--c:${esc(ci.color)}" data-cat="${esc(k)}" title="Kategori: ${esc(ci.label)} — bu kategoriye göre filtrele">${esc(ci.label)}</button>`;
  }).join('');
  if (c.length > max) h += `<span class="more" title="${esc(c.slice(max).map(k => catInfo(k).label).join(', '))}">+${c.length - max}</span>`;
  return h;
}
/** Real publisher of a relayed item (Google News, Yahoo, watchlist feeds put it in extra.publisher). */
function pubOf(it) { return String(obj(obj(it).extra).publisher || '').trim(); }
/** One key per publisher: the same outlet reached through two collectors counts once. */
function pubKey(it) { it = obj(it); return fold(pubOf(it) || it.source_name || it.source || '').trim(); }
/** Collector name to show as "via" when it differs from the publisher ('' when they are the same outlet). */
function viaOf(name, pub) {
  if (!pub || !name) return '';
  const n = fold(name), p = fold(pub);
  return n === p || n.includes(p) || p.includes(n) ? '' : String(name);
}
function srcBadge(source, name, stype, pub, more) {
  const via = viaOf(name, pub);
  const label = via ? pub : (name || source || '?');
  const viaShort = /^google news/i.test(via) ? 'GN' : via.split(/\s+/)[0];
  const tip = `${label}${via ? ` · ${via} üzerinden` : ''}${stype ? ' · ' + stypeLabel(stype) : ''}${more ? ' · ' + more : ''} — ${via ? `“${via}” akışına` : 'bu kaynağa'} göre filtrele`;
  return `<button type="button" class="srcb" style="--h:${hue(via ? label : (source || name))}" data-src="${esc(source || '')}" title="${esc(tip)}">${esc(label)}`
    + `${via ? `<small class="via">${esc(viaShort)}</small>` : ''}</button>`;
}
function sentKey(l) { return l === 'pos' || l === 'neg' ? l : 'neu'; }
function sentDot(label, val) {
  const l = sentKey(label), v = num(val);
  return `<span class="sdot ${l}" role="img" aria-label="Duygu: ${esc(sentLabel(l))}" title="Duygu: ${esc(sentLabel(l))}${v != null ? ' (' + fmtSigned(v, 2) + ')' : ''}"></span>`;
}
function impBar(v) {
  const n = clamp(Math.round(num(v) || 0), 0, 100);
  const lvl = n >= 70 ? 'hi' : n >= 40 ? 'md' : 'lo';
  return `<span class="impw" title="Önem: ${n}/100"><span class="imp ${lvl}"><i style="width:${n}%"></i></span><span class="imp-n">${n}</span></span>`;
}
function timeCell(ts) {
  ts = num(ts);
  if (!ts) return '<span class="t">—</span>';
  return `<span class="t" title="${esc(tsTitle(ts))}">${esc(whenShort(ts))}</span><span class="rel" data-ts="${ts}">${esc(rel(ts))}</span>`;
}
function newsTitle(it, cls = 'n-title') {
  const url = safeUrl(it.url);
  const title = esc(it.title || '(başlıksız)');
  const tip = esc(trunc(it.summary || it.title || '', 420));
  return url
    ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${tip}">${title}</a>`
    : `<span class="${cls}" title="${tip}">${title}</span>`;
}
function localClusterCounts(items) {
  const m = {};
  for (const it of items) if (it.cluster_id) (m[it.cluster_id] || (m[it.cluster_id] = new Set())).add(pubKey(it));
  const r = {};
  for (const k in m) r[k] = m[k].size;
  return r;
}
/** Distinct sources that carried this story. Server annotates rows with `cluster_sources`. */
function clusterN(it, local) {
  if (!it.cluster_id) return 0;
  const srv = num(it.cluster_sources);
  if (srv != null) return srv;
  return Math.max(num(it.source_count) || 0, S.clusterCount[it.cluster_id] || 0, (local && local[it.cluster_id]) || 0);
}
function newsRow(it, o = {}) {
  const sum = it.summary ? String(it.summary) : '';
  const n = o.cnt || 0;
  const imp = num(it.importance) || 0;
  const clu = n > 1 ? `<button type="button" class="clu" data-cid="${esc(it.cluster_id)}" title="Bu haberi ${n} farklı kaynak verdi — Kim Ne Diyor'da karşılaştır">+${n - 1} kaynak</button>` : '';
  return `<div class="nrow${o.isNew ? ' flash' : ''}${imp >= 70 ? ' hot' : ''}" data-id="${esc(it.id)}" role="listitem">`
    + `<div class="n-time">${timeCell(it.published_at)}</div>`
    + `<div class="n-src">${srcBadge(it.source, it.source_name, it.source_type, pubOf(it))}</div>`
    + `<div class="n-main">${newsTitle(it)}<div class="n-meta">${tkChips(it.tickers)}${catTags(it.categories)}${clu}${it.author ? `<span class="auth">${esc(trunc(it.author, 40))}</span>` : ''}</div>`
    + `${o.summaries && sum ? `<div class="n-sum">${esc(trunc(sum, 600))}</div>` : ''}</div>`
    + `<div class="n-sent">${sentDot(it.sentiment_label, it.sentiment)}</div>`
    + `<div class="n-imp">${impBar(imp)}</div></div>`;
}
function newsListHtml(items, o = {}) {
  const local = localClusterCounts(items);
  return `<div class="nlist" role="list"><div class="nhead" aria-hidden="true"><span>Saat (TR)</span><span>Kaynak</span><span>Başlık</span><span title="Duygu">Duygu</span><span>Önem</span></div>`
    + items.map(it => newsRow(it, { isNew: o.prev ? !o.prev.has(it.id) : false, summaries: o.summaries, cnt: clusterN(it, local) })).join('') + '</div>';
}
function colItem(it, isNew) {
  // A per-source column already names the collector; show the relayed publisher when there is one.
  const via = viaOf(it.source_name, pubOf(it));
  return `<div class="citem${isNew ? ' flash' : ''}" data-id="${esc(it.id)}">`
    + `<div class="ci-top">${timeCell(it.published_at)}${via ? `<span class="ci-pub" title="${esc(pubOf(it) + ' · ' + via + ' üzerinden')}">${esc(pubOf(it))}</span>` : ''}<span class="sp"></span>${sentDot(it.sentiment_label, it.sentiment)}${impBar(it.importance)}</div>`
    + newsTitle(it, 'ci-title')
    + `<div class="n-meta">${tkChips(it.tickers, 4)}${catTags(it.categories, 2)}</div></div>`;
}
function colHtml(c, items, prev) {
  return `<section class="col" data-src="${esc(c.source)}" aria-label="${esc(c.source_name || c.source)}"><header>${srcBadge(c.source, c.source_name, c.source_type)}`
    + `<span class="col-g">${esc(shortGroup(c.group))}</span><span class="sp"></span><span class="col-n" title="Gösterilen / aralıktaki toplam haber">${items.length}${num(c.total) > items.length ? ' / ' + fmtInt(c.total) : ''}</span></header>`
    + `<div class="col-body">${items.length ? items.map(it => colItem(it, prev ? !prev.has(it.id) : false)).join('') : '<div class="state sm">Bu aralıkta haber yok</div>'}</div></section>`;
}
function miniNewsList(items) {
  return `<div class="mini">${items.map(it => `<div class="mrow">${timeCell(it.published_at)}${srcBadge(it.source, it.source_name, it.source_type, pubOf(it))}`
    + `${newsTitle(it, 'm-title')}${sentDot(it.sentiment_label, it.sentiment)}</div>`).join('')}</div>`;
}

// ---- sparkline & gauge

function spark(values, o = {}) {
  const w = o.w || 120, h = o.h || 34, pad = 2;
  const v = arr(values).map(num).filter(x => x != null);
  const cls = o.cls ? ' ' + o.cls : '';
  if (v.length < 2) return `<svg class="spark empty${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"></svg>`;
  let min = Math.min(...v), max = Math.max(...v);
  let base = num(o.base);
  const span0 = max - min || Math.abs(max) * 0.01 || 1;
  if (base != null && (base < min - span0 * 0.6 || base > max + span0 * 0.6)) base = null;
  if (base != null) { min = Math.min(min, base); max = Math.max(max, base); }
  const rng = max - min || 1;
  const x = i => (i / (v.length - 1)) * w;
  const y = val => pad + (h - 2 * pad) * (1 - (val - min) / rng);
  const pts = v.map((val, i) => `${x(i).toFixed(1)},${y(val).toFixed(1)}`);
  const ref = base != null ? base : v[0];
  const dir = v[v.length - 1] > ref ? 'up' : v[v.length - 1] < ref ? 'down' : 'flat';
  const area = `M0,${h} L${pts.join(' L')} L${w},${h} Z`;
  const bl = base != null ? `<line class="base" x1="0" x2="${w}" y1="${y(base).toFixed(1)}" y2="${y(base).toFixed(1)}" vector-effect="non-scaling-stroke"/>` : '';
  return `<svg class="spark ${dir}${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path class="area" d="${area}"/>${bl}`
    + `<polyline points="${pts.join(' ')}" vector-effect="non-scaling-stroke"/></svg>`;
}
function fgBand(score) {
  const s = num(score);
  if (s == null) return null;
  return FG_BANDS.find(b => s < b[1]) || FG_BANDS[FG_BANDS.length - 1];
}
function gaugeSvg(score) {
  const cx = 110, cy = 108, r = 86, sw = 16;
  const pt = (deg, rr) => [cx + rr * Math.cos(deg * Math.PI / 180), cy - rr * Math.sin(deg * Math.PI / 180)];
  const arc = (a, b, rr) => {
    const p1 = pt(180 - a * 1.8, rr), p2 = pt(180 - b * 1.8, rr);
    return `M${p1[0].toFixed(1)} ${p1[1].toFixed(1)} A${rr} ${rr} 0 0 1 ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  };
  const s = num(score);
  let g = `<svg class="gauge" viewBox="0 0 220 128" role="img" aria-label="Korku ve Açgözlülük endeksi: ${s == null ? 'veri yok' : Math.round(s)}">`;
  g += `<path d="${arc(0, 100, r)}" class="g-track" stroke-width="${sw + 6}" fill="none"/>`;
  for (const [a, b, c] of FG_BANDS) g += `<path d="${arc(a + 0.7, b - 0.7, r)}" stroke="${c}" stroke-width="${sw}" fill="none" opacity=".9"/>`;
  for (const t of [0, 25, 50, 75, 100]) {
    const p = pt(180 - t * 1.8, r - sw / 2 - 9);
    g += `<text x="${p[0].toFixed(1)}" y="${(p[1] + 3).toFixed(1)}" class="g-tick" text-anchor="middle">${t}</text>`;
  }
  if (s != null) {
    const ang = 180 - clamp(s, 0, 100) * 1.8;
    const tip = pt(ang, r - 4), l = pt(ang + 90, 5), rr = pt(ang - 90, 5);
    g += `<path class="needle" d="M${l[0].toFixed(1)} ${l[1].toFixed(1)} L${tip[0].toFixed(1)} ${tip[1].toFixed(1)} L${rr[0].toFixed(1)} ${rr[1].toFixed(1)} Z"/><circle cx="${cx}" cy="${cy}" r="7" class="needle-hub"/>`;
  }
  return g + '</svg>';
}

// ============================================================ API

function errMsg(e) {
  if (!e) return 'Bilinmeyen hata';
  if (e.status === 404) return 'Bulunamadı (404)';
  if (e.name === 'TypeError' || /Failed to fetch|NetworkError|Load failed/i.test(e.message || '')) return 'Sunucuya ulaşılamadı';
  return String(e.message || e);
}
async function api(path, o = {}) {
  let url = path;
  if (o.params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(o.params)) if (v !== undefined && v !== null && v !== '' && v !== false) q.set(k, String(v));
    const qs = q.toString();
    if (qs) url += (url.indexOf('?') >= 0 ? '&' : '?') + qs;
  }
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), o.timeout || 20000) : null;
  try {
    const r = await fetch(url, {
      method: o.method || 'GET',
      headers: o.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: o.body !== undefined ? JSON.stringify(o.body) : undefined,
      signal: ctl ? ctl.signal : undefined,
      cache: 'no-store',
    });
    const txt = await r.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
    if (!r.ok) {
      const err = new Error((data && data.error) || `Sunucu hatası (HTTP ${r.status})`);
      err.status = r.status;
      throw err;
    }
    if (data == null && txt) throw new Error('Geçersiz yanıt (JSON değil)');
    return data == null ? {} : data;
  } catch (e) {
    if (e && e.name === 'AbortError') throw new Error('Zaman aşımı — sunucu yanıt vermedi');
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ============================================================ shell: badges, toasts, status, clocks

function setBadge(key, n, title) {
  const el = byId('b-' + key);
  if (!el) return;
  n = num(n) || 0;
  el.hidden = !n;
  el.textContent = n > 99 ? '99+' : n ? String(n) : '';
  if (title) el.title = title;
}
function updateOppBadge() {
  const items = arr(obj(S.opps).items);
  const strong = items.filter(o => (num(o.score) || 0) >= 60).length;
  setBadge('firsatlar', strong, `Skoru 60 ve üzeri ${strong} fırsat`);
}
function openExternal(url) {
  const u = safeUrl(url);
  if (!u || !HAS_DOM) return;
  const a = document.createElement('a');
  a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
  document.body.appendChild(a); a.click(); a.remove();
}
function toast(o) {
  const box = byId('toasts');
  if (!box) return;
  // `key`: a newer toast with the same key replaces the old one (e.g. the alert summary).
  if (o.key) for (const x of box.querySelectorAll('.toast[data-key]')) if (x.dataset.key === o.key) x.remove();
  const el = document.createElement('div');
  const kind = o.kind || 'info';
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  if (o.key) el.dataset.key = o.key;
  const url = safeUrl(o.url);
  if (url) { el.dataset.url = url; el.classList.add('link'); el.title = 'Haberi tarayıcıda aç'; }
  else if (o.act) { el.dataset.act = o.act; el.title = o.actTitle || ''; }
  el.innerHTML = `<div class="t-body">${o.meta ? `<div class="t-meta">${o.meta}</div>` : ''}<div class="t-title">${esc(o.title || '')}</div>`
    + `${o.body ? `<div class="t-text">${esc(o.body)}</div>` : ''}</div><button type="button" class="t-x" aria-label="Kapat" title="Kapat">×</button>`;
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  const life = o.timeout || (kind === 'alert' ? 15000 : kind === 'err' ? 9000 : 5000);
  // Hover pauses the countdown (remaining ms are kept); leaving resumes it, never less than 4 s.
  let timer = null, left = life, started = 0;
  const countdown = ms => { clearTimeout(timer); started = Date.now(); timer = setTimeout(() => el.remove(), ms); };
  countdown(life);
  el.addEventListener('mouseenter', () => { clearTimeout(timer); left = Math.max(0, left - (Date.now() - started)); });
  el.addEventListener('mouseleave', () => countdown(Math.max(left, 4000)));
}
let _actx = null;
function beep(force) {
  if (!force && !S.prefs.beep) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    _actx = _actx || new AC();
    if (_actx.state === 'suspended') _actx.resume();
    const t = _actx.currentTime, osc = _actx.createOscillator(), g = _actx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, t);
    osc.frequency.setValueAtTime(1175, t + 0.09);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.12, t + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.26);
    osc.connect(g); g.connect(_actx.destination);
    osc.start(t); osc.stop(t + 0.3);
  } catch (e) { /* audio unavailable */ }
}
function notifSupported() { return HAS_DOM && typeof Notification !== 'undefined'; }
function notifGranted() { return notifSupported() && Notification.permission === 'granted'; }
function notifState() {
  if (!notifSupported()) return 'Bu pencerede desteklenmiyor';
  return { granted: 'İzin verildi', denied: 'Engellendi (Windows bildirim ayarlarından açın)', default: 'Henüz izin istenmedi' }[Notification.permission] || String(Notification.permission);
}
function desktopNotify(title, body, url, tag) {
  if (!S.prefs.notify || !notifGranted()) return;
  try {
    const n = new Notification(title, { body, tag: tag || undefined });
    n.onclick = () => { try { window.focus(); } catch (e) { /* ignore */ } openExternal(url); n.close(); };
  } catch (e) { /* not supported in this webview */ }
}
async function askNotify() {
  if (!notifSupported()) { toast({ title: 'Masaüstü bildirimleri bu pencerede desteklenmiyor', kind: 'err' }); return; }
  try {
    const r = await Notification.requestPermission();
    setText('notif-state', notifState());
    if (r === 'granted') {
      S.prefs.notify = true;
      store.set('prefs', S.prefs);
      const cb = document.querySelector('[data-pref="notify"]');
      if (cb) cb.checked = true;
      toast({ title: 'Masaüstü bildirimleri açıldı', kind: 'ok' });
    } else toast({ title: 'Bildirim izni verilmedi', body: notifState(), kind: 'info' });
  } catch (e) { toast({ title: 'Bildirim izni istenemedi', body: errMsg(e), kind: 'err' }); }
}

function setOnline(ok, e) {
  S.online = ok;
  const el = byId('conn');
  if (!el) return;
  el.hidden = ok;
  if (!ok) { el.title = errMsg(e); el.textContent = S.stopped ? 'Uygulama kapatıldı' : 'Bağlantı yok'; }
}
/** Top-bar badge for an instance started without its own window (startup shortcut / --no-window). */
/** Top-bar warning badge: shown while any source group has failing sources (counts.source_groups[g].err > 0). */
function renderSourceWarn() {
  const el = byId('src-warn');
  if (!el) return;
  const groups = obj(obj(obj(S.status).counts).source_groups);
  const bad = Object.keys(groups).filter(k => num(obj(groups[k]).err) > 0);
  el.hidden = !bad.length;
  if (!bad.length) return;
  const n = bad.reduce((a, k) => a + num(obj(groups[k]).err), 0);
  el.textContent = `${fmtInt(n)} kaynak hatalı`;
  el.title = 'Hata veren kaynak grupları: ' + bad.map(k => `${groupLabel(k)} (${fmtInt(num(obj(groups[k]).err))})`).join(', ') + ' — Kaynaklar sayfasına bakın.';
}
function renderInstance() {
  const el = byId('bg-run');
  if (!el) return;
  const inst = obj(obj(S.status).instance);
  el.hidden = !inst.headless || !!S.stopped;
}
/** POST /api/shutdown (stops collection too), then stop polling. */
async function shutdownApp(btn) {
  if (!HAS_DOM || !window.confirm('Piyasa Masası tamamen kapatılsın mı? Haber ve veri toplama da duracak.')) return;
  if (btn) btn.disabled = true;
  try {
    await api('/api/shutdown', { method: 'POST', body: {} });
  } catch (e) {
    if (btn) btn.disabled = false;
    toast({ title: 'Uygulama kapatılamadı', body: errMsg(e), kind: 'err' });
    return;
  }
  S.stopped = true;
  if (S.tickTimer) { clearInterval(S.tickTimer); S.tickTimer = null; }
  renderInstance();
  const conn = byId('conn');
  if (conn) { conn.hidden = false; conn.textContent = 'Uygulama kapatıldı'; conn.title = ''; }
  const view = byId('view');
  if (view) view.innerHTML = `<div class="page">${stateHtml('empty', 'Uygulama kapatıldı; pencereyi kapatabilirsiniz.')}</div>`;
  toast({ title: 'Uygulama kapatıldı; pencereyi kapatabilirsiniz.', kind: 'ok', timeout: 60000 });
}
async function loadStatus() {
  try {
    const st = await api('/api/status', { timeout: 10000 });
    S.status = st;
    S.statusAt = Date.now();
    const now = num(st.now);
    if (now) S.skewMs = Math.abs(now * 1000 - Date.now()) > 3000 ? now * 1000 - Date.now() : 0;
    if (!S.newsSeen) { S.newsSeen = Math.floor(now || Date.now() / 1000); store.set('newsSeen', S.newsSeen); }
    setOnline(true);
    renderSideStats();
    renderInstance();
    renderSourceWarn();
    if (cur && cur.page.onStatus) cur.page.onStatus();
  } catch (e) {
    setOnline(false, e);
    throw e;
  }
}
function renderSideStats() {
  const el = byId('sb-stats');
  if (!el) return;
  const st = obj(S.status), c = obj(st.counts);
  const need = needConfigCount();
  el.innerHTML = `<div title="Son 24 saatte toplanan haber"><b>${fmtInt(c.news_24h)}</b> haber / 24 sa</div>`
    + `<div class="sb-src" title="Kaynak durumları: çalışıyor · hata · bekliyor"><span class="dot ok"></span>${fmtInt(c.sources_ok)}<span class="dot err"></span>${fmtInt(c.sources_err)}<span class="dot idle"></span>${fmtInt(c.sources_idle)}</div>`
    + (need ? `<a href="#ayarlar" class="sb-need" data-act="need-config" title="Ücretsiz anahtar ya da SEC e-postası girilince açılacak kaynaklar">${fmtInt(need)} kaynak ayar bekliyor</a>` : '')
    + (st.version ? `<div class="dim">v${esc(st.version)}</div>` : '');
}
/** Sources waiting on a key / SEC e-mail: server count when available, else derived from /api/sources. */
function needConfigCount() {
  const n = num(obj(obj(S.status).counts).sources_need_config);
  if (n != null) return n;
  return S.sources.filter(s => s.enabled !== false && srcState(s)[0] === 'need').length;
}
/** Opens Ayarlar and puts the cursor in the SEC identity field (focused once the form has rendered). */
function goSecSetting() {
  S.focusSec = true;
  if (cur && cur.key === 'ayarlar' && PAGES.ayarlar.cfgShown) PAGES.ayarlar.focusSec();
  else go('ayarlar');
}
async function loadMeta() {
  const m = await api('/api/meta');
  S.meta = Object.assign({}, obj(m), { categories: obj(m.categories), groups: obj(m.groups), source_types: obj(m.source_types), sentiments: obj(m.sentiments) });
  S.metaLoaded = true;
  if (cur && cur.page.onMeta) cur.page.onMeta();
}
async function loadSources() {
  const d = await api('/api/sources');
  S.sources = arr(d.items);
  S.sourcesAt = Date.now();
  renderSideStats();
  if (cur && cur.page.onSources) cur.page.onSources();
}
async function loadConfig() {
  S.config = await api('/api/config');
  if (cur && cur.page.onConfig) cur.page.onConfig();
}
function ingestClusters(items) {
  const m = {};
  for (const c of arr(items)) if (c && c.cluster_id) m[c.cluster_id] = arr(c.items).length ? new Set(arr(c.items).map(pubKey)).size : num(c.source_count) || 0;
  S.clusterCount = m;
  S.clusterAt = Date.now();
}
async function loadClusterCounts(hours) {
  const d = await api('/api/clusters', { params: { since_hours: hours || 24, min_sources: 2 } });
  ingestClusters(d.items);
}
function alertToast(it, wl) {
  const hits = arr(it.watchlist_hits).length ? arr(it.watchlist_hits) : arr(it.tickers).filter(t => wl.has(t));
  const why = it.alert_reason ? esc(it.alert_reason) : `önem ${Math.round(num(it.importance) || 0)}${hits.length ? ' · <span class="wl-hit">İzleme listesi</span>' : ''}`;
  const who = pubOf(it) || it.source_name || it.source || '';
  const meta = `${esc(who)} · ${why} · ${esc(whenShort(it.published_at))} ${tkChips(it.tickers, 4)}`;
  toast({ kind: 'alert', title: it.title || '', meta, url: it.url });
  desktopNotify(it.title || 'Yeni önemli haber', `${who}${arr(it.tickers).length ? ' · ' + arr(it.tickers).slice(0, 5).join(' ') : ''}`, it.url, it.id);
}
async function pollAlerts() {
  if (!S.statusAt) return; // wait for /api/status so the scheduler warm-up below is known
  if (!S.alertSince) S.alertSince = Math.floor(nowSec()) - 120;
  const d = await api('/api/alerts', { params: { since: S.alertSince } });
  const items = arr(d.items);
  const now = nowSec();
  let maxF = S.alertSince;
  const fresh = [];
  // Every row is marked seen; only recent stories not yet alerted (one toast per cluster) qualify.
  for (const it of items) {
    const f = num(it.fetched_at) || 0;
    if (f > maxF) maxF = f;
    if (!it.id || S.alertSeen.has(it.id)) continue;
    S.alertSeen.add(it.id);
    if (now - (num(it.published_at) || 0) > ALERT_MAX_AGE) continue;
    const ck = String(it.cluster_id || it.id);
    if (S.alertClusters.has(ck)) continue;
    // Also remember the row id: later copies of the story carry the earliest item's id as cluster_id.
    S.alertClusters.add(ck);
    S.alertClusters.add(String(it.id));
    fresh.push(it);
  }
  // Server returns `now` as the next cursor (rows fetched in its current second come next poll).
  S.alertSince = num(d.now) || (items.length ? maxF : S.alertSince);
  if (S.alertSeen.size > 3000) S.alertSeen = new Set([...S.alertSeen].slice(-1000));
  if (S.alertClusters.size > 3000) S.alertClusters = new Set([...S.alertClusters].slice(-1000));
  // The scheduler's first pass after start is backlog, not news: rows are consumed above, no toasts.
  const st0 = num(obj(obj(S.status).scheduler).started_at);
  if (st0 && now - st0 < ALERT_WARMUP) return;
  if (!fresh.length) return;
  fresh.sort((a, b) => (num(b.importance) || 0) - (num(a.importance) || 0) || (num(b.published_at) || 0) - (num(a.published_at) || 0));
  const wl = wlSet();
  if (fresh.length <= 2) for (const it of fresh) alertToast(it, wl);
  else {
    alertToast(fresh[0], wl);
    toast({ kind: 'alert', key: 'alert-sum', title: `+${fresh.length - 1} uyarı daha`, body: 'Tümü Haberler sayfasında', act: 'alerts-news', actTitle: 'Haberler sayfasını geliş zamanına göre aç' });
  }
  beep();
}
async function pollBadges() {
  if (cur && cur.key === 'haberler') return;
  if (!S.newsSeen) return;
  const d = await api('/api/news', { params: { after_fetched: S.newsSeen, since_hours: 72, limit: 1 } });
  setBadge('haberler', num(d.total) || 0, 'Son bakışınızdan beri gelen yeni haber');
}
async function pollOpps() {
  if (cur && cur.key === 'firsatlar') return;
  S.opps = await api('/api/opportunities');
  updateOppBadge();
}
function markNewsSeen(items) {
  // A filtered view shows only a subset: advancing the cursor would hide unseen stories from other sources.
  if (nfActive()) return;
  let m = S.newsSeen;
  for (const it of items) { const f = num(it.fetched_at) || 0; if (f > m) m = f; }
  if (m > S.newsSeen) { S.newsSeen = m; store.set('newsSeen', m); }
  setBadge('haberler', 0);
}

let _relTick = 0;
function clockTick() {
  const now = nowSec();
  setText('clk-tr', hms(now, TZ_TR));
  setText('clk-et', hm(now, TZ_ET));
  const sess = effSession(now);
  const sl = byId('sess');
  if (sl) { const cls = 'sess ' + sess; if (sl.className !== cls) sl.className = cls; setText('sess', sessLabel(sess)); }
  const tg = curTargets(sess, now);
  setText('countdown', tg.length ? `${tg[0].label} ${dur(tg[0].ts - now)}` : '');
  const cd = byId('countdown');
  if (cd && tg.length) cd.title = `${tg[0].label}: ${tsTitle(tg[0].ts)}`;
  const lu = num(obj(S.status).last_update);
  setText('last-upd', lu ? 'Son veri ' + rel(lu) : S.lastData ? 'Güncellendi ' + agoMs(S.lastData) : '');
  const luEl = byId('last-upd');
  if (luEl) luEl.title = (lu ? 'Kaynaklardan son başarılı çekim: ' + tsTitle(lu) + '\n' : '') + (S.lastData ? 'Ekran yenilendi: ' + hms(S.lastData / 1000) : '');
  if (cur && cur.page.tick) cur.page.tick(now, sess, tg);
  if (++_relTick % 30 === 0 && HAS_DOM) {
    const els = document.querySelectorAll('#view [data-ts]');
    for (let i = 0; i < els.length; i++) els[i].textContent = rel(+els[i].dataset.ts);
  }
}

// ============================================================ router & polling

const PAGES = {};
let cur = null;

function go(hash) {
  if (!HAS_DOM) return;
  if (location.hash === '#' + hash) route(true);
  else location.hash = hash;
}
function goTicker(sym) { const s = normSym(sym); if (s) go('hisse/' + s); }
function route(force) {
  let raw = '';
  try { raw = decodeURIComponent((location.hash || '').replace(/^#\/?/, '')); } catch (e) { raw = ''; }
  const parts = raw.split('/');
  const key = PAGES[parts[0]] ? parts[0] : 'haberler';
  const param = parts.slice(1).join('/');
  if (!force && cur && cur.key === key && cur.param === param) return;
  if (cur && cur.page.unmount) { try { cur.page.unmount(); } catch (e) { /* ignore */ } }
  cur = { key, param, page: PAGES[key], last: 0, busy: false, seq: 0 };
  for (const a of document.querySelectorAll('#nav .nav-item')) {
    const on = a.dataset.key === key;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  const view = byId('view');
  view.scrollTop = 0;
  view.innerHTML = '';
  try { cur.page.mount(view, param); } catch (e) {
    view.innerHTML = `<div class="page">${stateHtml('error', 'Sayfa açılamadı: ' + errMsg(e))}</div>`;
    if (typeof console !== 'undefined') console.error(e);
  }
  setPageTitle(cur.page.title + (key === 'hisse' && param ? ' · ' + param.toUpperCase() : ''));
  if (key === 'haberler') setBadge('haberler', 0);
  refreshPage(true);
}
function setPageTitle(t) {
  setText('page-title', t);
  if (HAS_DOM) document.title = `${t} — Piyasa Masası`;
}
/** Re-fetch the current page. Stale responses (older seq / other page) are dropped by `live()`. */
async function refreshPage(first) {
  if (!cur) return;
  const c = cur;
  const seq = ++c.seq;
  const live = () => c === cur && seq === c.seq;
  c.busy = true;
  c.last = Date.now();
  try {
    await c.page.refresh(!!first, live);
    if (live()) { S.lastData = Date.now(); pageOk(); }
  } catch (e) {
    if (live()) {
      pageErr(e);
      if (c.page.data == null) {
        if (c.page.onError) c.page.onError(e);
        else if (c.page.bodyId) setHtml(c.page.bodyId, stateHtml('error', errMsg(e)));
      }
    }
  } finally {
    if (seq === c.seq) c.busy = false;
  }
}
function pageErr(e) {
  const el = byId('page-err');
  if (!el || !cur || cur.page.data == null) return;
  el.innerHTML = `<div class="errbar" role="alert">${icon('warn')}<span>Güncellenemedi: ${esc(errMsg(e))} — son alınan veriler gösteriliyor.</span>`
    + `<button type="button" class="btn sm" data-act="retry">Tekrar dene</button></div>`;
}
function pageOk() { const el = byId('page-err'); if (el && el.firstChild) el.innerHTML = ''; }

const tasks = [];
function every(name, sec, fn, o = {}) { tasks.push(Object.assign({ name, sec, fn, last: 0, busy: false }, o)); }
async function runTask(t) {
  if (t.busy) return;
  t.busy = true;
  t.last = Date.now();
  try { await t.fn(); } catch (e) {
    if (t.retry) t.last = Date.now() - (t.sec - t.retry) * 1000;
  } finally { t.busy = false; }
}
function runNow(name) { const t = tasks.find(x => x.name === name); if (t) runTask(t); }
function tick() {
  clockTick();
  const hidden = HAS_DOM && document.hidden;
  const now = Date.now();
  for (const t of tasks) {
    if (hidden && !(t.whenHidden && t.whenHidden())) continue;
    if (now - t.last >= t.sec * 1000) runTask(t);
  }
  if (!hidden && cur && cur.page.poll && !cur.busy && now - cur.last >= cur.page.poll * 1000) refreshPage(false);
}
/** Keeps the first visible row in place while a list is re-rendered (new rows arrive at the top). */
function keepScroll(rowSel, fn) {
  const sc = byId('view');
  if (!sc) { fn(); return; }
  const top = sc.scrollTop;
  let anchor = null, off = 0;
  if (top > 4 && rowSel) {
    const st = sc.getBoundingClientRect().top + 60;
    const rows = sc.querySelectorAll(rowSel);
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i].getBoundingClientRect();
      if (r.bottom > st) { anchor = rows[i].dataset.id; off = r.top - sc.getBoundingClientRect().top; break; }
    }
  }
  fn();
  if (anchor != null) {
    const sel = `${rowSel}[data-id="${window.CSS && CSS.escape ? CSS.escape(anchor) : String(anchor).replace(/["\\]/g, '\\$&')}"]`;
    const el = sc.querySelector(sel);
    if (el) { sc.scrollTop += (el.getBoundingClientRect().top - sc.getBoundingClientRect().top) - off; return; }
  }
  sc.scrollTop = top;
}
function hasSelectionIn(id) {
  try {
    const s = window.getSelection();
    if (!s || s.isCollapsed) return false;
    const el = byId(id);
    return !!el && el.contains(s.anchorNode);
  } catch (e) { return false; }
}
function selectHtml(f, opts, val, cls, label) {
  return `<select class="sel${cls ? ' ' + cls : ''}" data-f="${esc(f)}"${label ? ` aria-label="${esc(label)}" title="${esc(label)}"` : ''}>`
    + opts.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(val) ? ' selected' : ''}>${esc(l)}</option>`).join('') + '</select>';
}
function segHtml(act, opts, val, label) {
  return `<div class="seg" role="group" aria-label="${esc(label || '')}">${opts.map(([v, l]) => `<button type="button" class="seg-b${String(v) === String(val) ? ' on' : ''}" data-act="${act}" data-v="${esc(v)}" aria-pressed="${String(v) === String(val)}">${esc(l)}</button>`).join('')}</div>`;
}
function searchBox(f, val, ph) {
  return `<div class="search">${icon('search')}<input type="search" class="inp" data-f="${esc(f)}" data-primary-search placeholder="${esc(ph)}" value="${esc(val)}" autocomplete="off" spellcheck="false"></div>`;
}

// ============================================================ page: Haberler

const NF_KEEP = ['since_hours', 'order', 'columns', 'summaries', 'srcOpen'];
const NF_BLANK = { q: '', sources: [], group: '', source_type: '', category: '', sentiment: '', ticker: '', min_importance: 0, limit: 200 };
const NF = Object.assign({ since_hours: 24, order: 'published', columns: false, summaries: false, srcOpen: false }, JSON.parse(JSON.stringify(NF_BLANK)), pick(obj(store.get('newsFilters', {})), NF_KEEP));
function saveNF() { store.set('newsFilters', pick(NF, NF_KEEP)); }
function nfActive() { return !!(NF.q || NF.sources.length || NF.group || NF.source_type || NF.category || NF.sentiment || NF.ticker || NF.min_importance); }
function nfReset() { Object.assign(NF, JSON.parse(JSON.stringify(NF_BLANK))); }
function nfSources() {
  if (NF.sources.length) return NF.sources;
  if (NF.group) return S.sources.filter(s => s.group === NF.group).map(s => s.source);
  return [];
}
function nfClientFilter() {
  const q = fold(NF.q.trim()), tk = NF.ticker, mi = NF.min_importance || 0;
  return it => (!q || fold(`${it.title || ''} ${it.summary || ''}`).includes(q))
    && (!tk || arr(it.tickers).includes(tk))
    && (!NF.category || arr(it.categories).includes(NF.category))
    && (!NF.sentiment || sentKey(it.sentiment_label) === NF.sentiment)
    && (num(it.importance) || 0) >= mi
    && (!NF.source_type || !it.source_type || it.source_type === NF.source_type);
}
function newsToolbar() {
  const cats = Object.keys(obj(S.meta.categories)).map(k => [k, catInfo(k).label]).sort((a, b) => a[1].localeCompare(b[1], 'tr'));
  if (NF.category && !cats.some(c => c[0] === NF.category)) cats.unshift([NF.category, catInfo(NF.category).label]);
  const stypes = Object.keys(Object.keys(obj(S.meta.source_types)).length ? S.meta.source_types : STYPE_LABELS).map(k => [k, stypeLabel(k)]);
  return searchBox('q', NF.q, 'Haberlerde ara…  ( / )')
    + `<input type="text" class="inp tk-inp" data-f="ticker" placeholder="Hisse" value="${esc(NF.ticker)}" maxlength="12" autocomplete="off" spellcheck="false" aria-label="Hisse sembolüne göre filtrele" title="Hisse sembolüne göre filtrele">`
    + selectHtml('category', [['', 'Tüm kategoriler'], ...cats], NF.category, '', 'Kategori')
    + selectHtml('sentiment', [['', 'Tüm duygular'], ['pos', sentLabel('pos')], ['neg', sentLabel('neg')], ['neu', sentLabel('neu')]], NF.sentiment, '', 'Duygu')
    + selectHtml('source_type', [['', 'Tüm kaynak türleri'], ...stypes], NF.source_type, '', 'Kaynak türü')
    + selectHtml('since_hours', [[1, 'Son 1 saat'], [3, 'Son 3 saat'], [6, 'Son 6 saat'], [12, 'Son 12 saat'], [24, 'Son 24 saat'], [48, 'Son 2 gün'], [72, 'Son 3 gün']], NF.since_hours, '', 'Zaman aralığı')
    + `<label class="range" title="Asgari önem puanı">Önem ≥ <input type="range" min="0" max="100" step="5" data-f="min_importance" value="${NF.min_importance}" aria-label="Asgari önem"><output>${NF.min_importance}</output></label>`
    + selectHtml('order', [['published', 'Yayın zamanı'], ['fetched', 'Geliş zamanı'], ['importance', 'Önem']], NF.order, '', 'Sıralama')
    + `<label class="chk"><input type="checkbox" data-f="summaries"${NF.summaries ? ' checked' : ''}> Özetler</label>`
    + `<span class="sp"></span><button type="button" class="btn${NF.columns ? ' on' : ''}" data-act="news-columns" aria-pressed="${NF.columns}" title="Her kaynak için ayrı sütun">${icon('columns')} Sütun görünümü</button>`;
}
function newsSrcBar() {
  const list = S.sources.filter(s => (s.kind !== 'calendar' && s.kind !== 'market') || (num(s.news_24h) || 0) > 0);
  if (!list.length) return `<div class="src-head"><span class="muted sm">${S.sourcesAt ? 'Haber kaynağı yok.' : 'Kaynak listesi yükleniyor…'}</span></div>`;
  const sel = new Set(NF.sources);
  const groups = groupBy(list, s => s.group || 'other');
  const order = groupOrder(Object.keys(groups));
  const names = {};
  for (const s of list) names[s.source] = s.source_name || s.source;
  let h = `<div class="src-head"><button type="button" class="btn sm ghost" data-act="src-open" aria-expanded="${NF.srcOpen}">${icon(NF.srcOpen ? 'chevUp' : 'chevDown')} Kaynaklar: <b>${sel.size ? sel.size + ' seçili' : 'tümü'}</b></button>`
    + selectHtml('group', [['', 'Tüm gruplar'], ...order.map(g => [g, shortGroup(g)])], NF.group, 'sm', 'Kaynak grubu');
  if (sel.size) {
    h += `<span class="sel-src">${[...sel].map(k => `<button type="button" class="schip on" data-act="src-chip" data-key="${esc(k)}" style="--h:${hue(k)}" title="Seçimden çıkar">${esc(names[k] || k)} ×</button>`).join('')}</span>`
      + '<button type="button" class="btn sm ghost" data-act="src-clear">Seçimi temizle</button>';
  }
  h += '</div>';
  if (NF.srcOpen) {
    h += '<div class="src-body">' + order.map(g => `<div class="sg"><button type="button" class="sg-l" data-act="src-group" data-g="${esc(g)}" title="Bu gruptaki tüm kaynakları seç">${esc(shortGroup(g))}</button><div class="sg-chips">`
      + groups[g].map(s => {
        const on = sel.has(s.source);
        return `<button type="button" class="schip${on ? ' on' : ''}${s.enabled === false ? ' off' : ''}" data-act="src-chip" data-key="${esc(s.source)}" style="--h:${hue(s.source)}" aria-pressed="${on}" title="${esc(s.description || s.source_name || s.source)}">`
          + `${esc(s.source_name || s.source)}${num(s.news_24h) ? `<small>${fmtInt(s.news_24h)}</small>` : ''}</button>`;
      }).join('') + '</div></div>').join('') + '</div>';
  }
  return h;
}
function nfChips() {
  const out = [];
  const add = (k, label) => out.push(`<button type="button" class="fchip" data-act="nf-clear" data-k="${k}" title="Filtreyi kaldır">${esc(label)} ×</button>`);
  if (NF.q) add('q', `“${trunc(NF.q, 24)}”`);
  if (NF.ticker) add('ticker', 'Hisse: ' + NF.ticker);
  if (NF.category) add('category', 'Kategori: ' + catInfo(NF.category).label);
  if (NF.sentiment) add('sentiment', 'Duygu: ' + sentLabel(NF.sentiment));
  if (NF.source_type) add('source_type', 'Tür: ' + stypeLabel(NF.source_type));
  if (NF.group) add('group', 'Grup: ' + shortGroup(NF.group));
  if (NF.min_importance) add('min_importance', 'Önem ≥ ' + NF.min_importance);
  if (out.length > 1) out.push('<button type="button" class="fchip clear" data-act="news-reset">Tümünü temizle</button>');
  return out.join('');
}
/** One-time hint: SEC sources (8-K, Form 4, S-1/424B) only need a name + e-mail. Shown while it is empty. */
function secTipHtml() {
  if (!S.config || String(S.config.sec_user_agent || '').trim() || store.get('secTipDismissed', false)) return '';
  return `<div class="tipbar" role="note">${icon('bell')}<span>SEC bildirimleri (8-K, Form 4, halka arz / ek arz) için Ayarlar'dan e-posta girin — ücretsiz.</span>`
    + '<button type="button" class="btn sm" data-act="need-config">Ayarlara git</button>'
    + '<button type="button" class="t-x" data-act="sec-tip-x" aria-label="Kapat" title="Bir daha gösterme">×</button></div>';
}

PAGES.haberler = {
  title: 'Haberler', poll: 30, bodyId: 'news-body',
  mount(view) {
    this.data = null; this.sig = ''; this.prev = null; this.mode = '';
    view.innerHTML = `<div class="page page-news${NF.columns ? ' colmode' : ''}">`
      + '<div class="toolbar sticky" id="news-tools"></div><div id="page-err"></div>'
      + `<div id="news-tip">${secTipHtml()}</div>`
      + '<div class="srcbar" id="news-src"></div><div class="infobar" id="news-info"></div>'
      + `<button type="button" class="newpill" id="news-newpill" data-act="news-top" hidden></button>`
      + `<div id="news-body" class="news-body">${stateHtml('loading', 'Haberler yükleniyor…')}</div></div>`;
    setHtml('news-tools', newsToolbar());
    setHtml('news-src', newsSrcBar());
  },
  onMeta() {
    const a = HAS_DOM && document.activeElement;
    if (!(a && a.closest && a.closest('#news-tools'))) setHtml('news-tools', newsToolbar());
    this.sig = '';
    this.rerender();
  },
  onSources() { setHtml('news-src', newsSrcBar()); },
  onConfig() { setHtml('news-tip', secTipHtml()); },
  onFilter(f, v, type, el) {
    if (f === 'q' || f === 'ticker') {
      if (type !== 'input') return;
      NF[f] = f === 'ticker' ? String(v).trim().toUpperCase().replace(/^\$/, '') : String(v);
      debounce('news', () => this.reload(), 350);
      return;
    }
    if (f === 'min_importance') {
      NF.min_importance = num(v) || 0;
      if (type === 'input') debounce('news', () => this.reload(), 300);
      return;
    }
    if (type !== 'change') return;
    if (f === 'summaries') { NF.summaries = !!v; saveNF(); this.sig = ''; this.rerender(); return; }
    if (f === 'since_hours') NF.since_hours = num(v) || 24;
    else if (f === 'group') { NF.group = String(v); NF.sources = []; setHtml('news-src', newsSrcBar()); }
    else NF[f] = String(v);
    saveNF();
    this.reload();
  },
  reload() {
    this.sig = ''; this.prev = null;
    const view = byId('view');
    if (view) view.scrollTop = 0;
    refreshPage(false);
  },
  params() {
    return {
      q: NF.q.trim(), source: nfSources().join(','), source_type: NF.source_type, category: NF.category,
      ticker: NF.ticker, sentiment: NF.sentiment, since_hours: NF.since_hours,
      min_importance: NF.min_importance || '', order: NF.order, limit: NF.limit,
    };
  },
  async refresh(first, live) {
    const mode = NF.columns ? 'cols' : 'list';
    const main = mode === 'cols'
      ? api('/api/news/by_source', { params: { since_hours: NF.since_hours, per_source: 30, group: NF.group } })
      : api('/api/news', { params: this.params() });
    const d = await main;
    // Fallback for servers that don't annotate rows with cluster_sources.
    const c0 = arr(d.items).find(it => it.cluster_id);
    if (mode === 'list' && c0 && c0.cluster_sources == null && Date.now() - S.clusterAt > POLL.clusters * 1000) {
      await loadClusterCounts(Math.max(24, NF.since_hours)).catch(() => null);
    }
    if (!live()) return;
    this.data = d;
    this.mode = mode;
    this.rerender();
  },
  rerender() {
    if (!this.data) return;
    if (this.mode === 'cols') this.renderCols(); else this.renderList();
  },
  info(left) {
    setHtml('news-info', `<span class="info-l">${left}</span><span class="fchips">${nfChips()}</span><span class="sp"></span><span class="dim sm">Saatler TR · ET için saatin üzerine gelin</span>`);
  },
  emptyHtml() {
    if (nfActive()) return stateHtml('empty', 'Bu filtrelerle eşleşen haber yok.', '<button type="button" class="btn sm" data-act="news-reset">Filtreleri temizle</button>');
    const tot = num(obj(obj(S.status).counts).news_total);
    return stateHtml('empty', !tot ? 'Henüz veri yok — kaynaklar çekiliyor…' : `Son ${hoursLabel(NF.since_hours)} içinde haber yok.`,
      tot ? '<button type="button" class="btn sm" data-act="news-wider">Son 3 güne genişlet</button>' : '');
  },
  renderList() {
    const items = arr(this.data.items);
    const total = num(this.data.total) ?? items.length;
    this.info(`<b>${fmtInt(total)}</b> haber${total > items.length ? ` · ilk ${fmtInt(items.length)} gösteriliyor` : ''}`);
    const local = localClusterCounts(items);
    const sig = `${NF.summaries}|` + items.map(it => `${it.id}:${Math.round(num(it.importance) || 0)}:${clusterN(it, local)}`).join('|');
    if (sig === this.sig || hasSelectionIn('news-body')) return;
    this.sig = sig;
    const prev = this.prev;
    const fresh = prev ? items.filter(it => !prev.has(it.id)).length : 0;
    let html = items.length ? newsListHtml(items, { prev, summaries: NF.summaries }) : this.emptyHtml();
    if (items.length && total > items.length && NF.limit < 500) html += '<div class="more-row"><button type="button" class="btn" data-act="news-more">Daha fazla göster (en fazla 500)</button></div>';
    const view = byId('view');
    const atTop = !view || view.scrollTop < 10;
    keepScroll('.nrow', () => setHtml('news-body', html));
    this.prev = new Set(items.map(it => it.id));
    if (fresh && !atTop) {
      const p = byId('news-newpill');
      if (p) { p.hidden = false; p.innerHTML = `${icon('up')} ${fresh} yeni haber`; }
    }
    markNewsSeen(items);
  },
  renderCols() {
    let cols = arr(this.data.sources);
    const sel = nfSources();
    if (sel.length) cols = cols.filter(c => sel.includes(c.source));
    if (NF.source_type) cols = cols.filter(c => !c.source_type || c.source_type === NF.source_type);
    const f = nfClientFilter();
    const filtering = nfActive();
    const shown = cols.map(c => ({ c, items: arr(c.items).filter(f) })).filter(x => x.items.length || !filtering);
    const n = shown.reduce((a, x) => a + x.items.length, 0);
    this.info(`<b>${shown.length}</b> kaynak sütunu · ${fmtInt(n)} haber`);
    const sig = shown.map(x => x.c.source + ':' + x.items.map(i => i.id + (num(i.importance) || 0)).join(',')).join('|');
    if (sig === this.sig || hasSelectionIn('news-body')) return;
    this.sig = sig;
    const prev = this.prev;
    const old = byId('news-cols');
    const left = old ? old.scrollLeft : 0, tops = {};
    if (old) for (const c of old.querySelectorAll('.col')) { const b = c.querySelector('.col-body'); if (b) tops[c.dataset.src] = b.scrollTop; }
    setHtml('news-body', shown.length ? `<div class="cols" id="news-cols">${shown.map(x => colHtml(x.c, x.items, prev)).join('')}</div>` : this.emptyHtml());
    const nw = byId('news-cols');
    if (nw) {
      nw.scrollLeft = left;
      for (const c of nw.querySelectorAll('.col')) { const b = c.querySelector('.col-body'); if (b && tops[c.dataset.src]) b.scrollTop = tops[c.dataset.src]; }
    }
    this.prev = new Set(shown.flatMap(x => x.items.map(i => i.id)));
    const all = shown.flatMap(x => x.items);
    markNewsSeen(all);
  },
};

// ============================================================ page: Kim Ne Diyor

const KF = { since_hours: 24, min_sources: 2, q: '', sort: 'importance' };
function agreement(items) {
  const n = { pos: 0, neg: 0, neu: 0 };
  for (const it of items) n[sentKey(it.sentiment_label)]++;
  return agreementOf(n);
}
/** Prefers the server's per-cluster sentiment counts (`labels`), which cover items beyond the ones returned. */
function clusterAgreement(c) {
  const l = obj(c.labels);
  if (['pos', 'neg', 'neu'].some(k => num(l[k]) != null)) return agreementOf({ pos: num(l.pos) || 0, neg: num(l.neg) || 0, neu: num(l.neu) || 0 });
  return agreement(arr(c.items));
}
/** Server counts are per collector; when two collectors relay one publisher, count the deduped rows instead. */
function pubAgreement(c, uniq) {
  return uniq.length && uniq.length < arr(c.items).length ? agreement(uniq) : clusterAgreement(c);
}
function agreementOf(n) {
  const tot = n.pos + n.neg + n.neu;
  let dom = 'neu';
  if (n.pos > n.neg && n.pos >= n.neu) dom = 'pos';
  else if (n.neg > n.pos && n.neg >= n.neu) dom = 'neg';
  else if (n.pos === n.neg && n.pos > n.neu) dom = 'mixed';
  const pct = tot ? Math.max(n.pos, n.neg, n.neu) / tot : 0;
  return { n, tot, dom, pct, conflict: n.pos > 0 && n.neg > 0 };
}
function agreeMeter(ag, avg) {
  const w = k => (ag.tot ? (ag.n[k] / ag.tot) * 100 : 0).toFixed(1);
  const lvl = ag.tot < 2 ? 'Tek görüş' : ag.conflict && ag.pct < 0.6 ? 'Kaynaklar ayrışıyor' : ag.pct >= 0.75 ? 'Güçlü uyum' : ag.pct >= 0.5 ? 'Kısmi uyum' : 'Ayrışma';
  const domTxt = { pos: 'çoğunluk olumlu', neg: 'çoğunluk olumsuz', neu: 'çoğunluk nötr', mixed: 'olumlu = olumsuz' }[ag.dom];
  const cls = ag.dom === 'mixed' ? 'neu' : ag.dom;
  return `<div class="agree" title="Olumlu ${ag.n.pos} · Nötr ${ag.n.neu} · Olumsuz ${ag.n.neg}">`
    + `<div class="ag-top"><span>Duygu uyumu</span><b class="s-${cls}">%${Math.round(ag.pct * 100)}</b></div>`
    + `<div class="ag-bar"><i class="pos" style="width:${w('pos')}%"></i><i class="neu" style="width:${w('neu')}%"></i><i class="neg" style="width:${w('neg')}%"></i></div>`
    + `<div class="ag-lbl">${esc(lvl)} · ${esc(domTxt)}${num(avg) != null ? ` · ort. <b class="${dirClass(avg)}">${fmtSigned(avg)}</b>` : ''}</div></div>`;
}
/** One row per publisher, earliest first: the same outlet reached through two collectors is one voice.
 *  Rows from /api/clusters already are one per voice (they carry `publisher` + `collectors`): kept as they are. */
function uniqPub(items) {
  const seen = new Set();
  const sorted = arr(items).slice().sort((a, b) => (num(a.published_at) || 0) - (num(b.published_at) || 0));
  if (sorted.length && sorted.every(it => it && Array.isArray(it.collectors))) return sorted;
  return sorted.filter(it => {
    const k = pubKey(it);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
function cluItem(it, first) {
  const cols = arr(it.collectors).filter(Boolean);
  const more = cols.length > 1 || (cols.length === 1 && cols[0] !== it.source_name) ? 'Akışlar: ' + cols.join(', ') : '';
  return `<div class="ci s-${sentKey(it.sentiment_label)}"><div class="ci-h">${srcBadge(it.source, it.source_name, it.source_type, String(it.publisher || '').trim() || pubOf(it), more)}`
    + `${first ? '<span class="pill first" title="Hikâyeyi ilk veren kaynak">İlk</span>' : ''}<span class="sp"></span>`
    + `<span class="t" title="${esc(tsTitle(it.published_at))}">${esc(whenShort(it.published_at))}</span>${sentDot(it.sentiment_label, it.sentiment)}</div>`
    + newsTitle(it, 'ci-title')
    + `${it.summary ? `<p class="ci-sum">${esc(trunc(it.summary, 220))}</p>` : ''}</div>`;
}
function clusterCard(c, ag, isNew, uniq) {
  const items = uniq || uniqPub(c.items);
  ag = ag || pubAgreement(c, items);
  const srcCount = items.length || num(c.source_count) || 0;
  const first = num(c.first_seen) || num(items[0] && items[0].published_at);
  const last = num(c.last_seen) || num(items.length && items[items.length - 1].published_at);
  return `<article class="card clu-card${isNew ? ' flash' : ''}" id="clu-${esc(c.cluster_id)}" data-id="${esc(c.cluster_id)}">`
    + `<header class="clu-h"><div class="clu-ht"><h3>${esc(c.title || (items[0] && items[0].title) || '(başlıksız)')}</h3>`
    + `<div class="n-meta">${tkChips(c.tickers, 8)}${catTags(c.categories, 5)}<span class="pill" title="Bağımsız yayıncı sayısı (aynı haberin kopyaları bir kez sayılır)">${srcCount} kaynak</span>`
    + `${num(c.collector_count) > srcCount ? `<span class="muted sm" title="Haberi taşıyan uygulama akışı (Google News, Yahoo, izleme listesi …) sayısı">${num(c.collector_count)} akış</span>` : ''}`
    + `${(num(c.item_count) || items.length) !== srcCount ? `<span class="muted sm">${num(c.item_count) || items.length} haber</span>` : ''}`
    + `${first ? `<span class="muted sm" title="${esc(tsTitle(first))}">ilk ${esc(whenShort(first))}</span>` : ''}`
    + `${last ? `<span class="muted sm" title="${esc(tsTitle(last))}">son ${esc(rel(last))}</span>` : ''}${impBar(c.importance_max)}</div></div>`
    + `${agreeMeter(ag, c.sentiment_avg)}</header>`
    + `<div class="clu-grid">${items.map((it, i) => cluItem(it, i === 0)).join('')}</div></article>`;
}
function clusterText(c) {
  return fold([c.title, ...arr(c.tickers), ...arr(c.items).map(i => `${i.title || ''} ${i.source_name || ''} ${pubOf(i)}`)].join(' '));
}

PAGES['kim-ne-diyor'] = {
  title: 'Kim Ne Diyor', poll: 60, bodyId: 'kn-body',
  mount(view, param) {
    this.data = null; this.sig = ''; this.prev = null;
    this.focus = param || '';
    if (this.focus && KF.since_hours < 72) KF.since_hours = 72;
    view.innerHTML = '<div class="page"><div class="toolbar sticky">'
      + searchBox('q', KF.q, 'Hikâye, hisse veya kaynak ara…')
      + selectHtml('since_hours', [[6, 'Son 6 saat'], [12, 'Son 12 saat'], [24, 'Son 24 saat'], [48, 'Son 2 gün'], [72, 'Son 3 gün']], KF.since_hours, '', 'Zaman aralığı')
      + selectHtml('min_sources', [[2, 'En az 2 kaynak'], [3, 'En az 3 kaynak'], [4, 'En az 4 kaynak'], [5, 'En az 5 kaynak']], KF.min_sources, '', 'Asgari kaynak')
      + selectHtml('sort', [['importance', 'Sıralama: önem'], ['recent', 'Sıralama: en yeni'], ['sources', 'Sıralama: kaynak sayısı'], ['disagree', 'Sıralama: en çok ayrışan']], KF.sort, '', 'Sıralama')
      + '<span class="sp"></span><span class="dim sm">Aynı haberi farklı kaynaklar nasıl verdi — başlıklar yan yana</span></div>'
      + `<div id="page-err"></div><div class="infobar" id="kn-info"></div><div id="kn-body">${stateHtml('loading')}</div></div>`;
  },
  onFilter(f, v, type) {
    if (f === 'q') { if (type === 'input') { KF.q = String(v); debounce('kn', () => this.render(true), 200); } return; }
    if (type !== 'change') return;
    if (f === 'sort') { KF.sort = String(v); this.render(true); return; }
    KF[f] = num(v) || KF[f];
    this.prev = null; this.sig = '';
    refreshPage(false);
  },
  onMeta() { this.render(true); },
  async refresh(first, live) {
    const d = await api('/api/clusters', { params: { since_hours: KF.since_hours, min_sources: KF.min_sources } });
    if (!live()) return;
    this.data = arr(d.items);
    this.available = d.available !== false;
    if (KF.min_sources === 2 && KF.since_hours >= 24) ingestClusters(this.data);
    this.render(false);
  },
  render(force) {
    if (!this.data) return;
    const q = fold(KF.q.trim());
    // Same publisher through two collectors (Google News + Yahoo …) is one source: re-check min_sources here.
    const list = this.data.filter(c => !q || clusterText(c).includes(q)).map(c => { const u = uniqPub(c.items); return { c, u, ag: pubAgreement(c, u) }; })
      .filter(x => !x.u.length || x.u.length >= KF.min_sources);
    const imp = x => num(x.c.importance_max) || 0, lastS = x => num(x.c.last_seen) || 0, srcs = x => x.u.length || num(x.c.source_count) || 0;
    const sorters = {
      importance: (a, b) => imp(b) - imp(a) || lastS(b) - lastS(a),
      recent: (a, b) => lastS(b) - lastS(a),
      sources: (a, b) => srcs(b) - srcs(a) || imp(b) - imp(a),
      disagree: (a, b) => (b.ag.conflict - a.ag.conflict) || (a.ag.pct - b.ag.pct) || imp(b) - imp(a),
    };
    list.sort(sorters[KF.sort] || sorters.importance);
    setHtml('kn-info', `<span class="info-l"><b>${list.length}</b> çok kaynaklı hikâye${q && list.length !== this.data.length ? ` · toplam ${this.data.length}` : ''}</span><span class="sp"></span><span class="dim sm">Yeşil olumlu · gri nötr · kırmızı olumsuz</span>`);
    const sig = KF.sort + '|' + list.map(x => x.c.cluster_id + ':' + arr(x.c.items).length).join('|');
    if (!force && sig === this.sig) return;
    if (!force && hasSelectionIn('kn-body')) return;
    this.sig = sig;
    const prev = this.prev;
    const html = list.length ? list.map(x => clusterCard(x.c, x.ag, prev ? !prev.has(x.c.cluster_id) : false, x.u)).join('')
      : stateHtml('empty', this.data.length && q ? 'Aramayla eşleşen hikâye yok.' : this.available === false ? 'Hikâye kümeleme modülü henüz hazır değil.' : 'Henüz birden fazla kaynağın verdiği ortak haber yok — kaynaklar çekildikçe burada görünecek.');
    keepScroll('.clu-card', () => setHtml('kn-body', html));
    this.prev = new Set(this.data.map(c => c.cluster_id));
    if (this.focus) {
      const el = byId('clu-' + this.focus);
      if (el) { el.scrollIntoView({ block: 'start' }); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
      else toast({ title: 'Seçilen haber kümesi bu filtrelerde görünmüyor', body: 'Zaman aralığını genişletmeyi deneyin.', kind: 'info' });
      this.focus = '';
    }
  },
};

// ============================================================ page: Fırsatlar

const OF = Object.assign({ dir: '', min: 0, wl: false, q: '' }, pick(obj(store.get('oppFilters', {})), ['dir', 'min', 'wl']));
function oppCard(o, isNew, open, newsHtml, noFooter) {
  const dir = DIRS[o.direction] ? o.direction : 'mixed';
  const sc = clamp(Math.round(num(o.score) || 0), 0, 100);
  const sym = String(o.ticker || '?');
  const reasons = arr(o.reasons).filter(Boolean);
  const sources = arr(o.sources).filter(Boolean);
  const nc = num(o.news_count) ?? arr(o.news_ids).length;
  const q = [];
  if (num(o.price) != null) q.push(`<b class="px">${fmtPrice(o.price)}</b>`);
  if (num(o.change_pct) != null) q.push(`<span class="chg ${dirClass(o.change_pct)}">${fmtPct(o.change_pct)}</span>`);
  if (num(o.volume) != null) q.push(`<span class="muted" title="Hacim">Hacim ${fmtCompact(o.volume)}</span>`);
  if (num(o.social_rank) != null) {
    const ch = num(o.social_change);
    q.push(`<span class="pill" title="Reddit (ApeWisdom) bahsedilme sırası${ch ? '; parantez içi 24 saatlik değişim' : ''}">Sosyal #${fmtInt(o.social_rank)}`
      + `${ch ? ` <span class="${ch > 0 ? 'up' : 'down'}">(${ch > 0 ? '+' : '−'}${fmtInt(Math.abs(ch))})</span>` : ''}</span>`);
  }
  return `<article class="card opp ${dir}${isNew ? ' flash' : ''}" data-id="${esc(sym)}">`
    + `<header class="opp-h">${tkChip(sym, 'big')}${o.company ? `<span class="opp-co" title="${esc(o.company)}">${esc(o.company)}</span>` : ''}${o.in_watchlist ? `<span class="star" title="İzleme listesinde">${icon('star')}</span>` : ''}`
    + `<span class="dirb ${dir}">${esc(ml('directions', dir, DIRS))}</span>${o.earnings_today ? '<span class="pill amber" title="Bugün bilanço açıklıyor">Bugün bilanço</span>' : ''}`
    + `<span class="sp"></span><div class="score" title="Fırsat skoru ${sc}/100"><b>${sc}</b><span class="sbar"><i style="width:${sc}%"></i></span></div></header>`
    + `<div class="opp-q">${q.length ? q.join('') : '<span class="muted">Fiyat verisi yok</span>'}</div>`
    + `${reasons.length ? `<ul class="reasons">${reasons.slice(0, 8).map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}`
    + `${arr(o.categories).length ? `<div class="n-meta">${catTags(o.categories, 6)}</div>` : ''}`
    + `${sources.length ? `<div class="opp-src"><span class="muted">Kaynaklar:</span>${sources.slice(0, 10).map(s => `<span class="src-t" style="--h:${hue(s)}">${esc(s)}</span>`).join('')}${sources.length > 10 ? `<span class="more">+${sources.length - 10}</span>` : ''}</div>` : ''}`
    + (noFooter ? `<div class="opp-f"><span class="muted sm">${fmtInt(nc)} haber · duygu ort. <b class="${dirClass(o.sentiment_avg)}">${fmtSigned(o.sentiment_avg)}</b></span></div>`
      : `<div class="opp-f"><button type="button" class="btn sm ghost" data-act="op-news" data-sym="${esc(sym)}" aria-expanded="${!!open}">${icon(open ? 'chevUp' : 'chevDown')} ${open ? 'Haberleri gizle' : `İlgili haberler (${fmtInt(nc)})`}</button>`
        + `<span class="sp"></span><span class="muted sm">Duygu ort. <b class="${dirClass(o.sentiment_avg)}">${fmtSigned(o.sentiment_avg)}</b></span></div>`)
    + `${open && !noFooter ? `<div class="opp-news">${newsHtml || stateHtml('loading')}</div>` : ''}</article>`;
}

PAGES.firsatlar = {
  title: 'Fırsatlar', poll: 60, bodyId: 'op-body',
  mount(view) {
    this.data = null; this.sig = ''; this.prev = null;
    this.open = this.open || new Set();
    this.newsCache = {};
    view.innerHTML = '<div class="page"><div class="toolbar sticky">'
      + searchBox('q', OF.q, 'Hisse veya gerekçe ara…')
      + segHtml('op-dir', [['', 'Tümü'], ['long', 'Yükseliş'], ['short', 'Düşüş'], ['mixed', 'Karışık']], OF.dir, 'Yön')
      + `<label class="range" title="Asgari fırsat skoru">Min skor <input type="range" min="0" max="100" step="5" data-f="min" value="${OF.min}" aria-label="Asgari skor"><output>${OF.min}</output></label>`
      + `<label class="chk"><input type="checkbox" data-f="wl"${OF.wl ? ' checked' : ''}> Sadece izleme listesi</label>`
      + '<span class="sp"></span><span class="dim sm">Kural tabanlı skor · yatırım tavsiyesi değildir</span></div>'
      + `<div id="page-err"></div><div class="infobar" id="op-info"></div><div id="op-body">${stateHtml('loading')}</div></div>`;
  },
  onFilter(f, v, type) {
    if (f === 'q') { if (type === 'input') { OF.q = String(v); debounce('op', () => this.render(true), 200); } return; }
    if (f === 'min') { OF.min = num(v) || 0; if (type === 'input') debounce('op', () => { store.set('oppFilters', pick(OF, ['dir', 'min', 'wl'])); this.render(true); }, 150); return; }
    if (f === 'wl' && type === 'change') { OF.wl = !!v; store.set('oppFilters', pick(OF, ['dir', 'min', 'wl'])); this.render(true); }
  },
  onMeta() { this.render(true); },
  setDir(v) {
    OF.dir = v;
    store.set('oppFilters', pick(OF, ['dir', 'min', 'wl']));
    for (const b of document.querySelectorAll('[data-act="op-dir"]')) { const on = b.dataset.v === v; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
    this.render(true);
  },
  async refresh(first, live) {
    const d = await api('/api/opportunities');
    if (!live()) return;
    if (obj(this.data).ts !== d.ts) this.newsCache = {};
    this.data = d;
    S.opps = d;
    updateOppBadge();
    this.render(false);
  },
  render(force) {
    if (!this.data) return;
    const all = arr(this.data.items);
    const q = fold(OF.q.trim());
    const list = all.filter(o => (!OF.dir || o.direction === OF.dir) && (num(o.score) || 0) >= OF.min && (!OF.wl || o.in_watchlist)
      && (!q || fold(`${o.ticker} ${o.company || ''} ${arr(o.reasons).join(' ')} ${arr(o.sources).join(' ')}`).includes(q)))
      .sort((a, b) => (num(b.score) || 0) - (num(a.score) || 0));
    const ts = num(this.data.ts), ws = num(this.data.window_start);
    setHtml('op-info', `<span class="info-l"><b>${list.length}</b> fırsat${list.length !== all.length ? ` · toplam ${all.length}` : ''}</span>`
      + `${ts ? `<span class="muted sm" title="${esc(tsTitle(ts))}">Hesaplandı ${esc(rel(ts))}</span>` : ''}`
      + `${ws ? `<span class="muted sm" title="${esc(tsTitle(ws))}">Haber penceresi: ${esc(whenShort(ws))} sonrası</span>` : ''}<span class="sp"></span>`);
    const sig = list.map(o => `${o.ticker}:${o.score}:${this.open.has(o.ticker) ? 1 : 0}`).join('|') + '|' + ts;
    if (!force && sig === this.sig) return;
    this.sig = sig;
    const prev = this.prev;
    let html;
    if (list.length) html = `<div class="opp-grid">${list.map(o => oppCard(o, prev ? !prev.has(o.ticker) : false, this.open.has(o.ticker), this.newsCache[o.ticker])).join('')}</div>`;
    else if (all.length) html = stateHtml('empty', 'Bu filtrelerle eşleşen fırsat yok.');
    else html = stateHtml('empty', 'Henüz fırsat hesaplanmadı — haberler toplandıkça skorlar burada görünecek.');
    keepScroll('.opp', () => setHtml('op-body', html));
    this.prev = new Set(all.map(o => o.ticker));
  },
  async toggleNews(sym) {
    if (this.open.has(sym)) { this.open.delete(sym); this.render(true); return; }
    this.open.add(sym);
    this.render(true);
    if (this.newsCache[sym]) return;
    const o = arr(obj(this.data).items).find(x => x.ticker === sym) || {};
    if (Array.isArray(o.news) && o.news.length && typeof o.news[0] === 'object') {
      this.newsCache[sym] = miniNewsList(o.news.slice(0, 12));
    } else {
      try {
        const d = await api('/api/news', { params: { ticker: sym, since_hours: 72, limit: 60 } });
        const ids = new Set(arr(o.news_ids));
        let items = arr(d.items);
        const linked = items.filter(i => ids.has(i.id));
        items = (linked.length ? linked : items).slice(0, 12);
        this.newsCache[sym] = items.length ? miniNewsList(items) : '<div class="state sm">İlgili haber bulunamadı.</div>';
      } catch (e) {
        this.newsCache[sym] = `<div class="state sm err">${esc(errMsg(e))}</div>`;
      }
    }
    if (cur && cur.key === 'firsatlar') this.render(true);
  },
};

// ============================================================ page: Piyasa Panosu

function quoteItems(q) { return Array.isArray(q) ? q : arr(obj(q).items); }
function extHtml(q) {
  const s = String(q.ext_session || '').toLowerCase(), v = num(q.ext_change_pct);
  if (!s || v == null) return '';
  const lbl = s === 'pre' ? 'Öncesi' : 'Sonrası';
  return `<span class="ext ${dirClass(v)}" title="${s === 'pre' ? 'Seans öncesi' : 'Seans sonrası'} işlem${num(q.ext_price) != null ? ': ' + fmtPrice(q.ext_price) : ''}${num(q.regular_price) != null ? ' · seans kapanışı ' + fmtPrice(q.regular_price) : ''}">${lbl} ${fmtPct(v)}</span>`;
}
function quoteTip(q) {
  const st = MARKET_STATE[String(q.market_state || '').toUpperCase()] || '';
  const dd = isFx(q) ? 4 : null;
  return [`${q.name || q.symbol} (${q.symbol || ''})`, st, num(q.prev_close) != null ? 'Önceki kapanış ' + fmtPrice(q.prev_close, dd) : '',
    num(q.day_low) != null && num(q.day_high) != null ? `Gün aralığı ${fmtPrice(q.day_low, dd)} – ${fmtPrice(q.day_high, dd)}` : '',
    num(q.last_ts) ? 'Son fiyat: ' + tsTitle(q.last_ts) : ''].filter(Boolean).join(' · ');
}
function quoteTile(q) {
  const ch = num(q.change_pct), d = dirClass(ch), dd = isFx(q) ? 4 : null;
  return `<div class="tile ${d}" title="${esc(quoteTip(q))}">`
    + `<div class="tl-h"><span class="tl-n">${esc(q.name || q.symbol || '?')}</span><span class="tl-s">${esc(q.symbol || '')}</span></div>`
    + `<div class="tl-p">${fmtPrice(q.price, dd)}</div>`
    + `<div class="tl-c ${d}"><span>${fmtSigned(q.change, dd || 2)}</span><b>${fmtPct(ch)}</b>${extHtml(q)}</div>`
    + spark(q.spark, { base: q.prev_close, w: 140, h: 34 }) + '</div>';
}
function quotesHtml(q) {
  const items = quoteItems(q).filter(x => x && x.group !== 'izleme');
  if (!items.length) return stateHtml('empty', 'Henüz fiyat verisi yok — piyasa verileri çekiliyor…');
  const known = new Set(QUOTE_GROUPS.map(g => g[0]));
  const groups = groupBy(items, x => (known.has(x.group) ? x.group : 'diger'));
  return QUOTE_GROUPS.filter(([k]) => groups[k]).map(([k, l]) => `<div class="qgroup"><div class="qg-l">${esc(ml('quote_groups', k, { [k]: l }))}</div><div class="tiles">${groups[k].map(quoteTile).join('')}</div></div>`).join('');
}
function fgHtml(fg) {
  fg = obj(fg);
  const s = num(fg.score);
  if (s == null) return stateHtml('empty', 'Korku & Açgözlülük verisi henüz yok.');
  const band = fgBand(s);
  const rating = fg.rating_tr || FG_RATING[String(fg.rating || '').toLowerCase()] || (band ? band[3] : '') || String(fg.rating || '');
  const cmp = (label, v) => {
    const n = num(v);
    if (n == null) return '';
    const b = fgBand(n);
    return `<div class="fg-c"><span>${label}</span><b style="color:${b ? b[2] : 'inherit'}">${Math.round(n)}</b><small>${esc(b ? b[3] : '')}</small></div>`;
  };
  const hist = arr(fg.history).map(h => num(h && h.score)).filter(v => v != null);
  const ind = arr(fg.indicators).filter(i => i && num(i.score) != null);
  return `<div class="fg">${gaugeSvg(s)}<div class="fg-v"><b style="color:${band ? band[2] : 'inherit'}">${Math.round(s)}</b><span>${esc(rating)}</span>`
    + `${fg.is_proxy ? '<small class="dim">yaklaşık değer (yedek hesaplama)</small>' : ''}</div>`
    + `<div class="fg-cmp">${cmp('Önceki kapanış', fg.previous_close)}${cmp('1 hafta önce', fg.week_ago)}${cmp('1 ay önce', fg.month_ago)}${cmp('1 yıl önce', fg.year_ago)}</div>`
    + (hist.length > 2 ? `<div class="fg-hist"><span class="dim sm">Son ${hist.length} işlem günü</span>${spark(hist, { w: 260, h: 34 })}</div>` : '')
    + (ind.length ? `<details class="fg-ind"><summary>Alt göstergeler (${ind.length})</summary>${ind.map(i => {
      const b = fgBand(i.score);
      return `<div class="fgi"><span class="fgi-n" title="${esc(i.name || i.key || '')}">${esc(i.name || i.key || '')}</span><span class="fgi-b"><i style="width:${clamp(num(i.score), 0, 100)}%;background:${b ? b[2] : 'var(--neu)'}"></i></span>`
        + `<b>${Math.round(num(i.score))}</b><small>${esc(i.rating_tr || (b ? b[3] : ''))}</small></div>`;
    }).join('')}</details>` : '')
    + '</div>';
}
function moverTable(title, rows, cls, mv, key) {
  rows = arr(rows).filter(r => r && r.symbol).slice(0, 12);
  const extKey = ['pre_change_pct', 'post_change_pct'].find(k => rows.some(r => num(r[k]) != null && Math.abs(num(r[k]) - (num(r.change_pct) || 0)) > 0.005)) || '';
  const extLbl = extKey === 'pre_change_pct' ? 'Öncesi' : 'Sonrası';
  // Pre/post tables: price and change_pct are the extended-hours values; show the regular session next to them.
  const isExt = key === 'premarket' || key === 'postmarket';
  const regCol = isExt && rows.some(r => num(r.regular_change_pct) != null);
  // Server adds `catalyst` (headline or null) to every row once the movers_why snapshot exists.
  const whyCol = rows.some(r => Object.prototype.hasOwnProperty.call(r, 'catalyst'));
  let note = '';
  if (isExt) {
    const sess = effSession(nowSec());
    // During the regular session the collector keeps this morning's gappers (and last night's after-hours movers).
    if (key === 'premarket' && sess !== 'premarket') note = ' · açılış öncesi veri';
    else if (key === 'postmarket' && (sess === 'premarket' || sess === 'open')) note = ' · önceki akşam verisi';
  }
  const body = rows.map(r => {
    const vr = num(r.rel_volume) ?? (num(r.avg_volume) ? (num(r.volume) || 0) / num(r.avg_volume) : null);
    const tip = [r.name, r.exchange, num(r.market_cap) ? 'Piyasa değeri ' + fmtCap(r.market_cap) : '', num(r.prev_close) != null ? 'Önceki kapanış ' + fmtPrice(r.prev_close) : ''].filter(Boolean).join(' · ');
    return `<tr><td class="l"${whyCol && tip ? ` title="${esc(tip)}"` : ''}>${tkChip(r.symbol)}</td><td class="l ell nm" title="${esc(tip)}">${esc(r.name || '')}</td>`
      + `<td>${fmtPrice(r.price)}</td><td class="${dirClass(r.change_pct)}"><b>${fmtPct(r.change_pct)}</b></td>`
      + `${extKey ? `<td class="${dirClass(r[extKey])}">${fmtPct(r[extKey])}</td>` : ''}`
      + `${regCol ? `<td class="${dirClass(r.regular_change_pct)}"${num(r.regular_price) != null ? ` title="Seans kapanışı ${fmtPrice(r.regular_price)}"` : ''}>${fmtPct(r.regular_change_pct)}</td>` : ''}`
      + `<td title="${num(r.avg_volume) ? 'Ortalama hacim ' + fmtCompact(r.avg_volume) : ''}">${fmtCompact(r.volume)}${vr != null && vr >= 1.5 ? ` <small class="vr${vr >= 3 ? ' hot' : ''}" title="Ortalama hacmin ${nf(1, 1).format(vr)} katı">×${nf(1, 1).format(vr)}</small>` : ''}</td>`
      + `${whyCol ? `<td class="l why">${catalystHtml(r.catalyst)}</td>` : ''}</tr>`;
  }).join('');
  const chgTh = isExt ? `<th title="${key === 'premarket' ? 'Seans öncesi' : 'Seans sonrası'} değişim">Uzatılmış</th>` : '<th>Değ.</th>';
  return `<section class="panel mv ${cls || ''}${whyCol ? ' has-why' : ''}"><h3 class="ph">${esc(title + note)}${isExt ? ` <span class="sp"></span>${upd(obj(mv).ext_ts)}` : ''}</h3>`
    + (rows.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l nm">Ad</th><th>Fiyat</th>${chgTh}${extKey ? `<th title="${extLbl === 'Öncesi' ? 'Seans öncesi' : 'Seans sonrası'} değişim">${extLbl}</th>` : ''}`
      + `${regCol ? '<th title="Normal seans değişimi">Seans</th>' : ''}<th>Hacim</th>${whyCol ? '<th class="l why" title="Hareketi açıklayan haber (otomatik eşleştirme)">Neden?</th>' : ''}</tr></thead><tbody>${body}</tbody></table></div>`
      : stateHtml('empty', 'Veri yok')) + '</section>';
}
/** "Neden?" cell: the catalyst headline (opens in the browser) or a dim dash. */
function catalystHtml(c) {
  c = c && typeof c === 'object' ? c : null;
  if (!c || !c.title) return '<span class="dim">—</span>';
  const pub = String(c.publisher || '').trim(), sn = String(c.source_name || '').trim();
  const who = pub && sn && fold(pub) !== fold(sn) ? `${pub} (${sn})` : pub || sn;
  const cats = arr(c.categories).map(k => catInfo(k).label).filter(Boolean);
  const tip = [c.title, who, cats.join(', '), num(c.source_count) > 1 ? `${num(c.source_count)} kaynak` : '',
    num(c.published_at) ? tsTitle(c.published_at) : ''].filter(Boolean).join(' · ');
  const url = safeUrl(c.url);
  const t = esc(trunc(c.title, 90));
  return url ? `<a class="why-a s-${sentKey(c.sentiment_label)}" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(tip)}">${t}</a>`
    : `<span class="why-a" title="${esc(tip)}">${t}</span>`;
}
/** Official trading halts that have not resumed yet (snapshot `halts`). */
function haltsHtml(h) {
  h = obj(h);
  const items = arr(h.items).filter(r => r && r.symbol);
  const luld = Object.entries(obj(h.luld_today)).filter(([, n]) => num(n) >= 3).sort((a, b) => num(b[1]) - num(a[1]));
  if (!items.length && !luld.length) return '';
  const now = nowSec();
  const rows = items.slice(0, 15).map(r => {
    const res = num(r.resume_ts);
    return `<tr><td class="l">${tkChip(r.symbol)}</td><td class="l ell" title="${esc([r.name, r.market].filter(Boolean).join(' · '))}">${esc(r.name || '')}</td>`
      + `<td class="l" title="Kod ${esc(r.reason_code || '—')}">${esc(r.reason_tr || r.reason_code || '—')}</td>`
      + `<td title="${esc(tsTitle(r.halt_ts))}">${num(r.halt_ts) ? esc(whenShort(r.halt_ts)) : '—'}</td>`
      + `<td title="${res ? esc(tsTitle(res)) : ''}">${res ? `${esc(hm(res))}${res > now ? ` <small class="dim">${esc(rel(res))}</small>` : ''}` : '<span class="dim">belirsiz</span>'}</td></tr>`;
  }).join('');
  const src = h.source === 'nyse' ? 'NYSE listesi' : h.source ? 'Nasdaq Trader' : '';
  return `<section class="panel halts"><h3 class="ph">İşlem durdurmaları <span class="upd">${items.length} devam ediyor${src ? ' · ' + esc(src) : ''}</span><span class="sp"></span>${upd(h.ts)}</h3>`
    + (items.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Ad</th><th class="l">Neden</th><th>Durdurma (TR)</th><th>Açılış (TR)</th></tr></thead><tbody>${rows}</tbody></table></div>` : '')
    + (luld.length ? `<div class="hint">Bugün en az 3 volatilite molası (LULD): ${luld.slice(0, 12).map(([s, n]) => `${tkChip(s)} <small>${fmtInt(n)}×</small>`).join(' ')}</div>` : '')
    + '</section>';
}
function moversHtml(mv) {
  mv = obj(mv);
  const parts = [['gainers', 'En çok yükselenler', 'g'], ['losers', 'En çok düşenler', 'l'], ['actives', 'En aktifler (hacim)', 'a'],
    ['premarket', 'Seans öncesi hareketliler', 'x'], ['postmarket', 'Seans sonrası hareketliler', 'x'], ['small_caps', 'Küçük ölçekli yükselenler', 's']];
  if (!parts.some(([k]) => arr(mv[k]).length)) return `<div class="panel">${stateHtml('empty', 'Hareketli hisse verisi henüz yok.')}</div>`;
  return parts.filter(([k], i) => i < 3 || arr(mv[k]).length).map(([k, t, c]) => moverTable(t, mv[k], c, mv, k)).join('');
}
function watchHtml(q) {
  const items = quoteItems(q).filter(x => x && x.group === 'izleme');
  if (!items.length) return stateHtml('empty', 'İzleme listesi fiyatları henüz yok.');
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Ad</th><th>Fiyat</th><th>Değişim</th><th>%</th><th title="Seans öncesi / sonrası">Uzatılmış</th><th class="c">Gün içi</th></tr></thead><tbody>'
    + items.map(x => `<tr title="${esc(quoteTip(x))}"><td class="l">${tkChip(x.symbol)}</td><td class="l ell">${esc(x.name || '')}</td><td>${fmtPrice(x.price, isFx(x) ? 4 : null)}</td>`
      + `<td class="${dirClass(x.change)}">${fmtSigned(x.change, isFx(x) ? 4 : 2)}</td><td class="${dirClass(x.change_pct)}"><b>${fmtPct(x.change_pct)}</b></td>`
      + `<td>${extHtml(x) || '<span class="dim">—</span>'}</td>`
      + `<td class="c">${spark(x.spark, { base: x.prev_close, w: 90, h: 22, cls: 'mini' })}</td></tr>`).join('')
    + '</tbody></table></div>';
}
const SOCIAL_ERR_NAMES = { stocktwits: 'StockTwits', apewisdom: 'ApeWisdom', apewisdom_wsb: 'ApeWisdom WSB', yahoo_trending: 'Yahoo trend', reddit: 'Reddit' };
function errsHtml(errs) {
  return errs.length ? `<div class="hint">${errs.map(([k, v]) => `${esc(SOCIAL_ERR_NAMES[k] || k)}: ${esc(trunc(v, 140))}`).join('<br>')}</div>` : '';
}
function socialHtml(so, which) {
  so = obj(so);
  const wsb = arr(so.apewisdom_wsb).filter(r => r && r.symbol);
  const useWsb = which === 'wsb' && wsb.length > 0;
  const ape = (useWsb ? wsb : arr(so.apewisdom).filter(r => r && r.symbol)).slice(0, 15);
  const st = arr(so.stocktwits).filter(r => r && r.symbol).slice(0, 30);
  const yt = arr(so.yahoo_trending).filter(r => r && r.symbol).slice(0, 30);
  const errs = Object.entries(obj(so.errors)).filter(e => e[1]);
  if (!ape.length && !st.length && !yt.length) return stateHtml('empty', errs.length ? 'Sosyal medya verisi alınamadı.' : 'Sosyal medya verisi henüz yok.') + errsHtml(errs);
  let h = '';
  if (ape.length) {
    h += `<div class="sub-row"><h4 class="sub">Reddit (ApeWisdom) — en çok konuşulanlar</h4>${wsb.length ? segHtml('so-src', [['all', 'Tüm Reddit'], ['wsb', 'r/wallstreetbets']], useWsb ? 'wsb' : 'all', 'Reddit kaynağı') : ''}</div>`
      + '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>#</th><th class="l">Sembol</th><th class="l">Ad</th><th>Bahsedilme</th><th title="24 saat öncesine göre bahsedilme değişimi">Δ 24 sa</th><th title="Sıra değişimi (24 saat)">Sıra Δ</th></tr></thead><tbody>'
      + ape.map(r => {
        const mPct = num(r.mentions_change_pct);
        const mAbs = num(r.mentions) != null && num(r.mentions_24h_ago) != null ? num(r.mentions) - num(r.mentions_24h_ago) : null;
        const rd = num(r.rank_change) ?? (num(r.rank) != null && num(r.rank_24h_ago) != null ? num(r.rank_24h_ago) - num(r.rank) : null);
        const mTxt = mPct != null ? fmtPct(mPct, 0) : mAbs != null ? fmtSigned(mAbs, 0) : '—';
        return `<tr><td class="dim">${fmtInt(r.rank)}</td><td class="l">${tkChip(r.symbol)}</td><td class="l ell" title="${esc(r.name || '')}">${esc(r.name || '')}</td>`
          + `<td title="${num(r.upvotes) != null ? fmtInt(r.upvotes) + ' beğeni' : ''}">${fmtInt(r.mentions)}</td><td class="${dirClass(mPct ?? mAbs)}">${mTxt}</td>`
          + `<td class="${dirClass(rd)}">${rd == null ? '—' : rd === 0 ? '=' : (rd > 0 ? '▲' : '▼') + Math.abs(rd)}</td></tr>`;
      }).join('') + '</tbody></table></div>';
  }
  const chips = (title, list) => `<h4 class="sub">${title}</h4><div class="st-chips">`
    + list.map(r => `<span class="st-chip" title="${esc(r.title || r.symbol)}${num(r.watchlist_count) ? ' · ' + fmtInt(r.watchlist_count) + ' takipçi' : ''}">${tkChip(r.symbol)}`
      + `${r.title ? `<span class="ell">${esc(trunc(r.title, 28))}</span>` : num(r.rank) ? `<span class="dim">#${fmtInt(r.rank)}</span>` : ''}</span>`).join('') + '</div>';
  if (st.length) h += chips('StockTwits — trend olanlar', st);
  if (yt.length) h += chips('Yahoo Finance — trend semboller', yt);
  return h + errsHtml(errs);
}
function upd(ts) { ts = num(ts); return ts ? `<span class="upd" title="${esc(tsTitle(ts))}">${esc(rel(ts))}</span>` : ''; }
function nextBriefsHtml() {
  const nb = arr(obj(S.status).next_briefs).filter(b => num(b.ts));
  if (!nb.length) return '';
  return '<div class="nb"><span class="muted">Sıradaki brifingler:</span>' + nb.slice(0, 3).map(b => `<span class="pill" title="${esc(tsTitle(b.ts))}">${esc(ml('brief_kinds', b.kind, BRIEF_KINDS))} ${esc(hm(b.ts))}</span>`).join('') + '</div>';
}

PAGES.pano = {
  title: 'Piyasa Panosu', poll: 60, bodyId: 'd-quotes',
  mount(view) {
    this.data = null;
    this.mvSrc = this.mvSrc || 'yahoo';
    this.soSrc = this.soSrc || 'all';
    const p = zp(nowSec(), TZ_ET);
    const trAt = (h, m) => hm(zTs(p.y, p.mo, p.d, h, m, TZ_ET));
    view.innerHTML = '<div class="page page-dash"><div id="page-err"></div>'
      + '<div class="dash-top"><section class="panel clock-panel"><h3 class="ph">Seans saati</h3>'
      + '<div class="clk-row"><div class="clk"><span class="clk-l">New York (ET)</span><b id="dc-et">--:--:--</b><span id="dc-et-d" class="muted"></span></div>'
      + '<div class="clk"><span class="clk-l">İstanbul (TR)</span><b id="dc-tr">--:--:--</b><span id="dc-tr-d" class="muted"></span></div>'
      + '<div class="clk sess-col"><span id="dc-sess" class="sess closed">—</span><div id="dc-cd" class="cd-list"></div></div></div>'
      + '<div class="sbar-wrap"><div class="sessbar" aria-hidden="true"><i class="pre" style="width:34.375%"></i><i class="reg" style="width:40.625%"></i><i class="post" style="width:25%"></i><span class="mark" id="dc-mark"></span></div>'
      + `<div class="sb-lbls"><span style="left:0">${trAt(4, 0)}<small>öncesi</small></span><span style="left:34.375%">${trAt(9, 30)}<small>açılış</small></span><span style="left:75%">${trAt(16, 0)}<small>kapanış</small></span><span style="left:100%">${trAt(20, 0)}<small>son</small></span></div>`
      + '<div class="dim sm sb-note" id="dc-note">Zaman çizelgesi TR saatiyle (ET 04:00 – 20:00)</div></div>'
      + `<div id="dc-nb">${nextBriefsHtml()}</div></section>`
      + `<section class="panel fg-panel"><h3 class="ph">Korku & Açgözlülük <span class="sp"></span><span id="d-fg-upd"></span></h3><div id="d-fg">${stateHtml('loading')}</div></section></div>`
      + `<section class="panel"><h3 class="ph">Vadeli, endeksler ve göstergeler <span class="sp"></span><span id="d-q-upd"></span></h3><div id="d-quotes">${stateHtml('loading')}</div></section>`
      + '<div class="ph-row"><h3 class="ph">Günün hareketlileri <span id="d-mv-upd"></span></h3><span class="sp"></span><div id="d-mv-src"></div></div>'
      + `<div class="mv-grid" id="d-movers">${stateHtml('loading')}</div><div id="d-halts"></div>`
      + `<div class="dash-2"><section class="panel"><h3 class="ph">İzleme listesi</h3><div id="d-watch">${stateHtml('loading')}</div></section>`
      + `<section class="panel"><h3 class="ph">Sosyal medya nabzı <span class="sp"></span><span id="d-so-upd"></span></h3><div id="d-social">${stateHtml('loading')}</div></section></div></div>`;
    const n0 = nowSec(), s0 = effSession(n0);
    this.tick(n0, s0, curTargets(s0, n0));
    this.onStatus();
  },
  onStatus() {
    setHtml('dc-nb', nextBriefsHtml());
    const st = obj(S.status);
    setText('dc-note', st.trading_day === false ? 'Bugün ABD piyasası kapalı (hafta sonu / tatil) · saatler TR' : 'Zaman çizelgesi TR saatiyle (ET 04:00 – 20:00)');
  },
  onError(e) {
    const h = stateHtml('error', errMsg(e));
    for (const id of ['d-fg', 'd-quotes', 'd-watch', 'd-social']) setHtml(id, h);
    setHtml('d-movers', `<div class="panel">${h}</div>`);
  },
  tick(now, sess, tg) {
    if (!byId('dc-et')) return;
    setText('dc-et', hms(now, TZ_ET));
    setText('dc-tr', hms(now, TZ_TR));
    setText('dc-et-d', dayName(now, TZ_ET));
    setText('dc-tr-d', dayName(now, TZ_TR));
    const s = byId('dc-sess');
    if (s) { const cls = 'sess big ' + sess; if (s.className !== cls) s.className = cls; setText('dc-sess', sessLabel(sess)); }
    setHtml('dc-cd', tg.map(t => `<div><span class="muted">${esc(t.label)}</span> <b>${esc(dur(t.ts - now))}</b></div>`).join('') || '<div class="muted">—</div>');
    const p = zp(now, TZ_ET), mins = p.h * 60 + p.mi + p.s / 60;
    const mk = byId('dc-mark');
    if (mk) {
      const inWin = mins >= 240 && mins <= 1200 && !(sess === 'closed');
      mk.hidden = !inWin;
      if (inWin) mk.style.left = `${clamp((mins - 240) / 960, 0, 1) * 100}%`;
    }
  },
  async refresh(first, live) {
    const d = await api('/api/market');
    if (!live()) return;
    this.data = d;
    S.market = d;
    this.render();
  },
  render() {
    const d = obj(this.data), u = obj(d.updated);
    // The 60 s re-render must not collapse an opened "Alt göstergeler" panel.
    const fgOpen = HAS_DOM && !!document.querySelector('#d-fg details[open]');
    setHtml('d-fg', fgHtml(d.fear_greed));
    if (fgOpen) { const x = document.querySelector('#d-fg details'); if (x) x.open = true; }
    setHtml('d-fg-upd', upd(u.fear_greed || obj(d.fear_greed).ts));
    setHtml('d-quotes', quotesHtml(d.quotes));
    setHtml('d-q-upd', upd(u.quotes || obj(d.quotes).ts));
    setHtml('d-watch', watchHtml(d.quotes));
    setHtml('d-social', socialHtml(d.social, this.soSrc));
    setHtml('d-so-upd', upd(u.social || obj(d.social).ts));
    const ma = obj(d.movers_alpaca);
    const hasA = ['gainers', 'losers', 'actives'].some(k => arr(ma[k]).length);
    if (!hasA) this.mvSrc = 'yahoo';
    setHtml('d-mv-src', hasA ? segHtml('mv-src', [['yahoo', 'Yahoo'], ['alpaca', 'Alpaca']], this.mvSrc, 'Hareketliler kaynağı') : '');
    const mv = this.mvSrc === 'alpaca' ? ma : obj(d.movers);
    setHtml('d-mv-upd', upd(this.mvSrc === 'alpaca' ? (u.movers_alpaca || ma.ts) : (u.movers || mv.ts)));
    setHtml('d-movers', moversHtml(mv));
    setHtml('d-halts', haltsHtml(d.halts));
  },
};

// ============================================================ page: Takvim

const CF = Object.assign({ days: 7, kind: '', impact: '', us: false, wl: false, q: '' }, pick(obj(store.get('calFilters', {})), ['days', 'kind', 'impact', 'us', 'wl']));
function saveCF() { store.set('calFilters', pick(CF, ['days', 'kind', 'impact', 'us', 'wl'])); }
function isUS(c) { return /^(US|USD|USA|United States|ABD)$/i.test(String(c || '').trim()); }
function etDayKey(ts) { const p = zp(ts, TZ_ET); return `${p.y}-${pad2(p.mo)}-${pad2(p.d)}`; }
/** Calendar event family: economic release, earnings report or IPO (anything else counts as macro). */
function calKind(e) { return e.kind === 'earnings' || e.kind === 'ipo' ? e.kind : 'macro'; }
/** Reported EPS: numeric extra.eps_actual (from the calendar collector) or the raw `actual` text. */
function epsActual(e) {
  const ex = obj(e.extra);
  if (ex.eps_actual != null && ex.eps_actual !== '') return ex.eps_actual;
  return e.actual ? e.actual : null;
}
function calendarHtml(items, f, wl, expanded) {
  const now = nowSec();
  const q = fold(String(f.q || '').trim());
  const rank = { high: 3, medium: 2, low: 1 };
  const inWl = e => !!e.in_watchlist || wl.has(String(e.ticker || '').toUpperCase());
  const ev = arr(items).filter(e => e && num(e.ts)).filter(e => {
    const k = calKind(e);
    if (f.kind && k !== f.kind) return false;
    if (k === 'macro') {
      if (f.impact === 'high' && e.impact !== 'high') return false;
      if (f.impact === 'medium' && (rank[e.impact] || 0) < 2) return false;
      if (f.us && !isUS(e.country)) return false;
    } else if (f.wl && !inWl(e)) return false;
    if (q) {
      const ex = obj(e.extra);
      if (!fold(`${e.title || ''} ${ex.title_tr || ''} ${e.ticker || ''} ${ex.company || ''} ${e.country || ''}`).includes(q)) return false;
    }
    return true;
  });
  if (!ev.length) return stateHtml('empty', arr(items).length ? 'Bu filtrelerle eşleşen etkinlik yok.' : 'Takvim verisi henüz yok — kaynaklar çekiliyor…');
  const byDay = groupBy(ev, e => etDayKey(num(e.ts)));
  const nextMacro = ev.filter(e => calKind(e) === 'macro' && !e.all_day && num(e.ts) > now).sort((a, b) => a.ts - b.ts)[0];
  const cap = e => num(obj(e.extra).market_cap) || 0;
  const surp = e => num(obj(e.extra).surprise_pct);
  return Object.keys(byDay).sort().map(day => {
    const list = byDay[day];
    const ts0 = num(list[0].ts);
    const dd = dayDiff(ts0, TZ_ET);
    const macro = list.filter(e => calKind(e) === 'macro').sort((a, b) => a.ts - b.ts);
    const ipos = list.filter(e => calKind(e) === 'ipo')
      .sort((a, b) => (inWl(b) ? 1 : 0) - (inWl(a) ? 1 : 0) || (num(obj(b.extra).offer_amount) || 0) - (num(obj(a.extra).offer_amount) || 0));
    let earn = list.filter(e => calKind(e) === 'earnings');
    // Reported days: the biggest beats / misses of companies worth $2B+ first (micro-cap surprises are noise).
    const results = earn.some(e => surp(e) != null || epsActual(e) != null);
    const big = e => cap(e) >= 2e9 && surp(e) != null;
    earn.sort((a, b) => {
      const w = (inWl(b) ? 1 : 0) - (inWl(a) ? 1 : 0);
      if (w) return w;
      if (results) {
        const g = (big(b) ? 1 : 0) - (big(a) ? 1 : 0);
        if (g) return g;
        if (big(a)) { const s = Math.abs(surp(b)) - Math.abs(surp(a)); if (s) return s; }
      }
      return cap(b) - cap(a) || String(a.ticker).localeCompare(String(b.ticker));
    });
    const totalEarn = earn.length, isOpen = expanded.has(day);
    if (!isOpen) earn = earn.slice(0, 25);
    const mRows = macro.map(e => {
      const ex = obj(e.extra), t = num(e.ts);
      const past = !e.all_day && t < now - 60;
      const imp = IMPACT[e.impact] ? e.impact : 'none';
      const trT = ex.title_tr && ex.title_tr !== e.title ? ex.title_tr : '';
      return `<tr class="${past ? 'past' : ''}${nextMacro === e ? ' next' : ''}">`
        + `<td class="l tm">${e.all_day ? '<b>Tüm gün</b>' : `<b title="${esc(tsTitle(t))}">${hm(t)}</b><small>${hm(t, TZ_ET)} ET</small>`}</td>`
        + `<td class="l"><span class="cty">${esc(e.country || '—')}</span></td>`
        + `<td class="l"><span class="impact ${imp}">${esc(IMPACT[e.impact] ? ml('impacts', e.impact, IMPACT) : '—')}</span></td>`
        + `<td class="l ev"><b>${esc(trT || e.title || '')}</b>${trT ? `<small>${esc(e.title || '')}</small>` : ''}${nextMacro === e ? `<span class="pill accent">sıradaki · ${esc(rel(t))}</span>` : ''}`
        + `${ex.provisional ? `<span class="pill amber prov" title="${esc(ex.note_tr || 'ForexFactory bu günü henüz yayımlamadı; Nasdaq takviminden geçici olarak eklendi.')}">Geçici</span>` : ''}</td>`
        + `<td>${esc(e.forecast || '—')}</td><td>${esc(e.previous || '—')}</td><td class="act">${e.actual ? `<b>${esc(e.actual)}</b>` : '—'}</td></tr>`;
    }).join('');
    const eRows = earn.map(e => {
      const ex = obj(e.extra), sym = String(e.ticker || '').toUpperCase(), w = inWl(e);
      const tm = EARN_TIME[ex.time] ? ex.time : 'unknown';
      const act = epsActual(e), sp = surp(e);
      return `<tr class="${w ? 'wl' : ''}"><td class="l">${tkChip(sym)}${w ? `<span class="star" title="İzleme listesinde">${icon('star')}</span>` : ''}</td>`
        + `<td class="l ell" title="${esc(ex.company || e.title || '')}">${esc(ex.company || e.title || '')}</td>`
        + `<td class="l"><span class="etime ${tm}">${esc(ml('earnings_times', tm, EARN_TIME))}</span></td>`
        + `<td title="${ex.num_estimates ? esc(ex.num_estimates + ' analist tahmini') : ''}${ex.last_year_eps != null ? esc(' · geçen yıl ' + ex.last_year_eps) : ''}">${fmtEps(ex.eps_forecast != null && ex.eps_forecast !== '' ? ex.eps_forecast : e.forecast)}</td>`
        + (results ? `<td class="act">${act != null ? `<b>${fmtEps(act)}</b>` : '—'}</td><td class="${dirClass(sp)}">${sp != null ? `<b>${fmtPct(sp, 1)}</b>` : '—'}</td>` : '')
        + `<td>${fmtCap(ex.market_cap)}</td></tr>`;
    }).join('');
    const iRows = ipos.map(e => {
      const ex = obj(e.extra), sym = String(e.ticker || '').toUpperCase(), w = inWl(e);
      const px = num(ex.price) != null ? '$' + fmtPrice(ex.price) : ex.price ? esc(ex.price) : '—';
      return `<tr class="${w ? 'wl' : ''}"><td class="l">${tkChip(sym)}</td>`
        + `<td class="l ell" title="${esc(ex.company || e.title || '')}">${esc(ex.company || e.title || '')}</td>`
        + `<td class="l ell" title="${esc(ex.exchange || '')}">${esc(ex.exchange || '—')}</td>`
        + `<td>${px}</td><td>${fmtCompact(ex.shares)}</td><td>${fmtCap(ex.offer_amount)}</td>`
        + `<td class="l">${esc(ex.status_tr || ex.status || '—')}</td></tr>`;
    }).join('');
    const dn = dayName(ts0, TZ_ET);
    let label = dd === 0 ? 'Bugün · ' + dn : dd === 1 ? 'Yarın · ' + dn : dd === -1 ? 'Dün · ' + dn : dn;
    if (dd < 0 && results) label += ' · sonuçlar';
    const blocks = [macro.length, totalEarn, ipos.length].filter(Boolean).length;
    return `<section class="panel cal-day${dd === 0 ? ' today' : ''}" data-day="${day}" data-id="${day}"><h3 class="ph">${esc(label)} <span class="upd">${macro.length} ekonomik veri · ${totalEarn} bilanço${ipos.length ? ` · ${ipos.length} halka arz` : ''}</span></h3>`
      + `<div class="cal-cols${blocks > 1 ? '' : ' one'}">`
      + (macro.length ? `<div><h4 class="sub">Ekonomik takvim</h4><div class="tbl-wrap"><table class="tbl cal-macro"><thead><tr><th class="l">Saat (TR)</th><th class="l">Ülke</th><th class="l">Önem</th><th class="l">Etkinlik</th><th>Beklenti</th><th>Önceki</th><th>Açıklanan</th></tr></thead><tbody>${mRows}</tbody></table></div></div>` : '')
      + (totalEarn ? `<div><h4 class="sub">Bilançolar</h4><div class="tbl-wrap"><table class="tbl cal-earn"><thead><tr><th class="l">Sembol</th><th class="l">Şirket</th><th class="l">Zaman</th><th title="Hisse başı kâr beklentisi">EPS bkl.</th>`
        + (results ? '<th title="Açıklanan hisse başı kâr">Açıklanan</th><th title="Açıklanan EPS beklentiden yüzde kaç saptı">Sürpriz</th>' : '')
        + `<th>Piyasa değ.</th></tr></thead><tbody>${eRows}</tbody></table></div>`
        + (totalEarn > 25 ? `<div class="more-row"><button type="button" class="btn sm ghost" data-act="cal-more" data-day="${day}">${isOpen ? 'Daha az göster' : `Tümünü göster (${totalEarn})`}</button></div>` : '') + '</div>' : '')
      + (ipos.length ? `<div class="cal-ipo-w"><h4 class="sub">Halka arzlar</h4><div class="tbl-wrap"><table class="tbl cal-ipo"><thead><tr><th class="l">Sembol</th><th class="l">Şirket</th><th class="l">Borsa</th><th title="Halka arz fiyatı">Fiyat</th><th title="Satılacak hisse adedi">Hisse adedi</th><th title="Halka arz büyüklüğü">Büyüklük</th><th class="l">Durum</th></tr></thead><tbody>${iRows}</tbody></table></div></div>` : '')
      + '</div></section>';
  }).join('');
}

PAGES.takvim = {
  title: 'Takvim', poll: 300, bodyId: 'cal-body',
  mount(view) {
    this.data = null;
    this.expanded = this.expanded || new Set();
    view.innerHTML = '<div class="page"><div class="toolbar sticky">'
      + searchBox('q', CF.q, 'Etkinlik veya sembol ara…')
      + segHtml('cal-kind', [['', 'Tümü'], ['macro', 'Ekonomik'], ['earnings', 'Bilanço'], ['ipo', 'Halka arz']], CF.kind, 'Tür')
      + selectHtml('impact', [['', 'Tüm önem düzeyleri'], ['high', 'Sadece yüksek'], ['medium', 'Orta ve yüksek']], CF.impact, '', 'Önem')
      + selectHtml('days', [[3, '3 gün'], [7, '7 gün'], [14, '14 gün']], CF.days, '', 'Gün sayısı')
      + `<label class="chk"><input type="checkbox" data-f="us"${CF.us ? ' checked' : ''}> Sadece ABD</label>`
      + `<label class="chk"><input type="checkbox" data-f="wl"${CF.wl ? ' checked' : ''}> Sadece izleme listesi bilançoları</label>`
      + '<span class="sp"></span><span class="dim sm">Günler ABD tarihine göre · saatler TR</span></div>'
      + `<div id="page-err"></div><div id="cal-body">${stateHtml('loading')}</div></div>`;
  },
  onFilter(f, v, type) {
    if (f === 'q') { if (type === 'input') { CF.q = String(v); debounce('cal', () => this.render(), 200); } return; }
    if (type !== 'change') return;
    if (f === 'days') { CF.days = num(v) || 7; saveCF(); refreshPage(false); return; }
    CF[f] = typeof v === 'boolean' ? v : String(v);
    saveCF();
    this.render();
  },
  onConfig() { this.render(); },
  setKind(v) {
    CF.kind = v;
    saveCF();
    for (const b of document.querySelectorAll('[data-act="cal-kind"]')) { const on = b.dataset.v === v; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
    this.render();
  },
  async refresh(first, live) {
    // prev=1: also the previous trading day, whose earnings rows carry the reported EPS / surprise.
    const d = await api('/api/calendar', { params: { days: CF.days, prev: 1 } });
    if (!live()) return;
    this.data = arr(d.items);
    this.render();
  },
  render() {
    if (!this.data) return;
    keepScroll('.cal-day', () => setHtml('cal-body', calendarHtml(this.data, CF, wlSet(), this.expanded)));
  },
};

// ============================================================ page: Brifingler

const BF = { kind: '', author: '', sel: null, genKind: 'manual', engine: 'kural', expKind: 'manual', exportPath: '' };
function briefListHtml(items, sel) {
  if (!items.length) return stateHtml('empty', 'Henüz brifing yok. "Şimdi oluştur" ile ilkini hazırlayın.');
  return '<div class="br-list" role="listbox" aria-label="Brifingler">' + items.map(b => `<button type="button" class="br-item${b.id === sel ? ' on' : ''}" data-act="brief-sel" data-id="${esc(b.id)}" role="option" aria-selected="${b.id === sel}">`
    + `<div class="br-tags"><span class="kind k-${esc(b.kind)}">${esc(b.kind ? ml('brief_kinds', b.kind, BRIEF_KINDS) : '—')}</span><span class="auth-b a-${esc(b.author)}">${esc(b.author ? ml('brief_authors', b.author, AUTHORS) : '—')}</span><span class="sp"></span><span class="dim sm" title="${esc(tsTitle(b.created_at))}">${esc(whenShort(b.created_at))}</span></div>`
    + `<div class="br-t">${esc(b.title || '(başlıksız)')}</div></button>`).join('') + '</div>';
}
function briefHtml(b) {
  if (!b) return stateHtml('empty', 'Okumak için soldan bir brifing seçin.');
  return `<header class="br-h"><div class="br-tags"><span class="kind k-${esc(b.kind)}">${esc(b.kind ? ml('brief_kinds', b.kind, BRIEF_KINDS) : '—')}</span><span class="auth-b a-${esc(b.author)}">${esc(b.author ? ml('brief_authors', b.author, AUTHORS) : '—')}</span>`
    + `<span class="muted sm" title="${esc(tsTitle(b.created_at))}">${esc(num(b.created_at) ? `${dmy(b.created_at)} ${hm(b.created_at)} TR · ${hm(b.created_at, TZ_ET)} ET` : '')}</span><span class="sp"></span>`
    + `<button type="button" class="btn sm ghost" data-act="brief-copy" title="Markdown metnini panoya kopyala">${icon('copy')} Kopyala</button></div></header>`
    + `<div class="md">${md(b.body_md || '')}</div>`;
}

PAGES.brifingler = {
  title: 'Brifingler', poll: 60, bodyId: 'br-list',
  mount(view) {
    this.data = null; this.readSig = ''; this.gen = false;
    const ollama = !!obj(obj(S.status).ml).ollama;
    const kinds = Object.entries(mapOf('brief_kinds', BRIEF_KINDS));
    view.innerHTML = '<div class="page page-briefs"><div class="toolbar sticky">'
      + selectHtml('kind', [['', 'Tüm türler'], ...kinds], BF.kind, '', 'Brifing türü')
      + selectHtml('author', [['', 'Tüm yazarlar'], ...Object.entries(mapOf('brief_authors', AUTHORS))], BF.author, '', 'Yazar')
      + '<span class="sp"></span><div class="tb-group"><span class="dim sm">Oluştur:</span>'
      + selectHtml('genKind', kinds, BF.genKind, 'sm', 'Oluşturulacak brifing türü')
      + `<select class="sel sm" data-f="engine" aria-label="Motor" id="br-engine"><option value="kural"${BF.engine === 'kural' ? ' selected' : ''}>Kural tabanlı</option><option value="ollama"${BF.engine === 'ollama' ? ' selected' : ''}${ollama ? '' : ' disabled'}>Ollama${ollama ? '' : ' (bulunamadı)'}</option></select>`
      + `<button type="button" class="btn primary sm" data-act="brief-gen" id="br-gen">${icon('play')} Şimdi oluştur</button></div>`
      + '<div class="tb-group">'
      + selectHtml('expKind', kinds, BF.expKind, 'sm', 'Dışa aktarılacak tür')
      + `<button type="button" class="btn sm" data-act="brief-export" title="Claude Code görevi için veri paketini dışa aktar">${icon('download')} Claude için dışa aktar</button></div></div>`
      + `<div id="page-err"></div><div class="infobar" id="br-info"></div><div class="brief-wrap"><aside id="br-list">${stateHtml('loading')}</aside><article id="br-read" class="br-read panel"></article></div></div>`;
    this.info();
  },
  onStatus() {
    this.info();
    const sel = byId('br-engine');
    const ollama = !!obj(obj(S.status).ml).ollama;
    if (sel && sel.options[1]) { sel.options[1].disabled = !ollama; sel.options[1].textContent = 'Ollama' + (ollama ? '' : ' (bulunamadı)'); }
  },
  info() {
    const nb = arr(obj(S.status).next_briefs).filter(b => num(b.ts));
    const parts = nb.map(b => `<span class="pill" title="${esc(tsTitle(b.ts))}">${esc(ml('brief_kinds', b.kind, BRIEF_KINDS))} · ${esc(hm(b.ts))} TR (${esc(hm(b.ts, TZ_ET))} ET) · ${esc(rel(b.ts))}</span>`);
    setHtml('br-info', `<span class="info-l">${parts.length ? '<span class="muted">Planlı:</span>' + parts.join('') : '<span class="muted">Planlı brifing bilgisi yok</span>'}</span><span class="sp"></span>`
      + (BF.exportPath ? `<span class="exp-path" title="Claude Code görevi bu dosyayı okur${BF.exportOut ? '; analizi ' + esc(BF.exportOut) + ' klasörüne yazar' : ''}">${icon('file')} <code>${esc(BF.exportPath)}</code>`
        + `<button type="button" class="btn sm ghost" data-act="copy-text" data-text="${esc(BF.exportPath)}" title="Yolu kopyala" aria-label="Yolu kopyala">${icon('copy')}</button>`
        + `<button type="button" class="btn sm ghost" data-act="open-folder" data-which="claude_in" title="Klasörü aç" aria-label="Klasörü aç">${icon('ext')}</button></span>` : ''));
  },
  onFilter(f, v, type) {
    if (type !== 'change') return;
    BF[f] = String(v);
    if (f === 'kind' || f === 'author') { this.readSig = ''; refreshPage(false); }
  },
  async refresh(first, live) {
    const d = await api('/api/briefs', { params: { limit: 50, kind: BF.kind, author: BF.author } });
    if (!live()) return;
    this.data = arr(d.items);
    if (!this.data.some(b => b.id === BF.sel)) BF.sel = this.data.length ? this.data[0].id : null;
    this.render();
  },
  render() {
    if (!this.data) return;
    const listEl = byId('br-list');
    const st = listEl ? listEl.scrollTop : 0;
    setHtml('br-list', briefListHtml(this.data, BF.sel));
    if (listEl) listEl.scrollTop = st;
    const b = this.data.find(x => x.id === BF.sel) || null;
    const sig = b ? `${b.id}|${(b.body_md || '').length}|${b.title}` : '';
    if (sig !== this.readSig) {
      this.readSig = sig;
      setHtml('br-read', briefHtml(b));
      const r = byId('br-read');
      if (r) r.scrollTop = 0;
    }
  },
  select(id) { BF.sel = id; this.render(); },
  async generate(btn) {
    if (this.gen) return;
    this.gen = true;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin sm"></span> Oluşturuluyor…'; }
    try {
      const b = await api('/api/briefs/generate', { method: 'POST', body: { kind: BF.genKind, engine: BF.engine }, timeout: 300000 });
      if (b && b.id) BF.sel = b.id;
      toast({ title: 'Brifing oluşturuldu', body: (b && b.title) || '', kind: 'ok' });
      this.readSig = '';
      await refreshPage(false);
    } catch (e) {
      toast({ title: 'Brifing oluşturulamadı', body: errMsg(e), kind: 'err' });
    } finally {
      this.gen = false;
      const bb = byId('br-gen');
      if (bb) { bb.disabled = false; bb.innerHTML = `${icon('play')} Şimdi oluştur`; }
    }
  },
  async exportClaude(btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/briefs/claude_export', { method: 'POST', body: { kind: BF.expKind }, timeout: 60000 });
      BF.exportPath = String(r.path || '');
      BF.exportOut = String(r.out_dir || '');
      this.info();
      toast({ title: 'Claude için dışa aktarıldı', body: BF.exportPath || 'Dosya yazıldı', kind: 'ok', timeout: 9000 });
    } catch (e) {
      toast({ title: 'Dışa aktarılamadı', body: errMsg(e), kind: 'err' });
    } finally { if (btn) btn.disabled = false; }
  },
};

// ============================================================ page: Hisse

function recentTickers() { return arr(store.get('recentTickers', [])).map(normSym).filter(Boolean).slice(0, 10); }
function addRecent(sym) { store.set('recentTickers', [sym, ...recentTickers().filter(s => s !== sym)].slice(0, 10)); }
function quickChips(sym) {
  const wl = [...wlSet()];
  const rec = recentTickers().filter(s => s !== sym);
  return (rec.length ? `<span class="dim sm">Son:</span>${rec.slice(0, 8).map(s => tkChip(s)).join('')}` : '')
    + (wl.length ? `<span class="dim sm">İzleme:</span>${wl.slice(0, 14).map(s => tkChip(s)).join('')}` : '');
}
function tickerSocialHtml(so) {
  so = obj(so);
  const ape = so.apewisdom && typeof so.apewisdom === 'object' && !Array.isArray(so.apewisdom) ? so.apewisdom
    : Array.isArray(so.apewisdom) ? so.apewisdom[0] : (so.rank != null || so.mentions != null ? so : null);
  const st = so.stocktwits && typeof so.stocktwits === 'object' ? (Array.isArray(so.stocktwits) ? so.stocktwits[0] : so.stocktwits) : null;
  const rows = [];
  if (ape) {
    const md0 = num(ape.mentions) != null && num(ape.mentions_24h_ago) != null ? num(ape.mentions) - num(ape.mentions_24h_ago) : null;
    rows.push(`<div class="kv"><span>Reddit sırası</span><b>#${fmtInt(ape.rank)}</b>${num(ape.rank_24h_ago) ? `<small class="dim">24 sa önce #${fmtInt(ape.rank_24h_ago)}</small>` : ''}</div>`);
    rows.push(`<div class="kv"><span>Bahsedilme (24 sa)</span><b>${fmtInt(ape.mentions)}</b>${md0 != null ? `<small class="${dirClass(md0)}">${fmtSigned(md0, 0)}</small>` : ''}</div>`);
    if (num(ape.upvotes) != null) rows.push(`<div class="kv"><span>Beğeni</span><b>${fmtInt(ape.upvotes)}</b></div>`);
  }
  if (st) rows.push(`<div class="kv"><span>StockTwits</span><b>Trend listesinde</b>${num(st.watchlist_count) ? `<small class="dim">${fmtInt(st.watchlist_count)} takipçi</small>` : ''}</div>${st.title ? `<div class="dim sm">${esc(st.title)}</div>` : ''}`);
  return rows.length ? rows.join('') : stateHtml('empty', 'Sosyal medyada öne çıkmıyor.');
}
function tickerEventsHtml(events) {
  events = arr(events).filter(e => num(e.ts)).sort((a, b) => a.ts - b.ts);
  if (!events.length) return stateHtml('empty', 'Yaklaşan etkinlik yok.');
  return '<div class="ev-list">' + events.slice(0, 12).map(e => {
    const ex = obj(e.extra);
    const when = e.kind === 'earnings' ? ml('earnings_times', EARN_TIME[ex.time] ? ex.time : 'unknown', EARN_TIME) : e.all_day ? 'Tüm gün' : `${hm(e.ts)} TR`;
    const pill = e.kind === 'earnings' ? '<span class="pill amber">Bilanço</span>' : e.kind === 'ipo' ? '<span class="pill">Halka arz</span>' : '';
    const act = e.kind === 'earnings' ? epsActual(e) : null, sp = num(ex.surprise_pct);
    const more = (ex.eps_forecast != null && ex.eps_forecast !== '' ? ' · EPS bkl. ' + fmtEps(ex.eps_forecast) : '')
      + (act != null ? ` · açıklanan ${fmtEps(act)}${sp != null ? ` (sürpriz <b class="${dirClass(sp)}">${fmtPct(sp, 1)}</b>)` : ''}` : '')
      + (e.kind === 'ipo' && ex.price ? ' · fiyat ' + (num(ex.price) != null ? '$' + fmtPrice(ex.price) : esc(ex.price)) : '')
      + (e.kind === 'ipo' && ex.status_tr ? ' · ' + esc(ex.status_tr) : '');
    return `<div class="ev"><span class="ev-d" title="${esc(tsTitle(e.ts))}">${esc(dayLabel(e.ts, TZ_ET))}</span><span class="ev-t">${pill}${esc(ex.title_tr || e.title || '')}</span>`
      + `<span class="muted sm">${esc(when)}${more}</span></div>`;
  }).join('') + '</div>';
}
function tickerHtml(d, wl) {
  d = obj(d);
  const sym = String(d.ticker || '').toUpperCase();
  const q = obj(d.quote), news = arr(d.news), opp = d.opportunity && typeof d.opportunity === 'object' ? d.opportunity : null;
  const bySrc = Object.entries(obj(d.by_source)).map(([k, v]) => [k, arr(v)]).filter(x => x[1].length).sort((a, b) => b[1].length - a[1].length);
  const inWl = S.config ? wl.has(sym) : !!d.in_watchlist;
  const sAvg = num(d.sentiment_avg);
  const ag = agreement(news);
  const head = `<section class="panel tk-head"><div class="tkh-l"><h2>${esc(sym)}</h2><div class="company">${esc(d.company || q.name || '')}</div></div>`
    + `<div class="tkh-q">${num(q.price) != null ? `<b class="px">${fmtPrice(q.price)}</b><span class="chg ${dirClass(q.change_pct)}">${fmtSigned(q.change)} (${fmtPct(q.change_pct)})</span>${extHtml(q)}${spark(q.spark, { base: q.prev_close, w: 160, h: 38 })}` : '<span class="muted">Fiyat verisi yok — izleme listesine eklerseniz fiyatı da takip edilir.</span>'}</div>`
    + `<div class="tkh-s"><div><span class="muted">Duygu ort.</span> <b class="${dirClass(sAvg)}">${fmtSigned(sAvg)}</b></div><div><span class="muted">Haber</span> <b>${fmtInt(news.length)}</b> · <span class="muted" title="Bağımsız yayıncı sayısı">Kaynak</span> <b>${fmtInt(num(d.source_count) ?? bySrc.length)}</b></div>`
    + `${news.length > 1 ? agreeMeter(ag, null) : ''}</div>`
    + `<div class="tkh-a"><button type="button" class="btn${inWl ? ' on' : ''}" data-act="wl-toggle" data-sym="${esc(sym)}" aria-pressed="${inWl}">${icon('star')} ${inWl ? 'İzleme listesinde' : 'İzleme listesine ekle'}</button>`
    + `<button type="button" class="btn ghost sm" data-act="tk-news" data-sym="${esc(sym)}">Haber akışında aç</button></div></section>`;
  const oppHtml = opp ? oppCard(opp, false, false, '', true) : stateHtml('empty', 'Bu hisse şu an fırsat listesinde değil.');
  const cols = bySrc.length
    ? `<div class="src-cols">${bySrc.map(([name, items]) => {
      const s0 = items[0] || {};
      return `<section class="scol" data-src="${esc(name)}"><header>${srcBadge(s0.source || name, name, s0.source_type)}<span class="sp"></span><span class="col-n">${items.length}</span></header>`
        + `<div class="scol-body">${items.slice(0, 10).map(it => colItem(it, false)).join('')}${items.length > 10 ? `<div class="dim sm pad">+${items.length - 10} haber daha (aşağıdaki akışta)</div>` : ''}</div></section>`;
    }).join('')}</div>`
    : stateHtml('empty', 'Bu hisse için son günlerde haber yok.');
  return head
    + `<div class="tk-grid"><section class="panel"><h3 class="ph">Fırsat skoru</h3>${oppHtml}</section>`
    + `<section class="panel"><h3 class="ph">Takvim</h3>${tickerEventsHtml(d.events)}</section>`
    + `<section class="panel"><h3 class="ph">Sosyal medya</h3>${tickerSocialHtml(d.social)}</section></div>`
    + `<section class="panel"><h3 class="ph">Kim ne diyor — kaynaklara göre haberler</h3>${cols}</section>`
    + (news.length ? `<section class="panel plain"><h3 class="ph">Zaman akışı (${fmtInt(news.length)})</h3>${newsListHtml(news.slice(0, 300), {})}</section>` : '');
}
function tickerEmpty() {
  const wl = [...wlSet()], rec = recentTickers();
  return `<div class="state empty big"><div>İncelemek istediğiniz hissenin sembolünü yazın (ör. <b>NVDA</b>).</div>`
    + `${wl.length ? `<div class="state-x"><span class="dim">İzleme listesi:</span> ${wl.map(s => tkChip(s)).join('')}</div>` : ''}`
    + `${rec.length ? `<div class="state-x"><span class="dim">Son bakılanlar:</span> ${rec.map(s => tkChip(s)).join('')}</div>` : ''}</div>`;
}
function updateHisseLink(sym) {
  const a = HAS_DOM && document.querySelector('#nav .nav-item[data-key="hisse"]');
  const s = sym || recentTickers()[0];
  if (a) a.setAttribute('href', s ? '#hisse/' + s : '#hisse');
}

PAGES.hisse = {
  title: 'Hisse', poll: 60, bodyId: 'tk-body',
  mount(view, param) {
    this.data = null; this.html = '';
    this.sym = normSym(param);
    // A word typed into the global search ("Fed") that turns out to have no data falls back to a news search.
    const gs = S.gsearch;
    S.gsearch = null;
    this.fallback = gs && gs.sym === this.sym ? gs.text : '';
    // The symbol joins "recent" (nav link, key 7, quick chips) only once the server knows it (see refresh).
    updateHisseLink();
    view.innerHTML = '<div class="page"><div class="toolbar sticky">'
      + `<form id="tk-form" class="search tk-search" role="search">${icon('search')}<input type="search" class="inp" name="sym" data-primary-search placeholder="Sembol (ör. NVDA)" value="${esc(this.sym)}" autocomplete="off" spellcheck="false" maxlength="12" aria-label="Hisse sembolü"></form>`
      + '<button type="submit" form="tk-form" class="btn primary sm">Göster</button>'
      + `<div class="quick" id="tk-quick">${quickChips(this.sym)}</div></div>`
      + `<div id="page-err"></div><div id="tk-body">${this.sym ? stateHtml('loading', this.sym + ' yükleniyor…') : tickerEmpty()}</div></div>`;
  },
  onConfig() { setHtml('tk-quick', quickChips(this.sym)); if (this.data) this.render(); else if (!this.sym) setHtml('tk-body', tickerEmpty()); },
  onError(e) {
    setHtml('tk-body', e && e.status === 404
      ? stateHtml('empty', `${this.sym} için veri bulunamadı. Sembolü kontrol edin; yeni eklenen hisseler için veriler birkaç dakika içinde toplanır.`)
      : stateHtml('error', errMsg(e)));
  },
  onMeta() { if (this.data) this.render(); },
  async refresh(first, live) {
    if (!this.sym) return;
    const d = await api('/api/ticker/' + encodeURIComponent(this.sym));
    if (!live()) return;
    const known = !!(d && (d.company || arr(d.news).length || d.quote));
    if (known) {
      if (recentTickers()[0] !== this.sym) addRecent(this.sym);
      updateHisseLink(this.sym);
    } else if (this.fallback) {
      const text = this.fallback;
      this.fallback = '';
      toast({ title: `${this.sym} için hisse verisi yok — haberlerde arandı`, body: `“${text}”`, kind: 'info' });
      nfReset();
      NF.q = text;
      try { location.replace('#haberler'); } catch (e) { go('haberler'); }
      return;
    }
    this.fallback = '';
    this.data = d;
    this.render();
  },
  render() {
    if (!this.data) return;
    if (hasSelectionIn('tk-body')) return;
    const html = tickerHtml(this.data, wlSet());
    if (html === this.html && byId('tk-body') && byId('tk-body').firstElementChild) return;
    this.html = html;
    // Per-source columns scroll on their own; keep each one where the user left it.
    const tops = {};
    for (const c of document.querySelectorAll('#tk-body .scol[data-src]')) { const b = c.querySelector('.scol-body'); if (b && b.scrollTop) tops[c.dataset.src] = b.scrollTop; }
    keepScroll('.nrow', () => setHtml('tk-body', html));
    for (const c of document.querySelectorAll('#tk-body .scol[data-src]')) { const b = c.querySelector('.scol-body'); if (b && tops[c.dataset.src]) b.scrollTop = tops[c.dataset.src]; }
  },
};

async function toggleWatch(sym, btn) {
  sym = normSym(sym);
  if (!sym) return;
  if (!S.config) { try { await loadConfig(); } catch (e) { toast({ title: 'Ayarlar okunamadı', body: errMsg(e), kind: 'err' }); return; } }
  const wl = arr(S.config.watchlist).map(s => String(s).toUpperCase());
  const has = wl.includes(sym);
  const next = has ? wl.filter(s => s !== sym) : [...wl, sym];
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/config', { method: 'POST', body: { watchlist: next } });
    applyConfigResponse(r, { watchlist: next });
    toast({ title: has ? `${sym} izleme listesinden çıkarıldı` : `${sym} izleme listesine eklendi`, kind: 'ok' });
    if (cur && cur.page.onConfig) cur.page.onConfig();
  } catch (e) {
    toast({ title: 'İzleme listesi güncellenemedi', body: errMsg(e), kind: 'err' });
  } finally { if (btn) btn.disabled = false; }
}
function applyConfigResponse(r, sent) {
  const c = r && typeof r === 'object' ? (r.config && typeof r.config === 'object' ? r.config : r) : null;
  if (c && ('watchlist' in c || 'port' in c)) S.config = c;
  else S.config = Object.assign({}, S.config || {}, sent || {});
}

// ============================================================ page: Kaynaklar & Ayarlar

const SRC_STATE_MAP = { ok: 'ok', partial: 'part', error: 'err', idle: 'idle', disabled: 'off', needs_config: 'need', running: 'run' };
const SRC_STATE_TR = { ok: 'OK', part: 'Kısmi hata', err: 'Hata', idle: 'Bekliyor', off: 'Kapalı', need: 'Ayar gerekli', run: 'Çalışıyor' };
/** [cssKey, label]; prefers the server's `state` / `state_label`, else derives from status fields. */
function srcState(s) {
  const k = SRC_STATE_MAP[s.state];
  if (k) return [k, String(s.state_label || ml('source_states', s.state, {}) || SRC_STATE_TR[k])];
  if (s.enabled === false) return ['off', SRC_STATE_TR.off];
  if (arr(s.requires_missing).length) return ['need', SRC_STATE_TR.need];
  if (s.running) return ['run', SRC_STATE_TR.run];
  if (s.last_error && (!s.last_ok || (num(s.last_run) || 0) > (num(s.last_ok) || 0))) return ['err', SRC_STATE_TR.err];
  if (s.last_ok) return s.warning ? ['part', SRC_STATE_TR.part] : ['ok', SRC_STATE_TR.ok];
  return ['idle', SRC_STATE_TR.idle];
}
function sourcesTableHtml(list, f) {
  if (!list.length) return stateHtml('empty', 'Kaynak listesi boş — sunucu kaynakları henüz yüklemedi.');
  const q = fold(String(f.q || '').trim());
  const rows = list.filter(s => (!q || fold(`${s.source} ${s.source_name} ${s.description || ''} ${s.group}`).includes(q)) && (!f.st || srcState(s)[0] === f.st));
  if (!rows.length) return stateHtml('empty', 'Filtreyle eşleşen kaynak yok.');
  const groups = groupBy(rows, s => s.group || 'other');
  return '<div class="tbl-wrap"><table class="tbl src-tbl"><thead><tr><th class="l">Kaynak</th><th class="l">Tür</th><th class="l">Durum</th><th>Son çalışma</th><th title="Son çalışmada gelen yeni öğe">Son</th><th title="Toplam öğe">Toplam</th><th title="Son 24 saatte haber">24 sa</th><th>Aralık</th><th class="l">Hata / not</th><th class="c">Aktif</th><th></th></tr></thead><tbody>'
    + groupOrder(Object.keys(groups)).map(g => `<tr class="grp"><td colspan="11">${esc(groupLabel(g))} <span class="dim">(${groups[g].length})</span></td></tr>`
      + groups[g].map(s => {
        const [st, stl] = srcState(s);
        const miss = arr(s.requires_missing).map(k => REQ_LABELS[k] || k);
        const note = s.hint ? String(s.hint)
          : miss.length ? `Gerekli: ${miss.join(', ')} — aşağıdan girin.`
            : s.last_error && (st === 'err' || st === 'run') ? String(s.last_error)
              : s.warning && (st === 'part' || st === 'run') ? String(s.warning) : '';
        const next = num(s.next_run);
        const runTip = [s.last_run ? 'Son çalışma: ' + tsTitle(s.last_run) : '', s.last_ok ? 'Son başarılı: ' + tsTitle(s.last_ok) : '',
          next ? 'Sonraki: ' + tsTitle(next) : '', num(s.duration_last) != null ? `Süre: ${nf(1, 1).format(num(s.duration_last))} sn` : '',
          num(s.fails) ? `Ardışık hata: ${s.fails}` : ''].filter(Boolean).join('\n');
        return `<tr class="st-${st}"><td class="l"><div class="src-n">${esc(s.source_name || s.source)}</div>${s.description ? `<div class="dim sm src-d" title="${esc(s.description)}">${esc(s.description)}</div>` : ''}</td>`
          + `<td class="l dim">${esc(stypeLabel(s.source_type) || s.kind || '')}</td>`
          + `<td class="l"><span class="stp ${st}">${esc(stl)}</span></td>`
          + `<td title="${esc(runTip)}">${s.last_run ? esc(rel(s.last_run)) : '—'}${next && st !== 'off' && st !== 'need' ? `<div class="dim sm">sonraki ${esc(rel(next))}</div>` : ''}</td>`
          + `<td>${fmtInt(s.items_last)}</td><td>${fmtInt(s.items_total)}</td><td>${fmtInt(s.news_24h)}</td><td class="dim">${fmtInterval(s.interval)}</td>`
          + `<td class="l err-t${st === 'err' ? ' down' : st === 'part' ? ' amber' : ''}" title="${esc(note)}">${esc(trunc(note, 110))}</td>`
          + `<td class="c"><label class="sw" title="${s.enabled === false ? 'Etkinleştir' : 'Devre dışı bırak'}"><input type="checkbox" data-act="src-toggle" data-src="${esc(s.source)}"${s.enabled === false ? '' : ' checked'} aria-label="${esc(s.source_name || s.source)} etkin"><span></span></label></td>`
          + `<td><button type="button" class="btn sm" data-act="src-refresh" data-src="${esc(s.source)}"${s.enabled === false || miss.length || st === 'run' ? ' disabled' : ''}>${icon('refresh')} Şimdi çek</button></td></tr>`;
      }).join('')).join('') + '</tbody></table></div>';
}
function srcSummary(list) {
  const c = { ok: 0, part: 0, err: 0, idle: 0, need: 0, off: 0, run: 0 };
  for (const s of list) c[srcState(s)[0]]++;
  return `${list.length} kaynak · <span class="up">${c.ok} OK</span>${c.part ? ` · <span class="amber">${c.part} kısmi hata</span>` : ''} · <span class="down">${c.err} hata</span>${c.run ? ` · ${c.run} çalışıyor` : ''} · ${c.idle} bekliyor · <span class="amber">${c.need} ayar gerekli</span> · ${c.off} kapalı`;
}
function fld(id, label, control, hint) {
  return `<div class="fld"><label for="${esc(id)}">${esc(label)}</label><div class="fld-c">${control}</div>${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
}
function secretField(k, label, c) {
  const set = !!c[k + '_set'];
  return fld('f-' + k, label, `<input class="inp" id="f-${esc(k)}" type="password" name="${esc(k)}" autocomplete="new-password" spellcheck="false" placeholder="${set ? 'kayıtlı ✓ — değiştirmek için yazın' : 'girilmedi'}">`
    + (set ? `<span class="pill ok">kayıtlı ✓</span><button type="button" class="btn sm ghost danger" data-act="secret-clear" data-key="${esc(k)}">Sil</button>` : '<span class="pill">yok</span>'));
}
function mlPill(v, yes, no, id) {
  const on = v && typeof v === 'object' ? !!(v.available || v.ok) : !!v;
  return `<span class="pill${on ? ' ok' : ''}"${id ? ` id="${id}" data-yes="${esc(yes || '')}" data-no="${esc(no || '')}"` : ''}>${on ? (yes || 'Çalışıyor ✓') : (no || 'Bulunamadı')}</span>`;
}
function settingsFormHtml(c, st) {
  c = obj(c); st = obj(st);
  const ml = obj(st.ml), bt = obj(c.brief_times_et);
  const paths = obj(st.paths);
  const dataDir = paths.data || st.data_dir || c.data_dir || '%LOCALAPPDATA%\\PiyasaMasasi (varsayılan)';
  const cIn = paths.claude_in || c.claude_in_dir || '', cOut = paths.claude_out || c.claude_out_dir || '';
  const ami = num(c.alert_min_importance) ?? 70;
  const extras = Object.keys(c).filter(k => !KNOWN_CFG.has(k) && !/_set$/.test(k) && ['string', 'number', 'boolean'].includes(typeof c[k]));
  const extraSecrets = Object.keys(c).filter(k => /_set$/.test(k) && !SECRET_KEYS.includes(k.slice(0, -4)) && typeof c[k] === 'boolean').map(k => k.slice(0, -4));
  const extraHtml = extras.length || extraSecrets.length
    ? `<section class="panel"><h3 class="ph">Diğer ayarlar</h3>${extraSecrets.map(k => secretField(k, k, c)).join('')}${extras.map(k => (typeof c[k] === 'boolean'
      ? fld('x-' + k, k, `<label class="sw"><input type="checkbox" id="x-${esc(k)}" data-extra="${esc(k)}"${c[k] ? ' checked' : ''}><span></span></label>`)
      : fld('x-' + k, k, `<input class="inp" id="x-${esc(k)}" data-extra="${esc(k)}" type="${typeof c[k] === 'number' ? 'number' : 'text'}" value="${esc(c[k])}">`))).join('')}`
      + '<div class="hint">Modüllerin eklediği ek ayarlar.</div></section>' : '';
  return '<form id="cfg-form" class="set-grid" autocomplete="off">'
    + '<section class="panel"><h3 class="ph">API anahtarları</h3>'
    + secretField('alpaca_key_id', 'Alpaca API Key ID', c) + secretField('alpaca_secret', 'Alpaca Secret Key', c) + secretField('finnhub_key', 'Finnhub API anahtarı', c)
    + '<div class="hint">Anahtarlar yalnızca bu bilgisayarda (veri klasöründe) saklanır ve arayüze geri gönderilmez. Alpaca için ücretsiz “paper trading” anahtarı, Finnhub için ücretsiz anahtar yeterlidir. Boş bırakılan alanlar değişmez.</div></section>'
    + '<section class="panel"><h3 class="ph">SEC EDGAR</h3>'
    + fld('f-sec', 'Kimlik (User-Agent)', `<input class="inp wide" id="f-sec" name="sec_user_agent" value="${esc(c.sec_user_agent || '')}" placeholder="Ad Soyad eposta@ornek.com" spellcheck="false">`,
      'SEC, her istekte ad ve e-posta ister. Boş kalırsa SEC kaynakları (8-K, Form 4…) beklemede kalır.')
    + '</section>'
    + '<section class="panel"><h3 class="ph">İzleme listesi</h3><div id="wl-editor"></div>'
    + '<div class="hint">Bu hisseler için ayrı haber akışları, uyarılar ve fırsat skorunda ek ağırlık kullanılır.</div></section>'
    + '<section class="panel"><h3 class="ph">Brifing saatleri <span class="upd">ABD Doğu saati (ET)</span></h3>'
    + ['sabah', 'ogle', 'aksam'].map(k => fld('bt-' + k, BRIEF_KINDS[k], `<input class="inp" type="time" id="bt-${k}" name="bt_${k}" value="${esc(bt[k] || '')}"><span class="dim sm" data-trof="bt_${k}">${esc(etToTr(bt[k]))}</span>`)).join('')
    + '<div class="hint">İşlem olmayan günlerde otomatik brifing atlanır. TR karşılığı yaz/kış saatine göre otomatik kayar.</div></section>'
    + '<section class="panel"><h3 class="ph">Uyarılar</h3>'
    + fld('f-ami', 'Uyarı eşiği (önem ≥)', `<span class="range"><input type="range" id="f-ami" name="alert_min_importance" min="0" max="100" step="5" value="${ami}"><output>${ami}</output></span>`)
    + fld('p-notify', 'Masaüstü bildirimi', `<label class="sw"><input type="checkbox" id="p-notify" data-pref="notify"${S.prefs.notify ? ' checked' : ''}><span></span></label><button type="button" class="btn sm" data-act="notif-perm">${icon('bell')} İzin iste</button><span class="dim sm" id="notif-state">${esc(notifState())}</span>`)
    + fld('p-beep', 'Sesli uyarı (kısa bip)', `<label class="sw"><input type="checkbox" id="p-beep" data-pref="beep"${S.prefs.beep ? ' checked' : ''}><span></span></label><button type="button" class="btn sm" data-act="beep-test">${icon('play')} Dinle</button><button type="button" class="btn sm ghost" data-act="alert-test">Deneme uyarısı</button>`)
    + '<div class="hint">Eşiği geçen ya da izleme listenizdeki hisseleri içeren yeni haberler sağ altta bildirim olarak görünür; tıklayınca haber açılır. Bildirim ve ses tercihi bu bilgisayarda saklanır.</div></section>'
    + '<section class="panel"><h3 class="ph">Yerel yapay zekâ (isteğe bağlı)</h3>'
    + fld('f-ou', 'Ollama adresi', `<input class="inp wide" id="f-ou" name="ollama_url" value="${esc(c.ollama_url || '')}" placeholder="http://127.0.0.1:11434" spellcheck="false">${mlPill(ml.ollama, '', '', 'ml-ollama')}`)
    + fld('f-om', 'Ollama modeli', `<input class="inp wide" id="f-om" name="ollama_model" value="${esc(c.ollama_model || '')}" placeholder="boş = yüklü ilk model (ör. qwen2.5:7b)" spellcheck="false">`)
    + fld('f-fb', 'FinBERT duygu modeli', `<label class="sw"><input type="checkbox" id="f-fb" name="use_finbert"${c.use_finbert ? ' checked' : ''}><span></span></label>${mlPill(ml.finbert, 'Kurulu ✓', 'Kurulu değil', 'ml-finbert')}`,
      'Kurulum: <code>py -m pip install transformers torch</code>. Kurulu değilse kural tabanlı analiz kullanılır.')
    + '</section>'
    + extraHtml
    + '<section class="panel"><h3 class="ph">Görünüm ve sistem</h3>'
    + fld('t-theme', 'Tema', `<button type="button" class="btn sm" id="t-theme" data-act="theme">Açık / koyu tema</button><button type="button" class="btn sm ghost" data-act="sidebar">Kenar çubuğunu daralt / genişlet</button>`)
    + fld('t-dir', 'Veri klasörü', pathCtl(dataDir, 'data', 't-dir'))
    + (cIn ? fld('t-cin', 'Claude girdi klasörü', pathCtl(cIn, 'claude_in', 't-cin')) : '')
    + (cOut ? fld('t-cout', 'Claude çıktı klasörü', pathCtl(cOut, 'claude_out', 't-cout'), 'Zamanlanmış Claude Code görevi analizini buraya yazar; uygulama yeni dosyaları otomatik olarak “Claude” brifingi olarak alır.') : '')
    + fld('t-ver', 'Sürüm', `<span id="t-ver">${esc(st.version || '—')}</span><span class="dim sm">port ${esc(c.port != null ? c.port : '—')}</span>`)
    + (obj(st.instance).can_shutdown ? fld('t-quit', 'Uygulama', `<button type="button" class="btn sm ghost danger" id="t-quit" data-act="app-shutdown">${icon('power')} Uygulamayı tamamen kapat</button>`,
      `Veri toplama dahil her şeyi durdurur${obj(st.instance).headless ? ' (şu an arka planda, pencere olmadan çalışıyor)' : ''}. Yeniden başlatmak için uygulamayı tekrar açın.`) : '')
    + fld('t-keys', 'Klavye kısayolları', '<span class="dim sm" id="t-keys">1–8 sekmeler · / arama · r yenile · Shift+R tüm kaynakları çek · Esc kapat</span>')
    + '</section></form>'
    + `<div class="savebar"><span id="cfg-dirty" class="muted sm">Değişiklik yok</span><span class="sp"></span><button type="button" class="btn primary" data-act="cfg-save" id="cfg-save">${icon('check')} Kaydet</button></div>`;
}
function pathCtl(path, which, id) {
  return `<code class="path" id="${esc(id)}">${esc(path)}</code><button type="button" class="btn sm ghost" data-act="copy-text" data-text="${esc(path)}" title="Yolu kopyala" aria-label="Yolu kopyala">${icon('copy')}</button>`
    + `<button type="button" class="btn sm ghost" data-act="open-folder" data-which="${esc(which)}" title="Klasörü Windows Gezgini'nde aç">${icon('ext')} Aç</button>`;
}
function wlEditorHtml(wl) {
  return `<div class="wl-chips">${wl.length ? wl.map(s => `<span class="wl-chip"><span class="tk static">${esc(s)}</span><button type="button" class="wl-x" data-act="wl-remove" data-sym="${esc(s)}" aria-label="${esc(s)} listeden çıkar" title="Çıkar">×</button></span>`).join('') : '<span class="muted">Liste boş</span>'}</div>`
    + '<div class="wl-add"><input class="inp" id="wl-input" placeholder="Sembol ekle (ör. NVDA, AMD)" autocomplete="off" spellcheck="false" aria-label="Eklenecek semboller"><button type="button" class="btn sm" data-act="wl-add">Ekle</button></div>';
}

PAGES.ayarlar = {
  title: 'Kaynaklar & Ayarlar', poll: 15, bodyId: 'src-table',
  mount(view) {
    this.data = null; this.cfgShown = false; this.wl = [];
    this.f = this.f || { q: '', st: '' };
    view.innerHTML = '<div class="page page-set"><div id="page-err"></div>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Kaynaklar</h3><span class="dim sm" id="src-sum"></span><span class="sp"></span>'
      + `<div class="search sm-s">${icon('search')}<input type="search" class="inp sm" data-f="src_q" placeholder="Kaynak ara…" value="${esc(this.f.q)}" autocomplete="off"></div>`
      + selectHtml('src_st', [['', 'Tüm durumlar'], ['ok', 'OK'], ['part', 'Kısmi hata'], ['err', 'Hata'], ['run', 'Çalışıyor'], ['idle', 'Bekliyor'], ['need', 'Ayar gerekli'], ['off', 'Kapalı']], this.f.st, 'sm', 'Durum filtresi')
      + `<button type="button" class="btn sm primary" data-act="src-refresh-all">${icon('refresh')} Tümünü şimdi çek</button></div>`
      + `<div id="src-table">${stateHtml('loading')}</div></section>`
      + `<div id="set-forms">${stateHtml('loading', 'Ayarlar yükleniyor…')}</div></div>`;
  },
  onFilter(f, v, type) {
    if (f === 'src_q') { if (type === 'input') { this.f.q = String(v); debounce('src', () => this.renderSources(), 150); } return; }
    if (f === 'src_st' && type === 'change') { this.f.st = String(v); this.renderSources(); }
  },
  onStatus() {
    const st = obj(S.status), ml = obj(st.ml);
    if (!this.cfgShown) return;
    setText('notif-state', notifState());
    setText('t-ver', String(st.version || '—'));
    for (const k of ['ollama', 'finbert']) {
      const el = byId('ml-' + k);
      if (el) el.outerHTML = mlPill(ml[k], el.dataset.yes, el.dataset.no, 'ml-' + k);
    }
  },
  onSources() { this.renderSources(); },
  onConfig() { if (!this.cfgShown || !this.dirtyKeys().length) this.renderForms(); },
  async refresh(first, live) {
    const jobs = [api('/api/sources')];
    if (first || !this.cfgShown) jobs.push(api('/api/config'));
    const [src, cfg] = await Promise.allSettled(jobs);
    if (!live()) return;
    if (cfg) {
      if (cfg.status === 'fulfilled') { S.config = cfg.value; this.renderForms(); }
      else if (!this.cfgShown) setHtml('set-forms', stateHtml('error', 'Ayarlar okunamadı: ' + errMsg(cfg.reason)));
    }
    if (src.status === 'rejected') throw src.reason;
    S.sources = arr(src.value.items);
    S.sourcesAt = Date.now();
    this.data = S.sources;
    this.renderSources();
  },
  renderSources() {
    if (!this.data && !S.sources.length) return;
    const list = S.sources;
    setHtml('src-sum', srcSummary(list));
    // Keep a text selection in the table (copying an error) alive; the next poll renders it.
    if (hasSelectionIn('src-table')) return;
    // Re-rendering replaces the toggles/buttons; put keyboard focus back where it was.
    const a = HAS_DOM ? document.activeElement : null;
    const focus = a && a.closest && a.closest('#src-table') && a.dataset.act ? [a.dataset.act, a.dataset.src] : null;
    keepScroll(null, () => setHtml('src-table', sourcesTableHtml(list, this.f)));
    if (focus) {
      const el = document.querySelector(`#src-table [data-act="${focus[0]}"][data-src="${window.CSS && CSS.escape ? CSS.escape(focus[1] || '') : focus[1]}"]`);
      if (el && !el.disabled) el.focus();
    }
  },
  renderForms() {
    // The config poll re-renders this form: keep a half-typed watchlist symbol and its focus.
    const wi = byId('wl-input'), wv = wi ? wi.value : '', wf = !!wi && document.activeElement === wi;
    setHtml('set-forms', settingsFormHtml(S.config, S.status));
    this.wl = arr(obj(S.config).watchlist).map(s => String(s).toUpperCase());
    this.renderWl();
    const ni = byId('wl-input');
    if (ni && wv) ni.value = wv;
    if (ni && wf) ni.focus();
    this.cfgShown = true;
    this.markDirty();
    if (S.focusSec) this.focusSec();
  },
  /** Puts the cursor in the SEC identity field (requested by goSecSetting). */
  focusSec() {
    S.focusSec = false;
    const el = byId('f-sec');
    if (el) el.focus();
  },
  renderWl() { setHtml('wl-editor', wlEditorHtml(this.wl)); },
  addWl() {
    const inp = byId('wl-input');
    if (!inp) return;
    const add = inp.value.split(/[\s,;]+/).map(normSym).filter(Boolean);
    const bad = inp.value.split(/[\s,;]+/).filter(x => x && !normSym(x));
    for (const s of add) if (!this.wl.includes(s)) this.wl.push(s);
    inp.value = '';
    this.renderWl();
    this.markDirty();
    const ni = byId('wl-input');
    if (ni) ni.focus();
    if (bad.length) toast({ title: 'Geçersiz sembol', body: bad.join(', '), kind: 'err' });
  },
  removeWl(sym) { this.wl = this.wl.filter(s => s !== sym); this.renderWl(); this.markDirty(); },
  collect() {
    const f = byId('cfg-form');
    if (!f) return {};
    const c = obj(S.config), out = {};
    const el = n => f.querySelector(`[name="${n}"]`);
    for (const inp of f.querySelectorAll('input[type="password"][name]')) { const v = inp.value.trim(); if (v) out[inp.name] = v; }
    for (const k of ['sec_user_agent', 'ollama_url', 'ollama_model']) {
      const e = el(k);
      if (e && e.value.trim() !== String(c[k] == null ? '' : c[k]).trim()) out[k] = e.value.trim();
    }
    const ami = el('alert_min_importance');
    if (ami && num(ami.value) !== num(c.alert_min_importance)) out.alert_min_importance = num(ami.value);
    const fb = el('use_finbert');
    if (fb && fb.checked !== !!c.use_finbert) out.use_finbert = fb.checked;
    const bt = Object.assign({}, obj(c.brief_times_et));
    let btCh = false;
    for (const k of ['sabah', 'ogle', 'aksam']) { const e = el('bt_' + k); if (e && e.value && e.value !== bt[k]) { bt[k] = e.value; btCh = true; } }
    if (btCh) out.brief_times_et = bt;
    if (this.wl.join(',') !== arr(c.watchlist).map(s => String(s).toUpperCase()).join(',')) out.watchlist = this.wl.slice();
    for (const e of f.querySelectorAll('[data-extra]')) {
      const k = e.dataset.extra, orig = c[k];
      let v;
      if (e.type === 'checkbox') v = e.checked;
      else if (typeof orig === 'number') { v = num(e.value); if (v == null) continue; } else v = e.value;
      if (v !== orig) out[k] = v;
    }
    return out;
  },
  dirtyKeys() { return Object.keys(this.collect()); },
  markDirty() {
    const k = this.dirtyKeys();
    setText('cfg-dirty', k.length ? `Kaydedilmemiş değişiklik: ${k.map(x => CFG_LABELS[x] || x).join(', ')}` : 'Değişiklik yok');
    const b = byId('cfg-save');
    if (b) b.classList.toggle('pulse', k.length > 0);
  },
  async save() {
    const body = this.collect();
    const keys = Object.keys(body);
    if (!keys.length) { toast({ title: 'Kaydedilecek değişiklik yok', kind: 'info' }); return; }
    const b = byId('cfg-save');
    if (b) b.disabled = true;
    try {
      const r = await api('/api/config', { method: 'POST', body });
      applyConfigResponse(r, null);
      try { S.config = await api('/api/config'); } catch (e) { /* keep merged copy */ }
      toast({ title: 'Ayarlar kaydedildi', body: keys.map(x => CFG_LABELS[x] || x).join(', '), kind: 'ok' });
      this.renderForms();
      runNow('sources');
    } catch (e) {
      toast({ title: 'Ayarlar kaydedilemedi', body: errMsg(e), kind: 'err' });
    } finally { const bb = byId('cfg-save'); if (bb) bb.disabled = false; }
  },
  async clearSecret(k, btn) {
    if (btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.textContent = 'Emin misiniz?';
      setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ''; btn.textContent = 'Sil'; } }, 3500);
      return;
    }
    btn.disabled = true;
    try {
      const r = await api('/api/config', { method: 'POST', body: { [k]: '__clear__' } });
      applyConfigResponse(r, { [k + '_set']: false });
      try { S.config = await api('/api/config'); } catch (e) { /* keep merged copy */ }
      toast({ title: `${CFG_LABELS[k] || k} silindi`, kind: 'ok' });
      this.renderForms();
    } catch (e) {
      btn.disabled = false;
      toast({ title: 'Silinemedi', body: errMsg(e), kind: 'err' });
    }
  },
};

async function toggleSource(input) {
  const src = input.dataset.src, enabled = input.checked;
  input.disabled = true;
  try {
    await api('/api/sources/toggle', { method: 'POST', body: { source: src, enabled } });
    const s = S.sources.find(x => x.source === src);
    if (s) { s.enabled = enabled; delete s.state; delete s.state_label; }
    setTimeout(() => runNow('sources'), 800);
    toast({ title: `${(s && s.source_name) || src} ${enabled ? 'etkinleştirildi' : 'devre dışı bırakıldı'}`, kind: 'ok', timeout: 3000 });
  } catch (e) {
    input.checked = !enabled;
    toast({ title: 'Kaynak durumu değiştirilemedi', body: errMsg(e), kind: 'err' });
  } finally {
    input.disabled = false;
    if (cur && cur.key === 'ayarlar') PAGES.ayarlar.renderSources();
  }
}
async function refreshSources(src, btn) {
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  try {
    const r = await api('/api/refresh', { method: 'POST', body: src ? { source: src } : {} });
    const q = arr(r.queued);
    const name = src ? ((S.sources.find(x => x.source === src) || {}).source_name || src) : '';
    toast({ title: src ? `${name} kuyruğa alındı` : `${q.length || 'Tüm'} kaynak kuyruğa alındı`, body: 'Çekim arka planda yapılıyor; sonuçlar birkaç saniye içinde görünür.', kind: 'ok', timeout: 4000 });
    setTimeout(() => { if (cur) refreshPage(false); runNow('status'); }, 6000);
  } catch (e) {
    toast({ title: 'Yenileme başlatılamadı', body: errMsg(e), kind: 'err' });
  } finally {
    if (btn) setTimeout(() => { btn.disabled = false; btn.classList.remove('busy'); }, src ? 4000 : 20000);
  }
}
async function openFolder(which) {
  try {
    const r = await api('/api/open_folder', { method: 'POST', body: { which } });
    toast({ title: 'Klasör açıldı', body: String(r.path || ''), kind: 'ok', timeout: 3000 });
  } catch (e) {
    toast({ title: 'Klasör açılamadı', body: errMsg(e), kind: 'err' });
  }
}
async function copyText(t) {
  try {
    await navigator.clipboard.writeText(t);
    toast({ title: 'Panoya kopyalandı', kind: 'ok', timeout: 2000 });
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast({ title: 'Panoya kopyalandı', kind: 'ok', timeout: 2000 }); } catch (e2) { toast({ title: 'Kopyalanamadı', kind: 'err' }); }
    ta.remove();
  }
}

// ============================================================ page: Sanal İşlem (paper trading)

const PT = { side: 'buy', type: 'market', qmode: 'qty', quote: null, quoteSym: '' };
const PT_SET_FIELDS = [
  ['max_position_pct', 'Tek hisse en fazla (% özsermaye)', 1], ['max_positions', 'En fazla açık pozisyon', 0],
  ['max_leverage', 'Kaldıraç (brüt / özsermaye)', 1], ['daily_loss_limit_pct', 'Günlük zarar limiti (%)', 1],
  ['slippage_bps', 'Kayma (baz puan, 5 = %0,05)', 1], ['commission_per_share', 'Komisyon ($ / hisse)', 3],
  ['bot_min_score', 'Bot: en düşük fırsat skoru', 0], ['bot_min_change_pct', 'Bot: en az günlük hareket (%)', 1],
  ['bot_max_change_pct', 'Bot: en fazla günlük hareket (%)', 1], ['bot_min_rel_volume', 'Bot: en az göreli hacim (x)', 1],
  ['bot_min_price', 'Bot: en düşük fiyat ($)', 2], ['bot_risk_per_trade_pct', 'Bot: işlem başına risk (% özsermaye)', 2],
  ['bot_stop_loss_pct', 'Bot: zarar durdur (%)', 1], ['bot_take_profit_pct', 'Bot: kâr al (%)', 1],
  ['bot_max_trades_per_day', 'Bot: günde en fazla işlem', 0], ['bot_min_dollar_volume', 'Bot: en az günlük işlem hacmi ($)', 0],
  ['bot_lock_pct', 'Bot: kâr kilidi (%, 0 = kapalı)', 1], ['bot_day_stop_pct', 'Bot: gün içi eleme (% zarar, 0 = kapalı)', 1],
  ['hakem_max_trades', 'Hakem: günde en fazla işlem', 0],
  ['evo_max_new', 'Evrim botu: günde en fazla yeni işlem', 0], ['evo_position_pct', 'Evrim botu: işlem başına (% özsermaye)', 1],
  ['evo_max_positions', 'Evrim botu: en fazla açık pozisyon', 0],
];
const PT_LEVEL_TR = { info: 'Bilgi', trade: 'İşlem', bot: 'Bot', warn: 'Uyarı', order: 'Emir' };

function usd(v, d = 2) { v = num(v); return v == null ? '—' : fmtNum(v, d) + ' $'; }
function usdSigned(v, d = 2) { v = num(v); return v == null ? '—' : fmtSigned(v, d) + ' $'; }
function ptLabel(group, k) { const L = obj(obj(PAGES.islem.data).labels); return obj(L[group])[k] || k || '—'; }

function ptKpis(d) {
  const a = obj(d.account);
  const k = (label, val, sub, cls, title) => `<div class="pt-kpi"${title ? ` title="${esc(title)}"` : ''}><div class="pt-kl">${esc(label)}</div>`
    + `<div class="pt-kv ${cls || ''}">${val}</div>${sub ? `<div class="pt-ks">${sub}</div>` : ''}</div>`;
  return k('Özsermaye', esc(usd(a.equity)), `Başlangıç ${esc(usd(a.start_cash, 0))}`)
    + k('Toplam K/Z', esc(usdSigned(a.total_pnl)), `<span class="${dirClass(a.total_pnl)}">${esc(fmtPct(a.total_pnl_pct))}</span>`, dirClass(a.total_pnl))
    + k('Bugün K/Z', esc(usdSigned(a.day_pnl)), a.day_pnl_pct != null
      ? `<span class="${dirClass(a.day_pnl)}">${esc(fmtPct(a.day_pnl_pct))}</span> · gerçekleşen ${esc(usdSigned(a.realized_today))}`
      : 'Seans başlayınca hesaplanır', dirClass(a.day_pnl))
    + k('Açık K/Z', esc(usdSigned(a.unrealized)), `Pozisyon değeri ${esc(usd(a.positions_value))}`, dirClass(a.unrealized))
    + k('Nakit', esc(usd(a.cash)), `Alım gücü ${esc(usd(a.buying_power))}`, '', 'Alım gücü: kaldıraç limiti içinde açılabilecek ek pozisyon')
    + (a.halted ? '<div class="pt-kpi warn"><div class="pt-kl">Risk</div><div class="pt-kv down">Durduruldu</div><div class="pt-ks">Günlük zarar limiti aşıldı; bugün yeni pozisyon açılamaz</div></div>' : '');
}

function ptOrderForm() {
  const sideSeg = segHtml('pt-side', [['buy', 'Al'], ['sell', 'Sat']], PT.side, 'Yön');
  return '<form id="pt-form" class="pt-form" autocomplete="off">'
    + '<div class="ph-row"><h3 class="ph">Yeni emir</h3><span class="sp"></span><span class="dim sm" id="pt-sess"></span></div>'
    + '<div class="pt-row"><label class="pt-f"><span>Sembol</span><input class="inp tk-inp" id="pt-sym" data-f="ptSym" placeholder="AAPL" maxlength="10" spellcheck="false" required></label>'
    + `<div class="pt-f"><span>Yön</span>${sideSeg}</div></div>`
    + '<div class="pt-quote" id="pt-quote"><span class="dim">Fiyatı görmek için sembol yazın</span></div>'
    + '<div class="pt-row"><label class="pt-f"><span>Miktar</span><span class="pt-inl"><select class="sel sm" id="pt-qmode" data-f="ptQmode" aria-label="Miktar türü"><option value="qty">Adet</option><option value="notional">Tutar ($)</option></select>'
    + '<input class="inp pt-num" id="pt-qty" inputmode="decimal" placeholder="10" data-f="ptQty" required></span></label>'
    + '<label class="pt-f"><span>Emir türü</span><select class="sel" id="pt-type" data-f="ptType"><option value="market">Piyasa</option><option value="limit">Limit</option><option value="stop">Stop</option></select></label>'
    + '<label class="pt-f" id="pt-price-f" hidden><span id="pt-price-l">Limit fiyatı ($)</span><input class="inp pt-num" id="pt-price" inputmode="decimal" data-f="ptPrice"></label></div>'
    + '<div class="pt-row"><label class="pt-f"><span>Zarar durdur (%)</span><input class="inp pt-num" id="pt-sl" inputmode="decimal" placeholder="ör. 2" title="Pozisyon açıldığında giriş fiyatına göre otomatik zarar durdur seviyesi"></label>'
    + '<label class="pt-f"><span>Kâr al (%)</span><input class="inp pt-num" id="pt-tp" inputmode="decimal" placeholder="ör. 4" title="Pozisyon açıldığında giriş fiyatına göre otomatik kâr al seviyesi"></label>'
    + '<label class="pt-f"><span>Geçerlilik</span><select class="sel" id="pt-tif"><option value="day">Gün sonuna kadar</option><option value="gtc">İptal edilene kadar</option></select></label></div>'
    + '<label class="pt-chk" id="pt-ext-f" hidden><input type="checkbox" id="pt-ext"> Uzatılmış seansta da gerçekleşsin (piyasa öncesi / sonrası)</label>'
    + '<div class="pt-est" id="pt-est"></div>'
    + `<div class="pt-row end"><button type="submit" class="btn primary" id="pt-submit">${icon('check')} Alış emrini gönder</button></div></form>`;
}

function ptPosTable(rows) {
  if (!rows.length) return stateHtml('empty', 'Açık pozisyon yok.');
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Yön</th><th>Adet</th><th>Ort. maliyet</th><th>Son fiyat</th><th>Değer</th><th>K/Z</th><th>K/Z %</th><th>Ağırlık</th><th>Zarar durdur</th><th>Kâr al</th><th class="l">Kaynak</th><th class="l">Çıkış</th><th></th></tr></thead><tbody>'
    + rows.map(p => `<tr><td class="l"><button type="button" class="tk" data-tk="${esc(p.symbol)}">${esc(p.symbol)}</button></td>`
      + `<td class="l">${p.side === 'short' ? '<span class="pill amber">Açığa</span>' : '<span class="pill ok">Uzun</span>'}</td>`
      + `<td>${esc(fmtInt(Math.abs(p.qty)))}</td><td>${esc(fmtPrice(p.avg_price))}</td>`
      + `<td title="${esc(p.last_ts ? 'Fiyat zamanı: ' + tsTitle(p.last_ts) : '')}">${esc(fmtPrice(p.last_price))}</td><td>${esc(usd(p.market_value))}</td>`
      + `<td class="${dirClass(p.unrealized)}">${esc(usdSigned(p.unrealized))}</td><td class="${dirClass(p.unrealized_pct)}">${esc(fmtPct(p.unrealized_pct))}</td>`
      + `<td>${esc(p.weight_pct != null ? '%' + fmtNum(p.weight_pct, 1) : '—')}</td>`
      + `<td>${esc(fmtPrice(p.stop_price))}</td><td>${esc(fmtPrice(p.target_price))}</td><td class="l">${esc(ptLabel('origins', p.origin))}</td>`
      + `<td class="l" title="${esc(p.exit_at ? tsTitle(p.exit_at) : '')}">${p.exit_at ? esc(whenShort(p.exit_at)) : '<span class="dim">—</span>'}</td>`
      + `<td class="l"><button type="button" class="btn sm ghost" data-act="pt-levels" data-sym="${esc(p.symbol)}" title="Zarar durdur / kâr al seviyelerini değiştir">Seviyeler</button>`
      + `<button type="button" class="btn sm danger" data-act="pt-close" data-sym="${esc(p.symbol)}">Kapat</button></td></tr>`).join('')
    + '</tbody></table></div>';
}

function ptOrderPrice(o) {
  if (o.type === 'limit') return fmtPrice(o.limit_price);
  if (o.type === 'stop') return 'tetik ' + fmtPrice(o.stop_price);
  return 'Piyasa';
}

function ptOpenTable(rows) {
  if (!rows.length) return stateHtml('empty', 'Bekleyen emir yok.');
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Zaman</th><th class="l">Sembol</th><th class="l">Yön</th><th>Adet</th><th class="l">Tür</th><th>Fiyat</th><th class="l">Geçerlilik</th><th class="l">Kaynak</th><th></th></tr></thead><tbody>'
    + rows.map(o => `<tr><td class="l" title="${esc(tsTitle(o.created_at))}">${esc(whenShort(o.created_at))}</td>`
      + `<td class="l"><button type="button" class="tk" data-tk="${esc(o.symbol)}">${esc(o.symbol)}</button></td>`
      + `<td class="l ${o.side === 'buy' ? 'up' : 'down'}">${esc(ptLabel('sides', o.side))}</td><td>${esc(fmtInt(o.qty))}</td>`
      + `<td class="l">${esc(ptLabel('types', o.type))}${o.ext_hours ? ' <span class="pill">uzatılmış</span>' : ''}</td><td>${esc(ptOrderPrice(o))}</td>`
      + `<td class="l">${o.expires_at ? `<span title="${esc(tsTitle(o.expires_at))}">bitiş ${esc(whenShort(o.expires_at))}</span>` : 'İptal edilene kadar'}</td>`
      + `<td class="l">${esc(ptLabel('origins', o.origin))}</td>`
      + `<td class="l"><button type="button" class="btn sm ghost danger" data-act="pt-cancel" data-id="${esc(o.id)}">İptal</button></td></tr>`).join('')
    + '</tbody></table></div>';
}

function ptHistTable(rows) {
  if (!rows.length) return stateHtml('empty', 'Henüz işlem yok.');
  const st = s => (s === 'filled' ? 'ok' : s === 'rejected' ? 'amber' : '');
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Zaman</th><th class="l">Sembol</th><th class="l">Yön</th><th>Adet</th><th class="l">Tür</th><th class="l">Durum</th><th>Gerçekleşen fiyat</th><th>Ücret</th><th>Gerçekleşen K/Z</th><th class="l">Kaynak</th><th class="l">Not</th></tr></thead><tbody>'
    + rows.map(o => {
      const t = o.filled_at || o.created_at;
      return `<tr><td class="l" title="${esc(tsTitle(t))}">${esc(whenShort(t))}</td>`
        + `<td class="l"><button type="button" class="tk" data-tk="${esc(o.symbol)}">${esc(o.symbol)}</button></td>`
        + `<td class="l ${o.side === 'buy' ? 'up' : 'down'}">${esc(ptLabel('sides', o.side))}</td><td>${esc(fmtInt(o.qty))}</td>`
        + `<td class="l">${esc(ptLabel('types', o.type))}</td><td class="l"><span class="pill ${st(o.status)}">${esc(ptLabel('status', o.status))}</span></td>`
        + `<td>${esc(fmtPrice(o.fill_price))}</td><td>${o.fee ? esc(usd(o.fee)) : '—'}</td>`
        + `<td class="${dirClass(o.realized_pnl)}">${o.realized_pnl != null ? esc(usdSigned(o.realized_pnl)) : '—'}</td>`
        + `<td class="l">${esc(ptLabel('origins', o.origin))}</td><td class="l ell" title="${esc(o.note || '')}">${esc(o.note || '')}</td></tr>`;
    }).join('')
    + '</tbody></table></div>';
}

function ptStats(st) {
  st = obj(st);
  const row = (l, v, cls) => `<div class="pt-st"><span class="muted">${esc(l)}</span><b class="${cls || ''}">${v}</b></div>`;
  return '<div class="pt-stats">'
    + row('Kapanan işlem', esc(fmtInt(st.trades)))
    + row('Kazanma oranı', st.win_rate != null ? esc('%' + fmtNum(st.win_rate, 1)) + ` <span class="dim sm">(${esc(fmtInt(st.wins))} / ${esc(fmtInt(st.losses))})</span>` : '—')
    + row('Ortalama kazanç', esc(usdSigned(st.avg_win)), 'up')
    + row('Ortalama kayıp', esc(usdSigned(st.avg_loss)), 'down')
    + row('Kâr faktörü', st.profit_factor != null ? esc(fmtNum(st.profit_factor, 2)) : '—', st.profit_factor == null ? '' : st.profit_factor >= 1 ? 'up' : 'down')
    + row('Toplam gerçekleşen', esc(usdSigned(st.realized_total)), dirClass(st.realized_total))
    + row('En büyük düşüş', st.max_drawdown_pct != null ? esc(fmtPct(st.max_drawdown_pct)) : '—', 'down')
    + '</div><p class="dim sm pt-hint">Kâr faktörü = toplam kazanç / toplam kayıp (1 üstü kârlı). Anlamlı bir sonuç için en az birkaç hafta ve onlarca işlem gerekir.</p>';
}

function ptChart(d) {
  const pts = arr(d.equity);
  if (pts.length < 2) return stateHtml('empty', 'Özsermaye eğrisi seans sırasında 5 dakikada bir kaydedilir.');
  const vals = pts.map(p => p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  return `<div class="pt-chart">${spark(vals, { w: 600, h: 150, base: obj(d.account).start_cash, cls: 'pt-curve' })}</div>`
    + `<div class="pt-chart-ax dim sm"><span>${esc(whenShort(pts[0][0]))}</span><span>en düşük ${esc(usd(lo, 0))} · en yüksek ${esc(usd(hi, 0))}</span><span>${esc(whenShort(pts[pts.length - 1][0]))}</span></div>`;
}

function ptBotPanel(s) {
  s = obj(s);
  const val = (k, dec) => (s[k] != null ? String(dec ? +Number(s[k]).toFixed(dec) : s[k]).replace('.', ',') : '');
  const fld = ([k, label, dec]) => `<label class="pt-f"><span>${esc(label)}</span><input class="inp pt-num" id="pt-s-${k}" inputmode="decimal" value="${esc(val(k, dec))}"></label>`;
  const chk = (k, label) => `<label class="pt-chk"><input type="checkbox" id="pt-s-${k}"${s[k] ? ' checked' : ''}> ${esc(label)}</label>`;
  const tm = (k, label) => `<label class="pt-f"><span>${esc(label)}</span><input class="inp" type="time" id="pt-s-${k}" value="${esc(s[k] || '')}"></label>`;
  return '<p class="pt-hint dim sm">Kural tabanlı (yapay zekâ yok): Fırsatlar listesinde skoru yüksek, gün içinde yeterli hareket ve hacim yakalamış hisselere girer; '
    + 'her pozisyona zarar durdur ve kâr al koyar, risk limitlerine uyar' + (s.eod_flatten ? ' ve kapanıştan 5 dk önce pozisyonlarını kapatır' : '')
    + '. Yalnızca ABD seansı açıkken çalışır; aynı hisseye günde bir kez girer.</p>'
    + `<div class="pt-set">${PT_SET_FIELDS.map(fld).join('')}${tm('bot_start_et', 'Bot: ilk giriş (ET)')}${tm('bot_last_entry_et', 'Bot: son giriş (ET)')}</div>`
    + `<div class="pt-row">${chk('eod_flatten', 'Gün sonunda bot pozisyonlarını kapat')}${chk('allow_short', 'Açığa satışa izin ver')}</div>`
    + `<div class="pt-row">${chk('bot_direction_mixed', 'Bot "karışık" yönlü fırsatlara da girsin')}${chk('bot_cut10', 'Bot: 10. dakikada %−0,8 ya da kötüyse kes')}${chk('bot_cull30', 'Bot: her 30 dakikada kâr yoksa çık')}</div>`
    + '<p class="pt-hint dim sm">Bot ayarları her gece canlı öğrenmeyle güncellenebilir: en iyi sonuç veren ayar çeşidi yeterli kanıtla öne geçerse uygulanır (Test Laboratuvarı → 0 · Canlı öğrenme; oradan kapatılabilir ve geri alınabilir).</p>'
    + `<div class="pt-row">${chk('hakem_enabled', 'Hakem bot (Test Laboratuvarı) açılışta sanal işlem yapsın')}<a href="#lab" class="dim sm">sonuçlarını önce Test Laboratuvarı'nda inceleyin</a></div>`
    + `<div class="pt-row">${chk('evo_enabled', 'Evrim botu açılıştan sonra sanal işlem yapsın (pozisyonları kuralın süresi kadar, en fazla 5 gün tutar)')}</div>`
    + `<div class="pt-row end"><button type="button" class="btn primary sm" data-act="pt-save">${icon('check')} Ayarları kaydet</button></div>`
    + `<div class="pt-reset"><span class="muted sm">Hesabı sıfırla (tüm sanal işlemler silinir):</span><input class="inp pt-num" id="pt-start" inputmode="decimal" value="${esc(String(s.start_cash || 100000))}" aria-label="Başlangıç bakiyesi ($)"><span class="dim sm">$</span>`
    + '<button type="button" class="btn sm ghost danger" data-act="pt-reset">Sıfırla</button></div>';
}

function ptLog(rows) {
  if (!rows.length) return stateHtml('empty', 'Günlük boş.');
  const cls = l => (l === 'warn' ? 'amber' : l === 'trade' ? 'ok' : l === 'bot' ? 'accent' : '');
  return '<div class="pt-log">' + rows.map(r => `<div class="pt-lr"><span class="dim" title="${esc(tsTitle(r.ts))}">${esc(whenShort(r.ts))}</span>`
    + `<span class="pill ${cls(r.level)}">${esc(PT_LEVEL_TR[r.level] || r.level)}</span>`
    + (r.symbol ? `<button type="button" class="tk" data-tk="${esc(r.symbol)}">${esc(r.symbol)}</button>` : '<span></span>')
    + `<span class="pt-msg">${esc(r.msg)}</span></div>`).join('') + '</div>';
}

/** Number from a form field; tr-TR "1.234,5" and plain "12.5" both work. null = empty, NaN = invalid. */
function ptNum(id) {
  const el = byId(id);
  const v = el ? String(el.value || '').trim().replace(/\s/g, '') : '';
  if (!v) return null;
  const n = Number(/,/.test(v) ? v.replace(/\./g, '').replace(',', '.') : v);
  return Number.isFinite(n) ? n : NaN;
}



function microRules(s) {
  s = obj(s);
  const n = (v, d = 1) => fmtNum(v, d).replace(/,0+$/, '');
  return [`fiyat ${n(s.min_price)}–${n(s.max_price)} $`, `değişim %${n(s.min_chg)}–${n(s.max_chg)}`,
    s.min_rvol ? `göreli hacim ≥ ${n(s.min_rvol)}x` : '', `hacim ≥ ${n((s.min_dvol || 0) / 1e6, 0)} M$/gün`,
    `zarar durdur %${n(s.sl, 2)}, kâr al %${n(s.tp)}`, s.cut10 ? '10 dk kesme' : '', s.cull30 ? '30 dk eleme' : '',
    s.lock ? `kâr kilidi %${n(s.lock)}` : '', s.day_stop ? `gün içi eleme −%${n(s.day_stop)}` : '',
    `${s.start || ''}–${s.last || ''} ET, günde ≤ ${fmtInt(s.max_trades)}`].filter(Boolean).join(' · ');
}

function ptMicroHtml(m) {
  if (!m || !m.account) return stateHtml('loading');
  const a = obj(m.account), cmp = obj(m.comparison), pos = arr(m.positions), st = obj(m.stats);
  const k = (label, val, sub, cls) => `<div class="pt-kpi"><div class="pt-kl">${esc(label)}</div><div class="pt-kv ${cls || ''}">${val}</div>${sub ? `<div class="pt-ks">${sub}</div>` : ''}</div>`;
  let h = '<div class="pt-kpis">'
    + k('Mikro özsermaye', esc(usd(a.equity)), `Başlangıç ${esc(usd(a.start_cash, 0))} · nakit ${esc(usd(a.cash))}`)
    + k('Toplam K/Z', esc(usdSigned(a.total_pnl)), `<span class="${dirClass(a.total_pnl)}">${esc(fmtPct(a.total_pnl_pct))}</span>`, dirClass(a.total_pnl))
    + k('Bugün', a.day_pnl != null ? esc(usdSigned(a.day_pnl)) : '—', a.day_pnl_pct != null ? `<span class="${dirClass(a.day_pnl)}">${esc(fmtPct(a.day_pnl_pct))}</span>` : 'seans başlayınca', dirClass(a.day_pnl))
    + k('Kapanan işlem', esc(fmtInt(st.trades)), `${esc(fmtInt(st.wins))} kazanç · toplam ${esc(usdSigned(st.realized_total))}`)
    + k('Ana hesap (100.000 $)', cmp.main_pnl_pct != null ? esc(fmtPct(cmp.main_pnl_pct)) : '—', 'aynı dönemde toplam getiri', dirClass(cmp.main_pnl_pct))
    + '</div>'
    + `<p class="muted sm">Kurallar (canlı öğrenmeden): ${esc(microRules(m.settings))}. Yalnızca normal seansta; her işlem en fazla 40 $, en fazla 3 pozisyon, kapanıştan 5 dk önce hepsi kapanır. Alış-satışta en az yarım sent fark (küçük hisselerde gerçekçi maliyet) düşülür.</p>`
    + `<div class="ph-row"><div class="sub">Açık küçük hisse pozisyonları</div><span class="sp"></span><button type="button" class="btn sm ghost danger" data-act="micro-reset">Mikro hesabı sıfırla</button></div>`;
  h += !pos.length ? '<p class="muted sm">Açık pozisyon yok (bot 1–10 $ arası hareketli hisseleri tarıyor).</p>'
    : '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th>Adet</th><th>Alış</th><th>Son fiyat</th><th>Değer</th><th>K/Z</th><th>K/Z %</th><th>Zarar durdur</th><th>Kâr al</th></tr></thead><tbody>'
      + pos.map(p => `<tr><td class="l"><button type="button" class="tk" data-tk="${esc(p.symbol)}">${esc(p.symbol)}</button></td><td>${esc(fmtInt(p.qty))}</td>`
        + `<td>${esc(fmtPrice(p.avg_price))}</td><td>${esc(fmtPrice(p.last_price))}</td><td>${esc(usd(p.market_value))}</td>`
        + `<td class="${dirClass(p.unrealized_pnl)}">${esc(usdSigned(p.unrealized_pnl))}</td><td class="${dirClass(p.unrealized_pct)}">${esc(fmtPct(p.unrealized_pct))}</td>`
        + `<td>${esc(fmtPrice(p.stop_price))}</td><td>${esc(fmtPrice(p.target_price))}</td></tr>`).join('')
      + '</tbody></table></div>';
  const orders = arr(m.orders).slice(0, 8);
  if (orders.length) {
    h += '<div class="sub">Son mikro işlemler</div><div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Zaman</th><th class="l">Sembol</th><th class="l">Yön</th><th>Adet</th><th>Fiyat</th><th>K/Z</th><th class="l">Not</th></tr></thead><tbody>'
      + orders.map(o => `<tr><td class="l" title="${esc(tsTitle(o.created_at))}">${esc(whenShort(o.created_at))}</td><td class="l">${esc(o.symbol)}</td>`
        + `<td class="l ${o.side === 'buy' ? 'up' : 'down'}">${o.side === 'buy' ? 'Al' : 'Sat'}</td><td>${esc(fmtInt(o.qty))}</td><td>${esc(fmtPrice(o.fill_price))}</td>`
        + `<td class="${dirClass(o.realized_pnl)}">${o.realized_pnl != null ? esc(usdSigned(o.realized_pnl)) : '—'}</td><td class="l ell" title="${esc(o.note || '')}">${esc(o.note || '')}</td></tr>`).join('')
      + '</tbody></table></div>';
  }
  return h;
}

// Realistic $100 account (real_sim.py): mixed bots under real cash-account rules.
function ptRealHtml(m) {
  if (!m || !m.account) return stateHtml('loading');
  const a = obj(m.account), cmp = obj(m.comparison), cost = obj(m.costs), pos = arr(m.positions), pend = arr(m.pending);
  const k = (label, val, sub, cls) => `<div class="pt-kpi"><div class="pt-kl">${esc(label)}</div><div class="pt-kv ${cls || ''}">${val}</div>${sub ? `<div class="pt-ks">${sub}</div>` : ''}</div>`;
  const pct = v => v != null ? `<span class="${dirClass(v)}">${esc(fmtPct(v))}</span>` : '—';
  let h = '<div class="pt-kpis">'
    + k('Özsermaye', esc(usd(a.equity)), `Başlangıç ${esc(usd(a.start_cash, 0))}`)
    + k('Toplam K/Z', esc(usdSigned(a.total_pnl)), pct(a.total_pnl_pct), dirClass(a.total_pnl))
    + k('Bugün', a.day_pnl != null ? esc(usdSigned(a.day_pnl)) : '—', a.day_pnl_pct != null ? pct(a.day_pnl_pct) : 'seans başlayınca', dirClass(a.day_pnl))
    + k('Kullanılabilir nakit', esc(usd(a.settled_cash)), `takasta bekleyen ${esc(usd(a.unsettled_cash))}${a.pending ? ` · emirde ${esc(usd(a.pending))}` : ''}`)
    + k('Gerçek dünya maliyeti', esc(usd((cost.fees || 0) + (cost.slippage || 0))), `ücret ${esc(usd(cost.fees))} · makas/gecikme ${esc(usd(cost.slippage))}`)
    + k('Karşılaştırma', pct(cmp.real_pnl_pct), `sanal mikro ${cmp.micro_pnl_pct != null ? esc(fmtPct(cmp.micro_pnl_pct)) : '—'} · ana hesap ${cmp.main_pnl_pct != null ? esc(fmtPct(cmp.main_pnl_pct)) : '—'}`)
    + '</div>';
  const curve = arr(m.curve).map(x => x.equity);
  if (curve.length > 1) h += `<div class="pt-real-curve">${spark(curve, { w: 600, h: 60, base: a.start_cash, cls: dirClass(a.total_pnl) })}</div>`;
  if (a.halted_today) h += '<p class="amber sm">Günlük %5 zarar sınırına ulaşıldı: bugün yeni işlem açılmayacak.</p>';
  const src = obj(m.by_source);
  h += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Bot (yuva)</th><th>Kapanan işlem</th><th>Kazanç</th><th>Gerçekleşen K/Z</th><th>Açık</th></tr></thead><tbody>'
    + Object.values(src).map(s => `<tr><td class="l">${esc(s.label)}</td><td>${esc(fmtInt(s.trades))}</td><td>${s.trades ? esc(fmtInt(s.wins)) + '/' + esc(fmtInt(s.trades)) : '—'}</td>`
      + `<td class="${dirClass(s.realized)}">${esc(usdSigned(s.realized))}</td><td>${s.open ? '<span class="pill ok">açık</span>' : '—'}</td></tr>`).join('')
    + '</tbody></table></div>';
  h += `<div class="ph-row"><div class="sub">Açık pozisyonlar</div><span class="sp"></span>`
    + `<button type="button" class="btn sm" data-act="real-toggle">${m.enabled ? 'Durdur' : 'Başlat'}</button>`
    + `<button type="button" class="btn sm ghost danger" data-act="real-reset">Sıfırla</button></div>`;
  h += !pos.length && !pend.length ? `<p class="muted sm">${m.enabled ? 'Açık pozisyon yok; bot sinyal bekliyor.' : '<b>Durduruldu.</b>'}</p>`
    : '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Bot</th><th>Adet</th><th>Alış</th><th>Son</th><th>Değer</th><th>K/Z %</th><th>Zarar durdur</th><th>Kâr al</th></tr></thead><tbody>'
      + pend.map(o => `<tr class="dim"><td class="l">${esc(o.symbol)}</td><td class="l">${esc(o.source)}</td><td colspan="7" class="l">piyasa emri gönderildi (${esc(usd(o.notional))}), dolum bekleniyor</td></tr>`).join('')
      + pos.map(p => `<tr><td class="l"><button type="button" class="tk" data-tk="${esc(p.symbol)}">${esc(p.symbol)}</button></td><td class="l">${esc(p.source_tr)}</td>`
        + `<td>${esc(fmtNum(p.qty, 4))}</td><td>${esc(fmtPrice(p.avg_price))}</td><td>${esc(fmtPrice(p.last_price))}</td><td>${esc(usd(p.market_value))}</td>`
        + `<td class="${dirClass(p.unrealized_pct)}">${esc(fmtPct(p.unrealized_pct))}</td><td>${esc(fmtPrice(p.stop_price))}</td><td>${esc(fmtPrice(p.target_price))}</td></tr>`).join('')
      + '</tbody></table></div>';
  const un = arr(m.unsettled);
  if (un.length) h += `<p class="dim sm">Takas bekleyen: ${un.map(u => `${esc(usd(u.amount))} → ${esc(u.settle_date)}`).join(' · ')}</p>`;
  const logs = arr(m.log).slice(0, 12);
  if (logs.length) h += '<div class="sub">Günlük</div>' + ptLog(logs);
  h += `<details class="sm"><summary class="dim">Bu hesabın gerçek dünya kuralları</summary><ul class="dim">${arr(m.rules).map(r => `<li>${esc(r)}</li>`).join('')}</ul></details>`;
  return h;
}

function ptLearnHtml(L) {
  if (!L) return '<p class="muted sm">Canlı öğrenme durumu alınamadı.</p>';
  const rep = obj(L.report), lv = obj(L.live), st = obj(L.settings), sp = obj(obj(L.speeds)[st.speed]);
  const fCh = arr(obj(rep.firsat).changes).filter(c => c.kind === 'ayar'), evo = obj(rep.evo);
  const evoN = k => arr(evo.changes).filter(c => c.kind === k).length;
  const champ = obj(obj(lv.firsat).champion);
  return `<p class="muted sm">Her gün bütün bot çeşitleri gölgede yarışır; gece kazananlar klonlanır, kaybedenler elenir, ertesi gün sanal hesap öğrenilenle işlem yapar `
    + `(${esc(sp.label || '')}${st.auto_apply ? ', otomatik uygulama açık' : ', <b>otomatik uygulama kapalı</b>'}).</p>`
    + '<div class="tbl-wrap"><table class="tbl"><tbody>'
    + `<tr><td class="l muted">Fırsat botu şu an</td><td class="l">${esc(obj(L.firsat).applied || '—')}</td></tr>`
    + `<tr><td class="l muted">Mikro bot şu an</td><td class="l">${esc(obj(L.mikro).applied || '—')}</td></tr>`
    + `<tr><td class="l muted">Evrim botları</td><td class="l">${L.trading_arm === 'ana' ? 'Ana kol (10 yıllık, değişmez)' : 'Evrim kolu'} işlemde · ${esc(fmtInt(obj(obj(L.counts).evrim).aktif))} bot, ${esc(fmtInt(obj(obj(L.counts).evrim).aday))} aday</td></tr>`
    + `<tr><td class="l muted">Son gece${rep.date ? ' (' + esc(rep.date) + ')' : ''}</td><td class="l">${rep.date ? `${fCh.length ? 'fırsat botu ayarı değişti: ' + esc(arr(fCh[0].diff).join(' · ')) : 'fırsat botu ayarı değişmedi'} · evrim: ${evoN('klon')} yeni aday, ${evoN('terfi')} terfi, ${evoN('emekli')} emekli` : 'henüz çalışmadı'}</td></tr>`
    + (lv.ts ? `<tr><td class="l muted">Gün içi (${esc(whenShort(lv.ts))})</td><td class="l">uygulanan ayar bugün ${champ.id ? labPct(champ.ret_pct, 3) + ` (${esc(fmtInt(champ.n))} işlem)` : '—'} · ${esc(fmtInt(obj(lv.firsat).eliminated || 0))} çeşit gün içi elemeyle durdu</td></tr>` : '')
    + '</tbody></table></div>';
}

PAGES.islem = {
  title: 'Sanal İşlem', poll: 10, bodyId: 'pt-kpis',
  mount(view) {
    this.data = null; this.botSig = '';
    view.innerHTML = '<div class="page page-paper">'
      + `<div class="pt-note">${icon('warn')}<span><b>Sanal para.</b> Emirler hiçbir aracı kuruma gönderilmez; Yahoo fiyatlarıyla simüle edilir (son fiyat ± kayma, emir defteri yok, tam hisse, açığa satışta ödünç maliyeti yok). Sanal sonuçlar gerçek getiriyi garanti etmez.</span></div>`
      + '<div id="page-err"></div>'
      + `<div class="pt-kpis" id="pt-kpis">${stateHtml('loading')}</div>`
      + `<div class="pt-grid"><section class="panel">${ptOrderForm()}</section>`
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Özsermaye eğrisi</h3><span class="upd" id="pt-eq-upd"></span></div><div id="pt-chart"></div></section></div>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Açık pozisyonlar</h3><span class="sp"></span><button type="button" class="btn sm ghost danger" data-act="pt-close-all" id="pt-close-all" hidden>Tümünü kapat</button></div><div id="pt-pos"></div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Bekleyen emirler</h3></div><div id="pt-open"></div></section>'
      + '<section class="panel" id="pt-real-panel"><div class="ph-row"><h3 class="ph">Gerçekçi hesap: 100 $, gerçek nakit hesap kurallarıyla (karma: evrim + fırsat + mikro)</h3></div><div id="pt-real-content"></div></section>'
      + '<section class="panel" id="pt-micro-panel"><div class="ph-row"><h3 class="ph">Mikro hesap: 100 $ ile küçük hisseler (1–10 $)</h3></div><div id="pt-micro-content"></div></section>'
      + '<section class="panel" id="pt-learn-panel"><div class="ph-row"><h3 class="ph">Canlı öğrenme</h3><span class="sp"></span><a href="#lab" class="dim sm">Ayrıntılar: Test Laboratuvarı → 0 · Canlı öğrenme</a></div>'
      + '<div id="pt-learn-content"></div></section>'
      + '<div class="dash-2"><section class="panel"><div class="ph-row"><h3 class="ph">Bot ve risk ayarları</h3><span class="sp"></span><span id="pt-bot-state" class="pt-bot-state"></span></div><div id="pt-bot"></div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Performans</h3></div><div id="pt-stats"></div></section></div>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">İşlem geçmişi</h3></div><div id="pt-hist"></div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Motor günlüğü</h3><span class="upd">bot kararları, gerçekleşen emirler, uyarılar</span></div><div id="pt-log"></div></section>'
      + '</div>';
    this.syncForm();
  },
  async refresh(first, live) {
    const [d, learn, micro, real] = await Promise.all([
      api('/api/paper'),
      api('/api/learn').catch(() => null),
      api('/api/paper/micro').catch(() => null),
      api('/api/paper/real').catch(() => null),
    ]);
    if (!live()) return;
    this.learnData = learn;
    this.microData = micro;
    this.realData = real;
    this.apply(d);
  },
  apply(d) {
    this.data = d;
    this.render();
  },
  render() {
    const d = this.data;
    if (!d) return;
    setHtml('pt-kpis', ptKpis(d));
    setHtml('pt-chart', ptChart(d));
    const pos = arr(d.positions);
    if (!hasSelectionIn('pt-pos')) setHtml('pt-pos', ptPosTable(pos));
    const ca = byId('pt-close-all');
    if (ca) ca.hidden = pos.length < 2;
    setHtml('pt-open', ptOpenTable(arr(d.open_orders)));
    if (!hasSelectionIn('pt-hist')) setHtml('pt-hist', ptHistTable(arr(d.history)));
    if (!hasSelectionIn('pt-log')) setHtml('pt-log', ptLog(arr(d.log)));
    setHtml('pt-stats', ptStats(d.stats));
    const s = obj(d.settings);
    // Settings inputs are re-rendered only when the stored settings change, so a poll never overwrites typing.
    const sig = JSON.stringify(s);
    if (sig !== this.botSig) { this.botSig = sig; setHtml('pt-bot', ptBotPanel(s)); }
    setHtml('pt-bot-state', `<span class="pill ${s.bot_enabled ? 'ok' : ''}">${s.bot_enabled ? 'Bot açık' : 'Bot kapalı'}</span>`
      + `<button type="button" class="btn sm${s.bot_enabled ? '' : ' primary'}" data-act="pt-bot-toggle">${s.bot_enabled ? 'Botu durdur' : 'Botu başlat'}</button>`);
    const eng = obj(d.engine);
    setText('pt-eq-upd', eng.last_tick ? 'motor ' + rel(eng.last_tick) : 'motor bekleniyor');
    setText('pt-sess', 'Seans: ' + (obj(obj(S.meta).sessions)[d.session] || d.session || '—'));
    setBadge('islem', pos.length, `${pos.length} açık sanal pozisyon`);
    setHtml('pt-learn-content', ptLearnHtml(this.learnData));
    if (!hasSelectionIn('pt-micro-content')) setHtml('pt-micro-content', ptMicroHtml(this.microData));
    if (!hasSelectionIn('pt-real-content')) setHtml('pt-real-content', ptRealHtml(this.realData));
  },
  onFilter(f, v, type) {
    if (f === 'ptType') { if (type === 'change') { PT.type = String(v); this.syncForm(); } }
    else if (f === 'ptQmode') { if (type === 'change') { PT.qmode = String(v); this.syncForm(); } }
    else if (f === 'ptSym') { if (type === 'input') debounce('pt-quote', () => this.loadQuote(), 450); }
    else if (f === 'ptQty' || f === 'ptPrice') this.estimate();
  },
  syncForm() {
    const pf = byId('pt-price-f'), ef = byId('pt-ext-f'), pl = byId('pt-price-l');
    if (pf) pf.hidden = PT.type === 'market';
    if (pl) pl.textContent = PT.type === 'stop' ? 'Tetik fiyatı ($)' : 'Limit fiyatı ($)';
    if (ef) ef.hidden = PT.type !== 'limit';
    const q = byId('pt-qty');
    if (q) q.placeholder = PT.qmode === 'notional' ? '5000' : '10';
    for (const b of document.querySelectorAll('[data-act="pt-side"]')) { const on = b.dataset.v === PT.side; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
    const sb = byId('pt-submit');
    if (sb) { sb.classList.toggle('sell', PT.side === 'sell'); sb.innerHTML = `${icon('check')} ${PT.side === 'sell' ? 'Satış emrini gönder' : 'Alış emrini gönder'}`; }
    this.estimate();
  },
  async loadQuote() {
    const sym = normSym((byId('pt-sym') || {}).value);
    PT.quoteSym = sym;
    if (!sym) { PT.quote = null; setHtml('pt-quote', '<span class="dim">Fiyatı görmek için sembol yazın</span>'); this.estimate(); return; }
    setHtml('pt-quote', `<span class="dim">${esc(sym)} fiyatı alınıyor…</span>`);
    try {
      const q = await api('/api/paper/quote', { params: { symbol: sym } });
      if (PT.quoteSym !== sym) return;
      PT.quote = q;
      const held = arr(obj(this.data).positions).find(p => p.symbol === sym);
      const kind = q.kind === 'pre' ? 'piyasa öncesi' : q.kind === 'post' ? 'piyasa sonrası' : 'son işlem';
      setHtml('pt-quote', `<b>${esc(sym)}</b> <span class="dim">${esc(q.name || '')}</span> · <b>${esc(fmtPrice(q.price))} $</b> `
        + `<span class="${dirClass(q.change_pct)}">${esc(fmtPct(q.change_pct))}</span> <span class="dim sm">(${esc(kind)}${q.ts ? ', ' + esc(rel(q.ts)) : ''})</span>`
        + (held ? ` · <span class="pill accent">Pozisyon: ${esc(fmtInt(held.qty))} adet</span>` : ''));
    } catch (e) {
      if (PT.quoteSym !== sym) return;
      PT.quote = null;
      setHtml('pt-quote', `<span class="down">${esc(errMsg(e))}</span>`);
    }
    this.estimate();
  },
  estimate() {
    const el = byId('pt-est');
    if (!el) return;
    const q = PT.quote, n = ptNum('pt-qty'), px = PT.type !== 'market' ? ptNum('pt-price') : null;
    const ref = px || (q && normSym(q.symbol) === normSym((byId('pt-sym') || {}).value) ? num(q.price) : null);
    if (!ref || !n || Number.isNaN(n)) { el.innerHTML = ''; return; }
    const qty = PT.qmode === 'notional' ? Math.floor(n / ref) : Math.floor(n);
    const eq = num(obj(obj(this.data).account).equity);
    el.innerHTML = qty >= 1
      ? `Yaklaşık ${esc(fmtInt(qty))} adet × ${esc(fmtPrice(ref))} $ = <b>${esc(usd(qty * ref))}</b>${eq ? ` <span class="dim">(özsermayenin %${esc(fmtNum(qty * ref / eq * 100, 1))} kadarı)</span>` : ''}`
      : '<span class="amber">Bu tutar 1 hisseye yetmiyor</span>';
  },
  async submitOrder() {
    const sym = normSym((byId('pt-sym') || {}).value);
    const n = ptNum('pt-qty'), px = ptNum('pt-price'), sl = ptNum('pt-sl'), tp = ptNum('pt-tp');
    if (!sym) { toast({ title: 'Sembol girin', kind: 'err' }); return; }
    if (!n || Number.isNaN(n) || n <= 0) { toast({ title: 'Geçerli bir miktar girin', kind: 'err' }); return; }
    if ([px, sl, tp].some(x => Number.isNaN(x))) { toast({ title: 'Sayı alanlarından biri geçersiz', kind: 'err' }); return; }
    const body = { symbol: sym, side: PT.side, type: PT.type, tif: (byId('pt-tif') || {}).value || 'day',
      ext_hours: !!(byId('pt-ext') || {}).checked, stop_loss_pct: sl, take_profit_pct: tp };
    body[PT.qmode === 'notional' ? 'notional' : 'qty'] = n;
    if (PT.type === 'limit') body.limit_price = px;
    if (PT.type === 'stop') body.stop_price = px;
    const btn = byId('pt-submit');
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/paper/order', { method: 'POST', body });
      const o = obj(r.order);
      if (o.status === 'filled') toast({ title: `${sym}: ${ptLabel('sides', o.side)} ${fmtInt(o.qty)} @ ${fmtPrice(o.fill_price)} $`, body: 'Sanal emir gerçekleşti', kind: 'ok' });
      else toast({ title: `${sym}: emir kaydedildi`, body: o.pending_reason || ptLabel('status', o.status), kind: 'info', timeout: 8000 });
      const q = byId('pt-qty');
      if (q) q.value = '';
      if (r.summary) this.apply(r.summary);
      this.estimate();
    } catch (e) {
      toast({ title: 'Emir reddedildi', body: errMsg(e), kind: 'err', timeout: 9000 });
    } finally { if (btn) btn.disabled = false; }
  },
  async post(path, body, okMsg) {
    try {
      const r = await api(path, { method: 'POST', body: body || {} });
      if (okMsg) toast({ title: okMsg, kind: 'ok' });
      const sum = r.summary || (r.account ? r : null);
      if (sum) this.apply(sum);
      return r;
    } catch (e) {
      toast({ title: 'İşlem yapılamadı', body: errMsg(e), kind: 'err', timeout: 9000 });
      return null;
    }
  },
  saveSettings() {
    const body = {};
    for (const [k, label] of PT_SET_FIELDS) {
      const v = ptNum('pt-s-' + k);
      if (Number.isNaN(v)) { toast({ title: 'Geçersiz sayı', body: label, kind: 'err' }); return; }
      if (v != null) body[k] = v;
    }
    for (const k of ['bot_start_et', 'bot_last_entry_et']) { const el = byId('pt-s-' + k); if (el && el.value) body[k] = el.value; }
    for (const k of ['eod_flatten', 'allow_short', 'hakem_enabled', 'evo_enabled', 'bot_direction_mixed', 'bot_cut10', 'bot_cull30']) { const el = byId('pt-s-' + k); if (el) body[k] = el.checked; }
    this.botSig = '';
    this.post('/api/paper/settings', body, 'Sanal işlem ayarları kaydedildi');
  },
  levels(sym) {
    const p = arr(obj(this.data).positions).find(x => x.symbol === sym);
    if (!p || !HAS_DOM) return;
    const cur = v => (v != null ? String(v).replace('.', ',') : '');
    const sp = window.prompt(`${sym} zarar durdur fiyatı ($) — boş bırakırsanız kaldırılır`, cur(p.stop_price));
    if (sp === null) return;
    const tp = window.prompt(`${sym} kâr al fiyatı ($) — boş bırakırsanız kaldırılır`, cur(p.target_price));
    if (tp === null) return;
    const n = s => { s = String(s).trim(); return s ? Number(/,/.test(s) ? s.replace(/\./g, '').replace(',', '.') : s) : null; };
    this.post('/api/paper/position', { symbol: sym, stop_price: n(sp), target_price: n(tp) }, `${sym} seviyeleri güncellendi`);
  },
};

// ============================================================ page: Sinyal Karnesi (signal scorecard)

const SC_DEFAULT = { days: 30, min_score: 0, direction: '', unit: 'first' };
const SC = Object.assign({}, SC_DEFAULT, obj(store.get('scorecard', {})));
const SC_BOT_TR = { target: 'Kâr al', stop: 'Zarar durdur', close: 'Gün sonu' };
const SC_ENTRY_TR = { intraday: 'Seans içi', open: 'Açılış' };

function scGroupLabel(g, kind) {
  if (kind === 'code') return g.label || catInfo(g.key).label;
  return g.label || g.key;
}
function scPctCell(v, d = 2) { return `<td class="${dirClass(v)}">${esc(fmtPct(v, d))}</td>`; }

function scKpis(d) {
  const o = obj(d.overall), br = obj(d.bot_rule);
  const k = (label, val, sub, cls, title) => `<div class="pt-kpi"${title ? ` title="${esc(title)}"` : ''}><div class="pt-kl">${esc(label)}</div>`
    + `<div class="pt-kv ${cls || ''}">${val}</div>${sub ? `<div class="pt-ks">${sub}</div>` : ''}</div>`;
  return k('Değerlendirilen sinyal', esc(fmtInt(o.n)), o.small ? `<span class="pill amber">az veri</span> en az ${esc(fmtInt(d.small_sample))} gerekli` : (SC.unit === 'all' ? 'tüm kayıtlar' : 'hisse başına günde ilk sinyal'))
    + k('Kazanma oranı', o.win_rate != null ? esc('%' + fmtNum(o.win_rate, 1)) : '—', 'gün sonunda sinyal yönünde kârda', o.win_rate == null ? '' : o.win_rate >= 50 ? 'up' : 'down')
    + k('Ortalama getiri', esc(fmtPct(o.avg_ret_close)), `medyan ${esc(fmtPct(o.median_ret_close))} · girişten kapanışa`, dirClass(o.avg_ret_close))
    + k('SPY\'ye göre fark', esc(fmtPct(o.avg_excess_spy)), 'piyasanın genel hareketi çıkarıldığında', dirClass(o.avg_excess_spy),
      'Sinyal getirisi eksi aynı zaman aralığında SPY getirisi (düşüş sinyallerinde ters çevrilir)')
    + k('İlk 30 / 60 dk', `${esc(fmtPct(o.avg_ret_30m))} <span class="dim">/</span> ${esc(fmtPct(o.avg_ret_60m))}`, 'girişten sonraki ortalama getiri', dirClass(o.avg_ret_60m))
    + k('En iyi / en kötü an', `<span class="up">${esc(fmtPct(o.avg_mfe))}</span> <span class="dim">/</span> <span class="down">${esc(fmtPct(o.avg_mae))}</span>`, 'gün içi ortalama en yüksek kâr / en derin zarar')
    + k('Bot kuralı', esc(fmtPct(o.bot_avg_ret)), o.bot_target_pct != null
      ? `kâr al %${esc(fmtNum(o.bot_target_pct, 0))} · zarar durdur %${esc(fmtNum(o.bot_stop_pct, 0))}${br.tp ? ` <span class="dim">(+%${esc(fmtNum(br.tp, 1))} / −%${esc(fmtNum(br.sl, 1))})</span>` : ''}`
      : '—', dirClass(o.bot_avg_ret), 'Sanal İşlem botunun çıkış kuralı (kâr al / zarar durdur / gün sonu) her sinyale uygulansaydı işlem başına ortalama getiri');
}

function scTable(rows, kind, head) {
  if (!rows.length) return stateHtml('empty', 'Veri yok.');
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">${esc(head)}</th><th>Sinyal</th><th>Kazanma</th><th>Ort. gün sonu</th><th>Medyan</th><th>30 dk</th><th>60 dk</th><th>SPY farkı</th><th>En iyi</th><th>En kötü</th><th>Kâr al</th><th>Zarar dur.</th><th>Bot ort.</th></tr></thead><tbody>`
    + rows.map(g => `<tr><td class="l">${kind === 'code' ? `<span class="cat-dot" style="--c:${esc(catInfo(g.key).color)}"></span>` : ''}${esc(scGroupLabel(g, kind))}${g.small ? ' <span class="pill sc-small" title="Az örnek: sonuç tesadüf olabilir">az veri</span>' : ''}</td>`
      + `<td>${esc(fmtInt(g.n))}</td><td class="${g.win_rate == null ? '' : g.win_rate >= 50 ? 'up' : 'down'}">${g.win_rate != null ? esc('%' + fmtNum(g.win_rate, 1)) : '—'}</td>`
      + scPctCell(g.avg_ret_close) + scPctCell(g.median_ret_close) + scPctCell(g.avg_ret_30m) + scPctCell(g.avg_ret_60m) + scPctCell(g.avg_excess_spy)
      + scPctCell(g.avg_mfe) + scPctCell(g.avg_mae)
      + `<td>${g.bot_target_pct != null ? esc('%' + fmtNum(g.bot_target_pct, 0)) : '—'}</td><td>${g.bot_stop_pct != null ? esc('%' + fmtNum(g.bot_stop_pct, 0)) : '—'}</td>`
      + scPctCell(g.bot_avg_ret) + '</tr>').join('')
    + '</tbody></table></div>';
}

function scRecent(rows) {
  if (!rows.length) return stateHtml('empty', 'Henüz değerlendirilmiş sinyal yok.');
  const dirs = { long: ['up', 'Yükseliş'], short: ['down', 'Düşüş'], mixed: ['flat', 'Karışık'] };
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sinyal zamanı</th><th class="l">Sembol</th><th>Skor</th><th class="l">Yön</th><th class="l">Gerekçe</th><th class="l">Giriş</th><th>30 dk</th><th>60 dk</th><th>Gün sonu</th><th>En iyi</th><th>En kötü</th><th>SPY</th><th class="l">Bot</th></tr></thead><tbody>'
    + rows.map(r => {
      const dr = dirs[r.direction] || dirs.mixed;
      const codes = arr(r.codes).filter(c => c !== 'watchlist').slice(0, 4).map(c => esc(obj(SC.codeLabels)[c] || catInfo(c).label)).join(', ');
      return `<tr><td class="l" title="${esc(tsTitle(r.ts))}">${esc(whenShort(r.ts))}</td>`
        + `<td class="l"><button type="button" class="tk" data-tk="${esc(r.ticker)}">${esc(r.ticker)}</button></td>`
        + `<td>${esc(fmtNum(r.score, 0))}</td><td class="l ${dr[0]}">${esc(dr[1])}</td><td class="l ell sc-codes" title="${codes}">${codes || '—'}</td>`
        + `<td class="l" title="${esc(r.entry_ts ? tsTitle(r.entry_ts) : '')}">${esc(SC_ENTRY_TR[r.entry_kind] || '—')} · ${esc(fmtPrice(r.entry_price))} $</td>`
        + scPctCell(r.ret_30m) + scPctCell(r.ret_60m) + scPctCell(r.ret_close) + scPctCell(r.mfe) + scPctCell(r.mae)
        + `<td class="${dirClass(r.spy_ret_close)}">${esc(fmtPct(r.spy_ret_close))}</td>`
        + `<td class="l"><span class="pill ${r.bot_result === 'target' ? 'ok' : r.bot_result === 'stop' ? 'amber' : ''}">${esc(SC_BOT_TR[r.bot_result] || '—')}</span> <span class="${dirClass(r.bot_ret)}">${esc(fmtPct(r.bot_ret))}</span></td></tr>`;
    }).join('')
    + '</tbody></table></div>';
}

function scEmpty(d) {
  const since = num(d.signals_since);
  return `<div class="panel">${stateHtml('empty', 'Henüz değerlendirilmiş sinyal yok.',
    `<p class="dim sm sc-p">Fırsat sinyalleri ${since ? esc(dayName(since)) + ' tarihinden beri' : 'uygulama açıkken'} kaydediliyor. Her sinyal, giriş gününün kapanışından yaklaşık 20 dakika sonra (TR ${esc(hm(closeTsTr()))} civarı) değerlendirilir. Bekleyen sinyal: <b>${esc(fmtInt(d.pending))}</b>.</p>`)}</div>`;
}
/** Today's 16:20 ET as a TR clock value (only for the empty-state hint). */
function closeTsTr() { const p = zp(nowSec(), TZ_ET); return zTs(p.y, p.mo, p.d, 16, 20, TZ_ET); }

PAGES.karne = {
  title: 'Sinyal Karnesi', poll: 120, bodyId: 'sc-body',
  mount(view) {
    this.data = null;
    view.innerHTML = '<div class="page page-score"><div class="toolbar sticky">'
      + selectHtml('days', [[7, 'Son 7 gün'], [30, 'Son 30 gün'], [60, 'Son 60 gün'], [180, 'Son 180 gün']], SC.days, '', 'Dönem')
      + selectHtml('min_score', [[0, 'Tüm skorlar'], [40, 'Skor ≥ 40'], [60, 'Skor ≥ 60'], [80, 'Skor ≥ 80']], SC.min_score, '', 'En düşük skor')
      + selectHtml('direction', [['', 'Tüm yönler'], ['long', 'Yükseliş'], ['short', 'Düşüş'], ['mixed', 'Karışık']], SC.direction, '', 'Yön')
      + selectHtml('unit', [['first', 'Hisse başına günde ilk sinyal'], ['all', 'Tüm sinyal kayıtları']], SC.unit, '', 'Sayım birimi')
      + `<span class="sp"></span><span class="dim sm" id="sc-run-info"></span><button type="button" class="btn sm" data-act="sc-run" id="sc-run">${icon('refresh')} Şimdi değerlendir</button></div>`
      + '<div class="pt-note sc-note">' + icon('warn') + '<span><b>Sinyaller sonradan ne kazandırdı?</b> Her fırsat sinyali, kaydedildiği andan itibaren gerçek 5 dakikalık fiyatlarla ölçülür: '
      + 'seans içindeki sinyal bir sonraki 5 dk barının açılışında, seans dışındaki sinyal ilk açılışta "alınmış" sayılır; getiri 30 dk, 60 dk ve kapanışta ölçülür. '
      + 'Düşüş sinyallerinde fiyatın düşmesi kazanç sayılır. Komisyon ve kayma dahil değildir. Geçmiş sonuçlar gelecekteki getiriyi garanti etmez; '
      + 'bir gerekçe hakkında karar vermek için en az 20–30 sinyal gerekir.</span></div>'
      + `<div id="page-err"></div><div id="sc-body">${stateHtml('loading')}</div></div>`;
  },
  onFilter(f, v, type) {
    if (type !== 'change' || !(f in SC_DEFAULT)) return;
    SC[f] = (f === 'days' || f === 'min_score') ? Number(v) : String(v);
    store.set('scorecard', { days: SC.days, min_score: SC.min_score, direction: SC.direction, unit: SC.unit });
    refreshPage(false);
  },
  async refresh(first, live) {
    const d = await api('/api/scorecard', { params: { days: SC.days, min_score: SC.min_score || '', direction: SC.direction, unit: SC.unit } });
    if (!live()) return;
    this.data = d;
    this.render();
  },
  render() {
    const d = this.data;
    if (!d) return;
    // Category labels come from /api/meta; render again once it has arrived.
    if (!S.metaLoaded) setTimeout(() => { if (cur && cur.page === this) this.render(); }, 1500);
    SC.codeLabels = {};
    for (const g of arr(d.by_code)) if (g.label) SC.codeLabels[g.key] = g.label;
    const lr = obj(d.last_result);
    setText('sc-run-info', d.running ? 'değerlendiriliyor…' : d.last_run ? `son değerlendirme ${rel(d.last_run)}${lr.evaluated ? ' · ' + fmtInt(lr.evaluated) + ' yeni' : ''} · bekleyen ${fmtInt(d.pending)}` : `bekleyen ${fmtInt(d.pending)}`);
    if (!num(obj(d.overall).n)) { setHtml('sc-body', scEmpty(d)); return; }
    setHtml('sc-body', `<div class="pt-kpis">${scKpis(d)}</div>`
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Skora göre</h3><span class="upd">yüksek skor gerçekten daha mı iyi?</span></div>${scTable(arr(d.by_band), 'band', 'Skor aralığı')}</section>`
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Gerekçeye göre</h3><span class="upd">hangi haber türü / koşul işe yarıyor?</span></div>${scTable(arr(d.by_code), 'code', 'Gerekçe')}</section>`
      + `<div class="dash-2"><section class="panel"><div class="ph-row"><h3 class="ph">Yöne göre</h3></div>${scTable(arr(d.by_direction), 'dir', 'Yön')}</section>`
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Giriş zamanına göre</h3></div>${scTable(arr(d.by_entry), 'entry', 'Giriş')}</section></div>`
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Son değerlendirilen sinyaller</h3><span class="upd">en fazla 150</span></div>${scRecent(arr(d.recent))}</section>`);
  },
  async run() {
    const b = byId('sc-run');
    if (b) { b.disabled = true; b.classList.add('busy'); }
    try {
      const r = await api('/api/scorecard/run', { method: 'POST', body: {}, timeout: 180000 });
      const x = obj(r.result);
      if (x.skipped) toast({ title: 'Değerlendirme zaten çalışıyor', kind: 'info' });
      else toast({ title: `Sinyal karnesi güncellendi: ${fmtInt(x.evaluated)} yeni sonuç`, body: x.waiting ? `${fmtInt(x.waiting)} sinyal, giriş gününün kapanışını bekliyor` : '', kind: 'ok' });
      refreshPage(false);
    } catch (e) {
      toast({ title: 'Değerlendirme yapılamadı', body: errMsg(e), kind: 'err' });
    } finally { if (b) { b.disabled = false; b.classList.remove('busy'); } }
  },
};

// ============================================================ page: Test Laboratuvarı (5-year data, four bots, referee)

const LAB_STATE_TR = { idle: 'Bekliyor', running: 'Çalışıyor', done: 'Tamamlandı', error: 'Hata', stopped: 'Durduruldu' };
const LAB_STEP_ORDER = ['universe', 'bars', 'earnings', 'filings', 'news'];
const LAB_CURVES = [['model', 'Hakem: öğrenen model', 'c1'], ['weighted', 'Hakem: ağırlıklı oy', 'c2'], ['majority', 'Basit oy çokluğu', 'c3'], ['spy', 'SPY al-tut', 'c4']];

function labPct(v, d = 2) { return `<span class="${dirClass(v)}">${esc(fmtPct(v, d))}</span>`; }
function labDate(ts) { return num(ts) ? dmy(ts) : '—'; }

function labData(d) {
  const c = obj(d.counts), dl = obj(d.download), steps = obj(dl.steps), labels = obj(dl.labels);
  const kv = (l, v, sub) => `<div class="lab-kv"><span class="muted sm">${esc(l)}</span><b>${v}</b>${sub ? `<span class="dim sm">${sub}</span>` : ''}</div>`;
  let h = '<div class="lab-kvs">'
    + kv('Hisse', esc(fmtInt(c.universe)), 'piyasa değeri ve işlem hacmine göre')
    + kv('Günlük fiyat', esc(fmtInt(c.bars)), c.bars_from ? `${esc(c.bars_from)} – ${esc(c.bars_to)}` : '')
    + kv('Bilanço kaydı', esc(fmtInt(c.earnings)), c.earnings_from ? `${esc(c.earnings_from)} – ${esc(c.earnings_to)}` : '')
    + kv('SEC bildirimi', esc(fmtInt(c.filings)), c.filings_from ? `${esc(labDate(c.filings_from))} – ${esc(labDate(c.filings_to))}` : '')
    + kv('Haber başlığı', esc(fmtInt(c.news)), c.news_from ? `${esc(labDate(c.news_from))} – ${esc(labDate(c.news_to))} · ${esc(fmtInt(c.news_symbols))} hisse` : 'Finnhub (≈ son 12 ay)')
    + kv('Veri dosyası', c.size_mb != null ? esc(fmtNum(c.size_mb, 1)) + ' MB' : '—', 'lab.db')
    + '</div>';
  const keys = [...LAB_STEP_ORDER.filter(k => steps[k]), ...Object.keys(steps).filter(k => !LAB_STEP_ORDER.includes(k))];
  if (keys.length) {
    h += '<div class="lab-steps">' + keys.map(k => {
      const st = obj(steps[k]), tot = num(st.total) || 0, dn = num(st.done) || 0;
      const pct = tot ? Math.min(100, dn / tot * 100) : (st.state === 'done' ? 100 : 0);
      const cls = st.state === 'done' ? 'ok' : st.state === 'error' ? 'amber' : '';
      return `<div class="lab-step"><span class="lab-sl">${esc(labels[k] || k)}</span><div class="lab-bar"><i style="width:${pct.toFixed(1)}%"></i></div>`
        + `<span class="dim sm lab-sn">${tot ? esc(fmtNum(pct, 0)) + '%' : ''}</span><span class="pill ${cls}">${esc(LAB_STATE_TR[st.state] || st.state || '')}</span>`
        + `<span class="dim sm lab-sm" title="${esc(st.message || '')}">${esc(st.message || '')}</span></div>`;
    }).join('') + '</div>';
  }
  const state = dl.state === 'idle' && obj(dl.last).state ? obj(dl.last).state : dl.state;
  h += `<div class="lab-dlstate"><span class="pill ${state === 'done' ? 'ok' : state === 'error' ? 'amber' : ''}">${esc(LAB_STATE_TR[state] || state || '—')}</span>`
    + `<span class="muted sm">${esc(dl.message || obj(dl.last).message || (d.last_download ? 'Son tam indirme: ' + whenShort(d.last_download) : 'Henüz indirilmedi'))}</span>`
    + (d.last_update ? `<span class="dim sm">· son günlük güncelleme ${esc(whenShort(d.last_update))}</span>` : '') + '</div>';
  const errs = arr(dl.errors);
  if (errs.length) h += `<details class="lab-errs"><summary class="dim sm">${esc(fmtInt(errs.length))} uyarı</summary>${errs.slice(-12).map(e => `<div class="dim sm">${esc(e)}</div>`).join('')}</details>`;
  return h;
}

function labButtons(d) {
  if (d.download_running) return `<button type="button" class="btn sm danger" data-act="lab-stop">Durdur</button>`;
  return (d.last_download ? `<button type="button" class="btn sm" data-act="lab-update" title="Son haftaların fiyat, bilanço, SEC ve haber verisini ekler">${icon('refresh')} Son günleri güncelle</button>` : '')
    + `<button type="button" class="btn sm${d.last_download ? '' : ' primary'}" data-act="lab-download">${icon('download')} ${d.last_download ? 'Tamamını yeniden kontrol et' : '5 yıllık veriyi indir'}</button>`;
}

function labTestPanel(d) {
  const t = obj(d.test), p = obj(obj(d.report).params);
  const val = (k, def) => esc(String(p[k] != null ? p[k] : def).replace('.', ','));
  const busy = d.test_running, dl = d.download_running && !obj(d.download).update;
  return `<div class="pt-row"><label class="pt-f"><span>Zarar durdur (%)</span><input class="inp pt-num" id="lab-sl" inputmode="decimal" value="${val('sl_pct', 2)}"></label>`
    + `<label class="pt-f"><span>Kâr al (%)</span><input class="inp pt-num" id="lab-tp" inputmode="decimal" value="${val('tp_pct', 4)}"></label>`
    + `<label class="pt-f"><span>Kayma (baz puan / taraf)</span><input class="inp pt-num" id="lab-slip" inputmode="decimal" value="${val('slippage_bps', 5)}"></label>`
    + `<label class="pt-f"><span>Test dönemi (eğitime girmez)</span>${selectHtml('labTm', [[2, 'Son 2 ay (önerilen)'], [1, 'Son 1 ay'], [3, 'Son 3 ay'], [6, 'Son 6 ay'], [12, 'Son 12 ay']], p.test_months || 2, '', 'Test dönemi')}</label>`
    + `<button type="button" class="btn primary" data-act="lab-test"${busy || dl ? ' disabled' : ''}>${icon('play')} Testi çalıştır</button></div>`
    + `<div class="muted sm">${busy ? `<span class="pill">Çalışıyor</span> ${esc(t.message || '')}` : dl ? 'Veri indirme bitince test çalıştırılabilir.'
      : t.state === 'error' ? `<span class="down">${esc(t.message)}</span>` : t.state === 'done' ? esc(t.message) : 'Her işlem açılışta girer; zarar durdur, kâr al ya da kapanışta çıkar. Test birkaç dakika sürebilir.'}</div>`;
}

function labVerdict(rep, wfMode) {
  const src = wfMode ? obj(rep.walk_forward) : rep;
  const per = wfMode ? 'son 12 ay' : (rep.test_label || 'son 12 ay');
  const m = obj(obj(src.methods).model), w = obj(obj(src.methods).weighted), spy = obj(obj(src.spy).stats);
  const hk = Object.keys(m).length ? m : w;
  const name = Object.keys(m).length ? 'Öğrenen hakem' : 'Ağırlıklı oy hakemi';
  const t = obj(hk.test), ps = obj(obj(hk.portfolio).stats);
  let cls = 'amber', txt;
  const pre = wfMode ? 'Kayan pencere (her 2 ay, yalnızca öncesiyle eğitilmiş hakem): ' : '';
  if (!num(t.n)) txt = `${name}, görmediği ${per}da hiç işlem açmadı: eğitim döneminde kârlı bir kural bulamadı.`;
  else if (t.n < 30) txt = `${name} ${per}da yalnızca ${fmtInt(t.n)} işlem yaptı; sonuç güvenilir değil.`;
  else if (num(t.avg_pct) <= 0) txt = `${name}, görmediği ${per}da işlem başına ${fmtPct(t.avg_pct)} ortalama ile zarar etti. Bu haliyle gerçek para için uygun değil; sanal işlemde izleyip kuralları geliştirmek gerekir.`;
  else if (num(ps.total_pct) < num(spy.total_pct)) { cls = ''; txt = `${name} ${per}da işlem başına kârda (${fmtPct(t.avg_pct)}; portföy ${fmtPct(ps.total_pct)}) ama aynı dönemde SPY'yi elde tutmak daha çok kazandırdı (${fmtPct(spy.total_pct)}).`; }
  else { cls = 'ok'; txt = `${name} ${per}da SPY'den iyi sonuç verdi: portföy ${fmtPct(ps.total_pct)}, SPY ${fmtPct(spy.total_pct)}. Umut verici ama kısa bir dönem; önce sanal işlemde doğrulayın.`; }
  return `<div class="lab-verdict ${cls}">${icon(cls === 'ok' ? 'check' : 'warn')}<span>${esc(pre + txt)}</span></div>`;
}

function labMethods(rep) {
  const M = obj(rep.methods);
  const order = ['momentum', 'meanrev', 'gap', 'event', 'majority', 'weighted', 'model'].filter(k => M[k]);
  const cell = m => `<td>${esc(fmtInt(m.n || 0))}</td><td>${m.win_rate != null ? esc('%' + fmtNum(m.win_rate, 1)) : '—'}</td><td>${labPct(m.avg_pct, 3)}</td>`;
  const row = (k, m) => {
    const tr = obj(m.train), te = obj(m.test), ps = obj(obj(m.portfolio).stats);
    const hk = k === 'weighted' || k === 'model';
    return `<tr class="${hk ? 'lab-hk' : ''}"><td class="l">${esc(m.label || k)}</td>${cell(tr)}${cell(te)}<td>${te.profit_factor != null ? esc(fmtNum(te.profit_factor, 2)) : '—'}</td>`
      + `<td>${labPct(ps.total_pct, 1)}</td><td class="down">${esc(fmtPct(ps.max_dd_pct, 1))}</td><td>${ps.sharpe != null ? esc(fmtNum(ps.sharpe, 2)) : '—'}</td><td>${esc(fmtInt(ps.trades))}</td></tr>`;
  };
  const spy = obj(obj(rep.spy).stats);
  const per = esc(rep.test_label || 'son 12 ay');
  return `<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l" rowspan="2">Yöntem</th><th colspan="3">Eğitim dönemi</th><th colspan="4">Test: görülmemiş ${per}</th><th colspan="4">Portföy testi (${per})</th></tr>`
    + '<tr><th>İşlem</th><th>Kazanma</th><th>Ort.</th><th>İşlem</th><th>Kazanma</th><th>Ort.</th><th>Kâr fakt.</th><th>Toplam</th><th>En büyük düşüş</th><th>Sharpe</th><th>İşlem</th></tr></thead><tbody>'
    + order.map(k => row(k, M[k])).join('')
    + `<tr class="lab-spy"><td class="l">SPY'yi elde tutmak</td><td colspan="7" class="dim c">karşılaştırma</td><td>${labPct(spy.total_pct, 1)}</td><td class="down">${esc(fmtPct(spy.max_dd_pct, 1))}</td><td>${spy.sharpe != null ? esc(fmtNum(spy.sharpe, 2)) : '—'}</td><td>—</td></tr>`
    + '</tbody></table></div><p class="dim sm pt-hint">Ort. = işlem başına net getiri (kayma dahil). Portföy testi: günde en fazla 5 işlem, her biri hesabın %20\'si. Eğitim dönemi sonuçları "geçmişe uyum" içerir; asıl ölçü test sütunlarıdır.</p>';
}

function labChart(rep) {
  const M = obj(rep.methods);
  const series = LAB_CURVES.map(([k, l, c]) => [l, c, arr(obj(k === 'spy' ? rep.spy : obj(M[k]).portfolio).curve)]).filter(s => s[2].length > 1);
  if (!series.length) return '';
  const W = 640, H = 220, P = 8;
  const vals = series.flatMap(s => s[2].map(p => p[1]));
  const lo = Math.min(...vals, 1), hi = Math.max(...vals, 1), rng = hi - lo || 1;
  const n = Math.max(...series.map(s => s[2].length));
  const y = v => P + (H - 2 * P) * (1 - (v - lo) / rng);
  const lines = series.map(([l, c, pts]) => `<polyline class="lab-l ${c}" points="${pts.map((p, i) => `${(i / (n - 1) * W).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" vector-effect="non-scaling-stroke"/>`).join('');
  const base = `<line class="lab-base" x1="0" x2="${W}" y1="${y(1).toFixed(1)}" y2="${y(1).toFixed(1)}" vector-effect="non-scaling-stroke"/>`;
  const first = series[0][2];
  return `<svg class="lab-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${base}${lines}</svg>`
    + `<div class="pt-chart-ax dim sm"><span>${esc(first[0][0])}</span><span>başlangıç = 1,00 · en düşük ${esc(fmtNum(lo, 2))} · en yüksek ${esc(fmtNum(hi, 2))}</span><span>${esc(first[first.length - 1][0])}</span></div>`
    + `<div class="lab-legend">${series.map(([l, c, pts]) => `<span><i class="${c}"></i>${esc(l)} <b class="${dirClass(pts[pts.length - 1][1] - 1)}">${esc(fmtPct((pts[pts.length - 1][1] - 1) * 100, 1))}</b></span>`).join('')}</div>`;
}

function labWalk(rep) {
  const wf = obj(rep.walk_forward);
  if (!arr(wf.blocks).length) return '';
  const M = obj(wf.methods), spy = obj(obj(wf.spy).stats);
  const order = ['majority', 'weighted', 'model'].filter(k => M[k]);
  const head = '<tr><th class="l">Yöntem</th><th>İşlem</th><th>Kazanma</th><th>Ort.</th><th>Kâr fakt.</th><th>Portföy</th><th>En büyük düşüş</th><th>Sharpe</th></tr>';
  const row = k => { const t = obj(M[k].test), ps = obj(obj(M[k].portfolio).stats);
    return `<tr class="${k !== 'majority' ? 'lab-hk' : ''}"><td class="l">${esc(M[k].label || k)}</td><td>${esc(fmtInt(t.n || 0))}</td><td>${t.win_rate != null ? esc('%' + fmtNum(t.win_rate, 1)) : '—'}</td>`
      + `<td>${labPct(t.avg_pct, 3)}</td><td>${t.profit_factor != null ? esc(fmtNum(t.profit_factor, 2)) : '—'}</td><td>${labPct(ps.total_pct, 1)}</td><td class="down">${esc(fmtPct(ps.max_dd_pct, 1))}</td><td>${ps.sharpe != null ? esc(fmtNum(ps.sharpe, 2)) : '—'}</td></tr>`; };
  const blocks = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Pencere</th><th>Oy çokluğu</th><th>Ağırlıklı oy</th><th>Öğrenen model</th></tr></thead><tbody>'
    + arr(wf.blocks).map(b => { const c = m => { m = obj(m); return m.n ? `${labPct(m.avg_pct, 3)} <span class="dim sm">(${esc(fmtInt(m.n))})</span>` : '<span class="dim">işlem yok</span>'; };
      return `<tr><td class="l">${esc(b.from)} – ${esc(b.to)}</td><td>${c(b.majority)}</td><td>${c(b.weighted)}</td><td>${c(b.model)}</td></tr>`; }).join('')
    + '</tbody></table></div>';
  return `<section class="panel"><div class="ph-row"><h3 class="ph">Kayan pencere kontrolü: son 12 ay</h3><span class="upd">her ${esc(fmtInt(wf.step_months))} aylık pencere, yalnızca kendinden önceki veriyle eğitilmiş hakemle</span></div>`
    + labVerdict(rep, true)
    + `<div class="tbl-wrap"><table class="tbl lab-tbl"><thead>${head}</thead><tbody>${order.map(row).join('')}`
    + `<tr class="lab-spy"><td class="l">SPY'yi elde tutmak</td><td colspan="4" class="dim c">karşılaştırma</td><td>${labPct(spy.total_pct, 1)}</td><td class="down">${esc(fmtPct(spy.max_dd_pct, 1))}</td><td>${spy.sharpe != null ? esc(fmtNum(spy.sharpe, 2)) : '—'}</td></tr></tbody></table></div>`
    + '<div class="sub">Pencere pencere işlem başına ortalama (işlem sayısı)</div>' + blocks
    + '<p class="dim sm pt-hint">Tek bir 2 aylık test şansa çok açıktır. Bu tablo aynı yöntemi son 12 ayın 6 ayrı penceresinde, her seferinde yalnızca o pencereden önceki veriyle eğitip dener; Finnhub başlıklarının olduğu aylar da böylece eğitime girer.</p></section>';
}

function labYears(rep) {
  const M = obj(rep.methods), bots = ['momentum', 'meanrev', 'gap', 'event'].filter(k => M[k]);
  const years = [...new Set(bots.flatMap(k => Object.keys(obj(M[k].years))))].sort();
  if (!years.length) return '';
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Bot</th>' + years.map(y => `<th>${esc(y)}</th>`).join('') + '</tr></thead><tbody>'
    + bots.map(k => `<tr><td class="l">${esc(M[k].label || k)}</td>` + years.map(y => {
      const m = obj(obj(M[k].years)[y]);
      return m.n ? `<td title="${esc(fmtInt(m.n))} işlem, kazanma %${esc(fmtNum(m.win_rate, 1))}">${labPct(m.avg_pct, 3)} <span class="dim sm">(${esc(fmtInt(m.n))})</span></td>` : '<td class="dim">—</td>';
    }).join('') + '</tr>').join('') + '</tbody></table></div>';
}

function labModel(rep) {
  const m = obj(obj(rep.methods).model), w = obj(obj(rep.methods).weighted);
  const imp = arr(m.importance).slice(0, 12);
  const mx = Math.max(0.0001, ...imp.map(x => Math.abs(num(x.coef) || 0)));
  const weights = obj(rep.live_weights || w.weights);
  const wb = Object.entries(weights).map(([k, v]) => `<span class="pill${v > 0 ? ' ok' : ''}">${esc(obj(rep.bots)[k] || k)}: %${esc(fmtNum(v * 100, 0))}</span>`).join(' ');
  return `<div class="dash-2 lab-model"><section class="panel"><div class="ph-row"><h3 class="ph">Öğrenen hakem neye bakıyor?</h3>`
    + (m.threshold != null ? `<span class="upd">işlem eşiği: olasılık ≥ %${esc(fmtNum(m.threshold * 100, 0))}</span>` : '') + '</div>'
    + (imp.length ? '<div class="lab-imp">' + imp.map(x => {
      const c = num(x.coef) || 0, wpx = Math.abs(c) / mx * 50;
      return `<div class="lab-ir"><span class="lab-il">${esc(x.label)}</span><span class="lab-ib"><i class="${c >= 0 ? 'pos' : 'neg'}" style="width:${wpx.toFixed(1)}%;${c >= 0 ? 'left:50%' : `right:50%`}"></i></span><span class="dim sm">${esc(fmtSigned(c, 2))}</span></div>`;
    }).join('') + '</div><p class="dim sm pt-hint">Yükseliş işlemi modelinin katsayıları (standartlaştırılmış). Yeşil: bu özellik arttıkça kazanma olasılığı artıyor; kırmızı: azalıyor. Düşüş işlemleri için ayrı bir model aynı şekilde eğitilir.'
      + (m.test_against_bots ? ` Test yılında hakem ${esc(fmtInt(m.test_against_bots))} kez botların çoğunluğunun tersine işlem açtı.` : '') + '</p>'
      : stateHtml('empty', 'Öğrenen model eğitilemedi (yeterli veri yok ya da scikit-learn kurulu değil).'))
    + `</section><section class="panel"><div class="ph-row"><h3 class="ph">Ağırlıklı oy: botlara güven</h3></div><div class="lab-w">${wb || '—'}</div>`
    + '<p class="dim sm pt-hint">Her botun ağırlığı, geçmiş yıllardaki işlem başına net getirisine göre belirlenir (az örnek küçültülür). Zarar ettiren botun ağırlığı sıfırdır; hiçbir bot kârlı değilse ağırlıklı oy işlem açmaz.</p></section></div>';
}

function labReport(rep) {
  if (!rep) return '<section class="panel">' + stateHtml('empty', 'Henüz test çalıştırılmadı.', '<p class="dim sm sc-p">Önce 5 yıllık veriyi indirin, sonra "Testi çalıştır" düğmesine basın.</p>') + '</section>';
  const tr = arr(rep.train_range), te = arr(rep.test_range);
  return `<section class="panel"><div class="ph-row"><h3 class="ph">3 · Sonuçlar</h3><span class="upd">${esc(whenShort(rep.created_at))} · ${esc(fmtInt(rep.symbols))} hisse · ${esc(fmtInt(rep.rows))} bot sinyali · ${esc(fmtNum(rep.seconds, 0))} sn</span></div>`
    + `<p class="muted sm lab-ranges">Eğitim: <b>${esc(tr[0] || '')} – ${esc(tr[1] || '')}</b> · Test (hiç görülmemiş): <b>${esc(te[0] || '')} – ${esc(te[1] || '')}</b> · haber başlığı olan sinyal: ${esc(fmtInt(rep.news_rows))}</p>`
    + labVerdict(rep) + labMethods(rep) + '</section>'
    + labWalk(rep)
    + `<section class="panel"><div class="ph-row"><h3 class="ph">Test döneminde hesap değeri</h3><span class="upd">${esc(rep.test_label || '')} · 1,00 ile başlayan sanal hesap</span></div>${labChart(rep)}</section>`
    + labModel(rep)
    + `<section class="panel"><div class="ph-row"><h3 class="ph">Botların yıllara göre sonucu</h3><span class="upd">işlem başına ortalama net getiri (işlem sayısı)</span></div>${labYears(rep)}</section>`;
}

function labSweep(d) {
  const sw = obj(d.sweep), rep = d.sweep_report;
  const running = d.sweep_running;
  let h = `<div class="pt-row"><button type="button" class="btn primary" data-act="lab-sweep"${running || d.test_running || d.download_running ? ' disabled' : ''}>${icon('play')} Toplu denemeyi çalıştır</button>`
    + `<span class="muted sm">${running ? `<span class="pill">Çalışıyor</span> ${esc(sw.message || '')}${num(sw.total) ? ` (${esc(fmtInt(sw.done))}/${esc(fmtInt(sw.total))})` : ''}`
      : sw.state === 'error' ? `<span class="down">${esc(sw.message)}</span>` : sw.state === 'done' ? esc(sw.message)
      : '8 çıkış kuralı × 3 tutma süresi = 24 ayar; her biri test dönemi ve son 12 ay kayan pencereyle denenir (~5–10 dk).'}</span></div>`;
  if (!rep) return h;
  const rows = arr(rep.rows);
  const m = x => { x = obj(x); return x.n ? `${labPct(x.avg_pct, 3)} <span class="dim sm">(${esc(fmtInt(x.n))})</span>` : '<span class="dim">işlem yok</span>'; };
  const bestBot = r => {
    let best = null;
    for (const [k, v] of Object.entries(obj(r.bots))) { const t = obj(v.test); if (t.n >= 30 && (!best || t.avg_pct > best[1].avg_pct)) best = [k, t]; }
    return best ? `${esc(obj(rep.bots)[best[0]] || best[0])}: ${labPct(best[1].avg_pct, 3)}` : '—';
  };
  h += `<p class="muted sm lab-ranges">${esc(whenShort(rep.created_at))} · test ${esc(rep.test_label || '')} (${esc(arr(rep.test_range)[0] || '')} – ${esc(arr(rep.test_range)[1] || '')}) · kayan pencere ${esc(arr(rep.wf_range)[0] || '')} – ${esc(arr(rep.wf_range)[1] || '')} · <b>${esc(fmtInt(rep.consistent))} / ${esc(fmtInt(rep.configs))}</b> ayar tutarlı</p>`
    + '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Çıkış kuralı</th><th>Süre</th><th>Model: eğitim</th><th>Model: test</th><th>Model: kayan pencere</th><th>Pozitif pencere</th><th>Hepsini al (kayan p.)</th><th>Oy çokluğu (test)</th><th class="l">En iyi bot (test)</th><th class="l">Sonuç</th></tr></thead><tbody>'
    + rows.map(r => `<tr class="${r.consistent ? 'lab-ok' : ''}"><td class="l">${esc(r.label)}</td><td>${esc(fmtInt(r.hold))} gün</td>`
      + `<td>${m(obj(r.model).train)}</td><td>${m(obj(r.model).test)}</td><td>${m(obj(r.wf).model)}</td>`
      + `<td title="${esc(arr(obj(r.wf).window_avgs).map(v => v == null ? '—' : fmtPct(v, 3)).join(' · '))}">${esc(fmtInt(obj(r.wf).windows_positive))} / ${esc(fmtInt(obj(r.wf).windows))}</td>`
      + `<td>${m(obj(r.baseline).wf)}</td><td>${m(obj(r.majority).test)}</td><td class="l">${bestBot(r)}</td>`
      + `<td class="l">${r.consistent ? '<span class="pill ok">Tutarlı</span>' : '<span class="pill">Değil</span>'}</td></tr>`).join('')
    + '</tbody></table></div>'
    + '<p class="dim sm pt-hint">"Tutarlı" = öğrenen model eğitimde, son 12 ayın toplamında ve 6 pencerenin en az 4\'ünde kârda VE botların işaretlediği her hisseyi körlemesine almaktan ("Hepsini al") daha iyi. 24 ayar arasından sonradan en iyisini seçmek sonucu olduğundan iyi gösterir; bu yüzden tek bir parlak satıra değil, tutarlılığa bakın. Uzun tutma süreleri yükselen piyasada kendiliğinden kazandırır; "Hepsini al" sütunu bunu gösterir.</p>';
  return h;
}

function labEvoChart(stages) {
  const hist = [], cuts = [];
  for (const s of arr(stages)) { if (hist.length) cuts.push(hist.length); for (const h of arr(s.history)) hist.push(h); }
  if (hist.length < 2) return '';
  const W = 640, H = 150, P = 6;
  const vals = hist.flatMap(h => [num(h.best), num(h.val_best)]).filter(v => v != null && v > -9);
  if (!vals.length) return '';
  const lo = Math.min(0, ...vals), hi = Math.max(...vals), rng = hi - lo || 1;
  const x = i => (i / (hist.length - 1) * W).toFixed(1), y = v => (P + (H - 2 * P) * (1 - (v - lo) / rng)).toFixed(1);
  const line = (k, c) => `<polyline class="lab-l ${c}" points="${hist.map((h, i) => `${x(i)},${y(Math.max(lo, num(h[k]) || lo))}`).join(' ')}" vector-effect="non-scaling-stroke"/>`;
  const sep = cuts.map(i => `<line class="lab-base" x1="${x(i)}" x2="${x(i)}" y1="0" y2="${H}" vector-effect="non-scaling-stroke"/>`).join('');
  return `<svg class="lab-chart evo-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${sep}<line class="lab-base" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" vector-effect="non-scaling-stroke"/>${line('best', 'c1')}${line('val_best', 'c3')}</svg>`
    + `<div class="lab-legend"><span><i class="c1"></i>Eğitimde en iyi uygunluk</span><span><i class="c3"></i>Doğrulamada en iyi (t)</span><span class="dim">dikey çizgiler: aşama geçişleri · toplam ${esc(fmtInt(hist.length))} nesil</span></div>`;
}

function evoCell(x, detail) {
  x = obj(x);
  if (!x.n) return '<span class="dim">işlem yok</span>';
  let h = `${labPct(x.avg_pct, 2)} <span class="dim sm">net · piyasa üstü</span> ${labPct(x.daily_excess_pct, 2)}`;
  if (detail) h += `<div class="dim sm">seçim ${esc(fmtPct(x.pick_pct, 2))} · zamanlama ${esc(fmtPct(x.timing_pct, 2))} · t ${esc(fmtNum(x.t, 1))}</div>`;
  return h + `<div class="dim sm">${esc(fmtInt(x.n))} işlem · ${esc(fmtInt(x.days))} gün</div>`;
}

function evoRule(e) {
  const r = obj(e.rule);
  return `<b class="${obj(e.genome).side > 0 ? 'up' : 'down'}">${esc(r.side)}</b> · ${esc(r.entry)}<div class="dim sm">${esc(r.exit)} · ${esc(fmtInt(r.hold))} gün${e.fitness != null ? ' · uygunluk ' + esc(fmtNum(e.fitness, 1)) : ''}</div>`;
}

function evoPortChart(p) {
  const series = [['bots', 'Evrim botları (hakem sıralaması)', 'c1'], ['dip', 'Basit "düşeni al"', 'c3'], ['spy', "SPY'yi elde tutmak", 'c4']]
    .map(([k, l, c]) => [l, c, arr(obj(p[k]).curve)]).filter(x => x[2].length > 1);
  if (!series.length) return '';
  const W = 640, H = 200, P = 6;
  const vals = series.flatMap(x => x[2].map(q => q[1]));
  const lo = Math.min(...vals), hi = Math.max(...vals), rng = hi - lo || 1;
  const n = Math.max(...series.map(x => x[2].length));
  const y = v => (P + (H - 2 * P) * (1 - (v - lo) / rng)).toFixed(1);
  const lines = series.map(([l, c, pts]) => `<polyline class="lab-l ${c}" points="${pts.map((q, i) => `${(i / (n - 1) * W).toFixed(1)},${y(q[1])}`).join(' ')}" vector-effect="non-scaling-stroke"/>`).join('');
  const first = series[0][2];
  return `<svg class="lab-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><line class="lab-base" x1="0" x2="${W}" y1="${y(1)}" y2="${y(1)}" vector-effect="non-scaling-stroke"/>${lines}</svg>`
    + `<div class="pt-chart-ax dim sm"><span>${esc(first[0][0])}</span><span>başlangıç = 1,00</span><span>${esc(first[first.length - 1][0])}</span></div>`
    + `<div class="lab-legend">${series.map(([l, c, pts]) => `<span><i class="${c}"></i>${esc(l)} <b class="${dirClass(pts[pts.length - 1][1] - 1)}">${esc(fmtPct((pts[pts.length - 1][1] - 1) * 100, 1))}</b></span>`).join('')}</div>`;
}

function evoPortfolios(rep) {
  const P = obj(rep.portfolios), set = obj(rep.portfolio_settings);
  const keys = ['unseen_stocks', 'lock', 'all_seen'].filter(k => P[k]);
  if (!keys.length) return '';
  const row = (label, x, strong) => { x = obj(obj(x).stats);
    return `<tr class="${strong ? 'lab-hk' : ''}"><td class="l">${esc(label)}</td><td>${labPct(x.total_pct, 1)}</td><td>${labPct(x.cagr_pct, 1)}</td><td class="down">${esc(fmtPct(x.max_dd_pct, 1))}</td>`
      + `<td>${x.sharpe != null ? esc(fmtNum(x.sharpe, 2)) : '—'}</td><td>${x.trades != null ? esc(fmtInt(x.trades)) : '—'}</td><td>${x.win_rate != null ? esc('%' + fmtNum(x.win_rate, 1)) : '—'}</td><td>${x.exposure_pct != null ? esc('%' + fmtNum(x.exposure_pct, 0)) : '—'}</td></tr>`; };
  return `<div class="sub">Portföy simülasyonu: günde en fazla ${esc(fmtInt(set.max_new))} yeni işlem, her biri hesabın %${esc(fmtNum((set.pos_frac || 0) * 100, 0))}'i, en fazla ${esc(fmtInt(set.max_pos))} açık pozisyon, her gün kapanışta değerleme</div>`
    + keys.map(k => `<div class="evo-port"><div class="muted sm"><b>${esc(P[k].label || k)}</b></div><div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Yöntem</th><th>Toplam</th><th>Yıllık</th><th>En büyük düşüş</th><th>Sharpe</th><th>İşlem</th><th>Kazanma</th><th>Yatırımda</th></tr></thead><tbody>`
      + row('Evrim botları (hakem sıralaması)', P[k].bots, true) + row('Basit "düşeni al" (aynı sınırlarla)', P[k].dip) + row("SPY'yi elde tutmak", P[k].spy)
      + `</tbody></table></div>${k === 'unseen_stocks' ? evoPortChart(P[k]) : ''}</div>`).join('')
    + '<p class="dim sm pt-hint">Görülmemiş hisseler: son aşamanın sınav grubu, botlar bu hisseleri seçim sırasında hiç görmedi. Kilitli kasa: son 6 ay, hiç görülmedi. "Eğitim dönemi" satırı botların gördüğü veridir ve iyimserdir. Yatırımda = hesabın ortalama ne kadarının pozisyonda olduğu (korku günü stratejisi çoğu zaman nakitte bekler).</p>';
}

function evoStability(st) {
  st = obj(st);
  const runs = arr(st.runs);
  if (runs.length < 2) return '';
  return '<div class="sub">Tekrar kontrolü: farklı rastgele başlangıçlarla yapılan turnuvalar</div><div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Turnuva</th><th>Final bot</th><th class="l">Baskın kural ailesi</th><th class="l">En sık göstergeler</th><th>Sınav (günlük net)</th><th>Görülmemiş hisse portföyü (yıllık)</th><th>Kilitli kasa (net / işlem)</th></tr></thead><tbody>'
    + runs.map(r => { const fam = Object.entries(obj(r.families)).sort((a, b) => b[1] - a[1]);
      return `<tr><td class="l">${esc(whenShort(r.created_at))} <span class="dim sm">tohum ${esc(String(r.seed ?? '—'))}</span></td><td>${esc(fmtInt(r.finals))}</td>`
        + `<td class="l">${fam.length ? esc(fam[0][0]) + ` <span class="dim sm">(${esc(fmtInt(fam[0][1]))}/${esc(fmtInt(r.finals))})</span>` : '—'}</td>`
        + `<td class="l dim sm">${esc(Object.keys(obj(r.features)).slice(0, 5).join(', '))}</td><td>${labPct(r.exam_daily_pct, 2)}</td>`
        + `<td>${labPct(obj(r.portfolio).cagr_pct, 1)}</td><td>${labPct(obj(r.lock).avg_pct, 2)}</td></tr>`; }).join('')
    + '</tbody></table></div>';
}

function evoToday(t) {
  t = obj(t);
  if (!t.candidates) return '<p class="muted sm">Bugünün sinyallerini görmek için düğmeye basın (açılış ya da piyasa öncesi fiyatları gerekir; ~2.600 hisse için 2–3 dk sürer).</p>';
  const rows = arr(t.candidates);
  return `<p class="muted sm">${esc(whenShort(t.ts))} · VIX (dünkü kapanış) ${t.vix != null ? esc(fmtNum(t.vix, 1)) : '—'} · piyasa genişliği %${t.breadth50 != null ? esc(fmtNum(t.breadth50 * 100, 0)) : '—'} · ${t.regime_tr ? `rejim: <b>${esc(t.regime_tr)}</b> · ` : ''}${esc(fmtInt(t.evaluated))} hisse · ${esc(fmtInt(t.bots))} bot · <b>${esc(fmtInt(t.total))}</b> aday${t.provisional ? ' · <b>tahmini</b> (piyasa öncesi)' : ''}</p>`
    + (rows.length ? '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Yön</th><th>Skor</th><th>Bot</th><th>Zarar dur.</th><th>Kâr al</th><th>Süre</th><th>RSI(2)</th><th>Açılış boşluğu</th><th class="l">En güçlü kural</th></tr></thead><tbody>'
      + rows.slice(0, 30).map(c => `<tr><td class="l"><button type="button" class="tk" data-tk="${esc(c.symbol)}">${esc(c.symbol)}</button></td><td class="l ${c.side === 'long' ? 'up' : 'down'}">${c.side === 'long' ? 'Al' : 'Sat'}</td>`
        + `<td>${esc(fmtNum(c.score, 1))}</td><td>${esc(fmtInt(c.bots))}</td><td>${c.sl_pct != null ? esc('%' + fmtNum(c.sl_pct, 1)) : 'yok'}</td><td>${c.tp_pct != null ? esc('%' + fmtNum(c.tp_pct, 1)) : 'yok'}</td>`
        + `<td>${esc(fmtInt(c.hold))} gün</td><td>${c.rsi2 != null ? esc(fmtNum(c.rsi2, 1)) : '—'}</td><td class="${dirClass(c.gap_pct)}">${esc(fmtPct(c.gap_pct))}</td><td class="l ell dim sm" title="${esc(c.rule)}">${esc(c.rule)}</td></tr>`).join('')
      + '</tbody></table></div>' : stateHtml('empty', 'Bugün hiçbir evrim botunun koşulu oluşmadı (çoğu VIX 20 üstü korku günü bekler).'));
}

function evoAudit(d) {
  const a = obj(d.intraday), st = obj(d.audit);
  let h = `<div class="pt-row"><button type="button" class="btn sm" data-act="lab-audit"${d.audit_running ? ' disabled' : ''}>${icon('play')} Gün içi denetimi çalıştır</button>`
    + `<span class="muted sm">${d.audit_running ? `<span class="pill">Çalışıyor</span> ${esc(st.message || '')}` : st.state === 'error' ? `<span class="down">${esc(st.message)}</span>`
      : 'Son 60 günün 5 dakikalık verisiyle: aynı gün hem zarar durdur hem kâr al seviyesine değen günlerde gerçekte hangisi önce geldi?'}</span></div>`;
  const rows = arr(a.rows);
  if (!rows.length) return h;
  return h + `<p class="muted sm">${esc(whenShort(a.created_at))} · ${esc(fmtInt(a.symbols))} hisse · zarar durdur = kâr al = m × ATR, açılıştan, aynı gün</p>`
    + '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>m × ATR</th><th class="l">Yön</th><th>Gün</th><th>İkisine de değen</th><th>Gerçekte önce kâr al</th><th>Aynı 5 dk barında</th><th>İşlem başına etki</th></tr></thead><tbody>'
    + rows.map(r => `<tr><td>${esc(fmtNum(r.mult, 1))}</td><td class="l">${r.side === 'long' ? 'Al' : 'Sat'}</td><td>${esc(fmtInt(r.days))}</td><td>${r.ambiguous_pct != null ? esc('%' + fmtNum(r.ambiguous_pct, 2)) : '—'}</td>`
      + `<td>${r.target_first_pct != null ? esc('%' + fmtNum(r.target_first_pct, 1)) : '—'}</td><td>${r.same_bar_pct != null ? esc('%' + fmtNum(r.same_bar_pct, 1)) : '—'}</td><td class="up">${r.bias_per_trade_pct != null ? esc('+' + fmtNum(r.bias_per_trade_pct, 3) + ' puan') : '—'}</td></tr>`).join('')
    + '</tbody></table></div><p class="dim sm pt-hint">İşlem başına etki: laboratuvarın ihtiyatlı "ikisine de değdiyse zarar say" kuralının sonuçları ortalama ne kadar kötü gösterdiği (yüzde puan). Pozitif değer, gerçek sonucun testtekinden biraz daha iyi olacağı anlamına gelir.</p>';
}

// Walk-forward neuroevolution: the champions' day-by-day record (each day's champion chosen from the past only).
function labNNWalk(rep) {
  const r = obj(rep.record), u = obj(rep.record_unseen), rl = obj(rep.rules);
  const ok = num(r.daily_excess_pct) > 0 && num(r.t_excess) >= 2;
  const verdict = !r.days ? ['amber', 'Sicil oluşmadı.']
    : ok ? ['ok', `Şampiyonlar 10 yıl boyunca, yalnızca geçmişe bakılarak seçildikleri halde piyasanın genel hareketini geçti (günde ${fmtPct(r.daily_excess_pct, 3)}, t ${fmtNum(r.t_excess, 1)}).`]
    : num(r.daily_pct) > 0 ? ['amber', `Şampiyonlar kârda (günde ${fmtPct(r.daily_pct, 3)}) ama bu kazanç piyasanın genel yükselişinden fazla değil (piyasaya göre ${fmtPct(r.daily_excess_pct, 3)}, t ${fmtNum(r.t_excess, 1)}): gerçek seçim becerisi kanıtlanmadı.`]
    : ['amber', `Şampiyonlar 10 yılda zarar etti (günde ${fmtPct(r.daily_pct, 3)}).`];
  const curve = arr(rep.curve);
  const years = Object.keys(obj(r.years));
  let h = `<p class="muted sm lab-ranges">${esc(whenShort(rep.created_at))} · ${esc(fmtInt(obj(rep.pool).symbols))} hisse · <b>${esc(arr(rep.range)[0] || '')} – ${esc(arr(rep.range)[1] || '')}</b> gün gün yaşandı (${esc(fmtInt(rep.days))} gün) · `
    + `${esc(fmtInt(rep.births))} ağ doğdu, ${esc(fmtInt(rep.deaths))} elendi · her ${rl.select_every === 1 ? 'işlem günü 1 ağ elenir' : `${esc(fmtInt(rl.select_every))} günde bir eleme`} (yalnızca kapanmış işlemlerle, son ${esc(fmtInt(rl.window))} gün; yeni doğan ${esc(fmtInt(rl.age_min))} gün korunur) · okul dönemi ${esc(rep.school_until || '')}'e kadar sayılmaz · ${esc(fmtNum(rep.seconds / 60, 1))} dk</p>`
    + `<div class="lab-verdict ${verdict[0]}">${icon(verdict[0] === 'ok' ? 'check' : 'warn')}<span>${esc(verdict[1])}</span></div>`;
  const row = (label, s) => `<tr><td class="l">${label}</td><td>${esc(fmtInt(s.days))}</td><td>${esc(fmtInt(s.n))}</td><td>${labPct(s.daily_pct, 3)}</td><td>${labPct(s.daily_excess_pct, 3)}</td><td>${esc(fmtNum(s.t, 1))} / ${esc(fmtNum(s.t_excess, 1))}</td><td>${s.win_rate != null ? esc('%' + fmtNum(s.win_rate, 0)) : '—'}</td></tr>`;
  h += '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Şampiyonların sicili</th><th>Gün</th><th>İşlem</th><th>Günlük net</th><th>Piyasaya göre</th><th>t (net / piyasaya göre)</th><th>Kazanma</th></tr></thead><tbody>'
    + row('İşlem yapılan hisseler (A–D)', r) + row('Hiç kullanılmayan hisseler (E, gölgede)', u)
    + '</tbody></table></div>';
  if (years.length) {
    h += '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Yıl</th>' + years.map(y => `<th>${esc(y)}</th>`).join('') + '</tr></thead><tbody>'
      + `<tr><td class="l">A–D günlük</td>${years.map(y => `<td>${labPct(obj(r.years)[y], 2)}</td>`).join('')}</tr>`
      + `<tr><td class="l">E günlük</td>${years.map(y => `<td>${labPct(obj(u.years)[y], 2)}</td>`).join('')}</tr></tbody></table></div>`;
  }
  if (curve.length > 1) {
    const last = curve[curve.length - 1];
    h += `<div class="sub">Birikimli sicil (işlem başına getirilerin toplamı): şampiyonlar ${labPct(last[1], 0)} · aynı günlerde rastgele hisse ${labPct(last[2], 0)}</div>`
      + `<div class="pt-real-curve">${spark(curve.map(x => x[1]), { w: 600, h: 60, base: 0, cls: dirClass(last[1] - last[2]) })}</div>`
      + `<div class="pt-real-curve">${spark(curve.map(x => x[2]), { w: 600, h: 40, base: 0, cls: 'dim' })}</div>`;
  }
  h += '<p class="dim sm">Sicil, her gün o ana kadar en iyi görünen ağın (şampiyon) seçimleridir; şampiyon yalnızca kapanmış geçmiş işlemlere göre seçildiği için bu, "canlıda yaşasaydık ne olurdu" sorusunun dürüst cevabıdır. "Piyasaya göre" = aynı günlerde rastgele hisse almanın üstündeki kısım. Aşağıdaki tabloda final ağların "son yıl" sonuçları seçimde kullanıldığı için iyimserdir; asıl ölçüt yukarıdaki sicil ve canlı sonuçtur.</p>';
  return h;
}

function labNN(d) {
  const st = obj(d.nn), rep = d.nn_report, running = d.nn_running;
  const dis = running || d.evo_running || d.test_running || d.sweep_running || d.download_running ? ' disabled' : '';
  let h = `<div class="pt-row"><button type="button" class="btn primary" data-act="lab-nn3"${dis}>${icon('play')} 3 çalıştırma (gece için)</button>`
    + `<button type="button" class="btn sm" data-act="lab-nn"${dis}>Tek çalıştırma</button>`
    + `<button type="button" class="btn sm" data-act="lab-nn-today"${rep ? '' : ' disabled'}>Bugünün seçimleri</button>`
    + `<span class="muted sm">${running ? `<span class="pill">Çalışıyor</span> ${esc(st.message || '')}`
      : st.state === 'error' ? `<span class="down">${esc(st.message)}</span>` : st.state === 'done' ? esc(st.message)
      : 'Yaşayarak evrim: elimizdeki tüm botların paralel sinir ağlı ikizleri + hibrit ağlar (60 sinir ağı, 32 girdi → 12 nöron → skor + çıkış kuralı) 10 yıllık geçmişe gün gün girer; her işlem günü yalnızca kapanmış işlemlerine göre en zayıf 1 ağ elenir, kazananların klonu yerine geçer.'}</span></div>`;
  h += '<div id="lab-nn-today"></div>';
  if (!rep) return h + '<p class="dim sm">Henüz çalıştırılmadı. Bölümler: eğitim (A–C hisseleri, eski yıllar) · doğrulama (son yıl, erken durdurma) · sınav (hiç görülmemiş E grubu) · kilitli kasa (son 6 ay, en sonda bir kez) · canlı ileri test (her gece).</p>';
  const R = obj(rep.ranges), models = arr(d.nn_population).length ? arr(d.nn_population) : arr(rep.models);
  const night = obj(d.nn_night), nch = arr(night.changes);
  const STAT = { aktif: ['ok', 'Aktif'], emekli: ['', 'Emekli'], elendi: ['amber', 'Elendi'] };
  h += `<p class="muted sm">Popülasyon: <b>${esc(fmtInt(models.filter(m => m.status === 'aktif').length))}</b> aktif sinir ağı (en fazla 24). Elimizdeki tüm botların (Evrim, Günlük, Momentum, Ort. Dönüş, Boşluk, Olay/Haber, Fırsat 100k$, Mikro 100$) sinir ağlı ikizleri ve 10 yıllık günlük elemeden sağ çıkanlar paralel yarışır; her gece canlıda zararda olanlar elenir, en iyilerin klonu sınavı geçerse eklenir.`
    + (night.ts ? ` Son gece (${esc(whenShort(night.ts))}): ${esc(fmtInt(nch.filter(c => c.kind === 'klon' && c.passed).length))} klon eklendi, ${esc(fmtInt(nch.filter(c => c.kind === 'klon' && !c.passed).length))} klon sınavda kaldı, ${esc(fmtInt(nch.filter(c => c.kind === 'emekli').length))} emeklilik.` : '') + '</p>';
  if (rep.mode === 'walk') h += labNNWalk(rep);
  else h += `<p class="muted sm lab-ranges">${esc(whenShort(rep.created_at))} · ${esc(fmtInt(obj(rep.pool).symbols))} hisse · ${esc(fmtInt(rep.generations))} nesil × ${esc(fmtInt(rep.population))} ağ (${esc(fmtInt(rep.evaluated))} değerlendirme) · ${esc(rep.reason || '')} · ${esc(fmtNum(rep.seconds / 60, 0))} dk</p>`
    + `<p class="muted sm">Eğitim <b>${esc(arr(R.train)[0] || '')} – ${esc(arr(R.train)[1] || '')}</b> · doğrulama <b>${esc(arr(R.val)[0] || '')} – ${esc(arr(R.val)[1] || '')}</b> · kilitli kasa <b>${esc(arr(R.lock)[0] || '')} – ${esc(arr(R.lock)[1] || '')}</b> · canlı ${esc(R.live_from || '')} sonrası</p>`;
  const cell = s => { s = obj(s); return s.n ? `${labPct(s.daily_pct, 3)} <span class="dim sm">(${esc(fmtInt(s.n))} işlem, t ${esc(fmtNum(s.t, 1))})</span>` : '—'; };
  const drift = s => { s = obj(s); return s.n ? labPct(s.daily_excess_pct, 3) : '—'; };
  h += '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Ağ</th><th class="l">Durum</th><th class="l">Çıkış</th><th>Doğrulama: günlük</th><th>Sınav (görülmemiş hisse)</th><th>Sınav: rastgeleye göre</th><th>Kilitli kasa</th><th>Canlı</th><th>Puan</th></tr></thead><tbody>'
    + models.map(m => {
      const lv = obj(m.live), s = STAT[m.status] || (m.passed ? STAT.aktif : STAT.elendi);
      return `<tr class="${m.status && m.status !== 'aktif' ? 'dim' : ''}"><td class="l"><b>${esc(m.id)}</b>${m.label ? ` · <span class="sm">${esc(m.label)}</span>` : ''}<div class="dim sm">${esc(m.origin || '')}${m.parent ? ' · ebeveyn ' + esc(m.parent) : ''}${m.born ? ' · ' + esc(m.born) : ''}</div></td>`
        + `<td class="l"><span class="pill ${s[0]}" title="${esc(m.why || '')}">${esc(s[1])}</span></td>`
        + `<td class="l sm">${esc(m.exit)}</td><td>${cell(m.val)}</td><td>${cell(m.exam)}</td><td>${drift(m.exam)}</td><td>${cell(m.lock)}</td>`
        + `<td>${lv.days ? `${labPct(lv.daily_pct, 2)} <span class="dim sm">(${esc(fmtInt(lv.n))}/${esc(fmtInt(lv.days))} gün)</span>` : '<span class="dim sm">birikiyor</span>'}</td>`
        + `<td>${m.score != null ? labPct(m.score, 3) : '—'}</td></tr>`;
    }).join('')
    + '</tbody></table></div>'
    + '<p class="dim sm">Günlük = günde ortalama net getiri (masraf dahil). "Rastgeleye göre" = aynı günlerde aynı çıkışla rastgele hisse almaya göre fark: piyasanın genel yükselişinden arındırılmış gerçek seçim becerisi. Sınav ve kilitli kasa seçimde hiç kullanılmadı. Haber göstergeleri yalnızca son ~12 ayda var; ağlar haberden henüz az öğrenebilir. Ağlar şimdilik gölgede: emir açmaz, her gece canlı günlerdeki sonuçları güncellenir.</p>';
  return h;
}

function labEvo(d) {
  const ev = obj(d.evo), rep = d.evo_report, running = d.evo_running;
  const dis = running || d.test_running || d.sweep_running || d.download_running ? ' disabled' : '';
  let h = `<div class="pt-row"><button type="button" class="btn primary" data-act="lab-evolve3"${dis}>${icon('play')} 3 turnuva (tekrar kontrolüyle)</button>`
    + `<button type="button" class="btn sm" data-act="lab-evolve"${dis}>Tek turnuva</button>`
    + `<span class="muted sm">${running ? `<span class="pill">Çalışıyor</span> ${esc(ev.message || '')}`
      : ev.state === 'error' ? `<span class="down">${esc(ev.message)}</span>` : ev.state === 'done' ? esc(ev.message)
      : d.download_running ? 'Veri indirme bitince başlatılabilir.'
      : 'Hisseler 5 gruba bölünür; botlar bir grupta üretilip görmedikleri gruba çıkar, kazananlar klonlanıp bir sonraki aşamaya geçer. Son 6 ay kilitli kasada kalır.'}</span></div>`;
  if (!rep || rep.kind !== 'tournament') return h + (rep ? '<p class="dim sm">Önceki sürümün sonucu; yeni turnuvayı başlatın.</p>' : '');
  const R = obj(rep.ranges), lp = obj(rep.lock_pooled), dl = obj(rep.dip_lock), finals = arr(rep.finals);
  const beats = finals.filter(f => f.beats_dip_lock).length;
  const verdict = !finals.length ? ['amber', 'Son aşamadan hiçbir bot çıkmadı: görmediği hisselerde tutarlı kazanç sağlayan kural bulunamadı.']
    : !num(lp.n) ? ['amber', 'Kilitli kasada (son 6 ay) son botlar hiç işlem yapmadı.']
    : num(lp.avg_pct) <= 0 ? ['amber', `Kilitli kasa açıldı: son ${fmtInt(finals.length)} bot, hiç görmedikleri son 6 ayda para kaybetti (işlem başına net ${fmtPct(lp.avg_pct, 2)}).`]
    : beats ? ['ok', `Kilitli kasa açıldı: son ${fmtInt(finals.length)} botun ${fmtInt(lp.bots_profitable)} tanesi son 6 ayda kârda, ${fmtInt(beats)} tanesi piyasayı ve basit "düşeni al" kuralını da geçti. Sıradaki gerçek sınav canlı sanal işlem.`]
    : ['', `Kilitli kasa açıldı: botlar kârda (işlem başına net ${fmtPct(lp.avg_pct, 2)}) ama hiçbiri tüm şartları ve basit "düşeni al" kuralını (günlük net ${fmtPct(dl.daily_pct, 2)}) birlikte geçemedi.`];
  h += `<p class="muted sm lab-ranges">${esc(whenShort(rep.created_at))} · ${esc(fmtInt(rep.symbols))} hisse (gruplar: ${arr(rep.group_symbols).map((n, i) => 'ABCDE'[i] + ' ' + fmtInt(n)).join(', ')}) · ${esc(fmtInt(rep.evaluated))} farklı bot · ${esc(fmtNum(rep.seconds / 60, 1))} dk</p>`
    + `<p class="muted sm">Eğitim bölgesi <b>${esc(arr(R.region)[0] || '')} – ${esc(arr(R.region)[1] || '')}</b> (her aşamanın doğrulama yılı ${esc(arr(R.inner_val)[0] || '')} sonrası) · Kilitli kasa <b>${esc(arr(R.lock)[0] || '')} – ${esc(arr(R.lock)[1] || '')}</b></p>`
    + `<div class="lab-verdict ${verdict[0]}">${icon(verdict[0] === 'ok' ? 'check' : 'warn')}<span>${esc(verdict[1])}</span></div>`
    + '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Aşama</th><th class="l">Üretim</th><th class="l">Sınav</th><th>Nesil</th><th>Denenen bot</th><th>Hayatta kalan</th><th>Çıtayı aşan</th><th>Çıta (düşeni al)</th><th class="l">Durma nedeni</th></tr></thead><tbody>'
    + arr(rep.stages).map(s => `<tr><td class="l">${esc(fmtInt(s.stage))}</td><td class="l">${esc(arr(s.breed_groups).join('+'))}</td><td class="l">${esc(s.exam_group)} <span class="dim sm">(hiç görmedi)</span></td>`
      + `<td>${esc(fmtInt(s.generations))}</td><td>${esc(fmtInt(s.evaluated))}</td><td>${esc(fmtInt(s.survivors))}</td><td>${esc(fmtInt(s.beats_dip))}</td><td>${labPct(s.dip_bar, 2)}</td><td class="l dim sm">${esc(s.stop_reason || '')}</td></tr>`).join('')
    + '</tbody></table></div>'
    + labEvoChart(rep.stages)
    + evoPortfolios(rep)
    + evoStability(d.evo_stability)
    + `<div class="sub">Kilitli kasa: son botlar hiç görmedikleri son 6 ayda (tüm hisseler)</div>`
    + (finals.length ? '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Kural</th><th>Hakem ağırlığı</th><th>Kilitli kasa</th><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th><th>Haber dönemi: olumlu / olumsuz / haber yok</th><th class="l">Sonuç</th></tr></thead><tbody>'
      + finals.map(f => `<tr class="${f.beats_dip_lock ? 'lab-ok' : ''}"><td class="l evo-rule">${evoRule(f)}</td><td>${f.weight != null ? esc(fmtNum(f.weight, 1)) : '—'}</td><td>${evoCell(f.lock, true)}</td>`
        + 'ABCDE'.split('').map(g => { const x = obj(obj(f.lock_groups)[g]); return `<td>${x.n ? labPct(x.daily_excess_pct, 2) + `<div class="dim sm">${esc(fmtInt(x.n))}</div>` : '<span class="dim">—</span>'}</td>`; }).join('')
        + `<td>${['pos', 'neg', 'none'].map(k => { const x = obj(obj(f.news)[k]); return x.n ? `${labPct(x.avg_pct, 2)} <span class="dim sm">(${esc(fmtInt(x.n))})</span>` : '<span class="dim">—</span>'; }).join(' / ')}</td>`
        + `<td class="l">${f.beats_dip_lock ? '<span class="pill ok">Çıtayı aştı</span>' : num(obj(f.lock).avg_pct) > 0 ? '<span class="pill">Kârda</span>' : '<span class="pill amber">Zararda</span>'}</td></tr>`).join('')
      + `<tr class="lab-spy"><td class="l">Basit "düşeni al" (RSI(14) &lt; 35, 5 gün)</td><td></td><td>${evoCell(dl, true)}</td><td colspan="7" class="dim c">çıta</td></tr>`
      + '</tbody></table></div>' : stateHtml('empty', 'Son aşamadan bot çıkmadı.'))
    + arr(rep.stages).map(s => `<details class="evo-stage"><summary class="muted sm">Aşama ${esc(fmtInt(s.stage))} adayları (${esc(arr(s.breed_groups).join('+'))} → ${esc(s.exam_group)}): ${esc(fmtInt(s.survivors))} hayatta kaldı</summary>`
      + '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Kural</th><th>Üretim</th><th>Doğrulama yılı</th><th>Sınav (görülmemiş grup)</th><th class="l">Durum</th></tr></thead><tbody>'
      + arr(s.candidates).map(e => `<tr class="${e.beats_dip ? 'lab-ok' : ''}"><td class="l evo-rule">${evoRule(e)}</td><td>${evoCell(e.breed)}</td><td>${evoCell(e.val)}</td><td>${evoCell(e.exam, true)}</td>`
        + `<td class="l">${e.beats_dip ? '<span class="pill ok">Çıtayı aştı</span>' : e.survived ? '<span class="pill">Hayatta kaldı</span>' : '<span class="pill">Elendi</span>'}</td></tr>`).join('')
      + '</tbody></table></div></details>').join('')
    + `<div class="sub">Bugünün evrim sinyalleri</div><div class="pt-row"><button type="button" class="btn sm" data-act="lab-evo-today" id="lab-evo-today">${icon('play')} Sinyalleri hesapla</button><a href="#islem" class="dim sm">Canlı sanal işlem: Sanal İşlem → "Evrim botu" seçeneği</a></div><div id="lab-evo-today-box">${evoToday(d.evo_today)}</div>`
    + `<div class="sub">Gün içi denetim (5 dakikalık veri)</div>${evoAudit(d)}`
    + '<p class="dim sm pt-hint">Net = kayma düşülmüş gerçek getiri; botların hayatta kalması için hem net kâr hem piyasa üstü getiri gerekir. Piyasa üstü = günlük ortalama (aynı gün açılan işlemler tek gözlem sayılır) olarak, aynı yön ve çıkış kuralıyla rastgele girişe göre fark. Seçim = aynı günlerde bütün hisselere göre fark (hisse seçme becerisi); zamanlama = hangi günlerde işlem açıldığından gelen kısım. Kayma hissenin işlem hacmine göre taraf başına %0,03–%0,20. Bugün borsada olmayan şirketler veride yok; bu, "düşeni al" türü kuralları olduğundan iyi gösterir.</p>';
  return h;
}

function labToday(t) {
  t = obj(t);
  if (!t.rows) return '<p class="muted sm">Bugünün kararlarını görmek için düğmeye basın. Açılış fiyatı gerekir (piyasa öncesinde son fiyatla tahmini hesaplanır).</p>';
  const rows = arr(t.rows);
  const vp = v => v > 0 ? '<span class="up">▲</span>' : v < 0 ? '<span class="down">▼</span>' : '<span class="dim">·</span>';
  return `<p class="muted sm">${esc(whenShort(t.ts))} · ${esc(fmtInt(t.candidates))} aday, hakem ${esc(fmtInt(t.take))} tanesinde işlem açar${t.provisional ? ' · <b>tahmini</b> (piyasa öncesi fiyatlarla)' : ''}${t.stale ? ` · ${esc(fmtInt(t.stale))} hissenin fiyat verisi eski` : ''}</p>`
    + (rows.length ? '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="l">Sembol</th><th class="l">Hakem</th><th>Olasılık</th><th class="l">İşlem</th><th class="c">Momentum</th><th class="c">Ort. dönüş</th><th class="c">Boşluk</th><th class="c">Olay</th><th>Açılış</th><th>Boşluk %</th></tr></thead><tbody>'
      + rows.map(r => `<tr><td class="l"><button type="button" class="tk" data-tk="${esc(r.symbol)}">${esc(r.symbol)}</button></td>`
        + `<td class="l ${r.direction === 'long' ? 'up' : 'down'}">${r.direction === 'long' ? 'Yükseliş' : 'Düşüş'}${r.bots_direction && (r.bots_direction > 0) !== (r.direction === 'long') ? ' <span class="pill amber" title="Botların çoğunluğunun tersine">ters</span>' : ''}</td>`
        + `<td>${r.probability != null ? esc('%' + fmtNum(r.probability * 100, 0)) : esc(fmtSigned(r.score, 2))}</td>`
        + `<td class="l">${r.take ? '<span class="pill ok">Girer</span>' : '<span class="pill">Girmez</span>'}</td>`
        + ['momentum', 'meanrev', 'gap', 'event'].map(k => `<td class="c">${vp(obj(r.votes)[k])}</td>`).join('')
        + `<td>${esc(fmtPrice(r.open))}</td><td class="${dirClass(r.gap_pct)}">${esc(fmtPct(r.gap_pct))}</td></tr>`).join('')
      + '</tbody></table></div>' : stateHtml('empty', 'Bugün hiçbir bot sinyal vermedi.'));
}

// ------------------------------------------------------------ canlı öğrenme (lab/learn.py)

const LEARN_KIND_TR = { firsat: 'Fırsat botu', mikro: 'Mikro bot' };
const LEARN_STATUS_TR = { aktif: 'İşlemde', aday: 'Aday', emekli: 'Emekli', 'şampiyon': 'Uygulanan', 'yarışıyor': 'Yarışıyor', elendi: 'Elendi' };
const LEARN_WHY_TR = { stop: 'zarar durdur', target: 'kâr al', eod: 'gün sonu', cut10: '10 dk kesme', cull30: '30 dk eleme', lock: 'kâr kilidi', open: 'açık' };

function learnKpis(L) {
  const k = (label, val, sub, cls) => `<div class="pt-kpi"><div class="pt-kl">${esc(label)}</div><div class="pt-kv ${cls || ''}">${val}</div>${sub ? `<div class="pt-ks">${sub}</div>` : ''}</div>`;
  const c = obj(L.counts), arms = obj(obj(obj(L.report).evo).arms), F = obj(L.firsat), M = obj(L.mikro);
  const armSub = a => { a = obj(a); return a.days ? `canlı ${esc(fmtInt(a.days))} gün · ${esc(fmtInt(a.trades))} işlem · ort. ${labPct(a.avg_pct, 2)}` : 'canlı sonuç bekleniyor (korku günlerinde işlem açar)'; };
  const NN = obj(L.nn);
  const nnSub = NN.live_days ? `canlı ${esc(fmtInt(NN.live_days))} gün · ${esc(fmtInt(NN.live_trades))} işlem · ort. ${labPct(NN.avg_pct, 2)}` : 'mevcut tüm botların 32→12→1 nöronlu ikizleri paralel yarışta';
  return k('Ana kol (10 yıllık, değişmez)', esc(fmtInt(obj(c.ana).aktif)) + ' bot', armSub(arms.ana))
    + k('Evrim kolu (her gece gelişir)', esc(fmtInt(obj(c.evrim).aktif)) + ' bot', `${esc(fmtInt(obj(c.evrim).aday))} aday · ${armSub(arms.evrim)}`)
    + k('Sinir ağı kolu (nöronlu ikizler)', esc(fmtInt(NN.active)) + ' ağ', nnSub)
    + k('Fırsat botu ayarları', esc(fmtInt(F.count)) + ' çeşit', `${esc(fmtInt(F.days))} günlük kayıt üzerinde yarışıyor`)
    + k('Mikro bot ayarları', esc(fmtInt(M.count)) + ' çeşit', `${esc(fmtInt(M.days))} günlük kayıt üzerinde yarışıyor`)
    + k('Hakem', L.trading_arm === 'ana' ? 'Ana kol işlemde' : 'Evrim kolu işlemde', 'evrim kolu canlıda ana kolun gerisine düşerse işlemi ana kol yapar');
}

function learnControls(L) {
  const st = obj(L.settings), run = obj(L.running), night = obj(L.night), rep = obj(L.report);
  const opts = Object.entries(obj(L.speeds)).map(([k, v]) => `<option value="${esc(k)}"${st.speed === k ? ' selected' : ''}>${esc(v.label)} (en az ${esc(fmtInt(v.min_results))} sonuç, ${esc(fmtInt(v.min_days))} gün)</option>`).join('');
  const msg = run.nightly ? `<span class="pill">Çalışıyor</span> ${esc(night.message || '')}`
    : night.state === 'error' ? `<span class="down">${esc(night.message)}</span>`
    : L.last_night ? `Son gece öğrenmesi: <b>${esc(L.last_night)}</b>${rep.seconds ? ` (${esc(fmtNum(rep.seconds / 60, 1))} dk)` : ''}. Her işlem günü, kapanıştan sonraki veri güncellemesinin ardından (TR ~00:00) kendiliğinden çalışır.`
    : 'Henüz çalışmadı: ilk çalışmada 10 yıllık botlar iki kola (ana ve evrim) yerleşir.';
  return `<div class="pt-row"><label class="pt-f"><span>Öğrenme hızı</span><select class="sel sm" data-f="learnSpeed">${opts}</select></label>`
    + `<label class="pt-chk"><input type="checkbox" data-f="learnAuto"${st.auto_apply ? ' checked' : ''}> Öğrenileni ertesi gün sanal hesapta otomatik uygula</label><span class="sp"></span>`
    + `<button type="button" class="btn sm" data-act="learn-run"${run.nightly ? ' disabled' : ''}>${icon('play')} Gece öğrenmesini şimdi çalıştır</button>`
    + `<button type="button" class="btn sm ghost danger" data-act="learn-rollback"${L.versions ? '' : ' disabled'}>Son değişikliği geri al</button></div>`
    + `<p class="muted sm">${msg}${L.live_from ? ` · Canlı takip ${esc(L.live_from)} itibarıyla` : ''}${obj(L.night_error).error ? ` · <span class="down">son hata: ${esc(L.night_error.error)}</span>` : ''}</p>`;
}

function learnChanges(rep) {
  rep = obj(rep);
  if (!rep.date) return '';
  const evo = obj(rep.evo), items = [];
  if (evo.error) items.push(`<li class="down">Evrim kolları: ${esc(evo.error)}</li>`);
  const evoCh = arr(evo.changes);
  for (const c of evoCh.filter(x => x.kind !== 'klon')) {
    if (c.kind === 'terfi') items.push(`<li><span class="pill ok">Terfi</span> ${esc(c.rule)} <span class="dim sm">canlı ${esc(fmtInt(obj(c.live).n))} işlem / ${esc(fmtInt(obj(c.live).days))} gün, puan ${esc(fmtPct(c.score, 3))}/gün</span></li>`);
    else if (c.kind === 'emekli') items.push(`<li><span class="pill amber">Emekli</span> ${esc(c.rule)} <span class="dim sm">${esc(c.why || `canlıda zararda (puan ${fmtPct(c.score, 3)}/gün)`)}</span></li>`);
    else if (c.kind === 'stoplu klon') items.push(`<li><span class="pill ${c.result === 'geçti' ? 'ok' : 'amber'}">Zarar durdursuz bot: ${c.result === 'geçti' ? 'stoplu klonu sınavı geçti' : 'stoplu klonu sınavı geçemedi'}</span> ${esc(c.result === 'geçti' ? c.new_rule : c.rule)} <span class="dim sm">10 yıllık uygunluk ${esc(fmtNum(c.fit_before, 2))}${c.fit_after != null ? ' → ' + esc(fmtNum(c.fit_after, 2)) : ''}</span></li>`);
  }
  const clones = evoCh.filter(x => x.kind === 'klon');
  for (const c of clones.slice(0, 6)) items.push(`<li><span class="pill">Yeni aday (klon)</span> ${esc(c.rule)} <span class="dim sm">10 yıl: günlük ${esc(fmtPct(obj(c.hist).daily_pct, 2))}, uygunluk ${esc(fmtNum(c.fit, 2))} (ebeveyn ${esc(fmtNum(c.parent_fit, 2))})</span></li>`);
  if (clones.length > 6) items.push(`<li class="muted">+${esc(fmtInt(clones.length - 6))} aday daha</li>`);
  for (const kind of ['firsat', 'mikro']) {
    const r = obj(rep[kind]);
    if (r.error) items.push(`<li class="down">${LEARN_KIND_TR[kind]}: ${esc(r.error)}</li>`);
    if (r.note) items.push(`<li class="muted">${LEARN_KIND_TR[kind]}: ${esc(r.note)}</li>`);
    for (const c of arr(r.changes).filter(x => x.kind === 'ayar')) {
      const ev = obj(c.evidence), inc = obj(c.incumbent);
      items.push(`<li><span class="pill ok">${LEARN_KIND_TR[kind]} ayarı ${c.applied ? 'değişti' : 'değişmeli (otomatik uygulama kapalı)'}</span> ${esc(arr(c.diff).join(' · '))} `
        + `<span class="dim sm">kanıt: ${esc(fmtInt(ev.n))} işlem / ${esc(fmtInt(ev.days))} gün, günlük ${esc(fmtPct(ev.daily_pct, 3))} (önceki ayar ${esc(fmtPct(inc.daily_pct, 3))})</span></li>`);
    }
    const nk = arr(r.changes).filter(c => c.kind === 'klon').length, ne = arr(r.changes).filter(c => c.kind === 'elendi').length;
    if (nk || ne) items.push(`<li class="muted">${LEARN_KIND_TR[kind]}: ${esc(fmtInt(ne))} çeşit elendi, ${esc(fmtInt(nk))} yeni klon yarışa girdi</li>`);
  }
  return `<div class="sub">${esc(rep.date)} gecesi ne değişti</div>`
    + (items.length ? `<ul class="learn-list">${items.join('')}</ul>` : '<p class="muted sm">Değişiklik yok: henüz yeterli kanıt birikmedi.</p>')
    + (evo.exams != null ? `<p class="dim sm">Evrim kolu: ${esc(fmtInt(evo.exams))} klon 10 yıllık sınava girdi, ${esc(fmtInt(evo.passed))} tanesi geçip aday oldu. Adaylar canlıda yeterli sonuç toplayınca işleme alınır.</p>` : '');
}

function learnLiveRows(rows, champ) {
  const show = rows.slice(0, 5);
  if (champ && champ.id && !show.some(r => r.id === champ.id)) show.push(champ);
  const worst = rows.length > 8 ? rows.slice(-3).reverse() : [];
  const row = r => `<tr class="${r.status === 'şampiyon' ? 'lab-ok' : ''}"><td class="l evo-rule">${esc(r.text)}</td><td>${labPct(r.ret_pct, 3)}</td><td>${esc(fmtInt(r.n))}${r.open ? ` <span class="dim sm">(${esc(fmtInt(r.open))} açık)</span>` : ''}</td>`
    + `<td class="l sm">${arr(r.trades).map(t => `${esc(t.symbol)} ${labPct(t.ret_pct, 1)}${t.why && t.why !== 'open' ? ` <span class="dim">${esc(LEARN_WHY_TR[t.why] || t.why)}</span>` : ''}`).join(', ')}</td>`
    + `<td class="l">${r.status === 'şampiyon' ? '<span class="pill ok">Uygulanan</span>' : ''}${r.elim ? ` <span class="pill amber">${esc(r.elim)} itibarıyla elendi</span>` : ''}</td></tr>`;
  return '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Ayar</th><th>Bugün (hesaba)</th><th>İşlem</th><th class="l">İşlemler</th><th class="l"></th></tr></thead><tbody>'
    + show.map(row).join('') + (worst.length ? `<tr><td colspan="5" class="dim sm l">En geride kalanlar</td></tr>${worst.map(row).join('')}` : '') + '</tbody></table></div>';
}

function learnLive(L) {
  const lv = obj(L.live);
  if (!lv.ts) return '<p class="muted sm">Seans açıldıktan sonra (10:00 ET / 17:00 TR) her 30 dakikada bir güncellenir.</p>';
  let h = `<p class="muted sm">${esc(whenShort(lv.ts))} itibarıyla (${esc(lv.day)}). "Bugün" = o ayarın bugünkü gölge işlemlerinin hesaba katkısı (kapanan + açık).</p>`;
  for (const kind of ['firsat', 'mikro']) {
    const x = obj(lv[kind]), rows = arr(x.rows);
    if (x.error) { h += `<p class="down sm">${LEARN_KIND_TR[kind]}: ${esc(x.error)}</p>`; continue; }
    if (!rows.length) { h += `<p class="muted sm">${LEARN_KIND_TR[kind]}: bugün henüz uygun aday yok (${esc(fmtInt(x.candidates || 0))} hisse kaydedildi).</p>`; continue; }
    h += `<div class="sub">${LEARN_KIND_TR[kind]}: ${esc(fmtInt(rows.length))} çeşit · ${esc(fmtInt(x.eliminated || 0))} tanesi gün içi elemeyle bugün durdu</div>` + learnLiveRows(rows, x.champion);
  }
  const evo = obj(lv.evo);
  const armLabel = a => a === 'ana' ? 'ana kol' : a === 'evrim' ? 'evrim kolu' : a === 'sinir' ? 'sinir ağı kolu' : a;
  const armTxt = Object.entries(obj(evo.arms)).map(([a, v]) => `${armLabel(a)} ${esc(fmtPct(v.avg_pct, 2))} (${esc(fmtInt(v.signals))} sinyal)`).join(' · ');
  h += '<div class="sub">Evrim kolları</div>' + (evo.signals
    ? `<p class="muted sm">Bu sabah ${esc(fmtInt(evo.signals))} hissede sinyal (VIX ${esc(fmtNum(evo.vix, 1))}). Açılıştan bu yana: ${armTxt}. Kesin sonuçlar gece günlük verilerle hesaplanır.</p>`
    : `<p class="muted sm">Bugün evrim botları sinyal vermedi${evo.vix != null ? ` (VIX ${esc(fmtNum(evo.vix, 1))}; botlar çoğunlukla 20 üstünü bekler)` : ''}.</p>`);
  return h;
}

function learnVariants(kind, X) {
  X = obj(X);
  const rows = arr(X.variants);
  if (!rows.length) return '<p class="muted sm">İlk gece öğrenmesinde 48 çeşit oluşturulur.</p>';
  return `<p class="muted sm">Sanal hesapta şu an uygulanan: <b>${esc(X.applied || '')}</b></p>`
    + learnForward(X.forward)
    + '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Ayar</th><th>Gün</th><th>İşlem</th><th>Kazanma</th><th>İşlem başına</th><th>Günlük (hesaba)</th><th>Puan</th><th class="l">Durum</th></tr></thead><tbody>'
    + rows.map(v => {
      const s = obj(v.stats);
      return `<tr class="${v.status === 'şampiyon' ? 'lab-ok' : ''}"><td class="l evo-rule">${esc(v.text)}<div class="dim sm">${esc(v.origin || '')}${v.born ? ' · ' + esc(v.born) : ''}</div></td>`
        + `<td>${esc(fmtInt(s.days))}</td><td>${esc(fmtInt(s.n))}</td><td>${s.win_rate != null ? esc('%' + fmtNum(s.win_rate, 0)) : '—'}</td>`
        + `<td>${labPct(s.avg_trade_pct, 2)}</td><td>${labPct(s.daily_pct, 3)}</td><td>${labPct(v.score, 3)}</td>`
        + `<td class="l"><span class="pill ${v.status === 'şampiyon' ? 'ok' : ''}">${esc(LEARN_STATUS_TR[v.status] || v.status)}</span></td></tr>`;
    }).join('')
    + `</tbody></table></div><p class="dim sm">${esc(fmtInt(X.count))} çeşitten ilk 16'sı. Günlük = günde hesaba ortalama katkı; puan = (20 × 0 + günlük toplam) / (20 + gün), yani az günlük şanslı sonuçlar küçültülür.</p>`;
}

// Honest forward test: the setting in effect each day was chosen the night before from earlier days only.
function learnForward(F) {
  F = obj(F);
  if (!F.days) return '<p class="dim sm">İleri test: seçilen ayarın, seçildikten <b>sonraki</b> günlerdeki sonucu burada birikecek (ilk kayıt bir sonraki gece öğrenmesinde).</p>';
  const rows = arr(F.rows).slice(0, 10);
  return `<div class="sub">İleri test (seçimden sonraki günler, ${esc(fmtInt(F.days))} gün)</div>`
    + `<p class="muted sm">Seçilen ayar toplam ${labPct(F.sum_pct, 2)} · rastgele ayar ortalaması ${labPct(F.mean_sum_pct, 2)} · fark ${labPct(F.edge_pct, 2)}`
    + ` (günde ${labPct(F.edge_daily_pct, 3)}) · ortalamayı geçtiği gün: ${esc(fmtInt(F.beat_days))}/${esc(fmtInt(F.days))}`
    + (F.reliable ? '' : ' · <span class="pill amber">az veri (20 günden az): henüz karar verilemez</span>') + '</p>'
    + '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Gün</th><th>Seçilen ayar</th><th>Ortalama</th><th>Medyan</th><th>Geriye dönük en iyi</th><th>İşlem</th></tr></thead><tbody>'
    + rows.map(r => `<tr><td class="l">${esc(r.day)}</td><td>${labPct(r.ret_pct, 2)}</td><td>${labPct(r.mean_pct, 2)}</td><td>${labPct(r.median_pct, 2)}</td>`
      + `<td class="dim">${labPct(r.best_pct, 2)}</td><td>${esc(fmtInt(r.trades))}</td></tr>`).join('')
    + '</tbody></table></div><p class="dim sm">"Geriye dönük en iyi" o gün bittikten sonra bilinebilir; yalnızca karşılaştırma içindir. Gerçek ölçüt seçilen ayarın ortalamayı uzun dönemde geçip geçmediğidir.</p>';
}

function learnMembers(L) {
  const ms = arr(L.members).filter(m => m.arm === 'evrim' && m.status !== 'emekli');
  if (!ms.length) return '<p class="muted sm">Evrim kolu ilk gece öğrenmesinde kurulur.</p>';
  const retired = arr(L.members).filter(m => m.arm === 'evrim' && m.status === 'emekli').length;
  return '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Kural</th><th class="l">Durum</th><th>10 yıl: günlük</th><th>10 yıl: t</th><th>Canlı işlem / gün</th><th>Canlı günlük</th><th>Puan</th><th>Hakem ağırlığı</th><th class="l" title="Rejim karnesi: geçmiş günlük getiri (gün) · canlı günlük getiri (gün) → o rejimde hakem ağırlığı">Rejim karnesi (K / Y / Z)</th></tr></thead><tbody>'
    + ms.map(m => {
      const lv = obj(m.live), h = obj(m.hist);
      return `<tr class="${m.status === 'aktif' ? '' : 'dim'}"><td class="l evo-rule">${esc(m.rule)}<div class="dim sm">${esc(m.origin || '')}${m.parent ? ' · ebeveyn ' + esc(m.parent) : ''} · ${esc(m.id)}</div></td>`
        + `<td class="l"><span class="pill ${m.status === 'aktif' ? 'ok' : ''}">${esc(LEARN_STATUS_TR[m.status] || m.status)}</span></td>`
        + `<td>${labPct(h.daily_pct, 2)}</td><td>${esc(fmtNum(h.t, 1))}</td>`
        + `<td>${esc(fmtInt(lv.n || 0))} / ${esc(fmtInt(lv.days || 0))}${lv.open ? ` <span class="dim sm">(${esc(fmtInt(lv.open))} açık)</span>` : ''}</td>`
        + `<td>${lv.days ? labPct(lv.daily_pct, 2) : '—'}</td><td>${labPct(m.score, 3)}</td>`
        + `<td>${esc(fmtNum(m.weight, 1))}${m.weight_base != null && Math.abs((m.weight || 0) - m.weight_base) > 0.05 ? ` <span class="dim sm">(başta ${esc(fmtNum(m.weight_base, 1))})</span>` : ''}</td>`
        + `<td class="l">${learnRegimeCell(lv.regime)}</td></tr>`;
    }).join('')
    + `</tbody></table></div>${retired ? `<p class="dim sm">${esc(fmtInt(retired))} bot emekliye ayrıldı (kayıtları duruyor).</p>` : ''}`
    + '<p class="dim sm">Rejim karnesi: K = korku günü (VIX ≥ 20), Y = sakin ve geniş yükseliş (hisselerin yarısından fazlası 50 günlük ortalamasının üstünde), Z = sakin ama zayıf piyasa. Her hücre: geçmişte o rejimdeki günlük getirisi (gün sayısı), canlı sonucu ve hakemin o rejimde verdiği ağırlık. Hakem bugünün rejimine bakar; o rejimde zarar eden bot oy veremez.</p>';
}

const REGIME_SHORT = { korku: 'K', yukselis: 'Y', zayif: 'Z' };
function learnRegimeCell(R) {
  R = obj(R);
  const keys = Object.keys(REGIME_SHORT).filter(k => R[k]);
  if (!keys.length) return '<span class="dim sm">gece öğrenmesinde hesaplanır</span>';
  return keys.map(k => {
    const r = obj(R[k]);
    const hist = r.h_days ? `${labPct(r.h_daily_pct, 2)} <span class="dim">(${esc(fmtInt(r.h_days))})</span>` : '<span class="dim">—</span>';
    const live = r.l_days ? ` · canlı ${labPct(r.l_daily_pct, 2)} <span class="dim">(${esc(fmtInt(r.l_days))})</span>` : '';
    return `<div class="sm"><b>${REGIME_SHORT[k]}</b> ${hist}${live} → <b class="${r.w > 0 ? '' : 'down'}">${esc(fmtNum(r.w, 1))}</b></div>`;
  }).join('');
}

function learnNNMembers(L) {
  const NN = obj(L.nn), ms = arr(NN.models);
  if (!ms.length) return '<p class="muted sm">Sinir ağı popülasyonu henüz oluşturulmadı.</p>';
  return '<div class="tbl-wrap"><table class="tbl lab-tbl"><thead><tr><th class="l">Sinir ağı (32→12→1)</th><th class="l">Köken / İkiz</th><th class="l">Çıkış</th><th>Doğrulama: günlük</th><th>Sınav (görülmemiş E)</th><th>Canlı</th><th>Puan</th><th class="l">Durum</th></tr></thead><tbody>'
    + ms.map(m => {
      const lv = obj(m.live), v = obj(m.val), ex = obj(m.exam);
      return `<tr class="${m.status === 'aktif' ? '' : 'dim'}"><td class="l"><b>${esc(m.id)}</b>${m.parent ? `<div class="dim sm">ebeveyn ${esc(m.parent)}</div>` : ''}</td>`
        + `<td class="l evo-rule">${esc(m.label || m.origin || '')}</td>`
        + `<td class="l sm">${esc(m.exit || '')}</td>`
        + `<td>${v.n ? `${labPct(v.daily_pct, 3)} <span class="dim sm">(${esc(fmtInt(v.n))})</span>` : '—'}</td>`
        + `<td>${ex.n ? `${labPct(ex.daily_pct, 3)} <span class="dim sm">(fark ${labPct(ex.daily_excess_pct, 3)})</span>` : '—'}</td>`
        + `<td>${lv.days ? `${labPct(lv.daily_pct, 2)} <span class="dim sm">(${esc(fmtInt(lv.n))}/${esc(fmtInt(lv.days))} gün)</span>` : '<span class="dim sm">birikiyor</span>'}</td>`
        + `<td>${m.score != null ? labPct(m.score, 3) : '—'}</td>`
        + `<td class="l"><span class="pill ${m.status === 'aktif' ? 'ok' : ''}">${esc(m.status || 'aktif')}</span></td></tr>`;
    }).join('')
    + '</tbody></table></div>';
}

function learnPanel(L) {
  if (!L) return stateHtml('loading');
  const sp = obj(obj(L.speeds)[obj(L.settings).speed]);
  return learnControls(L)
    + `<div class="pt-kpis">${learnKpis(L)}</div>`
    + learnChanges(L.report)
    + '<div class="sub">Gün içi (30 dakikada bir, gölgede)</div>' + learnLive(L)
    + `<details class="evo-stage"><summary class="muted sm">Fırsat botu: ayar çeşitleri ve sonuçları</summary>${learnVariants('firsat', L.firsat)}</details>`
    + `<details class="evo-stage"><summary class="muted sm">Mikro bot (100 $): ayar çeşitleri ve sonuçları</summary>${learnVariants('mikro', L.mikro)}</details>`
    + `<details class="evo-stage"><summary class="muted sm">Evrim kolu: işlemdeki botlar ve adaylar</summary>${learnMembers(L)}</details>`
    + `<details class="evo-stage"><summary class="muted sm">Sinir ağı kolu: tüm botların nöronlu ikizleri ve 10 yıllık günlük eleme şampiyonları</summary>${learnNNMembers(L)}</details>`
    + `<p class="dim sm pt-hint">Gölge = her bot çeşidinin her sinyali, gerçek sanal işlemle aynı giriş/çıkış kuralları ve maliyetle sanal olarak takip edilir; 48 ayarı denemek için 48 pozisyon açılmaz. `
    + `Puan bileşiktir: 10 yıllık sonuçların yarısı başlangıç beklentisi sayılır, canlı sonuçlar geldikçe ağırlığı artar (${esc(sp.label || '')}: beklenti ${esc(fmtInt(sp.n0))} gün yerine geçer; terfi, eleme ve ayar değişikliği için en az ${esc(fmtInt(sp.min_results))} sonuç ve ${esc(fmtInt(sp.min_days))} gün gerekir). `
    + 'Klonlar kuralın omurgasını korur (aynı göstergeler), yalnızca eşikleri ve çıkışı biraz değişir ve önce 10 yıllık sınavı geçmek zorundadır; eğitimsiz bot havuza giremez. Sonuçlar gelecekteki getiriyi garanti etmez.</p>';
}

// ---- bot ağacı (fleet.py): 2 dal × 5 grup × 10 bot, her biri kendi sanal hesabıyla
const LT_BRANCH_TR = { kural: 'Kural botları (evrim)', sinir: 'Sinir ağı botları' };
const LT_GROUP_TR = { gunluk: 'Günlük', korku: 'Korku günü (VIX ≥ 20)', haftalik: 'Haftalık (~5 gün)', aylik: 'Aylık (~20 gün)', serbest: 'Serbest (1–90 gün, stoplu)' };
const LT_GROUP_HINT = { gunluk: 'Her gün alım yapar, 1–2 gün tutar', korku: 'Sadece VIX 20 ve üstündeyken alır', haftalik: '5 işlem günü tutar (stop / hedef önce gelirse erken çıkar)',
  aylik: '20 işlem günü tutar', serbest: 'Tutma süresini kendisi seçer (1–90 gün), stop her zaman var' };
const LT_WHY_TR = { 'süre doldu': 'süre', 'zarar durdur': 'stop', 'kâr al': 'hedef', 'bot emekli': 'emekli' };
// kripto ağacı (crypto/cfleet.py): 2 dal × 5 grup × 4 bot; her grupta 1 bot 100 bin $, 3 bot 100 $
const CT_GROUP_TR = { gunluk: 'Günlük (1–2 gün)', korku: 'Korku günü (Korku-Açgözlülük ≤ 30)', haftalik: 'Haftalık (7 gün)', aylik: 'Aylık (30 gün)', serbest: 'Serbest (1–90 gün, stoplu)' };
const CT_GROUP_HINT = { gunluk: 'Her gün 03:05\'te (Türkiye saati) alır, 1–2 gün tutar', korku: 'Sadece dünkü Korku-Açgözlülük endeksi 30 ve altındayken alır',
  haftalik: '7 gün tutar (stop / hedef önce gelirse erken çıkar)', aylik: '30 gün tutar', serbest: 'Tutma süresini kendisi seçer (1–90 gün), stop her zaman var' };
function ltSym(sym, cr) { return cr ? `<span class="tk">${esc(String(sym || '').replace(/USDT$/, ''))}</span>` : `<button type="button" class="tk" data-tk="${esc(sym)}">${esc(sym)}</button>`; }
function ltOpen(key, dflt) { const st = obj(store.get('labTree')); return key in st ? !!st[key] : dflt; }
function ltSum(bots) {
  const big = bots.filter(b => b.start_cash >= 1000), small = bots.filter(b => b.start_cash < 1000);
  const avg = xs => xs.length ? xs.reduce((a, b) => a + (num(b.total_pnl_pct) || 0), 0) / xs.length : null;
  return { big, small, avgBig: avg(big), avgSmall: avg(small), open: bots.reduce((a, b) => a + arr(b.open).length, 0) };
}
function ltSumHtml(bots) {
  const s = ltSum(bots);
  return `<span class="lt-sum">${s.big.length ? `100 bin $ botları ort. ${labPct(s.avgBig)}` : ''}${s.small.length ? ` · 100 $ botları ort. ${labPct(s.avgSmall)}` : ''} · ${s.open} açık pozisyon</span>`;
}
function ltTrade(p, cr) {
  const days = p.hold_days ? ` · en fazla ${p.hold_days} gün` : '';
  return `<div class="lt-pos">${ltSym(p.symbol, cr)}`
    + `<span>${labPct(p.unrealized_pct)}</span><span class="dim sm" title="Alış ${esc(fmtPrice(p.avg_price))} · stop ${esc(fmtPrice(p.stop_price))} · hedef ${esc(fmtPrice(p.target_price))}${esc(days)}">`
    + `stop ${esc(fmtPrice(p.stop_price))} · hedef ${esc(fmtPrice(p.target_price))}</span></div>`;
}
function ltCard(b, cr) {
  const micro = b.start_cash < 1000;
  const lv = obj(b.live), wl = obj(obj(b.walk).last);
  const curve = arr(b.curve).map(x => x[1]);
  const closed = arr(b.closed);
  return `<div class="lt-card${micro ? ' micro' : ''}" data-bot="${esc(b.id)}">`
    + `<div class="lt-head"><b>#${esc(b.rank)}</b><span class="dim sm">${esc(b.hold)} gün</span>`
    + `${micro ? '<span class="pill amber">100 $</span>' : '<span class="pill">100 bin $</span>'}`
    + `${obj(b.walk).filled ? '<span class="pill" title="Sınavı geçen bot yetmediği için grubun en iyi 3 botundan klonlandı">klon</span>' : ''}<span class="sp"></span>`
    + `<span class="lt-eq">${esc(usd(b.equity, micro ? 2 : 0))}</span></div>`
    + `<div class="lt-k"><span>Toplam ${labPct(b.total_pnl_pct)}</span><span>Bugün ${b.day_pnl_pct == null ? '—' : labPct(b.day_pnl_pct)}</span>`
    + `<span class="dim">${esc(lv.n || 0)} işlem${lv.n ? ` · %${esc(fmtNum(lv.wins / lv.n * 100, 0))} kazanç` : ''}</span></div>`
    + (curve.length > 1 ? spark(curve, { w: 200, h: 26, base: b.start_cash }) : '')
    + `<div class="lt-rule ell" title="${esc(b.rule)}">${esc(b.rule)}</div>`
    + `<div class="dim sm">${cr ? 'Geçmiş' : '10 yıllık'} sicil (son yıl): işlem başı ${esc(fmtPct(wl.avg_pct))} · ${esc(wl.days ?? '—')} gün</div>`
    + (arr(b.open).length ? `<div class="lt-open">${arr(b.open).map(p => ltTrade(p, cr)).join('')}</div>` : '<div class="dim sm">Açık pozisyon yok</div>')
    + (closed.length ? `<details class="lt-more"><summary class="dim sm">Son ${closed.length} kapanan işlem</summary>`
      + closed.map(t => `<div class="lt-pos">${ltSym(t.symbol, cr)}<span>${labPct(t.ret_pct)}</span>`
        + `<span class="dim sm">${esc(LT_WHY_TR[t.why] || t.why || '')} · ${esc(whenShort(t.closed_at))}</span></div>`).join('') + '</details>' : '')
    + '</div>';
}
function ltCardSig(b) { return JSON.stringify([b.equity, b.day_pnl_pct, arr(b.open).map(p => [p.symbol, p.last_price]), arr(b.closed).map(t => t.closed_at), b.score]); }
function ltTreeHtml(F, cr) {
  const bots = arr(F.bots);
  const meta = obj(obj(F.tree).groups);
  const px = cr ? 'ct' : 'lt', kp = cr ? 'c:' : '', groups = cr ? CT_GROUP_TR : LT_GROUP_TR, hints = cr ? CT_GROUP_HINT : LT_GROUP_HINT;
  let h = '';
  for (const br of ['kural', 'sinir']) {
    const bb = bots.filter(b => b.branch === br);
    if (!bb.length) continue;
    h += `<details class="lt-branch" data-k="${kp}${br}"${ltOpen(kp + br, true) ? ' open' : ''}><summary><b>${esc(LT_BRANCH_TR[br])}</b><span class="dim sm">${bb.length} bot</span>`
      + `<span id="${px}-sum-${br}">${ltSumHtml(bb)}</span></summary><div class="lt-groups">`;
    for (const g of ['gunluk', 'korku', 'haftalik', 'aylik', 'serbest']) {
      const gb = bb.filter(b => b.niche === g);
      if (!gb.length) continue;
      const m = obj(obj(meta[br])[g]);
      const key = `${kp}${br}/${g}`;
      const fin = (num(m.passed) || 0) + (num(m.filled) || 0) || gb.length;
      h += `<details class="lt-group" data-k="${key}"${ltOpen(key, false) ? ' open' : ''}><summary><b>${esc(groups[g])}</b>`
        + `<span class="dim sm">${esc(hints[g])}</span><span class="sp"></span>`
        + (m.passed != null ? `<span class="dim sm" title="Gün gün evrimde sınavı geçen bot sayısı; eksik kalan yerler grubun en iyi 3 botunun klonlarıyla dolduruldu">${esc(m.passed)}/${esc(fin)} sınavı geçti</span>` : '')
        + `<span id="${px}-sum-${br}-${g}">${ltSumHtml(gb)}</span></summary><div class="lt-cards">${gb.map(b => ltCard(b, cr)).join('')}</div></details>`;
    }
    h += '</div></details>';
  }
  return h;
}
function ltHeader(F, job) {
  const dec = obj(F.decided), night = obj(F.night), t = obj(F.tree);
  const running = job && job.running;
  const parts = [];
  if (t.created_at) parts.push(`Ağaç kuruldu: ${esc(dmy(t.created_at))} (${esc(arr(t.range).join(' – '))} verisiyle)`);
  if (dec.date) parts.push(`Son sabah alımı: ${esc(dec.date)}${dec.error ? ` <span class="down">hata: ${esc(dec.error)}</span>` : ` · ${esc(dec.bought ?? 0)} alış${dec.fear ? ' · korku günü' : ''}`}`);
  if (night.day) parts.push(`Son gece öğrenmesi: ${esc(night.day)} · ${arr(night.changes).length} bot değişti`);
  return `<div class="lt-top"><span class="dim sm">${parts.join(' · ') || 'Ağaç henüz kurulmadı.'}</span><span class="sp"></span>`
    + (running ? `<span class="pill amber">${esc(obj(job.state).message || 'Ağaç kuruluyor…')}</span>`
      : `<button type="button" class="btn sm" data-act="lab-tree">${icon('play')} ${arr(F.bots).length ? 'Ağacı yeniden kur' : 'Ağacı kur'}</button>`)
    + '</div>';
}

function ctHeader(F) {
  const job = obj(F.job), T = obj(job.tree), dl = obj(job.download), dec = obj(F.decided), night = obj(F.night), t = obj(F.tree), data = obj(F.data);
  const parts = [];
  if (data.symbols) parts.push(`Veri: ${esc(data.symbols)} coin (${esc(data.gone || 0)} tanesi borsadan çıkmış), ${esc(data.first || '')} → ${esc(data.last || '')}`);
  if (t.created_at) parts.push(`Ağaç kuruldu: ${esc(dmy(t.created_at))}`);
  if (dec.date) parts.push(`Son alım günü: ${esc(dec.date)} · ${esc(dec.bought ?? 0)} alış${dec.fear ? ' · korku günü' : ''}${dec.fng != null ? ` · Korku-Açgözlülük ${esc(dec.fng)}` : ''}`);
  if (night.day) parts.push(`Son gece öğrenmesi: ${esc(night.day)} · ${arr(night.changes).length} bot değişti`);
  if (F.daily_error) parts.push(`<span class="down">Günlük adım bekliyor: ${esc(F.daily_error)}</span>`);
  const busy = job.tree_running ? (T.message || 'Kripto ağacı kuruluyor…') : job.download_running ? (dl.message || 'Kripto verisi indiriliyor…') : '';
  return `<div class="lt-top"><span class="dim sm">${parts.join(' · ') || 'Kripto ağacı henüz kurulmadı.'}</span><span class="sp"></span>`
    + (busy ? `<span class="pill amber">${esc(busy)}</span>`
      : `<button type="button" class="btn sm" data-act="crypto-tree">${icon('play')} ${arr(F.bots).length ? 'Kripto ağacını yeniden kur' : 'Kripto ağacını kur'}</button>`)
    + (T.state === 'error' && !job.tree_running ? `<div class="down sm">${esc(T.message)}</div>` : '')
    + '</div>';
}

PAGES.lab = {
  title: 'Test Laboratuvarı', poll: 10, bodyId: 'lab-tree',
  mount(view) {
    this.data = null; this.fleet = null; this.repSig = ''; this.testSig = ''; this.sweepSig = ''; this.evoSig = ''; this.learnSig = ''; this.nnSig = '';
    this.learn = null; this.treeSig = null; this.cardSig = {}; this.cfleet = null; this.ctSig = null; this.ctCardSig = {};
    view.innerHTML = '<div class="page page-lab">'
      + `<div class="pt-note">${icon('warn')}<span><b>Bot ağacı: 100 bot, her biri kendi sanal hesabıyla.</b> İki dal (kural botları ve sinir ağları) × beş grup (günlük, korku günü, haftalık, aylık, serbest) × 10 bot. Her grupta 2 bot 100 $, 8 bot 100.000 $ hesapla işlem yapar. Botlar 10 yıllık geçmişi gün gün yaşayıp her gün eleme yapılarak seçildi; her sabah açılıştan sonra kendi hisselerini alır, her gece canlı sonuçlara göre her grubun en zayıfı emekli olur, yerine en iyi 3'ün klonu gelir. Sadece sanal para; geçmiş sonuçlar gelecekteki getiriyi garanti etmez.</span></div>`
      + '<div id="page-err"></div>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Bot ağacı</h3></div><div id="lab-tree-top"></div><div id="lab-tree">' + stateHtml('loading') + '</div></section>'
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Kripto ağacı</h3><span class="dim sm">40 bot · her grupta 1 bot 100 bin $, 3 bot 100 $ · 7/24, bilgisayar ve uygulama açıkken · sadece sanal para</span></div>`
      + '<div id="ct-tree-top"></div><div id="ct-tree">' + stateHtml('loading') + '</div></section>'
      + '<details id="lab-adv" class="lab-adv"><summary>Gelişmiş araçlar (veri, testler, eski canlı öğrenme)</summary>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Eski canlı öğrenme (emekli)</h3></div><div id="lab-learn">' + stateHtml('loading') + '</div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Veri</h3><span class="sp"></span><span id="lab-dl-btns" class="lab-btns"></span></div><div id="lab-data">' + stateHtml('loading') + '</div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Test</h3></div><div id="lab-test"></div></section>'
      + '<div id="lab-report"></div>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Toplu deneme: çıkış kuralları ve tutma süreleri</h3></div><div id="lab-sweep"></div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Evrim turnuvası</h3></div><div id="lab-evo"></div></section>'
      + '<section class="panel"><div class="ph-row"><h3 class="ph">Sinir ağı evrimi</h3></div><div id="lab-nn"></div></section>'
      + `<section class="panel"><div class="ph-row"><h3 class="ph">Bugünün hakem kararları</h3><span class="sp"></span>`
      + `<button type="button" class="btn sm" data-act="lab-today" id="lab-today-btn">${icon('play')} Kararları hesapla</button></div><div id="lab-today"></div></section>`
      + '</details></div>';
    const adv = byId('lab-adv');
    if (adv) adv.addEventListener('toggle', () => { if (adv.open) refreshPage(false); });
    view.addEventListener('toggle', e => {
      const el = e.target;
      if (!el.matches || !el.matches('details[data-k]')) return;
      const st = obj(store.get('labTree'));
      st[el.dataset.k] = el.open;
      store.set('labTree', st);
    }, true);
  },
  async refresh(first, live) {
    const advOpen = !!(byId('lab-adv') && byId('lab-adv').open);
    const [F, C, d, L] = await Promise.all([api('/api/fleet', { timeout: 30000 }),
      api('/api/crypto/fleet', { timeout: 30000 }).catch(() => null),
      advOpen ? api('/api/lab', { timeout: 30000 }) : Promise.resolve(null),
      advOpen ? api('/api/learn', { timeout: 30000 }).catch(() => null) : Promise.resolve(null)]);
    if (!live()) return;
    this.fleet = F;
    this.cfleet = C;
    if (d) this.data = d;
    if (L) this.learn = L;
    this.render();
  },
  renderCrypto() {
    const C = this.cfleet;
    if (!C) return;
    setHtml('ct-tree-top', ctHeader(C));
    const bots = arr(C.bots);
    const tsig = bots.map(b => b.id).join(',');
    if (tsig !== this.ctSig || !(byId('ct-tree') && byId('ct-tree').querySelector('.lt-branch, .state'))) {
      this.ctSig = tsig;
      this.ctCardSig = {};
      setHtml('ct-tree', bots.length ? ltTreeHtml(C, true) : '<div class="state">Henüz kripto botu yok. "Kripto ağacını kur" Binance geçmişini (2018 → bugün) gün gün yaşatıp her grubun en iyi 4 botunu seçer, sonra 40 botun hesaplarını açar. Hisse ağacı kurulurken bekler, bitince kendiliğinden başlar.</div>');
      for (const b of bots) this.ctCardSig[b.id] = ltCardSig(b);
      return;
    }
    for (const b of bots) {
      const sig = ltCardSig(b);
      if (sig === this.ctCardSig[b.id]) continue;
      this.ctCardSig[b.id] = sig;
      const el = document.querySelector(`[data-bot="${CSS.escape(b.id)}"]`);
      if (!el) continue;
      const wasOpen = !!(el.querySelector('details') && el.querySelector('details').open);
      el.outerHTML = ltCard(b, true);
      if (wasOpen) { const n = document.querySelector(`[data-bot="${CSS.escape(b.id)}"] details`); if (n) n.open = true; }
    }
    for (const br of ['kural', 'sinir']) {
      const bb = bots.filter(b => b.branch === br);
      setHtml(`ct-sum-${br}`, ltSumHtml(bb));
      for (const g of ['gunluk', 'korku', 'haftalik', 'aylik', 'serbest']) setHtml(`ct-sum-${br}-${g}`, ltSumHtml(bb.filter(b => b.niche === g)));
    }
  },
  render() {
    this.renderCrypto();
    const F = this.fleet;
    if (F) {
      setHtml('lab-tree-top', ltHeader(F, F.job));
      const bots = arr(F.bots);
      const tsig = bots.map(b => b.id).join(',');
      if (tsig !== this.treeSig || !(byId('lab-tree') && byId('lab-tree').querySelector('.lt-branch, .state'))) {
        this.treeSig = tsig;
        this.cardSig = {};
        setHtml('lab-tree', bots.length ? ltTreeHtml(F) : '<div class="state">Henüz bot yok. "Ağacı kur" 10 yıllık gün gün evrimi iki dal için çalıştırır (birkaç saat sürer), sonra 100 botun hesaplarını açar.</div>');
        for (const b of bots) this.cardSig[b.id] = ltCardSig(b);
      } else {
        for (const b of bots) {
          const sig = ltCardSig(b);
          if (sig === this.cardSig[b.id]) continue;
          this.cardSig[b.id] = sig;
          const el = document.querySelector(`[data-bot="${CSS.escape(b.id)}"]`);
          if (!el) continue;
          const wasOpen = !!(el.querySelector('details') && el.querySelector('details').open);
          el.outerHTML = ltCard(b);
          if (wasOpen) { const n = document.querySelector(`[data-bot="${CSS.escape(b.id)}"] details`); if (n) n.open = true; }
        }
        for (const br of ['kural', 'sinir']) {
          const bb = bots.filter(b => b.branch === br);
          setHtml(`lt-sum-${br}`, ltSumHtml(bb));
          for (const g of ['gunluk', 'korku', 'haftalik', 'aylik', 'serbest']) setHtml(`lt-sum-${br}-${g}`, ltSumHtml(bb.filter(b => b.niche === g)));
        }
      }
    }
    const d = this.data;
    if (!d || !(byId('lab-adv') && byId('lab-adv').open)) return;
    const L = this.learn;
    if (L) {
      const lsig = JSON.stringify([L.settings, L.night, L.running, obj(L.report).ts, obj(L.live).ts, L.counts, L.versions, L.trading_arm,
        obj(L.firsat).days, obj(L.firsat).count, obj(L.mikro).days, obj(L.mikro).count, L.last_night]);
      if (lsig !== this.learnSig) {
        this.learnSig = lsig;
        const open = [...document.querySelectorAll('#lab-learn details')].map(x => x.open);
        setHtml('lab-learn', learnPanel(L));
        document.querySelectorAll('#lab-learn details').forEach((x, i) => { if (open[i]) x.open = true; });
      }
    }
    setHtml('lab-data', labData(d));
    setHtml('lab-dl-btns', labButtons(d));
    const tsig = [d.test_running, obj(d.test).state, obj(d.test).message, d.download_running, obj(d.report).created_at].join('|');
    if (tsig !== this.testSig) { this.testSig = tsig; setHtml('lab-test', labTestPanel(d)); }
    const rsig = String(obj(d.report).created_at || '');
    if (rsig !== this.repSig) { this.repSig = rsig; setHtml('lab-report', labReport(d.report)); }
    const ssig = [d.sweep_running, obj(d.sweep).message, obj(d.sweep).done, obj(d.sweep_report).created_at, d.test_running, d.download_running].join('|');
    if (ssig !== this.sweepSig) { this.sweepSig = ssig; setHtml('lab-sweep', labSweep(d)); }
    const esig = [d.evo_running, obj(d.evo).message, obj(d.evo_report).created_at, d.test_running, d.sweep_running, d.download_running,
      d.audit_running, obj(d.audit).message, obj(d.intraday).created_at, obj(d.evo_today).ts, JSON.stringify(obj(d.evo_stability).dominant || {})].join('|');
    if (esig !== this.evoSig) { this.evoSig = esig; setHtml('lab-evo', labEvo(d)); }
    const nsig = [d.nn_running, obj(d.nn).message, obj(d.nn_report).created_at, obj(d.nn_night).ts, arr(d.nn_population).length,
      d.evo_running, d.download_running].join('|');
    if (nsig !== this.nnSig) { this.nnSig = nsig; setHtml('lab-nn', labNN(d)); }
    const today = obj(obj(d.live).today);
    if (!this.todayShown && today.rows) setHtml('lab-today', labToday(today));
  },
  onError(e) { this.treeSig = null; this.ctSig = null; },
  cryptoStart() {
    const has = arr(obj(this.cfleet).bots).length;
    if (HAS_DOM && !window.confirm(`${has ? 'Kripto ağacı baştan kurulsun mu? Şu anki 40 kripto botu emekliye ayrılır (geçmişleri saklanır).' : 'Kripto ağacı kurulsun mu?'} Binance geçmişi (2018 → bugün) iki dal için gün gün yaşanır. Hisse ağacı kuruluyorsa önce onun bitmesi beklenir. Sadece sanal para.`)) return;
    api('/api/crypto/tree', { method: 'POST' })
      .then(() => { toast({ title: 'Kripto ağacı kuruluyor', body: 'Hisse ağacı kuruluyorsa bitmesi beklenir, sonra kural ve sinir ağı dalları gün gün yaşar.', kind: 'ok' }); this.ctSig = null; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  treeStart() {
    const has = arr(obj(this.fleet).bots).length;
    if (HAS_DOM && !window.confirm(`${has ? 'Bot ağacı baştan kurulsun mu? Şu anki 100 bot emekliye ayrılır (geçmişleri saklanır).' : 'Bot ağacı kurulsun mu?'} 10 yıllık gün gün evrim iki dal için çalışır; bilgisayarı birkaç saat yorar. Bitince 100 botun hesapları açılır ve eski botlar kapanır.`)) return;
    api('/api/lab/tree', { method: 'POST' })
      .then(() => { toast({ title: 'Bot ağacı kuruluyor', body: 'Önce 10 yıllık havuz hazırlanır, sonra kural ve sinir ağı dalları gün gün yaşar.', kind: 'ok' }); refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: e.message, kind: 'err' }));
  },
  nnStart(runs) {
    if (HAS_DOM && !window.confirm(`Sinir ağı evrimi başlasın mı? ${runs > 1 ? `${runs} bağımsız çalıştırma art arda (~${runs}×45 dk)` : '30–60 dakika'}; bilgisayarı yorar. Sınavı geçen ağlar popülasyona eklenir.`)) return;
    api('/api/lab/nn_evolve', { method: 'POST', body: { runs } })
      .then(() => { toast({ title: 'Sinir ağı evrimi başladı', body: 'Önce 10 yıllık havuz hazırlanır (birkaç dk), sonra nesiller ilerler.', kind: 'ok' }); this.nnSig = ''; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  async startDownload(update) {
    try {
      await api('/api/lab/download', { method: 'POST', body: { update: !!update } });
      toast({ title: update ? 'Günlük güncelleme başladı' : 'Veri indirme başladı', body: update ? '' : 'Finnhub dakikada 60 istekle sınırlı; tamamı 2–3 saat sürebilir. Durdurursanız kaldığı yerden devam eder.', kind: 'ok', timeout: 9000 });
    } catch (e) { toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }); }
    refreshPage(false);
  },
  async runTest() {
    const tmSel = document.querySelector('#lab-test select[data-f="labTm"]');
    const body = { sl_pct: ptNum('lab-sl'), tp_pct: ptNum('lab-tp'), slippage_bps: ptNum('lab-slip'), test_months: tmSel ? Number(tmSel.value) : 2 };
    if (['sl_pct', 'tp_pct', 'slippage_bps'].some(k => Number.isNaN(body[k]))) { toast({ title: 'Geçersiz sayı', kind: 'err' }); return; }
    try {
      await api('/api/lab/test', { method: 'POST', body });
      toast({ title: 'Test başladı', body: 'Botlar 5 yıllık veride çalışıyor, sonra hakem eğitiliyor.', kind: 'ok' });
    } catch (e) { toast({ title: 'Test başlatılamadı', body: errMsg(e), kind: 'err' }); }
    this.testSig = '';
    refreshPage(false);
  },
  onFilter(f, v, type) {
    if (type !== 'change' || (f !== 'learnSpeed' && f !== 'learnAuto')) return;
    this.learnSettings(f === 'learnSpeed' ? { speed: String(v) } : { auto_apply: !!v });
  },
  async learnSettings(body) {
    try {
      await api('/api/learn/settings', { method: 'POST', body });
      toast({ title: 'Öğrenme ayarı kaydedildi', body: 'auto_apply' in body ? (body.auto_apply ? 'Öğrenilen ertesi gün otomatik uygulanacak.' : 'Öğrenme gölgede sürecek; sanal hesaptaki ayarlar değişmeyecek.') : '', kind: 'ok' });
    } catch (e) { toast({ title: 'Kaydedilemedi', body: errMsg(e), kind: 'err' }); }
    this.learnSig = '';
    refreshPage(false);
  },
  async learnRun() {
    try {
      const r = await api('/api/learn/run', { method: 'POST', body: {} });
      toast({ title: 'Gece öğrenmesi başladı', body: `${r.day || ''} verisiyle: 10 yıllık havuz birkaç dakikada hazırlanır, sonra klonlar sınava girer.`, kind: 'ok', timeout: 9000 });
    } catch (e) { toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }); }
    this.learnSig = '';
    refreshPage(false);
  },
  async learnRollback() {
    if (HAS_DOM && !window.confirm('Son öğrenme değişikliği (evrim botları ve bot ayarları) geri alınsın mı? Otomatik uygulama da durdurulur; öğrenme gölgede sürer.')) return;
    try {
      const r = await api('/api/learn/rollback', { method: 'POST', body: { confirm: true } });
      toast({ title: 'Geri alındı', body: `${r.restored || ''} öncesine dönüldü; otomatik uygulama kapalı.`, kind: 'ok' });
    } catch (e) { toast({ title: 'Geri alınamadı', body: errMsg(e), kind: 'err' }); }
    this.learnSig = '';
    refreshPage(false);
  },
  async today() {
    const b = byId('lab-today-btn');
    if (b) { b.disabled = true; b.classList.add('busy'); }
    setHtml('lab-today', `<p class="muted sm">Hesaplanıyor: ~460 hissenin açılış fiyatı alınıyor…</p>`);
    try {
      const t = await api('/api/lab/today', { method: 'POST', body: {}, timeout: 120000 });
      this.todayShown = true;
      setHtml('lab-today', labToday(t));
    } catch (e) {
      setHtml('lab-today', `<p class="down sm">${esc(errMsg(e))}</p>`);
    } finally { if (b) { b.disabled = false; b.classList.remove('busy'); } }
  },
};

// ============================================================ global actions & events

function applyTheme(t) {
  t = t === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  store.set('theme', t);
}
function toggleSidebar() {
  const on = !document.documentElement.classList.contains('sb-collapsed');
  document.documentElement.classList.toggle('sb-collapsed', on);
  store.set('sbCollapsed', on);
}
function manualRefresh(all, btn) {
  if (all) refreshSources('', btn);
  runNow('status');
  if (cur) { cur.page.sig = ''; refreshPage(false); }
  const b = byId('btn-refresh');
  if (b) { b.classList.add('spinning'); setTimeout(() => b.classList.remove('spinning'), 900); }
}
function focusSearch() {
  const el = document.querySelector('#view [data-primary-search]') || byId('gsearch');
  if (el) { el.focus(); if (el.select) el.select(); }
}
/** Global search: a ticker when it looks like one ($NVDA, nvda, BRK.B, a watchlist/recent symbol), else '' (news search). */
function gsearchSym(v) {
  v = String(v || '').trim();
  if (!v || /\s/.test(v)) return '';
  const sym = normSym(v);
  if (!sym) return '';
  if (/^\$[A-Za-z]/.test(v) || wlSet().has(sym) || recentTickers().includes(sym)) return sym;
  return /^[A-Za-z]{1,5}([.\-][A-Za-z])?$/.test(v) ? sym : '';
}
function remountNews() { if (cur && cur.key === 'haberler') route(true); else go('haberler'); }

const ACTIONS = {
  retry: () => refreshPage(false),
  skip: () => { const v = byId('view'); if (v) v.focus(); },
  refresh: (el, e) => manualRefresh(!!(e && e.shiftKey) || el.dataset.all === '1', null),
  'refresh-all': el => manualRefresh(true, el),
  theme: () => applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'),
  sidebar: () => toggleSidebar(),
  'news-columns': () => { NF.columns = !NF.columns; saveNF(); remountNews(); },
  'news-reset': () => { nfReset(); remountNews(); },
  'news-wider': () => { NF.since_hours = 72; saveNF(); remountNews(); },
  'news-more': () => { NF.limit = 500; PAGES.haberler.sig = ''; refreshPage(false); },
  'news-top': el => { const v = byId('view'); if (v) v.scrollTop = 0; el.hidden = true; },
  'nf-clear': el => { const k = el.dataset.k; NF[k] = NF_BLANK[k] !== undefined ? JSON.parse(JSON.stringify(NF_BLANK[k])) : ''; remountNews(); },
  'src-open': () => { NF.srcOpen = !NF.srcOpen; saveNF(); setHtml('news-src', newsSrcBar()); },
  'src-chip': el => {
    const k = el.dataset.key;
    NF.sources = NF.sources.includes(k) ? NF.sources.filter(x => x !== k) : [...NF.sources, k];
    NF.group = '';
    setHtml('news-src', newsSrcBar());
    PAGES.haberler.reload();
  },
  'src-group': el => {
    const g = el.dataset.g;
    NF.sources = S.sources.filter(s => s.group === g).map(s => s.source);
    NF.group = '';
    setHtml('news-src', newsSrcBar());
    PAGES.haberler.reload();
  },
  'src-clear': () => { NF.sources = []; NF.group = ''; setHtml('news-src', newsSrcBar()); PAGES.haberler.reload(); },
  'op-dir': el => PAGES.firsatlar.setDir(el.dataset.v || ''),
  'op-news': el => PAGES.firsatlar.toggleNews(el.dataset.sym),
  'mv-src': el => { PAGES.pano.mvSrc = el.dataset.v === 'alpaca' ? 'alpaca' : 'yahoo'; PAGES.pano.render(); },
  'so-src': el => { PAGES.pano.soSrc = el.dataset.v === 'wsb' ? 'wsb' : 'all'; setHtml('d-social', socialHtml(obj(PAGES.pano.data).social, PAGES.pano.soSrc)); },
  'cal-kind': el => PAGES.takvim.setKind(el.dataset.v || ''),
  'cal-more': el => { const d = el.dataset.day, ex = PAGES.takvim.expanded; if (ex.has(d)) ex.delete(d); else ex.add(d); PAGES.takvim.render(); },
  'brief-sel': el => PAGES.brifingler.select(el.dataset.id),
  'brief-gen': el => PAGES.brifingler.generate(el),
  'brief-export': el => PAGES.brifingler.exportClaude(el),
  'brief-copy': () => { const b = arr(PAGES.brifingler.data).find(x => x.id === BF.sel); if (b) copyText(b.body_md || ''); },
  'copy-text': el => copyText(el.dataset.text || ''),
  'open-folder': el => openFolder(el.dataset.which || 'data'),
  'wl-toggle': el => toggleWatch(el.dataset.sym, el),
  'tk-news': el => { nfReset(); NF.ticker = normSym(el.dataset.sym); go('haberler'); },
  'src-refresh': el => refreshSources(el.dataset.src, el),
  'src-refresh-all': el => refreshSources('', el),
  'cfg-save': () => PAGES.ayarlar.save(),
  'secret-clear': el => PAGES.ayarlar.clearSecret(el.dataset.key, el),
  'wl-add': () => PAGES.ayarlar.addWl(),
  'wl-remove': el => PAGES.ayarlar.removeWl(el.dataset.sym),
  'notif-perm': () => askNotify(),
  'app-shutdown': el => shutdownApp(el),
  'pt-side': el => { PT.side = el.dataset.v === 'sell' ? 'sell' : 'buy'; PAGES.islem.syncForm(); },
  'pt-save': () => PAGES.islem.saveSettings(),
  'pt-bot-toggle': () => {
    const on = !!obj(obj(PAGES.islem.data).settings).bot_enabled;
    if (!on && HAS_DOM && !window.confirm('Sanal bot başlatılsın mı? Yalnızca sanal parayla, ABD seansı açıkken ve risk limitleri içinde işlem yapar.')) return;
    PAGES.islem.post('/api/paper/settings', { bot_enabled: !on }, on ? 'Bot durduruldu' : 'Bot başlatıldı');
  },
  'pt-reset': () => {
    const v = ptNum('pt-start');
    if (Number.isNaN(v)) { toast({ title: 'Geçersiz başlangıç bakiyesi', kind: 'err' }); return; }
    if (HAS_DOM && !window.confirm('Sanal hesap sıfırlansın mı? Tüm sanal emirler, pozisyonlar ve geçmiş silinir.')) return;
    PAGES.islem.botSig = '';
    PAGES.islem.post('/api/paper/reset', { confirm: true, start_cash: v }, 'Sanal hesap sıfırlandı');
  },
  'pt-close': el => {
    const sym = el.dataset.sym;
    if (HAS_DOM && !window.confirm(`${sym} pozisyonu piyasa emriyle kapatılsın mı?`)) return;
    PAGES.islem.post('/api/paper/close', { symbol: sym }, `${sym} kapatma emri gönderildi`);
  },
  'pt-close-all': () => {
    if (HAS_DOM && !window.confirm('Tüm sanal pozisyonlar piyasa emriyle kapatılsın mı?')) return;
    PAGES.islem.post('/api/paper/close', { all: true }, 'Tüm pozisyonlar için kapatma emri gönderildi');
  },
  'pt-cancel': el => PAGES.islem.post('/api/paper/cancel', { id: el.dataset.id }, 'Emir iptal edildi'),
  'pt-levels': el => PAGES.islem.levels(el.dataset.sym),
  'sc-run': () => PAGES.karne.run(),
  'micro-reset': async () => {
    if (HAS_DOM && !window.confirm('100 $ bütçeli mikro sanal hesap sıfırlansın mı? (tüm mikro işlemler silinir)')) return;
    try {
      await api('/api/paper/micro/reset', { method: 'POST', body: { confirm: true } });
      toast({ title: 'Mikro hesap sıfırlandı', body: 'Mikro hesap 100 $ başlangıç bakiyesine döndü.', kind: 'ok' });
      refreshPage(false);
    } catch (e) {
      toast({ title: 'Sıfırlanamadı', body: errMsg(e), kind: 'err' });
    }
  },
  'real-reset': async () => {
    if (HAS_DOM && !window.confirm('Gerçekçi 100 $ hesabı sıfırlansın mı? (tüm işlemleri silinir)')) return;
    try {
      await api('/api/paper/real/reset', { method: 'POST', body: { confirm: true } });
      toast({ title: 'Gerçekçi hesap sıfırlandı', body: 'Hesap 100 $ başlangıç bakiyesine döndü.', kind: 'ok' });
      refreshPage(false);
    } catch (e) {
      toast({ title: 'Sıfırlanamadı', body: errMsg(e), kind: 'err' });
    }
  },
  'real-toggle': async () => {
    try {
      const on = !obj(PAGES.islem.realData).enabled;
      await api('/api/paper/real/settings', { method: 'POST', body: { enabled: on } });
      toast({ title: on ? 'Gerçekçi hesap başlatıldı' : 'Gerçekçi hesap durduruldu', kind: 'ok' });
      refreshPage(false);
    } catch (e) {
      toast({ title: 'Değiştirilemedi', body: errMsg(e), kind: 'err' });
    }
  },
  'learn-run': () => PAGES.lab.learnRun(),
  'learn-rollback': () => PAGES.lab.learnRollback(),
  'lab-download': () => {
    if (HAS_DOM && !window.confirm('5 yıllık veri indirilsin mi? (~460 hisse; Finnhub sınırı yüzünden 2–3 saat sürebilir, arka planda çalışır)')) return;
    PAGES.lab.startDownload(false);
  },
  'lab-update': () => PAGES.lab.startDownload(true),
  'lab-stop': () => { api('/api/lab/stop', { method: 'POST', body: {} }).then(() => toast({ title: 'İndirme durduruluyor', kind: 'info' })).catch(e => toast({ title: 'Durdurulamadı', body: errMsg(e), kind: 'err' })); },
  'lab-test': () => PAGES.lab.runTest(),
  'lab-today': () => PAGES.lab.today(),
  'lab-evolve3': () => {
    if (HAS_DOM && !window.confirm('3 bağımsız turnuva arka arkaya çalıştırılsın mı? (~1–1,5 saat; uygulama açık kalmalı)')) return;
    api('/api/lab/evolve', { method: 'POST', body: { runs: 3 } })
      .then(() => { toast({ title: '3 turnuva başladı', body: 'Farklı rastgele başlangıçlarla; sonunda kural aileleri karşılaştırılır.', kind: 'ok' }); PAGES.lab.evoSig = ''; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  'lab-audit': () => {
    api('/api/lab/audit', { method: 'POST', body: {} })
      .then(() => { toast({ title: 'Gün içi denetim başladı', body: '~120 hissenin 60 günlük 5 dakikalık verisi indiriliyor.', kind: 'ok' }); PAGES.lab.evoSig = ''; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  'lab-evo-today': async () => {
    const b = byId('lab-evo-today');
    if (b) { b.disabled = true; b.classList.add('busy'); }
    setHtml('lab-evo-today-box', '<p class="muted sm">Hesaplanıyor: ~2.600 hissenin açılış fiyatı ve göstergeleri (2–3 dk)…</p>');
    try { const t = await api('/api/lab/evo_today', { method: 'POST', body: {}, timeout: 400000 }); setHtml('lab-evo-today-box', evoToday(t)); }
    catch (e) { setHtml('lab-evo-today-box', `<p class="down sm">${esc(errMsg(e))}</p>`); }
    finally { if (b) { b.disabled = false; b.classList.remove('busy'); } }
  },
  'lab-nn3': () => PAGES.lab.nnStart(3),
  'lab-nn': () => PAGES.lab.nnStart(1),
  'lab-tree': () => PAGES.lab.treeStart(),
  'crypto-tree': () => PAGES.lab.cryptoStart(),
  'lab-nn-today': async () => {
    setHtml('lab-nn-today', '<p class="muted sm">Hesaplanıyor: ~2.600 hissenin açılış göstergeleri (2–3 dk)…</p>');
    try {
      const t = await api('/api/lab/nn_today', { method: 'POST', body: {}, timeout: 400000 });
      setHtml('lab-nn-today', `<p class="muted sm">${esc(t.date)} seçimleri (emir açılmaz):</p>` + arr(t.items).map(x => `<div class="sm"><b>${esc(x.id)}</b> · ${esc(x.exit)}: `
        + (arr(x.picks).length ? arr(x.picks).map(p => `<button type="button" class="tk" data-tk="${esc(p.symbol)}">${esc(p.symbol)}</button>`).join(' ') : '<span class="dim">bugün seçim yok</span>') + '</div>').join(''));
    } catch (e) { setHtml('lab-nn-today', `<p class="down sm">${esc(errMsg(e))}</p>`); }
  },
  'lab-evolve': () => {
    api('/api/lab/evolve', { method: 'POST', body: {} })
      .then(() => { toast({ title: 'Turnuva başladı', body: 'Önce ~2.600 hissenin havuz verisi hazırlanır (birkaç dk), sonra 4 aşama ilerler.', kind: 'ok' }); PAGES.lab.evoSig = ''; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  'lab-sweep': () => {
    const sel = document.querySelector('#lab-test select[data-f="labTm"]');
    api('/api/lab/sweep', { method: 'POST', body: { test_months: sel ? Number(sel.value) : 2 } })
      .then(() => { toast({ title: 'Toplu deneme başladı', body: '24 ayar deneniyor; 5–10 dakika sürebilir.', kind: 'ok' }); PAGES.lab.sweepSig = ''; refreshPage(false); })
      .catch(e => toast({ title: 'Başlatılamadı', body: errMsg(e), kind: 'err' }));
  },
  'sec-tip-x': () => { store.set('secTipDismissed', true); setHtml('news-tip', ''); },
  'need-config': (el, e) => { if (e) e.preventDefault(); goSecSetting(); },
  'alerts-news': el => { const t = el.closest('.toast'); if (t) t.remove(); NF.order = 'fetched'; saveNF(); remountNews(); },
  'beep-test': () => beep(true),
  'alert-test': () => {
    toast({ kind: 'alert', title: 'Deneme uyarısı: önemli haberler burada görünür', meta: `Piyasa Masası · önem 90 ${tkChip('SPY')}` });
    desktopNotify('Piyasa Masası — deneme uyarısı', 'Masaüstü bildirimleri çalışıyor.', '', 'pm-test');
    beep();
  },
};

function bindEvents() {
  document.addEventListener('click', e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    const tx = t.closest('.t-x');
    if (tx) { const el = tx.closest('.toast'); if (el) { el.remove(); return; } }
    const tk = t.closest('[data-tk]');
    if (tk) { e.preventDefault(); goTicker(tk.dataset.tk); const ts = tk.closest('.toast'); if (ts) ts.remove(); return; }
    const act = t.closest('[data-act]');
    if (act) {
      if (act.tagName === 'INPUT' || act.tagName === 'SELECT') return;
      const fn = ACTIONS[act.dataset.act];
      if (fn) { e.preventDefault(); fn(act, e); }
      return;
    }
    const cid = t.closest('[data-cid]');
    if (cid) { e.preventDefault(); go('kim-ne-diyor/' + cid.dataset.cid); return; }
    // Category tags / source badges filter the news feed (added to current filters when already there).
    const onNews = cur && cur.key === 'haberler';
    const cat = t.closest('button.cat[data-cat]');
    if (cat) {
      e.preventDefault();
      if (!onNews) nfReset();
      NF.category = cat.dataset.cat;
      remountNews();
      return;
    }
    const src = t.closest('button.srcb[data-src]');
    if (src && src.dataset.src) {
      e.preventDefault();
      if (!onNews) nfReset();
      NF.sources = [src.dataset.src];
      NF.group = '';
      remountNews();
      return;
    }
    const toastEl = t.closest('.toast.link');
    if (toastEl && !t.closest('a')) { openExternal(toastEl.dataset.url); toastEl.remove(); }
  });
  document.addEventListener('input', e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.matches('.range input[type="range"]')) { const o = t.parentElement.querySelector('output'); if (o) o.textContent = t.value; }
    if (t.matches('input[type="time"][name^="bt_"]')) { const s = document.querySelector(`[data-trof="${t.name}"]`); if (s) s.textContent = etToTr(t.value); }
    if (t.closest('#cfg-form')) debounce('dirty', () => PAGES.ayarlar.markDirty(), 120);
    if (t.dataset.f && cur && cur.page.onFilter) cur.page.onFilter(t.dataset.f, t.type === 'checkbox' ? t.checked : t.value, 'input', t);
  });
  document.addEventListener('change', e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.dataset.pref) {
      S.prefs[t.dataset.pref] = t.checked;
      store.set('prefs', S.prefs);
      if (t.dataset.pref === 'notify' && t.checked && !notifGranted()) toast({ title: 'Bildirimler için önce “İzin iste” düğmesine basın', body: notifState(), kind: 'info' });
      if (t.dataset.pref === 'beep' && t.checked) beep(true);
      return;
    }
    if (t.matches('[data-act="src-toggle"]')) { toggleSource(t); return; }
    if (t.closest('#cfg-form')) PAGES.ayarlar.markDirty();
    if (t.dataset.f && cur && cur.page.onFilter) cur.page.onFilter(t.dataset.f, t.type === 'checkbox' ? t.checked : t.value, 'change', t);
  });
  document.addEventListener('submit', e => {
    e.preventDefault();
    const f = e.target;
    const inp = f.querySelector('input');
    const v = inp ? inp.value.trim() : '';
    if (f.id === 'gsearch-form') {
      if (!v) return;
      const sym = gsearchSym(v);
      if (sym) { S.gsearch = /^\$/.test(v) ? null : { sym, text: v }; goTicker(sym); }
      else { nfReset(); NF.q = v; remountNews(); }
      inp.value = '';
      inp.blur();
    } else if (f.id === 'pt-form') {
      PAGES.islem.submitOrder();
    } else if (f.id === 'tk-form') {
      const sym = normSym(v);
      if (sym) goTicker(sym); else toast({ title: 'Geçersiz sembol', body: v, kind: 'err' });
    }
  });
  document.addEventListener('keydown', e => {
    const t = e.target;
    // Checkboxes, sliders and buttons keep the shortcuts working; only text-like fields swallow keys.
    const tt = String((t && t.type) || '').toLowerCase();
    const typing = !!t && (t.isContentEditable || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
      || (t.tagName === 'INPUT' && !/^(checkbox|radio|range|button|submit|reset|color|file)$/.test(tt)));
    if (e.key === 'Escape') {
      if (typing) t.blur();
      else { const ts = byId('toasts'); if (ts) ts.innerHTML = ''; }
      return;
    }
    if (typing) {
      if (e.key === 'Enter' && t.id === 'wl-input') { e.preventDefault(); PAGES.ayarlar.addWl(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^[1-9]$/.test(e.key)) {
      e.preventDefault();
      const k = NAV_KEYS[+e.key - 1];
      if (k === 'hisse') { const r = recentTickers()[0]; go(r ? 'hisse/' + r : 'hisse'); } else go(k);
      return;
    }
    if (e.key === '/') { e.preventDefault(); focusSearch(); return; }
    if (e.key === 'r' || e.key === 'R') { e.preventDefault(); if (!e.repeat) manualRefresh(e.shiftKey, null); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    for (const t of tasks) if (t.name === 'status' || t.name === 'alerts' || t.name === 'badges') t.last = 0;
    if (cur) cur.last = 0;
  });
  const view = byId('view');
  if (view) view.addEventListener('scroll', () => { if (view.scrollTop < 10) { const p = byId('news-newpill'); if (p && !p.hidden) p.hidden = true; } }, { passive: true });
}

function boot() {
  applyTheme(store.get('theme', 'dark'));
  if (store.get('sbCollapsed', false)) document.documentElement.classList.add('sb-collapsed');
  bindEvents();
  updateHisseLink();
  window.addEventListener('hashchange', () => route(false));
  every('status', POLL.status, loadStatus);
  every('meta', POLL.meta, loadMeta, { retry: 15 });
  every('sources', POLL.sources, loadSources, { retry: 20 });
  every('config', POLL.config, loadConfig, { retry: 20 });
  every('alerts', POLL.alerts, pollAlerts, { whenHidden: () => S.prefs.notify && notifGranted() });
  every('badges', POLL.badges, pollBadges);
  every('opps', POLL.opps, pollOpps);
  route(true);
  tick();
  S.tickTimer = setInterval(tick, 1000);
}

// ============================================================ exports (node tests) & start

const API = {
  esc, num, fmtPrice, fmtPct, fmtSigned, fmtCompact, fmtCap, fmtInt, fmtInterval, md, mdInline, zp, zTs, hm, hms, rel, dur, whenShort, tsTitle, dayLabel,
  sessionTargets, localSession, guessNextOpen, etToTr, spark, gaugeSvg, newsRow, newsListHtml, colHtml, clusterCard, agreement, oppCard, quotesHtml, fgHtml, catalystHtml, haltsHtml,
  moverTable, watchHtml, socialHtml, calendarHtml, briefListHtml, briefHtml, tickerHtml, tickerEmpty, sourcesTableHtml, srcSummary, settingsFormHtml,
  wlEditorHtml, srcState, newsToolbar, newsSrcBar, nfChips, normSym, safeUrl, safeColor, S, NF, PAGES,
  fold, gsearchSym, uniqPub,
};
if (typeof module !== 'undefined' && module.exports) module.exports = API;
if (HAS_DOM) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}
})();
