const $ = (sel) => document.querySelector(sel);
const grid = $("#grid");
const search = $("#search");
const sortSel = $("#sort");
const tagBar = $("#tags");
const empty = $("#empty");
const drop = $("#drop");

let state = { gifs: [], tags: [], root: "" };
let activeTags = new Set();
let capabilities = { ocr: false, enrich: false, ffmpeg: false };

// ---------------------------------------------------------------- clipboard

// A copy puts two flavours on the clipboard: text/html referencing the GIF,
// which rich targets embed and keep animated, and an image/png still as the
// fallback. image/gif cannot go on as an image (ClipboardItem.supports reports
// false for it), which is why the PNG is a single frame.
function pngFromCanvas(width, height, draw) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  draw(canvas.getContext("2d"));
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

async function toPng(url, imgEl) {
  // The card's <img> is already decoded in memory, so rasterise that rather
  // than asking the server for bytes the browser is holding. Copying then
  // works even if the server has stopped.
  if (imgEl && imgEl.naturalWidth) {
    return pngFromCanvas(imgEl.naturalWidth, imgEl.naturalHeight, (ctx) =>
      ctx.drawImage(imgEl, 0, 0),
    );
  }
  let res;
  try {
    res = await fetch(url);
  } catch {
    // A bare "Failed to fetch" tells the user nothing. The usual cause is a
    // stopped server behind a page still open in a tab.
    throw new Error("gifhole's server is not responding. Is it still running?");
  }
  if (!res.ok) throw new Error(`server returned ${res.status} for that GIF`);
  const bitmap = await createImageBitmap(await res.blob());
  return pngFromCanvas(bitmap.width, bitmap.height, (ctx) => ctx.drawImage(bitmap, 0, 0));
}

const escapeAttr = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");

async function copyImage(gif, imgEl) {
  // Two flavours on one clipboard entry. ClipboardItem.supports("image/gif")
  // is false everywhere, so an animated GIF cannot go on as an image. But
  // text/html is supported, and a rich paste target (mail, chat, docs) will
  // pull the GIF in from the URL and keep it moving. Image-only targets fall
  // back to the PNG still.
  const png = toPng(gif.url, imgEl);
  const html = new Blob([`<img src="${escapeAttr(location.origin + gif.url)}" alt="">`], {
    type: "text/html",
  });
  const flavours = { "text/html": html, "image/png": png };
  try {
    // Passing the promise keeps Safari's user-gesture window open.
    await navigator.clipboard.write([new ClipboardItem(flavours)]);
  } catch {
    await navigator.clipboard.write([
      new ClipboardItem({ ...flavours, "image/png": await png }),
    ]);
  }
  return "copied (animated where the target supports it)";
}

async function copyText(text) {
  await navigator.clipboard.writeText(text);
  return text;
}

// The browser can only put a still PNG on the clipboard, so a paste into
// Discord or Slack loses the animation. The local server can hand over the
// actual file instead, which those apps upload as-is and keep moving.
async function copyFileViaServer(gif) {
  const res = await fetch(`/api/gifs/${gif.id}/clipboard`, { method: "POST" });
  if (!res.ok) throw new Error((await res.json()).detail || `server said ${res.status}`);
  return "copied the GIF file, animation intact";
}

async function handleClick(event, gif, card) {
  event.preventDefault();
  try {
    let what;
    if (event.shiftKey) what = await copyText(location.origin + gif.url);
    else if (event.altKey || event.metaKey) what = await copyText(`${state.root}/${gif.filename}`);
    else if (capabilities.file_clipboard) what = await copyFileViaServer(gif);
    else what = await copyImage(gif, card.querySelector('img'));
    toast(what);
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 500);
    fetch(`/api/gifs/${gif.id}/copied`, { method: "POST" });
  } catch (err) {
    toast(`copy failed: ${err.message}`);
  }
}

// ---------------------------------------------------------------- rendering

function card(gif) {
  const el = document.createElement("article");
  el.className = "card";
  // Static template only -- every value below is set as text/property so a
  // filename or tag can never be parsed as markup.
  el.innerHTML = `
    <figure><img alt="" loading="lazy"></figure>
    <div class="meta">
      <span class="name" contenteditable="plaintext-only" spellcheck="false"></span>
      <span class="dims"></span>
      <button class="del" title="move to trash">x</button>
    </div>
    <div class="rowtags">
      <span class="t"></span><button class="edit" title="edit tags">tags</button>
    </div>
    <div class="ocr">
      <span class="quote"></span>
      <button class="describe" title="describe with Claude">describe</button>
    </div>`;

  el.querySelector("img").src = gif.url;
  el.querySelector(".dims").textContent = `${gif.width}x${gif.height}`;

  // OCR text and Claude descriptions are both search keys; show whichever
  // exists so it's obvious why a GIF matched a query.
  const quote = el.querySelector(".quote");
  if (gif.ocr_text) quote.textContent = `“${gif.ocr_text}”`;
  else if (gif.description) quote.textContent = gif.description;
  else quote.textContent = gif.ocr_at ? "no text found" : "";

  const describe = el.querySelector(".describe");
  describe.disabled = !capabilities.enrich;
  if (!capabilities.enrich) describe.title = capabilities.enrich_reason || "unavailable";
  if (gif.description && gif.ocr_text) describe.textContent = "redescribe";
  describe.addEventListener("click", async () => {
    describe.disabled = true;
    const res = await fetch(`/api/gifs/${gif.id}/enrich`, { method: "POST" });
    if (res.ok) toast("describing…");
    else toast(`describe failed: ${(await res.json()).detail || res.status}`);
    pollJobs();
  });

  const name = el.querySelector(".name");
  name.textContent = gif.title || gif.filename.replace(/\.gif$/, "");
  name.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); name.blur(); }
  });
  name.addEventListener("blur", () => patch(gif.id, { title: name.textContent.trim() }));

  // Edited in place rather than via prompt(): suppressed dialogs would leave
  // tagging with no working affordance at all.
  const tagsEl = el.querySelector(".t");
  tagsEl.textContent = gif.tags.length ? gif.tags.join(" ") : "untagged";
  el.querySelector(".edit").addEventListener("click", () => {
    tagsEl.contentEditable = "plaintext-only";
    tagsEl.textContent = gif.tags.join(" ");
    tagsEl.focus();
    const range = document.createRange();
    range.selectNodeContents(tagsEl);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  });
  // Enter saves directly rather than by triggering blur. Leaning on blur alone
  // is fragile: the element can lose focus without the handler running, which
  // silently drops the edit.
  const saveTags = async () => {
    if (tagsEl.contentEditable !== "plaintext-only") return;
    tagsEl.contentEditable = "false";
    await patch(gif.id, { tags: tagsEl.textContent.trim() });
    load();
  };
  tagsEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveTags(); }
    if (e.key === "Escape") { tagsEl.contentEditable = "false"; load(); }
  });
  tagsEl.addEventListener("blur", saveTags);
  el.querySelector(".del").addEventListener("click", async () => {
    if (!confirm(`Move ${gif.filename} to .trash?`)) return;
    await fetch(`/api/gifs/${gif.id}`, { method: "DELETE" });
    toast("moved to .trash");
    load();
  });

  el.querySelector("figure").addEventListener("click", (e) => handleClick(e, gif, el));
  return el;
}

function renderTags() {
  tagBar.replaceChildren(
    ...state.tags.map(({ tag, count }) => {
      const b = document.createElement("button");
      b.className = "tag";
      b.setAttribute("aria-pressed", activeTags.has(tag));
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = count;
      b.append(tag, n);
      b.addEventListener("click", () => {
        activeTags.has(tag) ? activeTags.delete(tag) : activeTags.add(tag);
        load();
      });
      return b;
    }),
  );
}

function render() {
  grid.replaceChildren(...state.gifs.map(card));
  empty.hidden = state.gifs.length > 0;
  empty.textContent = state.gifs.length
    ? ""
    : search.value || activeTags.size
      ? "nothing matches"
      : `no GIFs yet. Drop some here, or put them in ${state.root_display || state.root}`;
  renderTags();
}

async function load() {
  const q = [search.value, ...activeTags].join(" ").trim();
  const params = new URLSearchParams({ q, sort: sortSel.value });
  // Capabilities gate the per-card "describe" button, so make sure they're
  // known before the first render rather than racing the job poll.
  const [gifs] = await Promise.all([
    fetch(`/api/gifs?${params}`).then((r) => r.json()),
    capsLoaded,
  ]);
  state = gifs;
  render();
}

let resolveCaps;
const capsLoaded = new Promise((r) => (resolveCaps = r));

function patch(id, body) {
  return fetch(`/api/gifs/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------- uploading

async function upload(files) {
  const gifs = [...files].filter((f) => f.type === "image/gif" || f.name.endsWith(".gif"));
  if (!gifs.length) return toast("no GIFs in that drop");
  let ok = 0;
  for (const file of gifs) {
    const body = new FormData();
    body.append("file", file);
    const res = await fetch("/api/gifs", { method: "POST", body });
    if (res.ok) ok += 1;
  }
  toast(`added ${ok} of ${gifs.length}`);
  load();
}

// dragenter/dragleave pairs are easy to lose (a drag that ends outside the
// window never fires leave), so the overlay is purely decorative: it ignores
// pointer events and self-clears on a timer rather than on balanced counts.
let dragTimer;
function showDrop() {
  drop.hidden = false;
  clearTimeout(dragTimer);
  dragTimer = setTimeout(() => (drop.hidden = true), 300);
}
addEventListener("dragenter", (e) => { e.preventDefault(); showDrop(); });
addEventListener("dragover", (e) => { e.preventDefault(); showDrop(); });
addEventListener("dragend", () => (drop.hidden = true));
addEventListener("drop", (e) => {
  e.preventDefault();
  clearTimeout(dragTimer);
  drop.hidden = true;
  upload(e.dataTransfer.files);
});

// ---------------------------------------------------------------- grabbing

// A page can hold hundreds of GIFs, so discovery and import are separate: we
// list what's there, let you pick, then download only the ticked ones.
async function grab(url) {
  if (!url) return;
  toast("looking…");
  const res = await fetch("/api/fetch/discover", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    toast(`grab failed: ${(await res.json()).detail || res.status}`);
    return;
  }
  const { kind, candidates } = await res.json();
  // A direct link is unambiguous; no point making you tick one box.
  if (kind === "direct") return importUrls(candidates);
  openPicker(candidates);
}

async function importUrls(candidates) {
  const titles = {};
  candidates.forEach((c) => {
    if (c.title) titles[c.url] = c.title;
  });
  const res = await fetch("/api/fetch/import", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ urls: candidates.map((c) => c.url), titles }),
  });
  if (!res.ok) {
    toast(`import failed: ${(await res.json()).detail || res.status}`);
    return;
  }
  toast(`importing ${candidates.length}…`);
  pollJobs();
}

// ---------------------------------------------------------------- picker

const picker = $("#picker");
const pickGrid = $("#pickgrid");
const pickCount = $("#pickcount");
let pickState = [];

function refreshPickCount() {
  const on = pickState.filter((p) => p.on).length;
  pickCount.textContent = `${on} of ${pickState.length} selected`;
  $("#pickgo").disabled = on === 0;
}

function openPicker(candidates) {
  // Everything starts ticked: a page URL means the whole page unless you say
  // otherwise. "select none" is one click away.
  pickState = candidates.map((c) => ({ ...c, on: true }));

  pickGrid.replaceChildren(
    ...pickState.map((item, i) => {
      const cell = document.createElement("article");
      cell.className = "pick on";

      const fig = document.createElement("figure");
      // Load straight from the source. The scraped URLs carry live signatures
      // and display fine, which costs the server no bandwidth. Those signatures
      // do expire, so fall back to the proxy if one stops working.
      let media;
      if (item.kind === "video") {
        media = document.createElement("video");
        media.muted = true;
        media.loop = true;
        media.autoplay = true;
        media.preload = "none";
      } else {
        media = document.createElement("img");
        media.loading = "lazy";
        media.alt = "";
      }
      let usedProxy = false;
      media.addEventListener("error", () => {
        if (!usedProxy) {
          usedProxy = true;
          media.src = `/api/preview?url=${encodeURIComponent(item.url)}`;
          return;
        }
        const note = document.createElement("div");
        note.className = "failed";
        note.textContent = "preview unavailable";
        fig.replaceChildren(note);
      });
      // Loaded on scroll rather than all at once: firing every candidate at the
      // source CDN simultaneously gets the whole burst rate-limited, which then
      // falls back to the proxy for images that would have loaded fine.
      media.dataset.src = item.url;
      fig.append(media);

      const row = document.createElement("div");
      row.className = "row";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = item.url.split("/").pop().split("?")[0];
      row.append(box, name);
      if (item.kind === "video") {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "video";
        badge.title = "converted to GIF on import (needs ffmpeg)";
        row.append(badge);
      }

      const toggle = (next) => {
        item.on = next;
        box.checked = next;
        cell.classList.toggle("on", next);
        refreshPickCount();
      };
      cell.addEventListener("click", (e) => {
        if (e.target !== box) toggle(!item.on);
      });
      box.addEventListener("change", () => toggle(box.checked));

      cell.append(fig, row);
      return cell;
    }),
  );

  refreshPickCount();
  picker.hidden = false;
  observePreviews();
}

// Only fetch a preview once its cell is near the viewport, so we never hit the
// source with hundreds of parallel requests.
let previewObserver = null;

function observePreviews() {
  previewObserver?.disconnect();
  previewObserver = new IntersectionObserver(
    (entries, obs) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const media = entry.target;
        obs.unobserve(media);
        if (media.dataset.src) {
          media.src = media.dataset.src;
          delete media.dataset.src;
        }
      }
    },
    { root: pickGrid, rootMargin: "300px" },
  );
  pickGrid.querySelectorAll("[data-src]").forEach((m) => previewObserver.observe(m));
}

function closePicker() {
  picker.hidden = true;
  pickGrid.replaceChildren(); // stop in-flight preview loads
  pickState = [];
}

$("#pickall").addEventListener("click", () => openPickerSelection(true));
$("#picknone").addEventListener("click", () => openPickerSelection(false));

function openPickerSelection(on) {
  pickState.forEach((p) => (p.on = on));
  [...pickGrid.children].forEach((cell) => {
    cell.classList.toggle("on", on);
    cell.querySelector("input[type=checkbox]").checked = on;
  });
  refreshPickCount();
}

$("#pickcancel").addEventListener("click", closePicker);
$("#pickgo").addEventListener("click", () => {
  const chosen = pickState.filter((p) => p.on);
  closePicker();
  importUrls(chosen);
});

// An inline field, not a prompt(): native prompt()/file dialogs are silently
// suppressed in embedded/preview contexts and can be disabled by the browser's
// "prevent additional dialogs" checkbox, which made this button look dead.
const grabUrl = $("#graburl");

function openGrab() {
  grabUrl.hidden = false;
  grabUrl.value = "";
  grabUrl.focus();
}
function closeGrab() {
  grabUrl.hidden = true;
}

$("#grab").addEventListener("click", () => (grabUrl.hidden ? openGrab() : closeGrab()));
grabUrl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    grab(grabUrl.value.trim());
    closeGrab();
  } else if (e.key === "Escape") {
    closeGrab();
  }
});

// Pasting a URL anywhere goes straight to the grabber, the common case. Skip
// it while typing in a field, including the grab box itself.
addEventListener("paste", (e) => {
  const el = document.activeElement;
  if (el === search || el === grabUrl || el?.isContentEditable) return;
  const text = e.clipboardData?.getData("text")?.trim();
  if (text && /^https?:\/\/\S+$/.test(text)) {
    e.preventDefault();
    grab(text);
  }
});

// ---------------------------------------------------------------- theme

// Each skin gets a period-correct tagline; the switch persists to localStorage
// and is applied pre-paint by the inline <head> script.
const TAGLINES = {
  // Memepool had no famous slogan the way Fark or AltaVista did, so this is
  // written in its register (a dry link-blog of obscure finds) rather than
  // passed off as a quotation.
  memepool: "animated oddities, found lying around the internet",
  fark: "it's not news, it's GIFs",
  zombo: "welcome. you can do anything. the only limit is yourself.",
  webvan: "GIFs delivered to your door in 30 minutes or less",
  petsdotcom: "because GIFs can't click for themselves",
  altavista: "smart is beautiful",
  linkedin: "grow your professional GIF network",
};

// A skin can rename the masthead to complete its costume; the linkedin skin
// brands the whole site as LinkedIn would. Default is the product name.
const PRODUCT = "gifhole";
const WORDMARKS = { linkedin: "linkedin" };

const themeSel = $("#theme");

function applyTheme(name) {
  if (!TAGLINES[name]) name = "memepool";
  document.documentElement.dataset.theme = name;
  themeSel.value = name;
  const tag = document.querySelector(".tagline");
  if (tag) tag.textContent = TAGLINES[name];
  const mark = document.querySelector("h1");
  if (mark) mark.textContent = WORDMARKS[name] || PRODUCT;
  try {
    localStorage.setItem("gifhole-theme", name);
  } catch {
    /* private mode, so theme just won't persist */
  }
}

themeSel.addEventListener("change", () => applyTheme(themeSel.value));
applyTheme(document.documentElement.dataset.theme || "memepool");

$("#add").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", (e) => upload(e.target.files));

// The modifier key for the path copy is "option" on a Mac (⌥ fires altKey) and
// "alt" elsewhere, so label it for whatever platform is actually viewing.
{
  // Case-insensitive: userAgentData.platform reports "macOS" (lowercase m),
  // while navigator.platform reports "MacIntel", so a /Mac/ regex misses the former.
  const isMac = /mac|iphone|ipad|ipod/i.test(
    navigator.userAgentData?.platform || navigator.platform || navigator.userAgent,
  );
  const hintEl = document.querySelector(".hint");
  if (hintEl) {
    hintEl.textContent =
      `click copies the image · shift-click copies the URL · ` +
      `${isMac ? "option" : "alt"}-click copies the file path`;
  }
}
$("#rescan").addEventListener("click", async () => {
  const r = await (await fetch("/api/rescan", { method: "POST" })).json();
  toast(`rescan: +${r.added} / -${r.removed}`);
  load();
});

// ---------------------------------------------------------------- jobs

// OCR and scraping run on the server's worker thread, so the UI polls. Polling
// stops once nothing is active, and any finished work triggers one reload so
// new GIFs and freshly-read text appear without a manual refresh.
const jobBar = $("#jobs");
let jobTimer = null;
let lastActive = 0;
const jobStatus = new Map();

function renderJobs(jobs) {
  const interesting = jobs.filter(
    (j) => j.status === "queued" || j.status === "running" || j.status === "error",
  );
  jobBar.hidden = interesting.length === 0;
  jobBar.replaceChildren(
    ...interesting.slice(0, 6).map((j) => {
      const row = document.createElement("div");
      row.className = `job ${j.status}`;
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = j.status === "error" ? "failed" : j.kind;
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = j.label;
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = j.status === "error" ? j.detail : "";
      row.append(kind, what, detail);
      return row;
    }),
  );
}

async function pollJobs() {
  let body;
  try {
    body = await (await fetch("/api/jobs")).json();
  } catch {
    // A failed poll must never strand capsLoaded: load() awaits it, so an
    // unresolved promise would leave the grid permanently empty.
    resolveCaps();
    jobTimer = setTimeout(pollJobs, 2000);
    return;
  }
  capabilities = body.capabilities;
  resolveCaps();
  renderJobs(body.jobs);

  // Reload as soon as an import or description lands. Waiting for the whole
  // queue to drain meant newly imported GIFs stayed invisible until every
  // queued OCR finished, which is minutes for a large import.
  let landed = false;
  for (const job of body.jobs) {
    const previous = jobStatus.get(job.id);
    if (previous && previous !== job.status && job.status === "done") {
      if (job.kind === "import" || job.kind === "enrich") landed = true;
    }
    jobStatus.set(job.id, job.status);
  }
  // OCR finishes in bulk, so those still refresh once when the queue empties.
  if (landed || (lastActive > 0 && body.active === 0)) load();
  lastActive = body.active;

  clearTimeout(jobTimer);
  if (body.active > 0) jobTimer = setTimeout(pollJobs, 700);
}

// ---------------------------------------------------------------- chrome

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 1800);
}

// Debounced: every keystroke otherwise refetches the whole library and
// rebuilds every card, which is ~10 requests per typed word.
let searchTimer;
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(load, 180);
});
sortSel.addEventListener("change", () => load());
addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== search) { e.preventDefault(); search.focus(); }
  if (e.key === "Escape") {
    if (!picker.hidden) return closePicker();
    search.value = ""; activeTags.clear(); load(); search.blur();
  }
});

load();
pollJobs();
