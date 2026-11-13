// 1. Generate & persist unique device fingerprint
async function getOrCreateDeviceId() {
  let stored = localStorage.getItem("iw_device_fp");
  if (!stored) {
    const raw = [
      navigator.userAgent,
      navigator.language,
      screen.width + "x" + screen.height,
      Intl.DateTimeFormat().resolvedOptions().timeZone,
      navigator.hardwareConcurrency || "4",
      crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)
    ].join("|");
    const encoder = new TextEncoder();
    const hashBuffer = await crypto.subtle.digest("SHA-256", encoder.encode(raw));
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    stored = hashArray.map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
    localStorage.setItem("iw_device_fp", stored);
  }
  const input = document.getElementById("device_id_input");
  if (input) input.value = stored;
  return stored;
}

document.addEventListener("DOMContentLoaded", () => {
  getOrCreateDeviceId();

  // 2. Auto-hide top-left animated flash toasts
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

// 3. Toggle Sidebar Menu
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
