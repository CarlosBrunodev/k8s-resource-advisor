"use strict";

/* ---------- utilidades ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const enc = (v) => encodeURIComponent(v);
const dec = (v) => decodeURIComponent(v);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const STATUS = {
  adequado: { label: "Adequado", icon: "✓", cls: "ok" },
  subdimensionado: { label: "Subdimensionado", icon: "▲", cls: "bad" },
  superdimensionado: { label: "Superdimensionado", icon: "▼", cls: "bad" },
  sem_configuracao: { label: "Sem configuração", icon: "○", cls: "neutral" },
  dados_insuficientes: { label: "Dados insuficientes", icon: "◌", cls: "neutral" },
  desvio: { label: "Com desvio", icon: "▲", cls: "bad" },
  pendente: { label: "Pendente", icon: "◌", cls: "neutral" },
};
const STATUS_OPTIONS = [
  ["", "Todos"], ["desvio", "Com desvio"], ["pendente", "Pendente"], ["adequado", "Adequado"],
  ["subdimensionado", "Subdimensionado"], ["superdimensionado", "Superdimensionado"],
  ["sem_configuracao", "Sem configuração"], ["dados_insuficientes", "Dados insuficientes"],
];
const DASH = { solid: "", dash: "6 4", dot: "2 3", suggest: "4 4" };
const pct = (v, total) => ((v / Math.max(total, 1)) * 100).toFixed(2);

const badge = (status) => {
  const s = STATUS[status] || { label: status, icon: "?", cls: "neutral" };
  return `<span class="badge ${s.cls}"><span aria-hidden="true">${s.icon}</span>${esc(s.label)}</span>`;
};
const fmtCpu = (v) => (v == null ? "—" : `${Math.round(v)}m`);
const fmtMem = (v) => {
  if (v == null) return "—";
  return v >= 1024 ? `${(v / 1024).toFixed(2).replace(".", ",")}Gi` : `${Math.round(v)}Mi`;
};
const fmtVal = (v, unit) => (unit === "m" ? fmtCpu(v) : fmtMem(v));
const fmtAdj = (v, unit) => {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${fmtVal(Math.abs(v), unit)}`;
};
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("pt-BR") : "—");
const fmtDay = (sec) => new Date(sec * 1000).toLocaleString("pt-BR", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});
const fmtPct = (r) => `${Math.round(r * 100)}%`;

function toQuery(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) {
    if (v !== "" && v != null && !(k === "sort" && v === "ajuste")) p.set(k, v);
  }
  return p.toString();
}
const options = (list, selected) => list.map(
  ([value, label]) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`
).join("");

/* ---------- cabeçalho, banner e erros ---------- */
function setCrumbs(html) { $("#crumbs").innerHTML = html; }

function showBanner(meta) {
  const el = $("#banner");
  if (meta && meta.stale) {
    el.textContent = `Dados desatualizados. Última coleta bem-sucedida em ${fmtDate(meta.lastSuccessfulAt)}. Motivo: ${meta.error}`;
    el.hidden = false;
  } else {
    el.hidden = true;
    el.textContent = "";
  }
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error([body.error, body.detail].filter(Boolean).join(": ") || `HTTP ${res.status}`);
  }
  showBanner(body.meta);
  return body;
}

/* ---------- visão geral ---------- */
/* Página inicial neutra: todos os namespaces no mesmo formato. Status e ajustes aparecem só ao abrir um namespace. */
function overviewRow(n, maxWorkloads) {
  return `
    <tr>
      <td><a href="#/ns/${enc(n.name)}">${esc(n.name)}</a></td>
      <td class="bar-cell" aria-hidden="true"><span class="bar-track"><span class="bar" data-w="${pct(n.workloads, maxWorkloads)}"></span></span></td>
      <td class="num">${n.workloads}</td>
      <td class="num"><a href="#/ns/${enc(n.name)}" aria-label="Abrir namespace ${esc(n.name)}">Abrir ›</a></td>
    </tr>`;
}

async function renderOverview() {
  const data = await api("api/overview");
  setCrumbs("");
  const q = data.quality;
  const retention = q.retentionSeconds == null
    ? "não identificada"
    : `${Math.round(q.retentionSeconds / 86400)} dias${q.retentionSufficient === false ? " (menor que a janela)" : ""}`;
  const namespaces = data.namespaces;
  const totalWorkloads = namespaces.reduce((sum, n) => sum + n.workloads, 0);
  const maxWorkloads = Math.max(0, ...namespaces.map((n) => n.workloads));
  const rows = namespaces.map((n) => overviewRow(n, maxWorkloads)).join("");
  return `
    <h1>Namespaces</h1>
    <p class="muted">Clique em um namespace para ver os workloads e as recomendações.</p>
    <section class="summary">
      <div class="stat"><span class="stat-label">Namespaces</span><span class="stat-value">${namespaces.length}</span></div>
      <div class="stat"><span class="stat-label">Workloads analisados</span><span class="stat-value">${totalWorkloads}</span></div>
      <div class="stat"><span class="stat-label">Janela de análise</span><span class="stat-value">${data.window.days} dias</span></div>
    </section>
    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr><th>Namespace</th><th>Workloads</th><th class="num">Quantidade</th><th class="num"><span class="sr-only">Ação</span></th></tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="4">Nenhum workload com métricas na janela analisada.</td></tr>'}</tbody>
      </table>
    </div>
    <details class="panel">
      <summary>Dados da coleta</summary>
      <ul class="quality">
        <li>Retenção do Prometheus: ${esc(retention)}</li>
        <li>Workloads analisados: ${q.workloads}. Recentes (histórico menor que a janela): ${q.recentWorkloads}.</li>
        <li>Containers com lacunas de coleta: ${q.gapContainers}.</li>
        <li>Itens sem configuração: ${q.withoutConfig}. Com dados insuficientes: ${q.insufficientData}.</li>
        <li>Containers com throttling de CPU: ${q.throttledContainers}. Com OOMKilled: ${q.oomContainers}.</li>
      </ul>
    </details>`;
}

/* ---------- namespace ---------- */
function topItem(w) {
  return w.items
    .filter((i) => i.adjustment != null)
    .sort((a, b) => Math.abs(b.adjustment) - Math.abs(a.adjustment))[0];
}

async function renderNamespace(ns, qs) {
  const params = new URLSearchParams(qs);
  const filters = {
    q: params.get("q") || "",
    status: params.get("status") || "",
    resource: params.get("resource") || "",
    sort: params.get("sort") || "ajuste",
  };
  const query = toQuery(filters);
  const data = await api(`api/namespaces/${enc(ns)}?${query}`);
  setCrumbs(`<a href="#/">Visão geral</a> / <strong>${esc(ns)}</strong>`);
  const t = data.totals;
  const suffix = query ? `?${query}` : "";

  const rows = data.workloads.map((w) => {
    const top = topItem(w);
    return `
      <tr>
        <td><a href="#/ns/${enc(ns)}/w/${enc(w.kind)}/${enc(w.name)}${suffix}">${esc(w.name)}</a>
            <div class="muted small">${esc(w.kind)}</div></td>
        <td>${badge(w.status)}</td>
        <td class="num">${w.replicas}</td>
        <td>${top ? `${esc(top.label)} <strong>${fmtAdj(top.adjustment, top.unit)}</strong>` : "—"}</td>
        <td class="num" title="Maior razão entre consumo de referência e configuração atual">${w.pressure ? `${w.pressure.toFixed(2).replace(".", ",")}×` : "—"}</td>
        <td class="num">${fmtPct(w.coverage)}</td>
      </tr>`;
  }).join("");

  return `
    <header class="page-head">
      <div>
        <h1>Namespace ${esc(ns)}</h1>
        <p class="muted small">${data.window ? `Janela: últimos ${data.window.days} dias` : ""}</p>
      </div>
      <a class="button secondary" href="api/namespaces/${enc(ns)}/export.csv${suffix}" download>Exportar CSV</a>
    </header>

    <form id="filters" class="filters" role="search">
      <label>Buscar workload
        <input name="q" type="search" value="${esc(filters.q)}" placeholder="nome ou tipo">
      </label>
      <label>Recurso
        <select name="resource">${options([["", "Todos"], ["cpu", "CPU"], ["memory", "Memória"]], filters.resource)}</select>
      </label>
      <label>Status
        <select name="status">${options(STATUS_OPTIONS, filters.status)}</select>
      </label>
      <label>Ordenar por
        <select name="sort">${options([["ajuste", "Maior ajuste sugerido"], ["pressao", "Maior pressão observada"], ["nome", "Nome"]], filters.sort)}</select>
      </label>
    </form>

    <section class="cards">
      <div class="card static">
        <h3>CPU em requests</h3>
        <p>Pode reduzir <strong>${fmtCpu(t.capacity.cpu.reclaim)}</strong></p>
        <p>Precisa acrescentar <strong>${fmtCpu(t.capacity.cpu.add)}</strong></p>
      </div>
      <div class="card static">
        <h3>Memória em requests</h3>
        <p>Pode reduzir <strong>${fmtMem(t.capacity.memory.reclaim)}</strong></p>
        <p>Precisa acrescentar <strong>${fmtMem(t.capacity.memory.add)}</strong></p>
      </div>
      <div class="card static">
        <h3>Status dos workloads exibidos</h3>
        <div class="counts">
          <span class="badge ok">✓ ${t.counts.adequado} adequados</span>
          <span class="badge bad">▲ ${t.counts.desvio} com desvio</span>
          <span class="badge neutral">◌ ${t.counts.pendente} pendentes</span>
        </div>
      </div>
    </section>
    <p class="muted small capacity-note">Totais dos workloads exibidos com os filtros atuais, estimados para o número atual de
      réplicas ativas. Itens sem configuração não entram nos totais.</p>

    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>Workload</th><th>Status</th><th class="num">Réplicas</th>
            <th>Maior ajuste sugerido</th><th class="num">Pressão</th><th class="num">Cobertura</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="6">Nenhum workload corresponde aos filtros.</td></tr>'}</tbody>
      </table>
    </div>`;
}

/* ---------- detalhe do workload ---------- */
function chartSeries(c, res, consumption) {
  const cfg = (key) => (c.config[key] || []).map((ch) => [Date.parse(ch.at) / 1000, ch.value]);
  const suggested = (kind) => c.items.find((i) => i.resource === res && i.kind === kind)?.suggested ?? null;
  return [
    { label: "Consumo (réplica mais carregada)", color: "#4C8DFF", style: "solid", points: consumption },
    { label: "Request", color: "#F97316", style: "dash", step: true, points: cfg(`${res}_request`) },
    { label: "Limit", color: "#D946EF", style: "dot", step: true, points: cfg(`${res}_limit`) },
    { label: "Sugerido (request)", color: "#14B8A6", style: "suggest", value: suggested("request") },
  ];
}

function chartSvg(series, unit) {
  const W = 720, H = 240, L = 64, R = 16, T = 14, B = 30;
  const consumption = series[0].points || [];
  if (!consumption.length) return '<p class="muted">Sem série de consumo no período.</p>';
  const pts = series.flatMap((s) => s.points || []);
  const values = pts.map((p) => p[1]).concat(series.filter((s) => s.value != null).map((s) => s.value));
  const x0 = Math.min(...pts.map((p) => p[0]));
  const x1 = Math.max(x0 + 1, ...pts.map((p) => p[0]));
  const y1 = Math.max(...values, 1) * 1.1;
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - v / y1) * (H - T - B);

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = (y1 * i) / 4;
    grid += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/>`;
    grid += `<text class="axis" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${fmtVal(v, unit)}</text>`;
  }
  [[0, "start"], [0.5, "middle"], [1, "end"]].forEach(([f, anchor]) => {
    const t = x0 + (x1 - x0) * f;
    grid += `<text class="axis" x="${X(t)}" y="${H - 8}" text-anchor="${anchor}">${fmtDay(t)}</text>`;
  });

  const paths = series.map((s) => {
    if (s.value != null) {
      return `<line x1="${L}" x2="${W - R}" y1="${Y(s.value)}" y2="${Y(s.value)}" stroke="${s.color}" `
        + `stroke-width="2" stroke-dasharray="${DASH[s.style]}"><title>${esc(s.label)}: ${fmtVal(s.value, unit)}</title></line>`
        + `<text class="direct" x="${W - R}" y="${Y(s.value) - 6}" text-anchor="end">Sugerido ${fmtVal(s.value, unit)}</text>`;
    }
    const p = s.points || [];
    if (!p.length) return "";
    let d;
    if (s.step) {
      d = `M${X(p[0][0]).toFixed(1)},${Y(p[0][1]).toFixed(1)}`;
      for (let i = 1; i < p.length; i++) d += `H${X(p[i][0]).toFixed(1)}V${Y(p[i][1]).toFixed(1)}`;
      d += `H${X(x1).toFixed(1)}`;
    } else {
      d = p.map((q, i) => `${i ? "L" : "M"}${X(q[0]).toFixed(1)},${Y(q[1]).toFixed(1)}`).join("");
    }
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-dasharray="${DASH[s.style]}"/>`;
  }).join("");

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Gráfico de ${unit}">${grid}${paths}</svg>`;
}

function chartBlock(title, series, unit) {
  const legend = series.map((s) => {
    const hasData = s.value != null || (s.points && s.points.length);
    return `<li><svg class="swatch" viewBox="0 0 28 8" aria-hidden="true">`
      + `<line x1="0" x2="28" y1="4" y2="4" stroke="${s.color}" stroke-width="2.5" stroke-dasharray="${DASH[s.style]}"/>`
      + `</svg>${esc(s.label)}${hasData ? "" : ' <span class="muted">(sem valor)</span>'}</li>`;
  }).join("");
  return `<figure class="chart-block"><figcaption>${esc(title)}</figcaption>${chartSvg(series, unit)}`
    + `<ul class="legend">${legend}</ul></figure>`;
}

/* escala: consumo de referência (faixa), valor atual (marca escura) e sugerido (marca tracejada) */
function gauge(it) {
  const vals = [it.current, it.reference, it.suggested].filter((v) => v != null);
  if (!vals.length) return "";
  const max = Math.max(...vals) * 1.15 || 1;
  const pos = (v) => (v == null ? null : Math.min(100, (v / max) * 100).toFixed(2));
  const ref = pos(it.reference), cur = pos(it.current), sug = pos(it.suggested);
  const label = `Atual ${fmtVal(it.current, it.unit)}; consumo de referência ${fmtVal(it.reference, it.unit)}; sugerido ${fmtVal(it.suggested, it.unit)}`;
  return `<div class="gauge" role="img" aria-label="${esc(label)}">`
    + (ref != null ? `<span class="gauge-ref" data-w="${ref}"></span>` : "")
    + (sug != null ? `<span class="gauge-sug" data-left="${sug}"></span>` : "")
    + (cur != null ? `<span class="gauge-cur" data-left="${cur}"></span>` : "")
    + "</div>";
}

/* larguras e posições via CSSOM: o CSP do nginx bloqueia atributos style inline */
function applyLayout() {
  app.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${el.dataset.w}%`; });
  app.querySelectorAll("[data-left]").forEach((el) => { el.style.left = `${el.dataset.left}%`; });
}

function itemRow(c, it, id) {
  const cfgKey = `${it.resource}_${it.kind}`;
  const changes = c.config[cfgKey] || [];
  const history = changes.length
    ? changes.map((ch) => `${fmtDate(ch.at)}: ${fmtVal(ch.value, it.unit)}`).join("; ")
    : "sem valor configurado no período";
  return `
    <tr data-row="${id}" tabindex="0" aria-expanded="false">
      <td>${esc(c.name)}</td>
      <td>${esc(it.label)}${gauge(it)}</td>
      <td class="num">${fmtVal(it.current, it.unit)}</td>
      <td class="num">${fmtVal(it.reference, it.unit)}</td>
      <td class="num">${fmtVal(it.suggested, it.unit)}</td>
      <td class="num">${fmtAdj(it.adjustment, it.unit)}</td>
      <td>${badge(it.status)}</td>
    </tr>
    <tr hidden>
      <td colspan="7">
        <p><strong>Método:</strong> ${esc(it.method)}</p>
        <p><strong>Justificativa:</strong> ${esc(it.justification)}</p>
        <p><strong>Histórico de ${esc(it.label)}:</strong> ${esc(history)}</p>
      </td>
    </tr>`;
}

function podsTable(pods) {
  const rows = pods.map((p) => `
    <tr>
      <td>${esc(p.pod)}</td>
      <td>${p.active ? "Ativa" : "Encerrada"}</td>
      <td class="num">${fmtCpu(p.cpuRef)}</td>
      <td class="num">${fmtCpu(p.cpuPeak)}</td>
      <td class="num">${fmtCpu(p.cpuSuggested)}</td>
      <td class="num">${fmtMem(p.memRef)}</td>
      <td class="num">${fmtMem(p.memPeak)}</td>
      <td class="num">${fmtMem(p.memSuggested)}</td>
      <td class="num">${fmtPct(p.coverage)}</td>
      <td class="num">${p.oomEvents}</td>
      <td class="num">${p.throttlingPct.toFixed(1).replace(".", ",")}%</td>
    </tr>`).join("");
  return `
    <div class="table-wrap">
      <table class="grid compact">
        <thead>
          <tr>
            <th>Réplica</th><th>Situação</th><th class="num">CPU referência</th><th class="num">CPU pico</th>
            <th class="num">Sugerido CPU</th><th class="num">Memória referência</th><th class="num">Memória pico</th>
            <th class="num">Sugerido memória</th><th class="num">Cobertura</th><th class="num">OOM</th>
            <th class="num">Throttling</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function containerSection(c, ci, charts) {
  const chart = charts.find((x) => x.container === c.name) || { cpu: { consumption: [] }, memory: { consumption: [] } };
  const rows = c.items.map((it, idx) => itemRow(c, it, `row-${ci}-${idx}`)).join("");
  return `
    <section class="panel" aria-label="Container ${esc(c.name)}">
      <header class="panel-head">
        <h2>Container <code>${esc(c.name)}</code></h2>
        <span class="muted small">Reinícios: ${c.restarts}; OOMKilled no período: ${c.oomEvents} intervalo(s); throttling de CPU: ${c.throttlingPct.toFixed(1).replace(".", ",")}%</span>
      </header>
      <div class="table-wrap">
        <table class="grid">
          <thead>
            <tr>
              <th>Container</th><th>Recurso</th><th class="num">Atual</th><th class="num">Consumo de referência</th>
              <th class="num">Sugerido</th><th class="num">Ajuste</th><th>Status</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="gauge-key" aria-label="Legenda da escala">
        <span><i class="key-ref"></i>Consumo de referência</span>
        <span><i class="key-cur"></i>Atual</span>
        <span><i class="key-sug"></i>Sugerido</span>
        <span>Selecione uma linha para ver a justificativa e o histórico.</span>
      </div>
      <div class="charts">
        ${chartBlock("CPU (m)", chartSeries(c, "cpu", chart.cpu.consumption), "m")}
        ${chartBlock("Memória (Mi)", chartSeries(c, "memory", chart.memory.consumption), "Mi")}
      </div>
      <h3>Réplicas</h3>
      ${podsTable(c.pods)}
      <h3>Trecho YAML para revisão</h3>
      <div class="yaml-wrap">
        <pre class="yaml" id="yaml-${ci}">${esc(c.yaml)}</pre>
        <button class="button secondary" type="button" data-copy="yaml-${ci}">Copiar YAML</button>
      </div>
    </section>`;
}

async function renderDetail(ns, kind, name, qs) {
  const data = await api(`api/namespaces/${enc(ns)}/workloads/${enc(kind)}/${enc(name)}`);
  const w = data.workload;
  const back = `#/ns/${enc(ns)}${qs ? `?${qs}` : ""}`;
  setCrumbs(`<a href="#/">Visão geral</a> / <a href="${back}">${esc(ns)}</a> / <strong>${esc(name)}</strong>`);
  return `
    <a class="back" href="${back}">Voltar para ${esc(ns)}</a>
    <header class="page-head">
      <div>
        <h1>${esc(name)}</h1>
        <p class="muted small">${esc(w.kind)}, namespace ${esc(ns)}</p>
      </div>
      ${badge(w.status)}
    </header>
    <section class="cards">
      <div class="card static"><h3>Réplicas ativas</h3><p class="big">${w.replicas}</p></div>
      <div class="card static">
        <h3>Histórico disponível</h3>
        <p class="big">${Math.round(w.historyHours)} h</p>
        ${w.recent ? '<p class="warn">Workload recente: histórico menor que a janela analisada.</p>' : ""}
      </div>
      <div class="card static"><h3>Cobertura da coleta</h3><p class="big">${fmtPct(w.coverage)}</p></div>
    </section>
    ${w.containers.map((c, ci) => containerSection(c, ci, data.charts)).join("")}`;
}

/* ---------- fórmula (administração) ---------- */
const FORMULA_FIELDS = [
  { name: "window_days", label: "Janela de análise (dias)", min: 1, max: 30, step: 1,
    hint: "Dias de histórico usados em todos os cálculos (1 a 30)." },
  { name: "reference_percentile", label: "Percentil do consumo de referência", min: 1, max: 100, step: "any",
    hint: "Base do request. P95: 95% dos instantes ficaram abaixo deste valor." },
  { name: "request_margin_percent", label: "Margem do request (%)", min: 0, max: 500, step: "any",
    hint: "Request sugerido = referência × (1 + margem)." },
  { name: "cpu_limit_percentile", label: "Percentil do pico de CPU (limit)", min: 1, max: 100, step: "any",
    hint: "Base do limit de CPU. P99 é o padrão." },
  { name: "memory_limit_percentile", label: "Percentil do pico de memória (limit)", min: 1, max: 100, step: "any",
    hint: "Base do limit de memória. 100 usa o máximo observado." },
  { name: "limit_margin_percent", label: "Margem do limit (%)", min: 0, max: 500, step: "any",
    hint: "Limit sugerido = pico × (1 + margem)." },
  { name: "tolerance_percent", label: "Tolerância para “adequado” (±%)", min: 0, max: 100, step: "any",
    hint: "Faixa em torno do valor sugerido que é classificada como adequada." },
];

const fmtNum = (v) => String(Number(v));
const pctLabel = (p) => (Number(p) >= 100 ? "máximo" : `P${fmtNum(p)}`);

/* prévia calculada no navegador, só para leitura; o texto oficial vem do back-end após salvar */
function previewTexts(v) {
  const days = fmtNum(v.window_days);
  return {
    request: `${days} d · ${pctLabel(v.reference_percentile)} · margem ${fmtNum(v.request_margin_percent)}%`,
    cpuLimit: `${days} d · ${pctLabel(v.cpu_limit_percentile)} · margem ${fmtNum(v.limit_margin_percent)}%`,
    memoryLimit: `${days} d · ${pctLabel(v.memory_limit_percentile)} · margem ${fmtNum(v.limit_margin_percent)}%`,
  };
}

function formulaList(values, texts) {
  return `
    <li><strong>Request:</strong> consumo de referência × (1 + ${fmtNum(values.request_margin_percent)}%). Método: ${esc(texts.request)}</li>
    <li><strong>Limit de CPU:</strong> pico × (1 + ${fmtNum(values.limit_margin_percent)}%). Método: ${esc(texts.cpuLimit)}</li>
    <li><strong>Limit de memória:</strong> pico × (1 + ${fmtNum(values.limit_margin_percent)}%). Método: ${esc(texts.memoryLimit)}</li>
    <li><strong>Tolerância:</strong> ±${fmtNum(values.tolerance_percent)}%. Valores fora dessa faixa são classificados como subdimensionado ou superdimensionado.</li>`;
}

function formField(f, value, editable) {
  return `
    <label>${esc(f.label)}
      <input name="${f.name}" type="number" inputmode="decimal" min="${f.min}" max="${f.max}" step="${f.step}"
        value="${esc(value)}" aria-describedby="hint-${f.name}"${editable ? "" : " disabled"}>
      <span class="hint" id="hint-${f.name}">${esc(f.hint)}</span>
      <span class="field-error" data-error="${f.name}" hidden></span>
    </label>`;
}

async function renderAdmin() {
  const data = await api("api/formula");
  setCrumbs(`<a href="#/">Visão geral</a> / <strong>Fórmula</strong>`);
  const { values, texts } = data.formula;
  const editable = data.editable;
  const notes = [];
  if (!editable) notes.push("Edição desabilitada: o back-end não tem ADMIN_TOKEN configurado.");
  if (!data.persisted) notes.push("A fórmula não está gravada em arquivo (FORMULA_FILE): a edição vale só até o back-end reiniciar.");
  return `
    <h1>Fórmula de recomendação</h1>
    <p class="muted">Os parâmetros valem para todos os namespaces e workloads. Ao salvar, o back-end recalcula os resultados e os gráficos.</p>

    <section class="panel">
      <h2>Fórmula vigente</h2>
      <ul class="formula-list" id="formula-current">${formulaList(values, texts)}</ul>
    </section>

    <section class="panel">
      <h2>Editar parâmetros</h2>
      ${notes.map((n) => `<p class="warn">${esc(n)}</p>`).join("")}
      <form id="formula-form" novalidate>
        <div class="form-grid">
          ${FORMULA_FIELDS.map((f) => formField(f, values[f.name], editable)).join("")}
          <label>Token de administrador
            <input name="token" type="password" autocomplete="off"${editable ? "" : " disabled"}>
            <span class="hint">Valor de ADMIN_TOKEN no back-end. O navegador não guarda este token.</span>
            <span class="field-error" data-error="token" hidden></span>
          </label>
        </div>
        <div class="preview" id="formula-preview" aria-live="polite"></div>
        <div class="form-actions">
          <button class="button" type="submit"${editable ? "" : " disabled"}>Salvar e recalcular</button>
          <button class="button secondary" type="reset">Desfazer alterações</button>
          <span class="form-status" id="form-status" role="status"></span>
        </div>
      </form>
    </section>`;
}

function formValues(form) {
  const out = {};
  for (const f of FORMULA_FIELDS) {
    const raw = form.elements[f.name].value.trim();
    const n = raw === "" ? NaN : Number(raw.replace(",", "."));
    out[f.name] = Number.isFinite(n) ? n : null;
  }
  return out;
}

function updatePreview(form) {
  const box = $("#formula-preview", form);
  const v = formValues(form);
  if (Object.values(v).some((x) => x === null)) {
    box.innerHTML = '<p class="muted">Preencha todos os campos para ver a prévia.</p>';
    return;
  }
  const t = previewTexts(v);
  box.innerHTML = `<p><strong>Prévia (ainda não salva)</strong></p>
    <p>Request: ${esc(t.request)}</p><p>Limit de CPU: ${esc(t.cpuLimit)}</p><p>Limit de memória: ${esc(t.memoryLimit)}</p>`;
}

function clearFormErrors(form) {
  form.querySelectorAll("[data-error]").forEach((el) => { el.hidden = true; el.textContent = ""; });
  form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
}

function showFieldErrors(form, fields) {
  for (const [name, message] of Object.entries(fields)) {
    const err = form.querySelector(`[data-error="${name}"]`);
    if (err) { err.textContent = message; err.hidden = false; }
    const input = form.elements[name];
    if (input) input.setAttribute("aria-invalid", "true");
  }
}

async function saveFormula(form) {
  const status = $("#form-status", form);
  const submit = form.querySelector('button[type="submit"]');
  clearFormErrors(form);
  status.classList.remove("error");
  status.textContent = "Salvando e recalculando…";
  submit.disabled = true;
  try {
    const res = await fetch("api/formula", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Admin-Token": form.elements.token.value,
      },
      body: JSON.stringify(formValues(form)),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) {
      showBanner(body.meta);
      const { values, texts } = body.formula;
      $("#formula-current").innerHTML = formulaList(values, texts);
      // o valor salvo passa a ser o padrão do formulário: "Desfazer" volta para ele
      FORMULA_FIELDS.forEach((f) => { form.elements[f.name].defaultValue = String(values[f.name]); });
      form.elements.token.value = "";
      status.textContent = `Fórmula salva. Resultados recalculados às ${new Date(body.generatedAt).toLocaleTimeString("pt-BR")}.`;
      return;
    }
    if (res.status === 400 && body.fields) {
      showFieldErrors(form, body.fields);
      status.textContent = "Corrija os campos destacados.";
    } else {
      status.textContent = [body.error, body.detail].filter(Boolean).join(": ") || `Erro HTTP ${res.status}`;
    }
    status.classList.add("error");
    if (body.formula) $("#formula-current").innerHTML = formulaList(body.formula.values, body.formula.texts);
  } catch (err) {
    status.textContent = `Não foi possível salvar: ${err.message}`;
    status.classList.add("error");
  } finally {
    submit.disabled = false;
  }
}

function setTabs(path) {
  const active = path === "/admin" ? "admin" : "visao";
  document.querySelectorAll(".tabs a").forEach((a) => {
    if (a.dataset.tab === active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

/* ---------- roteamento ---------- */
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const i = raw.indexOf("?");
  return { path: i < 0 ? raw : raw.slice(0, i), qs: i < 0 ? "" : raw.slice(i + 1) };
}

async function route() {
  const { path, qs } = parseHash();
  if (path === "/admin") return renderAdmin();
  let m = path.match(/^\/ns\/([^/]+)\/w\/([^/]+)\/([^/]+)$/);
  if (m) return renderDetail(dec(m[1]), dec(m[2]), dec(m[3]), qs);
  m = path.match(/^\/ns\/([^/]+)$/);
  if (m) return renderNamespace(dec(m[1]), qs);
  return renderOverview();
}

let renderSeq = 0;
async function render() {
  const seq = ++renderSeq;
  const focused = document.activeElement && document.activeElement.name;
  let html;
  try {
    html = await route();
  } catch (err) {
    setCrumbs("");
    html = `<section class="panel error"><h2>Não foi possível carregar os dados</h2>
      <p>${esc(err.message)}</p>
      <p class="muted">Verifique PROMETHEUS_URL, a conectividade com o Prometheus e os logs do back-end.</p></section>`;
  }
  if (seq !== renderSeq) return; // navegação mais recente já assumiu a tela
  app.innerHTML = html;
  applyLayout();
  setTabs(parseHash().path);
  const formEl = app.querySelector("#formula-form");
  if (formEl) updatePreview(formEl);
  if (focused) {
    const el = app.querySelector(`[name="${focused}"]`);
    if (el) el.focus();
  }
}

/* ---------- interações (delegação de eventos) ---------- */
function toggleRow(row) {
  const detail = row.nextElementSibling;
  const open = detail.hidden;
  detail.hidden = !open;
  row.setAttribute("aria-expanded", String(open));
}

async function copyText(button) {
  const source = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(source.textContent);
    button.textContent = "Copiado";
  } catch (err) {
    button.textContent = "Não foi possível copiar";
  }
  setTimeout(() => { button.textContent = "Copiar YAML"; }, 2000);
}

function applyFilters(form) {
  const { path } = parseHash();
  const ns = dec(path.split("/")[2] || "");
  const fd = new FormData(form);
  const query = toQuery({
    q: fd.get("q").trim(), resource: fd.get("resource"), status: fd.get("status"), sort: fd.get("sort"),
  });
  location.hash = `#/ns/${enc(ns)}${query ? `?${query}` : ""}`;
}

let searchTimer = null;
app.addEventListener("click", (ev) => {
  const copy = ev.target.closest("[data-copy]");
  if (copy) return copyText(copy);
  const row = ev.target.closest("tr[data-row]");
  if (row && !ev.target.closest("a")) toggleRow(row);
});
app.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("tr[data-row]")) {
    ev.preventDefault();
    toggleRow(ev.target);
  }
});
app.addEventListener("change", (ev) => {
  const form = ev.target.closest("#filters");
  if (form) applyFilters(form);
});
app.addEventListener("input", (ev) => {
  const form = ev.target.closest("#filters");
  if (form && ev.target.name === "q") {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applyFilters(form), 350);
  }
  const formula = ev.target.closest("#formula-form");
  if (formula) updatePreview(formula);
});
app.addEventListener("reset", (ev) => {
  if (ev.target.id === "formula-form") {
    setTimeout(() => { clearFormErrors(ev.target); updatePreview(ev.target); $("#form-status", ev.target).textContent = ""; }, 0);
  }
});
app.addEventListener("submit", (ev) => {
  if (ev.target.id === "filters") {
    ev.preventDefault();
    applyFilters(ev.target);
  }
  if (ev.target.id === "formula-form") {
    ev.preventDefault();
    saveFormula(ev.target);
  }
});

window.addEventListener("hashchange", render);
render();
