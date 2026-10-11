// Read-only helpers the bot evaluates inside the game page (window.__pkbot).
// They look at the same state a player sees and plan paths; they never change game state.
(() => {
  const G = () => window.__pk;
  const X = () => window.__pkx || {};
  const B = { ver: 15 };

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
    out.swim = !!(p.swimming || p.submerged);   // swimming = floating in water, submerged = head under
    out.ground = !!p.onGround;
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
                   emote: o.emote, block: o.block, trainer: o.trainer, consume: !!o.consume,
                   have: st && st.progress ? st.progress[i] : 0 })) };
      });
    } catch (e) { out.quests = []; }
    out.inv = s.inv.map((x, i) => (x ? { i, id: x.id, n: x.n } : null)).filter(Boolean);
    out.invCap = s.inv.length;
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
  // Ground blocks around the player the hoe can till (game rule farm.canTill, not protected).
  B.tillable = (r) => {
    const g = G(); const p = g.player.pos; const out = [];
    const px = Math.floor(p.x), pz = Math.floor(p.z), py = Math.floor(p.y);
    r = r || 4;
    for (let x = px - r; x <= px + r; x++) for (let z = pz - r; z <= pz + r; z++) {
      if (x === px && z === pz) continue;
      for (let y = py - 2; y <= py; y++) {
        if (g.build.isProtected(x, z)) continue;
        if (W().get(x, y + 1, z) !== 0) continue;
        if (g.farm.canTill(x, y, z)) { out.push({ x, y, z, d: Math.hypot(x + 0.5 - p.x, z + 0.5 - p.z) }); break; }
      }
    }
    return out.sort((a, b) => a.d - b.d);
  };
  // Pick-up decorations (flowers, mushrooms, apples, shells ...) still lying in the world, with the
  // item each one gives (game act.decorItem), nearest first.
  B.decorFind = (item, r) => {
    const g = G(); const p = g.player.pos; const grid = g.decor.grid; const w = Math.round(Math.sqrt(grid.length));
    const cell = 384 / w; r = r || 60; const out = [];
    const c0 = Math.max(0, Math.floor((p.x - r) / cell)), c1 = Math.min(w - 1, Math.floor((p.x + r) / cell));
    const r0 = Math.max(0, Math.floor((p.z - r) / cell)), r1 = Math.min(w - 1, Math.floor((p.z + r) / cell));
    for (let rz = r0; rz <= r1; rz++) for (let cx = c0; cx <= c1; cx++) {
      for (const a of grid[cx + w * rz]) {
        if (a.state !== 0) continue;
        let it = null; try { it = g.act.decorItem(a.d); } catch (e) { it = null; }
        if (!it || (item && it !== item)) continue;
        const d = Math.hypot(a.d.x - p.x, a.d.z - p.z);
        if (d <= r) out.push({ item: it, kind: a.kind, x: a.d.x, y: a.d.y, z: a.d.z, d });
      }
    }
    return out.sort((a, b) => a.d - b.d).slice(0, 40);
  };
  // Bugs flying around (game bugs.live), nearest first.
  B.bugs = (r) => {
    const g = G(); const p = g.player.pos; r = r || 50;
    return g.bugs.live.filter((b) => !b.leaving && b.alpha >= 0.4)
      .map((b) => ({ id: b.def.id, h: b.handle, x: b.x, y: b.y, z: b.z, d: Math.hypot(b.x - p.x, b.z - p.z) }))
      .filter((b) => b.d <= r).sort((a, b) => a.d - b.d);
  };

  // Would a net swing catch something right now? Same check as the game's swingNet: bugs within
  // 3 m of the point 1.6 m in front of the body (player.forward, not the camera) at chest height.
  B.netReach = () => {
    const p = G().player; const f = p.forward();
    return G().bugs.nearby(p.pos.x + f.x * 1.6, p.pos.y + 1, p.pos.z + f.z * 1.6, 3).length;
  };

  // Fishing state as the game holds it (state, reel marker phase/speed and the green zone).
  B.fishState = () => {
    const f = G().fishing; const r = f.reelObj;
    return { state: f.state, phase: f.phase, speed: f.speed, zone: [r.zone[0], r.zone[1]], pos: r.pos,
             hits: f.hits, misses: r.misses, msg: f.view.message, habitat: f.view.habitat, t: performance.now() };
  };
  // A dry standing spot near water: open water 5..9 blocks away in one of 8 directions.
  B.fishSpot = (r) => {
    const g = G(); const p = g.player.pos; r = r || 40;
    const px = Math.floor(p.x), pz = Math.floor(p.z), py = Math.floor(p.y);
    const deep = (x, z) => { const wl = B.waterLevel(x, z); if (wl <= 0) return false; return wl - W().groundBelow(x + 0.5, z + 0.5, Math.floor(wl) + 1) > 0.8; };
    let best = null;
    const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1], [0.71, 0.71], [0.71, -0.71], [-0.71, 0.71], [-0.71, -0.71]];
    for (let x = px - r; x <= px + r; x += 1) for (let z = pz - r; z <= pz + r; z += 1) {
      const d0 = Math.hypot(x - px, z - pz); if (d0 > r || (best && d0 >= best.d)) continue;
      if (B.waterLevel(x, z) > 0 && deep(x, z)) continue;          // stand on dry ground
      const sy = B.stand(x, z, py); if (sy === null) continue;
      for (const [dx, dz] of dirs) {
        let ok = true;
        for (let k = 5; k <= 9; k++) if (!deep(Math.floor(x + dx * k), Math.floor(z + dz * k))) { ok = false; break; }
        if (!ok) continue;
        const wx = x + 0.5 + dx * 7, wz = z + 0.5 + dz * 7;
        best = { x: x + 0.5, z: z + 0.5, y: sy, wx, wz, wy: B.waterLevel(Math.floor(wx), Math.floor(wz)), d: d0 };
        break;
      }
    }
    return best;
  };

  // Farming: empty farmland near the player, and our crops with their growth stage (>=3 ripe).
  B.farmland = (r) => {
    const g = G(); const p = g.player.pos; const out = []; r = r || 4;
    const px = Math.floor(p.x), pz = Math.floor(p.z), py = Math.floor(p.y);
    const FL = X().F ? X().F.Farmland : 26;
    for (let x = px - r; x <= px + r; x++) for (let z = pz - r; z <= pz + r; z++)
      for (let y = py - 2; y <= py; y++) {
        if (W().get(x, y, z) !== FL) continue;
        if (g.farm.cropOn(x, y, z)) continue;
        out.push({ x, y, z, d: Math.hypot(x + 0.5 - p.x, z + 0.5 - p.z) });
      }
    return out.sort((a, b) => a.d - b.d);
  };
  B.crops = () => {
    const g = G(); const p = g.player.pos; const now = Date.now();
    return (g.save.crops || []).map((c) => ({ x: c.x, y: c.y, z: c.z, crop: c.crop,
      stage: g.farm.stageOf(c, now), d: Math.hypot(c.x + 0.5 - p.x, c.z + 0.5 - p.z) })).sort((a, b) => a.d - b.d);
  };

  // Nearest open ground (outside protected areas) with at least `need` tillable patches around it.
  B.findTillSpot = (r, need) => {
    const g = G(); const p = g.player.pos; const w = W();
    r = r || 60; need = need || 6;
    const px = Math.floor(p.x), pz = Math.floor(p.z);
    const ok = (x, z) => {
      if (g.build.isProtected(x, z)) return null;
      const top = w.topY(x, z); const y = top - 1;
      if (y < 2 || w.get(x, y + 1, z) !== 0) return null;
      return g.farm.canTill(x, y, z) ? y : null;
    };
    for (let ring = 2; ring <= r; ring += 2) {
      for (let dx = -ring; dx <= ring; dx += 2) for (const dz of [-ring, ring]) {
        for (const [x, z] of [[px + dx, pz + dz], [px + dz, pz + dx]]) {
          if (ok(x, z) === null) continue;
          let n = 0;
          for (let ax = -2; ax <= 2; ax++) for (let az = -2; az <= 2; az++) if (ok(x + ax, z + az) !== null) n++;
          if (n >= need) return { x: x + 0.5, z: z + 0.5, n };
        }
      }
    }
    return null;
  };
  B.isProtected = (x, z) => !!G().build.isProtected(x, z);
  B.waterLevel = (x, z) => { try { return W().waterAt(x + 0.5, z + 0.5); } catch (e) { return -1; } };

  // A* over columns. Walking may step up 1 block (auto-jump) and drop 4; water is swimmable.
  B.path = (tx, tz, opt) => {
    opt = opt || {};
    const g = G(); const p = g.player.pos;
    const sx = Math.floor(p.x), sz = Math.floor(p.z);
    // swimming: the feet are under water, plan from the water surface the body floats at
    const wl0 = B.waterLevel(sx, sz);
    const sy = (g.player.swimming || g.player.submerged) && wl0 > p.y ? Math.floor(wl0) : Math.round(p.y);
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
      if (x < 1 || z < 1 || x > 382 || z > 382) return null;
      const wl = B.waterLevel(x, z);
      if (wl > 0) {
        const ground = W().groundBelow(x + 0.5, z + 0.5, Math.floor(wl) + 1);
        if (wl > ground + 0.4) {               // deep water here: swim at the surface
          const sy2 = Math.floor(wl);
          // same reach rule as land: up 1, down 4 (a pool on top of a cliff is not reachable)
          if (sy2 > y0 + 1 || sy2 < y0 - 4) return null;
          if (W().get(x, sy2, z) !== 0 || W().get(x, sy2 + 1, z) !== 0) return null;
          return { y: sy2, swim: true };
        }
      }
      const st = B.stand(x, z, y0);
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
    // all actors: far ones are just not rendered (visible=false) but they are there
    return g.npcs.actors.map((a) => ({
      id: a.def.id, name: a.def.name, title: a.def.title || '', trainer: !!a.def.trainer, shown: a.visible !== false,
      state: typeof a.state === 'string' ? a.state : null,                 // "inside" = at home (schedule)
      wake: a.sched && !a.sched.always ? a.sched.a : null,                 // hour the NPC comes out
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
  // Item definition facts the bag manager needs (game item table).
  B.itemDef = (id) => { try { const d = X().Ag(id); return d ? { id, kind: d.kind, sell: d.sell ?? null, museum: !!d.museum,
    treat: d.treat ?? null, heal: d.heal ?? null, name: d.name } : null; } catch (e) { return null; } };
  // favourite treat per species (game table _h) and the treat value of an item
  B.favourite = (species) => (X()._h || {})[species] || null;
  B.treatValue = (item) => { try { const d = X().Ag(item); return d ? { kind: d.kind, treat: d.treat ?? null } : null; } catch (e) { return null; } };
  // A place can only be "discovered" (quest visit objectives) if the game lists it in its place
  // table Kh; in this game version the mainland villages/caves/snow are missing from it.
  B.discoverable = (id) => !!(X().Kh && X().Kh[id]);
  B.places = () => G().world.places.map((pl) => ({ id: pl.id, name: (X().Kh && X().Kh[pl.id] && X().Kh[pl.id].name) || pl.name, x: pl.x, z: pl.z }));
  B.props = () => { const g = G(); const p = g.player.pos; return g.props.interactables().map((i) => ({ kind: i.kind, id: i.id, x: i.x, y: i.y, z: i.z, d: Math.hypot(i.x - p.x, i.z - p.z) })).sort((a, b) => a.d - b.d).slice(0, 30); };
  B.buddy = () => { const b = G().creatures.buddy; return b ? { x: b.pos.x, y: b.pos.y, z: b.pos.z } : null; };

  // camera ray (origin + direction) so the bot can aim like a player
  B.ray = () => { const r = G().cam.ray(); return { o: [r.o.x, r.o.y, r.o.z], d: [r.d.x, r.d.y, r.d.z] }; };
  B.hit = () => { const h = G().act.hit; return h ? { x: h.x, y: h.y, z: h.z, id: h.id, t: h.t, name: B.blockName(h.id) } : null; };

  // Quests the game would offer right now (quests.available: requires met, not taken yet,
  // repeatables cooled down), per giver NPC that is in this world.
  B.offers = () => {
    const g = G(); const p = g.player.pos; const out = [];
    for (const a of g.npcs.actors) {
      let av = [];
      try { av = g.quests.available(a.def.id) || []; } catch (e) { av = []; }
      for (const q of av) out.push({ id: q.id, title: q.title, giver: a.def.id, name: a.def.name,
        kinds: (q.objectives || []).map((o) => o.kind), x: a.pos.x, z: a.pos.z, d: Math.hypot(a.pos.x - p.x, a.pos.z - p.z) });
    }
    return out.sort((a, b) => a.d - b.d);
  };
  B.boardOffers = () => { try { return (G().quests.available('board') || []).map((q) => ({ id: q.id, title: q.title, kinds: (q.objectives || []).map((o) => o.kind) })); } catch (e) { return []; } };

  // ------------------------------------------------------------------ dialogue graph
  // Each NPC's talk is a graph of nodes; every choice carries the game's own actions
  // (giveQuest, turnIn, heal, openShop, battle ...). Map the choices on screen to their node and
  // report what each one does, directly and further down its branch.
  const acts = (a) => (Array.isArray(a) ? a : a ? [a] : []).map((x) => x.do + (x.quest ? ':' + x.quest : x.shop ? ':' + x.shop : x.trainer ? ':' + x.trainer : x.to ? ':' + x.to : ''));
  B.dlgPlan = () => {
    const g = G(); const def = g.talk && g.talk.npc; const d = g.menus.dlg;
    if (!def || !d || !d.cur || !def.nodes) return null;
    const shown = (d.cur.n.choices || []).map((c) => c.label);
    if (!shown.length) return { npc: def.id, choices: [] };
    const dl = g.dialogue;
    const byId = new Map(def.nodes.map((n) => [n.id, n]));
    const visible = (n) => (n.choices || []).filter((c) => { try { return dl.cond(c.if); } catch (e) { return true; } });
    // several nodes share the same answers ("Count me in!" / "Not right now"), so the line the
    // NPC is saying must match too
    const said = d.cur.full;
    let node = null, fallback = null;
    for (const n of def.nodes) {
      const vc = visible(n);
      if (vc.length !== shown.length) continue;
      if (!vc.every((c, i) => dl.fill(c.label, def) === shown[i])) continue;
      if (n.text !== undefined && dl.fill(n.text, def) === said) { node = n; break; }
      if (!fallback) fallback = n;
    }
    if (!node) {
      // the text did not match exactly (e.g. it changes with the hour): only trust a unique match
      const same = def.nodes.filter((n) => { const vc = visible(n); return vc.length === shown.length && vc.every((c, i) => dl.fill(c.label, def) === shown[i]); });
      node = same.length === 1 ? same[0] : null;
    }
    if (!node) return { npc: def.id, choices: shown.map((l) => ({ label: l, acts: [], reach: [], ends: null })) };
    // shortest distance from a choice to each action down its branch; never re-enters the
    // conversation's root nodes (that is a loop back to the start) and does not walk through
    // choices with side effects (gift / photo / sleep / ferry / battle / shop).
    const roots = new Set((def.roots || []).map((r) => r.node));
    // paying for what we came for (heal fee, quest items) is fine: the game disables a choice
    // we cannot afford; scenes like gift/photo/sleep/ferry/battle/menus are only taken on purpose
    const RISKY = /^(gift|photo|sleep|ferry|battle|openShop|openMuseum|openBox|openBank|openBounties)/;
    const reach = (start, maxDepth) => {
      const out = {}; const seen = new Set(); const q = [[start, 1]];
      while (q.length) {
        const [id, dd] = q.shift();
        if (id === undefined || seen.has(id) || dd > maxDepth || roots.has(id)) continue;
        seen.add(id);
        const n = byId.get(id); if (!n) continue;
        acts(n.action).forEach((a) => { if (!(a in out)) out[a] = dd; });
        for (const c of visible(n)) {
          const ca = acts(c.action);
          ca.forEach((a) => { if (!(a in out)) out[a] = dd; });
          if (!ca.some((a) => RISKY.test(a))) q.push([c.next, dd + 1]);
        }
        if (n.next !== undefined) q.push([n.next, dd + 1]);
      }
      return out;
    };
    return { npc: def.id, node: node.id, choices: visible(node).map((c) => {
      const a = acts(c.action);
      // a side effect can also sit on the node the answer leads to (e.g. the photo scene)
      const nx = c.next !== undefined ? byId.get(c.next) : null;
      const na = nx ? acts(nx.action) : [];
      const risky = a.some((x) => RISKY.test(x)) || na.some((x) => RISKY.test(x));
      return { label: dl.fill(c.label, def), acts: a.concat(na), risky,
               reach: risky ? {} : reach(c.next, 5), ends: c.next === undefined };
    }) };
  };

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
