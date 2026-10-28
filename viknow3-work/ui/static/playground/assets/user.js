const STORAGE_KEY = "viknow_playground_user";
const APP_KEY_STORAGE = "viknow_app_key";

function apiHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  const key = (localStorage.getItem(APP_KEY_STORAGE) || window.VIKNOW_APP_KEY || "").trim();
  if (key) {
    headers["X-App-Key"] = key;
  }
  return headers;
}

function showError(message) {
  const box = document.querySelector("#login-error");
  if (!box) return;
  box.hidden = false;
  box.textContent = message;
}

function clearError() {
  const box = document.querySelector("#login-error");
  if (!box) return;
  box.hidden = true;
  box.textContent = "";
}

function detailMessage(payload) {
  if (!payload || typeof payload !== "object") return "请求失败";
  const detail = payload.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    return detail.message || detail.detail || JSON.stringify(detail);
  }
  return payload.message || "请求失败";
}

async function createUser() {
  const nameInput = document.querySelector("#display-name");
  const result = document.querySelector("#create-result");
  const userInput = document.querySelector("#user-id");
  const btn = document.querySelector("#btn-create");
  if (!btn || !result) return;

  btn.disabled = true;
  clearError();
  try {
    const display_name = nameInput?.value?.trim() || null;
    const usersApi = window.PlaygroundApi ? window.PlaygroundApi.users() : "/api/v1/users";
    const response = await fetch(usersApi, {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ display_name }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    result.hidden = false;
    result.innerHTML = `<strong>已创建</strong><br /><span class="pg-mono">${payload.id}</span>`;
    if (userInput) {
      userInput.value = payload.id;
      userInput.focus();
    }
  } catch (error) {
    showError(error.message || "创建用户失败");
  } finally {
    btn.disabled = false;
  }
}

async function login(event) {
  if (event) event.preventDefault();
  clearError();
  const userInput = document.querySelector("#user-id");
  const submit = document.querySelector("#login-form button[type=submit]");
  const userId = userInput?.value?.trim();
  if (!userId) {
    showError("请输入用户 ID");
    return;
  }
  if (submit) submit.disabled = true;
  try {
    const usersApi = window.PlaygroundApi ? window.PlaygroundApi.users() : "/api/v1/users";
    const response = await fetch(`${usersApi}/${encodeURIComponent(userId)}`, {
      method: "GET",
      headers: apiHeaders(),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(detailMessage(payload));
    }
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        id: payload.id,
        display_name: payload.display_name,
        updated_at: payload.updated_at,
      }),
    );
    window.location.href = "/playground/workspace";
  } catch (error) {
    showError(error.message || "登录失败");
  } finally {
    if (submit) submit.disabled = false;
  }
}

const createBtn = document.querySelector("#btn-create");
if (createBtn) {
  createBtn.addEventListener("click", createUser);
}

const loginForm = document.querySelector("#login-form");
if (loginForm) {
  loginForm.addEventListener("submit", login);
  const cached = localStorage.getItem(STORAGE_KEY);
  if (cached && window.location.pathname === "/playground") {
    window.location.replace("/playground/workspace");
  }
} else {
  forgetDeletedUser();
}

async function forgetDeletedUser() {
  let userId = "";
  try {
    userId = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}").id || "";
  } catch {
    userId = "";
  }
  if (!userId) return;
  const usersApi = window.PlaygroundApi ? window.PlaygroundApi.users() : "/api/v1/users";
  const response = await fetch(`${usersApi}/${encodeURIComponent(userId)}`).catch(() => null);
  if (response && response.status === 404) {
    localStorage.removeItem(STORAGE_KEY);
    window.location.replace("/playground");
  }
}
