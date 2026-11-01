/* Media Lab guide — floating chat widget (NOIR system).
   Self-contained: injects its own styles; works on the Lab and, via the
   :7864 injecting proxy, over the Maestro GUI. Config (set before load):
     window.MEDIALAB_API          — API origin ('' = same origin)
     window.MEDIALAB_CHAT_BOTTOM  — fab offset from bottom in px (default 18) */
(function () {
  "use strict";
  if (window.__mlchat) return;
  window.__mlchat = true;

  var API = window.MEDIALAB_API || "";
  var BOTTOM = window.MEDIALAB_CHAT_BOTTOM || 18;
  var msgs = [];
  var busy = false;
  var selectedImageTemplate = null;
  var selectedProject = null;
  try { msgs = JSON.parse(localStorage.getItem("mlchat") || "[]").slice(-20); } catch (e) {}

  var css = [
    ".mlc{font-family:'Barlow',-apple-system,system-ui,sans-serif;font-weight:300}",
    ".mlc *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}",
    ".mlc-fab{position:fixed;right:16px;bottom:" + BOTTOM + "px;z-index:80;width:54px;height:54px;",
    " border-radius:50%;border:1px solid var(--line,rgba(255,255,255,.16));cursor:pointer;font-size:1.45rem;",
    " background:linear-gradient(140deg,var(--gold-hi,#E8C193),var(--gold,#C99A6A) 55%,var(--gold-2,#A97B4E));color:var(--on-accent,#160D06);",
    " box-shadow:0 18px 36px -14px rgba(0,0,0,.72),0 4px 12px -6px rgba(0,0,0,.44)}",
    ".mlc-panel{position:fixed;right:16px;bottom:" + (BOTTOM + 64) + "px;z-index:81;width:min(380px,calc(100vw - 32px));",
    " height:min(560px,calc(100vh - " + (BOTTOM + 84) + "px));display:none;flex-direction:column;overflow:hidden;",
    " background:var(--panel,rgba(14,10,7,.94));backdrop-filter:blur(20px) saturate(118%);-webkit-backdrop-filter:blur(20px) saturate(118%);",
    " border:1px solid var(--line,rgba(255,255,255,.12));border-radius:20px;",
    " box-shadow:0 34px 64px -22px rgba(0,0,0,.88),inset 0 1px 0 rgba(255,255,255,.17);color:var(--t-1,rgba(255,255,255,.95))}",
    ".mlc.open .mlc-panel{display:flex}",
    "@media (max-width:640px){.mlc-panel{right:0;bottom:0;left:0;top:0;width:100vw;height:100dvh;",
    " border-radius:0;border:0;padding-top:env(safe-area-inset-top)}",
    " .mlc-form{padding-bottom:calc(12px + env(safe-area-inset-bottom))}}",
    "html.mlc-lock,html.mlc-lock body{overflow:hidden!important;overscroll-behavior:none;touch-action:none}",
    ".mlc-panel{touch-action:auto}",
    ".mlc-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;",
    " border-bottom:1px solid var(--line,rgba(255,255,255,.09))}",
    ".mlc-context{display:none;align-items:center;gap:9px;padding:9px 12px;border-bottom:1px solid var(--line,rgba(255,255,255,.09));",
    " background:color-mix(in srgb,var(--gold,#C99A6A) 10%,transparent);font-size:.75rem;color:var(--t-2,rgba(255,255,255,.88))}",
    ".mlc-context.on{display:flex}.mlc-context strong{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}",
    ".mlc-context button{border:0;background:none;color:var(--gold-hi,#E8C193);cursor:pointer;font:inherit;padding:3px 5px}",
    ".mlc-title{font-family:'Space Grotesk',system-ui,sans-serif;font-weight:600;letter-spacing:-0.01em;font-size:1.1rem}",
    ".mlc-sub{font-size:.6875rem;letter-spacing:.14em;text-transform:uppercase;color:var(--gold,#C99A6A);font-weight:500}",
    ".mlc-x{background:none;border:0;color:var(--t-3,rgba(255,255,255,.76));font-size:1.5rem;cursor:pointer;line-height:1;padding:4px 8px}",
    ".mlc-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}",
    ".mlc-b{max-width:86%;padding:10px 13px;border-radius:15px;font-size:.9rem;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}",
    ".mlc-me{align-self:flex-end;background:linear-gradient(140deg,var(--gold,#C99A6A),var(--gold-2,#A97B4E));color:var(--on-accent,#160D06);font-weight:400;border-bottom-right-radius:5px}",
    ".mlc-bot{align-self:flex-start;background:var(--field,rgba(255,255,255,.055));border:1px solid var(--line,rgba(255,255,255,.1));",
    " border-bottom-left-radius:5px;cursor:copy}",
    ".mlc-bot.typing::after{content:'…';animation:mlcp 1.1s infinite}",
    "@keyframes mlcp{50%{opacity:.25}}",
    ".mlc-q{display:block;background:color-mix(in srgb,var(--gold,#C99A6A) 12%,transparent);border:1px solid color-mix(in srgb,var(--gold,#C99A6A) 38%,transparent);border-radius:10px;",
    " padding:9px 11px;margin:8px 0 2px;font-size:.875rem;color:var(--gold-hi,#E8C193)}",
    ".mlc-use{display:inline-block;margin-top:7px;border:1px solid color-mix(in srgb,var(--gold,#C99A6A) 50%,transparent);background:none;color:var(--gold-hi,#E8C193);",
    " border-radius:999px;padding:4px 12px;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;font-family:inherit}",
    ".mlc-form{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line,rgba(255,255,255,.09))}",
    ".mlc-in{flex:1;background:var(--field-strong,rgba(10,7,5,.62));border:1px solid var(--line,rgba(255,255,255,.12));border-radius:12px;",
    " color:var(--t-1,rgba(255,255,255,.95));font-family:inherit;font-size:1rem;padding:11px 13px;outline:none}",
    ".mlc-in:focus{border-color:var(--gold,#C99A6A)}",
    ".mlc-send{border:0;border-radius:12px;padding:0 16px;cursor:pointer;font-size:1.1rem;",
    " background:linear-gradient(140deg,var(--gold-hi,#E8C193),var(--gold,#C99A6A) 55%,var(--gold-2,#A97B4E));color:var(--on-accent,#160D06)}",
    ".mlc-send:disabled{opacity:.45}",
    ".mlc-toast{position:fixed;left:50%;transform:translateX(-50%);bottom:" + (BOTTOM + 70) + "px;z-index:99;",
    " background:var(--panel,rgba(10,7,5,.92));border:1px solid color-mix(in srgb,var(--gold,#C99A6A) 50%,transparent);color:var(--gold-hi,#E8C193);border-radius:999px;",
    " padding:8px 18px;font-size:.8125rem;letter-spacing:.06em;opacity:0;transition:.25s;pointer-events:none}",
    ".mlc-toast.on{opacity:1}",
    ".mlc-hint{font-size:.6875rem;color:var(--t-3,rgba(255,255,255,.5));text-align:center;padding:0 14px 10px}",
  ].join("");
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.className = "mlc";
  root.innerHTML =
    '<button class="mlc-fab" type="button" aria-label="Chat with the Media Lab guide">💬</button>' +
    '<section class="mlc-panel" aria-label="Media Lab guide">' +
    '  <div class="mlc-head"><div><div class="mlc-sub">VibeX Studio</div>' +
    '    <div class="mlc-title">Media Lab</div></div>' +
    '    <button class="mlc-x" aria-label="Close">&times;</button></div>' +
    '  <div class="mlc-context"><span aria-hidden="true">🖼</span><strong></strong><button type="button" aria-label="Remove selected image template">Clear</button></div>' +
    '  <div class="mlc-context mlc-project"><span aria-hidden="true">📁</span><strong></strong><button type="button" aria-label="Remove selected project">Clear</button></div>' +
    '  <div class="mlc-log" role="log" aria-live="polite"></div>' +
    '  <div class="mlc-hint">Tap any reply to copy it</div>' +
    '  <form class="mlc-form"><input class="mlc-in" type="text" autocomplete="off"' +
    '    placeholder="Ask how to make something…" aria-label="Message">' +
    '  <button class="mlc-send" type="submit" aria-label="Send">➤</button></form>' +
    "</section>" +
    '<div class="mlc-toast">Copied ✓</div>';
  document.body.appendChild(root);

  var fab = root.querySelector(".mlc-fab");
  var panel = root.querySelector(".mlc-panel");
  var log = root.querySelector(".mlc-log");
  var form = root.querySelector(".mlc-form");
  var input = root.querySelector(".mlc-in");
  var sendBtn = root.querySelector(".mlc-send");
  var toastEl = root.querySelector(".mlc-toast");
  var contextEl = root.querySelector(".mlc-context");
  var projectEl = root.querySelector(".mlc-project");
  var toastT = null;

  function boundedText(value, cap) { return String(value == null ? "" : value).slice(0, cap); }
  function templateContext(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
      id: Number.isInteger(raw.id) ? raw.id : 0,
      title: boundedText(raw.title, 200),
      category: boundedText(raw.category, 120),
      styles: Array.isArray(raw.styles) ? raw.styles.slice(0, 8).map(function (x) { return boundedText(x, 60); }) : [],
      scenes: Array.isArray(raw.scenes) ? raw.scenes.slice(0, 8).map(function (x) { return boundedText(x, 60); }) : [],
      prompt: boundedText(raw.prompt, 8192),
      source_label: boundedText(raw.source_label, 240),
      source_url: boundedText(raw.source_url, 600),
      github_url: boundedText(raw.github_url, 600),
      image: boundedText(raw.image, 320),
    };
  }
  function setSelectedTemplate(raw) {
    selectedImageTemplate = templateContext(raw);
    contextEl.classList.toggle("on", !!selectedImageTemplate);
    contextEl.querySelector("strong").textContent = selectedImageTemplate ? "Template #" + selectedImageTemplate.id + " · " + selectedImageTemplate.title : "";
  }
  contextEl.querySelector("button").addEventListener("click", function () { setSelectedTemplate(null); });

  function projectContext(raw) {
    if (!raw || raw.version !== 1 || typeof raw.projectId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(raw.projectId) ||
        typeof raw.title !== "string" || !raw.title.length || raw.title.length > 160 || !Array.isArray(raw.assets) || raw.assets.length > 32) return null;
    var paths = new Set();
    var assets = [];
    for (var asset of raw.assets) {
      if (!asset || typeof asset.path !== "string" || !asset.path.length || asset.path.length > 512 ||
          /[\\:\x00-\x1f\x7f]/.test(asset.path) || asset.path.split("/").some(function (part) {return !part || part === "." || part === "..";}) ||
          ["image", "video", "audio", "model", "sprites"].indexOf(asset.kind) < 0 || paths.has(asset.path)) return null;
      paths.add(asset.path);
      assets.push({path:asset.path, kind:asset.kind});
    }
    var value = {version:1, projectId:raw.projectId, title:raw.title, assets:assets};
    // Match the server's ASCII-escaped payload bound. File contents are never copied.
    if (JSON.stringify(value).replace(/[\u007f-\uffff]/g, function (ch) {return "\\u" + ch.charCodeAt(0).toString(16).padStart(4,"0");}).length > 32768) return null;
    return value;
  }
  function setSelectedProject(raw) {
    selectedProject = projectContext(raw);
    projectEl.classList.toggle("on", !!selectedProject);
    projectEl.querySelector("strong").textContent = selectedProject ? selectedProject.title + " · " + selectedProject.assets.length + (selectedProject.assets.length === 1 ? " asset" : " assets") : "";
    return !!selectedProject;
  }
  projectEl.querySelector("button").addEventListener("click", function () {setSelectedProject(null);});

  function toast(t) {
    toastEl.textContent = t || "Copied ✓";
    toastEl.classList.add("on");
    clearTimeout(toastT);
    toastT = setTimeout(function () { toastEl.classList.remove("on"); }, 1400);
  }
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { toast("Copied ✓"); },
        function () { toast("Could not copy"); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = t; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast("Copied ✓"); } catch (e) { toast("Could not copy"); }
      ta.remove();
    }
  }

  /* Render a finished assistant reply: fenced blocks become highlighted
     prompt cards with a "Use this" button; tapping the bubble copies all. */
  function renderBot(el, text) {
    el.textContent = "";
    var re = /```[a-zA-Z]*\n?([\s\S]*?)```/g;
    var last = 0, m;
    function addText(t) {
      if (!t) return;
      // minimal markdown: **label** becomes a bold span (the guide labels
      // multi-box prompts this way); everything else stays plain text
      var parts = t.split(/\*\*([^*\n]{1,60})\*\*/g);
      for (var i = 0; i < parts.length; i++) {
        if (!parts[i]) continue;
        if (i % 2) {
          var b = document.createElement("b");
          b.textContent = parts[i];
          el.appendChild(b);
        } else {
          el.appendChild(document.createTextNode(parts[i]));
        }
      }
    }
    while ((m = re.exec(text))) {
      addText(text.slice(last, m.index).replace(/\n{3,}/g, "\n\n"));
      var q = document.createElement("span");
      q.className = "mlc-q";
      q.textContent = m[1].trim();
      var use = document.createElement("button");
      use.className = "mlc-use";
      use.type = "button";
      use.textContent = "Use this";
      (function (promptText) {
        use.addEventListener("click", function (e) {
          e.stopPropagation();
          copyText(promptText);
        });
      })(m[1].trim());
      q.appendChild(document.createElement("br"));
      q.appendChild(use);
      el.appendChild(q);
      last = re.lastIndex;
    }
    addText(text.slice(last).replace(/\n{3,}/g, "\n\n"));
    el.dataset.full = text;
  }

  function bubble(role, text) {
    var el = document.createElement("div");
    el.className = "mlc-b mlc-" + (role === "user" ? "me" : "bot");
    el.textContent = text || "";
    if (role !== "user") {
      el.dataset.full = text || "";
      el.addEventListener("click", function () {
        if (el.dataset.full) copyText(el.dataset.full);
      });
    }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function persist() {
    try { localStorage.setItem("mlchat", JSON.stringify(msgs.slice(-20))); } catch (e) {}
  }

  var opened = false;
  var mobileQ = window.matchMedia("(max-width:640px)");
  function open() {
    root.classList.add("open");
    if (mobileQ.matches) document.documentElement.classList.add("mlc-lock");
    if (!opened) {
      opened = true;
      if (msgs.length) {
        msgs.forEach(function (m) {
          var el = bubble(m.role, m.content);
          if (m.role !== "user") renderBot(el, m.content);
        });
      } else {
        var greet = "Welcome to Media Lab — your studio, running entirely on its own hardware. " +
          "I'm the operative producer: tell me what you want to make or test and I can inspect the real studio, " +
          "select current characters and songs, queue a bounded take, and report its real job status. " +
          "Tip: install me to your Home Screen for the full app — on iPhone that also unlocks finish notifications.";
        renderBot(bubble("assistant", ""), greet);
      }
    }
    if (window.matchMedia("(min-width:641px)").matches) setTimeout(function () { input.focus(); }, 60);
  }
  function close() {
    root.classList.remove("open");
    document.documentElement.classList.remove("mlc-lock");
  }
  fab.addEventListener("click", function () {
    root.classList.contains("open") ? close() : open();
  });
  root.querySelector(".mlc-x").addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    bubble("user", text);
    msgs.push({ role: "user", content: text });
    persist();
    send();
  });

  function send() {
    busy = true;
    sendBtn.disabled = true;
    var out = bubble("assistant", "");
    out.classList.add("typing");
    var acc = "";
    var payload = { messages: msgs.slice(-20) };
    if (selectedImageTemplate) payload.selected_image_template = selectedImageTemplate;
    if (selectedProject) payload.selected_project = selectedProject;
    fetch(API + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error("chat unavailable");
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) return;
          buf += dec.decode(res.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(function (p) {
            var line = p.trim();
            if (line.indexOf("data:") !== 0) return;
            var obj;
            try { obj = JSON.parse(line.slice(5).trim()); } catch (e) { return; }
            if (obj.delta) {
              acc += obj.delta;
              out.classList.remove("typing");
              out.textContent = acc;
              log.scrollTop = log.scrollHeight;
            } else if (obj.receipt) {
              var rr = obj.receipt || {};
              var names = (rr.cast_names || []).join(", ") || "none";
              var block = "ACTION RECEIPT\n" +
                "Tool: " + (rr.tool || "unknown") + "\n" +
                "Decision: " + (rr.accepted ? "ACCEPTED" : "REJECTED") + "\n";
              if (rr.job_id) block += "Job: " + rr.job_id + "\n";
              if (rr.model) block += "Model: " + rr.model + "\n";
              if (rr.cast_names) block += "Cast: " + names + "\n";
              block += "Status: " + (rr.status || (rr.accepted ? "accepted" : "rejected")) + "\n";
              if (rr.queue_url) block += "Queue link: " + rr.queue_url + "\n";
              if (rr.error) block += "Reason: " + rr.error + "\n";
              acc += (acc ? "\n\n" : "") + block.trim() + "\n\n";
              out.classList.remove("typing");
              out.textContent = acc;
              log.scrollTop = log.scrollHeight;
            } else if (obj.status) {
              // transient progress line — replaced by the real reply, never
              // accumulated into it
              if (!acc) { out.textContent = obj.status; out.classList.add("typing"); }
            } else if (obj.error) {
              throw new Error(obj.error);
            }
          });
          return pump();
        });
      }
      return pump();
    }).then(function () {
      finishReply();
    }).catch(function () {
      acc = acc || "I couldn't reach the studio brain just now — it may be busy rendering. Try again in a moment.";
      finishReply();
    });
    function finishReply() {
      out.classList.remove("typing");
      renderBot(out, acc);
      msgs.push({ role: "assistant", content: acc });
      persist();
      busy = false;
      sendBtn.disabled = false;
      log.scrollTop = log.scrollHeight;
    }
  }

  function openWithTemplate(raw, draft) {
    setSelectedTemplate(raw);
    input.value = boundedText(draft || "Help me adjust this selected image template.", 1000);
    open();
    setTimeout(function () { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 80);
  }
  window.MediaLabChat = {
    open: open,
    setSelectedProject: setSelectedProject,
    clearSelectedProject: function () {setSelectedProject(null);},
    getSelectedProject: function () {return selectedProject ? JSON.parse(JSON.stringify(selectedProject)) : null;},
    close: close,
    openWithTemplate: openWithTemplate,
    clearSelectedTemplate: function () { setSelectedTemplate(null); },
    getSelectedTemplate: function () { return selectedImageTemplate; },
  };
  window.addEventListener("medialab:ask-image-template", function (event) {
    var detail = event.detail || {};
    openWithTemplate(detail.template, detail.draft);
  });
})();
