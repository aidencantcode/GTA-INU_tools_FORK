# Ariane + INU Tools: Aiden Fork

This repo combines two projects so one GitHub Actions workflow can build both:

| Folder | What it is | Based on |
|---|---|---|
| `ariane/` | Ariane, the GTA III / VC / SA map viewer and editor (builds `ariane.exe`) | [INU-ez/INU-Tools-GTA-Ariane-Support](https://github.com/INU-ez/INU-Tools-GTA-Ariane-Support) (Dryxio's Ariane, euryopa) |
| `blender/INU_tools/` | The INU Tools Blender add-on, including the Ariane bridge | [INU-ez/INU_Tools-GTA-Blender](https://github.com/INU-ez/INU_Tools-GTA-Blender) |

Each push to `main` or `master` builds both parts and publishes a GitHub Release with:

- `ariane.exe` for Windows x64 (D3D9)
- `INU_Tools-AidenFork-<ver>.zip` for Blender 4.2 and newer (Install from Disk)
- `INU_Tools-AidenFork-<ver>-legacy.zip` for Blender 2.83 to 4.1
- `SHA256SUMS.txt`

---

## New features

### Ariane: Aiden Fork QOL

You can open it from the main menu bar (**Aiden Fork QOL**) or from **Window → Aiden Fork QOL**.

- **Disable all view distance.** Ariane loads and draws every HD model at any distance and turns LOD swapping off, so the whole map is on screen at once. The far clip is adjustable, and you can turn fog off. Ariane streams in every model on the first frame after you turn this on, so expect a pause and high memory use.
- **LOD mode:**
  - *Game default*: swaps HD and LOD by distance.
  - *Remove all LOD*: HD only.
  - *LOD only*
  - *All LOD active*: draws HD and LOD together.
- **Export FULL MAP to Blender.** Sends every placed object to Blender without needing a selection. Options: LOD models, night lights, textures, collision, interiors, and only visible IPLs. The export runs a little each frame so the editor stays responsive, and it shows a progress bar and a Cancel button.
- Your settings are saved to `<game>\ariane\aidenqol.txt`.
- The in-app updater now checks this repo's releases instead of upstream, so it won't replace the fork with vanilla Ariane. It only offers an update when you raise `ARIANE_VERSION` in `ariane/tools/euryopa/version.h`.

### Blender: Import WHOLE MAP

Go to **N panel → GTA Tools → Export panel → Ariane tab → Whole Map (Aiden Fork)**.

| Toggle | Default | Effect |
|---|---|---|
| LOD | on | Imports the LOD models into a separate `LOD` collection. |
| Night Lights | on | Imports night-only timed models (neon signs, lit windows) into `Night Lights`. |
| 2DFX Lights | on | Imports 2DFX light and corona empties (light areas) into `Night Lights/2DFX Lights`. |
| Textures | on | Asks Ariane to send the TXDs. |
| Collision | off | Imports COL meshes into `Collision`. |
| Interiors | off | Includes interior areas. |
| Group by IPL | on | Adds one sub-collection per IPL (`LAe`, `LAe_LOD`, `LAe_night` …). |
| Hide LOD | on | Hides the LOD collection in the viewport after import, because LODs overlap the HD map. |
| Manifest | on | Writes the `GTA_MAP_README` and `GTA_MAP_MANIFEST.json` text blocks. |
| Only visible IPLs | off | Exports only the IPLs that are visible in Ariane's View panel. |

When you click **Import WHOLE MAP**, Blender turns the bridge watcher on and asks Ariane for the map. Ariane exports it, and Blender then imports it on its own. Blender imports each unique model once. Every repeat of that model is a linked duplicate that shares the same mesh data, which keeps the scene fast. If you run the import again, it skips placements that are already in the scene.

**Scene layout**

```
GTA Map
├── Map            HD models, one sub-collection per IPL
├── LOD            LOD models ("<ipl>_LOD"), hidden in the viewport by default
├── Night Lights   night-only models ("<ipl>_night")
│   └── 2DFX Lights
├── Interiors      (optional)
└── Collision      (optional)
```

Each object is named after its model and has these custom properties: `gta_model`, `gta_model_id`, `gta_guid` (`<ipl>#<index>`), `gta_ipl`, `gta_role` (HD / LOD / NIGHT / INTERIOR / 2DFX / COL), `gta_lod_guid`, `gta_txd`, `gta_draw_distance`, `gta_area` and the time-object hours. The `GTA_MAP_README` text block inside the .blend explains all of this, so you can give the file to another tool or an LLM.

---

## Setup

1. Download `ariane.exe` from the latest release and put it in your GTA folder.
2. Install the add-on zip in Blender.
3. In Blender, go to **GTA Tools → Ariane tab** and set the game folder (folder icon). The folder is the same one Ariane is in, because both sides share `<game>\ariane\bridge`.
4. Open Ariane and load the game. Then, in Blender, click **Import WHOLE MAP**, or use **Export FULL MAP to Blender** in Ariane.

## Building locally

- **Ariane:** see `ariane/README.md`. CI uses Premake 5 beta8 with VS2022 and librw at the commit pinned in the workflow. The `--update-repo=<owner>/<repo>` premake option sets which repo's releases the updater checks.
- **Add-on:** run `blender --command extension build --source-dir blender/INU_tools`.

## Uploading this repo to GitHub

The GitHub website's drag-and-drop upload only takes 100 files at a time, and this repo has more than that. Use git instead:

```bash
cd aiden-fork
git init -b main
git add .
git commit -m "Aiden Fork: full-map export/import + QOL"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

GitHub Desktop also works: use *Add local repository*, then *Publish*. The first push runs the workflow, and the release appears under **Releases** after about 10 minutes. Workflow permissions must allow writing: go to *Settings → Actions → General → Workflow permissions → Read and write*.

## Licenses

The Blender add-on is GPL-3.0-or-later (`blender/LICENSE`). Ariane is based on aap's librw, euryopa and Dryxio's Ariane, and keeps its upstream terms. Credit for both projects goes to their original authors.
