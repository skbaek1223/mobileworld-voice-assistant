/* 늘밥 음성 위젯 — added on top of the original 늘밥 parent/guardian screens.
 * - Mic button + subtitle bubble (yes/no buttons for confirmations). Nothing else changes.
 * - Keeps the page's localStorage (neulbab_settings / neulbab_orders) in sync with the
 *   server's family state: voice orders appear on the 늘밥 screen, and taps on the screen
 *   reach the voice assistant and the other family phones. */
(function () {
  "use strict";
  if (window.__nbvLoaded) return;
  window.__nbvLoaded = true;

  const ROLE = /child\.html$/.test(location.pathname) ? "guardian" : "parent";
  const KEYS = { settings: "neulbab_settings", orders: "neulbab_orders" };
  const VAD = { threshold: 0.025, endSilenceMs: 1200, noSpeechMs: 7000, maxMs: 15000 };
  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* storage blocked */ } },
  };

  let code = new URLSearchParams(location.search).get("code") || ls.get("nb_code") || "";
  if (code) ls.set("nb_code", code);

  let ws = null, lastSynced = null;
  let audioCtx = null, micStream = null, analyser = null;
  let recorder = null, recording = false, recTimer = null;
  let playQueue = new Map(), nextPlay = 1, lastSayId = 0, playing = null;
  let pendingListen = false, busy = false, hideTimer = null;

  /* ---------------- UI ---------------- */
  // Shadow DOM: the 늘밥 page's CSS cannot reach the widget, and the widget's CSS cannot
  // reach the page — the original screens stay exactly as they are.
  const host = document.createElement("div");
  host.id = "nbv-host";
  const shadow = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
  const root = document.createElement("div");
  root.className = "nbv";
  root.innerHTML =
    '<div class="nbv-bubble" hidden>' +
    '  <button class="nbv-close" type="button" aria-label="자막 닫기">닫기</button>' +
    '  <div class="nbv-heard" hidden></div>' +
    '  <div class="nbv-said" aria-live="polite"></div>' +
    '  <div class="nbv-yesno" hidden>' +
    '    <button type="button" class="yes" data-a="네">네</button>' +
    '    <button type="button" data-a="아니요">아니요</button>' +
    "  </div>" +
    '  <div class="nbv-state"></div>' +
    "</div>" +
    '<button class="nbv-mic" type="button" aria-label="말로 하기">' +
    '  <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 15a3.5 3.5 0 0 0 3.5-3.5v-6a3.5 3.5 0 1 0-7 0v6A3.5 3.5 0 0 0 12 15Zm6-3.5a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.91V20H8.5a1 1 0 1 0 0 2h7a1 1 0 1 0 0-2H13v-2.59a6 6 0 0 0 5-5.91Z"/></svg>' +
    "  <span>말하기</span>" +
    "</button>";
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/voice/widget.css";
  shadow.appendChild(css);
  shadow.appendChild(root);
  const $ = (sel) => root.querySelector(sel);
  const ui = {
    bubble: $(".nbv-bubble"), said: $(".nbv-said"), heard: $(".nbv-heard"),
    state: $(".nbv-state"), yesno: $(".nbv-yesno"), mic: $(".nbv-mic"), close: $(".nbv-close"),
  };

  function mount() {
    // room at the end of the page so its last item can scroll above the mic
    const spacer = document.createElement("div");
    spacer.style.height = "112px";
    spacer.setAttribute("aria-hidden", "true");
    document.body.appendChild(spacer);
    document.body.appendChild(host);
    placeAboveTabbar();
    setInterval(placeAboveTabbar, 1000);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);

  function placeAboveTabbar() {  // guardian screens have a bottom tab bar
    const bar = document.getElementById("tabbar");
    const h = bar && !bar.hidden && bar.offsetHeight ? bar.offsetHeight : 0;
    root.style.bottom = "calc(" + (16 + h) + "px + env(safe-area-inset-bottom))";
  }

  function showBubble() {
    clearTimeout(hideTimer);
    ui.bubble.hidden = false;
  }
  function hideBubbleSoon() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (!busy && !pendingListen && !recording && idle()) ui.bubble.hidden = true;
    }, 8000);
  }
  function setState(text, cls) {
    ui.state.textContent = text || "";
    ui.mic.classList.toggle("busy", cls === "busy");
    if (text) showBubble();
  }
  ui.close.addEventListener("click", () => { ui.bubble.hidden = true; });

  /* ---------------- connection + sync ---------------- */
  function connect() {
    if (!code) return;
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
      "/ws?role=" + ROLE + "&code=" + encodeURIComponent(code);
    ws = new WebSocket(url);
    playQueue.clear(); nextPlay = 1; lastSayId = 0;
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
    ws.onclose = (ev) => {
      ws = null;
      if (ev.code === 4401) { code = ""; ls.del("nb_code"); return; }
      setTimeout(connect, 2000);
    };
  }
  function send(msg) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); }

  function localSnapshot() { return JSON.stringify([ls.get(KEYS.settings), ls.get(KEYS.orders)]); }

  function applySync(m) {
    if (m.settings) ls.set(KEYS.settings, JSON.stringify(m.settings));
    else ls.del(KEYS.settings);
    ls.set(KEYS.orders, JSON.stringify(m.orders || []));
    lastSynced = localSnapshot();
    try { window.dispatchEvent(new StorageEvent("storage", { key: KEYS.orders })); } catch (e) { /* old browser */ }
  }

  setInterval(() => {  // a tap on the 늘밥 screen changed something: tell the server
    if (!ws || ws.readyState !== 1 || lastSynced === null) return;
    const now = localSnapshot();
    if (now === lastSynced) return;
    lastSynced = now;
    let settings = null, orders = [];
    try { settings = JSON.parse(ls.get(KEYS.settings) || "null"); orders = JSON.parse(ls.get(KEYS.orders) || "[]"); } catch (e) { return; }
    send({ type: "sync_up", settings: settings, orders: orders });
  }, 700);

  function handle(m) {
    switch (m.type) {
      case "sync": applySync(m); break;
      case "say":
        lastSayId = Math.max(lastSayId, m.id);
        ui.said.textContent = m.text;
        showBubble();
        break;
      case "audio": playQueue.set(m.id, m.data); pump(); break;
      case "heard":
        if (m.text) { ui.heard.textContent = "“" + m.text + "”"; ui.heard.hidden = false; showBubble(); }
        else if (!pendingListen) setState("잘 못 들었어요. 다시 말씀해 주세요");
        break;
      case "thinking": busy = true; setState("생각하고 있어요…", "busy"); break;
      case "listen": pendingListen = true; ui.yesno.hidden = false; showBubble(); afterPlayback(); break;
      case "done":
        busy = false; pendingListen = false; ui.yesno.hidden = true;
        afterPlayback(); hideBubbleSoon();
        break;
      case "busy": setState("아직 앞의 말씀을 처리하고 있어요", "busy"); break;
      case "error": busy = false; setState("문제가 생겼어요. 다시 말씀해 주세요"); break;
    }
  }

  /* ---------------- audio out ---------------- */
  function unlockAudio() {  // must run inside a tap (iPhone)
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const src = audioCtx.createBufferSource();
    src.buffer = audioCtx.createBuffer(1, 1, 22050);
    src.connect(audioCtx.destination);
    src.start(0);
  }
  function idle() { return !playing && nextPlay > lastSayId; }
  function pump() {
    if (playing || !playQueue.has(nextPlay)) return;
    const data = playQueue.get(nextPlay);
    playQueue.delete(nextPlay);
    nextPlay += 1;
    if (!data || !audioCtx) { pump(); afterPlayback(); return; }
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    playing = true;
    setState("말하고 있어요", "busy");
    audioCtx.decodeAudioData(bytes.buffer, (buf) => {
      if (playing !== true) return;
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      src.onended = () => { if (playing === src) { playing = null; pump(); afterPlayback(); } };
      playing = src;
      src.start(0);
    }, () => { playing = null; pump(); afterPlayback(); });
  }
  function stopPlayback() {
    if (playing && playing !== true) { const p = playing; playing = null; try { p.stop(); } catch (e) { /* done */ } }
    playing = null; playQueue.clear(); nextPlay = lastSayId + 1;
  }
  function afterPlayback() {
    if (!idle()) return;
    if (pendingListen && !recording) startRecording(true);
    else if (!busy && !recording) { setState(""); hideBubbleSoon(); }
  }

  /* ---------------- audio in ---------------- */
  async function openMic() {
    if (micStream) return true;
    if (!window.isSecureContext || !navigator.mediaDevices) { setState("이 주소에서는 마이크를 쓸 수 없어요"); return false; }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (audioCtx) {
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        audioCtx.createMediaStreamSource(micStream).connect(analyser);
      }
      return true;
    } catch (e) {
      setState("마이크를 쓸 수 없어요. 설정에서 마이크를 허용해 주세요");
      return false;
    }
  }
  function pickMime() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/aac"]) if (MediaRecorder.isTypeSupported(t)) return t;
    return "";
  }
  function startRecording(auto) {
    if (recording || !micStream || !window.MediaRecorder) return;
    if (!idle()) stopPlayback();
    const mime = pickMime();
    recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    let spoke = false, aborted = false;
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = () => {
      recording = false; clearInterval(recTimer); ui.mic.classList.remove("listening");
      if (aborted) return;
      if (!spoke) { if (pendingListen) send({ type: "silence" }); else { setState(""); hideBubbleSoon(); } return; }
      const type = recorder.mimeType || mime || "audio/webm";
      const reader = new FileReader();
      reader.onload = () => send({ type: "audio", mime: type, data: String(reader.result).split(",")[1] });
      reader.readAsDataURL(new Blob(chunks, { type: type }));
      setState("알아듣는 중이에요…", "busy");
    };
    recorder.abort = () => { aborted = true; recorder.stop(); };
    recorder.start();
    recording = true;
    ui.mic.classList.add("listening");
    if (!auto) { ui.heard.hidden = true; ui.said.textContent = ""; }
    setState(auto ? "대답해 주세요. 듣고 있어요" : "듣고 있어요. 말씀하세요");
    const buf = new Float32Array(analyser ? analyser.fftSize : 1024);
    const t0 = Date.now();
    let lastVoice = 0, voiced = 0;
    recTimer = setInterval(() => {
      const now = Date.now();
      if (analyser) {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        if (Math.sqrt(sum / buf.length) > VAD.threshold) { voiced += 1; lastVoice = now; if (voiced >= 3) spoke = true; }
      }
      const quiet = now - (lastVoice || t0);
      if ((spoke && quiet > VAD.endSilenceMs) || (!spoke && now - t0 > VAD.noSpeechMs) || now - t0 > VAD.maxMs) {
        if (!analyser) spoke = true;
        recorder.stop();
      }
    }, 50);
  }

  async function ensureCode() {
    if (code) return true;
    const entered = (window.prompt("음성 비서 접속 번호 6자리를 입력해 주세요") || "").trim();
    if (!entered) return false;
    try {
      const r = await (await fetch("/api/check?code=" + encodeURIComponent(entered))).json();
      if (!r.ok) { setState("접속 번호가 맞지 않아요"); return false; }
    } catch (e) { setState("서버에 연결하지 못했어요"); return false; }
    code = entered;
    ls.set("nb_code", code);
    connect();
    return true;
  }

  ui.mic.addEventListener("click", async () => {
    unlockAudio();
    if (recording) { recorder.stop(); return; }
    if (!(await ensureCode())) return;
    if (busy && !pendingListen) { setState("아직 앞의 말씀을 처리하고 있어요", "busy"); return; }
    if (!(await openMic())) return;
    if (!ws || ws.readyState !== 1) { setState("연결하는 중이에요. 잠시 뒤에 다시 눌러 주세요", "busy"); return; }
    startRecording(false);
  });

  ui.yesno.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-a]");
    if (!b) return;
    unlockAudio();
    if (recording && recorder) recorder.abort();
    stopPlayback();
    ui.yesno.hidden = true;
    pendingListen = false;
    ui.heard.textContent = "“" + b.dataset.a + "”"; ui.heard.hidden = false;
    send({ type: "text", text: b.dataset.a });
    setState("알겠어요…", "busy");
  });

  connect();
})();
