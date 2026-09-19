(function () {
  const $ = (id) => document.getElementById(id);
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  function outfitCard(o, opts) {
    const parts = (o.items || []).map((i) =>
      `<div class="part"><img src="${esc(i.thumb)}" alt="${esc(i.subtype)}" title="${esc(i.subtype)}">` +
      (opts.exclude ? `<button type="button" title="Don't use this piece" data-exclude="${i.id}">✕</button>` : "") + `</div>`).join("");
    const buttons = opts.actions
      ? `<div class="row">` +
        (o.status === "chosen"
          ? `<button data-worn="${o.id}" class="primary">I wore it</button>`
          : `<button data-choose="${o.id}" class="primary">Wear this</button>`) +
        `<button data-reject="${o.id}">Not this</button></div>`
      : "";
    return `<article class="outfit ${o.status === "chosen" ? "chosen" : ""}">
      <img class="collage" src="${esc(o.image)}" alt="${esc(o.name)}">
      <div class="body"><h3>${esc(o.name)}${o.status === "chosen" ? " ✓" : ""}</h3>
      <p class="why">${esc(o.reason)}</p>${parts ? `<div class="parts">${parts}</div>` : ""}${buttons}</div></article>`;
  }

  function render(state) {
    const today = state.today;
    $("today").innerHTML = today
      ? `<section class="card todaycard"><h2>Today: ${esc(today.name)}${today.status === "worn" ? " (worn)" : ""}</h2><p class="muted small">${esc(today.items_text)}</p></section>`
      : "";
    $("reply").textContent = state.reply || "";
    const proposals = state.outfits.filter((o) => !(today && o.id === today.id && today.status === "worn"));
    $("outfits").innerHTML = proposals.map((o) => outfitCard(o, { actions: true, exclude: true })).join("");
    if (state.session_id) document.querySelector('input[name="mode"][value="refine"]').checked = true;
  }

  async function run(message, extra) {
    const mode = document.querySelector('input[name="mode"]:checked').value;
    $("go").disabled = true;
    $("status").textContent = "Thinking… this can take up to a minute.";
    try {
      const state = await post("/api/advice", Object.assign({
        message, new_session: mode === "new", ignore_weather: $("noweather").checked,
      }, extra || {}));
      $("status").textContent = "";
      render(state);
      $("q").value = "";
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      $("go").disabled = false;
    }
  }

  $("ask").addEventListener("submit", (e) => { e.preventDefault(); run($("q").value); });
  $("chips").addEventListener("click", (e) => {
    const q = e.target.dataset && e.target.dataset.q;
    if (!q) return;
    document.querySelector('input[name="mode"][value="new"]').checked = true;
    run(q);
  });
  $("outfits").addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    try {
      if (d.choose) render(await post("/api/outfits/" + d.choose + "/choose"));
      else if (d.worn) render(await post("/api/outfits/" + d.worn + "/worn"));
      else if (d.reject) render(await post("/api/outfits/" + d.reject + "/reject"));
      else if (d.exclude) {
        document.querySelector('input[name="mode"][value="refine"]').checked = true;
        run("Without that piece, please suggest new outfits.", { exclude_ids: [Number(d.exclude)] });
      }
    } catch (err) { $("status").textContent = err.message; }
  });

  api("/api/advice/current").then(render).catch((e) => { $("status").textContent = e.message; });
})();
