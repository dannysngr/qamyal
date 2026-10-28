'use strict';
/* Окошко по нажатию на значок: на связи ли Qamyal, идёт ли запись созвона и знает ли расширение эту страницу. */

const set = (n, cls, text) => {
  document.getElementById('d' + n).className = 'dot ' + cls;
  document.getElementById('t' + n).textContent = text;
};

// страницы созвонов — те же, где работает content.js (из manifest.json)
const patterns = chrome.runtime.getManifest().content_scripts[0].matches.map(m =>
  new RegExp('^' + m.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$'));

chrome.runtime.sendMessage({ type: 'qamyal-status' }, st => {
  if (!st || !st.port) set(1, '', 'Qamyal не запущен — подсказки некуда передавать.');
  else if (st.recording) set(1, 'rec', 'Qamyal пишет созвон — подсказываю, кто говорит.');
  else set(1, 'ok', 'Qamyal на связи. Подсказки пойдут, когда в Диктофоне начнётся запись «Созвона».');
});

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  const url = (tabs[0] && tabs[0].url) || '';
  if (patterns.some(re => re.test(url))) set(2, 'ok', 'Эта страница — созвон: здесь расширение видит, кто говорит.');
  else set(2, '', 'Здесь не созвон. Работает в Телемосте, Zoom, Teams, Google Meet, МТС Линк и Битрикс24.');
});
