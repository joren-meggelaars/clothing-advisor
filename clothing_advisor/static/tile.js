// Compact tile for the Home Assistant dashboard. Shows today's prepared suggestions; one tap to pick one.
(function () {
  const root = document.getElementById("root");
  const CFG = window.CA || {};
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  let state = null;       // response of /api/advice/current
  let view = "suggestions"; // "suggestions" | "today"
  let index = 0;          // visible suggestion
  let busy = "";          // text of the "working" overlay
  let error = "";
  let lastSig = "";      // what the last refresh looked like, to avoid redrawing while you swipe

  const signature = (s) => JSON.stringify([s.outfits.map((o) => [o.id, o.status, o.rating && o.rating.stars]),
    s.today && [s.today.id, s.today.status, s.today.rating && s.today.rating.stars], s.weather_short]);

  function slide(o, label) {
    return `<article class="slide">
      <img class="collage" src="${esc(o.image_square || o.image)}" alt="${esc(o.name)}">
      <div class="cap">${label ? `<div class="label">${esc(label)}</div>` : ""}
        <h2>${esc(o.name)}</h2><p>${esc(o.reason || o.items_text || "")}</p></div></article>`;
  }

  function stars(o) {
    const cur = (o.rating && o.rating.stars) || 0;
    const buttons = [1, 2, 3, 4, 5].map((i) =>
      `<button type="button" class="star ${i <= cur ? "on" : ""}" data-act="rate" data-stars="${i}" aria-label="${i} of 5 stars">★</button>`).join("");
    return `<div class="rate"><span class="muted small">How was it?</span><span class="stars">${buttons}</span></div>`;
  }

  function frame(inner) {
    const wx = [state && state.weather_short, state && state.prepared_at ? "ready " + state.prepared_at : ""].filter(Boolean).join(" · ");
    return `<div class="frame">
      <div class="topline"><span class="wx">${esc(wx)}</span><a class="more" href="${esc(CFG.full || "/app")}" target="_blank" rel="noopener">Open app ↗</a></div>
      ${inner}
      ${error ? `<div class="err">${esc(error)}</div>` : ""}
      ${busy ? `<div class="busy">${esc(busy)}</div>` : ""}</div>`;
  }

  function render() {
    if (!state) { root.innerHTML = '<p class="msg">Loading…</p>'; return; }
    const today = state.today;
    const list = state.outfits;

    if (view === "today" && today) {
      const worn = today.status === "worn";
      root.innerHTML = frame(
        `<div class="stage"><div class="slides">${slide(today, worn ? "Today · worn" : "Today's outfit")}</div></div>` +
        `<footer class="actions">` + (worn ? stars(today)
          : `<button class="primary" data-act="wore">I wore it</button><button data-act="change">Change</button>`) + `</footer>`);
      return;
    }
    if (!list.length) {
      root.innerHTML = frame(
        `<div class="center"><div>${CFG.autoOn ? "Your outfit is prepared every morning at " + esc(CFG.autoTime) + "." : "No suggestions yet."}</div>` +
        `<button class="primary" data-act="suggest">Suggest now</button></div>`);
      return;
    }
    index = Math.min(index, list.length - 1);
    root.innerHTML = frame(
      `<div class="stage"><div class="slides" id="slides">${list.map((o) => slide(o, o.status === "chosen" ? "Your pick" : "")).join("")}</div>` +
      (list.length > 1 ? `<div class="dots" id="dots">${list.map((_, i) => `<i class="${i === index ? "on" : ""}"></i>`).join("")}</div>` : "") + `</div>` +
      `<footer class="actions"><button class="primary" data-act="wear">✓ Wear this</button><button data-act="another">↻ Another</button></footer>`);

    const slides = document.getElementById("slides");
    slides.scrollLeft = index * slides.clientWidth;
    slides.addEventListener("scroll", () => {
      const i = Math.round(slides.scrollLeft / Math.max(slides.clientWidth, 1));
      if (i !== index) {
        index = i;
        document.querySelectorAll("#dots i").forEach((d, n) => d.classList.toggle("on", n === i));
      }
    }, { passive: true });
  }

  async function run(label, fn) {
    busy = label; error = ""; render();
    try { await fn(); } catch (e) { error = e.message; }
    busy = "";
    if (state) lastSig = signature(state);
    render();
  }

  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || busy || !b.dataset.act) return;
    const act = b.dataset.act;
    if (act === "wear") {
      const o = state.outfits[index];
      run("Saving…", async () => { state = await post(`/api/outfits/${o.id}/choose`); view = "today"; });
    } else if (act === "wore") {
      run("Saving…", async () => { state = await post(`/api/outfits/${state.today.id}/worn`); });
    } else if (act === "rate") {
      const stars = Number(b.dataset.stars);
      run("Saving…", async () => { state = await post(`/api/outfits/${state.today.id}/rate`, { stars }); });
    } else if (act === "change") {
      view = "suggestions"; index = 0; render();
    } else if (act === "another") {
      run("Finding other options… this can take up to a minute.", async () => {
        state = await post("/api/advice", { message: "Give me different options.", new_session: !state.session_id });
        view = "suggestions"; index = 0;
      });
    } else if (act === "suggest") {
      run("Preparing your outfit… this can take up to a minute.", async () => {
        state = await post("/api/advice", { message: CFG.request || "Suggest my outfit for today.", new_session: true });
        view = "suggestions"; index = 0;
      });
    }
  });

  // Keep a wall-mounted tile current: refresh now and then, and poll while waiting for the morning suggestion.
  async function refresh() {
    if (busy) return;
    try {
      const fresh = await api("/api/advice/current");
      if (signature(fresh) === lastSig) return;
      const hadNothing = state && !state.outfits.length && !state.today;
      state = fresh;
      lastSig = signature(fresh);
      if (hadNothing || (!state.today && view === "today")) view = state.today ? "today" : "suggestions";
      render();
    } catch (e) { /* keep showing what we have */ }
  }

  api("/api/advice/current").then((s) => {
    state = s;
    lastSig = signature(s);
    view = s.today ? "today" : "suggestions";
    render();
  }).catch((e) => { root.innerHTML = `<p class="msg">${esc(e.message)}</p>`; });

  setInterval(refresh, 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
