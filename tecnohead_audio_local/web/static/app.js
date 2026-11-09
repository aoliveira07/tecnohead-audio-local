(() => {
  "use strict";

  const state = { config: null, snapshot: null, library: null };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const apiUrl = (path) => new URL(`api/${path.replace(/^\//, "")}`, document.baseURI).toString();

  async function api(path, options = {}) {
    const response = await fetch(apiUrl(path), { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) throw new Error(payload?.detail || `Falha HTTP ${response.status}`);
    return payload;
  }

  function toast(message, error = false) {
    const element = $("#toast");
    element.textContent = message;
    element.className = `toast show${error ? " error" : ""}`;
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => { element.className = "toast"; }, 4200);
  }

  function statusPill(value) {
    const text = String(value || "offline");
    const cls = ["ready", "finished", "ok", "online"].includes(text) ? "ok" : ["failed", "error", "offline"].includes(text) ? "error" : "warning";
    return `<span class="status-pill ${cls}">${esc(text)}</span>`;
  }

  function trackLabel(track) {
    if (!track) return `<div class="empty-state">Nenhuma faixa em reprodução.</div>`;
    return `<div class="cover">♫</div><div><div class="now-playing-title">${esc(track.title)}</div><div class="now-playing-artist">${esc(track.artist || "Artista não informado")} · ${esc(track.album || "Álbum não informado")}</div></div>`;
  }

  function renderOverview() {
    const snapshot = state.snapshot || {};
    const library = snapshot.library || {};
    const playback = snapshot.playback || {};
    const sync = snapshot.sync || {};
    $("#app-version").textContent = `v${snapshot.app?.version || "—"}`;
    $("#app-state").outerHTML = `<span id="app-state" class="status-pill ${playback.last_error ? "error" : playback.window_open ? "ok" : "neutral"}">${playback.last_error ? "atenção" : playback.window_open ? "janela aberta" : "fora da janela"}</span>`;
    $("#window-label").textContent = playback.window_open ? "Janela operacional aberta" : "Janela operacional fechada";
    $("#overview-cards").innerHTML = [
      ["Faixas prontas", library.ready || 0, "disponíveis offline"],
      ["Biblioteca", library.total || 0, `${library.ready || 0} prontas`],
      ["Volume", `${state.config?.playback?.volume_percent ?? 90}%`, "alvo do grupo"],
      ["Supervisão", playback.supervised ? "Ativa" : "Pausada", playback.last_success_at ? `último sucesso ${formatDate(playback.last_success_at)}` : "sem confirmação"],
    ].map(([label, value, note]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join("");
    $("#current-track").innerHTML = trackLabel(snapshot.current_track);
    const devices = Object.values(snapshot.devices || {});
    $("#device-cards").innerHTML = devices.length ? devices.map((device) => `<div class="device-card ${device.online ? "online" : ""}"><div class="device-header"><strong>${esc(device.name || device.device_id)}</strong><span class="device-dot"></span></div><div class="device-meta">${esc(device.host || "—")} · ${device.online ? "online" : esc(device.error || "offline")}</div><div class="device-meta">${device.online ? `${esc(device.status || "—")} · ${esc(device.volume_percent ?? "—")}%` : "Sem resposta confirmada"}</div></div>`).join("") : `<div class="empty-state">A supervisão ainda não fez uma leitura dos AudioCast.</div>`;
    $("#sync-card").innerHTML = `<div class="status-line"><strong>${esc(sync.status || "idle")}</strong>${statusPill(sync.status)}</div><div class="status-line"><span>Progresso</span><span>${esc(sync.progress_current || 0)} / ${esc(sync.progress_total || 0)}</span></div><div class="status-line"><span>Normalizadas</span><span>${esc(sync.counts?.normalized || 0)}</span></div><div class="status-line"><span>Falhas</span><span>${esc(sync.counts?.failed || 0)}</span></div><div class="muted">${sync.finished_at ? `Finalizada em ${esc(formatDate(sync.finished_at))}` : sync.error ? esc(sync.error) : "Nenhuma execução concluída."}</div>`;
    $("#alerts").innerHTML = (snapshot.alerts || []).map((alert) => `<div class="alert">${esc(alert)}</div>`).join("");
    $("#playback-track").innerHTML = trackLabel(snapshot.current_track);
    $("#confirmed-command").textContent = playback.last_command_confirmed_at ? `confirmado ${formatDate(playback.last_command_confirmed_at)}` : "sem comando confirmado";
  }

  function renderLibrary() {
    const summary = state.library?.summary || {};
    $("#library-metrics").innerHTML = [["Total", summary.total || 0], ["Prontas", summary.ready || 0], ["Processando", (summary.downloading || 0) + (summary.normalizing || 0)], ["Falhas", summary.failed || 0], ["Tamanho", formatBytes(summary.ready_bytes || 0)]].map(([label, value]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div></div>`).join("");
    const items = state.library?.items || [];
    $("#library-table").innerHTML = items.length ? items.map((track) => {
      const badge = track.status === "ready" ? "ready" : track.status === "failed" ? "failed" : track.status === "removed" ? "removed" : "processing";
      const action = track.status === "failed" ? `<button class="button button-ghost small" data-retry="${esc(track.entry_id)}">Tentar novamente</button>` : "—";
      return `<tr><td><div class="track-title">${esc(track.title)}</div><div class="track-subtitle">${esc(track.artist || "Artista não informado")}</div></td><td>${esc((track.position ?? 0) + 1)}</td><td><span class="badge ${badge}">${esc(track.status)}</span></td><td>${esc(formatBytes(track.file_size || 0))}</td><td>${action}</td></tr>`;
    }).join("") : `<tr><td colspan="5" class="empty-state">Nenhuma faixa encontrada. Configure a playlist e execute uma sincronização.</td></tr>`;
  }

  function renderSyncTimes(times) {
    const values = (times && times.length ? times : ["11:00", "15:00"]).slice(0, 8);
    $("#sync-times-list").innerHTML = values.map((time, index) => `<div class="schedule-row"><label>Horário ${index + 1}<input class="input sync-time" data-sync-time type="time" value="${esc(time)}" required /></label><button type="button" class="button button-ghost small" data-remove-sync="${index}" ${values.length <= 1 ? "disabled" : ""}>Remover</button></div>`).join("");
    $("#sync-times-hint").textContent = `${values.length} horário${values.length === 1 ? "" : "s"} configurado${values.length === 1 ? "" : "s"}. O fuso é ${esc($("#timezone").value || "America/Sao_Paulo")}.`;
  }

  function fillSettings() {
    const config = state.config || {};
    $("#playlist-url").value = config.playlist_url || "";
    $("#timezone").value = config.timezone || "America/Sao_Paulo";
    renderSyncTimes(config.sync?.times || ["11:00", "15:00"]);
    $("#sync-enabled").checked = config.sync?.enabled !== false;
    $("#target-lufs").value = config.normalization?.target_lufs ?? -18;
    $("#true-peak").value = config.normalization?.true_peak_db ?? -2;
    $("#mqtt-enabled").checked = config.mqtt?.enabled !== false;
    $("#mqtt-host").value = config.mqtt?.host || "core-mosquitto";
    $("#mqtt-port").value = config.mqtt?.port || 1883;
    $("#mqtt-username").value = config.mqtt?.username || "";
    $("#mqtt-password").value = "";
    $("#clear-mqtt-password").checked = false;
    $("#mqtt-discovery-prefix").value = config.mqtt?.discovery_prefix || "homeassistant";
    $("#mqtt-topic-prefix").value = config.mqtt?.topic_prefix || "tecnohead/audio_local";
    $("#mqtt-credential-state").textContent = `Senha MQTT: ${config.mqtt?.password_configured ? "configurada" : "não configurada"}. O botão será descoberto automaticamente quando o broker aceitar a conexão.`;
    const mqttState = $("#mqtt-state");
    mqttState.textContent = config.mqtt?.enabled === false ? "Desativado" : (config.mqtt?.password_configured ? "Configurado" : "Padrão do HA");
    mqttState.className = `status-pill ${config.mqtt?.enabled === false ? "neutral" : "ok"}`;
    $("#window-start").value = config.playback?.window_start || "05:30";
    $("#window-end").value = config.playback?.window_end || "21:30";
    $("#playback-volume").value = config.playback?.volume_percent ?? 90;
    $("#playback-enabled").checked = config.playback?.enabled !== false;
    $("#loop-enabled").checked = config.playback?.loop !== false;
    $("#shuffle-enabled").checked = config.playback?.shuffle === true;
    $("#volume-range").value = config.playback?.volume_percent ?? 90;
    $("#volume-output").textContent = `${$("#volume-range").value}%`;
    $("#media-base-url").value = config.media?.base_url || "";
    $("#media-port").value = config.media?.port || 18198;
    $("#device-settings").innerHTML = (config.devices || []).map((device) => `<div class="device-setting"><h3>${esc(device.name)}</h3><div class="form-grid"><label>Host<input class="input device-host" data-device="${esc(device.device_id)}" value="${esc(device.host)}" /></label><label>Porta<input class="input device-port" data-device="${esc(device.device_id)}" type="number" min="1" max="65535" value="${esc(device.port)}" /></label><label>Volume máximo (%)<input class="input device-volume" data-device="${esc(device.device_id)}" type="number" min="0" max="100" value="${esc(device.volume_percent)}" /></label><label class="check-label"><input class="device-enabled" data-device="${esc(device.device_id)}" type="checkbox" ${device.enabled !== false ? "checked" : ""} /> Habilitado</label></div></div>`).join("");
  }

  function collectConfig() {
    const current = state.config || {};
    const devices = (current.devices || []).map((device) => ({ ...device, host: $(`.device-host[data-device="${CSS.escape(device.device_id)}"]`).value.trim(), port: Number($(`.device-port[data-device="${CSS.escape(device.device_id)}"]`).value), volume_percent: Number($(`.device-volume[data-device="${CSS.escape(device.device_id)}"]`).value), enabled: $(`.device-enabled[data-device="${CSS.escape(device.device_id)}"]`).checked }));
    const times = [...document.querySelectorAll("[data-sync-time]")].map((input) => input.value.trim()).filter(Boolean);
    return { timezone: $("#timezone").value.trim(), playlist_url: $("#playlist-url").value.trim(), sync: { ...current.sync, enabled: $("#sync-enabled").checked, times }, playback: { ...current.playback, enabled: $("#playback-enabled").checked, window_start: $("#window-start").value, window_end: $("#window-end").value, volume_percent: Number($("#playback-volume").value), loop: $("#loop-enabled").checked, shuffle: $("#shuffle-enabled").checked }, normalization: { ...current.normalization, enabled: true, target_lufs: Number($("#target-lufs").value), true_peak_db: Number($("#true-peak").value) }, linkplay: current.linkplay, media: { base_url: $("#media-base-url").value.trim(), port: Number($("#media-port").value) }, mqtt: { enabled: $("#mqtt-enabled").checked, host: $("#mqtt-host").value.trim(), port: Number($("#mqtt-port").value), username: $("#mqtt-username").value.trim(), discovery_prefix: $("#mqtt-discovery-prefix").value.trim(), topic_prefix: $("#mqtt-topic-prefix").value.trim() }, devices };
  }

  function collectMqttCredentials() {
    const payload = {};
    const password = $("#mqtt-password").value.trim();
    if (password) payload.mqtt_password = password;
    if ($("#clear-mqtt-password").checked) payload.clear = true;
    return payload;
  }

  async function refresh() {
    try {
      [state.snapshot, state.config, state.library] = await Promise.all([api("state"), api("config"), api(`library?query=${encodeURIComponent($("#library-search")?.value || "")}&status=${encodeURIComponent($("#library-status")?.value || "")}`)]);
      renderOverview(); renderLibrary(); fillSettings();
    } catch (error) { toast(error.message, true); }
  }

  async function runDiagnostics() { try { $("#diagnostics-output").textContent = JSON.stringify(await api("diagnostics"), null, 2); } catch (error) { toast(error.message, true); } }
  async function loadLogs() { try { const result = await api("logs?limit=200"); $("#logs-output").innerHTML = (result.items || []).reverse().map((item) => `<div class="event"><div class="event-head"><span class="event-name">${esc(item.event)}</span><span class="event-time">${esc(formatDate(item.at))}</span></div><div class="event-message">${esc(item.message)}</div></div>`).join("") || `<div class="empty-state">Nenhum evento registrado.</div>`; } catch (error) { toast(error.message, true); } }
  async function saveSettings({ notify = true } = {}) {
    const result = await api("config", { method: "PUT", body: JSON.stringify(collectConfig()) });
    const credentials = collectMqttCredentials();
    if (Object.keys(credentials).length) await api("mqtt/credentials", { method: "PUT", body: JSON.stringify(credentials) });
    state.config = result;
    $("#settings-message").textContent = "Configurações salvas.";
    if (notify) toast("Configurações salvas.");
    await refresh();
    return result;
  }
  async function triggerSync() { try { await saveSettings({ notify: false }); await api("sync", { method: "POST", body: "{}" }); toast("Configurações salvas e sincronização iniciada."); await refresh(); } catch (error) { toast(error.message, true); } }
  async function playbackAction(action) { try { await api("playback/action", { method: "POST", body: JSON.stringify({ action }) }); toast(`Comando ${action} confirmado pela API LinkPlay.`); await refresh(); } catch (error) { toast(error.message, true); } }
  async function testGroup() { try { const result = await api("diagnostics/group", { method: "POST", body: "{}" }); $("#group-state").innerHTML = `<div class="status-line"><strong>Grupo ${result.confirmed ? "confirmado" : "não confirmado"}</strong>${statusPill(result.confirmed ? "ok" : "warning")}</div><div class="muted">Esperados: ${esc((result.expected_hosts || []).join(", "))}</div><div class="muted">Encontrados: ${esc((result.member_hosts || []).join(", ") || "nenhum")}</div>`; } catch (error) { toast(error.message, true); } }
  async function retryTrack(id) { try { await api(`sync/retry/${id}`, { method: "POST", body: "{}" }); toast("Retry da faixa iniciado."); await refresh(); } catch (error) { toast(error.message, true); } }

  function formatBytes(value) { const number = Number(value || 0); if (number < 1024) return `${number} B`; const units = ["KB", "MB", "GB", "TB"]; let size = number / 1024; let unit = units[0]; for (let index = 0; size >= 1024 && index < units.length - 1; index += 1) { size /= 1024; unit = units[index + 1]; } return `${size.toFixed(size >= 10 ? 0 : 1)} ${unit}`; }
  function formatDate(value) { if (!value) return "—"; try { return new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(new Date(value)); } catch (_) { return value; } }

  document.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]"); if (tab) { $$(".tab").forEach((item) => item.classList.toggle("active", item === tab)); $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab.dataset.tab}`)); if (tab.dataset.tab === "diagnostics") runDiagnostics(); if (tab.dataset.tab === "logs") loadLogs(); }
    const action = event.target.closest("[data-action]"); if (action) { if (action.dataset.action === "sync") triggerSync(); if (action.dataset.action === "refresh") refresh(); if (action.dataset.action === "diagnostics") runDiagnostics(); if (action.dataset.action === "logs") loadLogs(); if (action.dataset.action === "group-test") testGroup(); }
    const playback = event.target.closest("[data-playback]"); if (playback) playbackAction(playback.dataset.playback);
    const retry = event.target.closest("[data-retry]"); if (retry) retryTrack(retry.dataset.retry);
    const removeSchedule = event.target.closest("[data-remove-sync]"); if (removeSchedule) { const values = [...document.querySelectorAll("[data-sync-time]")].map((input) => input.value).filter(Boolean); values.splice(Number(removeSchedule.dataset.removeSync), 1); renderSyncTimes(values); }
    const preset = event.target.closest("[data-schedule-preset]"); if (preset) renderSyncTimes(["11:00", "15:00"]);
  });
  $("#refresh").addEventListener("click", refresh);
  $("#library-search").addEventListener("input", () => { window.clearTimeout(refresh.libraryTimer); refresh.libraryTimer = window.setTimeout(refresh, 250); });
  $("#library-status").addEventListener("change", refresh);
  $("#volume-range").addEventListener("input", (event) => { $("#volume-output").textContent = `${event.target.value}%`; });
  $("#volume-range").addEventListener("change", async (event) => { try { await api("playback/volume", { method: "POST", body: JSON.stringify({ volume_percent: Number(event.target.value) }) }); toast("Volume do grupo confirmado."); } catch (error) { toast(error.message, true); } });
  $("#settings-form").addEventListener("submit", async (event) => { event.preventDefault(); try { await saveSettings(); } catch (error) { $("#settings-message").textContent = error.message; toast(error.message, true); } });
  $("#add-sync-time").addEventListener("click", () => { const values = [...document.querySelectorAll("[data-sync-time]")].map((input) => input.value).filter(Boolean); if (values.length >= 8) return toast("A agenda aceita no máximo 8 horários.", true); values.push("18:00"); renderSyncTimes(values); });
  $("#timezone").addEventListener("change", () => { const values = [...document.querySelectorAll("[data-sync-time]")].map((input) => input.value).filter(Boolean); renderSyncTimes(values); });
  refresh();
  window.setInterval(refresh, 20000);
})();

