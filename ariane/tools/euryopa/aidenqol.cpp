// ─────────────────────────────────────────────────────────────────────────────
// Aiden Fork QOL
//
//  * View distance / LOD overrides for the live viewport:
//      - "Disable all view distance": every HD model is drawn at any distance
//        (the whole map is streamed in at once) and LOD swapping is turned off.
//      - LOD mode: game default / remove all LODs / LODs only / all LODs active.
//  * Full-map export to Blender (INU_Tools):
//      Walks EVERY placed instance (not just the selection), writes each model's
//      DFF / TXD / COL exactly once into  <game>\ariane\bridge\inbox\fullmap_<ts>\
//      and a single "fullmap_<ts>.mapjob" listing every placement with its role
//      (HD / LOD / night light), IPL, LOD link and IDE data. The INU_Tools addon
//      imports that job as a structured, named scene (Map / LOD / Night Lights).
//      Runs incrementally (a slice per frame) so the editor stays responsive.
//  * Blender can trigger the export itself by dropping
//      <game>\ariane\bridge\outbox\fullmap.req   (key=value options, one line)
// ─────────────────────────────────────────────────────────────────────────────
#include "euryopa.h"
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <unordered_set>
#include <vector>

#ifdef _WIN32
#define AQ_SEP "\\"
#else
#define AQ_SEP "/"
#endif

bool gAidenNoViewDistance = false;
int gAidenLodMode = AIDEN_LOD_NORMAL;
float gAidenFarClip = 12000.0f;

// ── effective render flags (QOL overrides win over the View panel radio) ──

bool
AidenRenderOnlyHD(void)
{
	if(gAidenNoViewDistance) return true;		// "without any LOD"
	switch(gAidenLodMode){
	case AIDEN_LOD_REMOVE_ALL: return true;
	case AIDEN_LOD_ONLY:
	case AIDEN_LOD_ALL_ACTIVE: return false;
	default: return gRenderOnlyHD;
	}
}

bool
AidenRenderOnlyLod(void)
{
	if(gAidenNoViewDistance) return false;
	switch(gAidenLodMode){
	case AIDEN_LOD_ONLY: return true;
	case AIDEN_LOD_REMOVE_ALL:
	case AIDEN_LOD_ALL_ACTIVE: return false;
	default: return gRenderOnlyLod;
	}
}

bool
AidenRenderAllActive(void)
{
	return !gAidenNoViewDistance && gAidenLodMode == AIDEN_LOD_ALL_ACTIVE;
}

// ── full-map export options ──────────────────────────────────────

struct AidenFullMapOpts
{
	bool lod = true;		// include LOD instances (grouped under "LOD" in Blender)
	bool night = true;		// include night-only timed models (neon / lit windows)
	bool col = false;		// include collision
	bool interiors = false;	// include interior areas (they float above the map)
	bool autoTxd = true;		// include textures
	bool visibleOnly = false;	// only IPLs currently visible in the View panel filter
	bool vanilla = true;		// vanilla import path (weld/dedup) on the Blender side
};
static AidenFullMapOpts gFmOpts;

// ── settings persistence (ariane\aidenqol.txt) ───────────────────

static bool gQolLoaded = false;

static void
qolSave(void)
{
	FILE *f = fopenArianeDataWrite("aidenqol.txt");
	if(f == nil) return;
	fprintf(f, "no_view_distance %d\n", gAidenNoViewDistance ? 1 : 0);
	fprintf(f, "lod_mode %d\n", gAidenLodMode);
	fprintf(f, "far_clip %.1f\n", gAidenFarClip);
	fprintf(f, "fm_lod %d\n", gFmOpts.lod ? 1 : 0);
	fprintf(f, "fm_night %d\n", gFmOpts.night ? 1 : 0);
	fprintf(f, "fm_col %d\n", gFmOpts.col ? 1 : 0);
	fprintf(f, "fm_interiors %d\n", gFmOpts.interiors ? 1 : 0);
	fprintf(f, "fm_auto_txd %d\n", gFmOpts.autoTxd ? 1 : 0);
	fprintf(f, "fm_visible_only %d\n", gFmOpts.visibleOnly ? 1 : 0);
	fclose(f);
}

static void
qolLoad(void)
{
	if(gQolLoaded) return;
	gQolLoaded = true;
	FILE *f = fopenArianeDataRead("aidenqol.txt");
	if(f == nil) return;
	char key[64];
	float v;
	while(fscanf(f, "%63s %f", key, &v) == 2){
		bool b = v != 0.0f;
		if(strcmp(key, "no_view_distance") == 0) gAidenNoViewDistance = b;
		else if(strcmp(key, "lod_mode") == 0) gAidenLodMode = clamp((int)v, 0, 3);
		else if(strcmp(key, "far_clip") == 0) gAidenFarClip = clamp(v, 1000.0f, 50000.0f);
		else if(strcmp(key, "fm_lod") == 0) gFmOpts.lod = b;
		else if(strcmp(key, "fm_night") == 0) gFmOpts.night = b;
		else if(strcmp(key, "fm_col") == 0) gFmOpts.col = b;
		else if(strcmp(key, "fm_interiors") == 0) gFmOpts.interiors = b;
		else if(strcmp(key, "fm_auto_txd") == 0) gFmOpts.autoTxd = b;
		else if(strcmp(key, "fm_visible_only") == 0) gFmOpts.visibleOnly = b;
	}
	fclose(f);
}

// ── classification helpers ───────────────────────────────────────

static bool
hourOn(int h, int on, int off)
{
	if(on == off) return true;
	return on < off ? (h >= on && h < off) : (h >= on || h < off);
}

// timed model that is lit at night but not during the day → "night light"
static bool
isNightOnly(ObjectDef *obj)
{
	if(obj == nil || !obj->m_isTimed) return false;
	bool night = hourOn(0, obj->m_timeOn, obj->m_timeOff) || hourOn(23, obj->m_timeOn, obj->m_timeOff);
	bool day = hourOn(12, obj->m_timeOn, obj->m_timeOff);
	return night && !day;
}

static bool
isLodInst(ObjectInst *inst, ObjectDef *obj)
{
	return inst->m_isBigBuilding || (obj && obj->m_isBigBuilding);
}

static bool
isInterior(ObjectInst *inst)
{
	return !(inst->m_area == 0 || inst->m_area == 13);
}

// "data/maps/LA/LAe" / "lae_stream3" → "LAe" / "lae"
static void
shortIplName(ObjectInst *inst, char *dst, size_t size)
{
	const char *src = inst->m_iplFilterKey;
	if(src[0] == '\0' && inst->m_file && inst->m_file->name)
		src = inst->m_file->name;
	const char *base = src;
	for(const char *s = src; *s; s++)
		if(*s == '/' || *s == '\\') base = s + 1;
	size_t n = 0;
	for(; base[n] && base[n] != '.' && n < size - 1; n++)
		dst[n] = base[n];
	dst[n] = '\0';
	// strip "_streamN"
	char *st = nil;
	for(char *s = dst; (s = strstr(s, "_stream")) != nil; s++)
		st = s;
	if(st){
		const char *d = st + 7;
		bool digits = *d != '\0';
		for(; *d; d++) if(*d < '0' || *d > '9') digits = false;
		if(digits) *st = '\0';
	}
	// tabs / spaces would break the job line
	for(char *s = dst; *s; s++)
		if(*s == '\t' || *s == ' ' || *s == '=') *s = '_';
	if(dst[0] == '\0')
		strncpy(dst, "unknown", size - 1), dst[size - 1] = '\0';
}

static const char*
gameTag(void)
{
	switch(gameversion){
	case GAME_III: return "III";
	case GAME_VC: return "VC";
	default: return "SA";
	}
}

// ── incremental full-map export ──────────────────────────────────

static struct
{
	bool running;
	bool fromBlender;
	AidenFullMapOpts opts;
	std::vector<ObjectInst*> list;
	size_t cursor;
	std::string body;
	std::unordered_set<std::string> models, txds, cols, badModels;
	char inbox[1024];
	char dir[1024];
	char dirName[128];
	int placed, failed, nHd, nLod, nNight, nInterior;
	char lastResult[256];
	double lastStatusWrite;
} fm;

static double
nowSeconds(void)
{
	using namespace std::chrono;
	return duration<double>(steady_clock::now().time_since_epoch()).count();
}

static void
writeStatus(const char *state)
{
	char path[1024];
	if(!GetArianeDataPath(path, sizeof(path), "bridge" AQ_SEP "fullmap_status.txt"))
		return;
	char buf[256];
	int n = snprintf(buf, sizeof(buf), "%s %d %d %d\n", state,
		(int)fm.cursor, (int)fm.list.size(), fm.placed);
	AidenWriteFile(path, buf, n);
}

static bool
startFullMapExport(const AidenFullMapOpts &opts, bool fromBlender)
{
	if(fm.running) return false;
	if(!GetArianeDataPath(fm.inbox, sizeof(fm.inbox), "bridge" AQ_SEP "inbox")){
		snprintf(fm.lastResult, sizeof(fm.lastResult), "Could not resolve the bridge inbox path");
		return false;
	}
	snprintf(fm.dirName, sizeof(fm.dirName), "fullmap_%u", (unsigned)time(nil));
	if(!BuildPath(fm.dir, sizeof(fm.dir), fm.inbox, fm.dirName)){
		snprintf(fm.lastResult, sizeof(fm.lastResult), "Bridge path too long");
		return false;
	}

	fm.opts = opts;
	fm.fromBlender = fromBlender;
	fm.list.clear();
	fm.models.clear();
	fm.txds.clear();
	fm.cols.clear();
	fm.badModels.clear();
	fm.body.clear();
	fm.cursor = 0;
	fm.placed = fm.failed = fm.nHd = fm.nLod = fm.nNight = fm.nInterior = 0;
	fm.lastStatusWrite = 0.0;

	for(CPtrNode *p = instances.first; p; p = p->next){
		ObjectInst *inst = (ObjectInst*)p->item;
		if(inst == nil || inst->m_isDeleted) continue;
		ObjectDef *obj = GetObjectDef(inst->m_objectId);
		if(obj == nil || obj->m_name[0] == '\0') continue;
		if(!opts.lod && isLodInst(inst, obj)) continue;
		if(!opts.night && isNightOnly(obj)) continue;
		if(!opts.interiors && isInterior(inst)) continue;
		if(opts.visibleOnly && !IsInstVisibleByIplFilter(inst)) continue;
		fm.list.push_back(inst);
	}
	if(fm.list.empty()){
		snprintf(fm.lastResult, sizeof(fm.lastResult), "Nothing to export (all instances filtered out)");
		return false;
	}
	fm.body.reserve(fm.list.size() * 220);
	fm.running = true;
	log("AidenQOL: full-map export started: %d instances -> %s\n", (int)fm.list.size(), fm.dir);
	writeStatus("running");
	return true;
}

static void
exportOne(ObjectInst *inst)
{
	ObjectDef *obj = GetObjectDef(inst->m_objectId);
	if(obj == nil || obj->m_name[0] == '\0' || inst->m_isDeleted) return;

	std::string model = obj->m_name;
	if(fm.badModels.count(model))
		return;				// DFF unavailable → can't place it
	if(fm.models.find(model) == fm.models.end()){
		if(!AidenExportAssetTo(fm.dir, obj->m_name, obj->m_name, "dff", obj->m_imageIndex)){
			log("AidenQOL: failed to export %s.dff\n", obj->m_name);
			fm.failed++;
			fm.badModels.insert(model);	// don't retry for every instance
			return;
		}
		fm.models.insert(model);
		if(fm.opts.col && AidenExportColTo(fm.dir, obj))
			fm.cols.insert(model);
	}

	TxdDef *txd = GetTxdDef(obj->m_txdSlot);
	const char *txdName = (txd && txd->name[0]) ? txd->name : "";
	// Each texture dictionary is written ONCE under its own name (generic.txd, …);
	// the job line carries txd=<name> and the addon resolves it from the same folder.
	if(fm.opts.autoTxd && txdName[0] && fm.txds.find(txdName) == fm.txds.end()){
		fm.txds.insert(txdName);
		if(!AidenExportAssetTo(fm.dir, txdName, txdName, "txd", txd->imageIndex))
			log("AidenQOL: failed to export %s.txd\n", txdName);
	}

	bool lod = isLodInst(inst, obj);
	bool night = isNightOnly(obj);
	bool interior = isInterior(inst);
	char ipl[96];
	shortIplName(inst, ipl, sizeof(ipl));
	char guid[300];
	GetInstGuid(inst, guid, sizeof(guid));
	char lodGuid[300] = "";
	if(inst->m_lod && !inst->m_lod->m_isDeleted)
		GetInstGuid(inst->m_lod, lodGuid, sizeof(lodGuid));
	const char *pair = (obj->m_relatedModel && obj->m_relatedModel != obj) ? obj->m_relatedModel->m_name : "";

	char line[1400];
	int n = snprintf(line, sizeof(line),
		"%s.dff\tid=%d\tiid=%d\tguid=%s\tdd=%.3f\ttxd=%s"
		"\tx=%.4f\ty=%.4f\tz=%.4f\tqx=%.6f\tqy=%.6f\tqz=%.6f\tqw=%.6f"
		"\tipl=%s\tlod=%d\tnight=%d\tinterior=%d\tarea=%d"
		"\ttimed=%d\ton=%d\toff=%d\tlodguid=%s\tpair=%s\tcol=%d\n",
		obj->m_name, obj->m_id, inst->m_id, guid, obj->GetLargestDrawDist(), txdName,
		inst->m_translation.x, inst->m_translation.y, inst->m_translation.z,
		inst->m_rotation.x, inst->m_rotation.y, inst->m_rotation.z, inst->m_rotation.w,
		ipl, lod ? 1 : 0, night ? 1 : 0, interior ? 1 : 0, inst->m_area,
		obj->m_isTimed ? 1 : 0, obj->m_timeOn, obj->m_timeOff, lodGuid, pair,
		fm.cols.count(model) ? 1 : 0);
	if(n > 0 && n < (int)sizeof(line)){
		fm.body.append(line, n);
		fm.placed++;
		if(night) fm.nNight++;
		else if(lod) fm.nLod++;
		else fm.nHd++;
		if(interior) fm.nInterior++;
	}
}

static void
finishFullMapExport(void)
{
	fm.running = false;
	std::string job;
	char hdr[512];
	snprintf(hdr, sizeof(hdr),
		"#v 2\n#fullmap dir=%s game=%s count=%d models=%d txds=%d\n"
		"#flags auto_txd=%d vanilla=%d ide=1 ipl=1 col=%d lod=%d night=%d interiors=%d\n",
		fm.dirName, gameTag(), fm.placed, (int)fm.models.size(), (int)fm.txds.size(),
		fm.opts.autoTxd ? 1 : 0, fm.opts.vanilla ? 1 : 0, fm.opts.col ? 1 : 0,
		fm.opts.lod ? 1 : 0, fm.opts.night ? 1 : 0, fm.opts.interiors ? 1 : 0);
	job = hdr;
	job += fm.body;
	fm.body.clear();
	fm.body.shrink_to_fit();

	char jobPath[1024];
	snprintf(jobPath, sizeof(jobPath), "%s" AQ_SEP "%s.mapjob", fm.inbox, fm.dirName);
	bool ok = fm.placed > 0 && AidenWriteFile(jobPath, job.data(), (int)job.size());
	if(ok){
		char pokePath[1024];
		snprintf(pokePath, sizeof(pokePath), "%s" AQ_SEP "poke", fm.inbox);
		char pokeBuf[32];
		int pn = snprintf(pokeBuf, sizeof(pokeBuf), "%u\n", (unsigned)time(nil));
		AidenWriteFile(pokePath, pokeBuf, pn);
		snprintf(fm.lastResult, sizeof(fm.lastResult),
			"Sent %d placements (%d HD, %d LOD, %d night) / %d models to Blender%s",
			fm.placed, fm.nHd, fm.nLod, fm.nNight, (int)fm.models.size(),
			fm.failed ? " - some models failed, see log" : "");
		writeStatus("done");
	}else{
		snprintf(fm.lastResult, sizeof(fm.lastResult), "Full-map export failed (could not write %s)", jobPath);
		writeStatus("failed");
	}
	log("AidenQOL: %s\n", fm.lastResult);
	Toast(TOAST_SAVE, "%s", fm.lastResult);
	fm.list.clear();
	fm.list.shrink_to_fit();
}

static void
cancelFullMapExport(void)
{
	if(!fm.running) return;
	fm.running = false;
	fm.list.clear();
	fm.body.clear();
	snprintf(fm.lastResult, sizeof(fm.lastResult), "Full-map export cancelled");
	writeStatus("cancelled");
	Toast(TOAST_SAVE, "%s", fm.lastResult);
}

bool
AidenFullMapExportRunning(void)
{
	return fm.running;
}

// Blender → ariane: outbox\fullmap.req  ("lod=1 night=1 col=0 interiors=0 auto_txd=1 visible_only=0")
static void
pollBlenderFullMapRequest(void)
{
	static int tick = 0;
	if(++tick < 20) return;
	tick = 0;
	char path[1024];
	if(!GetArianeDataPath(path, sizeof(path), "bridge" AQ_SEP "outbox" AQ_SEP "fullmap.req"))
		return;
	FILE *f = fopen(path, "rb");
	if(f == nil) return;
	char buf[1024] = "";
	size_t n = fread(buf, 1, sizeof(buf) - 1, f);
	buf[n] = '\0';
	fclose(f);
	remove(path);

	AidenFullMapOpts o = gFmOpts;
	for(char *t = strtok(buf, " \t\r\n"); t; t = strtok(nil, " \t\r\n")){
		char *eq = strchr(t, '=');
		if(!eq) continue;
		*eq = '\0';
		bool v = atoi(eq + 1) != 0;
		if(strcmp(t, "lod") == 0) o.lod = v;
		else if(strcmp(t, "night") == 0) o.night = v;
		else if(strcmp(t, "col") == 0) o.col = v;
		else if(strcmp(t, "interiors") == 0) o.interiors = v;
		else if(strcmp(t, "auto_txd") == 0) o.autoTxd = v;
		else if(strcmp(t, "visible_only") == 0) o.visibleOnly = v;
		else if(strcmp(t, "vanilla") == 0) o.vanilla = v;
	}
	if(fm.running){
		Toast(TOAST_SAVE, "Blender asked for the full map - an export is already running");
		return;
	}
	if(startFullMapExport(o, true))
		Toast(TOAST_SAVE, "Blender requested the full map - exporting %d instances...", (int)fm.list.size());
	else{
		Toast(TOAST_SAVE, "%s", fm.lastResult);
		writeStatus("failed");
	}
}

void
AidenFullMapExportUpdate(void)
{
	qolLoad();
	pollBlenderFullMapRequest();
	if(!fm.running) return;

	double deadline = nowSeconds() + 0.030;		// ~30 ms of export work per frame
	while(fm.cursor < fm.list.size()){
		exportOne(fm.list[fm.cursor++]);
		if((fm.cursor & 7) == 0 && nowSeconds() > deadline)
			break;
	}
	double t = nowSeconds();
	if(t - fm.lastStatusWrite > 0.5){
		fm.lastStatusWrite = t;
		writeStatus("running");
	}
	if(fm.cursor >= fm.list.size())
		finishFullMapExport();
}

// ── UI ───────────────────────────────────────────────────────────

static bool
helpMarker(const char *text)
{
	ImGui::SameLine();
	ImGui::TextDisabled("(?)");
	if(ImGui::IsItemHovered())
		ImGui::SetTooltip("%s", text);
	return false;
}

static void
drawFullMapExportControls(void)
{
	bool changed = false;
	changed |= ImGui::Checkbox("LOD models##fm", &gFmOpts.lod);
	helpMarker("Include every LOD instance. In Blender they are grouped in a separate\n"
		"\"LOD\" collection (per IPL), so the full-detail map stays clean.");
	ImGui::SameLine();
	changed |= ImGui::Checkbox("Night lights##fm", &gFmOpts.night);
	helpMarker("Include night-only timed models (neon signs, lit windows...).\n"
		"They go into a \"Night Lights\" collection in Blender.");
	changed |= ImGui::Checkbox("Textures (TXD)##fm", &gFmOpts.autoTxd);
	ImGui::SameLine();
	changed |= ImGui::Checkbox("Collision (COL)##fm", &gFmOpts.col);
	changed |= ImGui::Checkbox("Interiors##fm", &gFmOpts.interiors);
	helpMarker("Interior areas sit high above / below the map. Off = exterior only.");
	ImGui::SameLine();
	changed |= ImGui::Checkbox("Only visible IPLs##fm", &gFmOpts.visibleOnly);
	helpMarker("Respect the IPL Visibility list in the View panel\n"
		"(hide IPLs there to export only part of the map).");
	if(changed) qolSave();

	if(fm.running){
		float frac = fm.list.empty() ? 0.0f : (float)fm.cursor / (float)fm.list.size();
		char ov[96];
		snprintf(ov, sizeof(ov), "%d / %d", (int)fm.cursor, (int)fm.list.size());
		ImGui::ProgressBar(frac, ImVec2(-1, 0), ov);
		ImGui::Text("Models written: %d", (int)fm.models.size());
		if(ImGui::Button("Cancel export"))
			cancelFullMapExport();
	}else{
		if(ImGui::Button("Export FULL MAP to Blender", ImVec2(-1, 0))){
			if(startFullMapExport(gFmOpts, false))
				Toast(TOAST_SAVE, "Exporting %d instances to Blender...", (int)fm.list.size());
			else
				Toast(TOAST_SAVE, "%s", fm.lastResult);
		}
		if(ImGui::IsItemHovered())
			ImGui::SetTooltip("Sends EVERY placed object (no selection needed) to a running Blender\n"
				"with INU_Tools (Ariane tab -> Sync import on). Each model/texture is\n"
				"written once; placements keep their IPL, LOD links and IDE data.\n"
				"The same export can also be started from Blender: Ariane tab -> Import WHOLE MAP.");
	}
	if(fm.lastResult[0])
		ImGui::TextWrapped("%s", fm.lastResult);
}

void
uiAidenQolWindow(bool *open)
{
	qolLoad();
	ImGui::SetNextWindowSize(ImVec2(430.0f, 470.0f), ImGuiCond_FirstUseEver);
	if(!ImGui::Begin("Aiden Fork QOL", open)){
		ImGui::End();
		return;
	}
	bool changed = false;

	ImGui::SeparatorText("View distance");
	changed |= ImGui::Checkbox("Disable all view distance", &gAidenNoViewDistance);
	helpMarker("Load and draw the WHOLE map at once: every HD model is shown at any\n"
		"distance and LOD swapping is disabled (no LODs at all).\n"
		"The first frame after enabling streams in every model - this can take\n"
		"a while and uses a lot of memory.");
	if(gAidenNoViewDistance){
		ImGui::Indent();
		changed |= ImGui::SliderFloat("Far clip", &gAidenFarClip, 1000.0f, 50000.0f, "%.0f");
		ImGui::Checkbox("Fog", &gEnableFog);
		helpMarker("Fog hides distant geometry - turn it off to see the whole map.");
		ImGui::Unindent();
	}

	ImGui::SeparatorText("LOD");
	ImGui::BeginDisabled(gAidenNoViewDistance);
	changed |= ImGui::RadioButton("Game default (swap by distance)", &gAidenLodMode, AIDEN_LOD_NORMAL);
	changed |= ImGui::RadioButton("Remove all LOD (HD only)", &gAidenLodMode, AIDEN_LOD_REMOVE_ALL);
	changed |= ImGui::RadioButton("LOD only", &gAidenLodMode, AIDEN_LOD_ONLY);
	changed |= ImGui::RadioButton("All LOD active (HD + LOD together)", &gAidenLodMode, AIDEN_LOD_ALL_ACTIVE);
	ImGui::EndDisabled();
	if(gAidenNoViewDistance)
		ImGui::TextDisabled("LOD is off while view distance is disabled.");

	ImGui::SeparatorText("Full map -> Blender");
	drawFullMapExportControls();

	if(changed) qolSave();
	ImGui::End();
}

void
uiAidenQolMenu(bool *openWindow)
{
	qolLoad();
	if(!ImGui::BeginMenu("Aiden Fork QOL"))
		return;
	if(ImGui::MenuItem("Open QOL panel", nil, *openWindow))
		*openWindow = !*openWindow;
	ImGui::Separator();
	bool changed = false;
	if(ImGui::MenuItem("Disable all view distance", nil, gAidenNoViewDistance)){
		gAidenNoViewDistance = !gAidenNoViewDistance;
		changed = true;
	}
	if(ImGui::MenuItem("Remove all LOD", nil, gAidenLodMode == AIDEN_LOD_REMOVE_ALL, !gAidenNoViewDistance)){
		gAidenLodMode = gAidenLodMode == AIDEN_LOD_REMOVE_ALL ? AIDEN_LOD_NORMAL : AIDEN_LOD_REMOVE_ALL;
		changed = true;
	}
	if(ImGui::MenuItem("All LOD active", nil, gAidenLodMode == AIDEN_LOD_ALL_ACTIVE, !gAidenNoViewDistance)){
		gAidenLodMode = gAidenLodMode == AIDEN_LOD_ALL_ACTIVE ? AIDEN_LOD_NORMAL : AIDEN_LOD_ALL_ACTIVE;
		changed = true;
	}
	ImGui::Separator();
	if(ImGui::MenuItem(fm.running ? "Full-map export running..." : "Export FULL MAP to Blender", nil, false, !fm.running)){
		if(startFullMapExport(gFmOpts, false))
			Toast(TOAST_SAVE, "Exporting %d instances to Blender...", (int)fm.list.size());
		else
			Toast(TOAST_SAVE, "%s", fm.lastResult);
	}
	if(changed) qolSave();
	ImGui::EndMenu();
}
