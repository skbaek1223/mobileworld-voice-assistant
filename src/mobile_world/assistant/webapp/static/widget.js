/* 늘밥 음성 위젯 — added on top of the original 늘밥 parent/guardian screens.
 * - Mic button + subtitle bubble (yes/no buttons for confirmations). Nothing else changes.
 * - Keeps the page's localStorage (neulbab_settings / neulbab_orders) in sync with the
 *   server's family state: voice orders appear on the 늘밥 screen, and taps on the screen
 *   reach the voice assistant and the other family phones.
 * - 어르신 (parent): hands-free. After one tap to allow sound (browsers require it), every
 *   screen's guidance is read aloud ("오늘 뭐 드실래요? …"), the mic listens on its own, and
 *   네/아니요 answers the screen's own questions by pressing its buttons. */
(function () {
  "use strict";
  if (window.__nbvLoaded) return;
  window.__nbvLoaded = true;

  const ROLE = /child\.html$/.test(location.pathname) ? "guardian" : "parent";
  const HANDS_FREE = ROLE === "parent";
  const VOICE = ROLE === "parent";  // 보호자 screens: no mic or captions, only the family sync
  const KEYS = { settings: "neulbab_settings", orders: "neulbab_orders" };
  const VAD = { threshold: 0.025, ambientThreshold: 0.035, endSilenceMs: 1200, noSpeechMs: 7000, maxMs: 15000 };
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
  let started = false, handsFree = false, lastGuideKey = "", quietUntil = 0, ambientTimer = null;
  const sayTexts = new Map();  // clip id -> sentence; the caption shows the one being spoken

  /* ---------------- UI (Shadow DOM: page CSS and widget CSS never mix) ---------------- */
  const host = document.createElement("div");
  host.id = "nbv-host";
  const shadow = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
  const root = document.createElement("div");
  root.className = "nbv";
  root.innerHTML =
    '<div class="nbv-start" hidden><button type="button" class="nbv-start-btn">' +
    '  <span class="nbv-start-main">눌러서 시작하기</span>' +
    '  <span class="nbv-start-sub">화면을 한 번 누르시면<br>소리로 안내해 드려요</span></button></div>' +
    '<div class="nbv-bubble" hidden>' +
    '  <button class="nbv-close" type="button" aria-label="자막 닫기">×</button>' +
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
    '  <span class="nbv-mic-label">말하기</span>' +
    "</button>";
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/voice/widget.css";
  shadow.appendChild(css);
  shadow.appendChild(root);
  const $ = (sel) => root.querySelector(sel);
  const ui = {
    bubble: $(".nbv-bubble"), said: $(".nbv-said"), heard: $(".nbv-heard"), state: $(".nbv-state"),
    yesno: $(".nbv-yesno"), mic: $(".nbv-mic"), micLabel: $(".nbv-mic-label"), close: $(".nbv-close"),
    start: $(".nbv-start"), startBtn: $(".nbv-start-btn"),
  };

  function mount() {
    // One role per phone: the original "처음으로" goes to index.html, a hub that only shows
    // when both roles are logged in on the same browser — otherwise it bounces straight
    // back here. Send it to the first screen (보호자/어르신 선택) instead.
    document.querySelectorAll('a[data-testid="home-button"]').forEach((a) => a.setAttribute("href", "login.html"));
    if (!VOICE) return;  // keep syncing with the server, but show no voice controls
    const spacer = document.createElement("div");  // lets the last item scroll above the mic
    spacer.style.height = "112px";
    spacer.setAttribute("aria-hidden", "true");
    document.body.appendChild(spacer);
    document.body.appendChild(host);
    placeAboveTabbar();
    setInterval(placeAboveTabbar, 1000);
    if (HANDS_FREE) ui.start.hidden = false;  // browsers need one tap before any sound
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);

  function placeAboveTabbar() {  // guardian screens have a bottom tab bar
    const bar = document.getElementById("tabbar");
    const h = bar && !bar.hidden && bar.offsetHeight ? bar.offsetHeight : 0;
    root.style.bottom = "calc(" + (16 + h) + "px + env(safe-area-inset-bottom))";
  }

  function showBubble() { clearTimeout(hideTimer); ui.bubble.hidden = false; }
  function caption(text) {
    ui.heard.hidden = true;
    ui.said.textContent = text;
    showBubble();
  }
  function hideBubbleSoon() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (!busy && !pendingListen && idle()) ui.bubble.hidden = true;
    }, 4000);
  }
  function setState(text, cls) {
    ui.state.textContent = text || "";
    ui.mic.classList.toggle("busy", cls === "busy");
    if (text) showBubble();
  }
  function setMicLabel() {
    ui.micLabel.textContent = recording ? "듣는 중" : (HANDS_FREE && started && !handsFree ? "켜기" : "말하기");
  }
  ui.close.addEventListener("click", () => { ui.bubble.hidden = true; });

  /* ---------------- Korean helpers ---------------- */
  function hasBatchim(word) {
    const c = String(word || "").trim().slice(-1).charCodeAt(0);
    return c >= 0xac00 && c <= 0xd7a3 ? (c - 0xac00) % 28 !== 0 : null;
  }
  function subj(word) { const b = hasBatchim(word); return b === null ? "이(가)" : (b ? "이" : "가"); }
  function yesNo(text) {  // mirrors assistant/confirm.py (no wins, unclear is not yes)
    const t = " " + String(text || "").toLowerCase().replace(/\s+/g, " ").trim() + " ";
    if (!t.trim()) return "unclear";
    const no = ["아니", "안 ", "안해", "안 해", "안할", "안 할", "하지 마", "하지마", "싫", "됐어", "그만", "취소",
      "다음에", "나중에", "잠깐", "말고", "필요 없", "괜찮아", "no", "cancel", "stop"];
    if (no.some((w) => t.includes(w))) return "no";
    const yes = ["네", "예", "응", "어 ", "그래", "좋아", "해 줘", "해줘", "해 주세요", "해주세요", "주문해", "맞아",
      "오케이", "알았어", "알겠어", "부탁", "yes", "ok"];
    return yes.some((w) => t.includes(w)) ? "yes" : "unclear";
  }

  /* ---------------- what the 늘밥 screen shows (parent) ---------------- */
  function visible(el) { return !!el && !el.hidden && el.offsetParent !== null && el.textContent.trim() !== ""; }
  function txt(id) { const el = document.getElementById(id); return visible(el) ? el.textContent.trim() : ""; }
  function click(sel) { const el = document.querySelector(sel); if (el && el.offsetParent !== null) { el.click(); return true; } return false; }
  function currentScreen() {
    const s = Array.from(document.querySelectorAll("section.screen")).find((x) => !x.hidden);
    return s ? s.id : "";
  }
  function menuNames(section) {
    return Array.from(section.querySelectorAll('[data-testid="menu-name"]')).filter(visible).map((e) => e.textContent.trim());
  }
  function listSentence(names) {
    if (!names.length) return "";
    return names.join(", ") + subj(names[names.length - 1]) + " 있어요.";
  }
  function join(parts) {
    return parts.filter(Boolean).map((p) => (/[.?!]$/.test(p) ? p : p + ".")).join(" ");
  }
  /* -> {key, speak, hint, local}: local = actions for 네/아니요 on this screen */
  function guide() {
    const id = currentScreen();
    const sec = id && document.getElementById(id);
    if (!sec) return null;
    if (id === "pa-1" || id === "pa-m") {
      // "어르신, 오늘 뭐 드실래요? A, B, C이 있어요. 드시고 싶은 음식을 말씀해 주시거나 그림을 눌러 주세요."
      // What 어르신 say next goes to the assistant ("미역국 줘", "다른 거 있어?").
      const title = txt(id === "pa-1" ? "pa1-title" : "pam-title");
      const names = menuNames(sec);
      const empty = txt(id === "pa-1" ? "pa1-empty" : "pam-empty");
      const body = join([title, txt("health-note"), listSentence(names), empty]);
      return {
        key: id + "|" + body,
        speak: join([body, names.length ? "드시고 싶은 음식을 말씀해 주시거나 그림을 눌러 주세요" : ""]),
        hint: body,
      };
    }
    if (id === "pa-2") {
      const body = join(["이대로 주문할까요?", txt("pa2-menu"), txt("pa2-price"), txt("pa2-payment")]);
      return {
        key: id + "|" + body,
        speak: join([body, "주문하시려면 네, 아니면 아니요라고 말씀해 주세요"]),
        hint: body,
        local: { yes: () => click("#btn-order"), no: () => click("#btn-nav-back") },
      };
    }
    if (id === "pa-3") {
      if (txt("cancel-confirm")) {
        return {
          key: id + "|cancel-confirm",
          speak: "주문을 취소할까요? 네, 또는 아니요로 말씀해 주세요.",
          hint: "주문을 취소할까요?",
          local: { yes: () => click('[data-testid="btn-cancel-yes"]'), no: () => click('[data-testid="btn-cancel-no"]') },
        };
      }
      const body = join([txt("proxy-text") || txt("proxy-order-notice"), txt("arrival-banner"),
        txt("pa3-title"), txt("order-status"), txt("status-desc"), txt("cancelled-note"), txt("cancel-unavailable")]);
      return { key: id + "|" + body, speak: body, hint: body + " " + txt("pa3-menu") };
    }
    return null;
  }

  function guideLoop() {  // read each new screen (or status change) once
    if (!HANDS_FREE || !started || !ws || ws.readyState !== 1) return;
    const g = guide();
    if (!g || g.key === lastGuideKey) return;
    if (Date.now() < quietUntil || busy) {  // the assistant just said it: don't repeat
      lastGuideKey = g.key;
      return;
    }
    if (!idle() || pendingListen) return;  // wait until quiet, then read
    lastGuideKey = g.key;
    if (recording && recorder) recorder.abort();
    send({ type: "speak", text: g.speak });
  }
  setInterval(guideLoop, 700);

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

  let localIntent = null;  // 네/아니요 handlers of the screen the words were spoken on
  function handle(m) {
    switch (m.type) {
      case "sync": applySync(m); break;
      case "say":
        lastSayId = Math.max(lastSayId, m.id);
        sayTexts.set(m.id, m.text);
        if (!playing && nextPlay === m.id) caption(m.text);  // first sentence: show at once
        break;
      case "audio": playQueue.set(m.id, m.data); pump(); break;
      case "heard":
        if (m.text) { ui.heard.textContent = "“" + m.text + "”"; ui.heard.hidden = false; ui.said.textContent = ""; showBubble(); }
        if (m.transcribe_only) { onLocalAnswer(m.text); break; }
        if (!m.text && !pendingListen) { setState(""); afterPlayback(); }
        break;
      case "thinking": busy = true; setState("생각하고 있어요…", "busy"); break;
      case "listen": pendingListen = true; ui.yesno.hidden = false; showBubble(); afterPlayback(); break;
      case "done":
        busy = false; pendingListen = false; ui.yesno.hidden = true;
        quietUntil = Date.now() + 5000;  // the screen will change to what was just said
        afterPlayback(); hideBubbleSoon();
        break;
      case "busy": setState("아직 앞의 말씀을 처리하고 있어요", "busy"); break;
      case "error": busy = false; setState("문제가 생겼어요. 다시 말씀해 주세요"); afterPlayback(); break;
    }
  }

  function onLocalAnswer(text) {
    const intent = localIntent;
    localIntent = null;
    const verdict = yesNo(text);
    if (intent && verdict === "yes") { intent.yes(); setState(""); afterPlayback(); return; }
    if (intent && verdict === "no") { intent.no(); setState(""); afterPlayback(); return; }
    if (text) {  // something else ("다른 거 볼래") -> the assistant handles it
      const g = guide();
      send({ type: "text", text: text, screen: g ? g.hint : "" });
      return;
    }
    afterPlayback();
  }

  /* ---------------- audio out ---------------- */
  function unlockAudio() {  // must run inside a tap (iPhone, Android)
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
    if (sayTexts.has(nextPlay)) { caption(sayTexts.get(nextPlay)); sayTexts.delete(nextPlay); }
    nextPlay += 1;
    if (!data || !audioCtx) { pump(); afterPlayback(); return; }
    if (recording && recorder) recorder.abort();  // never record our own voice
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    playing = true;
    setState("", "busy");  // the mic colour shows it is speaking; keep the caption short
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
    if (pendingListen && !recording) { startRecording(true); return; }
    if (busy || recording) return;
    setState("");
    hideBubbleSoon();
    if (handsFree) {  // keep listening on its own
      clearTimeout(ambientTimer);
      ambientTimer = setTimeout(() => { if (handsFree && idle() && !busy && !recording) startRecording(false, true); }, 400);
    }
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
  /* auto: answering the assistant's question. ambient: hands-free waiting for the parent. */
  function startRecording(auto, ambient) {
    if (recording || !micStream || !window.MediaRecorder) return;
    if (!idle()) stopPlayback();
    const g = guide();
    const intent = !auto && g && g.local ? g.local : null;
    const mime = pickMime();
    recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    let spoke = false, aborted = false;
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = () => {
      recording = false; clearInterval(recTimer); ui.mic.classList.remove("listening"); setMicLabel();
      if (aborted) return;
      if (!spoke) {
        if (pendingListen) send({ type: "silence" });
        else if (ambient) afterPlayback();  // nothing said: keep waiting
        else { setState(""); hideBubbleSoon(); }
        return;
      }
      const type = recorder.mimeType || mime || "audio/webm";
      const reader = new FileReader();
      localIntent = intent;
      reader.onload = () => send({
        type: "audio", mime: type, data: String(reader.result).split(",")[1],
        transcribe_only: !!intent, screen: g ? g.hint : "",
      });
      reader.readAsDataURL(new Blob(chunks, { type: type }));
      setState("알아듣는 중이에요…", "busy");
    };
    recorder.abort = () => { aborted = true; try { recorder.stop(); } catch (e) { /* stopped */ } };
    recorder.start();
    recording = true;
    ui.mic.classList.add("listening");
    setMicLabel();
    if (!ambient) {
      if (!auto) { ui.heard.hidden = true; ui.said.textContent = ""; }
      setState(auto ? "대답해 주세요. 듣고 있어요" : "듣고 있어요. 말씀하세요");
    }
    const threshold = ambient ? VAD.ambientThreshold : VAD.threshold;
    const buf = new Float32Array(analyser ? analyser.fftSize : 1024);
    const t0 = Date.now();
    let lastVoice = 0, voiced = 0;
    recTimer = setInterval(() => {
      const now = Date.now();
      if (analyser) {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        if (Math.sqrt(sum / buf.length) > threshold) {
          voiced += 1; lastVoice = now;
          if (voiced >= (ambient ? 5 : 3)) spoke = true;
        }
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

  /* 어르신: the one tap that allows sound, then everything is automatic */
  async function start() {
    unlockAudio();
    ui.start.hidden = true;
    if (!(await ensureCode())) { ui.start.hidden = false; return; }
    await openMic();
    started = true;
    handsFree = !!micStream;
    lastGuideKey = "";
    setMicLabel();
    for (let i = 0; i < 40 && (!ws || ws.readyState !== 1); i++) await new Promise((r) => setTimeout(r, 100));
    guideLoop();
  }
  ui.startBtn.addEventListener("click", start);

  ui.mic.addEventListener("click", async () => {
    unlockAudio();
    if (HANDS_FREE && !started) { start(); return; }
    if (recording) {
      if (HANDS_FREE && handsFree && !pendingListen) {  // turn hands-free listening off
        handsFree = false; recorder.abort(); setMicLabel(); setState("자동 듣기를 껐어요. 다시 켜려면 눌러 주세요");
        return;
      }
      recorder.stop();
      return;
    }
    if (HANDS_FREE && !handsFree) { handsFree = true; setMicLabel(); setState(""); afterPlayback(); return; }
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
