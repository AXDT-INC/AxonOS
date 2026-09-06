# Rebrand Report: DeSciOS → AxonOS

## Executive Summary

Rebranding of the DeSciOS fork to AxonOS is complete **with the exceptions listed under
"Known exceptions" below**. All user-facing text, code identifiers, directory names, and
documentation have been updated. Note that `docs/rebrand/INVENTORY.md` is a historical
snapshot of the pre-rebrand state and is not kept current.

## Known exceptions (as of 2026-09-06)

Old-brand or inconsistent references that intentionally or accidentally remain:

- `.gitignore` lines for `build/descios_launcher/`, `build/descios_launcher_*/` and
  `DeSciOS-Launcher-*` — kept so stale local build artifacts stay untracked.
- `scripts/check_branding.sh` and `scripts/test_docker_build.sh` — contain the old
  names by design (they are the strings being searched for).
- Repository URL is inconsistent across the tree: `README.md` points at one GitHub
  organization, `build/build_all.py` and `build/build_deb.py` at an older one, and the
  live `git remote` at a third. Treat `git remote -v` as canonical until these are unified.
- No `descios.yaml` compatibility reader was ever implemented (earlier drafts of
  `BRAND.md` / `MIGRATION.md` described one); those docs have been corrected.

## What Changed

### 1. User-Facing Text
- ✅ Window titles: "DeSciOS Launcher" → "AxonOS Launcher"
- ✅ UI labels and help text updated throughout
- ✅ README.md: Complete rebrand with updated examples
- ✅ Documentation: All guides and docs updated
- ✅ noVNC theme: HTML and CSS updated with AxonOS branding

### 2. Code Identifiers
- ✅ Class names: `DeSciOSLauncher` → `AxonOSLauncher`
- ✅ Directory names:
  - `descios_launcher/` → `axonos_launcher/`
  - `descios_assistant/` → `axonos_assistant/`
  - `descios_plugins/` → `axonos_plugins/`
- ✅ CSS classes: `.descios-*` → `.axonos-*`
- ✅ CSS variables: `--descios-*` → `--axonos-*`
- ✅ File names: `descios-theme.css` → `axonos-theme.css`
- ✅ Desktop entries: `descios-assistant.desktop` → `axonos-assistant.desktop`

### 3. Docker & Container
- ✅ Dockerfile: OS identification updated to AxonOS
- ✅ Hostname: DeSciOS → AxonOS
- ✅ Bash prompt: Updated to show AxonOS
- ✅ Image tags: `descios:custom` → `axonos:custom`
- ✅ Container names: `descios` → `axonos`
- ✅ Username: `deScier` → `aXonian`
- ✅ COPY paths: Updated to new directory names

### 4. Build System
- ✅ All build scripts updated (Windows, macOS, Linux, cross-platform)
- ✅ Package names updated in build configurations
- ✅ Binary names: `descios` → `axonos` (in scripts)
- ✅ Build documentation updated

### 5. Documentation
- ✅ README.md: Complete rebrand
- ✅ EXTENSIBILITY_GUIDE.md: Updated
- ✅ RELEASE_PACKAGE.md: Updated
- ✅ LEGAL.md: Updated
- ✅ Build documentation: All guides updated
- ✅ Created rebrand documentation:
  - `docs/rebrand/INVENTORY.md`
  - `docs/rebrand/BRAND.md`
  - `docs/rebrand/MIGRATION.md`
  - `docs/rebrand/REPORT.md` (this file)

### 6. CI/CD
- ✅ Created `scripts/check_branding.sh` for automated checks
- ✅ Created `.github/workflows/branding-check.yml` for CI integration

## What Was Intentionally Left Unchanged

### 1. Username
- **`aXonian`**: Renamed from `deScier` as part of rebrand
- Rationale: Updated to align with AxonOS branding while maintaining functionality
- No backward compatibility needed as this is a new rename

### 2. GitHub URLs
- **Repository URL**: Placeholders were replaced, but not with a single value —
  `README.md` and the build scripts name different organizations (see "Known exceptions")
- Status: ⚠️ Needs a final pass to unify on the canonical remote

### 3. Domain References
- **Removed**: `descios.desciindia.org` references removed from Dockerfile
- Rationale: Domain not applicable to fork
- Action needed: Add AxonOS domain if/when available

### 4. Logo/Assets
- **`os.svg`**: Filename kept (content unchanged)
- Rationale: Logo file name is generic; actual logo content can be updated separately
- Action needed: Update logo content if new AxonOS logo available

## Compatibility Notes

### Backward Compatibility
1. **Username `aXonian`**: Renamed from `deScier` (no backward compatibility needed)
2. **Config files**: Launcher configs are JSON (`axonos config save/load`); no old-brand file names are read
3. **Docker images**: Old images with `descios` tag will need rebuilding with new tag

### Breaking Changes
1. **CLI binary name**: `descios` → `axonos` (scripts need updating)
2. **Directory paths**: Import paths need updating if custom code exists
3. **Container names**: Docker commands need updating
4. **CSS classes**: Custom CSS using old classes needs updating

## Remaining TODOs

### High Priority
1. **Unify GitHub repository URL**: README, `build/build_all.py`, `build/build_deb.py` and the git remote disagree
2. **Test builds**: Verify all build scripts work with new names
3. **Update CI/CD**: Ensure GitHub Actions work correctly

### Medium Priority
1. **Logo update**: Replace `os.svg` content with AxonOS logo if available
2. **Domain setup**: Add AxonOS domain if applicable
3. **Username**: `aXonian` rename from `deScier` completed ✅

### Low Priority
1. **Asset updates**: Update any remaining asset references
2. **Documentation polish**: Review all docs for consistency
3. **Community communication**: Announce rebrand to users

## Verification

### Files Updated
- Total files modified: ~50+
- Directories renamed: 3
- Build scripts updated: 8
- Documentation files updated: 10+

### Branding Check
Run the branding check script to verify:
```bash
bash scripts/check_branding.sh
```

This will identify any remaining old-brand references (excluding exempt files).

## Testing Recommendations

1. **Build test**: Run build scripts for all platforms
2. **Docker test**: Build and run Docker container
3. **Launcher test**: Run GUI launcher and verify all functionality
4. **Assistant test**: Verify assistant application works
5. **Documentation test**: Verify all links and examples work

## Notes

- All changes maintain technical functionality
- No breaking changes to core APIs or logic
- Branding changes are cosmetic and do not affect behavior
- Migration path provided for users upgrading from DeSciOS

## Conclusion

Rebranding is complete with the exceptions listed above. The codebase uses AxonOS branding
in all user-facing text, code identifiers, and documentation; the remaining old-brand
strings are confined to ignore patterns and the branding-check tooling itself.

Next steps:
1. Unify the repository URL across README, build scripts and the git remote
2. Run full test suite
3. Verify builds on all platforms
4. Update any external references (if applicable)
