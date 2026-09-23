"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const activeStatuses = new Set(["queued", "running"]);
  const state = { overview: null, runs: [], details: new Map(), selected: new Set(), displayedId: null, historyId: null, activeId: null, page: "overview", posting: false, connected: false, pollTimer: null, polling: false, refreshing: false };
  const paths = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    history: '<path d="M3 11a9 9 0 1 1 2.5 7M3 4v7h7"/><path d="M12 7v5l3 2"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    refresh: '<path d="M20 7a9 9 0 0 0-15-1L2 9m0-6v6h6m-4 8a9 9 0 0 0 15 1l3-3m0 6v-6h-6"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 5v2"/>',
    wallet: '<path d="M19 7V4H5a2 2 0 0 0 0 4h16v12H5a2 2 0 0 1-2-2V6m18 7h-5v4h5"/><path d="M18 15h.01"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    play: '<path d="m8 4 12 8-12 8V4Z"/>',
    layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
    shield: '<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Z"/><path d="m8 12 3 3 5-6"/>',
    flask: '<path d="M9 3h6m-5 0v7L4 20a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1l-6-10V3M7 15h10"/>',
    sliders: '<path d="M4 7h5m4 0h7M4 17h10m4 0h2"/><circle cx="11" cy="7" r="2"/><circle cx="16" cy="17" r="2"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM4 3v4M2 5h4"/>',
    chart: '<path d="M4 3v17h17M8 15l4-5 4 2 5-7"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    file: '<path d="M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 13h8M8 17h5"/>',
    arrow: '<path d="M7 17 17 7M7 7h10v10"/>'
  };
  function icon(name) { return `<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24">${paths[name] || paths.file}</svg></span>`; }
  $$('[data-icon]').forEach(el => { el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[el.dataset.icon] || paths.file}</svg>`; });
  function esc(value) { return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function num(value, decimals = 0) { return finite(value) ? value.toLocaleString("ru-RU", { maximumFractionDigits: decimals }) : "—"; }
  function signed(value) { return finite(value) ? `${value > 0 ? "+" : ""}${num(value)}` : "—"; }
  function percent(value) { return finite(value) ? `${value > 0 ? "+" : ""}${num(value * 100, 1)}%` : "—"; }
  function date(value) { const parsed = new Date(value); return value && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—"; }
  function duration(value) { return finite(value) ? `${num(value, 1)} с` : "—"; }
  function bytes(value) { return finite(value) ? value > 1048576 ? `${num(value / 1048576, 1)} МБ` : `${num(value / 1024, 1)} КБ` : "—"; }
  function modeLabel(mode) { return ({ single: "Один прогон", batch: "Серия прогонов", tests: "Проверка системы" })[mode] || "Запуск"; }
  function statusBadge(run) { const labels = { queued: "В очереди", running: "В работе", completed: "Завершён", failed: "Ошибка", cancelled: "Остановлен", interrupted: "Прерван" }; const kind = run.status === "completed" ? "success" : activeStatuses.has(run.status) ? "running" : run.status === "failed" ? "failed" : "neutral"; return `<span class="small-badge ${kind}">${esc(labels[run.status] || run.status)}</span>`; }
  function loading(message = "Загружаем данные…") { return `<div class="loading-state"><span class="spinner" aria-hidden="true"></span>${esc(message)}</div>`; }
  function empty(title, message, symbol = "chart") { return `<div class="empty-state"><span class="empty-icon">${icon(symbol)}</span><h3>${esc(title)}</h3><p>${esc(message)}</p></div>`; }
  let toastTimer;
  function toast(message, error = false) { const el = $("#toast"); el.textContent = message; el.classList.remove("hidden"); el.classList.toggle("error", error); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add("hidden"), error ? 8000 : 4500); }
  function connection(ok, error = "") { state.connected = ok; $("#connection-label").textContent = ok ? "Локально · подключено" : "Нет соединения"; $("#connection-dot").style.background = ok ? "#819a67" : "#c1876d"; $("#connection-error").classList.toggle("hidden", ok); if (!ok) $("#connection-error-text").textContent = error || "Сервер недоступен. Проверьте, что приложение запущено."; updateControls(); }
  async function api(path, method = "GET", body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, { method, signal: controller.signal, headers: { Accept: "application/json", ...(method === "POST" ? { "Content-Type": "application/json" } : {}) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      let data;
      try { data = await response.json(); } catch (_) { throw new Error(`Сервер вернул некорректный ответ (${response.status}).`); }
      if (!response.ok) throw new Error(data.error || `Ошибка запроса (${response.status}).`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не ответил вовремя. Проверьте соединение и обновите страницу.");
      if (error instanceof TypeError) throw new Error("Нет связи с локальным сервером. Запустите приложение и повторите подключение.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function setRun(run) { if (!run || !run.id) return; const index = state.runs.findIndex(item => item.id === run.id); if (index < 0) state.runs.unshift(run); else state.runs[index] = { ...state.runs[index], ...run }; if (run.result || run.events) state.details.set(run.id, run); }
  function activeRun() { return state.runs.find(run => run.id === state.activeId && activeStatuses.has(run.status)) || state.runs.find(run => activeStatuses.has(run.status)); }
  function updateControls() {
    const active = activeRun();
    const busy = state.posting || Boolean(active);
    $("#launch-button").disabled = busy || !state.connected || !state.overview?.dataset?.ready;
    $("#tests-button").disabled = busy || !state.connected;
    $("#prepare-button").disabled = busy || !state.connected;
    $("#launch-label").textContent = state.posting ? "Создаём запуск…" : active ? "Агент уже работает" : $("input[name=mode]:checked").value === "batch" ? "Запустить серию" : "Запустить агента";
    $("#cancel-button").disabled = !active || state.posting;
    $("#refresh-button").disabled = state.refreshing;
  }
  function renderOverview() {
    const overview = state.overview;
    if (!overview) return;
    $("#metric-audience").textContent = num(overview.dataset?.customers);
    const completed = state.runs.filter(run => run.status === "completed").length;
    $("#metric-runs").textContent = num(completed);
    $("#metric-runs-foot").textContent = `всего запусков: ${num(state.runs.length)} · история сохранена`;
    $("#sidebar-run-count").textContent = state.runs.length;
    renderData(); renderHistory(); renderProcess(); updateControls();
  }
  function renderProcess() {
    const run = activeRun() || state.details.get(state.displayedId);
    const active = run && activeStatuses.has(run.status);
    $("#result-title").textContent = active ? "Текущий запуск" : "Последний результат";
    $("#process-status").textContent = active ? run.mode === "tests" ? "Проверка системы" : "В работе" : run?.status === "completed" ? "Работа завершена" : "Готова к работе";
    $("#process-status").classList.toggle("running", Boolean(active));
    $("#process-bottom").classList.toggle("hidden", Boolean(active));
    $("#live-progress").classList.toggle("hidden", !active);
    let step = -1;
    if (run?.status === "completed" && run.mode !== "tests") step = 4;
    else if (active && run.mode !== "tests") {
      const events = run.events || [];
      const last = events[events.length - 1];
      step = run.status === "queued" ? -1 : run.progress?.pilot_index > 0 ? 1 : 0;
      if (last?.type === "trial_complete" || /план готов|оценщик/i.test(last?.message || "")) step = 3;
    }
    $$(".agent-pipeline li").forEach((li, index) => { li.classList.toggle("done", index < step); li.classList.toggle("active", index === step); $(".step-state", li).textContent = index < step ? "✓" : `0${index + 1}`; });
    if (active) {
      state.activeId = run.id;
      const p = run.progress || {};
      const trial = Math.max(0, p.trial_index || 0), total = p.trial_count || run.config?.runs || 1, pilot = p.pilot_index || 0, maximum = p.max_pilots || run.config?.max_pilots || 20;
      const fraction = Math.max(0, Math.min(.98, (Math.max(0, trial - 1) + (trial ? .1 + .8 * pilot / maximum : 0)) / total));
      $("#progress-message").textContent = p.message || (run.status === "queued" ? "Запуск ожидает своей очереди" : "Агент работает…");
      $("#progress-count").textContent = run.mode === "tests" ? "Тесты" : `Пилоты: ${pilot} / ${maximum}`;
      $("#progress-detail").textContent = run.mode === "batch" ? `Прогон ${trial} из ${total}` : run.mode === "tests" ? "Результаты появятся после проверки" : `Seed ${run.config?.seed ?? "—"}`;
      $("#progress-fill").style.width = `${run.mode === "tests" ? 10 : fraction * 100}%`;
    }
    const test = state.runs.find(item => item.mode === "tests");
    $("#test-status").innerHTML = test ? `${statusBadge(test)} <span>${activeStatuses.has(test.status) ? "Автоматическая проверка выполняется…" : test.summary?.tests_passed === true ? `Пройдено тестов: ${num(test.summary.tests_run)}` : test.summary?.tests_passed === false ? `Ошибок проверок: ${num(test.summary.failures || 0)}; исключений: ${num(test.summary.errors || 0)}` : "Подробности доступны в истории"}</span> <button class="text-button" type="button" data-open-run="${esc(test.id)}">Открыть</button>` : "Результатов проверок пока нет.";
  }
  function safeArtifactUrl(artifact, download = false) { try { const url = new URL(artifact.url, location.origin); if (url.origin !== location.origin || !url.pathname.startsWith("/api/runs/")) return null; if (download) url.searchParams.set("download", "1"); return url.pathname + url.search; } catch (_) { return null; } }
  function artifactsHtml(run) { return (run.artifacts || []).map(artifact => { const html = artifact.name?.endsWith(".html"); const url = safeArtifactUrl(artifact, !html); return url ? `<a class="button ${html ? "button-dark" : "button-outline"}" href="${esc(url)}" ${html ? 'target="_blank" rel="noopener"' : 'download'}>${icon(html ? "arrow" : "download")}${esc(artifact.label || artifact.name)}</a>` : ""; }).join(""); }
  function metric(label, value, foot, sign = false) { return `<div class="result-metric"><span>${esc(label)}</span><strong class="${sign && finite(value) ? value >= 0 ? "positive" : "negative" : ""}">${sign ? signed(value) : esc(value)}</strong><small>${esc(foot)}</small></div>`; }
  function table(headers, rows) { return `<div class="table-wrap"><table><thead><tr>${headers.map(title => `<th scope="col">${esc(title)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`; }
  function eventLog(run) {
    const events = run.events || [];
    if (!events.length) return "";
    const lines = events.slice(-150).map(event => {
      const time = event.at ? date(event.at) : "";
      let message = event.message || event.error;
      if (!message && event.type === "pilot") message = `Пилот ${event.pilot_index}: ${event.target_tariff || "—"} · ${event.channel || "—"} · ${num(event.n_customers)} клиентов · эффект ${percent(event.observed_lift_ratio)}`;
      if (!message && event.type === "trial_complete") message = `Прогон завершён · seed ${event.seed} · чистый эффект ${signed(event.net_arpu_gain ?? event.net)}`;
      return `${time ? time + "  " : ""}${message || event.type || "Событие"}`;
    });
    return `<details class="section-toggle"><summary>Журнал работы · событий: ${num(events.length)}</summary><pre>${esc(lines.join("\n"))}</pre></details>`;
  }
  function runHtml(run) {
    if (!run) return empty("Выберите запуск", "Здесь появится подробный результат.");
    const result = run.result || {}, summary = run.summary || result.summary || {};
    const trials = result.trials || [];
    const selected = trials.find(trial => trial.seed === result.selected_seed) || trials[0];
    const report = selected?.report || {}, score = selected?.score || {};
    const meta = `<div class="result-meta"><strong>${esc(modeLabel(run.mode))}</strong>${statusBadge(run)}<span>${date(run.created_at)}</span>${run.mode !== "tests" ? `<span>Seed ${esc(run.config?.seed ?? "—")}${run.mode === "batch" ? ` · прогонов: ${num(run.config?.runs)}` : ""}</span>` : ""}<span>${esc(duration(summary.total_runtime_seconds))}</span></div>`;
    if (activeStatuses.has(run.status)) return meta + loading(run.progress?.message || "Запуск выполняется. Результат обновится автоматически.") + `<div class="result-actions"><button class="button button-outline" type="button" data-cancel-run="${esc(run.id)}" ${state.posting ? "disabled" : ""}>Остановить запуск</button></div>` + eventLog(run);
    if (run.result_error) return meta + `<div class="run-error" role="alert"><strong>Сохранённый результат недоступен</strong><p>${esc(run.result_error)}</p><p>Исходные файлы сохранены. Можно повторить запуск с теми же настройками.</p></div><div class="result-actions"><button type="button" class="button button-outline" data-repeat-run="${esc(run.id)}">${icon("refresh")}Повторить настройки</button></div>` + eventLog(run);
    let content = meta;
    if (run.mode === "tests") {
      const passed = summary.tests_passed === true;
      content += `<div class="result-metrics">${metric("Результат проверки", summary.tests_passed == null ? "—" : passed ? "Всё в порядке" : "Есть ошибки", "автоматические тесты")}${metric("Выполнено тестов", num(summary.tests_run), "проверок системы")}${metric("Ошибок проверок", num(summary.failures), "непройденных утверждений")}${metric("Исключений", num(summary.errors), "ошибок выполнения")}</div>`;
      content += `<div class="result-note">${passed ? "Проверки завершены успешно. Подробный журнал доступен для скачивания." : "Изучите журнал tests.txt: в нём указаны конкретные ошибки и результаты проверок."}</div>`;
    } else if (selected) {
      const batch = run.mode === "batch";
      content += `<div class="result-metrics">${metric(batch ? "Средний чистый эффект" : "Чистый эффект", batch ? summary.mean_net : score.net_arpu_gain, "у.е. · учебный скоринг", true)}${metric(batch ? "Положительных прогонов" : "Расходы на связь", batch ? `${num(summary.positive_runs)} / ${num(summary.trials)}` : num(score.total_cost), batch ? "из всей серии" : "у.е. · пилоты + кампании")}${metric(batch ? "Минимальный эффект" : "Контактов использовано", batch ? signed(summary.min_net) : num(score.total_contacts), batch ? "у.е. · в серии" : "пилоты + кампании")}${metric(batch ? "Максимальный эффект" : "Итоговых кампаний", batch ? signed(summary.max_net) : num(selected.campaigns?.length), batch ? "у.е. · в серии" : `проведено пилотов: ${num(report.pilots?.length || 0)}`)}</div>`;
      content += '<p class="result-note">Результат рассчитан локальной учебной средой. Прогноз агента и результат оценщика — разные величины; итог скрытого судейского скоринга может отличаться.</p>';
    }
    if (run.error) content += `<div class="run-error"><strong>Запуск завершился с ошибкой</strong><p>${esc(run.error)}</p></div>`;
    else if (!selected && run.mode !== "tests") content += empty(run.status === "cancelled" ? "Запуск остановлен" : run.status === "interrupted" ? "Запуск был прерван" : "Результат не сформирован", "Можно повторить запуск с теми же настройками.");
    content += `<div class="result-actions">${artifactsHtml(run)}${!activeStatuses.has(run.status) ? `<button type="button" class="button button-outline" data-repeat-run="${esc(run.id)}">${icon("refresh")}Повторить настройки</button>` : ""}</div>`;
    if (trials.length > 1) {
      const max = Math.max(...trials.map(trial => Math.abs(trial.score?.net_arpu_gain || 0)), 1);
      content += `<div class="detail-section"><h3>Устойчивость по seed</h3><div class="batch-chart" aria-label="Чистый эффект по прогонам">${trials.map(trial => { const gain = trial.score?.net_arpu_gain; return `<div class="batch-bar-column" title="Seed ${esc(trial.seed)}: ${signed(gain)} у.е."><div class="batch-bar ${gain < 0 ? "negative" : ""}" style="height:${Math.max(3, 76 * Math.abs(gain || 0) / max)}px"></div><span>${esc(trial.seed)}</span></div>`; }).join("")}</div>${table(["Seed", "Чистый эффект, у.е.", "Расходы, у.е.", "Контакты", "Кампании"], trials.map(trial => `<tr><td class="row-title">${esc(trial.seed)}${trial.seed === result.selected_seed ? '<span class="row-subtitle">выбран для выгрузки</span>' : ""}</td><td class="${trial.score?.net_arpu_gain >= 0 ? "positive" : "negative"}">${signed(trial.score?.net_arpu_gain)}</td><td>${num(trial.score?.total_cost)}</td><td>${num(trial.score?.total_contacts)}</td><td>${num(trial.campaigns?.length)}</td></tr>`))}</div>`;
    }
    if (selected) {
      const campaigns = report.campaigns || selected.campaigns || [];
      content += `<div class="detail-section"><h3>План кампаний${trials.length > 1 ? ` · seed ${esc(selected.seed)}` : ""}</h3>${campaigns.length ? table(["Сегмент / кампания", "Тариф", "Канал", "Охват", "Прогноз эффекта, у.е."], campaigns.map(c => `<tr><td><span class="row-title">${esc(c.campaign_name || "Кампания")}</span><span class="row-subtitle">${esc([c.filter_current_tariff, c.filter_arpu_segment, c.filter_data_segment, c.filter_call_segment].filter(Boolean).join(" · ") || "Вся подходящая аудитория")}</span></td><td>${esc(c.target_tariff)}</td><td><span class="channel-tag">${esc(c.channel)}</span></td><td>${num(c.expected_contacts)}</td><td>${signed(c.expected_net_gain)}</td></tr>`)) : empty("Выгодные кампании не найдены", "Агент не выбрал кампании в рамках текущих оценок и ограничений.")}</div>`;
      if (campaigns.some(c => c.reason)) content += `<details class="section-toggle"><summary>Почему выбраны эти кампании</summary><ul>${campaigns.filter(c => c.reason).map(c => `<li><strong>${esc(c.campaign_name)}:</strong> ${esc(c.reason)}</li>`).join("")}</ul></details>`;
      const pilots = report.pilots || [];
      if (pilots.length) content += `<details class="section-toggle"><summary>Результаты пилотов · ${num(pilots.length)}</summary>${table(["№", "Тариф", "Канал", "Клиентов", "Наблюдаемый эффект", "Расходы"], pilots.map((pilot, i) => `<tr><td>${i + 1}</td><td>${esc(pilot.target_tariff)}</td><td>${esc(pilot.channel)}</td><td>${num(pilot.n_customers)}</td><td class="${pilot.observed_lift_ratio >= 0 ? "positive" : "negative"}">${percent(pilot.observed_lift_ratio)}</td><td>${num(pilot.cost)}</td></tr>`))}</details>`;
      if (report.decisions?.length) content += `<details class="section-toggle"><summary>Решения агентов</summary><ul>${report.decisions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>`;
      if (report.warnings?.length) content += `<details class="section-toggle" open><summary>Замечания агента</summary><ul>${report.warnings.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>`;
      if (report.assumptions?.length) content += `<details class="section-toggle"><summary>Допущения модели</summary><ul>${report.assumptions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>`;
      if (result.selection_note || result.submission_note) content += `<p class="result-note">${esc(result.selection_note || "")}${result.selection_note && result.submission_note ? " " : ""}${esc(result.submission_note || "")}</p>`;
      if (result.export_notes && typeof result.export_notes === "object") {
        const notes = Array.isArray(result.export_notes) ? result.export_notes.map(note => ["", note]) : Object.entries(result.export_notes);
        if (notes.length) content += `<details class="section-toggle"><summary>Что находится в выгрузках</summary><ul>${notes.map(([name, note]) => `<li>${name ? `<strong>${esc(name)}:</strong> ` : ""}${esc(note)}</li>`).join("")}</ul></details>`;
      }
    }
    return content + eventLog(run);
  }
  function detailKey(el) { return el?.textContent?.split(" · ")[0] || ""; }
  function preserveFocus(container) {
    const focused = document.activeElement;
    if (!focused || !container.contains(focused)) return () => {};
    const attribute = ["data-compare-run", "data-open-run", "data-repeat-run", "data-cancel-run", "href"].find(name => focused.hasAttribute(name));
    const value = attribute ? focused.getAttribute(attribute) : null;
    const summary = focused.tagName === "SUMMARY" ? detailKey(focused) : null;
    return () => {
      const match = attribute ? [...container.querySelectorAll(`[${attribute}]`)].find(el => el.getAttribute(attribute) === value) : summary ? [...container.querySelectorAll("summary")].find(el => detailKey(el) === summary) : null;
      if (match && !match.disabled) match.focus({ preventScroll: true });
    };
  }
  function renderResult(run, target = "#result-content") {
    const container = $(target), restoreFocus = preserveFocus(container);
    const openDetails = [...container.querySelectorAll("details[open]")].map(el => detailKey(el.querySelector("summary")));
    container.innerHTML = runHtml(run);
    container.querySelectorAll("details").forEach(el => { if (openDetails.includes(detailKey(el.querySelector("summary")))) el.open = true; });
    restoreFocus();
  }
  function renderHistory() {
    const restoreFocus = preserveFocus($("#history-content"));
    const warnings = state.overview?.history_warnings || [];
    $("#history-warnings").classList.toggle("hidden", !warnings.length);
    $("#history-warnings").textContent = warnings.join("\n");
    $("#history-count").textContent = state.runs.length;
    $("#compare-count").textContent = state.selected.size;
    $("#compare-button").disabled = state.selected.size < 2;
    if (!state.runs.length) { $("#history-content").innerHTML = empty("История начинается с первого запуска", "Вернитесь в обзор, выберите настройки и запустите агента.", "history"); return; }
    $("#history-content").innerHTML = table(["", "Запуск", "Статус", "Seed", "Чистый эффект", "Время", ""], state.runs.map(run => {
      const s = run.summary || {}, canSelect = run.status === "completed" && run.mode !== "tests";
      return `<tr class="history-row" data-run-row="${esc(run.id)}"><td><input type="checkbox" data-compare-run="${esc(run.id)}" aria-label="Выбрать запуск ${esc(date(run.created_at))} для сравнения" ${state.selected.has(run.id) ? "checked" : ""} ${canSelect ? "" : "disabled"}></td><td class="row-title">${esc(modeLabel(run.mode))}<span class="row-subtitle">${date(run.created_at)} · ${esc(run.id.slice(0, 6))}</span></td><td>${statusBadge(run)}</td><td>${run.mode === "tests" ? "—" : esc(run.config?.seed ?? "—")}${run.mode === "batch" ? `<span class="row-subtitle">Прогонов: ${num(run.config?.runs)}</span>` : ""}</td><td class="${s.mean_net >= 0 ? "positive" : ""}">${run.mode === "tests" ? s.tests_passed === true ? "Все тесты прошли" : s.tests_passed === false ? "Есть ошибки" : "—" : signed(s.mean_net)}${run.mode === "batch" && finite(s.mean_net) ? '<span class="row-subtitle">среднее по серии</span>' : ""}</td><td>${duration(s.total_runtime_seconds)}</td><td><button class="text-button" type="button" data-open-run="${esc(run.id)}" aria-label="Открыть запуск ${esc(run.id.slice(0, 6))}">Подробнее ↗</button></td></tr>`;
    }));
    restoreFocus();
  }
  function renderData() {
    const data = state.overview?.dataset;
    if (!data) return;
    $("#data-summary").innerHTML = [["Клиентов", num(data.customers), "целевая аудитория"], ["Тарифов", num(data.tariff_count), "в исходном справочнике"], ["Средний ARPU", data.customers ? num(data.baseline_arpu / data.customers) : "—", "у.е. · прогноз из профилей"]].map(([label, value, foot]) => `<article class="metric-card"><div class="metric-top">${label}</div><strong>${value}</strong><span class="metric-foot">${foot}</span></article>`).join("");
    const segments = data.segments?.arpu || [], max = Math.max(...segments.map(item => item.count), 1);
    const labels = { HIGH: "Высокий ARPU", MID: "Средний ARPU", LOW: "Низкий ARPU" };
    $("#segment-chart").innerHTML = segments.length ? segments.map(segment => `<div class="segment-row"><div class="segment-caption"><span>${esc(labels[segment.name] || segment.name)}</span><strong>${num(segment.count)} <span class="muted">· ${num(segment.count / data.customers * 100, 1)}%</span></strong></div><div class="segment-track"><div class="segment-fill" style="width:${Math.max(0, Math.min(100, segment.count / max * 100))}%"></div></div></div>`).join("") + '<p class="segment-legend">По заполненным значениям сегмента в профилях клиентов.</p>' : empty("Данные ещё не готовы", "Подготовьте файлы кейса для первого запуска.", "database");
    $("#data-readiness").textContent = data.ready ? "Все данные готовы" : "Нужна подготовка";
    $("#data-readiness").classList.toggle("success", data.ready);
    $("#data-files").innerHTML = table(["Файл", "Состояние", "Размер"], (data.files || []).map(file => `<tr><td class="file-name">${esc(file.name)}</td><td><span class="small-badge ${file.valid === true ? "success" : "failed"}">${file.valid === true ? "Готов" : file.exists ? "Нужна проверка" : "Отсутствует"}</span>${file.error ? `<span class="row-subtitle">${esc(file.error)}</span>` : ""}</td><td>${file.exists ? bytes(file.size) : "—"}</td></tr>`));
    if (data.error) $("#data-files").insertAdjacentHTML("beforeend", `<div class="run-error">${esc(data.error)}</div>`);
  }
  async function loadDetail(id) { const { run } = await api(`/api/runs/${encodeURIComponent(id)}`); setRun(run); return run; }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true; updateControls();
    try {
      const [overview, list] = await Promise.all([api("/api/overview"), api("/api/runs")]);
      state.overview = overview; state.runs = list.runs || []; connection(true);
      const active = activeRun(), latest = active || state.runs.find(item => item.id === state.displayedId) || overview.latest_run || state.runs[0];
      renderOverview();
      if (latest) { const run = await loadDetail(latest.id); state.displayedId = run.id; renderResult(run); renderProcess(); }
      else $("#result-content").innerHTML = empty("Здесь появится ваш первый результат", "Запустите агента: он проведёт пилоты, выберет кампании и сохранит отчёт для дальнейшего сравнения.");
      const historyId = state.historyId;
      if (historyId && historyId !== latest?.id) { const run = await loadDetail(historyId); if (state.historyId === historyId) renderResult(run, "#history-detail-content"); }
      else if (historyId) renderResult(state.details.get(historyId), "#history-detail-content");
      schedulePoll();
    } catch (error) { connection(false, error.message); schedulePoll(); }
    finally { state.refreshing = false; updateControls(); }
  }
  function schedulePoll() { clearTimeout(state.pollTimer); if (activeRun()) state.pollTimer = setTimeout(poll, 1000); }
  async function poll() {
    if (state.polling || state.refreshing) { schedulePoll(); return; }
    const active = activeRun(); if (!active) return;
    state.polling = true;
    try {
      const run = await loadDetail(active.id); connection(true); state.displayedId = run.id;
      renderProcess(); renderResult(run); renderHistory(); if (state.historyId === run.id) renderResult(run, "#history-detail-content");
      if (!activeStatuses.has(run.status)) {
        toast(run.status === "completed" ? run.mode === "tests" ? "Проверки завершены. Результат сохранён." : "Запуск завершён. Отчёт и файлы готовы." : run.status === "cancelled" ? "Запуск остановлен." : "Запуск завершился с ошибкой. Подробности в отчёте.", run.status === "failed");
        state.activeId = null;
        await refresh();
      }
    } catch (error) { connection(false, error.message); }
    finally { state.polling = false; updateControls(); schedulePoll(); }
  }
  async function createRun(config) {
    if (state.posting || activeRun()) return;
    state.posting = true; updateControls(); $("#run-form-error").classList.add("hidden");
    try {
      const { run } = await api("/api/runs", "POST", config);
      setRun(run); state.activeId = run.id; state.displayedId = run.id; connection(true);
      renderOverview(); renderResult(run); schedulePoll();
      toast(run.mode === "tests" ? "Проверка системы запущена." : "Агент запущен. Результат обновится автоматически.");
      if (run.mode !== "tests") $("#result-title").textContent = "Текущий запуск";
    } catch (error) { $("#run-form-error").textContent = error.message; $("#run-form-error").classList.remove("hidden"); toast(error.message, true); }
    finally { state.posting = false; updateControls(); }
  }
  function setPage() {
    const page = location.hash.slice(1);
    if (page === "main") return;
    state.page = ["overview", "history", "data"].includes(page) ? page : "overview";
    $$(".page").forEach(el => el.classList.toggle("hidden", el.id !== `page-${state.page}`));
    $$("[data-page]").forEach(el => { const selected = el.dataset.page === state.page; el.classList.toggle("active", selected); if (selected) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); });
    $("#breadcrumb-page").textContent = ({ overview: "Обзор", history: "История запусков", data: "Данные и проверки" })[state.page];
  }
  async function showHistoryDetail(id) {
    location.hash = "history"; setPage(); state.historyId = id; $("#history-detail").classList.remove("hidden"); $("#history-detail-content").innerHTML = loading("Загружаем результат…");
    $("#history-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    try { const run = await loadDetail(id); if (state.historyId === id) { renderResult(run, "#history-detail-content"); $("#history-detail-title").textContent = `Результат · ${date(run.created_at)}`; } }
    catch (error) { if (state.historyId === id) $("#history-detail-content").innerHTML = `<div class="run-error">${esc(error.message)}</div>`; }
  }
  function repeatRun(id) {
    const run = state.details.get(id) || state.runs.find(item => item.id === id); if (!run) return;
    if (run.mode === "tests") { location.hash = "data"; toast("Параметры не требуются. Нажмите «Запустить тесты»."); return; }
    $("#seed").value = run.config.seed; $("#max-pilots").value = run.config.max_pilots; $("#time-limit").value = run.config.time_limit_seconds; $("#batch-runs").value = run.mode === "batch" ? run.config.runs : 10;
    const radio = $(`input[name="mode"][value="${run.mode === "batch" ? "batch" : "single"}"]`); radio.checked = true; updateMode(); location.hash = "overview"; setPage(); $("#launch-title").scrollIntoView({ behavior: "smooth", block: "center" }); toast("Настройки перенесены. Можно изменить их и запустить снова.");
  }
  function updateMode() { const batch = $("input[name=mode]:checked").value === "batch"; $("#batch-field").classList.toggle("hidden", !batch); $("#single-note").classList.toggle("hidden", batch); $("#batch-runs").required = batch; $("#batch-runs").disabled = !batch; updateControls(); }
  $("#run-form").addEventListener("submit", event => { event.preventDefault(); if (!event.currentTarget.reportValidity()) return; const mode = $("input[name=mode]:checked").value; createRun({ mode, seed: Number($("#seed").value), max_pilots: Number($("#max-pilots").value), time_limit_seconds: Number($("#time-limit").value), runs: mode === "batch" ? Number($("#batch-runs").value) : 1 }); });
  $$("input[name=mode]").forEach(el => el.addEventListener("change", updateMode));
  $("#refresh-button").addEventListener("click", refresh); $("#reconnect-button").addEventListener("click", refresh);
  $("#tests-button").addEventListener("click", () => createRun({ mode: "tests", seed: 42, max_pilots: 20, time_limit_seconds: 240, runs: 1 }));
  $("#prepare-button").addEventListener("click", async () => {
    if (state.posting || activeRun()) return; state.posting = true; updateControls();
    try { const { dataset } = await api("/api/data/prepare", "POST", {}); state.overview.dataset = dataset; renderOverview(); toast(dataset.ready ? "Данные подготовлены. Можно запускать агента." : "Подготовка завершена, но часть данных недоступна.", !dataset.ready); }
    catch (error) { toast(error.message, true); }
    finally { state.posting = false; updateControls(); }
  });
  async function cancelRun(id) {
    const active = state.runs.find(run => run.id === id && activeStatuses.has(run.status)); if (!active || state.posting) return; state.posting = true; updateControls();
    try { const { run } = await api(`/api/runs/${encodeURIComponent(active.id)}/cancel`, "POST", {}); setRun(run); renderProcess(); renderResult(run); renderHistory(); if (state.historyId === run.id) renderResult(run, "#history-detail-content"); toast("Запрос на остановку отправлен."); if (!activeStatuses.has(run.status)) await refresh(); else schedulePoll(); }
    catch (error) { toast(error.message, true); }
    finally { state.posting = false; updateControls(); }
  }
  $("#cancel-button").addEventListener("click", () => cancelRun(activeRun()?.id));
  document.addEventListener("click", event => {
    const open = event.target.closest("[data-open-run]"); if (open) { showHistoryDetail(open.dataset.openRun); return; }
    const repeat = event.target.closest("[data-repeat-run]"); if (repeat) { repeatRun(repeat.dataset.repeatRun); return; }
    const cancel = event.target.closest("[data-cancel-run]"); if (cancel) { cancelRun(cancel.dataset.cancelRun); return; }
    const row = event.target.closest("[data-run-row]"); if (row && !event.target.closest("input,button,a,label")) showHistoryDetail(row.dataset.runRow);
  });
  $("#history-content").addEventListener("change", event => { const input = event.target.closest("[data-compare-run]"); if (!input) return; if (input.checked && state.selected.size >= 3) { input.checked = false; toast("Для сравнения можно выбрать до трёх запусков."); return; } if (input.checked) state.selected.add(input.dataset.compareRun); else state.selected.delete(input.dataset.compareRun); $("#compare-count").textContent = state.selected.size; $("#compare-button").disabled = state.selected.size < 2; });
  $("#compare-button").addEventListener("click", () => {
    const selected = state.runs.filter(run => state.selected.has(run.id)); if (selected.length < 2) return;
    const metrics = [["Режим", run => modeLabel(run.mode)], ["Seed", run => esc(run.config?.seed)], ["Пилотов на прогон", run => num(run.config?.max_pilots)], ["Лимит времени", run => duration(run.config?.time_limit_seconds)], ["Прогонов", run => num(run.summary?.trials)], ["Средний чистый эффект", run => signed(run.summary?.mean_net) + " у.е."], ["Минимальный эффект", run => signed(run.summary?.min_net) + " у.е."], ["Максимальный эффект", run => signed(run.summary?.max_net) + " у.е."], ["Положительных прогонов", run => `${num(run.summary?.positive_runs)} / ${num(run.summary?.trials)}`], ["Общее время", run => duration(run.summary?.total_runtime_seconds)]];
    $("#comparison-content").innerHTML = `<div class="table-wrap"><table class="comparison-table"><thead><tr><th scope="col">Показатель</th>${selected.map(run => `<th scope="col">${date(run.created_at)}<span class="row-subtitle">${esc(run.id.slice(0, 6))}</span></th>`).join("")}</tr></thead><tbody>${metrics.map(([label, get]) => `<tr><td>${label}</td>${selected.map(run => `<td>${get(run)}</td>`).join("")}</tr>`).join("")}</tbody></table></div><p class="result-note">Средний эффект для одиночного запуска равен его результату. Для серии это среднее по всем её прогонам.</p>`;
    $("#comparison-panel").classList.remove("hidden"); $("#comparison-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#close-comparison").addEventListener("click", () => $("#comparison-panel").classList.add("hidden"));
  $("#close-detail").addEventListener("click", () => { state.historyId = null; $("#history-detail").classList.add("hidden"); });
  window.addEventListener("hashchange", setPage);
  window.addEventListener("online", refresh);
  window.addEventListener("focus", () => { if (!activeRun() && state.overview) refresh(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.overview) refresh(); });
  setPage(); updateMode(); $("#history-content").innerHTML = loading(); refresh();
})();
