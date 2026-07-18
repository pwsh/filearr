"use strict";
// Filearr local web UI — read-only. This script issues ONLY GET requests to the
// agent's local query API (never a mutating verb). It relies on the same-origin
// session cookie (set by the bootstrap-token exchange) for auth.

(function () {
  var qInput = document.getElementById("q");
  var form = document.getElementById("search-form");
  var results = document.getElementById("results");
  var body = document.getElementById("results-body");
  var meta = document.getElementById("meta");
  var errBox = document.getElementById("error");
  var empty = document.getElementById("empty");
  var bannerScope = document.getElementById("banner-scope");
  var bannerStale = document.getElementById("banner-stale");
  var scopePreds = document.getElementById("scope-preds");
  var indexStatus = document.getElementById("index-status");

  var LIMIT = 200;
  var debounceTimer = null;
  var inflight = null; // AbortController for the current request

  function fmtSize(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return n + " B";
    var units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
    var v = n, i = -1;
    do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
    return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
  }

  function fmtMtime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit"
    });
  }

  function splitPath(rel) {
    var i = rel.lastIndexOf("/");
    if (i < 0) return { dir: "", name: rel };
    return { dir: rel.slice(0, i + 1), name: rel.slice(i + 1) };
  }

  function clearError() { errBox.hidden = true; errBox.textContent = ""; }
  function showError(msg) {
    errBox.hidden = false;
    errBox.textContent = msg;
    results.hidden = true;
    empty.hidden = true;
  }

  function renderScope(scope, stale) {
    var active = scope && scope.active;
    bannerScope.hidden = !active;
    if (active) {
      var preds = (scope.predicates || []);
      scopePreds.textContent = preds.length ? preds.join("  ·  ") : "";
    }
    bannerStale.hidden = !stale;
  }

  function copyToClipboard(text, btn) {
    function ok() {
      var prev = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text, ok); });
    } else {
      fallbackCopy(text, ok);
    }
  }
  function fallbackCopy(text, ok) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); ok(); } catch (e) { /* no-op */ }
    document.body.removeChild(ta);
  }

  function render(data) {
    clearError();
    renderScope(data.scope, data.scope && data.scope.stale);
    body.textContent = "";

    var rows = data.rows || [];
    if (rows.length === 0) {
      results.hidden = true;
      empty.hidden = false;
      meta.textContent = describeMeta(data, 0);
      return;
    }
    empty.hidden = true;
    results.hidden = false;

    var frag = document.createDocumentFragment();
    rows.forEach(function (row) {
      var tr = document.createElement("tr");

      var tdPath = document.createElement("td");
      tdPath.className = "path";
      var parts = splitPath(row.rel_path || "");
      if (parts.dir) {
        var dir = document.createElement("span");
        dir.className = "dir";
        dir.textContent = parts.dir;
        tdPath.appendChild(dir);
      }
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = parts.name;
      tdPath.appendChild(name);
      if (row.fuzzy_matched) {
        var tag = document.createElement("span");
        tag.className = "fuzzy-tag";
        tag.textContent = "fuzzy";
        tdPath.appendChild(tag);
      }
      tr.appendChild(tdPath);

      var tdSize = document.createElement("td");
      tdSize.className = "col-size";
      tdSize.textContent = fmtSize(row.size);
      tr.appendChild(tdSize);

      var tdMod = document.createElement("td");
      tdMod.className = "col-mod";
      tdMod.textContent = fmtMtime(row.mtime);
      tr.appendChild(tdMod);

      var tdKind = document.createElement("td");
      tdKind.className = "col-kind";
      tdKind.textContent = row.kind || "";
      tr.appendChild(tdKind);

      var tdCopy = document.createElement("td");
      tdCopy.className = "col-copy";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      btn.textContent = "Copy path";
      btn.addEventListener("click", function () { copyToClipboard(row.rel_path, btn); });
      tdCopy.appendChild(btn);
      tr.appendChild(tdCopy);

      frag.appendChild(tr);
    });
    body.appendChild(frag);
    meta.textContent = describeMeta(data, rows.length);
  }

  function describeMeta(data, shown) {
    var bits = [];
    bits.push(shown + (data.truncated ? "+" : "") + " result" + (shown === 1 ? "" : "s"));
    if (typeof data.elapsed_ms === "number") bits.push(data.elapsed_ms + " ms");
    if (data.fuzzy) bits.push("includes fuzzy matches");
    if (data.truncated) bits.push("result window truncated");
    return bits.join(" · ");
  }

  function search(q) {
    if (inflight) { inflight.abort(); }
    if (!q) {
      results.hidden = true;
      empty.hidden = true;
      meta.textContent = "";
      clearError();
      return;
    }
    var ctrl = new AbortController();
    inflight = ctrl;
    var url = "api/query?q=" + encodeURIComponent(q) + "&limit=" + LIMIT;
    fetch(url, { method: "GET", credentials: "same-origin", signal: ctrl.signal, headers: { "Accept": "application/json" } })
      .then(function (resp) {
        return resp.json().then(function (data) { return { status: resp.status, data: data }; });
      })
      .then(function (r) {
        if (ctrl !== inflight) return; // superseded
        if (r.status === 200) {
          render(r.data);
        } else {
          var msg = r.data && (r.data.reason || r.data.error) ? (r.data.error + (r.data.reason ? ": " + r.data.reason : "")) : ("query failed (HTTP " + r.status + ")");
          showError(msg);
          if (r.data && r.data.scope) renderScope(r.data.scope, r.data.scope.stale);
        }
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        showError("network error: " + (e && e.message ? e.message : e));
      });
  }

  function loadStatus() {
    fetch("api/status", { method: "GET", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s) return;
        if (typeof s.item_count === "number") {
          indexStatus.textContent = s.item_count.toLocaleString() + " items indexed" +
            (s.index_ready ? "" : " (index not ready)");
        }
        renderScope(s.scope, s.policy_stale);
      })
      .catch(function () { /* status is best-effort */ });
  }

  qInput.addEventListener("input", function () {
    clearTimeout(debounceTimer);
    var q = qInput.value.trim();
    debounceTimer = setTimeout(function () { search(q); }, 180);
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    clearTimeout(debounceTimer);
    search(qInput.value.trim());
  });

  loadStatus();
})();
