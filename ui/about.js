/* Росток — страница «Как устроен»: принцип работы, схема архитектуры, карта методов, как настроена модель и все скрытые промпты.
   Открывается маленькой ссылкой в настройках. Данные (промпты, последний реальный промпт) приходят из Python в state.about. */
(() => {
  "use strict";

  const R = window.Rostok;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { esc, icon, LOGO } = R;
  const page = $("#about");
  const scroll = $("#aboutScroll");
  let built = false;
  let io = null;

  const SECTIONS = [["principle", "Принцип"], ["scheme", "Схема"], ["methods", "Методы"], ["model", "Модель"], ["prompts", "Промпты"]];

  // ── схема архитектуры: поток одного хода ────────────────────────
  const NODES = [
    { id: "student", x: 20, y: 110, w: 128, t: "Ученик", s: "реплика", d: "Всё начинается с ответа ученика: мысли, вопроса, «не знаю» или попытки увести разговор." },
    { id: "diag", x: 182, y: 110, w: 136, t: "Диагностика", s: "LLM · function calling", llm: 1, d: "GigaChat читает реплику и заполняет строгую JSON-схему: намерение, верность, покрытые пункты, заблуждения с цитатой, глубина по ICAP, эмоциональное состояние." },
    { id: "bkt", x: 352, y: 110, w: 128, t: "Модель ученика", s: "BKT", d: "Байесовское отслеживание знаний: вероятность освоения каждой части материала, активные и снятые заблуждения, уровень подсказок, история эмоций." },
    { id: "policy", x: 514, y: 110, w: 128, t: "Политика", s: "явные правила", d: "Детерминированный код выбирает педагогический ход: уточнение, запрос обоснования, контрпример (эленхос), подсказка нужного уровня, объяснение с пересказом, кейс, рефлексия." },
    { id: "gen", x: 676, y: 110, w: 128, t: "Генерация", s: "LLM · конституция", llm: 1, d: "GigaChat формулирует реплику под выбранный ход: конституция тьютора + скрытое состояние занятия + материалы хода + последние реплики диалога." },
    { id: "check", x: 838, y: 110, w: 102, t: "Проверка", s: "правила + LLM", llm: 1, d: "Верификатор: правила (один вопрос в конце, длина, без списков) и LLM-аудитор (не выдал ли ответ, не похвалил ли ошибку, не выдумал ли факты). Не прошло — черновик переписывается." },
    { id: "text", x: 20, y: 320, w: 128, t: "Материал", s: "текст или тема", d: "Любой текст: конспект, статья, глава. Или просто тема — тогда GigaChat сначала пишет короткий конспект." },
    { id: "map", x: 250, y: 320, w: 190, t: "Карта урока", s: "ожидания · заблуждения", llm: 1, d: "Методист (LLM или правила с морфологией) превращает текст в карту: части материала, ожидаемое понимание, ключевые пункты, вопросы, лестница из трёх подсказок, типичные заблуждения с контрпримерами, кейсы." },
    { id: "offline", x: 610, y: 320, w: 190, t: "Автономный режим", s: "без нейросети", d: "Если GigaChat недоступен, диагностику делают ключевые слова и стемминг, а реплику собирают из банка вопросов и подсказок той же карты. Политика и проверка правилами работают как обычно." },
  ];
  const EDGES = [
    ["e1", "M148 142 H182", "", "student", "diag"],
    ["e2", "M318 142 H352", "", "diag", "bkt"],
    ["e3", "M480 142 H514", "", "bkt", "policy"],
    ["e4", "M642 142 H676", "", "policy", "gen"],
    ["e5", "M804 142 H838", "", "gen", "check"],
    ["e6", "M889 110 V56 Q889 44 877 44 H96 Q84 44 84 56 V110", "вопрос ученику", "check", "student"],
    ["e7", "M868 174 Q868 232 790 232 Q740 232 740 174", "переписать черновик", "check", "gen", "loop"],
    ["e8", "M148 352 H250", "", "text", "map"],
    ["e9", "M300 320 Q300 240 250 174", "что ожидать", "map", "diag"],
    ["e10", "M390 320 Q440 240 578 174", "ходы и подсказки", "map", "policy"],
    ["e11", "M705 320 V174", "если сеть недоступна", "offline", "gen", "dash"],
    ["e12", "M640 336 Q470 290 300 180", "", "offline", "diag", "dash"],
  ];

  function schemeSVG() {
    const nodes = NODES.map((n, i) => `
      <g class="node${n.llm ? " llm" : ""}" data-node="${n.id}" style="--k:${i}" tabindex="0" role="button" aria-label="${esc(n.t)}">
        <rect x="${n.x}" y="${n.y}" width="${n.w}" height="64" rx="10"/>
        ${n.llm ? `<circle class="spark" cx="${n.x + n.w - 12}" cy="${n.y + 12}" r="3.2"/>` : ""}
        <text x="${n.x + 14}" y="${n.y + 28}" class="nt">${esc(n.t)}</text>
        <text x="${n.x + 14}" y="${n.y + 47}" class="ns">${esc(n.s)}</text>
      </g>`).join("");
    const edges = EDGES.map(([id, d, label, a, b, kind], i) => `
      <g class="edge ${kind || ""}" data-a="${a}" data-b="${b}" style="--k:${i}">
        <path id="ap-${id}" d="${d}" pathLength="1" marker-end="url(#arrow)"/>
        ${kind === "dash" ? "" : `<circle r="3" class="flow"><animateMotion dur="${2.4 + (i % 3) * 0.5}s" repeatCount="indefinite" begin="${(i * 0.37).toFixed(2)}s"><mpath href="#ap-${id}"/></animateMotion></circle>`}
      </g>`).join("");
    const labels = EDGES.filter((e) => e[2]).map(([id, d, label]) => {
      const pos = { e6: [486, 36], e7: [804, 250], e9: [206, 262], e10: [476, 244], e11: [712, 262] }[id];
      return pos ? `<text class="elabel" x="${pos[0]}" y="${pos[1]}" text-anchor="middle">${esc(label)}</text>` : "";
    }).join("");
    return `<svg class="scheme" viewBox="0 0 960 420" role="img" aria-label="Схема: как устроен один ход разговора">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9 Z"/></marker></defs>
      <text class="band" x="20" y="96">один ход разговора</text>
      <text class="band" x="20" y="306">подготовка и запасной путь</text>
      ${edges}${labels}${nodes}
    </svg>`;
  }

  // ── карта методов (майнд-мэп) ───────────────────────────────────
  const BRANCHES = [
    { t: "Сократический метод", x: 190, y: 104, leaves: ["эленхос: контрпример вместо «неправильно»", "апория → майевтика", "типология вопросов Пола–Элдера", "вопрос в конце каждой реплики"] },
    { t: "Педагогика", x: 770, y: 104, leaves: ["EMT-диалог AutoTutor", "градуированный скаффолдинг", "эффект генерации", "самообъяснение и пересказ", "retrieval practice", "ICAP: от active к constructive"] },
    { t: "Модель ученика", x: 150, y: 452, leaves: ["Bayesian Knowledge Tracing", "каталог заблуждений", "покрытие ключевых пунктов", "аффект: досада, тревога, скука"] },
    { t: "Надёжность", x: 810, y: 452, leaves: ["generate → verify → regenerate", "анти-сикофантия", "удержание роли", "fallback без нейросети"] },
    { t: "Оценка", x: 480, y: 560, leaves: ["симулированные ученики-персоны", "LLM-судьи по рубрике", "сравнение с одним промптом"] },
  ];
  function methodsSVG() {
    const cx = 480, cy = 300;
    const parts = BRANCHES.map((b, i) => {
      const mx = (cx + b.x) / 2, my = (cy + b.y) / 2 + (b.y < cy ? 30 : -30);
      const up = b.y < cy;
      const leaves = b.leaves.map((l, j) => {
        const ly = up ? b.y - 32 - (b.leaves.length - 1 - j) * 19 : b.y + 34 + j * 19;
        return `<text class="leaf" x="${b.x}" y="${ly}" text-anchor="middle" style="--j:${j}">${esc(l)}</text>`;
      }).join("");
      return `<g class="branch" style="--k:${i}">
        <path class="stem" d="M${cx} ${cy} Q${mx} ${my} ${b.x} ${b.y}" pathLength="1"/>
        <g class="bnode"><rect x="${b.x - 92}" y="${b.y - 17}" width="184" height="34" rx="8"/><text x="${b.x}" y="${b.y + 5}" text-anchor="middle">${esc(b.t)}</text></g>
        ${leaves}</g>`;
    }).join("");
    return `<svg class="mind" viewBox="0 0 960 740" role="img" aria-label="Карта методов"><g transform="translate(0 76)">
      ${parts}
      <g class="core"><circle cx="${cx}" cy="${cy}" r="58"/><g transform="translate(${cx - 16} ${cy - 34}) scale(1)">${LOGO.replace('class="logo"', 'class="logo" width="32" height="32"')}</g>
        <text x="${cx}" y="${cy + 22}" text-anchor="middle">Росток</text></g>
    </g></svg>`;
  }

  // ── как настроена модель (честно: веса не дообучались) ──────────
  const MODEL = [
    ["Без дообучения весов", "GigaChat используется как есть, через API. Всё поведение задаётся архитектурой вокруг модели: так его можно проверить, объяснить и поменять за минуту, а не за новый цикл обучения."],
    ["In-context learning", "Конституция тьютора — 10 правил и few-shot примеры стиля «плохо / хорошо» на другой теме (спрос и предложение), чтобы модель копировала манеру, а не содержание."],
    ["Structured outputs", "Диагностика, аудит и карта урока возвращаются через function calling по JSON-схемам с перечислениями (enum) — это ограничивает выход модели и делает его машиночитаемым."],
    ["Декомпозиция ролей", "Одна модель — четыре роли с разными промптами и температурами: диагност (0.01), тьютор (0.55, при переписывании 0.4), аудитор (0.01), методист (0.2)."],
    ["Generate → verify → regenerate", "Черновик проходит правила и LLM-аудитора; при нарушении — повторная генерация с конкретной обратной связью, при повторном провале — вопрос из банка карты."],
    ["Knowledge tracing", "BKT обновляет вероятность освоения каждой части после каждого ответа; политика опирается на неё, а не на «ощущение» модели."],
    ["Оценка на симулированных учениках", "Стенд eval/: персоны (отличница-торопыга, путаник с заблуждениями, тревожный новичок, халявщик) проходят диалог с пайплайном и с базовым «одним промптом», LLM-судьи оценивают по рубрике."],
    ["Опора на исследования", "Решения взяты из обзора 84 проверенных работ (research/REPORT.md): AutoTutor, Bridge, StratL, TRAVER, SafeTutors, рубрика Maurya et al. и другие."],
  ];

  function build() {
    const S = R.state();
    const prompts = (S.about && S.about.prompts) || [];
    $("#aboutNav").innerHTML = SECTIONS.map(([id, t]) => `<button type="button" data-goto="${id}">${t}</button>`).join("");
    scroll.innerHTML = `
      <div class="ab-wrap">
        <section class="ab-hero reveal" id="ab-top">
          <div class="ab-logo">${LOGO}</div>
          <p class="ab-kicker">Как устроен Росток</p>
          <h1 id="aboutTitle">Сократ, разложенный на модули</h1>
          <p class="ab-lead">Росток не пытается быть «умным промптом». Модель понимает и формулирует, решает — явная педагогическая политика,
          а каждую реплику перед отправкой проверяет отдельный аудитор. Так диалог остаётся сократическим даже на двадцатом ходу.</p>
        </section>

        <section class="ab-sec reveal" id="ab-principle">
          <h2><span>01</span>Принцип</h2>
          <div class="ab-cards">
            <article class="ab-card"><b>Понимает — LLM</b><p>Диагностика раскладывает ответ ученика на намерение, верность, заблуждения и эмоцию — строго по схеме.</p></article>
            <article class="ab-card"><b>Решает — политика</b><p>Какой ход сделать, решает прозрачный код: подсказка какого уровня, контрпример, объяснение или кейс.</p></article>
            <article class="ab-card"><b>Проверяет — аудитор</b><p>Реплика не уйдёт ученику, если выдаёт ответ, хвалит ошибку или выдумывает факты вне материала.</p></article>
            <article class="ab-card"><b>Эмпатия с точностью</b><p>Тёплый тон без сикофантии: признать усилие и состояние, но не соглашаться с ошибкой ради настроения.</p></article>
          </div>
        </section>

        <section class="ab-sec reveal" id="ab-scheme">
          <h2><span>02</span>Схема</h2>
          <p class="ab-sub">Наведите на блок — подсветятся его связи. Точки бегут по пути, которым проходит каждый ход.</p>
          <div class="ab-figure">${schemeSVG()}</div>
          <p class="ab-node-info" id="nodeInfo" aria-live="polite">Выберите блок схемы, чтобы узнать, что он делает.</p>
          <div class="ab-legend"><span><i class="lg-solid"></i>поток хода</span><span><i class="lg-dash"></i>запасной путь</span><span><i class="lg-llm"></i>вызов GigaChat</span></div>
        </section>

        <section class="ab-sec reveal" id="ab-methods">
          <h2><span>03</span>Методы</h2>
          <p class="ab-sub">На чём держится разговор: от эленхоса Сократа до байесовского отслеживания знаний.</p>
          <div class="ab-figure">${methodsSVG()}</div>
        </section>

        <section class="ab-sec reveal" id="ab-model">
          <h2><span>04</span>Как настроена модель</h2>
          <p class="ab-sub">Коротко и честно: веса GigaChat мы не трогали. Качество дают архитектура, промпты, схемы и проверка.</p>
          <ol class="ab-steps">${MODEL.map(([t, d], i) => `<li style="--k:${i}"><span class="n">${String(i + 1).padStart(2, "0")}</span><div><b>${esc(t)}</b><p>${esc(d)}</p></div></li>`).join("")}</ol>
        </section>

        <section class="ab-sec reveal" id="ab-prompts">
          <h2><span>05</span>Скрытые промпты</h2>
          <p class="ab-sub">Ровно то, что уходит модели, — без сокращений. Ученик этого не видит.</p>
          <div class="ab-prompts">
            <div class="ab-tabs" role="tablist">${prompts.map((p, i) => `<button type="button" role="tab" data-prompt="${i}" aria-selected="${i === 0}"><b>${esc(p.title)}</b><small>${esc(p.role)}</small></button>`).join("")}</div>
            <div class="ab-code">
              <div class="ab-code-bar"><span id="promptName">${esc(prompts[0] ? prompts[0].title : "")}</span><button type="button" class="act" data-copy="prompt">${icon("copy")}Скопировать</button></div>
              <textarea id="promptText" readonly spellcheck="false">${esc(prompts[0] ? prompts[0].text : "")}</textarea>
            </div>
          </div>
          <h3 class="ab-h3">Полный промпт последнего хода</h3>
          <p class="ab-sub">Конституция + скрытое состояние занятия + диалог — как это собралось для вашего текущего разбора.</p>
          <div class="ab-code full">
            <div class="ab-code-bar"><span>system + история</span><button type="button" class="act" data-copy="last">${icon("copy")}Скопировать</button></div>
            <textarea id="lastPrompt" readonly spellcheck="false"></textarea>
          </div>
        </section>

        <footer class="ab-foot reveal">Росток · автор — Алексей Клушин · майевтика на GigaChat · исследование — research/REPORT.md (84 работы)</footer>
      </div>`;
    built = true;
    wire();
  }

  function fillLast() {
    const S = R.state();
    const last = (S.about && S.about.last) || "";
    const ta = $("#lastPrompt");
    if (!ta) return;
    ta.value = last || "Начните разбор и ответьте тьютору — здесь появится ровно то, что ушло модели на последнем ходе.";
    ta.classList.toggle("empty", !last);
  }

  function wire() {
    const info = $("#nodeInfo");
    $$(".node", scroll).forEach((g) => {
      const on = () => {
        const id = g.dataset.node;
        const n = NODES.find((x) => x.id === id);
        $$(".node", scroll).forEach((x) => x.classList.toggle("dim", x !== g));
        $$(".edge", scroll).forEach((e) => {
          const hit = e.dataset.a === id || e.dataset.b === id;
          e.classList.toggle("hot", hit);
          e.classList.toggle("dim", !hit);
        });
        info.classList.remove("swap");
        void info.offsetWidth;
        info.innerHTML = `<b>${esc(n.t)}.</b> ${esc(n.d)}`;
        info.classList.add("swap");
      };
      const off = () => $$(".node, .edge", scroll).forEach((x) => x.classList.remove("dim", "hot"));
      g.addEventListener("mouseenter", on);
      g.addEventListener("focus", on);
      g.addEventListener("click", on);
      g.addEventListener("mouseleave", off);
      g.addEventListener("blur", off);
    });
    $$("[data-prompt]", scroll).forEach((b) => b.addEventListener("click", () => {
      const S = R.state();
      const p = S.about.prompts[Number(b.dataset.prompt)];
      $$("[data-prompt]", scroll).forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      const ta = $("#promptText");
      ta.classList.add("fade");
      setTimeout(() => {
        ta.value = p.text;
        ta.scrollTop = 0;
        $("#promptName").textContent = p.title;
        ta.classList.remove("fade");
      }, 180);
    }));
    $$("[data-copy]", scroll).forEach((b) => b.addEventListener("click", () => {
      const ta = b.dataset.copy === "prompt" ? $("#promptText") : $("#lastPrompt");
      R.copyText(ta.value, b);
    }));
    io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }), { root: scroll, threshold: 0.12 });
    $$(".reveal", scroll).forEach((el) => io.observe(el));
    const spy = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) {
        const id = e.target.id.replace("ab-", "");
        $$("#aboutNav button").forEach((b) => b.classList.toggle("on", b.dataset.goto === id));
      }
    }), { root: scroll, rootMargin: "-40% 0px -55% 0px" });
    $$(".ab-sec", scroll).forEach((s) => spy.observe(s));
  }

  function open() {
    R.closeSettings();
    if (!built) build();
    fillLast();
    page.hidden = false;
    page.classList.remove("out");
    scroll.scrollTop = 0;
    requestAnimationFrame(() => page.classList.add("on"));
    setTimeout(() => { const b = $("[data-about-close]", page); if (b) b.focus({ preventScroll: true }); }, 60);
  }
  function close() {
    if (page.hidden) return;
    page.classList.remove("on");
    page.classList.add("out");
    setTimeout(() => { page.hidden = true; page.classList.remove("out"); }, 380);
  }

  document.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("[data-about]")) { e.stopPropagation(); open(); return; }
    if (t.closest("[data-about-close]")) return close();
    const go = t.closest("[data-goto]");
    if (go) {
      const el = $("#ab-" + go.dataset.goto);
      if (el) scroll.scrollTo({ top: el.offsetTop - 70, behavior: R.reduced() ? "auto" : "smooth" });
    }
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !page.hidden) { e.stopImmediatePropagation(); close(); } }, true);

  window.RostokAbout = { open, close, refresh: () => { if (!page.hidden) fillLast(); } };
})();
