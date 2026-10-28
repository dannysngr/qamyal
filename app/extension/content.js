'use strict';
/* Qamyal — кто говорит: на странице созвона следит за плитками участников. Когда у плитки появляется
   пометка «говорит» (класс со словами speak / talk / voice / active-speaker, aria-подпись «говорит»,
   атрибут data-speaking), берёт имя с плитки и сообщает Qamyal: speak_start / speak_end — в том же виде,
   что журнал Skype для бизнеса. Плюс «разведка» (kind: dom): какие классы у каких элементов
   включаются и выключаются — по ней распознавание для сервиса потом уточняется.
   Ничего не нажимает и не меняет на странице. */
(() => {
  if (window.__qamyalSpeakers) return;
  window.__qamyalSpeakers = true;

  const SITE = location.hostname.replace(/^www\./, '');
  // пометка «говорит»: speaking, talking, active/dominant/current speaker, voice/audio active, speaker-active…
  // (без голого «speaker»: так называют и динамики, и режим «докладчик»)
  const SPEAK = /(^|[-_\s])(is-?)?(speaking|talking|now-?speaking|voice-?activ\w*|(active|dominant|current|main)-?speaker|audio-?activ\w*|speaker-?(is-?)?active|talker-?active)([-_\s]|$)/i;
  const SPEAK_LOOSE = /speak|talk|voice|audiolevel|audio-level|activespeaker|active-speaker|sound/i;
  const NOT_SPEAK = /speaker-?(view|mode|layout|stage|select|output|device|settings|test)|speakers?-?(list|button|icon-?off)|loud-?speaker|talkback/i;
  const ARIA_SPEAK = /(говорит|speaking|is talking|talking now|говорит сейчас|активный докладчик|active speaker)/i;
  const SELF = /\((вы|you|я|me)\)|\s(вы|you)$|^(вы|you)$/i;
  const UI_WORDS = /^(вы|you|я|mute|unmute|микрофон|камера|camera|microphone|звук|выключен|включен|ещё|more|pin|закрепить|host|организатор|гость|guest|\d+|[^\p{L}]+)$/iu;

  const out = [];
  const speaking = new Map();      // элемент → {uri, name}
  const people = new Set();
  let sending = false, recording = false, domBudget = 0;

  function send() {
    if (!out.length || sending) return;
    sending = true;
    const events = out.splice(0, out.length);
    try {
      chrome.runtime.sendMessage({ type: 'qamyal-events', events }, () => { void chrome.runtime.lastError; sending = false; });
    } catch { sending = false; }
  }
  setInterval(send, 700);
  // идёт ли запись Qamyal — без неё «разведку» не копим
  function poll() {
    try {
      chrome.runtime.sendMessage({ type: 'qamyal-status' }, r => { void chrome.runtime.lastError; recording = !!(r && r.recording); });
    } catch { recording = false; }
  }
  poll();
  setInterval(poll, 5000);

  const now = () => Date.now() / 1000;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();

  /** Имя участника рядом с элементом: подписи aria/title/data на нём и предках, иначе короткий текст плитки. */
  function nameFor(el) {
    let node = el;
    for (let up = 0; node && up < 7; up++, node = node.parentElement) {
      for (const a of ['data-participant-name', 'data-name', 'data-tid-name', 'data-self-name', 'title', 'aria-label']) {
        const v = clean(node.getAttribute && node.getAttribute(a));
        if (v && v.length <= 60 && !UI_WORDS.test(v) && !/[{}<>]/.test(v)) return v.replace(ARIA_SPEAK, '').replace(/[,.:;—-]+\s*$/, '').trim();
      }
      if (node.querySelectorAll) {
        // подпись плитки: короткие тексты внутри; если текстов много — это уже не плитка, а вся страница
        const texts = [];
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        let t;
        while ((t = walker.nextNode()) && texts.length < 12) {
          const v = clean(t.nodeValue);
          if (v.length >= 2 && v.length <= 48 && !UI_WORDS.test(v) && /\p{L}/u.test(v)) texts.push(v);
        }
        if (texts.length && texts.length <= 6) return texts[0];
        if (texts.length > 6) break;
      }
    }
    return '';
  }

  function start(el, how) {
    if (speaking.has(el)) return;
    let name = nameFor(el);
    if (!name) return;
    const self = SELF.test(name);
    name = name.replace(SELF, '').trim() || name;
    const uri = `${SITE}:${name.toLowerCase()}`;
    if (!people.has(uri)) { people.add(uri); out.push({ kind: 'participant', uri, name, self, t: now(), src: SITE }); }
    speaking.set(el, { uri, name, self });
    out.push({ kind: 'speak_start', uri, name, self, t: now(), src: how });
  }

  function stop(el) {
    const p = speaking.get(el);
    if (!p) return;
    speaking.delete(el);
    out.push({ kind: 'speak_end', uri: p.uri, name: p.name, self: p.self, t: now() });
  }

  const tokens = v => new Set(String(v || '').split(/\s+/).filter(Boolean));
  const isSpeakToken = t => SPEAK.test(t) && !NOT_SPEAK.test(t);
  const isSpeakAttr = el => (el.getAttribute('data-speaking') === 'true' || el.getAttribute('data-is-speaking') === 'true' ||
    ARIA_SPEAK.test(el.getAttribute('aria-label') || ''));

  function describe(el) {
    const id = el.id ? '#' + el.id : '';
    return (el.tagName || '').toLowerCase() + id + ' ' + clean(el.getAttribute('aria-label') || '').slice(0, 60);
  }

  function scout(el, kind, added, removed) {
    if (!recording || domBudget <= 0) return;
    domBudget--;
    out.push({ kind: 'dom', uri: SITE, t: now(), src: kind,
      name: JSON.stringify({ el: describe(el), add: added.slice(0, 6), del: removed.slice(0, 6), who: nameFor(el).slice(0, 40) }) });
  }
  setInterval(() => { domBudget = 30; }, 1000);      // не больше 30 записей разведки в секунду

  new MutationObserver(list => {
    for (const m of list) {
      const el = m.target;
      if (!(el instanceof Element)) continue;
      if (m.attributeName === 'class') {
        const before = tokens(m.oldValue), after = tokens(el.getAttribute('class'));
        const added = [...after].filter(t => !before.has(t)), removed = [...before].filter(t => !after.has(t));
        if (added.some(isSpeakToken)) start(el, 'class');
        else if (removed.some(isSpeakToken) && ![...after].some(isSpeakToken)) stop(el);
        if ([...added, ...removed].some(t => SPEAK_LOOSE.test(t))) scout(el, 'class', added, removed);
      } else {
        const v = el.getAttribute(m.attributeName);
        if (isSpeakAttr(el)) start(el, m.attributeName);
        else if (speaking.has(el)) stop(el);
        if (SPEAK_LOOSE.test(m.attributeName) || ARIA_SPEAK.test(v || '') || ARIA_SPEAK.test(m.oldValue || ''))
          scout(el, m.attributeName, [clean(v).slice(0, 80)], [clean(m.oldValue).slice(0, 80)]);
      }
    }
  }).observe(document.documentElement, {
    subtree: true, attributes: true, attributeOldValue: true,
    attributeFilter: ['class', 'aria-label', 'data-speaking', 'data-is-speaking', 'data-audio-level', 'data-active'],
  });

  // плитка могла уже «говорить» к моменту загрузки или пометка снялась вместе с элементом
  setInterval(() => {
    for (const el of [...speaking.keys()]) {
      if (!el.isConnected || !([...tokens(el.getAttribute('class'))].some(isSpeakToken) || isSpeakAttr(el))) stop(el);
    }
  }, 1500);
})();
