# AxonOS Brand Guidelines

## Canonical Names

- **Primary name**: `AxonOS` (one word, camelCase)
- **Built-in AI name**: `AxonAI` (one word, camelCase)
- **Alternative form**: `Axon OS` (two words) - avoid unless needed for readability
- **Repository slug**: `axonos` (lowercase, no spaces)
- **Lowercase variant**: `axonos` (for IDs, paths, identifiers)

## Naming Rules

### When to Use Each Form

- **AxonOS**: Use for the operating system and platform in user-facing text, titles, and documentation
- **AxonAI**: Use for the built-in AI assistant in user-facing text and titles
- **axonos**: Use in:
  - File paths and directory names
  - Code identifiers (variables, functions, classes)
  - Docker image tags
  - Package names
  - Config file names
  - Environment variables
  - CSS class names (use `axonos-*` prefix)

### Typography

- Prefer hyphen `-` over em dash `—` in generated text
- Use standard ASCII characters in code identifiers
- Maintain consistent capitalization in user-facing text

## Brand Mapping

| Old Brand | New Brand | Context |
|-----------|-----------|---------|
| DeSciOS | AxonOS | Primary brand name |
| descios | axonos | Lowercase identifiers |
| DeSci OS | AxonOS | Avoid this form |
| DeSciOS.io | AxonOS.io | Only if domain exists; otherwise remove |
| DeSciOS Launcher | AxonOS Launcher | Application name |
| DeSciOS Assistant | AxonAI | Application name |
| deScier | aXonian | Default username |
| GizmoQuest/DeSciOS | AxonDAO-AXGT/AxonOS | GitHub repository |
| descios.desciindia.org | Remove or update | Domain references |

## Tone Guidelines

- **Technical and minimal**: No marketing hype
- **Professional**: Clear, concise descriptions
- **Accurate**: Only claim what the software does
- **Consistent**: Use same terminology throughout

## Code Style

### Python
- Module names: `axonos_launcher`, `axonos_assistant`
- Class names: `AxonOSLauncher`, `AxonAIWindow`
- Variable names: `axonos_dir`, `axonos_image`

### CSS
- Class names: `.axonos-branding`, `.axonos-theme`
- Variables: `--axonos-primary`, `--axonos-secondary`

### Shell/Docker
- Container names: `axonos`
- Image tags: `axonos:custom`, `axonos:latest`
- Directory paths: `/opt/axonos_assistant`

### File Names
- Desktop entries: `axonos-assistant.desktop`
- Config files: `axonos.yaml` (with backward compatibility for `descios.yaml`)
- Build outputs: `AxonOS-Launcher-0.1.0`

## Domain and URLs

- **Do not** add new domain references unless the domain exists
- **Remove** or update references to `descios.desciindia.org`
- **Update** GitHub URLs to new repository location
- **Do not** create placeholder domains

## Asset References

- Keep existing logo file name (`os.svg`) unless replacing with new logo
- Update CSS references to renamed theme files
- Update icon paths in desktop entries
- Maintain asset directory structure

## Backward Compatibility

- Support reading old config file names (`descios.yaml`) with deprecation warning
- Username `aXonian`: Renamed from `deScier` as part of rebrand (no backward compatibility needed)
- Docker image tags: support both old and new during transition period

## Examples

### Good
- "AxonOS is a containerized scientific computing environment."
- "Launch AxonOS with: `axonos --help`"
- "The `axonos_launcher` module provides the GUI interface."

### Avoid
- "AxonOS - The Future of Scientific Computing!" (too hype-y)
- "Axon OS" (inconsistent spacing)
- "AxonOS.io" (unless domain exists)
