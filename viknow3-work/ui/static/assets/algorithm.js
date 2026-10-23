const library = document.querySelector("#library");
const file = document.querySelector("#file");
const uploadButton = document.querySelector("#upload");
const uploaded = document.querySelector("#uploaded");
const mode = document.querySelector("#mode");
const indexButton = document.querySelector("#index");
const refreshButton = document.querySelector("#refresh");
const jobOut = document.querySelector("#job");

let asset = null;
let jobId = null;

function show(node, value) {
  node.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail?.message || payload.message || response.statusText);
  }
  return payload;
}

uploadButton.addEventListener("click", async () => {
  if (!file.files.length) {
    show(uploaded, "请先选择文件");
    return;
  }
  const body = new FormData();
  body.append("library_id", library.value.trim());
  body.append("file", file.files[0]);
  uploadButton.disabled = true;
  try {
    asset = await readJson(await fetch("/ui/knowledge/uploads", { method: "POST", body }));
    jobId = null;
    show(uploaded, asset);
    indexButton.disabled = false;
    refreshButton.disabled = true;
    show(jobOut, "");
  } catch (error) {
    asset = null;
    indexButton.disabled = true;
    show(uploaded, error.message);
  } finally {
    uploadButton.disabled = false;
  }
});

indexButton.addEventListener("click", async () => {
  if (!asset) {
    return;
  }
  indexButton.disabled = true;
  try {
    const job = await readJson(await fetch("/ui/knowledge/index-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        library_id: asset.library_id,
        asset_id: asset.asset_id,
        asset_path: asset.asset_path,
        title: asset.file_name,
        compile_mode: mode.value || null,
      }),
    }));
    jobId = job.job_id;
    refreshButton.disabled = false;
    show(jobOut, job);
  } catch (error) {
    show(jobOut, error.message);
  } finally {
    indexButton.disabled = false;
  }
});

refreshButton.addEventListener("click", async () => {
  if (!jobId) {
    return;
  }
  try {
    show(jobOut, await readJson(await fetch("/ui/knowledge/index-jobs/" + encodeURIComponent(jobId))));
  } catch (error) {
    show(jobOut, error.message);
  }
});
