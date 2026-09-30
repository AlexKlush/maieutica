/* Майевтика — логика интерфейса.
   Состояние приходит из Python (Streamlit custom component, протокол v1), события уходят обратно через setComponentValue.
   Пока Python думает, интерфейс показывает реплику ученика сразу и ждёт ответа; стадии конвейера читает
   из скрытого элемента .mx-live на родительской странице (его обновляет app.py). */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const icon = (id, cls = "i") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } },
  };
  const seen = {
    get(k) { try { return sessionStorage.getItem("mx-seen-" + k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem("mx-seen-" + k, v); } catch (e) { /* ignore */ } },
  };
  const num = (x, d = 0) => Number(x || 0).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
  const MIN_THINK = 750; // мгновенный ответ (автономный режим) не должен «моргать» индикатором

  // ── мост к Streamlit ──────────────────────────────────────────
  const post = (type, data) => window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type }, data || {}), "*");
  let seq = 0;
  const newId = () => (seq = Math.max(seq + 1, Date.now()));
  const fit = () => post("streamlit:setFrameHeight", { height: window.innerHeight });

  let S = null;
  let pending = null;
  let shownNotice = 0;
  let follow = true;
  let revealUntil = 0;
  let openPanel = null;
  const rendered = new Map();

  window.addEventListener("message", (e) => {
    const d = e.data;
    if (d && d.type === "streamlit:render" && d.args && d.args.state) onState(d.args.state);
  });

  function onState(st) {
    const prev = S;
    S = st;
    if (!prev) init(st);
    if (pending && st.handled >= pending.id) return settle(st, prev);
    if (pending) return chrome(st);
    if (!prev || prev.session !== st.session) rebuild(st, true);
    else sync(st, true, null);
    chrome(st);
    notice(st.notice);
  }

  function settle(st, prev) {
    const p = pending;
    const wait = p.thinkEl ? Math.max(0, MIN_THINK - (performance.now() - p.t0)) : 0;
    setTimeout(() => {
      pending = null;
      clearTimeout(p.live);
      if (p.thinkEl) {
        p.thinkEl.classList.add("out");
        setTimeout(() => p.thinkEl.remove(), 340);
      }
      if (!prev || prev.session !== st.session) rebuild(st, true);
      else sync(st, true, p);
      chrome(st);
      setBusy(false);
      notice(st.notice);
    }, wait);
  }

  function init(st) {
    shownNotice = st.notice ? st.notice.id : 0;
    $("#title").textContent = st.lesson.title;
    renderMaterial(st);
    buildSettings(st);
    applyTraceSwitch();
    document.documentElement.classList.add("ready");
  }

  // ── лента ─────────────────────────────────────────────────────
  const lastTutor = (st) => [...st.messages].reverse().find((m) => m.role === "tutor");

  function rebuild(st, animate) {
    const thread = $("#thread");
    thread.innerHTML = "";
    rendered.clear();
    $("#summary").innerHTML = "";
    const last = lastTutor(st);
    const reveal = animate && last && seen.get(st.session) !== last.id;
    st.messages.forEach((m, i) => {
      const isNew = reveal && m === last;
      const el = m.role === "tutor" ? tutorEl(m, { reveal: isNew, first: i === 0 }) : studentEl(m.text, false);
      if (!isNew) {
        el.classList.add("enter");
        el.style.animationDelay = Math.min(i, 10) * 45 + "ms";
      }
      thread.appendChild(el);
      rendered.set(m.id, el);
    });
    if (last) seen.set(st.session, last.id);
    follow = true;
    requestAnimationFrame(() => scrollDown(false));
    summary(st);
    markCurrent();
  }

  function sync(st, animate, p) {
    const thread = $("#thread");
    const last = lastTutor(st);
    for (const m of st.messages) {
      if (rendered.has(m.id)) continue;
      if (m.role === "student" && p && p.studentEl && !p.adopted && m.text.trim() === p.text.trim()) {
        p.adopted = true;
        rendered.set(m.id, p.studentEl);
        continue;
      }
      const isNew = animate && m === last;
      const el = m.role === "tutor" ? tutorEl(m, { reveal: isNew }) : studentEl(m.text, animate);
      thread.appendChild(el);
      rendered.set(m.id, el);
      if (isNew) seen.set(st.session, m.id);
    }
    if (p && p.studentEl && !p.adopted) {
      p.studentEl.classList.add("failed");
      setTimeout(() => p.studentEl.remove(), 1800);
    }
    summary(st);
    markCurrent();
  }

  function markCurrent() {
    const all = $$("#thread .msg.tutor");
    all.forEach((el, i) => el.classList.toggle("current", i === all.length - 1 && !(S && S.finished)));
  }

  function studentEl(text, animate) {
    const el = document.createElement("div");
    el.className = "msg student" + (animate ? " sent" : "");
    el.textContent = text;
    return el;
  }

  // Текст реплики → абзацы; последний вопрос выделяем: в нём — то, что нужно ученику сейчас.
  function paragraphs(text) {
    const paras = String(text || "").trim().split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
    const out = paras.map((p) => {
      const lines = p.split("\n");
      if (lines.length >= 2 && lines.every((l) => l.length <= 110)) return { type: "block", segments: [{ t: p, q: false }] };
      return { type: "p", segments: [{ t: lines.join(" "), q: false }] };
    });
    for (let i = out.length - 1; i >= 0; i--) {
      if (out[i].type !== "p") continue;
      const t = out[i].segments[0].t;
      const sentences = t.match(/[^.!?…]+(?:[.!?…]+["»”)]*|$)\s*/g) || [t];
      let k = -1;
      sentences.forEach((s, j) => { if (s.trim().endsWith("?") || /\?["»”)]*\s*$/.test(s)) k = j; });
      if (k < 0) continue;
      const before = sentences.slice(0, k).join("");
      const q = sentences.slice(k).join("");
      out[i].segments = [{ t: before, q: false }, { t: q, q: true }].filter((s) => s.t);
      break;
    }
    return out;
  }

  function typeset(box, parts, animate) {
    let d = 0;
    const n = parts.reduce((a, p) => a + p.segments.reduce((b, s) => b + s.t.length, 0), 0) || 1;
    const step = Math.min(22, Math.max(6, 2300 / n));
    for (const p of parts) {
      const node = document.createElement(p.type === "block" ? "div" : "p");
      if (p.type === "block") node.className = "block";
      for (const seg of p.segments) {
        let holder = node;
        if (seg.q) {
          holder = document.createElement("span");
          holder.className = "q";
        }
        if (!animate) {
          holder.append(seg.t);
        } else {
          for (const token of seg.t.split(/(\s+)/)) {
            if (!token) continue;
            if (/^\s+$/.test(token)) {
              holder.append(token);
              d += step;
              continue;
            }
            const w = document.createElement("span");
            w.className = "w";
            for (const ch of token) {
              const c = document.createElement("span");
              c.className = "c";
              c.textContent = ch;
              c.style.setProperty("--d", Math.round(d) + "ms");
              w.appendChild(c);
              d += step + (/[.!?…]/.test(ch) ? step * 7 : /[,;:—]/.test(ch) ? step * 3 : 0);
            }
            holder.appendChild(w);
          }
        }
        if (holder !== node) node.appendChild(holder);
      }
      box.appendChild(node);
      d += step * 5;
    }
    return d + 700;
  }

  function tutorEl(m, opts) {
    const o = opts || {};
    const el = document.createElement("article");
    el.className = "msg tutor" + (o.first ? " first" : "");
    const text = document.createElement("div");
    text.className = "text";
    el.appendChild(text);
    const parts = paragraphs(m.text);
    if (o.reveal && !reduced()) {
      const sr = document.createElement("div");
      sr.className = "sr";
      sr.textContent = m.text;
      el.prepend(sr);
      text.setAttribute("aria-hidden", "true");
      text.classList.add("reveal");
      const total = typeset(text, parts, true);
      revealUntil = Date.now() + total;
      const done = () => {
        if (el._done) return;
        el._done = true;
        clearTimeout(el._timer);
        clearInterval(el._scroll);
        text.classList.remove("reveal", "skip");
        text.removeAttribute("aria-hidden");
        sr.remove();
        text.innerHTML = "";
        typeset(text, parts, false);
        el.classList.add("settled");
        revealUntil = 0;
      };
      el._timer = setTimeout(done, total);
      text.addEventListener("click", () => { text.classList.add("skip"); setTimeout(done, 60); }, { once: true });
      el._scroll = setInterval(() => keepInView(el), 180);
    } else {
      typeset(text, parts, false);
      requestAnimationFrame(() => el.classList.add("settled"));
    }
    el.appendChild(metaEl(m));
    if (m.trace) el.appendChild(traceEl(m));
    return el;
  }

  function metaEl(m) {
    const meta = document.createElement("div");
    meta.className = "meta";
    const bits = [];
    if (m.move && !m.opening) bits.push(`<span class="move">${esc(m.move)}</span>`);
    if (m.offline) bits.push(`<span class="rules" title="${esc(m.note)}">без нейросети</span>`);
    if (m.trace) bits.push(`<button class="why" type="button" aria-expanded="false">почему так ${icon("down")}</button>`);
    meta.innerHTML = bits.join("");
    const why = $(".why", meta);
    if (why) {
      why.addEventListener("click", () => {
        const tr = meta.nextElementSibling;
        const open = !tr.classList.contains("open");
        tr.classList.toggle("open", open);
        why.setAttribute("aria-expanded", String(open));
        if (open) setTimeout(() => keepInView(tr, true), 420);
      });
    }
    return meta;
  }

  function traceEl(m) {
    const t = m.trace;
    const a = t.analysis;
    const v = t.verifier || {};
    const steps = [];
    if (a) {
      steps.push(`<li><b>Диагностика</b><div class="kv"><span>намерение <em>${esc(a.intent)}</em></span><span>верность <em>${esc(a.verdict)}</em></span><span>глубина <em>${esc(a.depth)}/3</em></span><span>эмоция <em>${esc(a.affect)}</em></span></div>`
        + a.misconceptions.map((x) => `<div class="bad">Заблуждение: ${esc(x)}</div>`).join("")
        + (a.diagnosis ? `<div class="note">${esc(a.diagnosis)}</div>` : "") + "</li>");
      steps.push(`<li><b>Модель ученика</b><div>Понятие «${esc(t.target)}», оценка освоения ${num(t.mastery, 2)}</div></li>`);
    }
    steps.push(`<li><b>Ход — ${esc(m.move)}</b><div>${esc(t.rationale)}</div></li>`);
    if (v.regenerated) {
      steps.push(`<li><b>Проверка</b><div>Черновик отклонён: ${esc((v.issues || []).join(" "))}</div><div class="draft">${esc(v.draft)}</div>`
        + (v.fallback ? "<div>Вторая попытка тоже не прошла — взят вопрос из банка урока.</div>" : "") + "</li>");
    } else if (a) {
      steps.push(`<li><b>Проверка</b><div>Пройдена: ${v.llm ? "правила и LLM-аудитор" : "правила"}</div></li>`);
    }
    const timing = m.offline ? "" : (t.stages || []).map((s) => `${esc(s.label)} ${num(s.latency, 1)} с${s.model ? " · " + esc(s.model) : ""}`).join(" · ");
    const note = m.offline ? `<div class="timing">Ответ собран по правилам, без нейросети${m.note ? ": " + esc(m.note) : ""}</div>` : "";
    const el = document.createElement("div");
    el.className = "trace";
    el.innerHTML = `<div><ol class="steps">${steps.join("")}</ol>${timing ? `<div class="timing">${timing}</div>` : ""}${note}</div>`;
    return el;
  }

  function thinkingEl() {
    const el = document.createElement("div");
    el.className = "thinking";
    el.setAttribute("role", "status");
    el.innerHTML = '<span class="orb" aria-hidden="true"></span><span class="words"><span class="shimmer">Думаю</span><span class="stage"></span></span>';
    return el;
  }

  function swapText(box, text) {
    const old = box.lastElementChild;
    if (old && old.textContent === text && !old.classList.contains("out")) return;
    if (old) {
      old.classList.add("out");
      setTimeout(() => old.remove(), 360);
    }
    if (!text) return;
    const s = document.createElement("span");
    s.textContent = text;
    box.appendChild(s);
  }

  function watchStages(p) {
    const tick = () => {
      if (pending !== p) return;
      let t = "";
      try {
        const el = window.parent.document.querySelector(".mx-live");
        t = el ? el.textContent.trim() : "";
      } catch (e) { /* другой origin — стадии просто не показываем */ }
      if (t && p.thinkEl) swapText($(".stage", p.thinkEl), t);
      p.live = setTimeout(tick, 140);
    };
    tick();
  }

  function summary(st) {
    const box = $("#summary");
    if (!st.finished || !st.report) {
      box.innerHTML = "";
      delete box.dataset.session;
      return;
    }
    if (box.dataset.session === st.session) return;
    box.dataset.session = st.session;
    const show = () => {
      box.innerHTML = `<section class="summary"><h2>Итог занятия</h2>`
        + `<p><b>Освоено самостоятельно:</b> ${esc(st.report.done.join(", ") || "—")}</p>`
        + `<p><b>Разобрано с объяснением:</b> ${esc(st.report.told.join(", ") || "—")}</p>`
        + `<button class="btn" type="button" data-restart>${icon("restart")}Начать заново</button></section>`;
      setTimeout(() => keepInView(box, true), 300);
    };
    const wait = Math.max(0, revealUntil - Date.now());
    setTimeout(show, wait);
  }

  // ── прокрутка ─────────────────────────────────────────────────
  const dockTop = () => $("#composer").getBoundingClientRect().top - 24;
  function keepInView(el, force) {
    if (!follow && !force) return;
    const r = el.getBoundingClientRect();
    const over = r.bottom - dockTop();
    if (over > 0) window.scrollBy({ top: over, behavior: reduced() ? "auto" : "smooth" });
  }
  function scrollDown(smooth) {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth && !reduced() ? "smooth" : "auto" });
  }
  let lastY = 0;
  window.addEventListener("scroll", () => {
    const y = window.scrollY;
    $("#top").classList.toggle("scrolled", y > 8);
    const nearBottom = window.innerHeight + y >= document.documentElement.scrollHeight - 140;
    if (y < lastY - 4 && !nearBottom) follow = false;
    if (nearBottom) follow = true;
    lastY = y;
    $("#down").classList.toggle("on", !nearBottom && document.documentElement.scrollHeight > window.innerHeight * 1.6);
  }, { passive: true });
  $("#down").addEventListener("click", () => { follow = true; scrollDown(true); });

  // ── отправка ──────────────────────────────────────────────────
  function submit(type, extra, opts) {
    if (pending || !S) return false;
    const o = opts || {};
    const id = newId();
    const p = { id, type, t0: performance.now(), text: (extra && extra.text) || "" };
    if (p.text) {
      p.studentEl = studentEl(p.text, true);
      $("#thread").appendChild(p.studentEl);
    }
    pending = p;
    if (o.think) {
      p.thinkEl = thinkingEl();
      $("#thread").appendChild(p.thinkEl);
      watchStages(p);
    }
    setBusy(true);
    follow = true;
    if (p.text || o.think) requestAnimationFrame(() => keepInView(p.thinkEl || p.studentEl, true));
    post("streamlit:setComponentValue", { value: Object.assign({ id, type }, extra || {}), dataType: "json" });
    return true;
  }

  const input = $("#input");
  const sendBtn = $("#send");
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 176) + "px";
    sendBtn.disabled = !!pending || !input.value.trim() || !!(S && S.finished);
  }
  function setBusy(on) {
    $("#composer").classList.toggle("busy", on);
    $$("[data-busy]").forEach((b) => { b.disabled = on; });
    autosize();
  }
  function sendText() {
    const t = input.value.trim();
    if (!t || !S || S.finished) return;
    if (submit("send", { text: t }, { think: true })) {
      input.value = "";
      autosize();
    }
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendText();
    }
  });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); sendText(); });

  // ── верхняя строка, прогресс, состояние ───────────────────────
  function chrome(st) {
    const ol = $("#rail ol");
    const phases = st.lesson.phases;
    if (!ol.children.length) ol.innerHTML = phases.map((p) => `<li title="${esc(p.label)}"><i></i></li>`).join("");
    const cur = phases.findIndex((p) => p.key === st.phase);
    $$("li", ol).forEach((li, i) => {
      li.classList.toggle("done", i < cur);
      li.classList.toggle("now", i === cur);
    });
    swapText($("#rail .now-label"), cur >= 0 ? phases[cur].label : "");

    const pct = Math.round(st.learner.overall * 100);
    $("#ringBtn .val").style.strokeDashoffset = String(100 - pct);
    countTo($("#ringBtn b"), pct, "%");
    $("#ringBtn").setAttribute("aria-label", `Прогресс: освоено ${pct}%`);

    const c = st.connection;
    $("#connDot").dataset.s = c.state;
    const off = $("#offline");
    if (c.state === "nokey" || c.state === "down") {
      off.hidden = false;
      off.innerHTML = (c.state === "nokey" ? "Работаю без нейросети: GigaChat не подключён." : "GigaChat недоступен — пока отвечаю по правилам.")
        + ' <button class="link" type="button" data-open="settings">Подключить</button>';
    } else {
      off.hidden = true;
    }

    input.placeholder = st.finished ? "Занятие завершено" : "Ваш ответ…";
    input.disabled = !!st.finished;
    autosize();
    updateSettings(st);
    if (openPanel && openPanel.id === "panel-progress") renderProgress(st, true);
  }

  function countTo(el, target, suffix) {
    const from = parseInt(el.textContent, 10) || 0;
    if (from === target) return;
    if (reduced()) { el.textContent = target + suffix; return; }
    const t0 = performance.now();
    const dur = 900;
    const frame = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 4);
      el.textContent = Math.round(from + (target - from) * e) + suffix;
      if (k < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  const STATE_LABEL = { mastered: "освоено", told: "объяснено", active: "сейчас" };
  function renderProgress(st, animateBars) {
    const L = st.learner;
    const pct = Math.round(L.overall * 100);
    let k = 0;
    const rows = L.concepts.map((c) => `
      <div class="concept reveal-item ${c.state === "told" ? "told" : ""}" style="--k:${++k}">
        <div class="row"><span>${esc(c.short)}${c.state ? `<span class="state ${c.state === "told" ? "told" : ""}">${STATE_LABEL[c.state]}</span>` : ""}</span><small>P(L) ${num(c.p, 2)}</small></div>
        <div class="bar"><i data-v="${(c.progress * 100).toFixed(1)}%"></i></div>
        ${c.live.map((x) => `<div class="mis">${icon("alert")}<span>${esc(x)}</span></div>`).join("")}
        ${c.fixed.map((x) => `<div class="mis fixed">${icon("check")}<span>${esc(x)}</span></div>`).join("")}
      </div>`).join("");
    const last = L.last ? `<div class="section reveal-item" style="--k:${++k}"><p class="label">Последний ответ</p><dl class="dl">
        <dt>намерение</dt><dd>${esc(L.last.intent)}</dd><dt>верность</dt><dd>${esc(L.last.verdict)}</dd>
        <dt>глубина</dt><dd>${esc(L.last.depth)} из 3</dd><dt>эмоция</dt><dd>${esc(L.last.affect)}</dd></dl></div>` : "";
    $("#progress").innerHTML = `<div class="big reveal-item" style="--k:0"><b>${pct}%</b><span>освоено</span></div>
      <div class="section">${rows}</div>${last}
      <div class="foot reveal-item" style="--k:${++k}">ходов ${num(L.turns)} · токенов ${num(L.tokens)}</div>`;
    const bars = () => $$("#progress .bar i").forEach((b) => { b.style.setProperty("--v", b.dataset.v); });
    if (animateBars) requestAnimationFrame(() => requestAnimationFrame(bars));
  }

  function renderMaterial(st) {
    $("#material").innerHTML = `<div class="reading reveal-item" style="--k:0">${esc(st.lesson.source)}</div>`;
  }

  // ── настройки ─────────────────────────────────────────────────
  function buildSettings(st) {
    $("#settings").innerHTML = `
      <div class="section reveal-item" style="--k:0"><p class="label">Тема</p>
        <div class="seg" id="segTheme" role="group" aria-label="Тема"><span class="thumb"></span>
          <button type="button" data-v="light">Светлая</button><button type="button" data-v="dark">Тёмная</button><button type="button" data-v="auto">Как в системе</button></div></div>
      <div class="section reveal-item" style="--k:1"><p class="label">GigaChat</p>
        <div class="status" id="connStatus"></div>
        <div class="row-actions"><button class="tbtn" type="button" id="checkBtn" data-busy>Проверить связь</button><button class="tbtn" type="button" id="ownKeyBtn">${icon("key")}Свой ключ</button><button class="tbtn" type="button" id="forgetBtn" data-busy hidden>Забыть ключ</button></div>
        <form id="keyForm" hidden>
          <label class="field"><span>Authorization key</span><input id="keyInput" type="password" autocomplete="off" spellcheck="false" placeholder="из личного кабинета GigaChat API"></label>
          <label class="field"><span>Тип ключа</span><select id="scopeSel"><option value="">подобрать автоматически</option><option>GIGACHAT_API_PERS</option><option>GIGACHAT_API_B2B</option><option>GIGACHAT_API_CORP</option></select></label>
          <div class="row-actions"><button class="btn" type="submit" id="keyBtn" data-busy>${icon("key")}Подключить</button></div>
          <p class="muted">Ключ хранится только в памяти этой вкладки.</p>
        </form>
        <label class="field" id="modelField"><span>Модель</span><select id="modelSel" data-busy></select></label></div>
      <div class="section reveal-item" style="--k:2"><p class="label">Обращение</p>
        <div class="seg" id="segAddr" role="group" aria-label="Обращение"><span class="thumb"></span><button type="button" data-v="вы">на «вы»</button><button type="button" data-v="ты">на «ты»</button></div>
        <p class="muted" id="addrNote">Диалог начнётся заново.</p></div>
      <div class="section reveal-item" style="--k:3"><label class="switch">Показывать «почему так» под ответами<input type="checkbox" id="traceSw"></label></div>
      <div class="section reveal-item" style="--k:4"><p class="label">Проверить тьютора</p><ul class="list" id="scenarios"></ul></div>
      <div class="section reveal-item" style="--k:5"><p class="label">Демо</p><ul class="list">
        <li><button type="button" data-jump="apply" data-busy>К практике ${icon("right")}</button></li>
        <li><button type="button" data-jump="reflect" data-busy>К рефлексии ${icon("right")}</button></li></ul></div>
      <div class="section reveal-item row-actions" style="--k:6"><button class="tbtn" type="button" id="restartBtn" data-busy>${icon("restart")}<span>Начать заново</span></button><button class="tbtn" type="button" id="downloadBtn">${icon("download")}Скачать лог</button></div>`;

    $("#scenarios").innerHTML = st.scenarios.map(([label], i) => `<li><button type="button" data-sc="${i}" data-busy>${esc(label)} ${icon("right")}</button></li>`).join("");

    $$("#segTheme button").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.v, b)));
    $$("#segAddr button").forEach((b) => b.addEventListener("click", () => {
      if (!S || b.dataset.v === S.settings.address) return;
      submit("address", { value: b.dataset.v });
    }));
    $("#checkBtn").addEventListener("click", () => submit("check"));
    $("#forgetBtn").addEventListener("click", () => submit("forget_key"));
    $("#ownKeyBtn").addEventListener("click", () => {
      const f = $("#keyForm");
      f.hidden = !f.hidden;
      if (!f.hidden) $("#keyInput").focus();
    });
    $("#keyForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const key = $("#keyInput").value.trim();
      if (!key) return $("#keyInput").focus();
      if (submit("key", { key, scope: $("#scopeSel").value })) $("#keyInput").value = "";
    });
    $("#modelSel").addEventListener("change", (e) => submit("model", { value: e.target.value }));
    $("#traceSw").addEventListener("change", (e) => { store.set("mx-trace", e.target.checked ? "1" : "0"); applyTraceSwitch(); });
    $$("#scenarios [data-sc]").forEach((b) => b.addEventListener("click", () => {
      if (S.finished) return;
      closePanel();
      setTimeout(() => submit("send", { text: S.scenarios[Number(b.dataset.sc)][1] }, { think: true }), 260);
    }));
    $$("[data-jump]").forEach((b) => b.addEventListener("click", () => {
      closePanel();
      setTimeout(() => submit("jump", { phase: b.dataset.jump }, { think: true }), 260);
    }));
    const restart = $("#restartBtn");
    restart.addEventListener("click", () => {
      if (restart.dataset.armed) {
        delete restart.dataset.armed;
        closePanel();
        submit("restart");
        return;
      }
      restart.dataset.armed = "1";
      $("span", restart).textContent = "Точно начать заново?";
      setTimeout(() => { delete restart.dataset.armed; $("span", restart).textContent = "Начать заново"; }, 3200);
    });
    $("#downloadBtn").addEventListener("click", () => {
      if (!S) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([S.export], { type: "application/json" }));
      a.download = `maieutica-${S.session.slice(0, 8)}.json`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    });
    syncSeg($("#segTheme"), store.get("mx-theme", "auto"));
  }

  function updateSettings(st) {
    const c = st.connection;
    const head = { ok: "Подключён", unknown: "Ключ найден", down: "Недоступен", nokey: "Не подключён" }[c.state] || c.state;
    let small = "";
    if (c.state === "ok") small = [c.host, c.scope, ...(c.swaps || [])].filter(Boolean).join(" · ");
    else if (c.state === "unknown") small = `Источник: ${c.source || "—"}. Связь проверится при первом ответе.`;
    else if (c.state === "down") small = `${c.why || ""}${c.retry ? ` Повторю через ${c.retry} с.` : ""}`;
    else small = "Вставьте Authorization key из личного кабинета GigaChat API.";
    const status = $("#connStatus");
    status.dataset.s = c.state;
    status.innerHTML = `<div>${esc(head)}<small>${esc(small)}</small></div>`;
    $("#checkBtn").hidden = c.state === "nokey";
    $("#forgetBtn").hidden = !c.own;
    const form = $("#keyForm");
    if (c.state === "nokey" || c.state === "down") form.hidden = false;
    else if (c.own) form.hidden = true;
    $("#ownKeyBtn").hidden = c.state === "nokey";

    const sel = $("#modelSel");
    $("#modelField").hidden = c.state === "nokey";
    const opts = c.models || [];
    if (sel.dataset.sig !== opts.join("|") + st.settings.model) {
      sel.innerHTML = opts.map((m) => `<option ${m === st.settings.model ? "selected" : ""}>${esc(m)}</option>`).join("");
      sel.dataset.sig = opts.join("|") + st.settings.model;
    }

    const seg = $("#segAddr");
    seg.classList.toggle("off", st.offline);
    $("#addrNote").textContent = st.offline ? "Без нейросети — только на «вы»." : "Диалог начнётся заново.";
    syncSeg(seg, st.settings.address);

    const phase = st.phase;
    $$("[data-jump]").forEach((b) => {
      b.hidden = (b.dataset.jump === "apply" && ["apply", "reflect", "done"].includes(phase)) || (b.dataset.jump === "reflect" && ["reflect", "done"].includes(phase));
    });
    $$("#scenarios [data-sc]").forEach((b) => { b.hidden = !!st.finished; });
    $$("[data-busy]").forEach((b) => { b.disabled = !!pending; });
  }

  function syncSeg(seg, value) {
    const btns = $$("button", seg);
    const active = btns.find((b) => b.dataset.v === value) || btns[0];
    btns.forEach((b) => b.setAttribute("aria-pressed", String(b === active)));
    const thumb = $(".thumb", seg);
    requestAnimationFrame(() => {
      thumb.style.width = active.offsetWidth + "px";
      thumb.style.transform = `translateX(${active.offsetLeft}px)`;
    });
  }

  function applyTraceSwitch() {
    const on = store.get("mx-trace", "1") === "1";
    document.body.classList.toggle("no-trace", !on);
    const sw = $("#traceSw");
    if (sw) sw.checked = on;
  }

  // ── тема ──────────────────────────────────────────────────────
  const darkMq = matchMedia("(prefers-color-scheme: dark)");
  const resolveTheme = (mode) => (mode === "auto" ? (darkMq.matches ? "dark" : "light") : mode);
  function paintParent() {
    try {
      const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      window.parent.document.documentElement.style.background = bg;
      window.parent.document.body.style.background = bg;
    } catch (e) { /* другой origin */ }
  }
  function applyTheme(mode, origin) {
    store.set("mx-theme", mode);
    const seg = $("#segTheme");
    if (seg) syncSeg(seg, mode);
    const next = resolveTheme(mode);
    const set = () => {
      document.documentElement.dataset.theme = next;
      paintParent();
    };
    if (document.documentElement.dataset.theme === next) return set();
    if (!document.startViewTransition || reduced() || !origin) return set();
    const r = origin.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const R = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    const vt = document.startViewTransition(set);
    vt.ready.then(() => {
      document.documentElement.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${R}px at ${x}px ${y}px)`] },
        { duration: 720, easing: "cubic-bezier(.65,0,.35,1)", pseudoElement: "::view-transition-new(root)" },
      );
    }).catch(() => {});
  }
  $("#themeBtn").addEventListener("click", (e) => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", e.currentTarget));
  darkMq.addEventListener("change", () => { if (store.get("mx-theme", "auto") === "auto") applyTheme("auto"); });

  // ── панели ────────────────────────────────────────────────────
  function openPanelBy(name, trigger) {
    const el = $("#panel-" + name);
    if (!el) return;
    if (openPanel && openPanel !== el) closePanel(true);
    if (name === "progress" && S) renderProgress(S, false);
    el.classList.add("on");
    $("#scrim").classList.add("on");
    openPanel = el;
    el._trigger = trigger || null;
    if (name === "progress") setTimeout(() => $$("#progress .bar i").forEach((b) => b.style.setProperty("--v", b.dataset.v)), 280);
    if (name === "settings" && S) updateSettings(S);
    setTimeout(() => { const c = $("[data-close]", el); if (c) c.focus({ preventScroll: true }); }, 60);
  }
  function closePanel(quiet) {
    if (!openPanel) return;
    const el = openPanel;
    el.classList.remove("on");
    openPanel = null;
    if (!quiet) $("#scrim").classList.remove("on");
    if (el._trigger && !quiet) el._trigger.focus({ preventScroll: true });
  }
  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open]");
    if (opener) return openPanelBy(opener.dataset.open, opener);
    if (e.target.closest("[data-close]")) return closePanel();
    if (e.target.closest("[data-restart]")) return submit("restart");
  });
  $("#scrim").addEventListener("click", () => closePanel());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePanel();
    if (e.key === "/" && document.activeElement !== input && !/INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || "")) {
      e.preventDefault();
      input.focus();
    }
  });

  // ── уведомления ───────────────────────────────────────────────
  function notice(n) {
    if (!n || n.id <= shownNotice) return;
    shownNotice = n.id;
    toast(n.kind, n.text);
  }
  function toast(kind, text) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = icon(kind === "ok" ? "check" : "alert") + `<span>${esc(text)}</span>`;
    $("#toasts").appendChild(el);
    const ttl = kind === "ok" ? 4200 : 9000;
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 380); }, ttl);
    el.addEventListener("click", () => { el.classList.add("out"); setTimeout(() => el.remove(), 380); });
  }

  // ── старт ─────────────────────────────────────────────────────
  applyTheme(store.get("mx-theme", "auto"));
  window.addEventListener("resize", fit);
  post("streamlit:componentReady", { apiVersion: 1 });
  fit();
})();
