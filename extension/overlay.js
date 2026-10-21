/* FerDim sayfa icindeki yuzen dugme.
 * Arka plan "medya bulundu" deyince gorunur; tiklayinca FerDim panelini acar. */
"use strict";
(() => {
  if (window.__ferdimOverlay) return;
  window.__ferdimOverlay = true;

  let btn = null;

  function ensure() {
    if (btn) return btn;
    btn = document.createElement("div");
    btn.id = "ferdim-float";
    btn.setAttribute("style", [
      "position:fixed", "right:18px", "bottom:18px", "z-index:2147483647",
      "display:none", "align-items:center", "gap:8px",
      "padding:10px 16px", "border-radius:999px",
      "background:linear-gradient(135deg,#38bdf8,#0369a1)",
      "color:#fff", "font:600 14px system-ui,sans-serif",
      "box-shadow:0 4px 18px rgba(2,132,199,.5)", "cursor:pointer",
      "user-select:none", "border:2px solid rgba(255,255,255,.35)",
    ].join(";") + ";");
    const label = (browser.i18n && browser.i18n.getMessage("overlayLabel")) ||
                  "FerDim ile indir";
    btn.innerHTML =
      '<svg width="18" height="18" viewBox="0 0 96 96">' +
      '<path d="M48 16v40m0 0 18-18m-18 18-18-18M20 78h56" stroke="#fff"' +
      'stroke-width="12" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    btn.appendChild(document.createTextNode(" " + label));
    btn.addEventListener("click", () => {
      browser.runtime.sendMessage({ openPanel: true }).catch(() => {});
    });
    const host = document.documentElement || document.body;
    host.appendChild(btn);
    return btn;
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || !msg.ferdimMedia) return;
    const b = ensure();
    b.style.display = "flex";
    b.animate(
      [{ transform: "scale(.7)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }],
      { duration: 220, easing: "ease-out" });
  });
})();
