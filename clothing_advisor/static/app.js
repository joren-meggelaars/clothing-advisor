// Shared helpers. The token is only present when the browser did not accept our cookie (e.g. inside an iframe).
window.api = function (path, opts) {
  const t = (window.CA && window.CA.t) || "";
  const url = t ? path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(t) : path;
  return fetch(url, opts).then(async (r) => {
    let body = null;
    try { body = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error((body && body.detail) || "Request failed (" + r.status + ")");
    return body;
  });
};
window.esc = function (s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};
