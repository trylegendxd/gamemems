/* eslint-env browser */
(function () {
  const $ = (id) => document.getElementById(id);
  const status = $("status");

  function load() {
    chrome.storage.sync.get({ backendUrl: "", apiToken: "" }, (cfg) => {
      $("backendUrl").value = cfg.backendUrl || "";
      $("apiToken").value = cfg.apiToken || "";
    });
  }

  function save() {
    const backendUrl = ($("backendUrl").value || "").trim().replace(/\/+$/, "");
    const apiToken = ($("apiToken").value || "").trim();
    if (!backendUrl) {
      status.className = "err";
      status.textContent = "Backend URL não pode estar vazio.";
      return;
    }
    if (!/^https?:\/\//.test(backendUrl)) {
      status.className = "err";
      status.textContent = "URL deve começar com http:// ou https://.";
      return;
    }
    if (!apiToken) {
      status.className = "err";
      status.textContent = "API token é obrigatório.";
      return;
    }
    chrome.storage.sync.set({ backendUrl, apiToken }, () => {
      status.className = "ok";
      status.textContent = "Guardado com sucesso.";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("save").addEventListener("click", save);
  });
})();
