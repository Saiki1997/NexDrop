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
  lookupUrlState: (urls) => ipcRenderer.invoke("lookup-url-state", urls),
  recentRuns: (limit) => ipcRenderer.invoke("recent-runs", limit),
  sourceMaintenance: (action) => ipcRenderer.invoke("source-maintenance", action),
  authList: () => ipcRenderer.invoke("auth-list"),
  authSave: (body) => ipcRenderer.invoke("auth-save", body),
  authRemove: (id) => ipcRenderer.invoke("auth-remove", id),
  authTest: (id) => ipcRenderer.invoke("auth-test", id),
  authChrome: (body) => ipcRenderer.invoke("auth-chrome", body),
  telegramLogin: (body) => ipcRenderer.invoke("telegram-login", body),
  sitesList: () => ipcRenderer.invoke("sites-list"),
  sitesTest: (body) => ipcRenderer.invoke("sites-test", body),
  sitesSave: (body) => ipcRenderer.invoke("sites-save", body),
  sitesAuthorize: (body) => ipcRenderer.invoke("sites-authorize", body),
  sitesRemove: (host) => ipcRenderer.invoke("sites-remove", host),
  onPipelineEvent: (callback) => {
    const listener = (_event, data) => callback(data);
    ipcRenderer.on("pipeline-event", listener);
    return () => ipcRenderer.removeListener("pipeline-event", listener);
  },
});
