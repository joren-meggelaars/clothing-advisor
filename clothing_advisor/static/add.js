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

  // Downscale in the browser before uploading: a phone photo is 3-8 MB (proxies often reject that, "Load failed"),
  // the server shrinks it to 1024 px anyway. Also turns HEIC into JPEG where the browser can decode it.
  async function shrink(file) {
    try {
      if (!window.createImageBitmap) return file;
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      if (bmp.close) bmp.close();
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
      return blob ? new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" }) : file;
    } catch (e) {
      return file; // fall back to the original; the server reports it if it cannot read it
    }
  }

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
      const up = await shrink(f);
      const fd = new FormData();
      fd.append("file", up, up.name);
      try {
        const r = await api("/api/upload", { method: "POST", body: fd });
        if (r.state === "duplicate") dup++; else queued++;
      } catch (e) {
        failures.push(`${f.name} (${Math.round(f.size / 1024)} KB, sent as ${Math.round(up.size / 1024)} KB): ${e.message}`);
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
