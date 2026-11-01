"""Aiden Fork — import the WHOLE Ariane map into Blender.

Flow
----
1. Blender (Ariane tab → «Import WHOLE MAP») drops ``<bridge>/outbox/fullmap.req``
   with the chosen options. Ariane (Aiden Fork build) can also start the export
   itself from its «Aiden Fork QOL» panel.
2. Ariane walks EVERY placed instance, writes each model's DFF / TXD / COL once
   into ``<bridge>/inbox/fullmap_<ts>/`` and lists every placement in
   ``<bridge>/inbox/fullmap_<ts>.mapjob`` (one TAB-separated line per instance:
   model, IDE id, guid, draw distance, txd, position, rotation, IPL, LOD/night
   flags, LOD link …).
3. The bridge watcher hands ``*.mapjob`` files to :func:`enqueue_mapjob`. Every
   unique model is imported ONCE (with its textures); every further placement is a
   linked duplicate (shared mesh data) — fast and light even for ~40k instances.

Scene layout (stable names, meant to be readable by people and LLMs alike)::

    GTA Map
    ├── Map            HD models, one sub-collection per IPL (e.g. "LAe")
    ├── LOD            LOD models, one sub-collection per IPL ("LAe_LOD")
    ├── Night Lights   night-only timed models (neon / lit windows) per IPL
    │   └── 2DFX Lights   2DFX light / corona empties of every model
    ├── Interiors      interior areas (optional)
    └── Collision      COL meshes (optional)

Every object carries custom properties: ``gta_model``, ``gta_guid`` (``<ipl>#<index>``),
``gta_ipl``, ``gta_role`` (HD / LOD / NIGHT / INTERIOR / 2DFX / COL), ``gta_model_id``,
``gta_txd``, ``gta_draw_distance``, ``gta_lod_guid`` (HD → its LOD instance),
``gta_area`` and time-object data. A ``GTA_MAP_README`` text block documents it and
``GTA_MAP_MANIFEST.json`` lists every placement.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import bpy


# ── state ────────────────────────────────────────────────────────

_pending_jobs = []          # mapjob paths waiting for the current import to finish
_st = None                  # active import state (dict) or None

_STAGING = "_gta_fullmap_staging"
_FX_COLL = "2DFX"           # collection dff_import drops 2DFX empties into


def is_running() -> bool:
    return _st is not None


def progress():
    """(done, total, label) of the running import, or None."""
    if _st is None:
        return None
    return (_st['done'], _st['total'], _st.get('phase', ''))


def last_summary() -> str:
    return _last_summary[0]


_last_summary = [""]


# ── request (Blender → Ariane) ───────────────────────────────────

def request_full_map(settings) -> str:
    """Write outbox/fullmap.req for Ariane. Returns the request path."""
    from .ariane_bridge import bridge_base
    out = os.path.join(bridge_base(), 'outbox')
    os.makedirs(out, exist_ok=True)
    opts = {
        'lod': settings.ariane_fm_lod,
        'night': settings.ariane_fm_night,
        'col': settings.ariane_fm_col,
        'interiors': settings.ariane_fm_interiors,
        'auto_txd': settings.ariane_fm_textures,
        'visible_only': settings.ariane_fm_visible_only,
        'vanilla': True,
    }
    line = " ".join(f"{k}={1 if v else 0}" for k, v in opts.items()) + "\n"
    path = os.path.join(out, 'fullmap.req')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(line)
    os.replace(tmp, path)
    return path


def ariane_status():
    """Ariane's export progress from bridge/fullmap_status.txt:
    (state, done, total, placed, age_seconds) or None."""
    from .ariane_bridge import bridge_base
    path = os.path.join(bridge_base(), 'fullmap_status.txt')
    try:
        st = os.stat(path)
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            parts = fh.read().split()
        state = parts[0]
        nums = [int(x) for x in parts[1:4]]
        while len(nums) < 3:
            nums.append(0)
        return (state, nums[0], nums[1], nums[2], time.time() - st.st_mtime)
    except (OSError, ValueError, IndexError):
        return None


# ── job parsing ──────────────────────────────────────────────────

def _parse_kv(tokens):
    out = {}
    for tok in tokens:
        if '=' in tok:
            k, v = tok.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def _read_mapjob(path):
    header = {}
    flags = {}
    entries = []
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for ln in fh:
            ln = ln.rstrip('\r\n')
            if not ln.strip():
                continue
            if ln.startswith('#'):
                if ln.startswith('#fullmap'):
                    header = _parse_kv(ln.split()[1:])
                elif ln.startswith('#flags'):
                    flags = {k: (v == '1') for k, v in _parse_kv(ln.split()[1:]).items()}
                continue
            parts = ln.split('\t')
            meta = {}
            for kv in parts[1:]:
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    meta[k.strip()] = v.strip()
            meta['model'] = os.path.splitext(os.path.basename(parts[0].strip()))[0]
            entries.append(meta)
    return header, flags, entries


def _flag(meta, key):
    return meta.get(key, '0') == '1'


def _role(meta):
    if _flag(meta, 'interior'):
        return 'INTERIOR'
    if _flag(meta, 'night'):
        return 'NIGHT'
    if _flag(meta, 'lod'):
        return 'LOD'
    return 'HD'


# ── collections ──────────────────────────────────────────────────

def _tagged_collection(key, name, parent):
    """Get-or-create a collection identified by custom prop gta_fullmap=key (so an
    unrelated user collection that happens to be called "LOD" is never reused)."""
    cache = _st['colls']
    c = cache.get(key)
    if c is not None:
        return c
    for cand in bpy.data.collections:
        if cand.get('gta_fullmap') == key:
            c = cand
            break
    if c is None:
        c = bpy.data.collections.new(name)
        c['gta_fullmap'] = key
    if parent is None:
        scene_root = bpy.context.scene.collection
        if c.name not in scene_root.children:
            scene_root.children.link(c)
    elif c.name not in parent.children:
        parent.children.link(c)
    cache[key] = c
    return c


def _root_coll():
    return _tagged_collection('root', "GTA Map", None)


def _group_coll(group):
    root = _root_coll()
    names = {
        'HD': "Map", 'LOD': "LOD", 'NIGHT': "Night Lights",
        'INTERIOR': "Interiors", 'COL': "Collision",
    }
    if group == '2DFX':
        night = _group_coll('NIGHT')
        return _tagged_collection('2dfx', "2DFX Lights", night)
    return _tagged_collection(group.lower(), names[group], root)


def _dest_coll(meta, role):
    group = _group_coll(role)
    if not _st['by_ipl']:
        return group
    ipl = meta.get('ipl') or 'unknown'
    suffix = {'HD': '', 'LOD': '_LOD', 'NIGHT': '_night', 'INTERIOR': '_int'}[role]
    return _tagged_collection(f"{role.lower()}:{ipl}", f"{ipl}{suffix}", group)


def _find_layer_coll(layer_coll, coll):
    if layer_coll.collection == coll:
        return layer_coll
    for ch in layer_coll.children:
        found = _find_layer_coll(ch, coll)
        if found is not None:
            return found
    return None


def _staging():
    st = bpy.data.collections.get(_STAGING)
    if st is None:
        st = bpy.data.collections.new(_STAGING)
    root = bpy.context.scene.collection
    if st.name not in root.children:
        root.children.link(st)
    return st


def _activate(coll):
    vl = bpy.context.view_layer
    lc = _find_layer_coll(vl.layer_collection, coll)
    if lc is not None:
        vl.active_layer_collection = lc


# ── import of one model (template) ───────────────────────────────

def _quat(meta):
    from mathutils import Quaternion
    # INU IPL convention (ide_ipl.py / ariane_bridge._apply_meta): Quaternion(w, -x, -y, -z)
    return Quaternion((float(meta.get('qw', 1.0)), -float(meta.get('qx', 0.0)),
                       -float(meta.get('qy', 0.0)), -float(meta.get('qz', 0.0))))


def _world_matrix(o):
    """World matrix from the parent chain WITHOUT a depsgraph update (the map
    collection is excluded from the view layer while importing)."""
    m = o.matrix_basis.copy()
    while o.parent is not None:
        m = o.parent.matrix_basis @ o.matrix_parent_inverse @ m
        o = o.parent
    return m


def _texture_pass(new_objs, meta):
    """Textures for a freshly imported model — each texture decoded ONCE per
    import. Already-loaded images are wired straight into the new materials;
    only the still-missing names are decoded from <txd>.txd (the exact file
    Ariane named — no folder-wide coverage scan)."""
    from .dff_import import _collect_dff_scope
    scope_mats, _needed = _collect_dff_scope(new_objs)
    imgs = _st['img_lower']
    missing = set()
    for m in scope_mats:
        n = (m.get('dff_texture_name') or '').lower()
        if not n:
            continue
        img = imgs.get(n)
        if img is None:
            missing.add(n)
            continue
        if m.node_tree is not None:
            for node in m.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image is None:
                    node.image = img
    if not missing or not _st['flags'].get('auto_txd', True):
        return
    txd = (meta.get('txd') or '').strip()
    if not txd:
        return
    key = txd.lower()
    missing = {n for n in missing if (key, n) not in _st['tex_tried']}
    if not missing:
        return
    path = os.path.join(_st['dir'], txd + '.txd')
    if not os.path.isfile(path):
        _st['tex_tried'].update((key, n) for n in missing)
        return
    from .txd_import import import_txd
    try:
        images = import_txd(filepath=path, name_filter=missing,
                            material_scope=scope_mats, swap_rb=None) or []
    except Exception as exc:                                # noqa: BLE001
        print(f"[ariane fullmap] TXD {txd} failed: {exc}")
        images = []
    for img in images:
        try:
            imgs[img.name.lower()] = img
        except Exception:                                   # noqa: BLE001
            pass
    # names this TXD doesn't have (parent txd / missing) — never retry them
    _st['tex_tried'].update((key, n) for n in missing)


def _import_template(meta):
    """Import <model>.dff (+ its textures / COL) once. Returns a template dict or None.

    Uses the bulk map-import path of import_dff: no per-model view_layer.update()
    or select_all() (both cost O(scene) and made the import slow down more and
    more as the map grew), a material cache shared across the whole map, and the
    once-per-texture pass above instead of the generic auto-TXD picker."""
    from .dff_import import import_dff
    model = meta['model']
    dff = os.path.join(_st['dir'], model + '.dff')
    if not os.path.isfile(dff):
        _st['errors'] += 1
        print(f"[ariane fullmap] missing {model}.dff")
        return None

    staging = _staging()
    fx_coll = bpy.data.collections.get(_FX_COLL)
    fx_before = set(fx_coll.objects) if fx_coll is not None else set()

    vanilla = bool(_st['flags'].get('vanilla', True))
    t0 = time.perf_counter()
    try:
        import_dff(filepath=dff, context=bpy.context, skip_2dfx=not _st['want_2dfx'],
                   bulk_mode=True, target_collection=staging,
                   material_cache=_st['mat_cache'], weld_sharpen=vanilla)
    except Exception as exc:                                # noqa: BLE001
        print(f"[ariane fullmap] import error for {model}: {exc}")
        _st['errors'] += 1
    _st['t_dff'] += time.perf_counter() - t0

    new_objs = list(staging.objects)
    for o in new_objs:
        staging.objects.unlink(o)
    fx_coll = bpy.data.collections.get(_FX_COLL)
    fx_objs = []
    if fx_coll is not None:
        fx_objs = [o for o in fx_coll.objects if o not in fx_before]
        for o in fx_objs:
            fx_coll.objects.unlink(o)
        if len(fx_coll.objects) == 0 and len(fx_coll.children) == 0 and not fx_before:
            try:
                bpy.data.collections.remove(fx_coll)
            except Exception:                               # noqa: BLE001
                pass
    if not new_objs:
        for o in fx_objs:
            bpy.data.objects.remove(o, do_unlink=True)
        _st['errors'] += 1
        return None

    t1 = time.perf_counter()
    _texture_pass(new_objs, meta)
    _st['t_txd'] += time.perf_counter() - t1

    # one root per model so placement = a single transform
    roots = [o for o in new_objs if o.parent is None]
    if len(roots) == 1:
        root = roots[0]
    else:
        root = bpy.data.objects.new(model, None)
        root.empty_display_type = 'PLAIN_AXES'
        root.empty_display_size = 1.0
        for r in roots:
            r.parent = root
        new_objs.insert(0, root)
    root.name = model
    root.rotation_mode = 'QUATERNION'

    # 2DFX empties are created in model space but un-parented → parent them to the
    # root (root is still at the origin, so local == model space).
    for o in fx_objs:
        if o.parent is None:
            o.parent = root

    col_objs = []
    if _st['want_col'] and _flag(meta, 'col'):
        col_path = os.path.join(_st['dir'], model + '.col')
        if os.path.isfile(col_path):
            try:
                from ..core.col import read_col_file
                from .col_import import import_col_from_models
                col_objs = import_col_from_models(
                    read_col_file(col_path), bulk_mode=True,
                    target_collection=staging, material_cache=_st['col_mats']) or []
                vis = max((o for o in new_objs if o.type == 'MESH'),
                          key=lambda o: len(o.data.vertices), default=None)
                mw = _world_matrix(vis) if vis is not None else None
                for co in col_objs:
                    for c in list(co.users_collection):
                        c.objects.unlink(co)
                    if co.parent is None:
                        co.parent = root
                        if mw is not None:
                            co.matrix_basis = mw      # root sits at identity here
            except Exception as exc:                        # noqa: BLE001
                print(f"[ariane fullmap] COL skipped for {model}: {exc}")
                col_objs = []

    return {
        'root': root,
        'model': [o for o in new_objs],
        'fx': fx_objs,
        'col': col_objs,
        'all': new_objs + fx_objs + col_objs,
    }


def _duplicate(tpl):
    """Linked duplicate of a template hierarchy (mesh data shared)."""
    mapping = {}
    for o in tpl['all']:
        mapping[o] = o.copy()
    for o, c in mapping.items():
        if o.parent is not None and o.parent in mapping:
            c.parent = mapping[o.parent]
    return {
        'root': mapping[tpl['root']],
        'model': [mapping[o] for o in tpl['model']],
        'fx': [mapping[o] for o in tpl['fx']],
        'col': [mapping[o] for o in tpl['col']],
    }


def _tag(o, meta, role):
    # linked duplicates inherit the template's props → reset ours first
    for k in [k for k in o.keys() if k.startswith('gta_')]:
        del o[k]
    o['gta_model'] = meta['model']
    o['gta_role'] = role
    o['gta_ipl'] = meta.get('ipl', '')
    o['ariane_name'] = meta['model']
    guid = meta.get('guid', '')
    if guid:
        o['gta_guid'] = guid
    for src, dst, conv in (('id', 'gta_model_id', int), ('dd', 'gta_draw_distance', float),
                           ('area', 'gta_area', int)):
        if src in meta:
            try:
                o[dst] = conv(float(meta[src]))
            except (TypeError, ValueError):
                pass
    if meta.get('txd'):
        o['gta_txd'] = meta['txd']
    if meta.get('lodguid'):
        o['gta_lod_guid'] = meta['lodguid']
    if meta.get('pair'):
        o['gta_pair_model'] = meta['pair']
    if _flag(meta, 'timed'):
        o['gta_timed'] = True
        try:
            o['gta_time_on'] = int(meta.get('on', 0))
            o['gta_time_off'] = int(meta.get('off', 24))
        except ValueError:
            pass


def _apply_inu_props(o, meta):
    inu = getattr(o, 'inu', None)
    if inu is None:
        return
    try:
        vals = (('model_id', int(float(meta.get('id', 0)))),
                ('draw_distance', float(meta.get('dd', 299.0))),
                ('txd_name', (meta.get('txd') or '').strip() or None))
    except (TypeError, ValueError):
        return
    for attr, val in vals:
        if val is None:
            continue
        try:
            setattr(inu, attr, val)
        except Exception:                                   # noqa: BLE001
            pass


def _place(inst, meta, role):
    from mathutils import Vector
    root = inst['root']
    root.rotation_mode = 'QUATERNION'
    try:
        root.location = Vector((float(meta.get('x', 0)), float(meta.get('y', 0)),
                                float(meta.get('z', 0))))
        root.rotation_quaternion = _quat(meta)
    except (TypeError, ValueError):
        pass

    dest = _dest_coll(meta, role)
    for o in inst['model']:
        dest.objects.link(o)
        _tag(o, meta, role)
        _apply_inu_props(o, meta)
    if inst['fx']:
        fx = _group_coll('2DFX')
        for o in inst['fx']:
            fx.objects.link(o)
            _tag(o, meta, '2DFX')
    if inst['col']:
        cc = _group_coll('COL')
        for o in inst['col']:
            cc.objects.link(o)
            _tag(o, meta, 'COL')
            o['ariane_is_col'] = True

    _st['placed'] += 1
    _st['counts'][role] = _st['counts'].get(role, 0) + 1
    if _st['manifest'] is not None:
        _st['manifest'].append({
            'object': root.name, 'model': meta['model'], 'role': role,
            'ipl': meta.get('ipl', ''), 'guid': meta.get('guid', ''),
            'model_id': meta.get('id', ''), 'txd': meta.get('txd', ''),
            'draw_distance': meta.get('dd', ''),
            'position': [meta.get('x'), meta.get('y'), meta.get('z')],
            'rotation_xyzw': [meta.get('qx'), meta.get('qy'), meta.get('qz'), meta.get('qw')],
            'lod_guid': meta.get('lodguid', ''), 'area': meta.get('area', '0'),
        })


def _import_entry(meta):
    role = _role(meta)
    guid = meta.get('guid', '')
    if guid and guid in _st['seen']:
        _st['skipped'] += 1
        return
    model = meta['model']
    tpls = _st['templates']
    if model not in tpls:
        tpls[model] = _import_template(meta)
        tpl = tpls[model]
        if tpl is None:
            return
        inst = {k: tpl[k] for k in ('root', 'model', 'fx', 'col')}   # first use = originals
    else:
        tpl = tpls[model]
        if tpl is None:
            return
        inst = _duplicate(tpl)
    _place(inst, meta, role)
    if guid:
        _st['seen'].add(guid)


# ── driver ───────────────────────────────────────────────────────

def enqueue_mapjob(path) -> int:
    """Queue a .mapjob (called by the bridge watcher). Returns placements queued."""
    if _st is not None:
        if path not in _pending_jobs:
            _pending_jobs.append(path)
        return 0
    return _start(path)


def _start(path) -> int:
    global _st
    try:
        header, flags, entries = _read_mapjob(path)
    except OSError as exc:
        print(f"[ariane fullmap] can't read {path}: {exc}")
        return 0
    inbox = os.path.dirname(path)
    d = os.path.join(inbox, header.get('dir', os.path.splitext(os.path.basename(path))[0]))
    try:
        os.remove(path)
    except OSError:
        pass

    s = bpy.context.scene.inu_settings
    want_lod = bool(getattr(s, 'ariane_fm_lod', True))
    want_night = bool(getattr(s, 'ariane_fm_night', True))
    want_int = bool(getattr(s, 'ariane_fm_interiors', False))
    keep = []
    for m in entries:
        if not want_lod and _flag(m, 'lod'):
            continue
        if not want_night and _flag(m, 'night'):
            continue
        if not want_int and _flag(m, 'interior'):
            continue
        keep.append(m)
    # import HD first, then LOD / night (so the map shows up in a useful order)
    order = {'HD': 0, 'INTERIOR': 1, 'NIGHT': 2, 'LOD': 3}
    keep.sort(key=lambda m: order[_role(m)])

    seen = set()
    for o in bpy.data.objects:
        g = o.get('gta_guid')
        if g:
            seen.add(g)

    _st = {
        'dir': d, 'header': header, 'flags': flags,
        'queue': keep, 'pos': 0, 'total': len(keep), 'done': 0,
        'templates': {}, 'colls': {}, 'col_mats': {}, 'seen': seen,
        'placed': 0, 'skipped': 0, 'errors': 0, 'counts': {},
        'want_2dfx': bool(getattr(s, 'ariane_fm_2dfx', True)),
        'want_col': bool(getattr(s, 'ariane_fm_col', False)) and flags.get('col', False),
        'by_ipl': bool(getattr(s, 'ariane_fm_group_ipl', True)),
        'hide_lod': bool(getattr(s, 'ariane_fm_hide_lod', True)),
        'manifest': [] if getattr(s, 'ariane_fm_manifest', True) else None,
        'prev_active': bpy.context.view_layer.active_layer_collection.collection,
        't0': time.monotonic(), 'phase': 'importing',
        'game': header.get('game', ''),
        # perf caches
        'mat_cache': {}, 'tex_tried': set(),
        'img_lower': {img.name.lower(): img for img in bpy.data.images},
        't_dff': 0.0, 't_txd': 0.0, 'last_redraw': 0.0, 'excluded': False,
    }
    root = _root_coll()
    root['gta_game'] = _st['game']
    # Keep the map OUT of the depsgraph while importing: with the root collection
    # excluded, adding objects doesn't make Blender re-evaluate / redraw the whole
    # (growing) map every tick. Re-included once at the end.
    lc = _find_layer_coll(bpy.context.view_layer.layer_collection, root)
    if lc is not None and not lc.exclude:
        lc.exclude = True
        _st['excluded'] = True
    _staging()
    _activate(_staging())
    _detect_game(keep)
    print(f"[ariane fullmap] {len(keep)} placements ({len(entries)} sent) from {d}")
    try:
        bpy.context.window_manager.progress_begin(0, 100)
    except Exception:                                       # noqa: BLE001
        pass
    if not bpy.app.timers.is_registered(_driver):
        bpy.app.timers.register(_driver, first_interval=0.05)
    return len(keep)


def _detect_game(entries):
    """Set the scene game from the first DFF once (import_one_dff did it per model)."""
    try:
        from ..core import game_versions as gv
        for m in entries:
            dff = os.path.join(_st['dir'], m['model'] + '.dff')
            if os.path.isfile(dff):
                gv.maybe_set_game_from_import(bpy.context.scene, gv.detect_game_from_dff(dff))
                return
    except Exception as exc:                                # noqa: BLE001
        print(f"[ariane fullmap] game detect skipped: {exc}")


def _driver():
    if _st is None:
        return None
    deadline = time.monotonic() + 0.5
    q = _st['queue']
    while _st['pos'] < len(q) and time.monotonic() < deadline:
        meta = q[_st['pos']]
        _st['pos'] += 1
        try:
            _import_entry(meta)
        except Exception as exc:                            # noqa: BLE001
            _st['errors'] += 1
            print(f"[ariane fullmap] {meta.get('model')}: {exc}")
        _st['done'] = _st['pos']
    try:
        bpy.context.window_manager.progress_update(int(100 * _st['done'] / max(_st['total'], 1)))
        bpy.context.workspace.status_text_set(
            f"GTA full map: {_st['done']}/{_st['total']} placements, "
            f"{len(_st['templates'])} models")
    except Exception:                                       # noqa: BLE001
        pass
    now = time.monotonic()
    if now - _st['last_redraw'] > 1.0:       # UI only — the map itself is excluded
        _st['last_redraw'] = now
        _redraw(ui_only=True)
    if _st['pos'] >= len(q):
        _finish()
        return None
    return 0.0


def _redraw(ui_only=False):
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                if ui_only:
                    for region in area.regions:
                        if region.type in ('UI', 'HEADER'):
                            region.tag_redraw()
                else:
                    area.tag_redraw()
    except Exception:                                       # noqa: BLE001
        pass


def cancel():
    if _st is None:
        return
    _st['queue'] = _st['queue'][:_st['pos']]
    _st['total'] = _st['pos']
    _finish(cancelled=True)


def _finish(cancelled=False):
    global _st
    st = _st
    # staging cleanup + restore the user's active collection
    stg = bpy.data.collections.get(_STAGING)
    if stg is not None:
        for o in list(stg.objects):
            stg.objects.unlink(o)
            if not o.users_collection:
                bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(stg)
    try:
        _activate(st['prev_active'])
    except Exception:                                       # noqa: BLE001
        pass

    if st.get('excluded'):
        lc = _find_layer_coll(bpy.context.view_layer.layer_collection, st['colls'].get('root'))
        if lc is not None:
            lc.exclude = False
    print(f"[ariane fullmap] time in DFF build {st['t_dff']:.0f}s, textures {st['t_txd']:.0f}s")

    if st['hide_lod']:
        lod = st['colls'].get('lod')
        if lod is not None and st['counts'].get('HD'):
            lc = _find_layer_coll(bpy.context.view_layer.layer_collection, lod)
            if lc is not None:
                lc.hide_viewport = True     # eye icon: LODs overlap the HD map

    _write_docs(st)

    if not cancelled:
        try:
            shutil.rmtree(st['dir'], ignore_errors=True)
        except Exception:                                   # noqa: BLE001
            pass
    try:
        bpy.context.window_manager.progress_end()
        bpy.context.workspace.status_text_set(None)
    except Exception:                                       # noqa: BLE001
        pass
    dt = time.monotonic() - st['t0']
    c = st['counts']
    _last_summary[0] = (f"{'Cancelled' if cancelled else 'Done'}: {st['placed']} placed "
                        f"(HD {c.get('HD', 0)}, LOD {c.get('LOD', 0)}, night {c.get('NIGHT', 0)}, "
                        f"int {c.get('INTERIOR', 0)}) · {len(st['templates'])} models · "
                        f"{st['skipped']} already in scene · {st['errors']} errors · {dt:.0f}s")
    print(f"[ariane fullmap] {_last_summary[0]}")
    _st = None
    _redraw()
    if _pending_jobs:
        nxt = _pending_jobs.pop(0)
        bpy.app.timers.register(lambda: (_start(nxt), None)[1], first_interval=0.2)


def _write_docs(st):
    c = st['counts']
    readme = f"""GTA MAP — imported from Ariane (Aiden Fork) via INU_Tools
Game: {st['game'] or 'unknown'}

COLLECTIONS
  GTA Map/
    Map/                HD (full-detail) models, one sub-collection per IPL file
    LOD/                low-detail LOD models, sub-collections "<ipl>_LOD"
                        (hidden in the viewport by default — they overlap the HD map)
    Night Lights/       night-only timed models (neon signs, lit windows), "<ipl>_night"
      2DFX Lights/      2DFX light/corona empties, parented to their model
    Interiors/          interior areas (only when enabled)
    Collision/          COL meshes (only when enabled), parented to their model

OBJECTS
  Each placement = one root object named after its model (Blender adds .001, .002 …
  for repeats; identical models share mesh data). Custom properties:
    gta_model          model name (IDE / DFF name)
    gta_model_id       IDE object id
    gta_guid           unique placement id "<ipl>#<index in IPL>"
    gta_ipl            IPL the placement comes from
    gta_role           HD | LOD | NIGHT | INTERIOR | 2DFX | COL
    gta_lod_guid       (HD only) gta_guid of the LOD placement that replaces it far away
    gta_pair_model     (III/VC) related HD/LOD model name
    gta_txd            texture dictionary name
    gta_draw_distance  IDE draw distance
    gta_area           interior area id (0 = exterior)
    gta_timed / gta_time_on / gta_time_off   time-object window (hours)
  Coordinates are GTA world units (1 unit = 1 m), Z up.

COUNTS
  HD {c.get('HD', 0)} · LOD {c.get('LOD', 0)} · Night {c.get('NIGHT', 0)} · Interiors {c.get('INTERIOR', 0)}
  Unique models: {len(st['templates'])}

See GTA_MAP_MANIFEST.json for the full placement list.
"""
    txt = bpy.data.texts.get("GTA_MAP_README") or bpy.data.texts.new("GTA_MAP_README")
    txt.clear()
    txt.write(readme)
    if st['manifest'] is not None:
        man = bpy.data.texts.get("GTA_MAP_MANIFEST.json") or bpy.data.texts.new("GTA_MAP_MANIFEST.json")
        # merge with a previous import of other parts of the map
        prev = []
        try:
            if man.as_string().strip():
                prev = json.loads(man.as_string()).get('placements', [])
        except Exception:                                   # noqa: BLE001
            prev = []
        man.clear()
        man.write(json.dumps({'game': st['game'], 'placements': prev + st['manifest']},
                             indent=0, separators=(',', ':')))


# ── operators ────────────────────────────────────────────────────

class GTATOOLS_OT_ariane_import_full_map(bpy.types.Operator):
    """Ask Ariane (Aiden Fork) to send the WHOLE map — every placed object, no
    selection needed — and import it here as a structured scene (Map / LOD /
    Night Lights). Turns the Ariane watcher on automatically"""

    bl_idname = "gtatools.ariane_import_full_map"
    bl_label = "Import WHOLE MAP"

    def execute(self, context):
        from .ariane_bridge import is_watching, start_watch, process_jobs
        if is_running():
            self.report({'WARNING'}, "A full-map import is already running")
            return {'CANCELLED'}
        s = context.scene.inu_settings
        try:
            path = request_full_map(s)
        except OSError as exc:
            self.report({'ERROR'}, f"Can't write the request: {exc}")
            return {'CANCELLED'}
        if not is_watching():
            start_watch()
        process_jobs()
        self.report({'INFO'}, f"Requested the full map from Ariane ({path}). "
                              "Keep Ariane open - import starts when it's done exporting")
        return {'FINISHED'}


class GTATOOLS_OT_ariane_full_map_cancel(bpy.types.Operator):
    """Stop the running full-map import (what's already imported stays)"""

    bl_idname = "gtatools.ariane_full_map_cancel"
    bl_label = "Cancel full-map import"

    def execute(self, context):
        cancel()
        return {'FINISHED'}


def draw_full_map_box(layout, context):
    s = context.scene.inu_settings
    box = layout.box()
    hdr = box.row(align=True)
    hdr.label(text="Whole Map (Aiden Fork)", icon='WORLD')

    col = box.column(align=True)
    row = col.row(align=True)
    row.prop(s, "ariane_fm_lod", toggle=True)
    row.prop(s, "ariane_fm_night", toggle=True)
    row.prop(s, "ariane_fm_2dfx", toggle=True)
    row = col.row(align=True)
    row.prop(s, "ariane_fm_textures", toggle=True)
    row.prop(s, "ariane_fm_col", toggle=True)
    row.prop(s, "ariane_fm_interiors", toggle=True)
    row = col.row(align=True)
    row.prop(s, "ariane_fm_group_ipl", toggle=True)
    row.prop(s, "ariane_fm_hide_lod", toggle=True)
    row.prop(s, "ariane_fm_manifest", toggle=True)
    row = col.row(align=True)
    row.prop(s, "ariane_fm_visible_only", toggle=True)

    if is_running():
        done, total, _ = progress()
        r = box.row(align=True)
        r.label(text=f"Importing {done}/{total}…", icon='SORTTIME')
        r.operator("gtatools.ariane_full_map_cancel", text="", icon='CANCEL')
    else:
        big = box.row(align=True)
        big.scale_y = 1.4
        big.operator("gtatools.ariane_import_full_map", icon='IMPORT')
        ast = ariane_status()
        if ast is not None and ast[0] == 'running' and ast[4] < 10:
            box.label(text=f"Ariane exporting {ast[1]}/{ast[2]}…", icon='TIME')
    if last_summary():
        sub = box.column(align=True)
        sub.enabled = False
        sub.label(text=last_summary(), icon='INFO')


classes = (
    GTATOOLS_OT_ariane_import_full_map,
    GTATOOLS_OT_ariane_full_map_cancel,
)
