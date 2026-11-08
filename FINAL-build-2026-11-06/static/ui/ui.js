/* UI motion layer: sliding selection pills, animated theme switch, menu section labels, number count-up.
   It only decorates the page - it never calls the API or changes application state.
   Remove the <script src="ui.js"> tag and the page still works (without the animations). */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- 1. Sliding selection pill (menu + rail) ---------- */
  function slidingPill(box) {
    if (!box) return;
    const pill = document.createElement("span");
    pill.className = "nav-pill";
    pill.setAttribute("aria-hidden", "true");
    box.prepend(pill);
    box.classList.add("has-pill");
    let first = true;

    const place = () => {
      const on = box.querySelector("button.on");
      const visible = on && on.offsetWidth > 0;
      if (!visible) { pill.style.opacity = "0"; return; }
      pill.style.width = on.offsetWidth + "px";
      pill.style.height = on.offsetHeight + "px";
      pill.style.transform = `translate(${on.offsetLeft}px,${on.offsetTop}px)`;
      pill.style.opacity = "1";
      if (first) {                       // first placement: jump there, don't fly in from the corner
        first = false;
        requestAnimationFrame(() => requestAnimationFrame(() => pill.classList.add("ready")));
      }
      if (box.scrollWidth > box.clientWidth + 2 && !reduce)   // horizontal (mobile) menu
        on.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
    };

    new MutationObserver(place).observe(box, { attributes: true, attributeFilter: ["class"], subtree: true });
    new ResizeObserver(place).observe(box);   // also fires when #app goes from display:none to grid
    addEventListener("resize", place);
    addEventListener("load", place);
    place();
  }

  /* ---------- 2. Menu section labels: hide a label when the role sees none of its items ---------- */
  function syncMenuLabels() {
    const menu = $(".menu");
    if (!menu) return;
    let label = null, any = false;
    const flush = () => { if (label) label.hidden = !any; };
    [...menu.children].forEach(el => {
      if (el.classList.contains("menu-label")) { flush(); label = el; any = false; }
      else if (el.tagName === "BUTTON" && getComputedStyle(el).display !== "none") any = true;
    });
    flush();
  }

  /* ---------- 3. Theme: remember choice, animate the switch ---------- */
  function themeSwitch() {
    const btn = $("#theme");
    const root = document.documentElement;
    const label = () => {
      const dark = root.dataset.theme === "dark";
      if (btn) {
        btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
        btn.title = dark ? "Light theme" : "Dark theme";
      }
    };
    label();
    if (!btn) return;
    // capture phase: take over the click so the change can be wrapped in a View Transition
    btn.addEventListener("click", e => {
      e.stopImmediatePropagation();
      const flip = () => {
        root.dataset.theme = root.dataset.theme === "dark" ? "" : "dark";
        try { localStorage.setItem("theme", root.dataset.theme === "dark" ? "dark" : "light"); } catch (_) {}
        label();
      };
      if (document.startViewTransition && !reduce) document.startViewTransition(flip);
      else flip();
    }, true);
  }

  /* ---------- 4. Count-up for big dashboard numbers ---------- */
  function countUp(el) {
    if (el.dataset.counted) return;
    el.dataset.counted = "1";
    const m = el.textContent.trim().match(/^(\d+(?:\.\d+)?)(.*)$/);
    if (!m || reduce) return;
    const end = parseFloat(m[1]), dec = (m[1].split(".")[1] || "").length, tail = m[2];
    const t0 = performance.now(), dur = 700;
    const step = now => {
      const p = Math.min(1, (now - t0) / dur), k = 1 - Math.pow(1 - p, 3);
      el.textContent = (end * k).toFixed(dec) + tail;
      if (p < 1) requestAnimationFrame(step); else el.textContent = m[1] + tail;
    };
    requestAnimationFrame(step);
  }
  function watchView() {
    const view = $("#view");
    if (!view) return;
    const scan = () => view.querySelectorAll(".big").forEach(countUp);
    new MutationObserver(scan).observe(view, { childList: true, subtree: true });
  }

  function init() {
    slidingPill($(".menu"));
    slidingPill($(".rail"));
    syncMenuLabels();
    new MutationObserver(syncMenuLabels).observe(document.body, { attributes: true, attributeFilter: ["data-role"] });
    themeSwitch();
    watchView();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
