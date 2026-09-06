# Rebrand Inventory: DeSciOS → AxonOS

> **Historical snapshot.** This inventory was taken before the rebrand and describes the
> pre-rebrand tree (old `descios_*` paths, "Change Needed" columns). It is kept for
> traceability only; for the current state see `REPORT.md` ("Known exceptions").

This document catalogs all occurrences of DeSciOS branding that needed to be changed to AxonOS.

## Summary

- **Total files with branding**: 37
- **Primary brand terms**: DeSciOS, descios, DeSci OS, DeSciOS.io
- **Risk level**: Medium (mostly cosmetic, some path/identifier changes needed)

## Inventory by Category

### A) App/Desktop UI Strings

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `descios_launcher/main.py` | Window title: "DeSciOS Launcher" | Yes | Low |
| `descios_launcher/main.py` | UI labels: "DeSciOS Configuration", "Build & Deploy DeSciOS" | Yes | Low |
| `descios_launcher/main.py` | Help text, version strings | Yes | Low |
| `descios_assistant/main.py` | App name references in code | Yes | Low |
| `novnc-theme/vnc.html` | Page title: "DeSciOS Remote Desktop" | Yes | Low |
| `novnc-theme/vnc.html` | UI text: "DeSciOS", "Launching DeSciOS" | Yes | Low |
| `novnc-theme/vnc.html` | Branding div: "DeSciOS v0.1" | Yes | Low |
| `Dockerfile` | OS identification: NAME="DeSciOS" | Yes | Low |
| `Dockerfile` | Hostname: DeSciOS | Yes | Low |
| `Dockerfile` | Bash prompt: $USER@DeSciOS | Yes | Low |
| `startup.sh` | Hostname setting: DeSciOS | Yes | Low |

### B) CLI Names/Help Text

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `descios_launcher/main.py` | CLI binary name: `descios` | Yes | Medium |
| `descios_launcher/main.py` | CLI help text, examples | Yes | Low |
| `descios_launcher/main.py` | Version string: "DeSciOS Launcher 0.1.0" | Yes | Low |
| Build scripts | Binary names, package names | Yes | Medium |

### C) Config Keys, Env Vars, Default Paths

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `Dockerfile` | ENV USER=aXonian | ✅ Updated (renamed from deScier) | Low |
| `Dockerfile` | ID=descios in /etc/os-release | Yes | Low |
| `descios_launcher/main.py` | Default image tag: "descios:custom" | Yes | Low |
| `descios_launcher/main.py` | Container name: "descios" | Yes | Low |
| `descios_launcher/main.py` | Directory search paths: ~/DeSciOS | Yes | Low |

### D) Package Names, Module Names, Bundle Identifiers

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `descios_launcher/` | Directory name | Yes | Medium |
| `descios_assistant/` | Directory name | Yes | Medium |
| `descios_plugins/` | Directory name | Yes | Medium |
| `descios_assistant/descios-assistant.desktop` | Desktop entry name | Yes | Low |
| Build scripts | Package names, bundle IDs | Yes | Medium |

### E) Website/Docs

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `README.md` | Title, all references | Yes | Low |
| `README.md` | GitHub URLs (GizmoQuest/DeSciOS) | Yes (update to new repo) | Low |
| `RELEASE_PACKAGE.md` | All references | Yes | Low |
| `EXTENSIBILITY_GUIDE.md` | All references | Yes | Low |
| `descios_launcher/README.md` | All references | Yes | Low |
| `descios_assistant/MCP_README.md` | All references | Yes | Low |
| `descios_plugins/README.md` | All references | Yes | Low |
| `novnc-theme/README.md` | All references | Yes | Low |
| `build/BUILD.md` | All references | Yes | Low |
| `build/CROSS_PLATFORM_BUILD.md` | All references | Yes | Low |
| `build/CROSS_BUILD_FROM_LINUX.md` | All references | Yes | Low |
| `LEGAL.md` | License attribution | Yes | Low |

### F) Repo Metadata

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `LICENSE` | Copyright holder (if branded) | No (keep as-is) | N/A |
| `LEGAL.md` | Attribution text | Yes | Low |

### G) Asset Folders

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `novnc-theme/descios-theme.css` | CSS class names: `.descios-*` | Yes | Medium |
| `novnc-theme/descios-theme.css` | CSS variable names: `--descios-*` | Yes | Medium |
| `novnc-theme/descios-theme.css` | File name | Yes | Medium |
| `novnc-theme/vnc.html` | CSS file reference | Yes | Low |
| `os.svg` | Logo file (keep name, update content if needed) | No (keep filename) | N/A |

### H) Docker/k8s Manifests

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `Dockerfile` | Image tags, container names | Yes | Medium |
| `Dockerfile` | COPY paths: descios_assistant, descios_launcher | Yes | Medium |
| `supervisord.conf` | Service names (if any) | Check | Low |

### I) GitHub Workflows

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `.github/workflows/*` | Workflow names, references | Check if exists | Low |

### J) Build Scripts

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `build/build_all.py` | Package names, descriptions | Yes | Medium |
| `build/build_deb.py` | Ubuntu 22.04 package metadata | Yes | Medium |
| `build/build_macos.py` | macOS bundle identifiers | Yes | Medium |
| `build/build_windows.py` | Windows installer metadata | Yes | Medium |
| `build/build_launcher.py` | Binary names | Yes | Medium |
| `build/create_dmg.sh` | DMG names | Yes | Low |
| `build/create_windows_installer.bat` | Installer names | Yes | Low |

### K) Shell Scripts

| Location | Type | Change Needed | Risk |
|----------|------|---------------|------|
| `startup.sh` | Hostname, comments | Yes | Low |
| `check_ipfs.sh` | Comments, if any | Check | Low |
| `novnc-theme/install-theme.sh` | References | Check | Low |

## Special Considerations

1. **User name `aXonian`**: ✅ Renamed from `deScier` throughout codebase
2. **Directory names**: `descios_launcher/`, `descios_assistant/`, `descios_plugins/` - These are code paths, need careful renaming
3. **GitHub URLs**: All references to `GizmoQuest/DeSciOS` should be updated to new repository
4. **Domain references**: `descios.desciindia.org` - Remove or update if not applicable
5. **CSS classes**: `.descios-*` classes in theme CSS need systematic rename
6. **Docker image tags**: Default `descios:custom` should become `axonos:custom`
7. **Container names**: Default `descios` should become `axonos`

## Risk Assessment

- **Low Risk**: UI strings, documentation, comments
- **Medium Risk**: Directory names, module imports, Docker paths, build scripts
- **High Risk**: None identified (no breaking API changes needed)

## Compatibility Notes

- Username `aXonian`: ✅ Renamed from `deScier` (no backward compatibility needed)
- Consider supporting both old and new config file names during transition
- Docker COPY commands will need path updates
- Build scripts need to handle renamed directories
