# Migration Guide: DeSciOS → AxonOS

This document describes breaking changes and migration steps for users upgrading from DeSciOS to AxonOS.

## Overview

AxonOS is a rebranded fork of DeSciOS. The core functionality remains unchanged, but branding, naming, and some paths have been updated.

## Breaking Changes

### 1. CLI Binary Name

**Old**: `descios`
**New**: `axonos`

**Migration**:
```bash
# Old command
descios --version

# New command
axonos --version
```

**Note**: If you have scripts using `descios`, update them to use `axonos`.

### 2. Directory Names

**Old**: 
- `descios_launcher/`
- `descios_assistant/`
- `descios_plugins/`

**New**:
- `axonos_launcher/`
- `axonos_assistant/`
- `axonos_plugins/`

**Migration**: If you have custom code importing from these directories, update import paths:
```python
# Old
from descios_launcher import something

# New
from axonos_launcher import something
```

### 3. Docker Image Tags

**Old**: `descios:custom`, `descios:latest`
**New**: `axonos:custom`, `axonos:latest`

**Migration**:
```bash
# Rebuild with new tag
docker build -t axonos:custom .

# Or tag existing image
docker tag descios:custom axonos:custom
```

### 4. Container Names

**Old**: `descios`
**New**: `axonos`

**Migration**:
```bash
# Stop and remove old container
docker stop descios
docker rm descios

# Start with new name
docker run -d --name axonos axonos:custom
```

### 5. Config Files

The launcher stores its configuration as JSON (`axonos config save --file axonos-config.json`
/ `axonos config load --file …`). No old-brand config file name is read; if you kept a
launcher config from the DeSciOS era, re-save it with the new CLI.

### 6. CSS Theme Classes

**Old**: `.descios-branding`, `.descios-theme`, etc.
**New**: `.axonos-branding`, `.axonos-theme`, etc.

**Migration**: If you have custom CSS or HTML using these classes, update class names.

## Non-Breaking Changes

### User-Facing Text
- Window titles, UI labels, help text now show "AxonOS" instead of "DeSciOS"
- No functional impact

### Documentation
- README, guides, and docs updated to AxonOS branding
- No code changes needed

### GitHub URLs
- Repository URLs were updated to the new location. Use the URL shown by `git remote -v`
  on a fresh clone as the canonical one; a few build-script metadata strings still
  carry older organization names (see `docs/rebrand/REPORT.md`, "Known exceptions").
- Update your git remotes:
```bash
git remote set-url origin <canonical AxonOS repository URL>
```

## Backward Compatibility

The following are maintained for compatibility:

1. **Username**: The default username `aXonian` (renamed from `deScier` as part of rebrand)
2. **Config files**: launcher configs are JSON; no old-brand file names are read
3. **Docker paths**: only the new `axonos_*` paths exist in the image

## Upgrade Steps

1. **Pull latest code**:
   ```bash
   git pull origin main
   ```

2. **Update Docker images**:
   ```bash
   docker build -t axonos:custom .
   ```

3. **Update running containers**:
   ```bash
   docker stop descios
   docker rm descios
   docker run -d --name axonos axonos:custom
   ```

4. **Update scripts**: Search for `descios` references and update to `axonos`

5. **Update config**: Rename config files if applicable

## Rollback

If you need to rollback:

1. Use previous git commit/tag
2. Rebuild Docker images with old tags
3. Restore old config files

## Support

For issues during migration:
- Check this document first
- Review `docs/rebrand/REPORT.md` for detailed changes
- Open an issue on GitHub
