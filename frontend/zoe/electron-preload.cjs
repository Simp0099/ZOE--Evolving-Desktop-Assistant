const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("electronWindow", {
  minimize: () => ipcRenderer.send("zoe-window-minimize"),
  maximize: () => ipcRenderer.send("zoe-window-maximize"),
  close: () => ipcRenderer.send("zoe-window-close"),
});
