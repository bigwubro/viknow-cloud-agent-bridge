const library = document.querySelector("#library");
const file = document.querySelector("#file");
const uploadButton = document.querySelector("#upload");
const uploaded = document.querySelector("#uploaded");
const mode = document.querySelector("#mode");
const indexButton = document.querySelector("#index");
const refreshButton = document.querySelector("#refresh");
const jobOut = document.querySelector("#job");

function show(node, value) {
  node.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

const notice =
  "此页已停用：请使用 Playground（/playground）通过 viknow-storage 上传资源，并用 ViKnow API 创建索引任务。";

uploadButton.disabled = true;
indexButton.disabled = true;
refreshButton.disabled = true;
show(uploaded, notice);
show(jobOut, notice);
