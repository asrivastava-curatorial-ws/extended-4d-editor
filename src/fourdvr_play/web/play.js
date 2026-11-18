"use strict";
// The pages' side of the protocol "4DPLAY": one WebSocket, JSON messages, reconnecting by itself.
const Play = (() => {
  const handlers = {};
  let ws = null, hello = null, retry = 500, timer = null, welcomed = false;

  function on(type, fn) { (handlers[type] = handlers[type] || []).push(fn); }
  function emit(type, msg) {
    for (const fn of handlers[type] || []) { try { fn(msg); } catch (e) { console.error(e); } }
  }
  function open() {
    clearTimeout(timer);
    if (!hello) return;
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => ws.send(JSON.stringify(Object.assign({type: "hello", play: 1}, hello)));
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (!m || typeof m.type !== "string") return;
      if (m.type === "welcome") { welcomed = true; retry = 500; }
      emit(m.type, m);
    };
    ws.onclose = (e) => {
      const was = welcomed;
      welcomed = false;
      emit("closed", {code: e.code, reason: e.reason, was: was});
      if (e.code === 4001 || !hello) return;          // refused (wrong key, no such seat): asking again will not help
      timer = setTimeout(open, retry);
      retry = Math.min(retry * 1.7, 5000);
    };
  }
  function connect(h) { close(); hello = h; open(); }
  function send(type, fields) {
    if (!ws || ws.readyState !== 1) return false;
    ws.send(JSON.stringify(Object.assign({type: type}, fields || {})));
    return true;
  }
  function close() {
    clearTimeout(timer);
    hello = null;
    if (ws) { ws.onclose = null; try { ws.close(); } catch (e) { /* closed already */ } }
    ws = null; welcomed = false;
  }
  return {on: on, connect: connect, send: send, close: close, online: () => welcomed};
})();

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}
function $(id) { return document.getElementById(id); }
function param(name) { return new URLSearchParams(location.search).get(name) || ""; }
function stored(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key) || "";
    localStorage.setItem(key, value);
  } catch (e) { /* private window: nothing is remembered */ }
  return value || "";
}
const PHASE_WORDS = {lobby: "Waiting to start", countdown: "Get ready", running: "Running", paused: "Paused", results: "Finished"};
