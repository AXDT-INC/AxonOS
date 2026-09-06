# AxonOS Custom Application Plugins

This directory contains custom application definitions that extend AxonOS with additional software packages.

The launcher reads `./axonos_plugins/` relative to its working directory (normally the repo root) and creates it if missing. Only `*.yaml` and `*.json` files are loaded (`*.yml` is ignored). Files are read once at launcher start.

## Plugin Format

### YAML Format
```yaml
app_id:
  name: "Display Name"
  description: "Brief description of the application"
  dockerfile_section: "RUN command to install the application"
  enabled: false  # Optional; defaults to false
```

Fields: `name`, `description` and `dockerfile_section` are required; entries missing any of them are skipped silently. `enabled` is optional. Internally each app is registered as `custom_<app_id>`. In `dockerfile_section`, doubled backslashes (`\\`) are collapsed to single ones, which matters for JSON authors.

### JSON Format
```json
{
  "app_id": {
    "name": "Display Name",
    "description": "Brief description of the application",
    "dockerfile_section": "RUN command to install the application",
    "enabled": false
  }
}
```

## Creating Custom Applications

### Method 1: Using Templates (Recommended)

1. Open the AxonOS Launcher GUI from the repo root: `python3 axonos_launcher/main.py --gui`
2. Go to the "🧩 Custom Apps" tab
3. Choose a template from the dropdown:
   - `python_package`: Install Python packages via pip
   - `apt_package`: Install system packages via apt
   - `github_release`: Download and install from GitHub releases
   - `web_app`: Create desktop shortcuts for web applications
   - `custom`: Write your own Dockerfile commands

4. Fill in the form fields
5. Click "🔄 Update Preview" to see the generated Dockerfile section
6. Click "💾 Save Application" (writes `axonos_plugins/<app_id>.yaml` with extra `template`/`template_fields` metadata)
7. Restart the launcher; new applications appear only after a restart

### Method 2: Manual Plugin Files

1. Create a new `.yaml` or `.json` file in this directory
2. Define your applications using the format above
3. Restart AxonOS Launcher to load the new applications

Alternatively, use "📁 Load Plugin File" in the Custom Apps tab to copy an existing YAML/JSON file into this directory, then restart.

## Examples

### Simple Python Package
```yaml
scikit_learn:
  name: "Scikit-learn"
  description: "Machine learning library for Python"
  dockerfile_section: "RUN pip install --no-cache-dir scikit-learn"
  enabled: false
```

### Complex Installation with Desktop Entry
```yaml
my_research_tool:
  name: "My Research Tool"
  description: "Custom tool for my research workflow"
  dockerfile_section: |
    # Install My Research Tool
    RUN wget https://example.com/tool.tar.gz && \
        tar -xzf tool.tar.gz -C /opt && \
        rm tool.tar.gz && \
        ln -s /opt/tool/bin/tool /usr/local/bin/my-tool && \
        echo '[Desktop Entry]\nName=My Tool\nExec=my-tool\nIcon=applications-science\nType=Application\nCategories=Science;' \
        > /usr/share/applications/my-tool.desktop
  enabled: false
```

### Web Application Shortcut
```yaml
my_web_app:
  name: "My Web App"
  description: "Custom web-based analysis tool"
  dockerfile_section: |
    # My Web App (via Browser)
    RUN echo '[Desktop Entry]\nName=My Web App\nExec=firefox https://myapp.example.com\nIcon=applications-internet\nType=Application\nCategories=Science;' \
        > /usr/share/applications/my-web-app.desktop
  enabled: false
```

## Best Practices

1. **Use descriptive app_id**: Use lowercase with underscores (e.g., `my_analysis_tool`)
2. **Provide clear descriptions**: Help users understand what your application does
3. **Include desktop entries**: For GUI applications, create `.desktop` files for easy access
4. **Clean up after installation**: Remove temporary files and caches
5. **Set appropriate categories**: Use standard categories like `Science`, `Development`, `Graphics`
6. **Test your plugins**: Verify that your Dockerfile sections work correctly

## Standard Desktop Categories

- `Science` - Scientific applications
- `Development` - Programming and development tools
- `Graphics` - Image processing and visualization
- `Office` - Productivity applications
- `Education` - Educational software
- `Network` - Network and communication tools

## File Naming Convention

- Use descriptive names: `bioinformatics_tools.yaml`
- Group related applications: `python_data_science.yaml`
- Use lowercase with underscores: `machine_learning_stack.yaml`

## Sharing Plugins

To share your custom applications with the community:

1. Create a plugin file following the format above
2. Test it thoroughly with different AxonOS configurations
3. Submit it to the AxonOS community repository
4. Include documentation about any special requirements

## Troubleshooting

**Plugin not loading?**
- Check YAML/JSON syntax is valid
- Ensure the required fields `name`, `description` and `dockerfile_section` are present (a missing field skips the entry without any message)
- Restart AxonOS Launcher after adding new files

**Application not installing?**
- Test your `dockerfile_section` commands manually
- Check for missing dependencies
- Verify download URLs are accessible

**Desktop entry not appearing?**
- Use absolute paths in Exec commands
- Include proper Icon and Categories
- Call `update-desktop-database` if needed 