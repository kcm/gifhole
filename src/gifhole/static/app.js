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

// One copy path for both mouse and keyboard: a click maps its modifiers to a
// mode, a shortcut names the mode outright. A keydown counts as a user gesture,
// so the clipboard write is allowed either way.
async function copyGif(gif, card, mode = "gif") {
  try {
    let what;
    if (mode === "url") what = await copyText(location.origin + gif.url);
    else if (mode === "path") what = await copyText(`${state.root}/${gif.filename}`);
    else if (capabilities.file_clipboard) what = await copyFileViaServer(gif);
    else what = await copyImage(gif, card.querySelector("img"));
    toast(what);
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 500);
    fetch(`/api/gifs/${gif.id}/copied`, { method: "POST" });
  } catch (err) {
    toast(`copy failed: ${err.message}`);
  }
}

function handleClick(event, gif, card) {
  event.preventDefault();
  const mode = event.shiftKey ? "url" : event.altKey || event.metaKey ? "path" : "gif";
  return copyGif(gif, card, mode);
}

// Selecting the contents of a contenteditable, so rename starts ready to type
// over rather than with a caret parked at one end.
function selectAll(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
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
      <button class="mark" title="select for bulk actions" aria-pressed="false"></button>
      <button class="del" title="move to trash">x</button>
    </div>
    <div class="rowtags">
      <span class="chips"></span>
      <input class="taginput" spellcheck="false" autocomplete="off" aria-label="add a tag">
      <ul class="ac" hidden></ul>
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

  // Tagging is the primary filing mechanism, so it is a chip input rather than
  // a blob of text: one tag is one object you can drop, the field is always
  // open for the next one, and nothing here calls load(). A full reload per tag
  // would refetch the library and rebuild every card, losing scroll position
  // and focus mid-file.
  tagEditor(el, gif);

  el.querySelector(".mark").addEventListener("click", () => toggleMark(gif.id));
  // No confirm on a single delete: it goes to the trash and "z" takes it
  // straight back, so asking every time costs more than the mistake does.
  el.querySelector(".del").addEventListener("click", () => trashIds([gif.id]));

  el.querySelector("figure").addEventListener("click", (e) => {
    // Clicking also moves the keyboard selection, so mouse and keyboard never
    // disagree about which GIF is "current".
    selectedId = gif.id;
    paintSelection();
    handleClick(e, gif, el);
  });
  return el;
}

// The vocabulary drives autocomplete, so it is kept current locally instead of
// being refetched: adding a tag adjusts its count in place. A later load()
// replaces these with the server's authoritative numbers.
function bumpTag(tag, delta) {
  const row = state.tags.find((r) => r.tag === tag);
  if (row) {
    row.count += delta;
    if (row.count <= 0) state.tags = state.tags.filter((r) => r !== row);
  } else if (delta > 0) {
    state.tags.push({ tag, count: 1 });
  }
  state.tags.sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
  renderTags();
}

// Mirrors split_tags() on the server, so what you see on the chip is what got
// stored: lowercased, split on whitespace and commas.
function splitTags(raw) {
  return raw.toLowerCase().replace(/,/g, " ").split(/\s+/).filter(Boolean);
}

function tagEditor(el, gif) {
  const chipsEl = el.querySelector(".chips");
  const input = el.querySelector(".taginput");
  const acEl = el.querySelector(".ac");
  let tags = [...gif.tags];
  let items = [];
  let cursor = -1;

  // Serialised: each PATCH carries the whole tag list, so two in flight at once
  // could land out of order and resurrect a tag that was just removed.
  let pending = Promise.resolve();
  const save = () => {
    gif.tags = [...tags];
    const body = { tags: tags.join(" ") };
    pending = pending.then(() => patch(gif.id, body));
    return pending;
  };

  const renderChips = () => {
    chipsEl.replaceChildren(
      ...tags.map((tag) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        // The label filters (recall), the x removes (filing). Two jobs, two
        // targets, so neither gesture can trigger the other by accident.
        const label = document.createElement("button");
        label.className = "chiplabel";
        label.textContent = tag;
        label.title = `show everything tagged ${tag}`;
        label.addEventListener("click", () => {
          activeTags.add(tag);
          load();
        });
        const x = document.createElement("button");
        x.className = "chipx";
        x.textContent = "×";
        x.title = `remove ${tag}`;
        x.tabIndex = -1;
        x.addEventListener("click", () => remove(tag));
        chip.append(label, x);
        return chip;
      }),
    );
    input.placeholder = tags.length ? "+tag" : "add tags";
  };

  const add = (raw) => {
    let changed = false;
    for (const tag of splitTags(raw)) {
      if (tags.includes(tag)) continue;
      tags.push(tag);
      bumpTag(tag, +1);
      changed = true;
    }
    if (changed) {
      renderChips();
      save();
    }
  };

  const remove = (tag) => {
    const i = tags.indexOf(tag);
    if (i < 0) return;
    tags.splice(i, 1);
    bumpTag(tag, -1);
    renderChips();
    save();
  };

  const closeAc = () => {
    acEl.hidden = true;
    items = [];
    cursor = -1;
    el.classList.remove("tagging");
  };

  const paint = () => {
    [...acEl.children].forEach((li, i) => li.classList.toggle("on", i === cursor));
  };

  // Suggesting from the existing vocabulary is the point of the whole control:
  // it is what stops "reaction" and "reactions" becoming two shelves holding
  // half the collection each. Prefix matches rank above substring, then by use.
  const openAc = () => {
    const typed = input.value.trim().toLowerCase();
    const pool = state.tags
      .filter(({ tag }) => !tags.includes(tag) && tag.includes(typed))
      .sort((a, b) => {
        const ap = a.tag.startsWith(typed);
        const bp = b.tag.startsWith(typed);
        if (ap !== bp) return ap ? -1 : 1;
        return b.count - a.count || a.tag.localeCompare(b.tag);
      })
      .slice(0, 8);
    items = pool.map((p) => p.tag);
    if (!items.length) return closeAc();
    acEl.replaceChildren(
      ...pool.map(({ tag, count }, i) => {
        const li = document.createElement("li");
        li.className = "acitem";
        const n = document.createElement("span");
        n.className = "acn";
        n.textContent = count;
        li.append(tag, n);
        // mousedown, not click: click fires after blur, which would have
        // already closed the list out from under the pointer.
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          commit(tag);
        });
        li.addEventListener("mouseenter", () => {
          cursor = i;
          paint();
        });
        return li;
      }),
    );
    acEl.hidden = false;
    // .card clips its contents so the figure keeps square corners; the dropdown
    // has to escape that box, but only while this card is being tagged.
    el.classList.add("tagging");
    cursor = -1;
    paint();
  };

  const commit = (value) => {
    add(value);
    input.value = "";
    closeAc();
    input.focus();
  };

  input.addEventListener("focus", openAc);

  // A separator commits, and it is handled here rather than on keydown so that
  // paste, autofill and IME input work too: those deliver text with no keydown
  // at all. Pasting "reaction meme dog" therefore lands two chips and leaves
  // "dog" in the field, still editable.
  input.addEventListener("input", () => {
    if (/[\s,]/.test(input.value)) {
      const finished = /[\s,]$/.test(input.value);
      const parts = splitTags(input.value);
      const remainder = finished ? "" : (parts.pop() ?? "");
      if (parts.length) add(parts.join(" "));
      input.value = remainder;
    }
    openAc();
  });
  input.addEventListener("blur", () => {
    // Commit rather than discard: typing a tag and clicking away should file
    // it, not throw it out silently.
    if (input.value.trim()) {
      add(input.value);
      input.value = "";
    }
    closeAc();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (acEl.hidden) return openAc();
      const step = e.key === "ArrowDown" ? 1 : -1;
      if (cursor < 0) cursor = step > 0 ? 0 : items.length - 1;
      else cursor = (cursor + step + items.length) % items.length;
      paint();
      return;
    }
    // Space and comma are not listed here on purpose; the input handler above
    // catches them, so every route into the field behaves the same.
    if (e.key === "Enter" || e.key === "Tab") {
      const picked = cursor >= 0 ? items[cursor] : input.value.trim();
      if (!picked) {
        if (e.key === "Enter") input.blur();
        return;
      }
      e.preventDefault();
      commit(picked);
      return;
    }
    if (e.key === "Escape") {
      // Stop the global handler, which would otherwise wipe search and filters
      // just because you backed out of a suggestion list.
      e.stopPropagation();
      if (!acEl.hidden) return closeAc();
      input.value = "";
      input.blur();
      return;
    }
    if (e.key === "Backspace" && !input.value && tags.length) {
      e.preventDefault();
      remove(tags[tags.length - 1]);
    }
  });

  renderChips();
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

// ------------------------------------------------------------------ selection

// Tracked by id rather than by index: a render can reorder or filter the wall,
// and the selection should follow the GIF, not the position.
let selectedId = null;

const selectedIndex = () => state.gifs.findIndex((g) => g.id === selectedId);

function paintSelection() {
  [...grid.children].forEach((el, i) =>
    el.classList.toggle("selected", state.gifs[i]?.id === selectedId),
  );
}

function select(i) {
  if (!state.gifs.length) return;
  const clamped = Math.max(0, Math.min(i, state.gifs.length - 1));
  selectedId = state.gifs[clamped].id;
  paintSelection();
  grid.children[clamped]?.scrollIntoView({ block: "nearest" });
}

function move(delta) {
  const i = selectedIndex();
  select(i < 0 ? 0 : i + delta);
}

// The grid is auto-fill, so the column count changes with the window; read it
// back rather than assuming, or vertical movement drifts at other widths.
const columnCount = () =>
  getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length || 1;

// Keyboard actions apply to the selected card, falling back to whatever the
// pointer is over, so both habits work without a mode switch.
function targetCard() {
  const i = selectedIndex();
  if (i >= 0) return { gif: state.gifs[i], el: grid.children[i] };
  if (hoveredCard) {
    const j = [...grid.children].indexOf(hoveredCard);
    if (j >= 0) return { gif: state.gifs[j], el: hoveredCard };
  }
  return null;
}

// Marks for bulk actions, kept by id for the same reason as the selection: a
// filter or re-sort must not silently re-point them at other GIFs.
let marked = new Set();

// The last batch of trash names, so a delete can be taken back. Cleared once
// the trash is emptied, since there would be nothing left to restore.
let lastTrashed = [];

function paintMarks() {
  [...grid.children].forEach((el, i) => {
    const on = marked.has(state.gifs[i]?.id);
    el.classList.toggle("marked", on);
    el.querySelector(".mark")?.setAttribute("aria-pressed", String(on));
  });
  $("#bulk").hidden = marked.size === 0;
  $("#bulkcount").textContent = `${marked.size} selected`;
}

function toggleMark(id) {
  marked.has(id) ? marked.delete(id) : marked.add(id);
  paintMarks();
}

function clearMarks() {
  marked.clear();
  paintMarks();
}

// What a bulk action applies to: the marked set, or the current GIF when
// nothing is marked, so "x" means the same thing either way.
function actionIds() {
  if (marked.size) return [...marked];
  const i = selectedIndex();
  return i >= 0 ? [state.gifs[i].id] : [];
}

async function trashIds(ids) {
  if (!ids.length) return;
  const res = await fetch("/api/gifs/delete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  const out = await res.json();
  lastTrashed = out.trashed || [];
  clearMarks();
  toast(`moved ${out.removed} to trash · press z to undo`);
  load();
}

async function undoTrash() {
  if (!lastTrashed.length) return toast("nothing to undo");
  const res = await fetch("/api/trash/restore", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ names: lastTrashed }),
  });
  const out = await res.json();
  lastTrashed = [];
  toast(`restored ${out.restored.length}`);
  load();
}

function render() {
  grid.replaceChildren(...state.gifs.map(card));
  paintSelection();
  paintMarks();
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
  // Only the modifier wording is rebuilt; the help button next to it is markup,
  // so setting textContent on the whole line would delete it.
  const hintEl = document.querySelector(".hinttext");
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
// "t" tags whatever the pointer is over, so filing a batch is hover, t, type,
// Enter, without ever going for the mouse a second time.
let hoveredCard = null;
grid.addEventListener("mouseover", (e) => {
  hoveredCard = e.target.closest?.(".card") || null;
});
grid.addEventListener("mouseleave", () => (hoveredCard = null));

const isTyping = (node) =>
  node instanceof HTMLElement &&
  (node.tagName === "INPUT" || node.tagName === "TEXTAREA" || node.isContentEditable);

// ---------------------------------------------------------------- the trash

const trashPanel = $("#trash");
const trashList = $("#trashlist");
const closeTrash = () => (trashPanel.hidden = true);

async function openTrash() {
  const data = await (await fetch("/api/trash")).json();
  const entries = data.entries || [];
  $("#trashcount").textContent = entries.length
    ? `${entries.length} in the trash`
    : "the trash is empty";
  $("#trashempty").disabled = !entries.length;
  $("#trashdir").textContent = data.dir || "";

  trashList.replaceChildren(
    ...entries.map((entry) => {
      const row = document.createElement("li");
      row.className = "trashrow";
      const name = document.createElement("span");
      name.className = "trashname";
      name.textContent = entry.filename;
      const when = document.createElement("span");
      when.className = "trashwhen";
      when.textContent = `${sizeOf(entry.bytes)} · ${agoOf(entry.deleted_at)}`;
      const put = document.createElement("button");
      put.className = "linkish";
      put.textContent = "restore";
      put.addEventListener("click", async () => {
        await postJSON("/api/trash/restore", { names: [entry.name] });
        toast(`restored ${entry.filename}`);
        openTrash();
        load();
      });
      const gone = document.createElement("button");
      gone.className = "linkish danger";
      gone.textContent = "delete";
      gone.addEventListener("click", async () => {
        if (!confirm(`Delete ${entry.filename} for good? This cannot be undone.`)) return;
        await postJSON("/api/trash/purge", { names: [entry.name] });
        openTrash();
      });
      row.append(name, when, put, gone);
      return row;
    }),
  );
  trashPanel.hidden = false;
}

const sizeOf = (bytes) =>
  bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

function agoOf(seconds) {
  const mins = Math.max(0, (Date.now() / 1000 - seconds) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)} min ago`;
  if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} d ago`;
}

const postJSON = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json());

$("#trashbtn").addEventListener("click", openTrash);
$("#trashclose").addEventListener("click", closeTrash);
$("#trashempty").addEventListener("click", async () => {
  // Everything else in gifhole is recoverable; this is the one place that
  // isn't, so it asks plainly and says so.
  if (!confirm("Delete everything in the trash for good? This cannot be undone.")) return;
  const out = await postJSON("/api/trash/purge", { all: true });
  lastTrashed = [];
  toast(`deleted ${out.purged} for good`);
  openTrash();
});

$("#bulktrash").addEventListener("click", () => trashIds([...marked]));
$("#bulkclear").addEventListener("click", clearMarks);
$("#clearall").addEventListener("click", async () => {
  const total = state.gifs.length;
  if (!total) return toast("nothing to clear");
  if (!confirm(`Move all ${total} GIFs to the trash? You can restore them from there.`)) return;
  const out = await postJSON("/api/gifs/clear", { confirm: "clear" });
  lastTrashed = out.trashed || [];
  clearMarks();
  toast(`moved ${out.removed} to trash · press z to undo`);
  load();
});

const help = $("#help");
const closeHelp = () => (help.hidden = true);
const toggleHelp = () => (help.hidden = !help.hidden);
$("#helpbtn").addEventListener("click", toggleHelp);

// Acting on the current GIF. Where a button already exists these go through it
// rather than around it, so the confirm on delete and the disabled state on
// describe keep applying.
const CARD_KEYS = {
  c: (t) => copyGif(t.gif, t.el, "gif"),
  Enter: (t) => copyGif(t.gif, t.el, "gif"),
  u: (t) => copyGif(t.gif, t.el, "url"),
  p: (t) => copyGif(t.gif, t.el, "path"),
  t: (t) => t.el.querySelector(".taginput").focus(),
  r: (t) => {
    const name = t.el.querySelector(".name");
    name.focus();
    selectAll(name);
  },
  e: (t) => t.el.querySelector(".describe").click(),
};

addEventListener("keydown", (e) => {
  const typing = isTyping(e.target);

  // Escape backs out one layer at a time rather than resetting everything, so
  // leaving a suggestion list doesn't also wipe the search you were refining.
  if (e.key === "Escape") {
    if (!help.hidden) return closeHelp();
    if (!trashPanel.hidden) return closeTrash();
    if (!picker.hidden) return closePicker();
    if (typing) return; // fields handle their own Escape
    if (marked.size) return clearMarks();
    if (selectedId !== null) {
      selectedId = null;
      return paintSelection();
    }
    search.value = "";
    activeTags.clear();
    load();
    search.blur();
    return;
  }

  // Shortcuts are bare letters, so they must not fire mid-word in a tag field,
  // a rename or the search box. Modified keys belong to the browser.
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;

  // "?" is shift+/ on most layouts, so it has to be tested before "/".
  if (e.key === "?") {
    e.preventDefault();
    return toggleHelp();
  }
  if (e.key === "/") {
    e.preventDefault();
    return search.focus();
  }

  const cols = columnCount();
  const step = { j: cols, ArrowDown: cols, k: -cols, ArrowUp: -cols,
                 l: 1, ArrowRight: 1, h: -1, ArrowLeft: -1 }[e.key];
  if (step !== undefined) {
    e.preventDefault();
    return move(step);
  }
  if (e.key === "Home") {
    e.preventDefault();
    return select(0);
  }
  if (e.key === "End") {
    e.preventDefault();
    return select(state.gifs.length - 1);
  }

  // Removal. "x" trashes the marked set, or just the current GIF when nothing
  // is marked, so it means the same thing whether or not you are batching.
  if (e.key === "x" || e.key === "Delete") {
    const ids = actionIds();
    if (!ids.length) return;
    e.preventDefault();
    // One GIF goes without asking because "z" takes it straight back; a batch
    // asks, because that is the one you would not want to fire by accident.
    if (ids.length > 1 && !confirm(`Move ${ids.length} GIFs to the trash?`)) return;
    return trashIds(ids);
  }
  if (e.key === "z") {
    e.preventDefault();
    return undoTrash();
  }
  if (e.key === " " || e.key === "v") {
    const i = selectedIndex();
    if (i < 0) return;
    e.preventDefault();
    return toggleMark(state.gifs[i].id);
  }
  if (e.key === "A") {
    e.preventDefault();
    if (marked.size === state.gifs.length) return clearMarks();
    marked = new Set(state.gifs.map((g) => g.id));
    return paintMarks();
  }
  if (e.key === "T") {
    e.preventDefault();
    return trashPanel.hidden ? openTrash() : closeTrash();
  }

  // Library-wide actions reuse the toolbar buttons, so there is one
  // implementation of each and the shortcut can never drift from the click.
  const button = { a: "#add", g: "#grab", R: "#rescan" }[e.key];
  if (button) {
    e.preventDefault();
    return $(button).click();
  }
  if (e.key === "s") {
    e.preventDefault();
    sortSel.selectedIndex = (sortSel.selectedIndex + 1) % sortSel.options.length;
    sortSel.dispatchEvent(new Event("change"));
    return;
  }

  const action = CARD_KEYS[e.key];
  if (!action) return;
  const target = targetCard();
  if (!target) return;
  e.preventDefault();
  action(target);
});

help.addEventListener("click", closeHelp);

load();
pollJobs();
