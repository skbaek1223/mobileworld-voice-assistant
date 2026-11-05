/* 늘밥 음성 비서 — phone client (Android Chrome, iPhone Safari).
 * Mic -> (auto stop on silence) -> server STT/LLM -> sentences + mp3 -> played in order. */
"use strict";

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

const VAD = { threshold: 0.025, endSilenceMs: 1200, noSpeechMs: 7000, maxMs: 15000 };

let ws = null, role = null, code = "", greeted = false, onTalk = false;
let audioCtx = null, micStream = null, analyser = null;
let recorder = null, recording = false, recTimer = null;
let playQueue = new Map(), nextPlay = 1, lastSayId = 0, playing = null;
let pendingListen = false, busy = false;

/* ---------------- start screen ---------------- */
async function check(c) {
  try {
    const r = await fetch("/api/check?code=" + encodeURIComponent(c || ""));
    return await r.json();
  } catch (e) {
    return { ok: false, offline: true };
  }
}

(async function init() {
  code = store.get("nb_code") || "";
  const r = await check(code);
  if (!r.ok && !r.offline) {
    $("code-box").hidden = false;
    $("code").value = code;
  }
  document.querySelectorAll("[data-role]").forEach((b) =>
    b.addEventListener("click", () => start(b.dataset.role)));
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
})();

function showError(text) {
  $("start-error").textContent = text;
  $("start-error").hidden = !text;
}

function unlockAudio() {
  // must run inside the tap (iPhone): create/resume the audio context and play silence
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!audioCtx) audioCtx = new Ctx();
  if (audioCtx.state === "suspended") audioCtx.resume();
  const src = audioCtx.createBufferSource();
  src.buffer = audioCtx.createBuffer(1, 1, 22050);
  src.connect(audioCtx.destination);
  src.start(0);
}

async function start(r) {
  unlockAudio();
  showError("");
  if (!$("code-box").hidden) code = $("code").value.trim();
  const ok = await check(code);
  if (!ok.ok) {
    $("code-box").hidden = false;
    showError(ok.offline ? "서버에 연결하지 못했어요. 인터넷을 확인해 주세요" : "접속 번호가 맞지 않아요");
    return;
  }
  store.set("nb_code", code);
  role = r;
  await openMic();
  $("who").textContent = role === "parent" ? "어르신" : "보호자";
  $("screen-start").hidden = true;
  $("screen-talk").hidden = false;
  onTalk = true;
  greeted = false;
  connect();
}

async function openMic() {
  if (micStream) return;
  if (!window.isSecureContext || !navigator.mediaDevices) {
    setState("이 주소에서는 마이크를 쓸 수 없어요 (https 필요). 글로 써 주세요");
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const source = audioCtx.createMediaStreamSource(micStream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
  } catch (e) {
    setState("마이크를 쓸 수 없어요. 설정에서 마이크를 허용하거나 글로 써 주세요");
  }
}

$("btn-home").addEventListener("click", () => {
  onTalk = false;
  stopPlayback();
  if (recording) abortRecording();
  if (ws) ws.close();
  $("screen-talk").hidden = true;
  $("screen-start").hidden = false;
});

/* ---------------- connection ---------------- */
function connect() {
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
    "/ws?role=" + role + "&code=" + encodeURIComponent(code);
  ws = new WebSocket(url);
  playQueue.clear();
  nextPlay = 1;
  lastSayId = 0;
  ws.onopen = () => {
    send(greeted ? { type: "state" } : { type: "start" });
    greeted = true;
    setIdle();
  };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = (ev) => {
    if (ev.code === 4401) {
      onTalk = false;
      $("screen-talk").hidden = true;
      $("screen-start").hidden = false;
      $("code-box").hidden = false;
      showError("접속 번호가 맞지 않아요");
      return;
    }
    if (onTalk) {
      setState("연결이 끊겼어요. 다시 연결하는 중…", "busy");
      setTimeout(() => onTalk && connect(), 1500);
    }
  };
}

function send(msg) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg));
}

setInterval(() => onTalk && send({ type: "state" }), 5000);

function handle(m) {
  switch (m.type) {
    case "sync":  // the standalone page shows state via "state" messages instead
      break;
    case "say":
      lastSayId = Math.max(lastSayId, m.id);
      $("said").textContent = m.text;
      addLog(m.text, "bot");
      break;
    case "audio":
      playQueue.set(m.id, m.data);
      pump();
      break;
    case "heard":
      if (m.text) addLog(m.text, "me");
      else if (!pendingListen) setState("잘 못 들었어요. 다시 말씀해 주세요");
      break;
    case "thinking":
      busy = true;
      setState("생각하고 있어요…", "busy");
      break;
    case "listen":
      pendingListen = true;
      $("yesno").hidden = false;
      afterPlayback();
      break;
    case "done":
      busy = false;
      pendingListen = false;
      $("yesno").hidden = true;
      afterPlayback();
      break;
    case "busy":
      setState("아직 앞의 말씀을 처리하고 있어요", "busy");
      break;
    case "state":
      renderState(m);
      break;
    case "error":
      busy = false;
      setState("문제가 생겼어요. 다시 말씀해 주세요");
      break;
  }
}

function addLog(text, who) {
  const li = document.createElement("li");
  li.className = who;
  li.textContent = text;
  const log = $("log");
  log.appendChild(li);
  while (log.children.length > 8) log.removeChild(log.firstChild);
}

function renderState(s) {
  const o = s.order;
  if (!o || o.status === "cancelled" && false) {
    $("order-card").hidden = true;
    return;
  }
  $("order-card").hidden = false;
  $("order-main").textContent = o.menu_name + " · " + o.status_label;
  const parts = [o.status_desc];
  if (o.status !== "arrived" && o.status !== "cancelled") parts.push(o.eta_text + "쯤 도착해요");
  if (o.ordered_by_text) parts.push(o.ordered_by_text);
  $("order-sub").textContent = parts.join(". ");
}

/* ---------------- speaking (mp3 clips in order) ---------------- */
function idle() {
  return !playing && nextPlay > lastSayId;
}

function pump() {
  if (playing || !playQueue.has(nextPlay)) return;
  const data = playQueue.get(nextPlay);
  playQueue.delete(nextPlay);
  nextPlay += 1;
  if (!data) { pump(); afterPlayback(); return; }  // TTS failed: text is on screen
  const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  playing = true;
  setState("말하고 있어요", "busy");
  audioCtx.decodeAudioData(bytes.buffer, (buf) => {
    if (playing !== true) return;  // stopped meanwhile
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.onended = () => { if (playing === src) { playing = null; pump(); afterPlayback(); } };
    playing = src;
    src.start(0);
  }, () => { playing = null; pump(); afterPlayback(); });
}

function stopPlayback() {
  if (playing && playing !== true) {
    const p = playing;
    playing = null;
    try { p.stop(); } catch (e) { /* already stopped */ }
  }
  playing = null;
  playQueue.clear();
  nextPlay = lastSayId + 1;
}

function afterPlayback() {
  if (!idle()) return;
  if (pendingListen && !recording) startRecording(true);
  else if (!busy && !recording) setIdle();
}

/* ---------------- listening (auto stop on silence) ---------------- */
function pickMime() {
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/aac"]) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

function startRecording(auto) {
  if (recording) return;
  if (!micStream || !window.MediaRecorder) {
    setState(pendingListen ? "아래 버튼을 누르거나 글로 써 주세요" : "글로 써 주세요");
    return;
  }
  if (!idle()) stopPlayback();  // barge-in: stop talking and listen
  const mime = pickMime();
  recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  let spoke = false, aborted = false;
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = () => {
    recording = false;
    clearInterval(recTimer);
    $("mic").classList.remove("listening");
    if (aborted) return;
    if (!spoke) {
      if (pendingListen) send({ type: "silence" });
      else setIdle();
      return;
    }
    const type = recorder.mimeType || mime || "audio/webm";
    const reader = new FileReader();
    reader.onload = () => send({ type: "audio", mime: type, data: String(reader.result).split(",")[1] });
    reader.readAsDataURL(new Blob(chunks, { type }));
    setState("알아듣는 중이에요…", "busy");
  };
  recorder.abort = () => { aborted = true; recorder.stop(); };
  recorder.start();
  recording = true;
  $("mic").classList.add("listening");
  setState(auto ? "대답해 주세요. 듣고 있어요" : "듣고 있어요. 말씀하세요", "listening");

  const buf = new Float32Array(analyser ? analyser.fftSize : 1024);
  const t0 = Date.now();
  let lastVoice = 0, voicedFrames = 0;
  recTimer = setInterval(() => {
    const now = Date.now();
    if (analyser) {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      if (rms > VAD.threshold) {
        voicedFrames += 1;
        lastVoice = now;
        if (voicedFrames >= 3) spoke = true;
      }
    }
    const silentFor = now - (lastVoice || t0);
    if ((spoke && silentFor > VAD.endSilenceMs) || (!spoke && now - t0 > VAD.noSpeechMs) ||
        now - t0 > VAD.maxMs) {
      if (!analyser) spoke = true;  // no level meter: send what we have
      recorder.stop();
    }
  }, 50);
}

function stopRecording() { if (recording && recorder) recorder.stop(); }
function abortRecording() { if (recording && recorder) recorder.abort(); }

$("mic").addEventListener("click", () => {
  unlockAudio();
  if (recording) return stopRecording();
  if (busy && !pendingListen) return setState("아직 앞의 말씀을 처리하고 있어요", "busy");
  startRecording(false);
});

document.querySelectorAll("[data-answer]").forEach((b) => b.addEventListener("click", () => {
  unlockAudio();
  if (recording) abortRecording();
  stopPlayback();
  $("yesno").hidden = true;
  addLog(b.dataset.answer, "me");
  send({ type: "text", text: b.dataset.answer });
  pendingListen = false;
  setState("알겠어요…", "busy");
}));

$("text-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("text").value.trim();
  if (!t) return;
  unlockAudio();
  if (recording) abortRecording();
  $("text").value = "";
  if (pendingListen) { $("yesno").hidden = true; pendingListen = false; }
  send({ type: "text", text: t });
});

function setState(text, cls) {
  $("state").textContent = text;
  $("mic").classList.toggle("busy", cls === "busy");
}

function setIdle() {
  setState("눌러서 말씀하세요");
}
