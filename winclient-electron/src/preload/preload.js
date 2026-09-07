'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/**
 * Единственный мост между окнами и main-процессом.
 *
 * contextIsolation включён, nodeIntegration выключен, поэтому рендерер не
 * видит ни fs, ни сети напрямую — только перечисленные ниже вызовы. Все они
 * возвращают {ok, ...} либо {ok: false, error}, чтобы окно не ловило
 * исключения через IPC.
 */
contextBridge.exposeInMainWorld('portal', {
  // --- вход ---
  getSettings: () => ipcRenderer.invoke('settings:get'),
  tryStoredLogin: () => ipcRenderer.invoke('auth:try-stored'),
  login: (payload) => ipcRenderer.invoke('auth:login', payload),
  logout: () => ipcRenderer.invoke('auth:logout'),

  // --- устройства ---
  getSession: () => ipcRenderer.invoke('session:get'),
  listPeers: () => ipcRenderer.invoke('devices:list'),
  listGroups: () => ipcRenderer.invoke('devices:groups'),
  setOnlineOnly: (value) => ipcRenderer.invoke('settings:online-only', value),

  // --- действия ---
  connect: (peer) => ipcRenderer.invoke('session:open', peer),
  openInBrowser: (peerId) => ipcRenderer.invoke('session:browser', peerId),
  copyId: (peerId) => ipcRenderer.invoke('clipboard:id', peerId),

  // Уведомление о протухшем токене приходит из main, окно на него закрывается.
  onAuthExpired: (handler) => ipcRenderer.on('auth:expired', () => handler()),
});
