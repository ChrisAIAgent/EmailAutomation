const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("emailAutomation", Object.freeze({
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  openLogs: () => ipcRenderer.invoke("open-logs"),
  repair: () => ipcRenderer.invoke("repair-runtime"),
  quitAndStop: () => ipcRenderer.invoke("quit-and-stop"),
  reload: () => ipcRenderer.invoke("reload-window"),
  getRuntimeStatus: () => ipcRenderer.invoke("runtime-status"),
  getDiagnostics: () => ipcRenderer.invoke("diagnostics"),
  investigate: (target) => ipcRenderer.invoke("diagnostics-investigate", target || "system"),
}));
