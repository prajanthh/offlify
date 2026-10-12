/* Offlify — frontend controller (vanilla JS, no build step).
 * Downloads audio from YouTube via yt-dlp on the server.
 * All user/remote text is inserted with textContent, never innerHTML.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1500;
  const ACTIVE = new Set(["queued", "processing"]);

  const state = {
    health: null,
    playlist: null,      // the loaded playlist, or a single video shaped like one
    tracks: [],          // server track + { selected, job, _row }
    job: null,
    pollTimer: null,
    pollFailures: 0,
  };

  // ------------------------------------------------------------------ utils
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key in node && typeof value !== "string") node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new Error("Can't reach the server. Is it still running?");
    }
    let data = null;
    try { data = await response.json(); } catch { /* non-JSON body */ }
    if (!response.ok) {
      throw new Error((data && data.error) || `Request failed (HTTP ${response.status}).`);
    }
    return data;
  }

  const postJSON = (path, body) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function toast(message, kind = "info", ms = 5000) {
    const node = el("div", { class: `toast ${kind === "error" ? "error" : ""}`, role: kind === "error" ? "alert" : "status", text: message });
    $("toasts").append(node);
    setTimeout(() => node.remove(), ms);
  }

  const pad = (n, width) => String(n).padStart(width, "0");
  const jobRunning = () => Boolean(state.job && !state.job.finished);

  // ------------------------------------------------------------ server info
  async function loadHealth() {
    const chips = $("statusChips");
    try {
      state.health = await api("/api/health");
    } catch (err) {
      chips.replaceChildren(el("span", { class: "chip bad", text: "Server offline" }));
      showNotice(err.message);
      return;
    }
    const h = state.health;
    chips.replaceChildren(
      el("span", { class: `chip ${h.youtube_api_configured ? "ok" : "bad"}`, text: "YouTube API" }),
      el("span", { class: `chip ${h.ffmpeg_available ? "ok" : "bad"}`, text: "FFmpeg" }),
      el("span", { class: `chip ${h.ytdlp_available ? "ok" : "bad"}`, text: "yt-dlp" }),
    );
    const problems = [];
    if (!h.youtube_api_configured) problems.push("No YouTube Data API key is configured on the server, so links can't be fetched yet. Set YOUTUBE_API_KEY in .env and restart (see README).");
    if (!h.ffmpeg_available) problems.push("FFmpeg was not found on the server, so audio can't be processed. Install FFmpeg and restart (see README).");
    if (!h.ytdlp_available) problems.push("yt-dlp is not available on the server. Install it with: pip install yt-dlp");
    if (problems.length) showNotice(problems.join(" "));

    const select = $("formatSelect");
    select.replaceChildren(...h.formats.map((f) => el("option", { value: f.value, text: f.label })));
    if (!h.formats.length) select.append(el("option", { value: "", text: "No formats available" }));
  }

  function showNotice(text) {
    const notice = $("serverNotice");
    notice.textContent = text;
    notice.hidden = false;
  }

  // -------------------------------------------------------- fetch playlist
  async function onFetchPlaylist(event) {
    event.preventDefault();
    const input = $("playlistUrl");
    const url = input.value.trim();
    const errorEl = $("urlError");
    errorEl.hidden = true;
    input.removeAttribute("aria-invalid");

    if (!url) return showUrlError("Please paste a YouTube playlist or video link.");
    if (jobRunning() && !confirm("A download is still running. Load a different link anyway?")) return;

    setFetching(true);
    try {
      const playlist = await postJSON("/api/resolve", { url });
      loadPlaylist(playlist);
    } catch (err) {
      showUrlError(err.message);
      $("loadingState").hidden = true;
      $("emptyState").hidden = Boolean(state.playlist);
      if (state.playlist) $("playlistSection").hidden = false;
    } finally {
      setFetching(false);
    }
  }

  function showUrlError(message) {
    const errorEl = $("urlError");
    errorEl.textContent = message;
    errorEl.hidden = false;
    $("playlistUrl").setAttribute("aria-invalid", "true");
  }

  function setFetching(busy) {
    const btn = $("fetchBtn");
    btn.disabled = busy;
    btn.classList.toggle("loading", busy);
    btn.querySelector(".btn-label").textContent = busy ? "Fetching" : "Fetch";
    if (busy) {
      $("emptyState").hidden = true;
      $("playlistSection").hidden = true;
      $("loadingState").hidden = false;
    }
  }

  function loadPlaylist(playlist) {
    stopPolling();
    state.playlist = playlist;
    state.job = null;
    state.tracks = playlist.tracks.map((t) => ({ ...t, selected: t.available, job: null }));

    $("loadingState").hidden = true;
    $("emptyState").hidden = true;
    $("resultsSection").hidden = true;
    $("playlistSection").hidden = false;
    $("processBar").hidden = false;

    const single = playlist.kind === "video";
    $("playlistKind").textContent = single ? "Video" : "Playlist";
    $("playlistTitle").textContent = playlist.title;
    const meta = (single
      ? [playlist.channel, state.tracks[0] && state.tracks[0].duration_text]
      : [
        playlist.channel,
        `${playlist.available_count} available video${playlist.available_count === 1 ? "" : "s"}`,
        playlist.unavailable_count ? `${playlist.unavailable_count} unavailable` : null,
      ]).filter(Boolean);
    $("playlistMeta").textContent = meta.join(" · ");
    const thumb = $("playlistThumb");
    if (playlist.thumbnail) { thumb.src = playlist.thumbnail; thumb.hidden = false; } else { thumb.hidden = true; thumb.removeAttribute("src"); }

    const truncated = $("truncatedNotice");
    truncated.hidden = !playlist.truncated;
    truncated.textContent = playlist.truncated
      ? `This playlist is larger than the server limit; showing the first ${playlist.max_items} items.`
      : "";

    // A single video needs no selection tools: one button downloads it.
    $("toolbar").hidden = single;
    $("processAllBtn").hidden = single;
    $("processSelectedBtn").textContent = single ? "Download" : "Download selected";

    renderTracks();
    updateOverall();
    if (!state.tracks.length) toast("This playlist is empty.");
  }

  // ---------------------------------------------------------------- tracks
  function renderTracks() {
    const width = String(state.tracks.length).length < 2 ? 2 : String(state.tracks.length).length;
    const rows = state.tracks.map((track) => buildRow(track, width));
    $("trackList").replaceChildren(...rows);
    state.tracks.forEach(updateRow);
    updateSelectionSummary();
  }

  function buildRow(track, width) {
    const checkbox = el("input", {
      type: "checkbox",
      "aria-label": `Select track ${track.position}: ${track.title}`,
      onchange: (e) => { track.selected = e.target.checked; updateRow(track); updateSelectionSummary(); },
    });
    const placeholder = () => el("div", { class: "track-thumb placeholder", "aria-hidden": "true", text: "♪" });
    const thumb = track.thumbnail
      ? el("img", { class: "track-thumb", src: track.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })
      : placeholder();
    if (track.thumbnail) thumb.addEventListener("error", () => thumb.replaceWith(placeholder()), { once: true });

    const metaParts = [];
    if (track.channel) metaParts.push(el("span", { text: track.channel }));
    if (track.duration_text) metaParts.push(el("span", { class: "mono", text: track.duration_text }));
    if (!track.available) metaParts.push(el("span", { class: "badge", text: track.unavailable_reason || "Unavailable" }));
    const meta = el("p", { class: "track-meta" });
    metaParts.forEach((part, i) => { if (i) meta.append(el("span", { "aria-hidden": "true", text: "·" })); meta.append(part); });

    const pill = el("span", { class: "pill" });
    const fill = el("span");
    const bar = el("div", { class: "mini-bar", hidden: true }, fill);
    const error = el("p", { class: "track-error", hidden: true });

    const root = el("li", { class: `track${track.available ? "" : " unavailable"}` },
      el("label", { class: "track-check" }, checkbox),
      el("span", { class: "track-num", text: pad(track.position, width) }),
      thumb,
      el("div", { class: "track-main" }, el("p", { class: "track-title", title: track.title, text: track.title }), meta),
      el("div", { class: "track-status" }, pill, bar, error),
    );
    track._row = { root, checkbox, pill, bar, fill, error };
    return root;
  }

  function trackStatus(track) {
    if (track.job) return track.job.status;
    return track.available ? "ready" : "unavailable";
  }

  const STATUS_TEXT = {
    "unavailable": "Unavailable", ready: "Ready", queued: "Queued",
    processing: "Downloading", completed: "Completed", failed: "Failed", cancelled: "Cancelled",
  };

  function updateRow(track) {
    const r = track._row;
    if (!r) return;
    r.checkbox.checked = track.selected;
    r.root.classList.toggle("selected", track.selected);

    const status = trackStatus(track);
    r.pill.dataset.status = status;
    r.pill.textContent = status === "processing" ? `Downloading ${Math.round(track.job.progress)}%` : STATUS_TEXT[status];
    const showBar = status === "processing" || status === "queued";
    r.bar.hidden = !showBar;
    r.fill.style.width = `${showBar ? track.job.progress : 0}%`;
    const error = track.job && track.job.error && track.job.status !== "completed" ? track.job.error : "";
    r.error.textContent = error;
    r.error.hidden = !error;
  }

  function updateSelectionSummary() {
    const selected = state.tracks.filter((t) => t.selected && t.available);
    const total = state.tracks.filter((t) => t.available).length;
    $("selectionSummary").textContent = `${selected.length} of ${total} selected`;
    updateControls();
  }

  function setAllSelected(value) {
    state.tracks.forEach((t) => { if (t.available) t.selected = value; updateRow(t); });
    updateSelectionSummary();
  }

  // ------------------------------------------------------------ processing
  async function startProcessing(tracks) {
    const format = $("formatSelect").value;
    if (!format) return toast("No output format is available on the server.", "error");
    const items = tracks.map((t) => ({
      track_id: t.id,
      position: t.position,
      title: t.title,
      video_id: t.video_id,
    }));
    setControlsBusy(true);
    try {
      const job = await postJSON("/api/process-youtube", { format, items, playlist_title: state.playlist.title });
      state.tracks.forEach((t) => { t.job = null; });
      applyJob(job);
      startPolling();
    } catch (err) {
      toast(err.message, "error", 8000);
    } finally {
      setControlsBusy(false);
    }
  }

  function onProcessSelected() {
    const chosen = state.tracks.filter((t) => t.selected && t.available);
    if (!chosen.length) return toast("Select at least one available track first.");
    startProcessing(chosen);
  }

  function onProcessAll() {
    const ready = state.tracks.filter((t) => t.available);
    if (!ready.length) return toast("No available tracks to download.", "error");
    startProcessing(ready);
  }

  async function onCancel() {
    if (!state.job) return;
    $("cancelBtn").disabled = true;
    try {
      applyJob(await postJSON(`/api/jobs/${state.job.id}/cancel`, {}));
      toast("Cancelling… tracks already finished are kept.");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      $("cancelBtn").disabled = false;
    }
  }

  async function onRetry() {
    if (!state.job) return;
    const retryable = state.job.items.filter((i) => i.status === "failed" || i.status === "cancelled");
    setControlsBusy(true);
    try {
      applyJob(await postJSON(`/api/jobs/${state.job.id}/retry`, { track_ids: retryable.map((i) => i.track_id) }));
      startPolling();
    } catch (err) {
      toast(err.message, "error", 8000);
    } finally {
      setControlsBusy(false);
    }
  }

  function startPolling() {
    stopPolling();
    state.pollFailures = 0;
    const tick = async () => {
      if (!state.job) return;
      try {
        const job = await api(`/api/jobs/${state.job.id}`);
        state.pollFailures = 0;
        applyJob(job);
        if (job.finished) { stopPolling(); announceFinished(job); return; }
      } catch (err) {
        state.pollFailures += 1;
        if (state.pollFailures === 3) toast(`Lost contact with the server: ${err.message}`, "error");
        if (state.pollFailures > 30) { stopPolling(); return; }
      }
      state.pollTimer = setTimeout(tick, POLL_MS * Math.min(1 + state.pollFailures, 5));
    };
    state.pollTimer = setTimeout(tick, POLL_MS / 2);
  }

  function stopPolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function announceFinished(job) {
    const c = job.counts;
    const parts = [`${c.completed} of ${c.total} completed`];
    if (c.failed) parts.push(`${c.failed} failed`);
    if (c.cancelled) parts.push(`${c.cancelled} cancelled`);
    toast(parts.join(", ") + ".", c.completed ? "info" : "error");
  }

  function applyJob(job) {
    state.job = job;
    const byId = new Map(job.items.map((i) => [i.track_id, i]));
    for (const track of state.tracks) {
      const item = byId.get(track.id);
      if (item) { track.job = item; updateRow(track); }
    }
    updateOverall();
    renderResults();
    updateControls();
  }

  function updateOverall() {
    const job = state.job;
    const label = $("overallLabel");
    const pct = job ? job.progress : 0;
    $("overallFill").style.width = `${pct}%`;
    $("overallBar").setAttribute("aria-valuenow", String(Math.round(pct)));
    $("overallPct").textContent = job ? `${Math.round(pct)}%` : "";
    if (!job) { label.textContent = "Ready"; return; }
    const c = job.counts;
    const names = { running: "Downloading", cancelling: "Cancelling", completed: "Done", partial: "Finished with issues", failed: "Failed", cancelled: "Cancelled" };
    label.textContent = `${names[job.state] || job.state} · ${c.completed}/${c.total} done${c.failed ? ` · ${c.failed} failed` : ""}`;
  }

  function setControlsBusy(busy) {
    $("processSelectedBtn").disabled = busy;
    $("processAllBtn").disabled = busy;
    if (!busy) updateControls();
  }

  function updateControls() {
    const running = jobRunning();
    const canProcess = Boolean(state.health && state.health.ffmpeg_available && state.health.formats.length && state.health.ytdlp_available);
    $("processSelectedBtn").disabled = running || !canProcess;
    $("processAllBtn").disabled = running || !canProcess;
    $("formatSelect").disabled = running;
    $("cancelBtn").hidden = !running;
    $("retryBtn").disabled = running;
  }

  // --------------------------------------------------------------- results
  function renderResults() {
    const job = state.job;
    const section = $("resultsSection");
    if (!job) { section.hidden = true; return; }
    section.hidden = false;

    const completed = job.items.filter((i) => i.status === "completed" && i.file_id);
    const failed = job.items.filter((i) => i.status === "failed" || i.status === "cancelled");

    $("completedBlock").hidden = !completed.length;
    $("completedList").replaceChildren(...completed.map((i) =>
      el("li", {},
        el("div", {}, el("div", { class: "file-name", text: i.filename }), el("div", { class: "small muted mono", text: i.size_text })),
        el("a", { href: `/api/download/${i.file_id}`, download: i.filename, text: "Download" }),
      )));

    $("failedBlock").hidden = !failed.length;
    $("failedList").replaceChildren(...failed.map((i) =>
      el("li", {},
        el("div", {}, el("div", { class: "file-name", text: `${i.position}. ${i.title}` }), el("div", { class: "fail-reason", text: i.error || i.status })),
      )));

    const zip = $("zipBtn");
    zip.hidden = !completed.length;
    zip.href = `/api/download-zip/${job.id}`;
    zip.textContent = completed.length > 1 ? `Download ZIP (${completed.length})` : "Download ZIP";

    if (!completed.length && !failed.length) {
      $("completedBlock").hidden = false;
      $("completedList").replaceChildren(el("li", {}, el("span", { class: "muted", text: "Files will appear here as each track finishes." })));
    }
  }

  // ------------------------------------------------------------- 3D tilt
  // The intro card leans a few degrees towards the pointer.
  function enableTilt(card, maxDeg = 2.5) {
    if (!matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    card.addEventListener("pointermove", (e) => {
      const box = card.getBoundingClientRect();
      const x = (e.clientX - box.left) / box.width - 0.5;
      const y = (e.clientY - box.top) / box.height - 0.5;
      card.style.setProperty("--ry", `${(x * 2 * maxDeg).toFixed(2)}deg`);
      card.style.setProperty("--rx", `${(-y * 2 * maxDeg).toFixed(2)}deg`);
    });
    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--rx", "0deg");
      card.style.setProperty("--ry", "0deg");
    });
  }

  // ------------------------------------------------------------------ init
  document.addEventListener("DOMContentLoaded", () => {
    $("playlistForm").addEventListener("submit", onFetchPlaylist);
    $("playlistThumb").addEventListener("error", (e) => { e.target.hidden = true; });
    $("selectAllBtn").addEventListener("click", () => setAllSelected(true));
    $("deselectAllBtn").addEventListener("click", () => setAllSelected(false));
    $("processSelectedBtn").addEventListener("click", onProcessSelected);
    $("processAllBtn").addEventListener("click", onProcessAll);
    $("cancelBtn").addEventListener("click", onCancel);
    $("retryBtn").addEventListener("click", onRetry);
    window.addEventListener("beforeunload", (e) => { if (jobRunning()) { e.preventDefault(); e.returnValue = ""; } });
    enableTilt($("introCard"));
    loadHealth();
  });

  // Exposed for debugging in the browser console only.
  window.Offlify = { state };
})();
