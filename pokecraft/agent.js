// Read-only helpers the bot evaluates inside the game page (window.__pkbot).
// They look at the same state a player sees and plan paths; they never change game state.
(() => {
  const G = () => window.__pk;
  const X = () => window.__pkx || {};
  const B = { ver: 3 };

  B.blockName = (id) => { const d = X().Du && X().Du[id]; return d ? d.name : String(id); };
  B.blockIds = (re) => {
    const out = []; const du = X().Du || {};
    for (const k of Object.keys(du)) { const d = du[k]; if (d && d.name && re.test(d.name)) out.push(Number(k)); }
    return out;
  };
  B.itemName = (id) => { try { return X().jg ? X().jg(id) : id; } catch (e) { return id; } };

  // ------------------------------------------------------------------ state
  B.state = () => {
    const g = G();
    if (!g) return { phase: 'nohook' };
    const out = { phase: g.phase, playing: !!g.playing, busy: !!g.busy, battle: !!g.battleOn,
                  menus: !!(g.menus && g.menus.isOpen), typing: !!g.typing, photo: !!g.photo,
                  build: !!g.buildMode };
    const p = g.player, s = g.save;
    if (!s || !p || !g.playing) return out;
    out.pos = { x: p.pos.x, y: p.pos.y, z: p.pos.z };
    out.yaw = g.cam.yaw; out.pitch = g.cam.pitch;
    out.swim = !!p.submerged;
    out.lvl = s.lvl; out.xp = s.xp; out.gems = s.gems; out.hour = g.clock.hour;
    const d = g.menus.dlg;
    if (d && !d.dead && d.cur) {
      out.dlg = { speaker: d.cur.n.speaker || '', text: d.cur.full, typing: !!d.cur.typing,
                  choices: (d.cur.n.choices || []).map((c) => ({ label: c.label, off: !!c.disabled })) };
    }
    out.layers = g.menus.layers.filter((l) => !l.dead).map((l) => (l.el && l.el.className) || '?');
    out.prompt = g.act.lastPrompt || '';
    const f = g.act.focus;
    if (f) {
      out.focus = { k: f.k };
      if (f.a && f.a.def) { out.focus.id = f.a.def.id; out.focus.name = f.a.def.name; }
      if (f.c) { out.focus.species = f.c.species; out.focus.level = f.c.level; out.focus.cid = f.c.id; }
      if (f.p) { out.focus.prop = f.p.kind; out.focus.pid = f.p.id; }
      if (f.st) out.focus.station = f.st;
    }
    try {
      out.journey = g.journey.state();
      const t = g.journey.point();
      if (t) out.jtarget = { x: t.x, z: t.z, label: t.label || '' };
    } catch (e) { /* no journey */ }
    try {
      out.quests = g.quests.active().map((q) => {
        const st = s.quests[q.id];
        let lines = [];
        try { lines = g.quests.describe(q, st).map((x) => ({ text: x.text, done: !!x.done })); } catch (e) { /**/ }
        return { id: q.id, title: q.title, phase: st && st.phase, giver: q.giver || null,
                 turnInTo: q.turnInTo || null, lines,
                 objs: (q.objectives || []).map((o, i) => ({ kind: o.kind, n: o.n, item: o.item, alt: o.alt,
                   npc: o.npc, place: o.place, fish: o.fish, species: o.species, what: o.what,
                   have: st && st.progress ? st.progress[i] : 0 })) };
      });
    } catch (e) { out.quests = []; }
    out.inv = s.inv.map((x, i) => (x ? { i, id: x.id, n: x.n } : null)).filter(Boolean);
    out.sel = s.sel; out.held = g.heldTool ? g.heldTool() : null;
    const z1 = X().z1;
    out.party = s.party.map((c) => ({ uid: c.uid, species: c.species, level: c.level, hp: c.hp,
                                      max: z1 ? z1(c) : null, moves: c.moves, bond: c.bond }));
    out.box = s.box.length;
    out.fishing = g.fishing ? g.fishing.state : null;
    out.flags = { worldOpen: !!(s.flags && s.flags['open.world']) };
    return out;
  };

  // ------------------------------------------------------------------ world queries
  const W = () => G().world;
  const solid = (x, y, z) => W().get(x, y, z) !== 0;

  // Standing height on column (x,z) close to yHint, or null.
  B.stand = (x, z, yHint) => {
    const w = W();
    if (x < 1 || z < 1 || x > 382 || z > 382) return null;
    for (let y = Math.min(70, yHint + 2); y >= Math.max(1, yHint - 5); y--) {
      if (w.get(x, y - 1, z) !== 0 && w.get(x, y, z) === 0 && w.get(x, y + 1, z) === 0) return y;
    }
    return null;
  };
  // A block the player may break here (game rule build.canBreak: in bounds, y>=2, not a
  // protected village/landmark, not a locked chest) and that is natural terrain, not a build.
  const NATURAL = /grass|dirt|stone$|^stone|cobble|sand|gravel|clay|snow|ice|leaves|log$|ore|mud|moss|tall|bush|fern|flower|mushroom/i;
  const NEVER = /door|chest|workbench|furnace|planter|lantern|lamp|window|glass|brick|plank|fence|sign|bed|barrel|crate|cloth|tile|plaster/i;
  B.canBreak = (x, y, z) => {
    const g = G(); const id = W().get(x, y, z);
    if (!id) return { ok: false, air: true };
    const name = B.blockName(id);
    const rule = !!(g.build && g.build.canBreak(x, y, z));
    return { ok: rule && NATURAL.test(name) && !NEVER.test(name), rule, name, id };
  };
  B.waterLevel = (x, z) => { try { return W().waterAt(x + 0.5, z + 0.5); } catch (e) { return -1; } };

  // A* over columns. Walking may step up 1 block (auto-jump) and drop 4; water is swimmable.
  B.path = (tx, tz, opt) => {
    opt = opt || {};
    const g = G(); const p = g.player.pos;
    const sx = Math.floor(p.x), sz = Math.floor(p.z), sy = Math.round(p.y);
    tx = Math.floor(tx); tz = Math.floor(tz);
    const near = opt.near || 1.2, maxN = opt.max || 90000, swimCost = opt.swimCost || 4;
    const key = (x, z) => x * 512 + z;
    const avoid = new Set((opt.avoid || []).map(([ax, az]) => key(ax, az)));
    const open = [[0, sx, sz, sy, 0]];
    const gs = new Map([[key(sx, sz), 0]]);
    const from = new Map();
    const yAt = new Map([[key(sx, sz), sy]]);
    const swimAt = new Map();
    let best = null, bestH = 1e9, n = 0;
    const H = (x, z) => Math.hypot(tx - x, tz - z);
    const push = (a) => { open.push(a); let i = open.length - 1; while (i > 0) { const j = (i - 1) >> 1; if (open[j][0] <= open[i][0]) break; [open[i], open[j]] = [open[j], open[i]]; i = j; } };
    const pop = () => { const top = open[0]; const last = open.pop(); if (open.length) { open[0] = last; let i = 0; for (;;) { const l = 2 * i + 1, r = l + 1; let m = i; if (l < open.length && open[l][0] < open[m][0]) m = l; if (r < open.length && open[r][0] < open[m][0]) m = r; if (m === i) break; [open[i], open[m]] = [open[m], open[i]]; i = m; } } return top; };
    const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
    const cell = (x, z, y0) => {
      const wl = B.waterLevel(x, z);
      const st = B.stand(x, z, Math.max(y0, Math.floor(wl)));
      if (wl > 0 && (st === null || wl > st + 0.4)) {
        const sy2 = Math.floor(wl);
        if (W().get(x, sy2, z) !== 0 || W().get(x, sy2 + 1, z) !== 0) return null;
        return { y: sy2, swim: true };
      }
      if (st === null) return null;
      if (st > y0 + 1 || st < y0 - 4) return null;
      return { y: st, swim: false };
    };
    while (open.length && n < maxN) {
      const [, x, z, y, gc] = pop(); n++;
      const k = key(x, z);
      if (gc > (gs.get(k) ?? 1e18)) continue;
      const h = H(x, z);
      if (h < bestH) { bestH = h; best = k; }
      if (h <= near) { best = k; bestH = h; break; }
      for (const [dx, dz] of dirs) {
        const nx = x + dx, nz = z + dz;
        if (avoid.has(key(nx, nz))) continue;
        const c = cell(nx, nz, y);
        if (!c) continue;
        if (dx && dz) {   // no corner cutting
          const a = cell(x + dx, z, y), b2 = cell(x, z + dz, y);
          if (!a || !b2) continue;
        }
        // the body is wider than a block: avoid 1-wide slots between two walls
        const side1 = cell(nx + dz, nz + dx, y), side2 = cell(nx - dz, nz - dx, y);
        const squeeze = !side1 && !side2 ? 3 : 0;
        const step = (dx && dz ? 1.414 : 1) * (c.swim ? swimCost : 1) + (c.y > y ? 0.6 : 0) + squeeze;
        const ng = gc + step, nk = key(nx, nz);
        if (ng < (gs.get(nk) ?? 1e18)) {
          gs.set(nk, ng); from.set(nk, k); yAt.set(nk, c.y); swimAt.set(nk, c.swim);
          push([ng + H(nx, nz), nx, nz, c.y, ng]);
        }
      }
    }
    if (best === null) return { ok: false, n };
    const pts = [];
    for (let k = best; k !== undefined; k = from.get(k)) {
      pts.push([Math.floor(k / 512) + 0.5, (k % 512) + 0.5, yAt.get(k), !!swimAt.get(k)]);
      if (k === key(sx, sz)) break;
    }
    pts.reverse();
    // keep only turning points (and swim/height changes)
    const out = [];
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i], c = pts[i + 1];
      if (!a || !c) { out.push(b); continue; }
      const d1x = b[0] - a[0], d1z = b[1] - a[1], d2x = c[0] - b[0], d2z = c[1] - b[1];
      if (d1x !== d2x || d1z !== d2z || a[2] !== b[2] || b[3] !== c[3] || i % 6 === 0) out.push(b);
    }
    return { ok: bestH <= near + 0.5, reach: bestH, n, pts: out };
  };

  // Logs of trees near the player: [{x,y,z,d}] trunk blocks with a free spot beside them.
  B.trees = (r) => {
    r = r || 24;
    const g = G(); const p = g.player.pos; const w = W();
    const logs = new Set(B.blockIds(/log$/i));
    const out = [];
    const px = Math.floor(p.x), pz = Math.floor(p.z), py = Math.floor(p.y);
    for (let x = px - r; x <= px + r; x++) for (let z = pz - r; z <= pz + r; z++) {
      for (let y = Math.max(1, py - 6); y <= py + 10; y++) {
        const id = w.get(x, y, z);
        if (!logs.has(id)) continue;
        if (logs.has(w.get(x, y - 1, z))) continue;          // only the bottom log
        if (g.build && g.build.isProtected && g.build.isProtected(x, z)) continue;
        out.push({ x, y, z, d: Math.hypot(x - p.x, z - p.z), id, name: B.blockName(id) });
        break;
      }
    }
    out.sort((a, b) => a.d - b.d);
    return out.slice(0, 20);
  };

  B.findBlocks = (re, r) => {
    r = r || 40;
    const g = G(); const p = g.player.pos; const w = W();
    const ids = new Set(B.blockIds(re));
    const out = [];
    const px = Math.floor(p.x), pz = Math.floor(p.z), py = Math.floor(p.y);
    for (let x = px - r; x <= px + r; x++) for (let z = pz - r; z <= pz + r; z++)
      for (let y = Math.max(1, py - 8); y <= py + 8; y++) {
        const id = w.get(x, y, z);
        if (ids.has(id)) out.push({ x, y, z, d: Math.hypot(x - p.x, z - p.z), name: B.blockName(id) });
      }
    out.sort((a, b) => a.d - b.d);
    return out.slice(0, 30);
  };

  B.npcs = () => {
    const g = G(); const p = g.player.pos;
    return g.npcs.actors.filter((a) => a.visible !== false).map((a) => ({
      id: a.def.id, name: a.def.name, title: a.def.title || '', trainer: !!a.def.trainer,
      x: a.pos.x, y: a.pos.y, z: a.pos.z, d: Math.hypot(a.pos.x - p.x, a.pos.z - p.z),
    })).sort((a, b) => a.d - b.d);
  };
  B.npcDefs = () => {
    // every npc definition the world knows (also ones far away / not spawned yet)
    const g = G();
    return g.npcs.actors.map((a) => ({ id: a.def.id, name: a.def.name, home: a.def.home || null }));
  };
  B.wilds = (r) => {
    const g = G(); const p = g.player.pos; r = r || 60;
    return g.creatures.list.filter((c) => c !== g.creatures.buddy && c.wild !== false && c.species)
      .map((c) => ({ id: c.id, species: c.species, level: c.level, x: c.pos.x, y: c.pos.y, z: c.pos.z,
                     d: Math.hypot(c.pos.x - p.x, c.pos.z - p.z), kind: c.kind || '' }))
      .filter((c) => c.d <= r).sort((a, b) => a.d - b.d);
  };
  // favourite treat per species (game table _h) and the treat value of an item
  B.favourite = (species) => (X()._h || {})[species] || null;
  B.treatValue = (item) => { try { const d = X().Ag(item); return d ? { kind: d.kind, treat: d.treat ?? null } : null; } catch (e) { return null; } };
  B.places = () => G().world.places.map((pl) => ({ id: pl.id, name: (X().Kh && X().Kh[pl.id] && X().Kh[pl.id].name) || pl.name, x: pl.x, z: pl.z }));
  B.props = () => { const g = G(); const p = g.player.pos; return g.props.interactables().map((i) => ({ kind: i.kind, id: i.id, x: i.x, y: i.y, z: i.z, d: Math.hypot(i.x - p.x, i.z - p.z) })).sort((a, b) => a.d - b.d).slice(0, 30); };
  B.buddy = () => { const b = G().creatures.buddy; return b ? { x: b.pos.x, y: b.pos.y, z: b.pos.z } : null; };

  // camera ray (origin + direction) so the bot can aim like a player
  B.ray = () => { const r = G().cam.ray(); return { o: [r.o.x, r.o.y, r.o.z], d: [r.d.x, r.d.y, r.d.z] }; };
  B.hit = () => { const h = G().act.hit; return h ? { x: h.x, y: h.y, z: h.z, id: h.id, t: h.t, name: B.blockName(h.id) } : null; };

  // ------------------------------------------------------------------ UI
  // Visible buttons of the top-most menu layer, with their screen rectangles.
  B.buttons = (all) => {
    const g = G();
    const layers = g.menus.layers.filter((l) => !l.dead);
    const root = all ? document : (layers.length ? layers[layers.length - 1].el : document.body);
    const out = [];
    root.querySelectorAll('button, [data-fid], [role=menuitem], [role=tab]').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return;
      out.push({ text: (el.innerText || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80),
                 fid: el.dataset ? el.dataset.fid || '' : '', off: el.disabled || el.getAttribute('aria-disabled') === 'true',
                 x: r.x + r.width / 2, y: r.y + r.height / 2, cls: String(el.className).slice(0, 60) });
    });
    return out;
  };
  B.text = (sel) => { const el = document.querySelector(sel); return el ? el.innerText : null; };
  B.toasts = () => Array.from(document.querySelectorAll('.mgm-toasts > *, .toast, [class*=toast]')).map((e) => e.innerText.trim()).filter(Boolean).slice(-6);

  window.__pkbot = B;
  return B.ver;
})();
