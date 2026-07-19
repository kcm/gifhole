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
      <span class="desc" contenteditable="plaintext-only" spellcheck="false"
            data-placeholder="add a description"></span>
      <button class="describe" title="describe with Claude">describe</button>
    </div>`;

  el.querySelector("img").src = gif.url;
  el.querySelector(".dims").textContent = `${gif.width}x${gif.height}`;

  // Both are search keys, and they answer different questions: the quote is
  // text burned into the picture, read locally, and is a fact about the file.
  // The description is prose about it, which Claude may write and you may
  // rewrite. Showing only one (which this used to do) hid the description
  // entirely on any GIF with text in it.
  const quote = el.querySelector(".quote");
  quote.textContent = gif.ocr_text ? `“${gif.ocr_text}”` : "";
  quote.hidden = !gif.ocr_text;

  const desc = el.querySelector(".desc");
  desc.textContent = gif.description;
  // Saved directly, not by asking blur to do it. Blur is not reliable enough
  // to be the only path: the element can lose focus without the handler
  // running, and the edit vanishes with no sign it was dropped. Same reason
  // the tag field commits on its own.
  const saveDesc = () => {
    const text = desc.textContent.trim();
    if (text === gif.description) return;
    gif.description = text;
    patch(gif.id, { description: text });
  };
  desc.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveDesc();
      desc.blur();
      return;
    }
    if (e.key === "Escape") {
      // Put back what was there rather than saving a half-typed edit.
      e.stopPropagation();
      desc.textContent = gif.description;
      desc.blur();
    }
  });
  desc.addEventListener("blur", saveDesc);

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
function tokenize(raw) {
  return raw.toLowerCase().replace(/,/g, " ").split(/\s+/).filter(Boolean);
}

// A leading "-" removes instead of adds. That is the only way to take a tag off
// a whole batch at once; per card you would just click the chip's x.
function parseTags(raw) {
  const add = [];
  const remove = [];
  for (const token of tokenize(raw)) {
    if (token[0] !== "-") add.push(token);
    else if (token.length > 1) remove.push(token.slice(1));
  }
  return { add, remove };
}

// The suggesting tag field, shared by the per-card editor and the bulk bar.
// Sharing it is the point: a bulk field with no suggestions would become the
// fastest way to invent the near-duplicate tags autocomplete exists to prevent.
//
// opts.current()   tags already on the target ([] when there is no single one)
// opts.scoped      restrict "-" suggestions to current(); false suggests all
// opts.commit(add, remove)
// opts.onOpen/onClose, opts.onBackspaceEmpty  optional
function tagInput(input, acEl, opts) {
  let items = [];
  let cursor = -1;

  const close = () => {
    acEl.hidden = true;
    items = [];
    cursor = -1;
    opts.onClose?.();
  };

  const paint = () => {
    [...acEl.children].forEach((li, i) => li.classList.toggle("on", i === cursor));
  };

  const open = () => {
    const raw = input.value.trim().toLowerCase();
    const removing = raw.startsWith("-");
    const typed = removing ? raw.slice(1) : raw;
    const have = opts.current();
    // Removing offers what you have; adding offers what you don't. Unscoped
    // (the bulk bar) there is no single "have", so everything is on offer.
    const pool = state.tags
      .filter(({ tag }) => {
        if (!tag.includes(typed)) return false;
        if (!removing) return !have.includes(tag);
        return opts.scoped ? have.includes(tag) : true;
      })
      .sort((a, b) => {
        const ap = a.tag.startsWith(typed);
        const bp = b.tag.startsWith(typed);
        if (ap !== bp) return ap ? -1 : 1;
        return b.count - a.count || a.tag.localeCompare(b.tag);
      })
      .slice(0, 8);
    items = pool.map((p) => (removing ? `-${p.tag}` : p.tag));
    if (!items.length) return close();
    acEl.replaceChildren(
      ...pool.map(({ tag, count }, i) => {
        const li = document.createElement("li");
        li.className = "acitem";
        const n = document.createElement("span");
        n.className = "acn";
        n.textContent = count;
        li.append(removing ? `remove ${tag}` : tag, n);
        // mousedown, not click: click fires after blur, which would have
        // already closed the list out from under the pointer.
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          commit(items[i]);
        });
        li.addEventListener("mouseenter", () => {
          cursor = i;
          paint();
        });
        return li;
      }),
    );
    acEl.hidden = false;
    opts.onOpen?.();
    cursor = -1;
    paint();
  };

  const apply = (raw) => {
    const { add, remove } = parseTags(raw);
    if (add.length || remove.length) opts.commit(add, remove);
  };

  const commit = (value) => {
    apply(value);
    input.value = "";
    close();
    input.focus();
  };

  input.addEventListener("focus", open);

  // A separator commits, and it is handled here rather than on keydown so that
  // paste, autofill and IME input work too: those deliver text with no keydown
  // at all. Pasting "reaction meme dog" therefore files two and leaves "dog"
  // in the field, still editable.
  input.addEventListener("input", () => {
    if (/[\s,]/.test(input.value)) {
      const finished = /[\s,]$/.test(input.value);
      const parts = tokenize(input.value);
      const remainder = finished ? "" : (parts.pop() ?? "");
      if (parts.length) apply(parts.join(" "));
      input.value = remainder;
    }
    open();
  });

  input.addEventListener("blur", () => {
    // Commit rather than discard: typing a tag and clicking away should file
    // it, not throw it out silently.
    if (input.value.trim()) {
      apply(input.value);
      input.value = "";
    }
    close();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (acEl.hidden) return open();
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
      if (!acEl.hidden) return close();
      input.value = "";
      input.blur();
      return;
    }
    if (e.key === "Backspace" && !input.value) opts.onBackspaceEmpty?.(e);
  });

  return { open, close };
}

function tagEditor(el, gif) {
  const chipsEl = el.querySelector(".chips");
  const input = el.querySelector(".taginput");
  const acEl = el.querySelector(".ac");
  let tags = [...gif.tags];

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
    for (const tag of tokenize(raw)) {
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

  tagInput(input, acEl, {
    current: () => tags,
    scoped: true,
    commit: (toAdd, toRemove) => {
      toRemove.forEach(remove);
      if (toAdd.length) add(toAdd.join(" "));
    },
    // .card clips its contents so the figure keeps square corners; the dropdown
    // has to escape that box, but only while this card is being tagged.
    onOpen: () => el.classList.add("tagging"),
    onClose: () => el.classList.remove("tagging"),
    onBackspaceEmpty: (e) => {
      if (!tags.length) return;
      e.preventDefault();
      remove(tags[tags.length - 1]);
    },
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
  const describe = $("#bulkdescribe");
  describe.disabled = !capabilities.enrich;
  if (!capabilities.enrich) describe.title = capabilities.enrich_reason || "needs an API key";
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
  try {
    const out = await postJSON("/api/gifs/delete", { ids });
    lastTrashed = out.trashed || [];
    clearMarks();
    toast(`moved ${out.removed} to trash · press z to undo`);
    load();
  } catch (err) {
    toast(`delete failed: ${err.message}`);
  }
}

async function undoTrash() {
  if (!lastTrashed.length) return toast("nothing to undo");
  try {
    const out = await postJSON("/api/trash/restore", { names: lastTrashed });
    lastTrashed = [];
    toast(`restored ${out.restored.length}`);
    load();
  } catch (err) {
    toast(`undo failed: ${err.message}`);
  }
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

// Files the server recognised as duplicates, held back for a decision. Kept as
// the original File objects so "add anyway" can re-post the exact bytes rather
// than asking the user to find the file again.
let pendingDupes = [];

async function postFile(file, force = false) {
  const body = new FormData();
  body.append("file", file);
  if (force) body.append("force", "1");
  const res = await fetch("/api/gifs", { method: "POST", body });
  return res.ok ? res.json() : null;
}

// Pasted files can arrive with no name at all, so the type is checked first and
// the name check tolerates its absence.
const isGifFile = (f) => f.type === "image/gif" || !!f.name?.toLowerCase().endsWith(".gif");

async function upload(files, source = "drop") {
  const gifs = [...files].filter(isGifFile);
  if (!gifs.length) return toast(`no GIFs in that ${source}`);
  let ok = 0;
  const dupes = [];
  for (const file of gifs) {
    const out = await postFile(file);
    if (!out) continue;
    // A duplicate answers 200 with matches instead of creating anything, so
    // nothing lands until the user says so.
    if (out.duplicate) dupes.push({ file, matches: out.matches });
    else ok += 1;
  }
  if (ok) toast(`added ${ok} of ${gifs.length}`);
  load();
  if (dupes.length) showDupes(dupes);
  else if (!ok) toast("nothing added");
}

// ---------------------------------------------------------------- duplicates

const dupePanel = $("#dupes");
const closeDupes = () => {
  dupePanel.hidden = true;
  pendingDupes = [];
};

function showDupes(items) {
  resetDupePanel();
  pendingDupes = items;
  $("#dupecount").textContent =
    items.length === 1 ? "1 looks familiar" : `${items.length} look familiar`;

  $("#dupelist").replaceChildren(
    ...items.map(({ file, matches }, i) => {
      const row = document.createElement("div");
      row.className = "duperow";
      row.dataset.index = String(i);

      const incoming = document.createElement("figure");
      incoming.className = "dupefig";
      const img = document.createElement("img");
      // The candidate is not on the server, so preview it from the local file.
      img.src = URL.createObjectURL(file);
      img.addEventListener("load", () => URL.revokeObjectURL(img.src), { once: true });
      const caption = document.createElement("figcaption");
      caption.textContent = file.name;
      incoming.append(img, caption);

      const versus = document.createElement("div");
      versus.className = "dupematches";
      for (const match of matches) {
        const fig = document.createElement("figure");
        fig.className = "dupefig";
        const mi = document.createElement("img");
        mi.src = match.url;
        const mc = document.createElement("figcaption");
        mc.textContent = `${match.match} · ${match.filename}`;
        fig.append(mi, mc);
        versus.append(fig);
      }

      const actions = document.createElement("div");
      actions.className = "dupeactions";
      const add = document.createElement("button");
      add.textContent = "Add anyway";
      add.addEventListener("click", async () => {
        add.disabled = true;
        await postFile(file, true);
        row.remove();
        toast(`added ${file.name}`);
        load();
        if (!$("#dupelist").children.length) closeDupes();
      });
      const skip = document.createElement("button");
      skip.textContent = "Skip";
      skip.addEventListener("click", () => {
        row.remove();
        if (!$("#dupelist").children.length) closeDupes();
      });
      actions.append(add, skip);

      row.append(incoming, versus, actions);
      return row;
    }),
  );
  dupePanel.hidden = false;
}

// Duplicates already sitting in the library, as opposed to one arriving. Same
// panel, different shape: every member is a real GIF, so each can be trashed
// (recoverably) rather than accepted or skipped.
function showExistingDupes(groups) {
  pendingDupes = [];
  $("#dupeaddall").hidden = true;
  $("#dupeskipall").textContent = "Done";
  document.querySelector("#dupes h2").textContent = "duplicates in your library";
  $("#dupecount").textContent =
    groups.length === 1 ? "1 group" : `${groups.length} groups`;

  $("#dupelist").replaceChildren(
    ...groups.map((group) => {
      const row = document.createElement("div");
      row.className = "duperow";
      const matches = document.createElement("div");
      matches.className = "dupematches";
      for (const gif of group) {
        const fig = document.createElement("figure");
        fig.className = "dupefig";
        const img = document.createElement("img");
        img.src = gif.url;
        const cap = document.createElement("figcaption");
        cap.textContent = `${gif.filename} · ${gif.width}x${gif.height}`;
        const bin = document.createElement("button");
        bin.className = "linkish danger";
        bin.textContent = "trash this";
        bin.addEventListener("click", async () => {
          await trashIds([gif.id]);
          fig.remove();
          if (matches.querySelectorAll(".dupefig").length < 2) row.remove();
          if (!$("#dupelist").children.length) closeDupes();
        });
        fig.append(img, cap, bin);
        matches.append(fig);
      }
      row.append(matches);
      return row;
    }),
  );
  dupePanel.hidden = false;
}

// Restores the panel to its arriving-duplicate shape, since the two share it.
function resetDupePanel() {
  $("#dupeaddall").hidden = false;
  $("#dupeskipall").textContent = "Skip all";
  document.querySelector("#dupes h2").textContent = "already have these?";
}

$("#dupeskipall").addEventListener("click", closeDupes);
$("#dupeaddall").addEventListener("click", async () => {
  const items = [...pendingDupes];
  closeDupes();
  for (const { file } of items) await postFile(file, true);
  toast(`added ${items.length} anyway`);
  load();
});

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
// Dragging out of a web page hands over a URL, not a file: the browser never
// downloaded a copy to give away. So a drag from Giphy, Tenor or a search
// results page arrives as text, and treating "no files" as "no GIFs" reported
// nothing usable when the URL was right there.
//
// Types are tried in order of how specific they are. text/x-moz-url is
// Firefox's own, and is "url\ntitle", so the first line is the URL.
function urlFromTransfer(dt) {
  if (!dt) return null;

  // Firefox first, because its type is the only one that carries a title, and
  // the title is the only readable name a Giphy or Tenor drag ever supplies:
  // every file there is called giphy.gif.
  const moz = dt.getData("text/x-moz-url");
  if (moz) {
    const [url, title] = moz.split(/[\r\n]+/);
    if (url && /^https?:\/\/\S+$/i.test(url.trim())) {
      return { url: url.trim(), title: (title || "").trim() };
    }
  }

  const uriList = dt.getData("text/uri-list");
  if (uriList) {
    // Comment lines start with "#" in the uri-list format.
    const first = uriList.split(/[\r\n]+/).find((line) => line && !line.startsWith("#"));
    if (first && /^https?:\/\//i.test(first)) return { url: first.trim(), title: "" };
  }

  for (const type of ["text/plain", "URL", "text"]) {
    const raw = dt.getData(type);
    const first = raw?.split(/[\r\n]+/)[0]?.trim();
    if (first && /^https?:\/\/\S+$/i.test(first)) return { url: first, title: "" };
  }

  // Last resort: the dragged markup. Parsed, not matched with a regex, and
  // DOMParser neither runs scripts nor loads the image.
  const html = dt.getData("text/html");
  if (html) {
    const img = new DOMParser().parseFromString(html, "text/html").querySelector("img");
    const src = img?.getAttribute("src") || "";
    // alt text is a real name surprisingly often on media sites.
    if (/^https?:\/\//i.test(src)) return { url: src, title: img.getAttribute("alt") || "" };
  }
  return null;
}

addEventListener("drop", (e) => {
  e.preventDefault();
  clearTimeout(dragTimer);
  drop.hidden = true;

  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) return upload(files);

  const dragged = urlFromTransfer(e.dataTransfer);
  // grab() takes a direct .gif or a page and works out which, so a drag of the
  // image and a drag of the link to it both land somewhere sensible.
  if (dragged) return grab(dragged.url, dragged.title);

  toast("nothing to add in that drop");
});

// ---------------------------------------------------------------- grabbing

// A page can hold hundreds of GIFs, so discovery and import are separate: we
// list what's there, let you pick, then download only the ticked ones.
async function grab(url, title = "") {
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
  // A direct link is unambiguous; no point making you tick one box. A dragged
  // title is the only name a Giphy or Tenor link carries, so it is kept.
  if (kind === "direct") {
    return importUrls(candidates.map((c) => ({ ...c, title: c.title || title })));
  }
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

// Pasting anywhere adds: a copied GIF file goes in like a drop, a URL goes to
// the grabber. Skipped while typing in a field, including the grab box itself,
// or pasting a caption into a description would upload something instead.
addEventListener("paste", (e) => {
  const el = document.activeElement;
  if (el === search || el === grabUrl || el?.isContentEditable || el?.tagName === "INPUT") return;

  const data = e.clipboardData;
  if (!data) return;

  // Gather from both places before deciding anything. Copying a GIF in Finder
  // puts a real file on the clipboard, which is what a drop delivers too, so it
  // takes the same path and gets the same duplicate check. Some sources expose
  // it only as an item rather than in .files.
  const files = [...(data.files || [])];
  if (!files.length) {
    files.push(
      ...[...(data.items || [])]
        .filter((i) => i.kind === "file")
        .map((i) => i.getAsFile())
        .filter(Boolean),
    );
  }

  const gifs = files.filter(isGifFile);
  if (gifs.length) {
    e.preventDefault();
    return upload(gifs, "paste");
  }

  // An image, but not a GIF. Worth saying so: copying a picture out of a web
  // page usually yields a PNG, and the animation was never on the clipboard at
  // all, so silence would look like a bug rather than an explanation.
  const image = files.find((f) => f.type?.startsWith("image/"));
  if (image) {
    e.preventDefault();
    return toast(`that is ${image.type}, not a GIF. Copy the file itself, or paste its URL`);
  }

  const pasted = urlFromTransfer(data);
  if (pasted) {
    e.preventDefault();
    grab(pasted.url, pasted.title);
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

  // A queue is the only place a long run is visible, so it is where stopping
  // one belongs. Only offered when something is actually waiting: with just
  // one job running there is nothing left to stop.
  const waiting = interesting.filter((j) => j.status === "queued").length;
  if (waiting) {
    const stop = document.createElement("div");
    stop.className = "job stoprow";
    const label = document.createElement("span");
    label.className = "kind";
    label.textContent = "queued";
    const count = document.createElement("span");
    count.className = "what";
    count.textContent = `${waiting} waiting`;
    const button = document.createElement("button");
    button.className = "linkish";
    button.textContent = "stop the rest";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const out = await postJSON("/api/jobs/cancel", {});
        // Says "the rest" everywhere because the running one is not killed.
        toast(out.cancelled ? `stopped ${out.cancelled} queued` : "nothing left to stop");
      } catch (err) {
        toast(`could not stop: ${err.message}`);
      }
      pollJobs();
    });
    stop.append(label, count, button);
    jobBar.append(stop);
  }
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
      // Must match the kinds app.py submits. This drifted once already: the
      // enrich job was renamed to "describe" and this kept testing the old
      // name, so a finished description never refreshed its card.
      if (job.kind === "import" || job.kind === "describe") landed = true;
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

// ------------------------------------------------------------ library panel

// One place for the things that act on the whole library rather than on a
// GIF: describing in bulk, rescanning, duplicates, the trash, and clearing.
// They were scattered across the toolbar and the footer, which put a costly
// action and a destructive one a click apart from everyday buttons.
const libPanel = $("#library");
const libScope = $("#libscope");
const closeLibrary = () => (libPanel.hidden = true);
let libStats = null;

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function paintScopeCount() {
  const n = libStats ? (libStats[libScope.value] ?? 0) : 0;
  $("#libscopecount").textContent = n ? `${plural(n, "GIF")} to describe` : "nothing to do";
  // Disabled on nothing-to-do as well as no-key: a live button that would make
  // zero calls is the same lie as one whose calls all fail.
  $("#libdescribe").disabled = !n || !capabilities.enrich;
  $("#libdescribe").title = capabilities.enrich
    ? ""
    : capabilities.enrich_reason || "needs an API key";
}

async function openLibrary() {
  try {
    const body = await (await fetch("/api/library")).json();
    libStats = body.stats;
  } catch {
    return toast("could not read the library. Is the server running the current code?");
  }
  const s = libStats;
  const mb = s.bytes > 1048576 ? `${(s.bytes / 1048576).toFixed(0)} MB` : `${Math.round(s.bytes / 1024)} KB`;
  // The version is here rather than tucked in a corner because a server
  // running older code than the page it serves has already caused one
  // baffling bug: buttons that looked live and silently did nothing.
  $("#libsummary").textContent =
    `gifhole ${body.version} · ${plural(s.total, "GIF")} · ${mb} · ` +
    `${plural(s.tags, "tag")} · ${s.described} described`;
  paintScopeCount();
  libPanel.hidden = false;
}

// Built from location.origin, so it points at wherever this instance actually
// is: a different port, or a server, not a hardcoded 127.0.0.1.
//
// It navigates rather than fetching. A bookmarklet runs in the visited page's
// origin, so a fetch here would be cross-origin and would be refused by the
// same middleware that stops a random site driving your library. Opening a URL
// is a plain top-level GET and sidesteps all of it.
// Collects media from the rendered page as well as sending the page address.
// Both, not either: a page that renders its media in JavaScript (Imgur) has
// nothing in its HTML for the server to scrape, while Reddit is better scraped
// server-side, where old.reddit.com exposes every comment rather than only the
// ones currently rendered. The union covers both, and gifhole dedupes.
//
// Findings travel in the URL fragment, which is never sent to the server: no
// request-line length limit, and no list of URLs in the access log.
function bookmarkletSource() {
  const target = `${location.origin}/#add=`;
  const script = `(function(){
    var seen={},out=[];
    var keep=function(raw){
      if(!raw||out.length>=${BOOKMARKLET_MAX})return;
      var u;try{u=new URL(raw,location.href).href}catch(e){return}
      u=u.replace(/\\.gifv(\\?|$)/i,'.mp4$1');
      if(!/\\.(gif|mp4|webm)(\\?|$)/i.test(u)||seen[u])return;
      seen[u]=1;out.push(u);
    };
    var all=document.querySelectorAll('img,source,video,a');
    for(var i=0;i<all.length;i++){var e=all[i];
      keep(e.currentSrc);keep(e.getAttribute('src'));
      keep(e.getAttribute('data-src'));keep(e.getAttribute('href'));keep(e.poster);
    }
    window.open(${JSON.stringify(target)}+encodeURIComponent(
      JSON.stringify({page:location.href,urls:out})),'_blank');
  })()`;
  return "javascript:" + script.replace(/\s*\n\s*/g, "");
}

// Enough for a long thread, and short of the point where a fragment gets
// unwieldy. Reported rather than silently truncated, per the same rule that
// governs scraping a page.
const BOOKMARKLET_MAX = 300;

const bookmarklet = $("#bookmarklet");
// href as a property, never interpolated into markup.
bookmarklet.href = bookmarkletSource();
bookmarklet.addEventListener("click", (e) => {
  // Clicking it here would try to add gifhole itself. It is for dragging.
  e.preventDefault();
  toast("drag it to your bookmarks bar, then press it on a page with GIFs");
});

libScope.addEventListener("change", paintScopeCount);
$("#librarybtn").addEventListener("click", openLibrary);
$("#libclose").addEventListener("click", closeLibrary);

$("#libdescribe").addEventListener("click", async () => {
  const scope = libScope.value;
  const n = libStats?.[scope] ?? 0;
  if (!n) return;
  if (!confirm(`Describe ${plural(n, "GIF")} with Claude? That is ${n} API calls, and costs money.`)) {
    return;
  }
  closeLibrary();
  try {
    const out = await postJSON("/api/gifs/describe", { scope });
    toast(`describing ${out.queued} · watch the job strip`);
    pollJobs();
  } catch (err) {
    toast(`describe failed: ${err.message}`);
  }
});

$("#librescan").addEventListener("click", () => {
  closeLibrary();
  $("#rescan").click();
});
$("#libtrash").addEventListener("click", () => {
  closeLibrary();
  openTrash();
});
$("#libclear").addEventListener("click", () => {
  closeLibrary();
  $("#clearall").click();
});
$("#libdupes").addEventListener("click", async () => {
  toast("looking for duplicates…");
  let groups;
  try {
    groups = (await (await fetch("/api/duplicates")).json()).groups;
  } catch {
    return toast("could not check for duplicates");
  }
  closeLibrary();
  if (!groups.length) return toast("no duplicates found");
  showExistingDupes(groups);
});

// ---------------------------------------------------------------- the trash

const trashPanel = $("#trash");
const trashList = $("#trashlist");
const closeTrash = () => {
  trashPanel.hidden = true;
  disarmEmpty?.();
};

async function openTrash() {
  let data;
  try {
    const res = await fetch("/api/trash");
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch {
    // Say so rather than showing a convincing but false "the trash is empty".
    toast("could not read the trash. Is the server running the current code?");
    return;
  }
  const entries = data.entries || [];
  $("#trashcount").textContent = entries.length
    ? `${entries.length} in the trash`
    : "the trash is empty";
  $("#trashempty").disabled = !entries.length;
  disarmEmpty?.();
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
        try {
          await postJSON("/api/trash/restore", { names: [entry.name] });
        } catch (err) {
          return toast(`restore failed: ${err.message}`);
        }
        toast(`restored ${entry.filename}`);
        openTrash();
        load();
      });
      const gone = document.createElement("button");
      gone.className = "linkish danger";
      gone.textContent = "delete";
      armable(gone, "really?", async () => {
        try {
          await postJSON("/api/trash/purge", { names: [entry.name] });
        } catch (err) {
          return toast(`delete failed: ${err.message}`);
        }
        row.remove();
        toast(`deleted ${entry.filename} for good`);
        if (!trashList.children.length) closeTrash();
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

// Throws on a non-OK response instead of handing back the error body as if it
// were data. A server running older code answers new routes with 404 or 405,
// and quietly reading that as "nothing to do" is exactly how a live-looking
// button does nothing at all.
async function postJSON(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("gifhole's server is not responding. Is it still running?");
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    if (res.status === 404 || res.status === 405) {
      throw new Error(`this needs a newer server than the one running. Restart gifhole`);
    }
    throw new Error(detail?.detail || `server said ${res.status}`);
  }
  return res.json();
}

$("#trashbtn").addEventListener("click", openTrash);
$("#trashclose").addEventListener("click", closeTrash);
// Arms in place rather than opening a confirm() on top of the panel: stacking
// a dialog over a dialog is horrible, but this is still the one action in
// gifhole that destroys anything, so it does not go on a single click either.
// One press arms it, a second does it, anything else disarms.
function armable(button, armedLabel, run) {
  const idle = button.textContent;
  const disarm = () => {
    button.textContent = idle;
    button.classList.remove("armed");
    delete button.dataset.armed;
  };
  button.addEventListener("click", async () => {
    if (!button.dataset.armed) {
      button.dataset.armed = "1";
      button.textContent = armedLabel;
      button.classList.add("armed");
      return;
    }
    disarm();
    await run();
  });
  return disarm;
}

const disarmEmpty = armable($("#trashempty"), "Really? No undo", async () => {
  try {
    const out = await postJSON("/api/trash/purge", { all: true });
    lastTrashed = [];
    // Closes rather than re-rendering an empty list: the panel exists to show
    // what you can still get back, and there is nothing left.
    closeTrash();
    toast(`deleted ${out.purged} for good`);
  } catch (err) {
    toast(`could not empty the trash: ${err.message}`);
  }
});

// Every GIF here is a billable call, so this one always asks, and says how
// many. Already-described GIFs are skipped server-side, so re-running over a
// batch you have partly done costs only the remainder.
async function describeIds(ids) {
  if (!ids.length) return;
  if (!capabilities.enrich) {
    return toast(capabilities.enrich_reason || "describing needs an API key");
  }
  if (!confirm(`Describe ${ids.length} GIF${ids.length > 1 ? "s" : ""} with Claude? ` +
      `That is one API call each, and costs money.`)) {
    return;
  }
  try {
    const out = await postJSON("/api/gifs/describe", { ids });
    const skipped = out.skipped ? `, skipped ${out.skipped} already described` : "";
    toast(out.queued ? `describing ${out.queued}${skipped}` : `nothing to do${skipped}`);
    pollJobs();
  } catch (err) {
    toast(`describe failed: ${err.message}`);
  }
}

$("#bulkdescribe").addEventListener("click", () => describeIds([...marked]));

$("#bulktrash").addEventListener("click", () => {
  const ids = [...marked];
  if (ids.length > 1 && !confirm(`Move ${ids.length} GIFs to the trash?`)) return;
  trashIds(ids);
});
$("#bulkclear").addEventListener("click", clearMarks);

// Filing a scraped batch is the case this exists for, so it keeps the marks
// after applying: tagging 40 GIFs "reaction" then "meme" is two keystrokes
// apart, not two rounds of re-selecting.
tagInput($("#bulktag"), $("#bulkac"), {
  current: () => [],
  scoped: false,
  commit: async (add, remove) => {
    const ids = [...marked];
    if (!ids.length) return;
    const out = await postJSON("/api/gifs/tag", {
      ids,
      add: add.join(" "),
      remove: remove.join(" "),
    });
    const what = [
      add.length ? `+${add.join(" +")}` : "",
      remove.length ? `-${remove.join(" -")}` : "",
    ]
      .filter(Boolean)
      .join(" ");
    toast(`${what} on ${out.changed} of ${out.asked}`);
    // Counts and chips both come from the server here rather than being
    // adjusted locally: a batch touches too many rows to keep in step by hand.
    load();
  },
});
$("#clearall").addEventListener("click", async () => {
  const total = state.gifs.length;
  if (!total) return toast("nothing to clear");
  if (!confirm(`Move all ${total} GIFs to the trash? You can restore them from there.`)) return;
  let out;
  try {
    out = await postJSON("/api/gifs/clear", { confirm: "clear" });
  } catch (err) {
    return toast(`could not clear the library: ${err.message}`);
  }
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
  // Same rule as x: act on the marked set when there is one, otherwise on the
  // current GIF. So "t" is always "tag what I mean", never a different key.
  t: (t) => (marked.size ? $("#bulktag").focus() : t.el.querySelector(".taginput").focus()),
  r: (t) => {
    const name = t.el.querySelector(".name");
    name.focus();
    selectAll(name);
  },
  // Same rule as t and x: the marked set when there is one, else the current.
  e: (t) => (marked.size ? describeIds([...marked]) : t.el.querySelector(".describe").click()),
};

addEventListener("keydown", (e) => {
  const typing = isTyping(e.target);

  // Escape backs out one layer at a time rather than resetting everything, so
  // leaving a suggestion list doesn't also wipe the search you were refining.
  if (e.key === "Escape") {
    if (!help.hidden) return closeHelp();
    if (!dupePanel.hidden) return closeDupes();
    if (!libPanel.hidden) return closeLibrary();
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

// Arriving from the bookmarklet. Two shapes, because an older bookmark saved
// before this existed still sends ?add=<page url> and should keep working.
function handleAddParam() {
  const params = new URLSearchParams(location.search);
  const url = params.get("add");
  if (!url) return;
  // Clean it out of the address bar first, so a refresh does not add it twice
  // and the URL is not left carrying someone else's link.
  params.delete("add");
  const rest = params.toString();
  history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : ""));
  if (/^https?:\/\//i.test(url)) grab(url);
  else toast("that bookmark did not carry a usable link");
}

const looksLikeVideo = (url) => /\.(mp4|webm)(\?|$)/i.test(url);

// The current bookmarklet: everything it found in the page, plus the page
// itself, in the fragment.
async function handleAddFragment() {
  if (!location.hash.startsWith("#add=")) return;
  let payload;
  try {
    payload = JSON.parse(decodeURIComponent(location.hash.slice(5)));
  } catch {
    return toast("that bookmark sent something unreadable");
  }
  history.replaceState(null, "", location.pathname + location.search);

  toast("looking…");
  const candidates = [];
  const seen = new Set();

  // The server first, so its platform-specific handling wins: it reaches
  // Reddit comments the rendered page has not loaded.
  if (typeof payload.page === "string" && /^https?:\/\//i.test(payload.page)) {
    try {
      const res = await fetch("/api/fetch/discover", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: payload.page }),
      });
      if (res.ok) {
        for (const c of (await res.json()).candidates || []) {
          if (!seen.has(c.url)) {
            seen.add(c.url);
            candidates.push(c);
          }
        }
      }
    } catch {
      // A page the server cannot read is the normal case here, not an error:
      // it is exactly why the bookmarklet also looks at the DOM.
    }
  }

  for (const url of payload.urls || []) {
    if (typeof url === "string" && /^https?:\/\//i.test(url) && !seen.has(url)) {
      seen.add(url);
      candidates.push({ url, kind: looksLikeVideo(url) ? "video" : "gif", title: "" });
    }
  }

  if (!candidates.length) return toast("nothing to import from that page");
  if (candidates.length === 1) return importUrls(candidates);
  openPicker(candidates);
}

load();
pollJobs();
handleAddParam();
handleAddFragment();
// Changing only the fragment does not reload the document, so a bookmarklet
// press landing on an already-open gifhole would otherwise do nothing at all.
addEventListener("hashchange", handleAddFragment);
