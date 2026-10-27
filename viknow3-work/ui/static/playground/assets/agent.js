const PlaygroundAgent = (() => {
  const STORAGE_KEY = "viknow_playground_user";
  const AGENT_API = "/api/v1/agent";
  const KNOWLEDGE_API = "/api/v1/knowledge";

  const AGENTS_NEED_LIBRARY = new Set(["expertbuddy-deepagents", "expertbuddy-adk"]);

  function detailMessage(payload) {
    if (!payload || typeof payload !== "object") return "请求失败";
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    }
    if (detail && typeof detail === "object") {
      return detail.message || detail.detail || JSON.stringify(detail);
    }
    return payload.message || "请求失败";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    return payload;
  }

  function requireUser() {
    if (!localStorage.getItem(STORAGE_KEY)) {
      window.location.replace("/playground");
    }
  }

  function currentUserId() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return "";
      return JSON.parse(raw).id || "";
    } catch {
      return "";
    }
  }

  function agentIdFromPath() {
    const match = window.location.pathname.match(/\/playground\/agent\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function formatBytes(size) {
    if (!Number.isFinite(size) || size < 1024) return `${size || 0} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function mediaLabel(file) {
    if (!file) return "";
    const type = (file.type || "").toLowerCase();
    if (type.startsWith("image/")) return "图片";
    if (type.startsWith("video/")) return "视频";
    if (type.startsWith("audio/")) return "音频";
    if (type.includes("pdf")) return "PDF";
    return "文件";
  }

  function appendMessage(container, role, text, attachment) {
    const div = document.createElement("div");
    div.className = role === "user" ? "pg-turn pg-turn-user" : "pg-turn pg-turn-assistant";
    const label = role === "user" ? "You" : "Agent";
    div.innerHTML = `<div class="pg-turn-label">${label}</div><div class="pg-turn-body"><div class="pg-turn-text"></div></div>`;
    div.querySelector(".pg-turn-text").textContent = text;
    if (attachment && role === "user") {
      const chip = document.createElement("div");
      chip.className = "pg-turn-attach";
      chip.innerHTML = `<span class="pg-attach-chip pg-attach-chip-inline"><span class="pg-attach-file-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg></span><span class="pg-attach-label"><span class="pg-attach-name">${attachment.name}</span><span class="pg-attach-sep">·</span><span class="pg-attach-meta">${attachment.label} · ${attachment.size}</span></span></span>`;
      div.querySelector(".pg-turn-body").appendChild(chip);
    }
    container.appendChild(div);
    const scroll = container.closest(".pg-chat-scroll") || container;
    scroll.scrollTop = scroll.scrollHeight;
    return div;
  }

  const signedUrlCache = new Map();
  const URI_FIELDS = ["file_path", "source_uri", "asset_path", "object_key", "uri", "video_url", "image_url", "document_url", "url"];

  function s3Uri(value) {
    if (typeof value !== "string") {
      return "";
    }
    const text = value.trim().split("#")[0].split("?")[0].replace(/^\/+/, "");
    if (!text.startsWith("knowledge/assets/")) {
      return "";
    }
    if (text.split("/").some((part) => part === "" || part === "." || part === "..")) {
      return "";
    }
    return text;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function fragmentRange(value) {
    if (typeof value !== "string") {
      return [null, null];
    }
    const match = value.match(/#t=([0-9.]+),([0-9.]+)/);
    if (!match) {
      return [null, null];
    }
    return [Number(match[1]), Number(match[2])];
  }

  function clock(seconds) {
    const whole = Math.max(0, Math.floor(seconds));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  }

  function previewLabel(item) {
    const name = item.title || item.object_key.split("/").pop();
    if (item.begin_seconds != null && item.end_seconds != null) {
      return `${name} ${clock(item.begin_seconds)}–${clock(item.end_seconds)}`;
    }
    if (item.page) {
      return `${name} 第 ${item.page} 页`;
    }
    return name;
  }

  function positiveInt(value) {
    return Number.isInteger(value) && value > 0 ? value : null;
  }

  function nearby(node) {
    const list = [node];
    ["source", "metadata", "render"].forEach((name) => {
      const child = node[name];
      if (child && typeof child === "object" && !Array.isArray(child)) {
        list.push(child);
      }
    });
    const source = node.source && typeof node.source === "object" ? node.source : null;
    if (source && source.metadata && typeof source.metadata === "object") {
      list.push(source.metadata);
    }
    [node.anchors, source && source.anchors].forEach((anchors) => {
      if (!Array.isArray(anchors)) {
        return;
      }
      anchors.forEach((anchor) => {
        if (anchor && typeof anchor === "object") {
          list.push(anchor);
        }
      });
    });
    return list;
  }

  function keyFrom(nodes) {
    let found = "";
    let raw = "";
    nodes.forEach((node) => {
      if (found) {
        return;
      }
      URI_FIELDS.forEach((field) => {
        if (found) {
          return;
        }
        const key = s3Uri(node[field]);
        if (key) {
          found = key;
          raw = String(node[field]);
        }
      });
    });
    return [found, raw];
  }

  function rangeFrom(nodes, raw) {
    let begin = null;
    let end = null;
    nodes.forEach((node) => {
      if (begin != null && end != null) {
        return;
      }
      const range = node.time_range && typeof node.time_range === "object" ? node.time_range : {};
      const start = finite(node.begin_seconds) ?? finite(node.begin_ts) ?? finite(node.timestamp_start) ?? finite(node.absolute_start_seconds) ?? finite(range.start_seconds);
      const stop = finite(node.end_seconds) ?? finite(node.end_ts) ?? finite(node.timestamp_end) ?? finite(node.absolute_end_seconds) ?? finite(range.end_seconds);
      if (start != null && stop != null && stop >= start) {
        begin = start;
        end = stop;
      }
    });
    if (begin == null || end == null) {
      return fragmentRange(raw);
    }
    return [begin, end];
  }

  function pageFrom(nodes) {
    let indexed = null;
    for (const node of nodes) {
      const numbered = positiveInt(node.document_page)
        ?? positiveInt(node.document_inspect_page)
        ?? positiveInt(node.visual_ground_page)
        ?? positiveInt(node.page_number);
      if (numbered) {
        return numbered;
      }
      if (node.page_range && positiveInt(node.page_range.start_page)) {
        return node.page_range.start_page;
      }
      if ((node.kind === "document_page" || node.kind === "text_span" || node.kind === "image_region") && Number.isInteger(node.page) && node.page >= 0) {
        indexed = node.page + 1;
      }
    }
    if (indexed) {
      return indexed;
    }
    for (const node of nodes) {
      if (Number.isInteger(node.page) && node.page >= 0) {
        return node.page === 0 ? 1 : node.page;
      }
    }
    return null;
  }

  function previewFromNode(node, marker) {
    const nodes = nearby(node);
    const [key, raw] = keyFrom(nodes);
    if (!key) {
      return null;
    }
    const [begin, end] = rangeFrom(nodes, raw);
    const page = pageFrom(nodes);
    const named = node.title || (node.source && node.source.title) || "";
    const title = String(named || ((begin == null && page == null) ? node.display_label : "") || "");
    return {
      object_key: key,
      begin_seconds: begin != null && end != null && end >= begin ? begin : null,
      end_seconds: begin != null && end != null && end >= begin ? end : null,
      page,
      title,
      marker: marker || "",
      display_label: "",
    };
  }

  function rememberPreview(list, item) {
    item.display_label = previewLabel(item);
    const same = list.find((row) => row.object_key === item.object_key && row.begin_seconds === item.begin_seconds && row.end_seconds === item.end_seconds && row.page === item.page);
    if (same) {
      if (!same.marker && item.marker) {
        same.marker = item.marker;
      }
      if (!same.title && item.title) {
        same.title = item.title;
        same.display_label = previewLabel(same);
      }
      return;
    }
    const bare = item.begin_seconds != null || item.page != null
      ? list.find((row) => row.object_key === item.object_key && row.begin_seconds == null && row.page == null)
      : null;
    if (bare) {
      Object.assign(bare, item);
      bare.display_label = previewLabel(bare);
      return;
    }
    if (item.begin_seconds == null && item.page == null && list.some((row) => row.object_key === item.object_key && (row.begin_seconds != null || row.page != null))) {
      return;
    }
    list.push(item);
  }

  function walkPreviews(value, marker, list) {
    if (!value || typeof value !== "object") {
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item) => walkPreviews(item, marker, list));
      return;
    }
    const ownMarker = typeof value.marker === "string" && /^\d+$/.test(value.marker) ? value.marker : marker;
    const found = previewFromNode(value, ownMarker);
    if (found) {
      rememberPreview(list, found);
    }
    Object.entries(value).forEach(([key, child]) => {
      const next = /^\d+$/.test(key) ? key : ownMarker;
      walkPreviews(child, next, list);
    });
  }

  function previewsIn(value, limit) {
    const list = [];
    walkPreviews(value, "", list);
    return list.slice(0, limit);
  }

  function parsePayload(value) {
    if (value && typeof value === "object") {
      return value;
    }
    if (typeof value !== "string" || !value.trim()) {
      return null;
    }
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }

  function signedUrl(objectKey) {
    const cached = signedUrlCache.get(objectKey);
    if (cached) {
      return cached;
    }
    const pending = api(`${KNOWLEDGE_API}/downloads`, {
      method: "POST",
      body: JSON.stringify({ asset_path: objectKey }),
    }).then((body) => {
      if (!body || typeof body.url !== "string" || !body.url) {
        throw new Error("预签名失败");
      }
      return body.url;
    });
    signedUrlCache.set(objectKey, pending);
    pending.catch(() => signedUrlCache.delete(objectKey));
    return pending;
  }

  function previewBlock(citation) {
    const block = document.createElement("div");
    block.className = "pg-preview";
    if (citation.marker) {
      block.dataset.marker = String(citation.marker);
    }
    const head = document.createElement("button");
    head.type = "button";
    head.className = "pg-preview-open";
    head.textContent = citation.display_label || citation.title || "预览";
    const stage = document.createElement("div");
    stage.className = "pg-preview-stage";
    stage.hidden = true;
    head.addEventListener("click", () => {
      const opening = stage.hidden;
      stage.hidden = !opening;
      if (opening && stage.dataset.loaded !== "1") {
        stage.dataset.loaded = "1";
        mountPreview(stage, citation);
      }
    });
    block.append(head, stage);
    return block;
  }

  function mountPreview(host, citation) {
    const key = citation.object_key || "";
    if (!key) {
      host.textContent = "没有可预览的对象";
      return;
    }
    const lower = key.toLowerCase();
    const video = citation.modality === "video" || /\.(mp4|webm|mov|mkv)$/.test(lower);
    if (video) {
      mountVideo(host, citation, key);
      return;
    }
    if (/\.(png|jpe?g|gif|webp)$/.test(lower)) {
      const image = document.createElement("img");
      image.className = "pg-preview-image";
      image.alt = citation.display_label || "预览";
      host.append(image);
      signedUrl(key).then((url) => {
        image.src = url;
      }).catch(() => {
        host.append(previewError());
      });
      return;
    }
    const open = openOriginalButton(citation, key);
    host.append(open);
    if (lower.endsWith(".pdf")) {
      const frame = document.createElement("iframe");
      frame.className = "pg-preview-frame";
      frame.title = citation.display_label || "文档预览";
      host.insertBefore(frame, open);
      signedUrl(key).then((url) => {
        frame.src = `${url}${playbackFragment(citation)}`;
      }).catch(() => {
        frame.remove();
      });
    }
  }

  function playbackFragment(citation) {
    const lower = String(citation.object_key || "").toLowerCase();
    const start = citation.begin_seconds;
    const end = citation.end_seconds;
    if (/\.(mp4|webm|mov|mkv)$/.test(lower) && typeof start === "number") {
      return typeof end === "number" && end >= start ? `#t=${start},${end}` : `#t=${start}`;
    }
    if (lower.endsWith(".pdf") && citation.page) {
      return `#page=${citation.page}`;
    }
    return "";
  }

  function mountVideo(host, citation, key) {
    const video = document.createElement("video");
    video.className = "pg-preview-video";
    video.controls = true;
    video.preload = "metadata";
    const start = typeof citation.begin_seconds === "number" ? citation.begin_seconds : null;
    const end = typeof citation.end_seconds === "number" ? citation.end_seconds : null;
    video.addEventListener("loadedmetadata", () => {
      if (start != null) {
        video.currentTime = start;
      }
    });
    let stopAt = null;
    video.addEventListener("timeupdate", () => {
      if (stopAt !== null && video.currentTime >= stopAt) {
        video.pause();
        stopAt = null;
      }
    });
    const play = document.createElement("button");
    play.type = "button";
    play.className = "pg-button pg-button-link";
    play.textContent = "播放片段";
    play.addEventListener("click", () => {
      if (start != null) {
        video.currentTime = start;
      }
      stopAt = end != null && start != null && end >= start ? end : null;
      video.play().catch(() => {});
    });
    host.append(video, play);
    signedUrl(key).then((url) => {
      video.src = `${url}${playbackFragment(citation)}`;
    }).catch(() => {
      host.append(previewError());
    });
  }

  function openOriginalButton(citation, key) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pg-button pg-button-link";
    button.textContent = citation.page ? `打开原文 · 第 ${citation.page} 页` : "打开原文";
    button.addEventListener("click", async () => {
      try {
        const url = await signedUrl(key);
        window.open(`${url}${playbackFragment(citation)}`, "_blank", "noopener");
      } catch {
        button.textContent = "打开失败";
      }
    });
    return button;
  }

  function previewError() {
    const note = document.createElement("div");
    note.className = "pg-preview-error";
    note.textContent = "预览地址签名失败";
    return note;
  }

  function markerFrom(node) {
    const raw = String((node && (node.agent_citation || node.marker)) || "");
    const match = raw.match(/(\d+)/);
    return match ? match[1] : "";
  }

  function claimPreview(node) {
    if (!node || typeof node !== "object") {
      return null;
    }
    const preview = previewFromNode(node, markerFrom(node));
    if (!preview) {
      return null;
    }
    const label = String(node.display_label || (node.render && node.render.label) || "");
    preview.display_label = label || previewLabel(preview);
    preview.claim = String(node.claim || "");
    const kind = node.render && node.render.kind;
    const sourceType = node.source && node.source.type;
    if (kind === "video_clip" || sourceType === "video") {
      preview.modality = "video";
    }
    return preview;
  }

  function claimArray(node) {
    return node && typeof node === "object" && Array.isArray(node.claims) ? node.claims : null;
  }

  function claimsIn(value) {
    const payload = parsePayload(value);
    if (!payload || typeof payload !== "object") {
      return [];
    }
    const data = payload.data && typeof payload.data === "object" ? payload.data : null;
    const result = data && data.result && typeof data.result === "object" ? data.result : null;
    const lists = [
      claimArray(payload),
      claimArray(payload.result),
      claimArray(data),
      claimArray(result),
    ].filter(Boolean);
    const claims = lists.find((list) => list.length) || [];
    return claims.filter((item) => item && typeof item === "object");
  }

  const PREVIEW_TOOLS = new Set(["document_inspect", "video_inspect", "video_inspect_2_0"]);

  function isPreviewTool(name) {
    return PREVIEW_TOOLS.has(String(name || ""));
  }

  function renderToolReview(el) {
    const panel = el.querySelector(".pg-tool-review");
    if (!panel) {
      return;
    }
    panel.replaceChildren();
    const claims = Array.isArray(el._claims) ? el._claims : [];
    if (!claims.length) {
      const empty = document.createElement("div");
      empty.className = "pg-citation-empty";
      empty.textContent = "该引用暂无可预览内容。";
      panel.append(empty);
      return;
    }
    claims.forEach((claim) => {
      const entry = document.createElement("div");
      entry.className = "pg-citation-entry";
      const meta = document.createElement("div");
      meta.className = "pg-citation-meta";
      meta.textContent = String(claim.display_label || (claim.render && claim.render.label) || "引用");
      entry.append(meta);
      if (claim.claim) {
        const text = document.createElement("div");
        text.className = "pg-citation-claim";
        text.textContent = String(claim.claim);
        entry.append(text);
      }
      const preview = claimPreview(claim);
      if (preview) {
        mountPreview(entry, preview);
      } else {
        const empty = document.createElement("div");
        empty.className = "pg-citation-empty";
        empty.textContent = "该引用暂无可预览内容。";
        entry.append(empty);
      }
      panel.append(entry);
    });
  }

  function citationButton(marker, citation) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pg-answer-citation";
    button.dataset.marker = String(marker);
    button.textContent = `[${marker}]`;
    const tip = [citation.display_label, citation.claim].filter(Boolean).join("\n");
    button.title = tip || `[${marker}]`;
    button.addEventListener("click", () => {
      openCitationPreview(button, marker, citation);
    });
    return button;
  }

  function openCitationPreview(anchor, marker, citation) {
    const parent = anchor.parentElement;
    const current = parent && parent.querySelector(":scope > .pg-citation-panel");
    if (current && current.dataset.marker === String(marker)) {
      current.remove();
      return;
    }
    if (current) {
      current.remove();
    }
    const panel = document.createElement("div");
    panel.className = "pg-citation-panel";
    panel.dataset.marker = String(marker);
    const head = document.createElement("div");
    head.className = "pg-citation-panel-head";
    const title = document.createElement("div");
    title.className = "pg-citation-panel-title";
    const label = String((citation && citation.display_label) || "");
    title.textContent = label ? `[${marker}] ${label}` : `[${marker}]`;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "pg-citation-panel-close";
    close.textContent = "关闭";
    close.addEventListener("click", () => panel.remove());
    head.append(title, close);
    panel.append(head);
    if (citation && citation.claim) {
      const claim = document.createElement("div");
      claim.className = "pg-citation-claim";
      claim.textContent = String(citation.claim);
      panel.append(claim);
    }
    const preview = claimPreview(citation);
    if (preview) {
      mountPreview(panel, preview);
    } else {
      const empty = document.createElement("div");
      empty.className = "pg-citation-empty";
      empty.textContent = "该引用暂无可预览内容。";
      panel.append(empty);
    }
    anchor.after(panel);
  }

  function decorateAnswer(node, citations) {
    const value = node.textContent || "";
    const pattern = /\[(\d+)\]/g;
    if (!pattern.test(value)) {
      return;
    }
    pattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of value.matchAll(pattern)) {
      fragment.append(document.createTextNode(value.slice(last, match.index)));
      const marker = match[1];
      fragment.append(citations[marker] ? citationButton(marker, citations[marker]) : document.createTextNode(match[0]));
      last = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(value.slice(last)));
    node.replaceChildren(fragment);
  }

  function pretty(value) {
    if (value == null || value === "") {
      return "";
    }
    if (typeof value === "string") {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch {
        return value;
      }
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function oneLine(value) {
    const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
    return text.replace(/\s+/g, " ").slice(0, 80);
  }

  function formatDuration(ms) {
    if (!Number.isFinite(ms) || ms < 0) {
      return "";
    }
    if (ms < 1000) {
      return `${Math.round(ms)}ms`;
    }
    if (ms < 10000) {
      return `${(ms / 1000).toFixed(1)}s`;
    }
    const seconds = Math.round(ms / 1000);
    if (seconds < 60) {
      return `${seconds}s`;
    }
    return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
  }

  function eventTime(at) {
    const parsed = Date.parse(at || "");
    return Number.isFinite(parsed) ? parsed : Date.now();
  }

  function createAssistantTurn(container) {
    const div = document.createElement("div");
    div.className = "pg-turn pg-turn-assistant";
    div.innerHTML = `<div class="pg-turn-label">Agent</div><div class="pg-turn-body pg-turn-stream"></div>`;
    const body = div.querySelector(".pg-turn-body");
    container.appendChild(div);
    const tools = new Map();
    let ticker = null;
    let textEl = null;
    let full = "";
    let statusEl = null;
    let completedText = "";
    let usage = null;
    let answerCitations = {};

    function scroll() {
      const scroller = container.closest(".pg-chat-scroll") || container;
      scroller.scrollTop = scroller.scrollHeight;
    }

    function clearStatus() {
      if (statusEl) {
        statusEl.remove();
        statusEl = null;
      }
    }

    function showStatus(text) {
      clearStatus();
      statusEl = document.createElement("div");
      statusEl.className = "pg-activity";
      statusEl.textContent = text;
      body.appendChild(statusEl);
      scroll();
    }

    function ensureText() {
      clearStatus();
      if (textEl) {
        return textEl;
      }
      textEl = document.createElement("div");
      textEl.className = "pg-turn-text pg-streaming";
      body.appendChild(textEl);
      return textEl;
    }

    function closeText() {
      if (textEl) {
        textEl.classList.remove("pg-streaming");
        textEl = null;
      }
    }

    function note(text, kind) {
      clearStatus();
      closeText();
      const line = document.createElement("div");
      line.className = kind ? `pg-note pg-note-${kind}` : "pg-note";
      line.textContent = text;
      body.appendChild(line);
      scroll();
    }

    function toolRow(payload) {
      const key = String(payload.tool_call_id || payload.id || payload.name || "tool");
      const existing = tools.get(key);
      if (existing) {
        return existing;
      }
      closeText();
      clearStatus();
      const el = document.createElement("div");
      el.className = "pg-tool pg-tool-running";
      el.innerHTML = `<div class="pg-tool-row"><button type="button" class="pg-tool-head"><span class="pg-tool-mark" aria-hidden="true"></span><span class="pg-tool-name"></span><span class="pg-tool-summary"></span><span class="pg-tool-state">运行中</span><span class="pg-tool-chevron" aria-hidden="true"></span></button><button type="button" class="pg-tool-cite" hidden title="展开引用" aria-label="展开引用" aria-expanded="false">▶</button></div><div class="pg-tool-review" hidden></div><div class="pg-tool-detail" hidden><div class="pg-tool-block"><div class="pg-tool-block-label">参数</div><pre class="pg-tool-pre pg-tool-args"></pre></div><div class="pg-tool-block pg-tool-result-block" hidden><div class="pg-tool-block-label">结果</div><pre class="pg-tool-pre pg-tool-result"></pre></div></div>`;
      el.querySelector(".pg-tool-name").textContent = payload.name || "tool";
      el.querySelector(".pg-tool-head").addEventListener("click", () => {
        const detail = el.querySelector(".pg-tool-detail");
        const open = detail.hidden;
        detail.hidden = !open;
        el.classList.toggle("pg-tool-open", open);
      });
      if (isPreviewTool(payload.name)) {
        const cite = el.querySelector(".pg-tool-cite");
        cite.hidden = false;
        cite.classList.add("pg-tool-cite-running");
        cite.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const review = el.querySelector(".pg-tool-review");
          const opening = review.hidden;
          review.hidden = !opening;
          cite.classList.toggle("active", opening);
          cite.setAttribute("aria-expanded", String(opening));
          if (opening) {
            renderToolReview(el);
          }
        });
      }
      body.appendChild(el);
      tools.set(key, el);
      scroll();
      return el;
    }

    function fillArgs(el, payload) {
      if (payload.arguments == null && payload.args == null) {
        return;
      }
      const args = payload.arguments ?? payload.args;
      el.querySelector(".pg-tool-args").textContent = pretty(args) || "（无参数）";
      const summary = oneLine(args);
      if (summary && summary !== "{}") {
        el.querySelector(".pg-tool-summary").textContent = summary;
      }
    }

    function toolDuration(el, endedAt) {
      const started = Number(el.dataset.started);
      if (!started) {
        return "";
      }
      return formatDuration((endedAt || Date.now()) - started);
    }

    function tickTools() {
      body.querySelectorAll(".pg-tool-running").forEach((el) => {
        const duration = toolDuration(el);
        el.querySelector(".pg-tool-state").textContent = duration ? `运行中 · ${duration}` : "运行中";
      });
      if (!body.querySelector(".pg-tool-running")) {
        stopTicker();
      }
    }

    function ensureTicker() {
      if (ticker) {
        return;
      }
      ticker = setInterval(tickTools, 200);
    }

    function stopTicker() {
      if (ticker) {
        clearInterval(ticker);
        ticker = null;
      }
    }

    return {
      apply(type, payload, at) {
        const bodyPayload = payload || {};
        const moment = eventTime(at);
        if (type === "model_call_started") {
          if (!textEl) {
            showStatus("正在生成");
          }
        } else if (type === "message_delta" && bodyPayload.text) {
          full += String(bodyPayload.text);
          ensureText().textContent += String(bodyPayload.text);
          scroll();
        } else if (type === "message_completed") {
          completedText = String(bodyPayload.text || "");
          usage = bodyPayload.token_usage || null;
          answerCitations = bodyPayload.citations && typeof bodyPayload.citations === "object" ? bodyPayload.citations : {};
        } else if (type === "tool_call_started") {
          const el = toolRow(bodyPayload);
          el.dataset.started = String(moment);
          fillArgs(el, bodyPayload);
          tickTools();
          ensureTicker();
        } else if (type === "tool_call_completed" || type === "tool_call_failed") {
          const el = toolRow(bodyPayload);
          fillArgs(el, bodyPayload);
          const failed = type === "tool_call_failed" || bodyPayload.is_error;
          const duration = toolDuration(el, moment);
          el.classList.toggle("pg-tool-running", false);
          el.classList.toggle("pg-tool-error", failed);
          el.querySelector(".pg-tool-state").textContent = `${failed ? "失败" : "完成"}${duration ? ` · ${duration}` : ""}`;
          tickTools();
          const result = bodyPayload.text || bodyPayload.result || bodyPayload.error || "";
          const block = el.querySelector(".pg-tool-result-block");
          block.hidden = false;
          el.querySelector(".pg-tool-result").textContent = pretty(result) || "（无结果）";
          const cite = el.querySelector(".pg-tool-cite");
          if (cite && !cite.hidden) {
            cite.classList.remove("pg-tool-cite-running");
            el._claims = claimsIn(result);
            const review = el.querySelector(".pg-tool-review");
            if (review && !review.hidden) {
              renderToolReview(el);
            }
          } else {
            const found = previewsIn(parsePayload(result), 6);
            if (found.length) {
              const list = document.createElement("div");
              list.className = "pg-preview-list";
              found.forEach((item) => list.append(previewBlock(item)));
              el.querySelector(".pg-tool-detail").append(list);
            }
          }
          scroll();
        } else if (type === "answer_review_started") {
          showStatus(`检查回答 ${bodyPayload.attempt || 1}/${bodyPayload.max_attempts || ""}`.replace(/\/$/, ""));
        } else if (type === "answer_review_completed") {
          const passed = bodyPayload.passed === true || bodyPayload.result?.passed === true;
          note(passed ? "回答检查通过" : "回答检查未通过", passed ? "" : "error");
        } else if (type === "run_failed") {
          note(`运行失败：${bodyPayload.message || "unknown"}`, "error");
        } else if (type === "run_interrupted") {
          note("已中断");
        } else if (type === "run_completed") {
          closeText();
          clearStatus();
        }
      },
      seal() {
        stopTicker();
        body.querySelectorAll(".pg-tool-running").forEach((el) => {
          const duration = toolDuration(el);
          el.classList.remove("pg-tool-running");
          el.querySelector(".pg-tool-state").textContent = `未完成${duration ? ` · ${duration}` : ""}`;
        });
        closeText();
        clearStatus();
        if (completedText && Object.keys(answerCitations).length) {
          let text = body.querySelector(".pg-turn-text");
          if (!text) {
            text = document.createElement("div");
            text.className = "pg-turn-text";
            body.appendChild(text);
          }
          text.textContent = completedText;
          decorateAnswer(text, answerCitations);
        } else if (!full && completedText) {
          const el = document.createElement("div");
          el.className = "pg-turn-text";
          el.textContent = completedText;
          body.appendChild(el);
        }
        if (usage && (usage.input_tokens || usage.output_tokens || usage.total_tokens)) {
          const meta = document.createElement("div");
          meta.className = "pg-turn-meta";
          meta.textContent = `输入 ${usage.input_tokens || 0} · 输出 ${usage.output_tokens || 0}`;
          body.appendChild(meta);
        }
        if (!body.childElementCount) {
          div.remove();
        }
        scroll();
      },
    };
  }

  function parseSseBlock(block) {
    const lines = block.split("\n");
    let eventType = "message";
    let dataRaw = "";
    lines.forEach((line) => {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        dataRaw = line.slice(6);
      }
    });
    if (!dataRaw) {
      return null;
    }
    try {
      const data = JSON.parse(dataRaw);
      return { eventType, data };
    } catch {
      return null;
    }
  }

  function formatSessionTime(iso) {
    if (!iso) {
      return "";
    }
    try {
      return new Date(iso).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return String(iso).slice(0, 16);
    }
  }

  function sessionPath(agentId, sessionId) {
    return `${AGENT_API}/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}`;
  }

  async function consumeRunEvents(agentId, sessionId, runId, handlers) {
    const response = await fetch(`${sessionPath(agentId, sessionId)}/runs/${encodeURIComponent(runId)}/events`);
    if (!response.ok) {
      throw new Error("无法订阅 run events");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      parts.forEach((block) => {
        const parsed = parseSseBlock(block.trim());
        if (!parsed) {
          return;
        }
        const type = parsed.data.type || parsed.eventType;
        handlers.onEvent(type, parsed.data);
      });
    }
  }

  async function submitRun(agentId, sessionId, model, message, file, libraryId, needsLibrary) {
    const userId = currentUserId();
    if (!userId) {
      throw new Error("请先登录");
    }
    if (needsLibrary && !libraryId) {
      throw new Error("请先选择一个知识库");
    }
    const form = new FormData();
    form.append("message", message);
    form.append("user_id", userId);
    if (!sessionId && needsLibrary && libraryId) {
      form.append("library_ids", libraryId);
    }
    if (model && model !== "server-default") {
      form.append("agent_model", model);
    }
    if (file) {
      form.append("files", file, file.name);
    }
    const target = sessionId
      ? `${sessionPath(agentId, sessionId)}/runs`
      : `${AGENT_API}/agents/${encodeURIComponent(agentId)}/sessions`;
    const response = await fetch(target, {
      method: "POST",
      body: form,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    return payload;
  }

  async function initAgentListPage() {
    const loading = document.querySelector("#agents-loading");
    const list = document.querySelector("#agent-list");
    const empty = document.querySelector("#agent-list-empty");
    try {
      const agents = await api(`${AGENT_API}/agents`);
      loading.hidden = true;
      if (!agents.length) {
        empty.hidden = false;
        return;
      }
      list.innerHTML = "";
      agents.forEach((agent) => {
        const li = document.createElement("li");
        li.className = "pg-list-item";
        const link = document.createElement("a");
        link.href = `/playground/agent/${encodeURIComponent(agent.id)}`;
        link.innerHTML = `<strong>${agent.label || agent.id}</strong><span class="pg-mono">${agent.id}</span>`;
        li.appendChild(link);
        list.appendChild(li);
      });
      list.hidden = false;
    } catch (error) {
      loading.textContent = error.message || "加载失败";
    }
  }

  async function initChatPage() {
    const agentId = agentIdFromPath();
    if (!agentId) {
      window.location.replace("/playground/agents");
      return;
    }
    const needsLibrary = AGENTS_NEED_LIBRARY.has(agentId);
    document.querySelector("#chat-agent-id").textContent = agentId;

    const messages = document.querySelector("#chat-messages");
    const form = document.querySelector("#chat-form");
    const input = document.querySelector("#chat-input");
    const modelSelect = document.querySelector("#model-select");
    const sendBtn = document.querySelector("#chat-send");
    const attachBtn = document.querySelector("#btn-attach");
    const fileInput = document.querySelector("#attachment-input");
    const attachmentSlot = document.querySelector("#attachment-slot");
    const attachmentName = document.querySelector("#attachment-name");
    const attachmentMeta = document.querySelector("#attachment-meta");
    const attachmentRemove = document.querySelector("#attachment-remove");
    const libFoot = document.querySelector("#aside-library");
    const pickerSection = document.querySelector("#library-picker-section");
    const pickerToggle = document.querySelector("#library-picker-toggle");
    const pickerPanel = document.querySelector("#library-picker-panel");
    const pickerCurrent = document.querySelector("#library-picker-current");
    const pickerLoading = document.querySelector("#library-picker-loading");
    const pickerEmpty = document.querySelector("#library-picker-empty");
    const pickerList = document.querySelector("#library-picker-list");
    const pickerError = document.querySelector("#library-picker-error");
    const sessionListEl = document.querySelector("#session-list");
    const sessionLoadingEl = document.querySelector("#session-list-loading");
    const sessionEmptyEl = document.querySelector("#session-list-empty");
    const sessionErrorEl = document.querySelector("#session-list-error");
    const newSessionBtn = document.querySelector("#btn-new-session");

    let pendingFile = null;
    let sessionId = null;
    let selectedLibraryId = null;
    let libraryLocked = false;
    let introShown = false;
    const libraryNames = new Map();

    const layout = document.querySelector(".pg-chat-layout");
    const railToggle = document.querySelector("#btn-toggle-sessions");
    const railKey = "viknow_session_rail_open";

    function setRailOpen(open) {
      if (!layout || !railToggle) {
        return;
      }
      layout.classList.toggle("pg-sessions-collapsed", !open);
      railToggle.setAttribute("aria-expanded", String(open));
      railToggle.setAttribute("aria-label", open ? "收起会话列表" : "展开会话列表");
      railToggle.title = open ? "收起会话列表" : "展开会话列表";
      railToggle.textContent = open ? "‹" : "›";
      localStorage.setItem(railKey, open ? "1" : "0");
    }

    if (railToggle) {
      if (localStorage.getItem(railKey) === "1") {
        setRailOpen(true);
      }
      railToggle.addEventListener("click", () => {
        setRailOpen(layout.classList.contains("pg-sessions-collapsed"));
      });
    }

    function setLibraryPanel(open) {
      if (!pickerPanel || !pickerToggle || (open && libraryLocked)) {
        return;
      }
      pickerPanel.hidden = !open;
      pickerToggle.setAttribute("aria-expanded", String(open));
    }

    function chatIntroText(agentLabel) {
      return needsLibrary
        ? `你好，我是 ${agentLabel}。先点上方「知识库」选一个库，再输入问题；可附带 1 个文件。`
        : `你好，我是 ${agentLabel}。输入文字后可发送；可附带 1 个文件。`;
    }

    function showIntro(agentLabel) {
      if (introShown) {
        return;
      }
      appendMessage(messages, "assistant", chatIntroText(agentLabel));
      introShown = true;
    }

    function applyLibraryFromSession(libraryIds) {
      if (!needsLibrary || !libraryIds?.length) {
        return;
      }
      const id = libraryIds[0];
      setLibrarySelection(id);
      libraryLocked = true;
      if (pickerSection) {
        pickerSection.classList.add("pg-library-picker-locked");
      }
      setLibraryPanel(false);
      if (pickerList) {
        pickerList.querySelectorAll('input[type="radio"]').forEach((el) => {
          el.disabled = true;
          el.checked = el.value === id;
        });
      }
    }

    function unlockLibraryPicker() {
      libraryLocked = false;
      if (pickerSection) {
        pickerSection.classList.remove("pg-library-picker-locked");
      }
      if (pickerList) {
        pickerList.querySelectorAll('input[type="radio"]').forEach((el) => {
          el.disabled = false;
        });
      }
    }

    async function refreshSessionList() {
      const userId = currentUserId();
      if (!userId || !sessionListEl) {
        return;
      }
      sessionErrorEl.hidden = true;
      try {
        const rows = await api(
          `${AGENT_API}/agents/${encodeURIComponent(agentId)}/sessions?user_id=${encodeURIComponent(userId)}&limit=50`,
        );
        sessionLoadingEl.hidden = true;
        if (!rows.length) {
          sessionEmptyEl.hidden = false;
          sessionListEl.hidden = true;
          return;
        }
        sessionEmptyEl.hidden = true;
        sessionListEl.hidden = false;
        sessionListEl.innerHTML = "";
        rows.forEach((row) => {
          const li = document.createElement("li");
          li.className = "pg-session-item";
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "pg-session-link";
          if (row.id === sessionId) {
            btn.classList.add("pg-session-active");
          }
          btn.dataset.sessionId = row.id;
          const title = (row.title || "").trim() || "新对话";
          const titleEl = document.createElement("span");
          titleEl.className = "pg-session-link-title";
          titleEl.textContent = title;
          const metaEl = document.createElement("span");
          metaEl.className = "pg-session-link-meta";
          metaEl.textContent = formatSessionTime(row.updated_at);
          btn.append(titleEl, metaEl);
          btn.addEventListener("click", () => {
            openSession(row.id);
          });
          const del = document.createElement("button");
          del.type = "button";
          del.className = "pg-session-delete";
          del.title = "删除会话";
          del.setAttribute("aria-label", `删除会话「${title}」`);
          del.textContent = "×";
          del.addEventListener("click", (event) => {
            event.stopPropagation();
            deleteSession(row.id, title);
          });
          li.append(btn, del);
          sessionListEl.appendChild(li);
        });
      } catch (error) {
        sessionLoadingEl.hidden = true;
        sessionErrorEl.textContent = error.message || "无法加载会话";
        sessionErrorEl.hidden = false;
      }
    }

    async function deleteSession(targetSessionId, title) {
      if (!window.confirm(`删除会话「${title}」？其中的消息和附件都会删除。`)) {
        return;
      }
      sessionErrorEl.hidden = true;
      try {
        await api(`${sessionPath(agentId, targetSessionId)}?user_id=${encodeURIComponent(currentUserId())}`, {
          method: "DELETE",
        });
      } catch (error) {
        sessionErrorEl.textContent = `删除失败：${error.message}`;
        sessionErrorEl.hidden = false;
        return;
      }
      if (targetSessionId === sessionId) {
        startNewSession();
      } else {
        refreshSessionList();
      }
    }

    async function loadSessionHistory(targetSessionId) {
      const runs = await api(
        `${AGENT_API}/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(targetSessionId)}/runs?limit=100`,
      );
      const ordered = [...runs].sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
      const histories = await Promise.all(
        ordered.map(async (run) => {
          if (run.status === "running") {
            return null;
          }
          try {
            return await api(
              `${sessionPath(agentId, targetSessionId)}/runs/${encodeURIComponent(run.id)}/history`,
            );
          } catch {
            return null;
          }
        }),
      );
      ordered.forEach((run, index) => {
        const userText = (run.input || "").trim();
        if (userText) {
          appendMessage(messages, "user", userText);
        }
        const events = histories[index];
        if (events && events.length) {
          const turn = createAssistantTurn(messages);
          events.forEach((event) => turn.apply(event.type, event.payload || {}, event.created_at));
          turn.seal();
          return;
        }
        const assistantText = (run.output || "").trim();
        if (assistantText) {
          appendMessage(messages, "assistant", assistantText);
        } else if (run.status === "failed") {
          appendMessage(messages, "assistant", "（该轮运行失败）");
        } else if (run.status === "running") {
          appendMessage(messages, "assistant", "（这一轮还在运行）");
        }
      });
    }

    async function openSession(targetSessionId) {
      if (targetSessionId === sessionId) {
        return;
      }
      sessionId = targetSessionId;
      messages.innerHTML = "";
      introShown = true;
      unlockLibraryPicker();
      setLibrarySelection(null);
      try {
        const session = await api(
          `${AGENT_API}/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(targetSessionId)}`,
        );
        applyLibraryFromSession(session.library_ids);
        await loadSessionHistory(targetSessionId);
        document.querySelector("#aside-updated").textContent = session.updated_at || "—";
      } catch (error) {
        appendMessage(messages, "assistant", error.message || "无法加载会话");
      }
      await refreshSessionList();
      syncSendState();
    }

    function startNewSession() {
      sessionId = null;
      messages.innerHTML = "";
      introShown = false;
      unlockLibraryPicker();
      setLibrarySelection(null);
      if (pickerList) {
        pickerList.querySelectorAll('input[type="radio"]').forEach((el) => {
          el.checked = false;
        });
      }
      showIntro(agent?.label || agentId);
      refreshSessionList();
      syncSendState();
      input.focus();
    }

    function updateAsideLibrary() {
      if (libFoot) {
        libFoot.textContent = selectedLibraryId || "未选择";
      }
    }

    function syncSendState() {
      const hasText = input.value.trim().length > 0;
      const hasLibrary = !needsLibrary || Boolean(selectedLibraryId);
      sendBtn.disabled = !(hasText && hasLibrary);
      if (pickerSection) {
        pickerSection.classList.toggle("pg-library-picker-missing", needsLibrary && hasText && !hasLibrary);
      }
    }

    function setLibrarySelection(libraryId) {
      selectedLibraryId = libraryId;
      if (pickerCurrent) {
        pickerCurrent.textContent = libraryId ? libraryNames.get(libraryId) || libraryId : "未选择";
      }
      updateAsideLibrary();
      syncSendState();
      if (libraryId) {
        setLibraryPanel(false);
      }
    }

    function clearAttachment() {
      pendingFile = null;
      fileInput.value = "";
      attachmentSlot.hidden = true;
    }

    function setAttachment(file) {
      pendingFile = file;
      attachmentName.textContent = file.name;
      attachmentMeta.textContent = `${mediaLabel(file)} · ${formatBytes(file.size)}`;
      attachmentSlot.hidden = false;
    }

    async function loadLibraryPicker() {
      if (!needsLibrary || !pickerSection) {
        return;
      }
      pickerSection.hidden = false;
      try {
        const rows = await api(`${KNOWLEDGE_API}/libraries?limit=100`);
        pickerLoading.hidden = true;
        if (!rows.length) {
          pickerEmpty.hidden = false;
          setLibraryPanel(true);
          return;
        }
        pickerList.innerHTML = "";
        const groupName = `library-${agentId}`;
        rows.forEach((row) => {
          const named = row.library_id || row.id;
          if (named) {
            libraryNames.set(named, row.title || named);
          }
        });
        rows.forEach((row) => {
          const id = row.library_id || row.id;
          if (!id) {
            return;
          }
          const li = document.createElement("li");
          const label = document.createElement("label");
          label.className = "pg-library-option";
          const inputEl = document.createElement("input");
          inputEl.type = "radio";
          inputEl.name = groupName;
          inputEl.value = id;
          inputEl.disabled = libraryLocked;
          label.addEventListener("click", () => {
            inputEl.checked = true;
            setLibrarySelection(id);
          });
          inputEl.addEventListener("change", () => {
            if (inputEl.checked) {
              setLibrarySelection(id);
            }
          });
          const text = document.createElement("span");
          text.className = "pg-library-option-text";
          text.innerHTML = `<strong>${row.title || id}</strong><span>${id}</span>`;
          label.appendChild(inputEl);
          label.appendChild(text);
          li.appendChild(label);
          pickerList.appendChild(li);
        });
        pickerList.hidden = false;
        if (selectedLibraryId && pickerCurrent) {
          pickerCurrent.textContent = libraryNames.get(selectedLibraryId) || selectedLibraryId;
        } else if (!libraryLocked && !selectedLibraryId && rows.length === 1) {
          const only = rows[0].library_id || rows[0].id;
          const inputEl = [...pickerList.querySelectorAll("input")].find((el) => el.value === only);
          if (inputEl) {
            inputEl.checked = true;
            setLibrarySelection(only);
          }
        }
      } catch (error) {
        pickerLoading.hidden = true;
        pickerError.textContent = error.message || "无法加载知识库";
        pickerError.hidden = false;
      }
    }

    let agent;
    try {
      agent = await api(`${AGENT_API}/agents/${encodeURIComponent(agentId)}`);
    } catch (error) {
      appendMessage(messages, "assistant", error.message || "无法加载智能体");
      return;
    }

    const headTitle = document.querySelector("#chat-head-title");
    if (headTitle) {
      headTitle.textContent = agent.label || agent.id;
    }
    document.querySelector("#aside-title").textContent = agent.label || agent.id;
    document.querySelector("#aside-description").textContent = agent.description || "";
    document.querySelector("#aside-default-model").textContent = agent.default_model || "（服务端默认）";
    document.querySelector("#aside-updated").textContent = agent.updated_at || "—";
    updateAsideLibrary();
    document.title = `ViKnow · ${agent.label || agent.id}`;

    await loadLibraryPicker();

    let modelsPayload;
    try {
      modelsPayload = await api(`${AGENT_API}/models`);
    } catch {
      modelsPayload = { default_model: agent.default_model, models: [] };
    }
    const models = modelsPayload.models || [];
    modelSelect.innerHTML = "";
    const defaultId = agent.default_model || modelsPayload.default_model || models[0]?.id || "server-default";
    if (!models.length) {
      const opt = document.createElement("option");
      opt.value = "server-default";
      opt.textContent = "server-default";
      modelSelect.appendChild(opt);
    } else {
      models.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        if (m.id === defaultId) {
          opt.selected = true;
        }
        modelSelect.appendChild(opt);
      });
    }

    if (newSessionBtn) {
      newSessionBtn.addEventListener("click", () => startNewSession());
    }
    if (pickerToggle) {
      pickerToggle.addEventListener("click", () => {
        setLibraryPanel(pickerPanel.hidden);
      });
    }
    document.addEventListener("click", (event) => {
      if (pickerSection && !pickerSection.contains(event.target)) {
        setLibraryPanel(false);
      }
    });
    await refreshSessionList();
    showIntro(agent.label || agent.id);

    input.addEventListener("input", syncSendState);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (!sendBtn.disabled) {
          form.requestSubmit();
        }
      }
    });
    syncSendState();

    attachBtn.addEventListener("click", () => {
      fileInput.click();
    });

    fileInput.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      if (!file) {
        return;
      }
      setAttachment(file);
    });

    attachmentRemove.addEventListener("click", () => {
      clearAttachment();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) {
        return;
      }
      if (needsLibrary && !selectedLibraryId) {
        syncSendState();
        setLibraryPanel(true);
        return;
      }
      const model = modelSelect.value;
      const attachment = pendingFile
        ? {
            name: pendingFile.name,
            label: mediaLabel(pendingFile),
            size: formatBytes(pendingFile.size),
          }
        : null;

      appendMessage(messages, "user", text, attachment);
      const fileToSend = pendingFile;
      const libraryForRun = selectedLibraryId;
      input.value = "";
      clearAttachment();
      syncSendState();

      input.disabled = true;
      sendBtn.disabled = true;
      attachBtn.disabled = true;

      const turn = createAssistantTurn(messages);

      try {
        const run = await submitRun(
          agentId,
          sessionId,
          model,
          text,
          fileToSend,
          libraryForRun,
          needsLibrary,
        );
        sessionId = run.session_id || sessionId;
        if (needsLibrary && !libraryLocked) {
          applyLibraryFromSession(libraryForRun ? [libraryForRun] : []);
        }
        refreshSessionList();
        await consumeRunEvents(agentId, run.session_id, run.run_id, {
          onEvent(type, data) {
            turn.apply(type, data.payload || {}, data.created_at);
          },
        });
        turn.seal();
      } catch (error) {
        turn.seal();
        appendMessage(messages, "assistant", error.message || "发送失败");
      } finally {
        input.disabled = false;
        attachBtn.disabled = false;
        syncSendState();
        input.focus();
      }
    });
  }

  return { requireUser, initAgentListPage, initChatPage };
})();
