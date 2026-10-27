"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  startPipeline: (config) => ipcRenderer.invoke("start-pipeline", config),
  stopPipeline: () => ipcRenderer.invoke("stop-pipeline"),
  openPath: (targetPath) => ipcRenderer.invoke("open-path", targetPath),
  openLogs: (which) => ipcRenderer.invoke("open-logs", which),
  openLogFile: (file) => ipcRenderer.invoke("open-log-file", file),
  flareStatus: () => ipcRenderer.invoke("flare-status"),
  flareSet: (on) => ipcRenderer.invoke("flare-set", on),
  getDefaults: () => ipcRenderer.invoke("get-defaults"),
  saveGuiSetting: (patch) => ipcRenderer.invoke("save-gui-setting", patch),
  pickCookiesFile: () => ipcRenderer.invoke("pick-cookies-file"),
  pickDownloadDir: () => ipcRenderer.invoke("pick-download-dir"),
  pickJobDir: () => ipcRenderer.invoke("pick-job-dir"),
  jobsExport: (jobs) => ipcRenderer.invoke("jobs-export", jobs),
  jobsImport: () => ipcRenderer.invoke("jobs-import"),
  lookupUrlState: (urls) => ipcRenderer.invoke("lookup-url-state", urls),
  recentRuns: (limit) => ipcRenderer.invoke("recent-runs", limit),
  sourceMaintenance: (action) => ipcRenderer.invoke("source-maintenance", action),
  authList: () => ipcRenderer.invoke("auth-list"),
  authSave: (body) => ipcRenderer.invoke("auth-save", body),
  authRemove: (id) => ipcRenderer.invoke("auth-remove", id),
  authTest: (id) => ipcRenderer.invoke("auth-test", id),
  authChrome: (body) => ipcRenderer.invoke("auth-chrome", body),
  telegramLogin: (body) => ipcRenderer.invoke("telegram-login", body),
  applyAppOptions: () => ipcRenderer.invoke("app-options"),
  openReleasePage: (url) => ipcRenderer.invoke("open-release-page", url),
  updateCheck: () => ipcRenderer.invoke("update-check"),
  updateInstall: () => ipcRenderer.invoke("update-install"),
  updateSkip: (version) => ipcRenderer.invoke("update-skip", version),
  updateAuto: (on) => ipcRenderer.invoke("update-auto", on),
  historySearch: (query) => ipcRenderer.invoke("history-search", query),
  historyReveal: (id) => ipcRenderer.invoke("history-reveal", id),
  historyRename: (body) => ipcRenderer.invoke("history-rename", body),
  namingCorrections: (body) => ipcRenderer.invoke("naming-corrections", body),
  onPipelineEvent: (callback) => {
    const listener = (_event, data) => callback(data);
    ipcRenderer.on("pipeline-event", listener);
    return () => ipcRenderer.removeListener("pipeline-event", listener);
  },
});
