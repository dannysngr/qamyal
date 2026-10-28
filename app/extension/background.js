'use strict';
/* Qamyal — кто говорит: принимает события от страниц созвонов и передаёт их в Qamyal на этом компьютере
   (http://127.0.0.1:<порт>/api/rec/hint). Пока Qamyal не пишет созвон, события просто отбрасываются. */

const PORTS = [47615, 47616, 47617, 47618, 47619, 47620];
let port = null, recording = false, checked = 0, queue = [];

async function findQamyal() {
  for (const p of port ? [port, ...PORTS] : PORTS) {
    try {
      const r = await fetch(`http://127.0.0.1:${p}/api/rec/hint`, { cache: 'no-store' });
      if (r.ok) { const j = await r.json(); port = p; recording = !!j.recording; checked = Date.now(); return true; }
    } catch { /* следующий порт */ }
  }
  port = null; recording = false; checked = Date.now();
  return false;
}

async function flush() {
  if (!queue.length) return;
  if (Date.now() - checked > 4000) await findQamyal();
  if (!port || !recording) { queue = []; return; }
  const events = queue.splice(0, 200);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/rec/hint`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ events }),
    });
    recording = r.ok && !!(await r.json()).recording;
  } catch { port = null; }
}

// сразу после установки и при каждом запуске Chrome — дать знать Qamyal, что расширение на месте
chrome.runtime.onInstalled.addListener(() => { findQamyal(); });
chrome.runtime.onStartup.addListener(() => { findQamyal(); });

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'qamyal-events' && Array.isArray(msg.events)) {
    queue.push(...msg.events);
    if (queue.length > 2000) queue = queue.slice(-2000);
    flush();
  }
  if (msg && msg.type === 'qamyal-status') {
    (Date.now() - checked > 4000 ? findQamyal() : Promise.resolve()).then(() => reply({ recording, port }));
    return true;
  }
  return false;
});
