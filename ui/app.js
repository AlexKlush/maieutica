/* Росток — логика интерфейса.
   Состояние приходит из Python (Streamlit custom component, протокол v1), события уходят обратно через setComponentValue.
   Пока Python работает, интерфейс сразу показывает действие (сообщение, карточку материала) и ждёт ответа; стадии
   читает из скрытого элемента .mx-live на родительской странице (его обновляет app.py). Одно событие за раз: новое
   событие во время работы Streamlit прервал бы текущий запуск скрипта. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const icon = (id, cls = "i") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
  const LOGO = '<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><path class="lg-stem" d="M14.8 15.2h2.4v13.6a1.2 1.2 0 0 1-2.4 0Z"/><path class="lg-leaf lg-l" d="M15.6 18.6C9.2 19.1 4.6 15.2 3.6 8.4c6.6-.2 11.6 3.5 12 10.2Z"/><path class="lg-leaf lg-r" d="M16.4 15.4C16 8.2 20.6 3.7 28.4 3.2c.3 7.9-4.2 12.3-12 12.2Z"/></svg>';
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const wide = () => matchMedia("(min-width: 861px)").matches;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } },
  };
  const tab = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };
  const num = (x, d = 0) => Number(x || 0).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100, b = a % 10;
    return a > 10 && a < 20 ? many : b > 1 && b < 5 ? few : b === 1 ? one : many;
  };
  const MIN_THINK = 750; // мгновенный ответ (правила без нейросети) не должен «моргать» индикатором

  // ── мост к Streamlit ──────────────────────────────────────────
  const post = (type, data) => window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type }, data || {}), "*");
  let seq = 0;
  const newId = () => (seq = Math.max(seq + 1, Date.now()));
  const fit = () => post("streamlit:setFrameHeight", { height: window.innerHeight });

  let S = null;
  let pending = null;
  let shownNotice = 0;
  let stick = true;
  let revealUntil = 0;
  let threadKey = "";
  const rendered = new Map();
  const app = $("#app");
  const scroller = $("#scroller");
  const input = $("#input");
  const composer = $("#composer");
  const sendBtn = $("#send");

  const clientId = (() => {
    let id = store.get("rs-client", "");
    if (!id) {
      id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
      store.set("rs-client", id);
    }
    return id;
  })();

  window.addEventListener("message", (e) => {
    const d = e.data;
    if (d && d.type === "streamlit:render" && d.args && d.args.state) onState(d.args.state);
  });

  function onState(st) {
    const prev = S;
    S = st;
    if (!prev) init(st);
    if (pending && st.handled >= pending.id) return settle(st, prev);
    if (pending) return renderSide(st);
    render(st, true);
  }

  function settle(st, prev) {
    const p = pending;
    const wait = p.thinkEl ? Math.max(0, MIN_THINK - (performance.now() - p.t0)) : 0;
    clearTimeout(p.watchdog);
    setTimeout(() => {
      pending = null;
      clearTimeout(p.live);
      if (p.thinkEl) {
        p.thinkEl.classList.add("out");
        setTimeout(() => p.thinkEl.remove(), 420);
      }
      const rejected = st.rejected && st.rejected.id === p.id;
      if (rejected) {
        threadKey = "";
        setView("home", true);
        input.value = st.rejected.text || p.text || "";
        showReject(st.rejected.why);
        autosize();
        input.focus();
      }
      render(st, true, p);
      setBusy(false);
    }, wait);
  }

  function init(st) {
    shownNotice = st.notice ? st.notice.id : 0;
    greet();
    renderSamples(st);
    buildSettings();
    applyTraceSwitch();
    placeholder();
    document.documentElement.classList.add("ready");
    if (!st.ready) submit("hello", { client: clientId, open: tab.get("rs-active") || "" }, { quiet: true });
  }

  function render(st, animate, p) {
    app.classList.remove("switching");
    renderSide(st);
    const chat = st.chat;
    if (chat) {
      tab.set("rs-active", chat.id);
      setView("chat", !!p && p.fromHome);
      renderChat(chat, animate, p);
    } else {
      tab.set("rs-active", "");
      if (!(p && p.keepHome)) setView("home", !!p && app.dataset.view === "chat");
      threadKey = "";
    }
    renderBar(chat);
    updateComposer();
    renderPanel(true);
    if (!$("#settings").hidden) updateSettings();
    if (window.RostokAbout) window.RostokAbout.refresh();
    notice(st.notice);
  }

  // ── вид: главная или разбор ───────────────────────────────────
  function setView(view, animate) {
    if (app.dataset.view === view && composer.parentElement === (view === "home" ? $("#homeSlot") : $("#dock"))) return;
    const first = composer.getBoundingClientRect();
    app.dataset.view = view;
    if (view === "home") {
      $("#homeSlot").append(composer, $("#reject"));
      greet();
    } else {
      $("#dock").insertBefore(composer, $("#fine"));
      $("#dock").append($("#reject"));
      hideReject();
    }
    closeMenus();
    placeholder();
    autosize();
    if (!animate || reduced()) return;
    const last = composer.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    if (Math.abs(dy) < 2 && Math.abs(dx) < 2) return;
    composer.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 560, easing: "cubic-bezier(.22, 1, .36, 1)" });
  }

  function greet() {
    const h = new Date().getHours();
    $("#greet").textContent = h >= 5 && h < 12 ? "Доброе утро" : h >= 12 && h < 17 ? "Добрый день" : h >= 17 && h < 23 ? "Добрый вечер" : "Доброй ночи";
    const ask = $("#ask");
    const text = "Над чем сегодня поработаем?";
    if (reduced()) { ask.textContent = text; return; }
    let i = 0;
    ask.innerHTML = text.split(/(\s+)/).map((w) => (/^\s+$/.test(w) ? w : `<span style="display:inline-block;white-space:nowrap">${[...w].map((ch) => `<span class="ch" style="--i:${i++}">${esc(ch)}</span>`).join("")}</span>`)).join("");
    ask.setAttribute("aria-label", text);
    $$(".hello .logo .lg-stem, .hello .logo .lg-leaf").forEach((el) => { el.style.animation = "none"; void el.getBBox(); el.style.animation = ""; });
  }

  // ── боковая панель ────────────────────────────────────────────
  function renderSide(st) {
    const ul = $("#chats");
    const sig = JSON.stringify([st.active, st.chats.map((c) => [c.id, c.title, Math.round(c.progress * 100), c.finished])]);
    if (ul.dataset.sig !== sig) {
      const before = new Set($$("li", ul).map((li) => li.dataset.id));
      ul.innerHTML = st.chats.map((c, k) => {
        const pct = c.finished ? icon("check") : `${Math.round(c.progress * 100)}%`;
        return `<li data-id="${esc(c.id)}" class="${c.id === st.active ? "active" : ""}" style="--k:${before.has(c.id) ? 0 : k}">
          <button type="button" class="item" data-chat="${esc(c.id)}" title="${esc(c.title)} · ${esc(c.phase)}"><span class="t">${esc(c.title)}</span><span class="pct">${pct}</span></button>
          <button type="button" class="del" data-del="${esc(c.id)}" aria-label="Удалить разбор" title="Удалить">${icon("trash")}</button></li>`;
      }).join("");
      $$("li", ul).forEach((li) => { if (before.has(li.dataset.id)) li.style.animation = "none"; });
      ul.dataset.sig = sig;
    }
    $("#chatsEmpty").hidden = st.chats.length > 0;
    $("#chatCount").textContent = st.chats.length ? String(st.chats.length) : "";
    const ex = $("#examples");
    if (!ex.children.length) {
      ex.innerHTML = st.examples.map((e, k) => `<li style="--k:${k}"><button type="button" class="item" data-example="${esc(e.id)}"><span class="t">${esc(e.title)}</span><span class="area">${esc(e.area)}</span></button></li>`).join("");
    }
    const c = st.connection;
    $("#connDot").dataset.s = c.state;
    $("#connLabel").textContent = { ok: "GigaChat", unknown: "GigaChat", down: "GigaChat недоступен", nokey: "Без нейросети" }[c.state] || "GigaChat";
    $(".conn").title = { ok: "GigaChat подключён", unknown: "Ключ GigaChat найден, связь проверится при первом ответе", down: "GigaChat недоступен — отвечаю по правилам", nokey: "GigaChat не подключён — отвечаю по правилам" }[c.state] || "";
  }

  function renderSamples(st) {
    $("#sampleGrid").innerHTML = st.examples.map((e, k) => `<button type="button" class="sample" data-example="${esc(e.id)}" style="--k:${k}">
      ${icon("right")}<span class="area">${esc(e.area)}</span><span class="t">${esc(e.title)}</span></button>`).join("");
    $$("#starters .chip").forEach((b, k) => b.style.setProperty("--k", k));
  }

  function toggleSide(force) {
    if (!wide()) {
      const open = force != null ? force : !app.classList.contains("side-open");
      app.classList.toggle("side-open", open);
      return;
    }
    const closed = force != null ? !force : document.documentElement.dataset.side !== "closed";
    document.documentElement.dataset.side = closed ? "closed" : "open";
    store.set("rs-side", closed ? "closed" : "open");
    $("#sideToggle").setAttribute("aria-label", closed ? "Развернуть панель" : "Свернуть панель");
    $("#sideToggle").title = closed ? "Развернуть панель" : "Свернуть панель";
  }

  // ── лента разбора ─────────────────────────────────────────────
  const lastTutor = (chat) => [...chat.messages].reverse().find((m) => m.role === "tutor");

  function renderChat(chat, animate, p) {
    const key = chat.id + ":" + chat.session;
    if (key !== threadKey) return rebuild(chat, animate, p);
    sync(chat, animate, p);
  }

  function docEl(chat, optimistic) {
    const m = chat.material;
    const kindText = { text: "Ваш текст", example: "Пример", topic: "Конспект GigaChat" }[chat.kind] || "Материал";
    const el = document.createElement("div");
    const brief = chat.kind === "topic";
    el.className = "msg " + (brief ? "brief" : "user");
    el.dataset.key = "doc";
    const meta = optimistic ? `${kindText} · ${num(m.chars)} ${plural(m.chars, "знак", "знака", "знаков")}` : `${kindText} · ${num(m.chars)} ${plural(m.chars, "знак", "знака", "знаков")}${m.truncated ? " · обрезан" : ""}`;
    const lines = (m.text || "").split(/\n+/).map((x) => x.trim()).filter(Boolean);
    const body = lines.filter((x) => !/^#/.test(x) && (x.length > 70 || /[.!?…:;]$/.test(x)));  // без заголовков
    const excerpt = (body.length ? body : lines).slice(0, 3).join(" ");
    el.innerHTML = `<button type="button" class="doc" data-panel="material" title="Открыть материал">
      <span class="ico">${icon("doc")}</span><span class="t">${esc(m.title)}</span><span class="m">${esc(meta)}</span>
      <span class="ex">${esc(excerpt)}</span></button>`;
    return el;
  }

  function rebuild(chat, animate, p) {
    const thread = $("#thread");
    thread.innerHTML = "";
    rendered.clear();
    $("#summary").innerHTML = "";
    threadKey = chat.id + ":" + chat.session;
    const last = lastTutor(chat);
    const seenKey = "rs-seen-" + chat.session;
    const reveal = animate && last && tab.get(seenKey) !== last.id;
    const items = [];
    if (chat.kind === "topic" && chat.request) items.push(Object.assign(document.createElement("div"), { className: "msg user", innerHTML: `<div class="bubble">${esc(chat.request)}</div>` }));
    items.push(docEl(chat));
    chat.messages.forEach((m, i) => {
      const isNew = reveal && m === last;
      const el = m.role === "tutor" ? tutorEl(m, { reveal: isNew }) : studentEl(m.text, false);
      rendered.set(m.id, el);
      items.push(el);
    });
    const fresh = p && ["new", "example", "restart"].includes(p.type);
    items.forEach((el, i) => {
      if (fresh && el.dataset.key === "doc") el.classList.remove("sent");
      else if (!el.classList.contains("revealing") && !(fresh && el.classList.contains("user"))) {
        el.classList.add("enter");
        el.style.animationDelay = (fresh ? 0 : Math.min(i, 8) * 40) + "ms";
      }
      thread.appendChild(el);
    });
    app.classList.remove("switching");
    if (last) tab.set(seenKey, last.id);
    stick = true;
    requestAnimationFrame(() => toBottom(false));
    summary(chat);
    markCurrent(chat);
  }

  function sync(chat, animate, p) {
    const thread = $("#thread");
    const last = lastTutor(chat);
    for (const m of chat.messages) {
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
      if (isNew) tab.set("rs-seen-" + chat.session, m.id);
    }
    if (p && p.studentEl && !p.adopted) {
      p.studentEl.classList.add("failed");
      setTimeout(() => p.studentEl.remove(), 1800);
    }
    summary(chat);
    markCurrent(chat);
    follow();
  }

  function markCurrent(chat) {
    const all = $$("#thread .msg.tutor");
    all.forEach((el, i) => el.classList.toggle("current", i === all.length - 1 && !chat.finished));
  }

  function studentEl(text, animate) {
    const el = document.createElement("div");
    el.className = "msg user" + (animate ? " sent" : "");
    el.innerHTML = `<div class="bubble"></div>`;
    $(".bubble", el).textContent = text;
    return el;
  }

  // Текст реплики → абзацы; последний вопрос выделяем маркером: в нём — то, что нужно ученику сейчас.
  function paragraphs(text) {
    const paras = String(text || "").trim().split(/\n{2,}/).map((x) => x.trim()).filter(Boolean);
    const out = paras.map((x) => {
      const lines = x.split("\n");
      if (lines.length >= 2 && lines.every((l) => l.length <= 110)) return { type: "block", segments: [{ t: x, q: false }] };
      return { type: "p", segments: [{ t: lines.join(" "), q: false }] };
    });
    for (let i = out.length - 1; i >= 0; i--) {
      if (out[i].type !== "p") continue;
      const t = out[i].segments[0].t;
      const sentences = t.match(/[^.!?…]+(?:[.!?…]+["»”)]*|$)\s*/g) || [t];
      let k = -1;
      sentences.forEach((s, j) => { if (/\?["»”)]*\s*$/.test(s)) k = j; });
      if (k < 0) continue;
      out[i].segments = [{ t: sentences.slice(0, k).join(""), q: false }, { t: sentences.slice(k).join("").trimEnd(), q: true }].filter((s) => s.t);
      break;
    }
    return out;
  }

  function typeset(box, parts, animate) {
    let d = 0;
    const n = parts.reduce((a, x) => a + x.segments.reduce((b, s) => b + s.t.length, 0), 0) || 1;
    const step = Math.min(20, Math.max(5, 2200 / n));
    for (const x of parts) {
      const node = document.createElement(x.type === "block" ? "div" : "p");
      if (x.type === "block") node.className = "block";
      for (const seg of x.segments) {
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
            if (/^\s+$/.test(token)) { holder.append(token); d += step; continue; }
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
    return d + 600;
  }

  function tutorEl(m, opts) {
    const o = opts || {};
    const el = document.createElement("article");
    el.className = "msg tutor";
    el.innerHTML = `<span class="av">${LOGO}</span>`;
    const text = document.createElement("div");
    text.className = "text";
    el.appendChild(text);
    const parts = paragraphs(m.text);
    if (o.reveal && !reduced()) {
      el.classList.add("revealing");
      const sr = document.createElement("div");
      sr.className = "sr";
      sr.textContent = m.text;
      el.prepend(sr);
      text.setAttribute("aria-hidden", "true");
      text.classList.add("reveal");
      const total = typeset(text, parts, true);
      revealUntil = Date.now() + total;
      const started = performance.now();
      const done = () => {
        if (el._done) return;
        el._done = true;
        clearTimeout(el._timer);
        text.classList.remove("reveal", "skip");
        text.removeAttribute("aria-hidden");
        sr.remove();
        text.innerHTML = "";
        typeset(text, parts, false);
        el.classList.remove("revealing");
        requestAnimationFrame(() => el.classList.add("settled"));
        revealUntil = 0;
        follow();
      };
      el._timer = setTimeout(done, total);
      text.addEventListener("click", () => { text.classList.add("skip"); setTimeout(done, 60); }, { once: true });
      const chars = $$(".c", text);
      const at = chars.map((c) => parseFloat(c.style.getPropertyValue("--d")) || 0);
      let idx = 0;
      const loop = () => {
        if (el._done) return;
        const spent = performance.now() - started;
        while (idx < chars.length - 1 && at[idx + 1] <= spent) idx++;
        if (stick && chars[idx]) keepVisible(chars[idx]);
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    } else {
      typeset(text, parts, false);
      requestAnimationFrame(() => el.classList.add("settled"));
    }
    el.appendChild(actsEl(m));
    if (m.trace) el.appendChild(traceEl(m));
    return el;
  }

  function actsEl(m) {
    const row = document.createElement("div");
    row.className = "acts";
    const bits = [`<button type="button" class="act copy" title="Скопировать">${icon("copy")}</button>`];
    if (m.trace) bits.push(`<button type="button" class="act why" aria-expanded="false">Почему такой вопрос ${icon("chev", "i chev")}</button>`);
    if (m.move && !m.opening) bits.push(`<span class="tag">${esc(m.move)}</span>`);
    if (m.offline) bits.push(`<span class="tag rules" title="${esc(m.note)}">без нейросети</span>`);
    row.innerHTML = bits.join("");
    $(".copy", row).addEventListener("click", (e) => copyText(m.text, e.currentTarget));
    const why = $(".why", row);
    if (why) {
      why.addEventListener("click", () => {
        const tr = row.nextElementSibling;
        const open = !tr.classList.contains("open");
        tr.classList.toggle("open", open);
        why.setAttribute("aria-expanded", String(open));
        if (open) setTimeout(() => reveal(tr), 450);
      });
    }
    return row;
  }

  function copyText(text, btn) {
    const ok = () => {
      btn.innerHTML = icon("check");
      setTimeout(() => { btn.innerHTML = icon("copy"); }, 1400);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); ok(); } catch (e) { /* нет доступа к буферу */ }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, fallback);
    else fallback();
  }

  function traceEl(m) {
    const t = m.trace;
    const a = t.analysis;
    const v = t.verifier || {};
    const steps = [];
    if (a) {
      steps.push(`<li><b>Диагностика</b><div class="kv"><span>намерение <em>${esc(a.intent)}</em></span><span>верность <em>${esc(a.verdict)}</em></span><span>глубина <em>${esc(a.depth)}/3</em></span><span>настрой <em>${esc(a.affect)}</em></span></div>`
        + a.misconceptions.map((x) => `<div class="bad">Заблуждение: ${esc(x)}</div>`).join("")
        + (a.diagnosis ? `<div>${esc(a.diagnosis)}</div>` : "") + "</li>");
      steps.push(`<li><b>Картина понимания</b><div>«${esc(t.target)}» — освоено на ${num(t.mastery * 100)}%</div></li>`);
    }
    steps.push(`<li><b>Ход: ${esc(m.move)}</b><div>${esc(t.rationale)}</div></li>`);
    if (v.regenerated) {
      steps.push(`<li><b>Проверка</b><div>Черновик отклонён: ${esc((v.issues || []).join(" "))}</div><div class="draft">${esc(v.draft)}</div>`
        + (v.fallback ? "<div>Вторая попытка тоже не прошла — взят вопрос из плана разбора.</div>" : "") + "</li>");
    } else if (a) {
      steps.push(`<li><b>Проверка</b><div>Пройдена: ${v.llm ? "правила и проверка моделью" : "правила"}</div></li>`);
    }
    const timing = m.offline ? "" : (t.stages || []).map((s) => `${esc(s.label)} ${num(s.latency, 1)} с${s.model ? " · " + esc(s.model) : ""}`).join(" · ");
    const note = m.offline ? `<div class="timing">Ответ собран по правилам, без нейросети${m.note ? ": " + esc(m.note) : ""}</div>` : "";
    const el = document.createElement("div");
    el.className = "trace";
    el.innerHTML = `<div><div class="inner"><ol class="tsteps">${steps.join("")}</ol>${timing ? `<div class="timing">${timing}</div>` : ""}${note}</div></div>`;
    return el;
  }

  // «Думаю»: росток прорастает, вокруг него — то, что сейчас происходит (читает, выбирает ход, пишет, проверяет),
  // ниже — живой список пройденных стадий со временем, призрачные строки будущего ответа и, если ждать долго, подсказки.
  const MODES = { read: "read", analyze: "read", model: "plan", plan: "plan", generate: "write", regenerate: "write", write: "write",
    open: "write", rules: "plan", verify: "check", map: "map" };
  const TIPS = [
    "Пока я думаю — попробуйте продолжить свою мысль ещё на шаг.",
    "Сократ называл это майевтикой: мысль рождается у того, кто рассуждает.",
    "Ошибка в рассуждении — не провал, а материал для следующего вопроса.",
    "Подсказки идут по нарастающей: сначала направление, потом опора.",
    "Что уже понятно, а что нет — в панели «Прогресс» справа.",
  ];
  const BUILD_TIPS = [
    "Делю материал на смысловые части и ищу в каждой главное.",
    "К каждой части готовлю открытый вопрос и три подсказки — от намёка до опоры.",
    "Ищу, где легко ошибиться: на эти места будут контрпримеры.",
    "Большой текст — дольше план. Обычно это занимает до минуты.",
  ];
  function thinkingEl(build) {
    const el = document.createElement("div");
    el.className = "thinking" + (build ? " build" : "");
    el.setAttribute("role", "status");
    el.dataset.mode = build ? "map" : "read";
    el.innerHTML = `<div class="think-head">
        <span class="orb" aria-hidden="true">${LOGO}</span>
        <span class="words"><span class="shimmer">Думаю</span><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="stage"></span><span class="clock"></span></span>
      </div>
      <ol class="trail" aria-hidden="true"></ol>
      <div class="ghost" aria-hidden="true"><i></i><i></i><i></i></div>
      <div class="tiles" aria-hidden="true">${Array.from({ length: 6 }, (_, i) => `<i style="--i:${i}"></i>`).join("")}</div>
      <p class="tip" aria-live="off"></p>`;
    return el;
  }

  function setStage(p, label, key) {
    if (!p.thinkEl || !label || p.stageLabel === label) return;
    const now = performance.now();
    const el = p.thinkEl;
    const trail = $(".trail", el);
    const cur = $("li.now", trail);
    if (cur) {
      cur.classList.remove("now");
      cur.classList.add("done");
      $("em", cur).textContent = num((now - p.stageT0) / 1000, 1) + " с";
    }
    const li = document.createElement("li");
    li.className = "now";
    li.innerHTML = `<b></b><span></span><em></em>`;
    $("span", li).textContent = label;
    trail.appendChild(li);
    while (trail.children.length > 5) trail.firstElementChild.remove();
    p.stageLabel = label;
    p.stageT0 = now;
    el.dataset.mode = MODES[key] || el.dataset.mode || "read";
    el.classList.remove("pop");
    void el.offsetWidth;
    el.classList.add("pop");  // на каждую новую стадию росток «выпускает» пыльцу
    swapText($(".stage", el), label);
  }

  function watchStages(p) {
    let tipN = 0;
    const tick = () => {
      if (pending !== p) return;
      let t = "";
      let key = "";
      try {
        const el = window.parent.document.querySelector(".mx-live");
        t = el ? el.textContent.trim() : "";
        key = el ? el.dataset.key || "" : "";
      } catch (e) { /* другой origin — стадии просто не показываем */ }
      if (t) setStage(p, t, key);
      const spent = (performance.now() - p.t0) / 1000;
      if (p.thinkEl) {
        const clock = $(".clock", p.thinkEl);
        if (spent >= 2) clock.textContent = Math.floor(spent) + " с";
        p.thinkEl.style.setProperty("--spent", Math.min(1, spent / 60).toFixed(3));  // дуга прогресса для долгого плана
        const tips = p.thinkEl.classList.contains("build") ? BUILD_TIPS : TIPS;
        const due = spent < 6 ? 0 : 1 + Math.floor((spent - 6) / 7);
        if (due > tipN) {
          tipN = due;
          const tip = $(".tip", p.thinkEl);
          tip.classList.remove("on");
          void tip.offsetWidth;
          tip.textContent = tips[(due - 1) % tips.length];
          tip.classList.add("on");
        }
      }
      p.live = setTimeout(tick, 140);
    };
    tick();
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

  function summary(chat) {
    const box = $("#summary");
    if (!chat.finished || !chat.report) {
      box.innerHTML = "";
      delete box.dataset.session;
      return;
    }
    if (box.dataset.session === chat.session) return;
    box.dataset.session = chat.session;
    const show = () => {
      box.innerHTML = `<section class="summary"><div class="card"><h2>Итог разбора</h2>`
        + `<p><b>Разобрались сами:</b> ${esc(chat.report.done.join(", ") || "—")}</p>`
        + `<p><b>Разобрали с объяснением:</b> ${esc(chat.report.told.join(", ") || "—")}</p>`
        + `<div class="row-actions"><button class="btn" type="button" data-home>${icon("plus")}Новый разбор</button>`
        + `<button class="tbtn" type="button" data-restart>${icon("restart")}Пройти ещё раз</button>`
        + `<button class="tbtn" type="button" data-panel="progress">${icon("chart")}Прогресс</button></div></div></section>`;
      setTimeout(() => reveal(box), 250);
    };
    setTimeout(show, Math.max(0, revealUntil - Date.now()));
  }

  // ── прокрутка: следуем за текстом, пока человек сам не прокрутил вверх ──
  // Своя прокрутка отличается от ручной: запоминаем, куда прокрутили сами, и время плавной прокрутки.
  let setTop = -1;
  let smoothUntil = 0;
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 72;
  function scrollTo(top, smooth) {
    const max = scroller.scrollHeight - scroller.clientHeight;
    setTop = Math.max(0, Math.min(max, Math.round(top)));
    if (smooth && !reduced()) {
      smoothUntil = performance.now() + 700;
      scroller.scrollTo({ top: setTop, behavior: "smooth" });
    } else {
      scroller.scrollTop = setTop;
    }
  }
  function toBottom(smooth) { scrollTo(scroller.scrollHeight, smooth); }
  function follow() {
    if (stick && app.dataset.view === "chat" && Date.now() > revealUntil) toBottom(false);
  }
  function keepVisible(el) {
    const r = el.getBoundingClientRect();
    const box = scroller.getBoundingClientRect();
    const over = r.bottom - (box.bottom - 28);
    if (over > 0) scrollTo(scroller.scrollTop + over, false);
  }
  function reveal(el) {
    const r = el.getBoundingClientRect();
    const box = scroller.getBoundingClientRect();
    const over = r.bottom - box.bottom + 16;
    if (over > 0) scroller.scrollBy({ top: over, behavior: reduced() ? "auto" : "smooth" });
  }
  scroller.addEventListener("scroll", () => {
    const near = nearBottom();
    const mine = Math.abs(scroller.scrollTop - setTop) <= 2 || performance.now() < smoothUntil;
    if (near) stick = true;
    else if (!mine && Date.now() > revealUntil) stick = false;
    $("#bar").classList.toggle("scrolled", scroller.scrollTop > 4);
    $("#down").classList.toggle("on", !near && app.dataset.view === "chat" && scroller.scrollHeight > scroller.clientHeight * 1.3);
  }, { passive: true });
  scroller.addEventListener("wheel", (e) => { if (e.deltaY < 0) stick = false; }, { passive: true });
  let touchY = 0;
  scroller.addEventListener("touchstart", (e) => { touchY = e.touches[0].clientY; }, { passive: true });
  scroller.addEventListener("touchmove", (e) => { if (e.touches[0].clientY > touchY + 4) stick = false; }, { passive: true });
  $("#down").addEventListener("click", () => { stick = true; toBottom(true); });
  new ResizeObserver(() => follow()).observe($("#thread"));

  // ── отправка ──────────────────────────────────────────────────
  function submit(type, extra, opts) {
    if (pending || !S) return false;
    const o = opts || {};
    const id = newId();
    const p = { id, type, t0: performance.now(), text: (extra && extra.text) || "", fromHome: !!o.fromHome, keepHome: !!o.keepHome };
    pending = p;
    if (o.chat) {  // новый разбор: сразу показываем его — карточку материала и «думаю»
      threadKey = "pending";
      rendered.clear();
      $("#thread").innerHTML = "";
      $("#summary").innerHTML = "";
      setView("chat", true);
      renderBar({ title: o.chat.title, phase: "recall", finished: false, learner: { overall: 0 }, phases: (S.chat && S.chat.phases) || null });
      const doc = o.chat.request
        ? Object.assign(document.createElement("div"), { className: "msg user sent", innerHTML: `<div class="bubble">${esc(o.chat.request)}</div>` })
        : docEl({ kind: o.chat.kind, material: { title: o.chat.title, text: o.chat.text || "", chars: (o.chat.text || "").length } }, true);
      doc.classList.add("sent");
      $("#thread").appendChild(doc);
    }
    if (p.text && type === "send") {
      p.studentEl = studentEl(p.text, true);
      $("#thread").appendChild(p.studentEl);
    }
    if (o.think) {
      p.thinkEl = thinkingEl(!!o.chat);
      setStage(p, o.first || "читаю ответ", o.firstKey || (o.chat ? "read" : "analyze"));
      $("#thread").appendChild(p.thinkEl);
      watchStages(p);
    }
    if (!o.quiet) setBusy(true);
    p.watchdog = setTimeout(() => {  // Python так и не ответил на событие (упал или связь прервалась) — не держим «думаю» вечно
      if (pending !== p) return;
      pending = null;
      clearTimeout(p.live);
      if (p.thinkEl) p.thinkEl.remove();
      setBusy(false);
      toast("error", "Сервер долго не отвечает. Попробуйте ещё раз или обновите страницу.");
    }, 200000);
    stick = true;
    if (o.think || p.studentEl) requestAnimationFrame(() => toBottom(true));
    post("streamlit:setComponentValue", { value: Object.assign({ id, type }, extra || {}), dataType: "json" });
    return true;
  }

  function looksLikeTopic(t) {
    const s = (t.match(/[.!?…](\s|$)/g) || []).length;
    return t.length < 240 && s < 3;
  }

  function sendText() {
    const t = input.value.trim();
    if (!t || !S || pending) return;
    if (app.dataset.view === "home") {
      hideReject();
      const topic = looksLikeTopic(t);
      const title = topic ? t : guessTitle(t);
      const ok = submit("new", { text: t }, {
        think: true, fromHome: true, first: topic ? "пишу конспект по теме" : "читаю материал", firstKey: topic ? "write" : "read",
        chat: topic ? { kind: "topic", title: t, request: t } : { kind: "text", title, text: t },
      });
      if (ok) { input.value = ""; autosize(); }
      return;
    }
    if (!S.chat || S.chat.finished) return;
    if (submit("send", { text: t }, { think: true })) {
      input.value = "";
      autosize();
    }
  }

  function guessTitle(t) {
    const first = t.split(/\n/).map((x) => x.replace(/^#+\s*/, "").trim()).find(Boolean) || "Ваш текст";
    if (first.length <= 80 && !/[.!?]$/.test(first)) return first;
    const words = first.split(/\s+/).slice(0, 7).join(" ");
    return words.replace(/[,;:—–.!?]+$/, "") + "…";
  }

  function startExample(id) {
    const ex = (S && S.examples || []).find((e) => e.id === id);
    if (!ex || pending) return;
    closeMenus();
    if (!wide()) app.classList.remove("side-open");
    const ok = submit("example", { name: id }, { think: true, fromHome: app.dataset.view === "home", first: "читаю материал", chat: { kind: "example", title: ex.title, text: "" } });
    if (ok) { input.value = ""; hideReject(); autosize(); }
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, Math.round(window.innerHeight * 0.38)) + "px";
    $(".field").classList.toggle("filled", input.value.length > 0);
    const chat = S && S.chat;
    const home = app.dataset.view === "home";
    sendBtn.disabled = !!pending || !input.value.trim() || (!home && (!chat || chat.finished));
    const counter = $("#chars");
    const t = input.value.trim();
    if (home && t) {
      const c = S ? S.connection.state : "nokey";
      const canWrite = c === "ok" || c === "unknown";
      counter.innerHTML = looksLikeTopic(t)
        ? (canWrite ? "Тема — <b>подготовлю конспект</b> и начнём" : "Похоже на тему — для неё нужен GigaChat, или вставьте текст")
        : `Материал · <b>${num(t.length)}</b> ${plural(t.length, "знак", "знака", "знаков")}`;
    } else {
      counter.textContent = "";
    }
  }
  function setBusy(on) {
    composer.classList.toggle("busy", on);
    $$("[data-busy]").forEach((b) => { b.disabled = on; });
    app.classList.toggle("busy", on);
    autosize();
  }
  input.addEventListener("input", () => { autosize(); if (app.dataset.view === "home") hideReject(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendText();
    }
  });
  composer.addEventListener("submit", (e) => { e.preventDefault(); sendText(); });

  function showReject(text) {
    const r = $("#reject");
    r.innerHTML = icon("alert") + `<span>${esc(text)}</span>`;
    r.hidden = false;
  }
  function hideReject() { $("#reject").hidden = true; }

  // Подсказка в пустом поле: на главной — сменяющиеся примеры, в разборе — «Ваш ответ».
  let phTimer = 0;
  const PH_HOME = ["Вставьте конспект, статью или главу учебника…", "…или напишите тему: «фотосинтез»", "…или «как работает инфляция»", "…или перетащите сюда файл .txt"];
  function placeholder() {
    clearInterval(phTimer);
    const box = $("#ph");
    const chat = S && S.chat;
    if (app.dataset.view === "home") {
      let i = 0;
      swapText(box, PH_HOME[0]);
      if (!reduced()) phTimer = setInterval(() => { i = (i + 1) % PH_HOME.length; swapText(box, PH_HOME[i]); }, 3200);
    } else {
      swapText(box, chat && chat.finished ? "Разбор завершён — начните новый или пройдите ещё раз" : "Ваш ответ…");
    }
  }

  function updateComposer() {
    const c = S.connection;
    const name = c.state === "nokey" ? "Без нейросети" : c.state === "down" ? "GigaChat недоступен" : S.settings.model;
    $("#modelName").innerHTML = `<span class="dot" data-s="${esc(c.state)}" style="display:inline-block"></span>${esc(name)}`;
    input.disabled = app.dataset.view === "chat" && !!(S.chat && S.chat.finished);
    placeholderIfChanged();
    autosize();
  }
  let lastPh = "";
  function placeholderIfChanged() {
    const sig = app.dataset.view + (S.chat ? S.chat.finished : "");
    if (sig !== lastPh) { lastPh = sig; placeholder(); }
  }

  // ── меню «+» и модель ─────────────────────────────────────────
  function openMenu(which) {
    const menu = $(which === "plus" ? "#plusMenu" : "#modelMenu");
    const btn = $(which === "plus" ? "#plusBtn" : "#modelBtn");
    const open = menu.hidden;
    closeMenus();
    if (!open) return;
    menu.innerHTML = which === "plus" ? plusMenu() : modelMenu();
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const first = $("button:not(:disabled)", menu);
    if (first) first.focus({ preventScroll: true });
  }
  function closeMenus() {
    $$(".menu").forEach((m) => { m.hidden = true; });
    $$("#plusBtn, #modelBtn").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }
  function plusMenu() {
    if (app.dataset.view === "home" || !S.chat) {
      return `<button type="button" data-act="file">${icon("clip")}Загрузить файл<span class="sub">.txt, .md</span></button>
        <button type="button" data-act="paste">${icon("doc")}Вставить из буфера</button><hr><p class="head">Примеры</p>`
        + S.examples.map((e) => `<button type="button" data-example="${esc(e.id)}">${icon("sparkles")}${esc(e.title)}<span class="sub">${esc(e.area)}</span></button>`).join("");
    }
    const chat = S.chat;
    const ph = chat.phase;
    const sc = chat.finished ? "" : `<p class="head">Проверить тьютора: ответить за ученика</p>` + chat.scenarios.map(([label], i) => `<button type="button" data-sc="${i}">${icon("flask")}${esc(label)}</button>`).join("") + "<hr>";
    return sc
      + (["apply", "reflect", "done"].includes(ph) ? "" : `<button type="button" data-jump="apply">${icon("right")}Перейти к практике</button>`)
      + (["reflect", "done"].includes(ph) ? "" : `<button type="button" data-jump="reflect">${icon("right")}Перейти к итогу</button>`)
      + `<button type="button" data-restart>${icon("restart")}Начать этот разбор заново</button>`
      + `<button type="button" data-act="download">${icon("download")}Скачать лог разбора<span class="sub">JSON</span></button>`
      + `<hr><button type="button" data-home>${icon("plus")}Новый разбор</button>`;
  }
  function modelMenu() {
    const c = S.connection;
    if (c.state === "nokey") {
      return `<p class="head">GigaChat не подключён — отвечаю по правилам, проще и однообразнее</p><button type="button" data-settings>${icon("key")}Подключить GigaChat…</button>`;
    }
    const models = (c.models || []).map((m) => `<button type="button" role="menuitemradio" aria-checked="${m === S.settings.model}" data-model="${esc(m)}">${m === S.settings.model ? icon("check") : '<span class="i-sp"></span>'}${esc(m)}</button>`).join("");
    const head = c.state === "down" ? `<p class="head">GigaChat недоступен${c.retry ? `, повторю через ${c.retry} с` : ""} — пока отвечаю по правилам</p>` : `<p class="head">Модель GigaChat</p>`;
    return head + models + `<hr><button type="button" data-act="check">${icon("restart")}Проверить связь</button><button type="button" data-settings>${icon("settings")}Подключение и ключ…</button>`;
  }

  // ── верхняя строка ────────────────────────────────────────────
  const PHASES = [["recall", "Вспоминаем"], ["explore", "Исследуем"], ["apply", "Применяем"], ["reflect", "Осмысляем"], ["done", "Итог"]];
  function renderBar(chat) {
    if (!chat) return;
    $("#chatTitle").textContent = chat.title || "";
    const phases = (chat.phases && chat.phases.map((x) => [x.key, x.label])) || PHASES;
    const cur = phases.findIndex(([k]) => k === chat.phase);
    const ol = $("#steps");
    if (ol.children.length !== phases.length) ol.innerHTML = phases.map(([, label]) => `<li><i></i><span>${esc(label)}</span></li>`).join("");
    $$("li", ol).forEach((li, i) => {
      li.classList.toggle("done", i < cur);
      li.classList.toggle("now", i === cur);
      li.title = phases[i][1];
    });
    swapText($("#phaseNow"), cur >= 0 ? phases[cur][1] : "");
    const pct = Math.round(((chat.learner && chat.learner.overall) || 0) * 100);
    $("#meterBtn .val").style.strokeDashoffset = String(100 - pct);
    countTo($("#meterPct"), pct, "%");
    $("#meterBtn").setAttribute("aria-label", `Прогресс: понятно ${pct}%`);
    const concepts = (chat.learner && chat.learner.concepts) || [];
    $("#edgeMap").innerHTML = concepts.map((c) => `<i data-s="${esc(c.state || "")}" title="${esc(c.short)}"></i>`).join("");
  }

  function countTo(el, target, suffix) {
    const from = parseInt(el.textContent, 10) || 0;
    if (from === target) { el.textContent = target + suffix; return; }
    if (reduced()) { el.textContent = target + suffix; return; }
    const t0 = performance.now();
    const frame = (t) => {
      const k = Math.min(1, (t - t0) / 900);
      el.textContent = Math.round(from + (target - from) * (1 - Math.pow(1 - k, 4))) + suffix;
      if (k < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  // ── правая панель: материал и прогресс ────────────────────────
  const TABS = ["material", "progress"];
  let panelTab = TABS.includes(store.get("rs-tab", "")) ? store.get("rs-tab", "") : "material";
  function openPanel(which) {
    if (!S || !S.chat || !TABS.includes(which)) return;
    const already = app.classList.contains("panel-open") && panelTab === which;
    if (already) return closePanel();
    panelTab = which;
    store.set("rs-tab", which);
    app.classList.add("panel-open");
    renderPanel(false, true);
  }
  function closePanel() {
    app.classList.remove("panel-open");
    $$(".tool").forEach((b) => b.classList.remove("on"));
  }
  function renderPanel(quiet, fresh) {
    const chat = S && S.chat;
    if (!app.classList.contains("panel-open") || !chat) {
      if (!chat) app.classList.remove("panel-open");
      return;
    }
    $$(".tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === panelTab)));
    $$(".bar-tools .tool").forEach((b) => b.classList.toggle("on", b.dataset.panel === panelTab));
    requestAnimationFrame(() => {
      const on = $(`.tabs [data-tab="${panelTab}"]`);
      const ink = $("#tabInk");
      ink.style.width = on.offsetWidth + "px";
      ink.style.transform = `translateX(${on.offsetLeft}px)`;
    });
    const body = $("#panelBody");
    const sig = panelTab + ":" + chat.id + ":" + chat.session + ":" + (panelTab === "progress" ? JSON.stringify(chat.learner) : chat.phase + chat.learner.concepts.map((c) => c.state).join());
    if (body.dataset.sig === sig) return;
    const changedTab = body.dataset.shown !== panelTab || fresh;
    body.dataset.sig = sig;
    body.dataset.shown = panelTab;
    if (panelTab === "material") body.innerHTML = materialHTML(chat);
    else body.innerHTML = progressHTML(chat);
    if (!changedTab && quiet) $$(":scope > *", body).forEach((el) => { el.style.animation = "none"; });
    if (changedTab) body.scrollTop = 0;
    const bars = () => $$("[data-v]", body).forEach((b) => b.style.setProperty("--v", b.dataset.v));
    requestAnimationFrame(() => requestAnimationFrame(bars));
    if (panelTab === "progress") countTo($(".big b", body), Math.round(chat.learner.overall * 100), "%");
  }

  const STATE_LABEL = { mastered: "понятно", told: "объяснено", active: "сейчас" };
  function materialHTML(chat) {
    const m = chat.material;
    const kind = { text: "Ваш текст", example: "Пример", topic: "Конспект от GigaChat" }[chat.kind] || "Материал";
    const by = m.built_by ? (m.built_by.startsWith("правила") ? "план разбора — по правилам" : m.built_by.startsWith("подготовлен") ? "план разбора подготовлен заранее" : `план разбора — ${m.built_by}`) : "";
    const concepts = chat.learner.concepts.map((c) => `<li class="${c.state}"><span>${esc(c.title)}</span>${c.state ? `<span class="st">${STATE_LABEL[c.state]}</span>` : ""}</li>`).join("");
    const text = (m.text || "").split(/\n{2,}/).map((x) => x.trim()).filter(Boolean).map((x) => {
      const line = x.replace(/^#+\s*/, "");
      return /^#/.test(x) || (line.length < 80 && !/[.!?:;]$/.test(line) && !line.includes("\n")) ? `<h5>${esc(line)}</h5>` : `<p>${esc(x).replace(/\n/g, "<br>")}</p>`;
    }).join("");
    let k = 0;
    return `<div style="--k:${k++}"><h3 class="p-title">${esc(m.title)}</h3><p class="p-meta">${esc(kind)} · ${num(m.chars)} ${plural(m.chars, "знак", "знака", "знаков")}${by ? " · " + esc(by) : ""}</p>
        ${m.note ? `<p class="p-note">${esc(m.note)}</p>` : ""}${m.truncated ? `<p class="p-note">Материал длинный: для разбора взята первая часть.</p>` : ""}</div>
      ${m.goal ? `<div class="p-sec" style="--k:${k++}"><h4>Цель</h4><p class="p-goal">${esc(m.goal)}</p></div>` : ""}
      <div class="p-sec" style="--k:${k++}"><h4>План разбора</h4><ol class="plan">${concepts}</ol></div>
      <div class="p-sec" style="--k:${k++}"><h4>Текст</h4><div class="reading">${text}</div></div>`;
  }

  function progressHTML(chat) {
    const L = chat.learner;
    const pct = Math.round(L.overall * 100);
    let k = 0;
    const rows = L.concepts.map((c) => `
      <div class="con ${c.state}">
        <div class="top"><span>${esc(c.short)}${c.state ? `<span class="st ${c.state}">${STATE_LABEL[c.state]}</span>` : ""}</span><small>${num(c.progress * 100)}%</small></div>
        <div class="bar"><i data-v="${(c.progress * 100).toFixed(1)}%"></i></div>
        ${c.live.map((x) => `<div class="mis">${icon("alert")}<span>${esc(x)}</span></div>`).join("")}
        ${c.fixed.map((x) => `<div class="mis fixed">${icon("check")}<span>${esc(x)}</span></div>`).join("")}
      </div>`).join("");
    const last = L.last ? `<div class="p-sec" style="--k:${k + 3}"><h4>Последний ответ</h4><dl class="dl">
        <dt>намерение</dt><dd>${esc(L.last.intent)}</dd><dt>верность</dt><dd>${esc(L.last.verdict)}</dd>
        <dt>глубина</dt><dd>${esc(L.last.depth)} из 3</dd><dt>настрой</dt><dd>${esc(L.last.affect)}</dd></dl></div>` : "";
    return `<div style="--k:${k++}"><div class="big"><b>0%</b><span>материала понятно</span></div><div class="big-bar"><i data-v="${pct}%"></i></div></div>
      <div class="p-sec" style="--k:${k++}"><h4>По частям материала</h4><div class="cons">${rows}</div></div>${last}
      <p class="p-foot" style="--k:${k + 4}">${num(L.turns)} ${plural(L.turns, "ответ", "ответа", "ответов")}${L.tokens ? ` · ${num(L.tokens)} ${plural(L.tokens, "токен", "токена", "токенов")}` : ""}</p>`;
  }

  // ── настройки ─────────────────────────────────────────────────
  function buildSettings() {
    $("#settingsBody").innerHTML = `
      <section class="sec"><p class="label">Тема</p>
        <div class="seg" id="segTheme" role="group" aria-label="Тема"><span class="thumb"></span>
          <button type="button" data-v="light">Светлая</button><button type="button" data-v="dark">Тёмная</button><button type="button" data-v="auto">Как в системе</button></div></section>
      <section class="sec"><p class="label">GigaChat</p>
        <div class="status" id="connStatus"></div>
        <div class="row-actions"><button class="tbtn" type="button" id="checkBtn" data-busy>${icon("restart")}Проверить связь</button><button class="tbtn" type="button" id="ownKeyBtn">${icon("key")}Свой ключ</button><button class="tbtn" type="button" id="forgetBtn" data-busy hidden>Забыть ключ</button></div>
        <form id="keyForm" class="fieldset" hidden>
          <label class="fl"><span>Authorization key</span><input id="keyInput" type="password" autocomplete="off" spellcheck="false" placeholder="из личного кабинета GigaChat API"></label>
          <label class="fl"><span>Тип ключа</span><select id="scopeSel"><option value="">подобрать автоматически</option><option>GIGACHAT_API_PERS</option><option>GIGACHAT_API_B2B</option><option>GIGACHAT_API_CORP</option></select></label>
          <div><button class="btn" type="submit" id="keyBtn" data-busy>${icon("key")}Подключить</button></div>
          <p class="muted">Ключ хранится только в памяти этой вкладки.</p>
        </form>
        <label class="fl" id="modelField" style="margin-top:12px"><span>Модель</span><select id="modelSel" data-busy></select></label></section>
      <section class="sec"><p class="label">Обращение</p>
        <div class="seg" id="segAddr" role="group" aria-label="Обращение"><span class="thumb"></span><button type="button" data-v="вы">на «вы»</button><button type="button" data-v="ты">на «ты»</button></div>
        <p class="muted" id="addrNote"></p></section>
      <section class="sec"><label class="switch">Показывать «Почему такой вопрос» под ответами<input type="checkbox" id="traceSw"></label></section>
      <section class="sec"><p class="muted" style="margin:0">Росток помогает разобраться в материале вопросами — как сократический диалог. Разборы хранятся в памяти сервера, пока приложение запущено, и видны только в этом браузере.</p>
        <button type="button" class="about-link" data-about>Как устроен Росток ${icon("chev-r")}</button></section>`;
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
    $("#traceSw").addEventListener("change", (e) => { store.set("rs-trace", e.target.checked ? "1" : "0"); applyTraceSwitch(); });
  }

  function updateSettings() {
    const c = S.connection;
    const head = { ok: "Подключён", unknown: "Ключ найден", down: "Недоступен", nokey: "Не подключён" }[c.state] || c.state;
    let small = "";
    if (c.state === "ok") small = [c.host, c.scope, ...(c.swaps || [])].filter(Boolean).join(" · ");
    else if (c.state === "unknown") small = `Источник: ${c.source || "—"}. Связь проверится при первом ответе.`;
    else if (c.state === "down") small = `${c.why || ""}${c.retry ? ` Повторю через ${c.retry} с.` : ""}`;
    else small = "Без ключа Росток отвечает по правилам — проще и однообразнее. Вставьте Authorization key из личного кабинета GigaChat API.";
    const st = $("#connStatus");
    st.innerHTML = `<span class="dot" data-s="${esc(c.state)}"></span><div><b>${esc(head)}</b><small>${esc(small)}</small></div>`;
    $("#checkBtn").hidden = c.state === "nokey";
    $("#forgetBtn").hidden = !c.own;
    const form = $("#keyForm");
    if (c.state === "nokey" || c.state === "down") form.hidden = false;
    else if (c.own) form.hidden = true;
    $("#ownKeyBtn").hidden = c.state === "nokey";
    const sel = $("#modelSel");
    $("#modelField").hidden = c.state === "nokey";
    const opts = c.models || [];
    if (sel.dataset.sig !== opts.join("|") + S.settings.model) {
      sel.innerHTML = opts.map((m) => `<option ${m === S.settings.model ? "selected" : ""}>${esc(m)}</option>`).join("");
      sel.dataset.sig = opts.join("|") + S.settings.model;
    }
    const offline = c.state === "nokey";
    const seg = $("#segAddr");
    seg.classList.toggle("off", offline);
    const started = S.chat && S.chat.messages.some((m) => m.role === "student");
    $("#addrNote").textContent = offline ? "Без нейросети — только на «вы»." : started ? "Для текущего разбора обращение уже выбрано — новое будет в следующем." : "Обращение в этом и новых разборах.";
    syncSeg(seg, S.settings.address);
    syncSeg($("#segTheme"), store.get("rs-theme", "auto"));
    $$("[data-busy]").forEach((b) => { b.disabled = !!pending; });
  }

  function syncSeg(seg, value) {
    const btns = $$("button", seg);
    const active = btns.find((b) => b.dataset.v === value) || btns[0];
    btns.forEach((b) => b.setAttribute("aria-pressed", String(b === active)));
    const thumb = $(".thumb", seg);
    requestAnimationFrame(() => {
      thumb.style.width = active.offsetWidth + "px";
      thumb.style.transform = `translateX(${active.offsetLeft - 3}px)`;
      thumb.style.left = "3px";
    });
  }

  function applyTraceSwitch() {
    const on = store.get("rs-trace", "1") === "1";
    document.body.classList.toggle("no-trace", !on);
    const sw = $("#traceSw");
    if (sw) sw.checked = on;
  }

  function openSettings() {
    closeMenus();
    const m = $("#settings");
    m.hidden = false;
    m.classList.remove("out");
    updateSettings();
    setTimeout(() => { const c = $("[data-close]", m); if (c) c.focus({ preventScroll: true }); }, 50);
  }
  function closeSettings() {
    const m = $("#settings");
    if (m.hidden) return;
    m.classList.add("out");
    setTimeout(() => { m.hidden = true; m.classList.remove("out"); }, 200);
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
    store.set("rs-theme", mode);
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
        { duration: 700, easing: "cubic-bezier(.65,0,.35,1)", pseudoElement: "::view-transition-new(root)" },
      );
    }).catch(() => {});
  }
  $("#themeBtn").addEventListener("click", (e) => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", e.currentTarget));
  darkMq.addEventListener("change", () => { if (store.get("rs-theme", "auto") === "auto") applyTheme("auto"); });

  // ── файлы ─────────────────────────────────────────────────────
  function readFile(file) {
    if (!file) return;
    if (file.size > 400 * 1024) return toast("error", "Файл слишком большой: возьму до 400 КБ текста. Вставьте нужную часть вручную.");
    if (!/\.(txt|md|markdown)$/i.test(file.name) && !/^text\//.test(file.type)) return toast("error", "Пока умею читать только текстовые файлы: .txt и .md");
    const r = new FileReader();
    r.onload = () => {
      if (app.dataset.view !== "home") {
        if (pending) return;
        submit("home", {}, { keepHome: true });
        setView("home", true);
      }
      input.value = String(r.result || "").trim();
      autosize();
      input.focus();
      composer.classList.remove("attn");
      void composer.offsetWidth;
      composer.classList.add("attn");
    };
    r.onerror = () => toast("error", "Не удалось прочитать файл");
    r.readAsText(file, "utf-8");
  }
  $("#file").addEventListener("change", (e) => { readFile(e.target.files[0]); e.target.value = ""; });
  let dragDepth = 0;
  composer.addEventListener("dragenter", (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { dragDepth++; composer.classList.add("dragging"); } });
  composer.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) composer.classList.remove("dragging"); });
  composer.addEventListener("dragover", (e) => { if (composer.classList.contains("dragging")) e.preventDefault(); });
  composer.addEventListener("drop", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    dragDepth = 0;
    composer.classList.remove("dragging");
    readFile(e.dataTransfer.files[0]);
  });

  async function pasteClipboard() {
    try {
      const t = await navigator.clipboard.readText();
      if (t) { input.value = t; autosize(); input.focus(); return; }
    } catch (e) { /* браузер не дал доступ к буферу */ }
    input.focus();
    toast("ok", "Нажмите Ctrl+V (⌘V на Mac), чтобы вставить текст");
  }

  function download() {
    if (!S || !S.chat) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([S.chat.export], { type: "application/json" }));
    const slug = (S.chat.title || "razbor").toLowerCase().replace(/[^a-zа-яё0-9]+/gi, "-").replace(/^-|-$/g, "").slice(0, 40);
    a.download = `rostok-${slug}-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  // ── клики ─────────────────────────────────────────────────────
  document.addEventListener("click", (e) => {
    const t = e.target;
    const hit = (sel) => t.closest(sel);
    let el;
    if (!hit(".menu") && !hit("#plusBtn") && !hit("#modelBtn")) closeMenus();
    if (hit("#plusBtn")) return openMenu("plus");
    if (hit("#modelBtn")) return openMenu("model");
    if ((el = hit("[data-example]"))) return startExample(el.dataset.example);
    if ((el = hit("[data-chat]"))) {
      if (!wide()) app.classList.remove("side-open");
      if (S && el.dataset.chat === S.active) return;
      if (submit("open", { chat: el.dataset.chat })) app.classList.add("switching");
      return;
    }
    if ((el = hit("[data-del]"))) {
      if (el.dataset.armed) {
        const li = el.closest("li");
        if (!pending) {
          li.classList.add("leaving");
          submit("delete", { chat: el.dataset.del }, { quiet: true, keepHome: true });
        }
        return;
      }
      el.dataset.armed = "1";
      el.classList.add("armed");
      el.title = "Нажмите ещё раз, чтобы удалить";
      setTimeout(() => { delete el.dataset.armed; el.classList.remove("armed"); el.title = "Удалить"; }, 3000);
      return;
    }
    if (hit("[data-home]")) {
      closeMenus();
      if (!wide()) app.classList.remove("side-open");
      if (app.dataset.view === "home") { input.focus(); return; }
      if (!pending) {
        submit("home", {}, { quiet: true });
        setView("home", true);
        closePanel();
        setTimeout(() => input.focus(), 80);
      }
      return;
    }
    if ((el = hit("[data-panel]"))) return openPanel(el.dataset.panel);
    if ((el = hit(".tabs [data-tab]"))) { panelTab = el.dataset.tab; store.set("rs-tab", panelTab); return renderPanel(false, true); }
    if (hit("#panelClose")) return closePanel();
    if (hit("[data-settings]")) return openSettings();
    if (hit("[data-close]") || t === $("#settings")) return closeSettings();
    if (hit("#sideToggle")) return toggleSide();
    if (hit("#sideOpen")) return toggleSide(true);
    if (t === $("#sideScrim")) return toggleSide(false);
    if ((el = hit("[data-expand]"))) {
      if (!wide()) return;
      if (document.documentElement.dataset.side === "closed") toggleSide(true);
      const g = $("#g-" + el.dataset.expand);
      setTimeout(() => g.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "nearest" }), 60);
      return;
    }
    if ((el = hit("[data-starter]"))) return starter(el.dataset.starter, el);
    if ((el = hit("[data-sc]"))) {
      closeMenus();
      if (S.chat && !S.chat.finished) submit("send", { text: S.chat.scenarios[Number(el.dataset.sc)][1] }, { think: true });
      return;
    }
    if ((el = hit("[data-jump]"))) { closeMenus(); return submit("jump", { phase: el.dataset.jump }, { think: true }); }
    if (hit("[data-restart]")) {
      closeMenus();
      if (!S.chat || pending) return;
      threadKey = "pending";
      $("#thread").innerHTML = "";
      $("#summary").innerHTML = "";
      return submit("restart", {}, { think: true, first: "готовлю первый вопрос", firstKey: "open" });
    }
    if ((el = hit("[data-model]"))) { closeMenus(); if (el.dataset.model !== S.settings.model) submit("model", { value: el.dataset.model }); return; }
    if ((el = hit("[data-act]"))) {
      closeMenus();
      const act = el.dataset.act;
      if (act === "file") return $("#file").click();
      if (act === "paste") return pasteClipboard();
      if (act === "download") return download();
      if (act === "check") return submit("check");
    }
  });

  function starter(kind, btn) {
    $$("#starters .chip").forEach((b) => b.classList.toggle("on", b === btn && kind !== "file"));
    if (kind === "file") return $("#file").click();
    if (kind === "how") {
      const how = $("#how");
      how.hidden = !how.hidden;
      btn.classList.toggle("on", !how.hidden);
      return;
    }
    const box = $("#ph");
    clearInterval(phTimer);
    swapText(box, kind === "topic" ? "Напишите тему: например, «производная» или «как работает память»" : "Вставьте сюда текст: конспект, статью, главу — от пары абзацев");
    input.focus();
    composer.classList.remove("attn");
    void composer.offsetWidth;
    composer.classList.add("attn");
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$(".menu:not([hidden])") && $("#settings").hidden && app.classList.contains("panel-open")) closePanel();
      closeMenus();
      closeSettings();
      if (!wide()) app.classList.remove("side-open");
    }
    const tag = ((document.activeElement || {}).tagName || "");
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(tag)) {
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
    const bye = () => { el.classList.add("out"); setTimeout(() => el.remove(), 320); };
    setTimeout(bye, kind === "ok" ? 4200 : 9000);
    el.addEventListener("click", bye);
  }

  // Мост для about.js (страница «Как устроен Росток»)
  window.Rostok = { state: () => S, esc, icon, LOGO, reduced, copyText, closeSettings };

  // ── старт ─────────────────────────────────────────────────────
  applyTheme(store.get("rs-theme", "auto"));
  if (document.documentElement.dataset.side === "closed") toggleSide(false);
  $("#homeSlot").append(composer, $("#reject"));
  window.addEventListener("resize", () => { fit(); autosize(); });
  post("streamlit:componentReady", { apiVersion: 1 });
  fit();
})();
