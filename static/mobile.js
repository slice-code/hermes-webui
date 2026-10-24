/* ==========================================================================
   Hermes Web — perilaku "app-like" bersama (4 halaman + login).

   Isi:
   1. viewport: --app-vh dari visualViewport + body.kb-mode saat keyboard terbuka
   2. tab bar: haptic ringan, tab aktif tidak reload
   3. sheet: kunci scroll body, kembalikan fokus ke pemicu, animasi masuk
   4. service worker: didaftarkan HANYA di secure context, gagal = senyap

   Tidak ada dependency, tidak ada build step. Semua best-effort: kalau satu
   API tidak ada (visualViewport, vibrate, serviceWorker), halaman tetap jalan.
   ========================================================================== */
(() => {
  "use strict";

  const docEl = document.documentElement;
  const body = document.body;
  const MOBILE_Q = "(max-width: 899.98px)";
  const mq = window.matchMedia ? window.matchMedia(MOBILE_Q) : null;
  const mobile = () => (mq ? mq.matches : window.innerWidth < 900);
  const vv = window.visualViewport || null;

  /* ------------------------------------------------------------------ *
   * 1. Viewport + mode keyboard
   * ------------------------------------------------------------------ */

  const TEXT_TYPES = /^(text|search|password|number|email|url|tel|date|datetime-local|time)$/i;

  function isTextEntry(el) {
    if (!el || !el.tagName) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName === "INPUT") {
      // type kosong = text; checkbox/radio/button/file tidak membuka keyboard teks
      return TEXT_TYPES.test(el.getAttribute("type") || "text");
    }
    return false;
  }

  let kbOverride = false;   // dipaksa lewat API (untuk uji/otomasi)
  // Autofokus saat halaman dimuat (chat memanggil input.focus()) BUKAN keyboard
  // sungguhan: di HP browser tidak membuka keyboard tanpa gestur user. Tanpa
  // penanda ini, tab bar langsung hilang begitu halaman dibuka.
  let userInteracted = false;

  // Setiap gestur juga memicu syncKeyboard: kalau textarea SUDAH fokus
  // (autofokus, atau tap kedua di composer), tidak ada event focusin baru —
  // tanpa ini tab bar tidak pernah turun saat keyboard benar-benar muncul.
  ["pointerdown", "touchstart", "keydown"].forEach((evt) => {
    document.addEventListener(evt, () => {
      userInteracted = true;
      syncKeyboard();
    }, { capture: true, passive: true });
  });

  function setKb(on) {
    const want = !!on && mobile();
    if (body.classList.contains("kb-mode") === want) return;
    body.classList.toggle("kb-mode", want);
  }

  function textFocused() {
    return isTextEntry(document.activeElement);
  }

  // Keyboard dianggap terbuka kalau: field teks fokus SETELAH user menyentuh
  // halaman, ATAU visualViewport menyusut >140px saat field teks masih fokus
  // (Chrome Android kadang tidak mengirim focus/blur dengan rapi).
  function syncKeyboard() {
    if (!mobile()) { setKb(false); return; }
    const focused = textFocused();
    let byViewport = false;
    if (vv) {
      const shrunk = window.innerHeight - vv.height;
      byViewport = shrunk > 140 && focused;
    }
    setKb(kbOverride || (focused && userInteracted) || byViewport);
  }

  function syncViewportHeight() {
    // --app-vh dipakai body.chat-app supaya tinggi layout mengikuti area yang
    // benar-benar terlihat (keyboard + address bar), bukan 100vh yang bohong.
    const h = vv ? vv.height : window.innerHeight;
    if (h > 0) docEl.style.setProperty("--app-vh", Math.round(h) + "px");
    syncKeyboard();
  }

  document.addEventListener("focusin", (e) => {
    if (isTextEntry(e.target)) syncKeyboard();
  }, true);
  document.addEventListener("focusout", (e) => {
    if (!isTextEntry(e.target)) return;
    // Tunggu satu frame: fokus bisa langsung pindah ke field lain.
    setTimeout(syncKeyboard, 0);
  }, true);

  if (vv) {
    vv.addEventListener("resize", syncViewportHeight);
    vv.addEventListener("scroll", syncViewportHeight);
  }
  window.addEventListener("resize", syncViewportHeight);
  window.addEventListener("orientationchange", () => setTimeout(syncViewportHeight, 120));
  syncViewportHeight();

  /* ------------------------------------------------------------------ *
   * 2. Bottom tab bar
   * ------------------------------------------------------------------ */

  const tabbar = document.querySelector(".tabbar");
  if (tabbar) {
    tabbar.addEventListener("click", (event) => {
      const link = event.target.closest("a");
      if (!link) return;
      // Getaran 8 ms — boleh gagal senyap (iOS tidak punya navigator.vibrate).
      try { if (navigator.vibrate) navigator.vibrate(8); } catch (_) { /* diabaikan */ }
      if (link.getAttribute("aria-current") === "page") {
        // Tab yang sudah aktif: jangan reload, cukup kembali ke atas.
        event.preventDefault();
        const scroller = document.querySelector(".thread") || document.scrollingElement;
        if (scroller && scroller.scrollTo) scroller.scrollTo({ top: 0, behavior: "smooth" });
        else window.scrollTo({ top: 0, behavior: "smooth" });
      }
    });
    // iOS lama hanya mengaktifkan :active kalau ada listener sentuh.
    document.addEventListener("touchstart", () => {}, { passive: true });
  }

  /* ------------------------------------------------------------------ *
   * 3. Sheet / panel / modal: kunci scroll + kembalikan fokus
   * ------------------------------------------------------------------ */

  // `.panel` dan `.modal` memakai kelas `open`; `.sheetWrap` memakai atribut hidden.
  function overlayOpen(el) {
    if (el.classList.contains("panel") || el.classList.contains("modal")) {
      return el.classList.contains("open");
    }
    return !el.hasAttribute("hidden");
  }

  function overlays() {
    return Array.prototype.slice.call(document.querySelectorAll(".panel, .sheetWrap, .modal"));
  }

  function anyOpen() {
    if (body.classList.contains("side-open")) return true;
    return overlays().some(overlayOpen);
  }

  let lastTrigger = null;
  let locked = false;
  let lockY = 0;

  // Simpan pemicu terakhir: pointerdown terjadi SEBELUM handler halaman
  // mengubah kelas, jadi activeElement masih elemen lama.
  document.addEventListener("pointerdown", (event) => {
    const el = event.target.closest && event.target.closest(
      "button, a[href], [role='tab'], summary, .card, .sess, .rrow");
    if (el && !el.closest(".panel, .sheetWrap, .modal")) lastTrigger = el;
  }, true);

  function lockScroll() {
    if (locked) return;
    locked = true;
    lockY = window.scrollY || window.pageYOffset || 0;
    docEl.classList.add("lock");
    if (mobile()) {
      // iOS Safari mengabaikan overflow:hidden pada body → pakai position:fixed
      // lalu pulihkan posisi scroll saat dibuka kembali.
      body.style.position = "fixed";
      body.style.top = -lockY + "px";
      body.style.left = "0";
      body.style.right = "0";
      body.style.width = "100%";
    }
  }

  function unlockScroll() {
    if (!locked) return;
    locked = false;
    docEl.classList.remove("lock");
    body.style.position = "";
    body.style.top = "";
    body.style.left = "";
    body.style.right = "";
    body.style.width = "";
    if (lockY) window.scrollTo(0, lockY);
  }

  let wasOpen = anyOpen();
  let queued = false;

  function reconcile() {
    queued = false;
    const now = anyOpen();
    if (now && !wasOpen) {
      lockScroll();
    } else if (!now && wasOpen) {
      unlockScroll();
      if (lastTrigger && document.contains(lastTrigger) && lastTrigger.focus) {
        try { lastTrigger.focus({ preventScroll: true }); } catch (_) {
          try { lastTrigger.focus(); } catch (__) { /* diabaikan */ }
        }
      }
    }
    wasOpen = now;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    (window.requestAnimationFrame || window.setTimeout)(reconcile, 16);
  }

  const mo = new MutationObserver(schedule);
  mo.observe(body, { attributes: true, attributeFilter: ["class", "hidden"], subtree: true });

  /* ------------------------------------------------------------------ *
   * 4. Service worker — hanya secure context
   * ------------------------------------------------------------------ */

  const secure = location.protocol === "https:"
    || location.hostname === "localhost"
    || location.hostname === "127.0.0.1";

  if (secure && "serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" })
        .catch(() => { /* diabaikan: PWA opsional, UI tidak boleh terganggu */ });
    });
  }

  /* ------------------------------------------------------------------ *
   * 5. Transisi masuk halaman (opacity saja → tidak ada layout shift)
   * ------------------------------------------------------------------ */

  const shell = document.querySelector("#papan") || document.querySelector("main");
  if (shell) shell.classList.add("app-fade");

  /* ------------------------------------------------------------------ *
   * 6. API kecil untuk pengujian/otomasi
   * ------------------------------------------------------------------ */

  window.HermesMobile = {
    isMobile: mobile,
    kbMode: (on) => { kbOverride = !!on; syncKeyboard(); return body.classList.contains("kb-mode"); },
    overlayOpen: anyOpen,
    tabbarHeight: () => {
      const el = document.querySelector(".tabbar");
      return el && getComputedStyle(el).display !== "none" ? el.getBoundingClientRect().height : 0;
    },
  };
})();
