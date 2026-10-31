#pragma once

// Ariane version — bump this when releasing updates.
// Keep this monotonic for the updater: after 1.29, use 1.30, not 1.3.
#define ARIANE_VERSION "1.40.10"

// Update channel — set at build time via preprocessor define
// Defaults to "master" if not set by the build system
#ifndef ARIANE_CHANNEL
#define ARIANE_CHANNEL "master"
#endif

// Aiden Fork: GitHub "<owner>/<repo>" whose releases the updater checks.
// Empty = update check disabled. Set by premake (--update-repo) in CI.
#ifndef ARIANE_UPDATE_REPO
#define ARIANE_UPDATE_REPO ""
#endif
#define ARIANE_FORK_NAME "Aiden Fork"

// Display name for the channel (shown in title bar / update popup)
#define ARIANE_CHANNEL_DISPLAY (strcmp(ARIANE_CHANNEL, "PE") == 0 ? "FLA" : ARIANE_CHANNEL)
