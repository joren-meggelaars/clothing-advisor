(function () {
  const $ = (id) => document.getElementById(id);
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const REASONS = ["colours", "too warm", "too cold", "too formal", "too casual", "not my style", "uncomfortable"];

  let lastState = null;
  // Open feedback panel: {id, mode: "rate" | "reject", stars, reasons: [], comment: ""}
  let fb = null;

  function stars(o, label) {
    const cur = (o.rating && o.rating.stars) || 0;
    const buttons = [1, 2, 3, 4, 5].map((i) =>
      `<button type="button" class="star ${i <= cur ? "on" : ""}" data-stars="${i}" data-outfit="${o.id}" aria-label="${i} of 5 stars">★</button>`).join("");
    return `<div class="rate"><span class="muted small">${esc(label)}</span><span class="stars">${buttons}</span></div>`;
  }

  function panel(o) {
    if (!fb || fb.id !== o.id) return "";
    const reject = fb.mode === "reject";
    const chips = REASONS.map((r) =>
      `<button type="button" class="${fb.reasons.includes(r) ? "on" : ""}" data-reason="${esc(r)}">${esc(r)}</button>`).join("");
    return `<div class="fb">
      <p class="muted small">${reject ? "What was wrong with it? (optional)" : "What made it that rating? (optional)"}</p>
      <div class="chips">${chips}</div>
      <input type="text" id="fbtext" maxlength="500" placeholder="Anything to add?" value="${esc(fb.comment)}">
      <div class="row">
        <button type="button" class="primary" data-fbsave="1">${reject ? "Send & remove" : "Save"}</button>
        <button type="button" data-fbskip="1">${reject ? "Just remove" : "Done"}</button>
      </div></div>`;
  }

  function outfitCard(o) {
    const parts = (o.items || []).map((i) =>
      `<div class="part"><img src="${esc(i.thumb)}" alt="${esc(i.subtype)}" title="${esc(i.subtype)}">` +
      `<button type="button" title="Don't use this piece" data-exclude="${i.id}">✕</button></div>`).join("");
    const buttons = `<div class="row">` +
      (o.status === "chosen"
        ? `<button data-worn="${o.id}" class="primary">I wore it</button>`
        : `<button data-choose="${o.id}" class="primary">Wear this</button>`) +
      `<button data-reject="${o.id}">Not this</button></div>`;
    return `<article class="outfit ${o.status === "chosen" ? "chosen" : ""}">
      <img class="collage" src="${esc(o.image)}" alt="${esc(o.name)}">
      <div class="body"><h3>${esc(o.name)}${o.status === "chosen" ? " ✓" : ""}</h3>
      <p class="why">${esc(o.reason)}</p>${parts ? `<div class="parts">${parts}</div>` : ""}
      ${stars(o, "Rate")}${panel(o)}${buttons}</div></article>`;
  }

  function render(state) {
    lastState = state;
    const today = state.today;
    $("today").innerHTML = today
      ? `<section class="card todaycard"><h2>Today: ${esc(today.name)}${today.status === "worn" ? " (worn)" : ""}</h2>
         <p class="muted small">${esc(today.items_text)}</p>
         ${today.status === "worn" ? stars(today, "How was it?") + panel(today) : ""}</section>`
      : "";
    $("reply").textContent = state.reply || "";
    const proposals = state.outfits.filter((o) => !(today && o.id === today.id && today.status === "worn"));
    $("outfits").innerHTML = proposals.map(outfitCard).join("");
    if (state.session_id) document.querySelector('input[name="mode"][value="refine"]').checked = true;
  }

  async function run(message, extra) {
    const mode = document.querySelector('input[name="mode"]:checked').value;
    $("go").disabled = true;
    $("status").textContent = "Thinking… this can take up to a minute.";
    try {
      const state = await post("/api/advice", Object.assign({
        message, new_session: mode === "new", ignore_weather: $("noweather").checked,
        ignore_workdays: $("ignorework").checked, extra_formal: $("formal").checked,
      }, extra || {}));
      $("status").textContent = "";
      fb = null;
      render(state);
      $("q").value = "";
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      $("go").disabled = false;
    }
  }

  function readNote() {
    const t = $("fbtext");
    if (t && fb) fb.comment = t.value;
  }

  $("ask").addEventListener("submit", (e) => { e.preventDefault(); run($("q").value); });
  $("chips").addEventListener("click", (e) => {
    const q = e.target.dataset && e.target.dataset.q;
    if (!q) return;
    document.querySelector('input[name="mode"][value="new"]').checked = true;
    run(q);
  });

  document.addEventListener("click", async (e) => {
    const b = e.target.closest ? e.target.closest("button") : null;
    if (!b || b.closest("#chips") || b.closest("#ask")) return;
    const d = b.dataset || {};
    try {
      if (d.stars) {
        const id = Number(d.outfit);
        const rated = lastState && [...lastState.outfits, lastState.today].find((o) => o && o.id === id);
        const keep = fb && fb.id === id && fb.mode === "rate" ? fb : null;
        fb = { id, mode: "rate", stars: Number(d.stars),
               reasons: keep ? keep.reasons : ((rated && rated.rating && rated.rating.reasons) || []),
               comment: keep ? keep.comment : ((rated && rated.rating && rated.rating.comment) || "") };
        render(await post("/api/outfits/" + id + "/rate", { stars: fb.stars }));
      } else if (d.reason) {
        readNote();
        const i = fb.reasons.indexOf(d.reason);
        if (i >= 0) fb.reasons.splice(i, 1); else fb.reasons.push(d.reason);
        render(lastState);
      } else if (d.fbsave) {
        readNote();
        const body = { reasons: fb.reasons, comment: fb.comment };
        const id = fb.id;
        if (fb.mode === "reject") {
          fb = null;
          render(await post("/api/outfits/" + id + "/reject", body));
        } else {
          body.stars = fb.stars;
          fb = null;
          render(await post("/api/outfits/" + id + "/rate", body));
        }
      } else if (d.fbskip) {
        const id = fb.id, reject = fb.mode === "reject";
        fb = null;
        render(reject ? await post("/api/outfits/" + id + "/reject", {}) : lastState);
      } else if (d.reject) {
        fb = { id: Number(d.reject), mode: "reject", stars: 1, reasons: [], comment: "" };
        render(lastState);
      } else if (d.choose) {
        render(await post("/api/outfits/" + d.choose + "/choose"));
      } else if (d.worn) {
        render(await post("/api/outfits/" + d.worn + "/worn"));
      } else if (d.exclude) {
        document.querySelector('input[name="mode"][value="refine"]').checked = true;
        run("Without that piece, please suggest new outfits.", { exclude_ids: [Number(d.exclude)] });
      }
    } catch (err) { $("status").textContent = err.message; }
  });

  api("/api/advice/current").then(render).catch((e) => { $("status").textContent = e.message; });
})();
