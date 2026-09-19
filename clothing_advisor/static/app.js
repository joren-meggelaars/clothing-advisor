// Shared helpers. The token is only present when the browser did not accept our cookie (e.g. inside an iframe).
window.api = function (path, opts) {
  const t = (window.CA && window.CA.t) || "";
  const url = t ? path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(t) : path;
  return fetch(url, opts).then(async (r) => {
    let body = null;
    try { body = await r.json(); } catch (e) { /* not JSON */ }
    if (r.status === 413) throw new Error("File too large for the server or proxy (HTTP 413)");
    if (!r.ok) throw new Error((body && body.detail) || "Request failed (HTTP " + r.status + ")");
    return body;
  });
};
window.esc = function (s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};
