(function () {
  const $ = (id) => document.getElementById(id);
  const files = $("files");
  const per = window.PER_PHOTO || 0;

  files.addEventListener("change", () => {
    const n = files.files.length;
    $("upload").disabled = n === 0;
    if (n) {
      api("/api/estimate?n=" + n).then((e) => {
        $("est").textContent = `${n} photos ≈ EUR ${e.total_eur.toFixed(2)} (${e.basis}); budget left this month: EUR ${e.budget_left_eur.toFixed(2)}.`;
      }).catch(() => {});
    }
  });

  $("upload").addEventListener("click", async () => {
    const list = Array.from(files.files);
    if (!list.length) return;
    const est = (per * list.length).toFixed(2);
    if (!confirm(`Upload ${list.length} photos? Cataloguing costs about EUR ${est}.`)) return;
    $("upload").disabled = true;
    $("prog").hidden = false; $("prog").max = list.length; $("prog").value = 0;
    let queued = 0, dup = 0;
    const failures = [];
    for (const f of list) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await api("/api/upload", { method: "POST", body: fd });
        if (r.state === "duplicate") dup++; else queued++;
      } catch (e) {
        failures.push(`${f.name} (${Math.round(f.size / 1024)} KB): ${e.message}`);
      }
      $("prog").value += 1;
      $("upmsg").textContent = `${queued} queued, ${dup} duplicates, ${failures.length} failed`;
    }
    $("failures").innerHTML = failures.map((x) => `<li>${esc(x)}</li>`).join("");
    files.value = "";
    poll();
  });

  $("import").addEventListener("click", async () => {
    if (!confirm("Import everything in the inbox folder?")) return;
    try {
      const r = await api("/api/inbox/import", { method: "POST" });
      $("upmsg").textContent = `Inbox: ${r.queued} queued, ${r.duplicate} duplicates, ${r.failed} failed`;
      $("inboxn").textContent = "0"; $("inboxeur").textContent = "0.00"; $("import").disabled = true;
      poll();
    } catch (e) { $("upmsg").textContent = e.message; }
  });

  async function poll() {
    try {
      const q = await api("/api/queue");
      $("queue").textContent = `${q.queued} waiting · ${q.processing} in progress · ${q.error} failed · ${q.done_unreviewed} ready to review` +
        (q.paused ? ` — paused: ${q.paused}` : "");
      $("failed_items").innerHTML = (q.failed_items || []).map((f) =>
        `<li>#${f.id}: ${esc(f.error || "unknown error")}</li>`).join("");
    } catch (e) { /* ignore */ }
  }
  poll();
  setInterval(poll, 4000);
})();
