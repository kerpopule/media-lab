/* Custom local-only Image Template Collection for Media Lab. */
(function () {
  "use strict";
  if (window.ImageTemplateCollection) return;

  var DATA_URL = "/static/template-library/cases.json";
  var PAGE_SIZE = 36;
  var state = { data: null, filtered: [], shown: 0, selected: null, originalPrompt: "", loading: null };
  var launch = document.getElementById("imageTemplateLaunch");
  if (!launch) return;

  var root = document.createElement("section");
  root.className = "itl";
  root.id = "imageTemplateCollection";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", "itlTitle");
  root.innerHTML =
    '<header class="itl-top">' +
    ' <button class="itl-iconbtn itl-close" type="button" aria-label="Back to Images">←</button>' +
    ' <div class="itl-title"><div class="itl-kicker">Local collection</div><h1 id="itlTitle">Image templates</h1></div>' +
    ' <a class="itl-topnotice" href="/static/template-library/NOTICE.md" target="_blank" rel="noopener">Rights notice</a>' +
    ' <div class="itl-count" id="itlCount">Loading 524…</div>' +
    '</header>' +
    '<div class="itl-body">' +
    ' <aside class="itl-filterbar" aria-label="Template filters">' +
    '  <label class="itl-field"><span>Search</span><input id="itlSearch" type="search" placeholder="Search title, prompt, source…" autocomplete="off"></label>' +
    '  <label class="itl-field"><span>Category</span><select id="itlCategory" aria-label="Category"><option value="">All categories</option></select></label>' +
    '  <label class="itl-field"><span>Style</span><select id="itlStyle" aria-label="Style"><option value="">All styles</option></select></label>' +
    '  <label class="itl-field"><span>Scene</span><select id="itlScene" aria-label="Scene"><option value="">All scenes</option></select></label>' +
    '  <button class="itl-clear" id="itlClear" type="button">Clear filters</button>' +
    '  <div class="itl-warning"><strong>Research & learning collection.</strong> Source repository: MIT. Third-party prompts and previews may have separate rights; commercial use is not guaranteed. Check the original source and obtain rightsholder permission. <a href="/static/template-library/NOTICE.md" target="_blank" rel="noopener">Full notice</a>.</div>' +
    ' </aside>' +
    ' <main class="itl-results" id="itlResults"><div class="itl-grid" id="itlGrid"></div><div class="itl-empty" id="itlEmpty">No templates match those filters.</div><div class="itl-sentinel" id="itlSentinel" aria-hidden="true"></div></main>' +
    '</div>' +
    '<div class="itl-detailshade" id="itlDetailShade"></div>' +
    '<aside class="itl-detail" id="itlDetail" aria-label="Selected template" aria-hidden="true">' +
    ' <header class="itl-detailhead"><div><div class="itl-kicker" id="itlDetailMeta">Selected template</div><h2 id="itlDetailTitle"></h2></div><button class="itl-iconbtn itl-detailclose" type="button" aria-label="Close template detail">×</button></header>' +
    ' <div class="itl-detailbody"><img class="itl-hero" id="itlHero" alt=""><div class="itl-tags" id="itlTags"></div>' +
    '  <label><div class="itl-promptlabel"><span>Editable prompt</span><span id="itlChars"></span></div><textarea class="itl-prompt" id="itlPrompt" maxlength="8192"></textarea></label>' +
    '  <div class="itl-actions"><button class="itl-action" id="itlReset" type="button">Reset prompt</button><button class="itl-action" id="itlCopy" type="button">Copy</button><button class="itl-action chat" id="itlAsk" type="button">💬 Ask chat</button><button class="itl-action" id="itlFilm" type="button">🎬 Film as video</button><button class="itl-action" id="itlImages" type="button" style="display:none">🖼 Use in Images</button><button class="itl-action primary" id="itlUse" type="button">🖼 Use in Images</button></div>' +
    '  <div class="itl-source" id="itlSource"></div>' +
    ' </div>' +
    '</aside>' +
    '<div class="itl-toast" id="itlToast" role="status" aria-live="polite"></div>';
  document.body.appendChild(root);

  var el = function (id) { return document.getElementById(id); };
  var search = el("itlSearch"), category = el("itlCategory"), style = el("itlStyle"), scene = el("itlScene");
  var grid = el("itlGrid"), count = el("itlCount"), empty = el("itlEmpty"), results = el("itlResults");
  var detail = el("itlDetail"), shade = el("itlDetailShade"), promptBox = el("itlPrompt"), toastTimer = null;

  function text(value, cap) { return String(value == null ? "" : value).slice(0, cap || 8192); }
  function safeUrl(value) {
    try { var u = new URL(String(value || ""), location.origin); return /^https?:$/.test(u.protocol) ? u.href : ""; }
    catch (e) { return ""; }
  }
  function toast(message) {
    var node = el("itlToast"); node.textContent = message; node.classList.add("on");
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { node.classList.remove("on"); }, 1500);
  }
  function option(select, value) { var o = document.createElement("option"); o.value = value; o.textContent = value; select.appendChild(o); }
  function fillFilters(data) {
    data.categories.forEach(function (x) { option(category, x); });
    data.styles.forEach(function (x) { option(style, x); });
    data.scenes.forEach(function (x) { option(scene, x); });
  }
  function load() {
    if (state.data) return Promise.resolve(state.data);
    if (state.loading) return state.loading;
    state.loading = fetch(DATA_URL, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("Template collection is unavailable.");
      return r.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.cases) || data.cases.length !== data.totalCases) throw new Error("Template collection is incomplete.");
      state.data = data; fillFilters(data); applyFilters(); return data;
    }).catch(function (err) {
      count.textContent = "Unavailable"; empty.textContent = err.message || "Could not load templates."; empty.classList.add("on");
      throw err;
    });
    return state.loading;
  }
  function searchText(c) {
    return [c.title, c.prompt, c.promptPreview, c.category, c.sourceLabel]
      .concat(c.styles || [], c.scenes || []).join(" ").toLocaleLowerCase();
  }
  function applyFilters() {
    if (!state.data) return;
    var q = search.value.trim().toLocaleLowerCase(), cat = category.value, sty = style.value, scn = scene.value;
    state.filtered = state.data.cases.filter(function (c) {
      return (!q || searchText(c).indexOf(q) !== -1) && (!cat || c.category === cat) &&
        (!sty || (c.styles || []).indexOf(sty) !== -1) && (!scn || (c.scenes || []).indexOf(scn) !== -1);
    });
    state.shown = 0; grid.textContent = ""; empty.classList.toggle("on", state.filtered.length === 0);
    count.textContent = state.filtered.length.toLocaleString() + (state.filtered.length === 1 ? " template" : " templates");
    appendPage(); results.scrollTop = 0;
    if (typeof fillViewport === "function") setTimeout(fillViewport, 0);
  }
  function cardFor(c) {
    var card = document.createElement("button"); card.type = "button"; card.className = "itl-card";
    card.setAttribute("aria-label", "Open template " + text(c.title, 120)); card.dataset.caseId = String(c.id);
    var img = document.createElement("img"); img.loading = "lazy"; img.decoding = "async";
    img.src = text(c.image, 260); img.alt = text(c.imageAlt || c.title, 180);
    var badge = document.createElement("span"); badge.className = "itl-id"; badge.textContent = "#" + c.id;
    var body = document.createElement("span"); body.className = "itl-cardbody";
    var title = document.createElement("span"); title.className = "itl-cardtitle"; title.textContent = text(c.title, 160);
    var meta = document.createElement("span"); meta.className = "itl-cardmeta"; meta.textContent = text(c.category, 100);
    body.appendChild(title); body.appendChild(meta); card.appendChild(img); card.appendChild(badge); card.appendChild(body);
    card.addEventListener("click", function () { selectCase(c); }); return card;
  }
  function appendPage() {
    if (state.shown >= state.filtered.length) return;
    var end = Math.min(state.shown + PAGE_SIZE, state.filtered.length), frag = document.createDocumentFragment();
    for (var i = state.shown; i < end; i++) frag.appendChild(cardFor(state.filtered[i]));
    grid.appendChild(frag); state.shown = end;
  }
  function tag(label) { var x = document.createElement("span"); x.className = "itl-tag"; x.textContent = label; return x; }
  function sourceLink(parent, label, href) {
    var url = safeUrl(href); if (!url) return;
    if (parent.childNodes.length) parent.appendChild(document.createTextNode(" · "));
    var a = document.createElement("a"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = label; parent.appendChild(a);
  }
  function selectCase(c) {
    state.selected = c; state.originalPrompt = text(c.prompt, 8192);
    el("itlDetailMeta").textContent = "Template #" + c.id + " · " + text(c.category, 100);
    el("itlDetailTitle").textContent = text(c.title, 180); el("itlHero").src = text(c.image, 260);
    el("itlHero").alt = text(c.imageAlt || c.title, 180); promptBox.value = state.originalPrompt; updateChars();
    var tags = el("itlTags"); tags.textContent = "";
    (c.styles || []).concat(c.scenes || []).slice(0, 16).forEach(function (x) { tags.appendChild(tag(text(x, 60))); });
    var source = el("itlSource"); source.textContent = "Attribution: " + text(c.sourceLabel || "Not supplied", 200) + ". ";
    sourceLink(source, "Original source", c.sourceUrl); sourceLink(source, "Source case record", c.githubUrl); sourceLink(source, "Related post", c.relatedUrl);
    source.appendChild(document.createElement("br")); source.appendChild(document.createTextNode("Research/learning reference; commercial rights are not guaranteed. Verify with the original rightsholder."));
    detail.classList.add("on"); shade.classList.add("on"); detail.setAttribute("aria-hidden", "false");
    setTimeout(function () { promptBox.focus(); }, 80);
  }
  function closeDetail() { detail.classList.remove("on"); shade.classList.remove("on"); detail.setAttribute("aria-hidden", "true"); }
  function updateChars() { el("itlChars").textContent = promptBox.value.length.toLocaleString() + " / 8,192"; }
  function selectedContext() {
    var c = state.selected || {};
    return { id: c.id, title: text(c.title, 160), category: text(c.category, 100), styles: (c.styles || []).slice(0, 8),
      scenes: (c.scenes || []).slice(0, 8), prompt: text(promptBox.value, 8192), source_label: text(c.sourceLabel, 200),
      source_url: text(c.sourceUrl, 500), github_url: text(c.githubUrl, 500), related_url: text(c.relatedUrl, 500),
      workflow: c.workflow || null, rights_note: text(c.rightsNote, 500), image: text(c.image, 260) };
  }
  function copyPrompt() {
    var value = promptBox.value;
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).then(function () { toast("Prompt copied"); }, function () { toast("Could not copy"); });
    else { promptBox.select(); try { document.execCommand("copy"); toast("Prompt copied"); } catch (e) { toast("Could not copy"); } }
  }
  function sendPromptTo(inputId, tabId) {
    var target = document.getElementById(inputId); if (!target) return;
    target.value = promptBox.value; target.dispatchEvent(new Event("input", { bubbles: true }));
    closeDetail(); closeCollection();
    if (typeof window.showTab === "function") window.showTab(tabId);
    else if (typeof showTab === "function") showTab(tabId);
    target.scrollIntoView({ behavior: "smooth", block: "center" }); target.focus();
  }
  /* every template works everywhere: the same prompt paints a still, films a
     take, seeds a music video, or opens a storyboard brief. The explorer knows
     which page launched it and the primary button follows. */
  var DEST = {
    images: { input: "i_prompt",   tab: "tab-images", label: "\ud83d\uddbc Use in Images" },
    video:  { input: "prompt",     tab: "tab-video",  label: "\ud83c\udfac Use in Video" },
    mv:     { input: "mv_concept", tab: "tab-music",  label: "\ud83c\udfb5 Use in Music video" },
    board:  { input: "s_idea",     tab: "tab-board",  label: "\ud83c\udf9e Use in Storyboard" },
  };
  state.target = "images";
  function useTemplate() { var d = DEST[state.target] || DEST.images; sendPromptTo(d.input, d.tab); }
  function filmTemplate() { sendPromptTo("prompt", "tab-video"); }
  function imagesTemplate() { sendPromptTo("i_prompt", "tab-images"); }
  function paintActions() {
    var d = DEST[state.target] || DEST.images;
    el("itlUse").textContent = d.label;
    el("itlFilm").style.display = state.target === "video" ? "none" : "";
    el("itlImages").style.display = state.target === "images" ? "none" : "";
  }
  function askChat() {
    var draft = "Help me adapt this selected image template for my idea. Ask what I want to change, then return a polished image prompt.";
    var context = selectedContext(); closeDetail(); closeCollection();
    if (typeof window.showTab === "function") window.showTab("tab-images");
    else if (typeof showTab === "function") showTab("tab-images");
    if (window.MediaLabChat && typeof window.MediaLabChat.openWithTemplate === "function") window.MediaLabChat.openWithTemplate(context, draft);
    else window.dispatchEvent(new CustomEvent("medialab:ask-image-template", { detail: { template: context, draft: draft } }));
  }
  function openCollection(target) {
    state.target = DEST[target] ? target : "images";
    paintActions();
    root.hidden = false; document.documentElement.classList.add("itl-lock");
    load().then(function () { search.focus(); }).catch(function () {});
  }
  function closeCollection() { root.hidden = true; document.documentElement.classList.remove("itl-lock"); closeDetail(); launch.focus(); }

  launch.addEventListener("click", openCollection); root.querySelector(".itl-close").addEventListener("click", closeCollection);
  root.querySelector(".itl-detailclose").addEventListener("click", closeDetail); shade.addEventListener("click", closeDetail);
  [search, category, style, scene].forEach(function (node) { node.addEventListener(node === search ? "input" : "change", applyFilters); });
  el("itlClear").addEventListener("click", function () { search.value = category.value = style.value = scene.value = ""; applyFilters(); });
  el("itlReset").addEventListener("click", function () { promptBox.value = state.originalPrompt; updateChars(); toast("Original prompt restored"); });
  el("itlCopy").addEventListener("click", copyPrompt); el("itlUse").addEventListener("click", useTemplate); el("itlFilm").addEventListener("click", filmTemplate); el("itlImages").addEventListener("click", imagesTemplate); el("itlAsk").addEventListener("click", askChat);
  promptBox.addEventListener("input", updateChars);
  document.addEventListener("keydown", function (event) { if (event.key !== "Escape" || root.hidden) return; detail.classList.contains("on") ? closeDetail() : closeCollection(); });
  /* Paging is belt-and-braces: the old observer-only path silently stalled at
     one page (36 cards) — an observer only re-fires on intersection CHANGES,
     and entries[0] can be a stale non-intersecting record, so page 2 never
     came. A plain scroll listener always works; the observer stays as a bonus;
     fillViewport tops the grid up whenever a page leaves the pane short. */
  function nearBottom() { return results.scrollTop + results.clientHeight > results.scrollHeight - 700; }
  function fillViewport() {
    var guard = 0;
    while (state.shown < state.filtered.length && guard++ < 20 &&
           (results.scrollHeight <= results.clientHeight + 700 || nearBottom())) appendPage();
  }
  results.addEventListener("scroll", function () { if (nearBottom()) fillViewport(); });
  if ("IntersectionObserver" in window)
    new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) fillViewport();
    }, { root: results, rootMargin: "500px" }).observe(el("itlSentinel"));

  window.ImageTemplateCollection = { open: function (t) { openCollection(typeof t === "string" ? t : "images"); }, close: closeCollection };
})();
