'use strict';
/* Язык интерфейса. Код страниц пишет по-русски, а здесь надписи на экране переводятся по словарю
   (i18n-dict.js: строки [русский, English, нохчийн]). Содержимое записей — реплики, названия,
   краткое содержание, задачи, ответы ИИ — не трогаем: оно остаётся на языке разговора.
   Ключ с {n}, {t}, {x} — шаблон: подставленное значение тоже переводится (длительность, этап…). */
(function () {
  let lang = 'ru';
  try { lang = JSON.parse(localStorage.getItem('lang')) || 'ru'; } catch (e) { /* по умолчанию русский */ }
  const col = { en: 1, ce: 2 }[lang];
  window.LANG = col ? lang : 'ru';
  document.documentElement.lang = window.LANG;
  window.tr = s => s;
  if (!col || typeof I18N_ROWS === 'undefined') return;

  const norm = s => s.replace(/\s+/g, ' ').trim();
  const exact = new Map(), pats = [];
  for (const row of I18N_ROWS) {
    const key = norm(row[0]), to = row[col];
    if (!to) continue;
    if (/\{\w+\}/.test(key)) {
      // {n} {a} {b} {h} {m} — только число; {t} (время, длительность) и {x} — что угодно
      const re = key.split(/(\{\w+\})/).map(part => {
        const m = part.match(/^\{(\w+)\}$/);
        if (!m) return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return /^[nabhm]$/.test(m[1]) ? `(?<${m[1]}>[\\d.,+%]+)` : `(?<${m[1]}>.+?)`;
      }).join('');
      pats.push({ re: new RegExp('^' + re + '$', 's'), to, weight: key.replace(/\{\w+\}/g, '').length });
    } else exact.set(key, to);
  }
  pats.sort((a, b) => b.weight - a.weight);          // сначала самые конкретные шаблоны
  const CYR = /[А-Яа-яЁё]/;

  function core(s, depth) {
    if (!s || !CYR.test(s)) return null;
    const hit = exact.get(s);
    if (hit != null) return hit;
    for (const p of pats) {
      const m = p.re.exec(s);
      if (m) return p.to.replace(/\{(\w+)\}/g, (_, k) => { const v = m.groups[k]; return (depth < 3 && core(v, depth + 1)) || v; });
    }
    if (depth > 2) return null;
    // составные строки: «этап · 62% · пауза…», несколько предложений подряд
    for (const sep of [/( ·(?: |$))/, /(?<=[.!?…])( )(?=[А-ЯЁA-Z«])/]) {
      const parts = s.split(sep);
      if (parts.length < 3) continue;
      let any = false;
      const out = parts.map((p, i) => { if (i % 2) return p; const t = core(p, depth + 1); if (t != null) any = true; return t != null ? t : p; });
      if (any) return out.join('');
    }
    return null;
  }

  function tr(s) {
    if (!s || !CYR.test(s)) return s;
    const t = core(norm(s), 0);
    if (t == null) return s;
    return s.match(/^\s*/)[0] + t + s.match(/\s*$/)[0];
  }
  window.tr = tr;

  // где показывается содержимое записей — не переводим
  const SKIP = '.tx, .live-line, .lead, .jobcard .t, .workcard .t, .job-head .title, .qa .q, .qa .a, .hit, .task .what,' +
    ' #sc li, .samples a, .modal textarea, textarea, script, style, [translate="no"]';
  const ATTRS = ['placeholder', 'title', 'aria-label'];

  function fixText(n) {
    const p = n.parentElement;
    if (!p || p.closest(SKIP)) return;
    const v = n.nodeValue, t = tr(v);
    if (t !== v) n.nodeValue = t;
  }
  function fixAttrs(el) {
    if (el.closest(SKIP)) return;
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v) { const t = tr(v); if (t !== v) el.setAttribute(a, t); }
    }
  }
  function walk(root) {
    if (root.nodeType === 3) return fixText(root);
    if (root.nodeType !== 1 || root.closest(SKIP)) return;
    fixAttrs(root);
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.nodeType === 1 && n.matches(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    let n;
    while ((n = it.nextNode())) { if (n.nodeType === 3) fixText(n); else fixAttrs(n); }
  }

  walk(document.body);
  new MutationObserver(list => {
    for (const m of list) {
      if (m.type === 'characterData') fixText(m.target);
      else if (m.type === 'attributes') fixAttrs(m.target);
      else m.addedNodes.forEach(walk);
    }
  }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  // системные окна подтверждения
  const confirm0 = window.confirm.bind(window), prompt0 = window.prompt.bind(window), alert0 = window.alert.bind(window);
  window.confirm = m => confirm0(tr(String(m)));
  window.prompt = (m, d) => prompt0(tr(String(m)), d);
  window.alert = m => alert0(tr(String(m)));
})();
