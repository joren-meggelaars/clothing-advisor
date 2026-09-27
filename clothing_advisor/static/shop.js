(function () {
  const $ = (id) => document.getElementById(id);
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  function gapView(gap) {
    if (!gap) return "";
    return `<section class="card"><h2>What you already have</h2>
      <ul>${gap.gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></section>`;
  }

  function briefView(brief) {
    if (!brief) return "";
    const sources = (brief.sources || []).map((s) =>
      `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></li>`).join("");
    return `<section class="card"><h2>Season research (${esc(brief.season)})</h2>
      <p>${esc(brief.text)}</p>
      ${sources ? `<p class="muted small">Sources:</p><ul class="small">${sources}</ul>` : ""}
      <p class="muted small">From ${esc((brief.created_at || "").slice(0, 10))}.</p></section>`;
  }

  function buyItem(b) {
    return `<div class="part buy"><div class="buy-body"><b>${esc(b.description)}</b>
      <span class="muted small">EUR ${Math.round(b.price_low)}-${Math.round(b.price_high)} · ${esc(b.store_suggestion)}</span>
      <span class="muted small">${esc(b.why)}</span></div></div>`;
  }

  function feedbackRow(adviceId, idx, fb) {
    const cur = fb && fb.verdict;
    return `<div class="row">
      <button type="button" class="${cur === "like" ? "on" : ""}" data-shop-fb="like" data-advice="${adviceId}" data-idx="${idx}">👍 Like</button>
      <button type="button" class="${cur === "dislike" ? "on" : ""}" data-shop-fb="dislike" data-advice="${adviceId}" data-idx="${idx}">👎 Not for me</button>
      ${cur ? `<button type="button" data-shop-fb="clear" data-advice="${adviceId}" data-idx="${idx}">Clear</button>` : ""}
      </div>`;
  }

  function outfitCard(o, adviceId, idx) {
    const owned = (o.owned || []).map((i) =>
      `<div class="part"><img src="${esc(i.thumb)}" alt="${esc(i.subtype)}" title="${esc(i.subtype)} (already owned)"></div>`).join("");
    const buy = (o.buy || []).map(buyItem).join("");
    return `<article class="outfit">
      <div class="body"><h3>${esc(o.name)}</h3>
      <p class="why">${esc(o.rationale)}</p>
      ${owned ? `<p class="muted small">Already have:</p><div class="parts">${owned}</div>` : ""}
      ${buy ? `<p class="muted small">To buy:</p><div class="parts">${buy}</div>` : ""}
      ${feedbackRow(adviceId, idx, o.feedback)}</div></article>`;
  }

  function render(state) {
    $("gap").innerHTML = gapView(state.gap);
    const advice = state.advice;
    $("reply").textContent = advice ? advice.reply : "";
    $("brief").innerHTML = briefView(state.brief);
    $("outfits").innerHTML = advice ? advice.outfits.map((o, i) => outfitCard(o, advice.id, i)).join("")
      : '<p class="muted">No shopping advice yet.</p>';
  }

  async function run() {
    $("go").disabled = true;
    $("status").textContent = "Researching and thinking… this can take a minute, longer the first time (season research).";
    try {
      const state = await post("/api/shop/advise", { request: $("q").value, refresh_brief: $("refresh").checked });
      $("status").textContent = "";
      render(state);
      $("q").value = "";
      $("refresh").checked = false;
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      $("go").disabled = false;
    }
  }

  $("ask").addEventListener("submit", (e) => { e.preventDefault(); run(); });

  document.addEventListener("click", async (e) => {
    const b = e.target.closest ? e.target.closest("button[data-shop-fb]") : null;
    if (!b) return;
    try {
      render(await post("/api/shop/feedback", {
        advice_id: Number(b.dataset.advice), outfit_idx: Number(b.dataset.idx), verdict: b.dataset.shopFb,
      }));
    } catch (err) { $("status").textContent = err.message; }
  });

  api("/api/shop/current").then(render).catch((e) => { $("status").textContent = e.message; });
})();
