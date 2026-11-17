async function sha256Hex(str) {
  const encoder = new TextEncoder();
  const hashBuffer = await crypto.subtle.digest("SHA-256", encoder.encode(str));
  return Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

function getCanvasFingerprint() {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 240;
    canvas.height = 60;
    const ctx = canvas.getContext("2d");
    ctx.textBaseline = "top";
    ctx.font = "14px 'Arial'";
    ctx.fillStyle = "#064e3b";
    ctx.fillRect(10, 5, 80, 22);
    ctx.fillStyle = "#10b981";
    ctx.fillText("InfinityWork-FP-2026", 12, 10);
    ctx.fillStyle = "rgba(245, 158, 11, 0.75)";
    ctx.fillText("∞৳", 180, 12);
    return canvas.toDataURL().slice(-80);
  } catch (e) {
    return "no-canvas";
  }
}

function getWebGLFingerprint() {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (!gl) return "no-webgl";
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    const exts = (gl.getSupportedExtensions() || []).length;
    const base = dbg
      ? `${gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)}|${gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)}`
      : `${gl.getParameter(gl.VENDOR)}|${gl.getParameter(gl.RENDERER)}`;
    return `${base}|${exts}|${gl.getParameter(gl.MAX_TEXTURE_SIZE)}`;
  } catch (e) {
    return "no-webgl";
  }
}

// 4-Layer Persistent Lock (Cookie + localStorage + IndexedDB + CacheStorage)
function readCookie(name) {
  const m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[2]) : "";
}

function idbGet(key) {
  return new Promise((resolve) => {
    try {
      const req = indexedDB.open("InfinityWorkLockDB", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("kv");
      req.onsuccess = () => {
        const tx = req.result.transaction("kv", "readonly");
        const getReq = tx.objectStore("kv").get(key);
        getReq.onsuccess = () => resolve(getReq.result || "");
        getReq.onerror = () => resolve("");
      };
      req.onerror = () => resolve("");
    } catch (e) {
      resolve("");
    }
  });
}

function idbSet(key, val) {
  try {
    const req = indexedDB.open("InfinityWorkLockDB", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("kv");
    req.onsuccess = () => {
      const tx = req.result.transaction("kv", "readwrite");
      tx.objectStore("kv").put(val, key);
    };
  } catch (e) {}
}

async function cacheGetLock() {
  try {
    if (!("caches" in window)) return "";
    const cache = await caches.open("iw-lock-cache-v1");
    const res = await cache.match("/__iw_lock");
    return res ? await res.text() : "";
  } catch (e) {
    return "";
  }
}

async function cacheSetLock(val) {
  try {
    if (!("caches" in window)) return;
    const cache = await caches.open("iw-lock-cache-v1");
    await cache.put("/__iw_lock", new Response(val));
  } catch (e) {}
}

// 1. Generate Deterministic Device ID & Recover Persistent Lock Token
async function getOrCreateDeviceId() {
  const raw = [
    getWebGLFingerprint(),
    screen.width + "x" + screen.height + "x" + screen.colorDepth,
    window.devicePixelRatio || 1,
    navigator.hardwareConcurrency || 0,
    navigator.deviceMemory || 0,
    navigator.maxTouchPoints || 0,
    navigator.platform || "",
    navigator.language || "",
    Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    getCanvasFingerprint(),
  ].join("|");

  const deviceId = await sha256Hex(raw);

  const fromCookie = readCookie("iw_lock_token");
  const fromLS = localStorage.getItem("iw_lock_token") || "";
  const fromIDB = await idbGet("iw_lock_token");
  const fromCache = await cacheGetLock();

  const existingLock = fromCookie || fromLS || fromIDB || fromCache || "";
  if (existingLock) {
    localStorage.setItem("iw_lock_token", existingLock);
    idbSet("iw_lock_token", existingLock);
    cacheSetLock(existingLock);
  }

  const devInput = document.getElementById("device_id_input");
  const lockInput = document.getElementById("lock_token_input");
  if (devInput) devInput.value = deviceId;
  if (lockInput) lockInput.value = existingLock;

  return { deviceId, existingLock };
}

function persistDeviceLockFromCookie() {
  const token = readCookie("iw_lock_token");
  if (token) {
    localStorage.setItem("iw_lock_token", token);
    idbSet("iw_lock_token", token);
    cacheSetLock(token);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  getOrCreateDeviceId();
  persistDeviceLockFromCookie();

  // 2. Auto-hide Top-Left Animated Flash Toasts
  const toasts = document.querySelectorAll(".iw-flash-toast");
  toasts.forEach((toast, idx) => {
    setTimeout(() => {
      toast.classList.remove("opacity-0", "-translate-x-8");
      toast.classList.add("opacity-100", "translate-x-0");
    }, idx * 100 + 30);

    setTimeout(() => {
      toast.classList.add("opacity-0", "-translate-x-8");
      setTimeout(() => toast.remove(), 350);
    }, 4500 + idx * 300);
  });
});

// 3. Toggle Sidebar Menu Drawer
function toggleMenuDrawer(forceClose = false) {
  const drawer = document.getElementById("iw-drawer");
  const backdrop = document.getElementById("iw-drawer-backdrop");
  if (!drawer || !backdrop) return;

  const isOpen = !drawer.classList.contains("translate-x-full");
  if (forceClose || isOpen) {
    drawer.classList.add("translate-x-full");
    backdrop.classList.add("hidden");
  } else {
    drawer.classList.remove("translate-x-full");
    backdrop.classList.remove("hidden");
  }
}

// 4. Copy Text Helper
function copyText(text, btnId) {
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById(btnId);
    if (btn) {
      const old = btn.innerText;
      btn.innerText = "কপি হয়েছে ✓";
      setTimeout(() => (btn.innerText = old), 2000);
    }
  });
}

// 5. Register PWA Service Worker & Handle Install Prompt
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  const btn = document.getElementById("pwa-install-btn");
  if (btn) btn.classList.remove("hidden");
});

async function installInfinityPWA() {
  if (!deferredPrompt) return;
  deferredPrompt.prompt();
  await deferredPrompt.userChoice;
  deferredPrompt = null;
  const btn = document.getElementById("pwa-install-btn");
  if (btn) btn.classList.add("hidden");
}
