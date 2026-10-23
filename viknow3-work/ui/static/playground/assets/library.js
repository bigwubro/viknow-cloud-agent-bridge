/* global PlaygroundLibrary */
const PlaygroundLibrary = (() => {
  const STORAGE_KEY = "viknow_playground_user";
  const APP_KEY_STORAGE = "viknow_app_key";
  const KNOWLEDGE = "/api/v1/knowledge";
  const INDEX_JOB_TERMINAL = new Set(["done", "failed", "cancelled", "completed"]);
  const INDEX_JOB_SUCCESS = new Set(["done", "completed"]);
  const INDEX_JOB_POLL_MS = 2000;

  function detailMessage(payload) {
    if (!payload || typeof payload !== "object") return "请求失败";
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      return detail.message || detail.detail || JSON.stringify(detail);
    }
    return payload.message || "请求失败";
  }

  function apiHeaders(extra = {}) {
    const headers = { "Content-Type": "application/json", ...extra };
    const key = (localStorage.getItem(APP_KEY_STORAGE) || window.VIKNOW_APP_KEY || "").trim();
    if (key) {
      headers["X-App-Key"] = key;
    }
    return headers;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: apiHeaders(options.headers || {}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    return payload;
  }

  async function apiMultipart(path, formData) {
    const headers = {};
    const key = (localStorage.getItem(APP_KEY_STORAGE) || window.VIKNOW_APP_KEY || "").trim();
    if (key) {
      headers["X-App-Key"] = key;
    }
    const response = await fetch(path, { method: "POST", headers, body: formData });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    return payload;
  }

  function requireUser() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      window.location.replace("/playground");
    }
  }

  function libraryIdFromPath() {
    const match = window.location.pathname.match(/\/playground\/library\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function normalizeIndexJobStatus(status) {
    return String(status || "").toLowerCase();
  }

  function isIndexJobRunning(status) {
    if (!status) return false;
    return !INDEX_JOB_TERMINAL.has(normalizeIndexJobStatus(status));
  }

  function isIndexJobFailed(status) {
    return normalizeIndexJobStatus(status) === "failed";
  }

  function assetIndexedInCatalog(row) {
    return Boolean(row?.current_index_version_id);
  }

  function assetIndexUiKind(assetId, row, jobs) {
    const entry = jobs.get(assetId);
    const status = entry ? normalizeIndexJobStatus(entry.status) : "";
    if (entry && isIndexJobRunning(entry.status)) return "indexing";
    if (INDEX_JOB_SUCCESS.has(status) || assetIndexedInCatalog(row)) return "indexed";
    if (isIndexJobFailed(entry?.status)) return "failed";
    return "not_indexed";
  }

  function formatOverall(progress) {
    if (!progress || progress.overall == null) return "—";
    const pct = Math.round(Number(progress.overall) * 100);
    return `${pct}%`;
  }

  async function loadLibraries() {
    return api(`${KNOWLEDGE}/libraries?limit=100`);
  }

  async function createLibrary(libraryId, title) {
    return api(`${KNOWLEDGE}/libraries`, {
      method: "POST",
      body: JSON.stringify({ library_id: libraryId, title: title || null }),
    });
  }

  async function loadAssets(libraryId) {
    return api(`${KNOWLEDGE}/libraries/${encodeURIComponent(libraryId)}/assets?limit=100`);
  }

  async function fetchLibraryAssetsIndexStatusBatch(libraryId) {
    return api(
      `${KNOWLEDGE}/libraries/${encodeURIComponent(libraryId)}/assets/index-status-batch?limit=100`,
    );
  }

  async function uploadLibraryFile(libraryId, file) {
    const form = new FormData();
    form.append("file", file, file.name);
    return apiMultipart(`${KNOWLEDGE}/libraries/${encodeURIComponent(libraryId)}/uploads/direct`, form);
  }

  async function presignGet(assetPath, downloadName) {
    return api(`${KNOWLEDGE}/downloads`, {
      method: "POST",
      body: JSON.stringify({ asset_path: assetPath, download_name: downloadName || null }),
    });
  }

  async function createAsset(libraryId, body) {
    return api(`${KNOWLEDGE}/libraries/${encodeURIComponent(libraryId)}/assets`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async function createIndexJob(libraryId, row, compileMode) {
    const body = {
      library_id: libraryId,
      asset_id: row.asset_id,
      asset_path: row.asset_path,
    };
    if (compileMode === "fast_ingest" || compileMode === "pre_compile") {
      body.compile_mode = compileMode;
    }
    return api(`${KNOWLEDGE}/index-jobs`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async function fetchIndexJob(jobId) {
    return api(`${KNOWLEDGE}/index-jobs/${encodeURIComponent(jobId)}`);
  }

  async function fetchIndexJobTasks(jobId) {
    return api(`${KNOWLEDGE}/index-jobs/${encodeURIComponent(jobId)}/tasks`);
  }

  function initLibraryListPage() {
    const listEl = document.querySelector("#library-list");
    const emptyEl = document.querySelector("#library-list-empty");
    const loadingEl = document.querySelector("#libraries-loading");
    const errEl = document.querySelector("#library-form-error");
    const btn = document.querySelector("#btn-create-library");

    async function refresh() {
      loadingEl.hidden = false;
      listEl.hidden = true;
      emptyEl.hidden = true;
      try {
        const rows = await loadLibraries();
        loadingEl.hidden = true;
        listEl.innerHTML = "";
        if (!rows.length) {
          emptyEl.hidden = false;
          return;
        }
        rows.forEach((row) => {
          const li = document.createElement("li");
          li.className = "pg-list-item";
          const link = document.createElement("a");
          link.href = `/playground/library/${encodeURIComponent(row.library_id)}`;
          link.innerHTML = `<strong>${row.title || row.library_id}</strong><span class="pg-mono">${row.library_id}</span>`;
          li.appendChild(link);
          listEl.appendChild(li);
        });
        listEl.hidden = false;
      } catch (error) {
        loadingEl.textContent = error.message || "加载失败";
      }
    }

    btn?.addEventListener("click", async () => {
      errEl.hidden = true;
      const libraryId = document.querySelector("#new-library-id")?.value?.trim();
      const title = document.querySelector("#new-library-title")?.value?.trim();
      if (!libraryId) {
        errEl.textContent = "请填写 Library ID";
        errEl.hidden = false;
        return;
      }
      btn.disabled = true;
      try {
        await createLibrary(libraryId, title || null);
        window.location.href = `/playground/library/${encodeURIComponent(libraryId)}`;
      } catch (error) {
        errEl.textContent = error.message || "创建失败";
        errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });

    refresh();
  }

  function initLibraryDetailPage() {
    const libraryId = libraryIdFromPath();
    if (!libraryId) {
      window.location.replace("/playground/workspace");
      return;
    }
    document.querySelector("#library-id-label").textContent = libraryId;

    const titleEl = document.querySelector("#library-title");
    const metaEl = document.querySelector("#library-meta");
    const uploadBtn = document.querySelector("#btn-upload");
    const uploadInput = document.querySelector("#upload-file");
    const uploadStatus = document.querySelector("#upload-status");
    const uploadError = document.querySelector("#upload-error");
    const assetsLoading = document.querySelector("#assets-loading");
    const assetTable = document.querySelector("#asset-table");
    const assetRows = document.querySelector("#asset-rows");
    const assetEmpty = document.querySelector("#asset-list-empty");
    const compileModeSelect = document.querySelector("#index-compile-mode");

    const modal = document.querySelector("#index-progress-modal");
    const modalTitle = document.querySelector("#index-progress-title");
    const modalSummary = document.querySelector("#index-progress-summary");
    const modalError = document.querySelector("#index-progress-error");
    const modalTasks = document.querySelector("#index-progress-tasks");
    const modalClose = document.querySelector("#index-progress-close");

    /** @type {Map<string, { jobId: string, status: string }>} */
    const assetIndexJobs = new Map();
    let progressPollTimer = null;
    let activeProgressJobId = null;
    /** @type {Map<string, number>} */
    const backgroundPollTimers = new Map();

    function mergeIndexStatusFromApi(row, statusPayload) {
      if (!statusPayload || typeof statusPayload !== "object") return;
      const status = String(statusPayload.status || "").toLowerCase();
      if (status === "indexed") {
        row.current_index_version_id = statusPayload.current_index_version_id || row.current_index_version_id || "1";
        return;
      }
      if (status === "indexing" && statusPayload.job_id) {
        assetIndexJobs.set(row.asset_id, { jobId: String(statusPayload.job_id), status: "running" });
        return;
      }
      if (status === "failed" && statusPayload.job_id) {
        assetIndexJobs.set(row.asset_id, { jobId: String(statusPayload.job_id), status: "failed" });
        return;
      }
      if (status === "not_indexed") {
        assetIndexJobs.delete(row.asset_id);
      }
    }

    async function hydrateRowsIndexStatus(rows) {
      try {
        const statuses = await fetchLibraryAssetsIndexStatusBatch(libraryId);
        const byAssetId = new Map((statuses || []).map((item) => [item.asset_id, item]));
        rows.forEach((row) => {
          mergeIndexStatusFromApi(row, byAssetId.get(row.asset_id));
        });
      } catch {
        /* index-status is authoritative; ignore batch errors */
      }
    }

    function stopBackgroundPoll(assetId) {
      const timer = backgroundPollTimers.get(assetId);
      if (timer) {
        clearInterval(timer);
        backgroundPollTimers.delete(assetId);
      }
    }

    function updateIndexStatusCell(assetId, row) {
      const cell = document.querySelector(`tr[data-asset-id="${assetId}"] [data-role="index-status"]`);
      if (!cell) return;
      const kind = assetIndexUiKind(assetId, row, assetIndexJobs);
      cell.innerHTML = "";
      if (kind === "indexed") {
        const span = document.createElement("span");
        span.className = "pg-muted";
        span.textContent = "已索引";
        cell.appendChild(span);
        return;
      }
      if (kind === "indexing") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pg-button pg-button-link";
        btn.dataset.action = "index-status";
        btn.textContent = "索引中";
        btn.addEventListener("click", () => {
          const entry = assetIndexJobs.get(assetId);
          if (!entry?.jobId) return;
          openProgressModal(entry.jobId, row?.title || assetId);
        });
        cell.appendChild(btn);
        return;
      }
      if (kind === "failed") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pg-button pg-button-link pg-error";
        btn.dataset.action = "index-status";
        btn.textContent = "失败";
        btn.addEventListener("click", () => {
          const entry = assetIndexJobs.get(assetId);
          if (!entry?.jobId) return;
          openProgressModal(entry.jobId, row?.title || assetId);
        });
        cell.appendChild(btn);
        return;
      }
      const span = document.createElement("span");
      span.className = "pg-muted";
      span.textContent = "未索引";
      cell.appendChild(span);
    }

    async function pollAssetIndexJob(assetId, row) {
      const entry = assetIndexJobs.get(assetId);
      if (!entry?.jobId) return;
      try {
        const job = await fetchIndexJob(entry.jobId);
        assetIndexJobs.set(assetId, { jobId: entry.jobId, status: job.status });
        document.querySelectorAll(`[data-asset-id="${assetId}"][data-action="index"]`).forEach((btn) => {
          applyIndexButtonState(btn, assetId);
        });
        updateIndexStatusCell(assetId, row);
        if (!isIndexJobRunning(job.status)) {
          stopBackgroundPoll(assetId);
        }
      } catch {
        stopBackgroundPoll(assetId);
      }
    }

    function ensureBackgroundPoll(assetId, row) {
      const entry = assetIndexJobs.get(assetId);
      if (!entry?.jobId || !isIndexJobRunning(entry.status)) return;
      if (backgroundPollTimers.has(assetId)) return;
      const timer = setInterval(() => pollAssetIndexJob(assetId, row), INDEX_JOB_POLL_MS);
      backgroundPollTimers.set(assetId, timer);
      pollAssetIndexJob(assetId, row);
    }

    function applyIndexButtonState(btnIndex, assetId) {
      const entry = assetIndexJobs.get(assetId);
      const running = entry && isIndexJobRunning(entry.status);
      btnIndex.disabled = running;
      btnIndex.textContent = running ? "索引中…" : "建索引";
    }

    function stopProgressPoll() {
      if (progressPollTimer) {
        clearInterval(progressPollTimer);
        progressPollTimer = null;
      }
    }

    function closeProgressModal() {
      stopProgressPoll();
      activeProgressJobId = null;
      if (modal) modal.hidden = true;
    }

    function renderProgressTasks(tasks) {
      if (!modalTasks) return;
      modalTasks.innerHTML = "";
      if (!tasks?.length) {
        const row = document.createElement("tr");
        row.innerHTML = '<td colspan="3" class="pg-muted">暂无任务阶段</td>';
        modalTasks.appendChild(row);
        return;
      }
      tasks.forEach((task) => {
        const tr = document.createElement("tr");
        const name = task.name || task.stage || "—";
        tr.innerHTML = `
          <td>${name}</td>
          <td><span class="pg-mono">${task.status || "—"}</span></td>
          <td class="pg-mono">${task.error_message || "—"}</td>`;
        modalTasks.appendChild(tr);
      });
    }

    async function refreshProgressModal(jobId) {
      if (!modal || modal.hidden || activeProgressJobId !== jobId) return;
      modalError.hidden = true;
      try {
        const [job, tasks] = await Promise.all([fetchIndexJob(jobId), fetchIndexJobTasks(jobId)]);
        const assetId = job.asset_id;
        if (assetId) {
          assetIndexJobs.set(assetId, { jobId, status: job.status });
          const rowEl = document.querySelector(`tr[data-asset-id="${assetId}"]`);
          const title = rowEl?.dataset?.assetTitle || assetId;
          document.querySelectorAll(`[data-asset-id="${assetId}"][data-action="index"]`).forEach((btn) => {
            applyIndexButtonState(btn, assetId);
          });
          if (INDEX_JOB_SUCCESS.has(String(job.status).toLowerCase()) && rowEl) {
            rowEl.dataset.indexed = "1";
          }
          updateIndexStatusCell(assetId, {
            asset_id: assetId,
            title,
            current_index_version_id: rowEl?.dataset?.indexed === "1" ? "1" : null,
          });
          if (!isIndexJobRunning(job.status)) {
            stopBackgroundPoll(assetId);
          }
        }
        const progress = job.progress || {};
        const overall = formatOverall(progress);
        const stage = progress.current_stage ? ` · 当前阶段 ${progress.current_stage}` : "";
        modalSummary.textContent = `状态 ${job.status} · 整体进度 ${overall}${stage}`;
        if (job.error_message) {
          modalError.textContent = job.error_message;
          modalError.hidden = false;
        }
        renderProgressTasks(tasks);
        if (!isIndexJobRunning(job.status)) {
          stopProgressPoll();
        }
      } catch (error) {
        modalError.textContent = error.message || "刷新失败";
        modalError.hidden = false;
        stopProgressPoll();
      }
    }

    function openProgressModal(jobId, label) {
      if (!modal) return;
      activeProgressJobId = jobId;
      modalTitle.textContent = label ? `索引进度 · ${label}` : "索引进度";
      modalSummary.textContent = "加载中…";
      modalError.hidden = true;
      renderProgressTasks([]);
      modal.hidden = false;
      stopProgressPoll();
      refreshProgressModal(jobId);
      progressPollTimer = setInterval(() => refreshProgressModal(jobId), INDEX_JOB_POLL_MS);
    }

    modalClose?.addEventListener("click", closeProgressModal);
    modal?.addEventListener("click", (event) => {
      if (event.target === modal) closeProgressModal();
    });

    async function refreshMeta() {
      try {
        const lib = await api(`${KNOWLEDGE}/libraries/${encodeURIComponent(libraryId)}`);
        titleEl.textContent = lib.title || lib.library_id;
        metaEl.textContent = `创建于 ${lib.created_at}`;
      } catch (error) {
        titleEl.textContent = libraryId;
        metaEl.textContent = error.message || "";
      }
    }

    async function refreshAssets() {
      assetsLoading.hidden = false;
      assetTable.hidden = true;
      assetEmpty.hidden = true;
      try {
        const rows = await loadAssets(libraryId);
        await hydrateRowsIndexStatus(rows);
        assetsLoading.hidden = true;
        assetRows.innerHTML = "";
        if (!rows.length) {
          assetEmpty.hidden = false;
          return;
        }
        rows.forEach((row) => {
          const tr = document.createElement("tr");
          tr.dataset.assetId = row.asset_id;
          tr.dataset.assetTitle = row.title || row.asset_id;
          tr.dataset.indexed = assetIndexedInCatalog(row) ? "1" : "0";
          tr.innerHTML = `
            <td class="pg-mono">${row.asset_id}</td>
            <td>${row.title || "—"}</td>
            <td class="pg-mono pg-path">${row.asset_path}</td>
            <td data-role="index-status" class="pg-index-status"></td>
            <td class="pg-actions">
              <button type="button" class="pg-button pg-button-link" data-action="preview">预览</button>
              <button type="button" class="pg-button pg-button-link" data-action="download">下载</button>
              <button type="button" class="pg-button pg-button-link" data-action="index" data-asset-id="${row.asset_id}">建索引</button>
            </td>`;
          const btnPreview = tr.querySelector('[data-action="preview"]');
          const btnDownload = tr.querySelector('[data-action="download"]');
          const btnIndex = tr.querySelector('[data-action="index"]');

          applyIndexButtonState(btnIndex, row.asset_id);
          updateIndexStatusCell(row.asset_id, row);
          ensureBackgroundPoll(row.asset_id, row);

          btnPreview.addEventListener("click", async () => {
            const signed = await presignGet(row.asset_path, null);
            window.open(signed.url, "_blank", "noopener");
          });
          btnDownload.addEventListener("click", async () => {
            const signed = await presignGet(row.asset_path, row.title || row.asset_id);
            window.location.href = signed.url;
          });
          btnIndex.addEventListener("click", async () => {
            btnIndex.disabled = true;
            try {
              const compileMode = compileModeSelect?.value || "";
              const job = await createIndexJob(libraryId, row, compileMode);
              assetIndexJobs.set(row.asset_id, { jobId: job.job_id, status: job.status });
              applyIndexButtonState(btnIndex, row.asset_id);
              updateIndexStatusCell(row.asset_id, row);
              ensureBackgroundPoll(row.asset_id, row);
              openProgressModal(job.job_id, row.title || row.asset_id);
            } catch (error) {
              btnIndex.disabled = false;
              window.alert(error.message || "建索引失败");
            }
          });
          assetRows.appendChild(tr);
        });
        assetTable.hidden = false;
      } catch (error) {
        assetsLoading.textContent = error.message || "加载失败";
      }
    }

    uploadBtn?.addEventListener("click", async () => {
      uploadError.hidden = true;
      uploadStatus.hidden = true;
      const file = uploadInput?.files?.[0];
      if (!file) {
        uploadError.textContent = "请选择文件";
        uploadError.hidden = false;
        return;
      }
      uploadBtn.disabled = true;
      try {
        uploadStatus.hidden = false;
        uploadStatus.textContent = "上传到对象网关…";
        const stored = await uploadLibraryFile(libraryId, file);
        uploadStatus.textContent = "创建 Asset 记录…";
        await createAsset(libraryId, {
          asset_id: stored.asset_id,
          asset_path: stored.asset_path,
          title: file.name,
        });
        uploadStatus.textContent = "完成";
        uploadInput.value = "";
        await refreshAssets();
      } catch (error) {
        uploadError.textContent = error.message || "上传失败";
        uploadError.hidden = false;
        uploadStatus.hidden = true;
      } finally {
        uploadBtn.disabled = false;
      }
    });

    refreshMeta();
    refreshAssets();
  }

  return {
    requireUser,
    initLibraryListPage,
    initLibraryDetailPage,
  };
})();
