#!/usr/bin/env python3
"""
AxonOS Launcher Core - Shared business logic for GUI and CLI
No GUI dependencies - works on headless servers
"""

import os
import yaml
import json
from pathlib import Path

# Application templates
APPLICATION_TEMPLATES = {
    "python_package": {
        "name": "Python Package Template",
        "dockerfile_section": "RUN pip install --no-cache-dir {package}",
        "description": "Install Python package via pip",
        "fields": ["package"]
    },
    "apt_package": {
        "name": "APT Package Template", 
        "dockerfile_section": "RUN apt update && apt install -y {packages}",
        "description": "Install system package via apt",
        "fields": ["packages"]
    },
    "github_release": {
        "name": "GitHub Release Template",
        "dockerfile_section": '''# Install {name}
RUN wget {download_url} && \\
    tar -xzf {archive_name} -C /opt && \\
    rm {archive_name} && \\
    ln -s /opt/{app_dir}/{executable} /usr/local/bin/{app_name} && \\
    echo '[Desktop Entry]\\nName={name}\\nExec={app_name}\\nIcon=applications-science\\nType=Application\\nCategories=Science;' \\
    > /usr/share/applications/{app_id}.desktop''',
        "description": "Install from GitHub release tarball",
        "fields": ["download_url", "archive_name", "app_dir", "executable", "app_name"]
    },
    "web_app": {
        "name": "Web Application Template",
        "dockerfile_section": '''# {name} (via Browser)
RUN echo '[Desktop Entry]\\nName={name}\\nExec=firefox {url}\\nIcon=applications-internet\\nType=Application\\nCategories={categories};' \\
    > /usr/share/applications/{app_id}.desktop''',
        "description": "Create desktop entry for web application",
        "fields": ["url", "categories"]
    },
    "custom": {
        "name": "Custom Dockerfile Section",
        "dockerfile_section": "{custom_commands}",
        "description": "Write custom Dockerfile commands",
        "fields": ["custom_commands"]
    }
}

class AxonOSLauncherCore:
    """Core launcher logic without GUI dependencies"""
    
    def __init__(self):
        self.plugins_dir = Path("axonos_plugins")
        self.plugins_dir.mkdir(exist_ok=True)
        
        # Application definitions
        self.applications = {
            "jupyterlab": {
                "name": "JupyterLab",
                "description": "Interactive development environment for notebooks",
                "dockerfile_section": 'RUN pip install --no-cache-dir jupyterlab',
                "enabled": True
            },
            "r_rstudio": {
                "name": "R & RStudio",
                "description": "Statistical computing language and IDE",
                "dockerfile_section": '''# Install R for Ubuntu 22.04 (jammy)
RUN apt update -qq && \\
    apt install --no-install-recommends -y ca-certificates curl gnupg && \\
    install -d -m 0755 /etc/apt/keyrings && \\
    curl -fsSL https://cloud.r-project.org/bin/linux/ubuntu/marutter_pubkey.asc \\
      | gpg --dearmor -o /etc/apt/keyrings/cran.gpg && \\
    echo "deb [signed-by=/etc/apt/keyrings/cran.gpg] https://cloud.r-project.org/bin/linux/ubuntu jammy-cran40/" \\
      > /etc/apt/sources.list.d/cran.list && \\
    apt update -qq && \\
    apt install --no-install-recommends -y r-base

# Install RStudio Desktop (Open Source)
RUN apt update && apt install -y gdebi-core && \\
    wget https://download1.rstudio.org/electron/jammy/amd64/rstudio-2025.05.0-496-amd64.deb && \\
    gdebi -n rstudio-2025.05.0-496-amd64.deb && \\
    rm rstudio-2025.05.0-496-amd64.deb && \\
    echo '[Desktop Entry]\\nName=RStudio\\nExec=rstudio --no-sandbox\\nIcon=rstudio\\nType=Application\\nCategories=Development;' \\
    > /usr/share/applications/rstudio.desktop''',
                "enabled": True
            },
            "spyder": {
                "name": "Spyder",
                "description": "Scientific Python IDE",
                "dockerfile_section": 'RUN pip install --no-cache-dir spyder',
                "enabled": True
            },
            "ugene": {
                "name": "UGENE",
                "description": "Bioinformatics suite",
                "dockerfile_section": '''# Install UGENE (Bioinformatics suite)
RUN wget https://github.com/ugeneunipro/ugene/releases/download/52.1/ugene-52.1-linux-x86-64.tar.gz && \\
    tar -xzf ugene-52.1-linux-x86-64.tar.gz -C /opt && \\
    rm ugene-52.1-linux-x86-64.tar.gz && \\
    ln -s /opt/ugene-52.1/ugene /usr/local/bin/ugene && \\
    echo '[Desktop Entry]\\nName=UGENE\\nExec=ugene -ui\\nIcon=/opt/ugene-52.1/ugene.png\\nType=Application\\nCategories=Science;' \\
    > /usr/share/applications/ugene.desktop''',
                "enabled": True
            },
            "octave": {
                "name": "GNU Octave",
                "description": "MATLAB-compatible scientific computing",
                "dockerfile_section": 'RUN apt update && apt install -y octave',
                "enabled": True
            },
            "fiji": {
                "name": "Fiji (ImageJ)",
                "description": "Image processing and analysis",
                "dockerfile_section": '''# Install Fiji (ImageJ) with bundled JDK
RUN apt update && apt install -y unzip wget && \\
    wget https://downloads.imagej.net/fiji/latest/fiji-latest-linux64-jdk.zip && \\
    unzip fiji-latest-linux64-jdk.zip -d /opt && \\
    rm fiji-latest-linux64-jdk.zip && \\
    chown $USER:$USER -R /opt/Fiji && \\
    chmod +x /opt/Fiji/fiji-linux-x64 && \\
    echo 'alias fiji=/opt/Fiji/fiji-linux-x64' >> /home/$USER/.bashrc && \\
    echo '[Desktop Entry]\\nName=Fiji\\nExec=bash -c "cd /opt/Fiji && ./fiji"\\nIcon=applications-science\\nType=Application\\nCategories=Science;' \\
    > /usr/share/applications/fiji.desktop''',
                "enabled": True
            },
            "nextflow": {
                "name": "Nextflow",
                "description": "Workflow management system",
                "dockerfile_section": '''# Install Nextflow
RUN apt-get update && apt-get install -y openjdk-17-jre-headless && \\
    apt-get clean && rm -rf /var/lib/apt/lists/* && \\
    curl -s https://get.nextflow.io | bash && \\
    mv /nextflow /usr/bin/nextflow && \\
    chmod +x /usr/bin/nextflow && \\
    chown $USER:$USER /usr/bin/nextflow''',
                "enabled": True
            },
            "gromacs": {
                "name": "GROMACS (MPI)",
                "description": "Molecular dynamics package (release-2026, MPI-enabled)",
                "dockerfile_section": '''# Install GROMACS (release-2026, MPI-enabled)
RUN apt update && apt install -y \\
    && apt clean && \\
    git clone --branch release-2026 --depth 1 https://github.com/gromacs/gromacs.git /opt/gromacs-src && \\
    CUFFTMP_INCLUDE="$(find /opt/nvidia/hpc_sdk /usr/local/cuda -type f -iname 'cufft*mp*.h' 2>/dev/null | head -n 1)" && \\
    CUFFTMP_LIBRARY="$(find /opt/nvidia/hpc_sdk /usr/local/cuda -type f -iname 'libcufft*mp*.so*' 2>/dev/null | head -n 1)" && \\
    CUFFTMP_ROOT="$(dirname "${CUFFTMP_INCLUDE}")/.." && \\
    if [ -z "$CUFFTMP_ROOT" ] || [ -z "$CUFFTMP_INCLUDE" ] || [ -z "$CUFFTMP_LIBRARY" ]; then \\
      echo "cuFFTMp not found under /opt/nvidia/hpc_sdk or /usr/local/cuda" >&2; \\
      find /opt/nvidia/hpc_sdk -maxdepth 4 -type d 2>/dev/null || true; \\
      exit 1; \\
    fi && \\
    cmake -S /opt/gromacs-src -B /opt/gromacs-build \\
      -DGMX_BUILD_OWN_FFTW=ON \\
      -DREGRESSIONTEST_DOWNLOAD=OFF \\
      -DGMX_GPU=CUDA \\
      -DGMX_MPI=ON \\
      -DGMX_OPENMP=ON \\
      -DGMX_USE_CUFFTMP=ON \\
      -DcuFFTMp_ROOT="${CUFFTMP_ROOT}" \\
      -DcuFFTMp_INCLUDE_DIR="$(dirname "${CUFFTMP_INCLUDE}")" \\
      -DcuFFTMp_LIBRARY="${CUFFTMP_LIBRARY}" \\
      -DCUDAToolkit_ROOT=/usr/local/cuda \\
      -DCMAKE_CUDA_ARCHITECTURES="${GMX_CUDA_ARCHS}" \\
      -DCMAKE_INSTALL_PREFIX=/opt/gromacs && \\
    cmake --build /opt/gromacs-build -j"$(nproc)" && \\
    cmake --install /opt/gromacs-build && \\
    ln -s /opt/gromacs/bin/gmx_mpi /usr/local/bin/gmx && \\
    ln -s /opt/gromacs/bin/gmx_mpi /usr/local/bin/gmx_mpi && \\
    echo 'source /opt/gromacs/bin/GMXRC' > /etc/profile.d/gromacs.sh && \\
    echo 'source /opt/gromacs/bin/GMXRC' >> /home/$USER/.bashrc && \\
    rm -rf /opt/gromacs-src /opt/gromacs-build && \\
    echo '[Desktop Entry]\\nName=GROMACS (MPI)\\nExec=bash -lc "gmx"\\nIcon=applications-science\\nType=Application\\nTerminal=true\\nCategories=Science;' \\
    > /usr/share/applications/gromacs.desktop''',
                "enabled": True
            },
            "qgis_grass": {
                "name": "QGIS & GRASS GIS",
                "description": "Geographic Information Systems",
                "dockerfile_section": '''# Install QGIS and GRASS GIS 8
RUN apt update && apt install -y qgis qgis-plugin-grass grass && \\
    sed -i 's|^Exec=grass$|Exec=bash -c "export GRASS_PYTHON=/usr/bin/python3; grass"|' /usr/share/applications/grass82.desktop && \\
    echo 'export GRASS_PYTHON=/usr/bin/python3' >> /home/$USER/.bashrc && \\
    echo 'export GRASS_PYTHON=/usr/bin/python3' >> /root/.bashrc && \\
    update-desktop-database /usr/share/applications''',
                "enabled": True
            },
            "syncthing": {
                "name": "Syncthing",
                "description": "Continuous file synchronization",
                "dockerfile_section": 'RUN apt update && apt install -y syncthing',
                "enabled": True
            },
            "ethercalc": {
                "name": "EtherCalc",
                "description": "Collaborative spreadsheet (browser-based)",
                "dockerfile_section": '''# EtherCalc (via Browser)
RUN echo '[Desktop Entry]\\nName=EtherCalc\\nExec=firefox https://calc.domainepublic.net\\nIcon=applications-office\\nType=Application\\nCategories=Office;' \\
    > /usr/share/applications/ethercalc.desktop''',
                "enabled": True
            },
            "beakerx": {
                "name": "BeakerX",
                "description": "Multi-language kernel extension for JupyterLab",
                "dockerfile_section": '''# BeakerX for JupyterLab (multi-language kernel extension)
RUN pip install --no-cache-dir beakerx && \\
    beakerx install''',
                "enabled": True
            },
            "ngl_viewer": {
                "name": "NGL Viewer",
                "description": "Molecular visualization (browser-based)",
                "dockerfile_section": '''# NGL Viewer (via Browser)
RUN echo '[Desktop Entry]\\nName=NGL Viewer\\nExec=firefox https://nglviewer.org/ngl\\nIcon=applications-science\\nType=Application\\nCategories=Science;' \\
    > /usr/share/applications/nglviewer.desktop''',
                "enabled": True
            },
            "remix_ide": {
                "name": "Remix IDE",
                "description": "Ethereum development environment (browser-based)",
                "dockerfile_section": '''# Remix IDE (via Browser)
RUN echo '[Desktop Entry]\\nName=Remix IDE\\nExec=firefox https://remix.ethereum.org\\nIcon=applications-development\\nType=Application\\nCategories=Development;' \\
    > /usr/share/applications/remix-ide.desktop''',
                "enabled": True
            },
            "nault": {
                "name": "Nault",
                "description": "Nano cryptocurrency wallet (browser-based)",
                "dockerfile_section": '''# Nault (Nano wallet via Browser)
RUN echo '[Desktop Entry]\\nName=Nault\\nExec=firefox https://nault.cc\\nIcon=applications-finance\\nType=Application\\nCategories=Finance;' \\
    > /usr/share/applications/nault.desktop''',
                "enabled": True
            },
            "cellmodeller": {
                "name": "CellModeller",
                "description": "Bacterial cell growth simulation",
                "dockerfile_section": '''# CellModeller
# Install Qt5 and X11 dependencies for CellModeller GUI
RUN apt update && apt install -y \\
    qtbase5-dev qtchooser qt5-qmake qtbase5-dev-tools \\
    libqt5widgets5 libqt5gui5 libqt5core5a \\
    libqt5opengl5 libqt5opengl5-dev \\
    libxcb1 libxcb-glx0 libxcb-keysyms1 \\
    libxcb-image0 libxcb-shm0 libxcb-icccm4 \\
    libxcb-sync1 libxcb-xfixes0 libxcb-shape0 \\
    libxcb-randr0 libxcb-render-util0 \\
    libxkbcommon-x11-0 libxkbcommon0 \\
    libxcb-xinerama0 libxcb-cursor0 \\
    mesa-utils x11-apps && apt clean

# Clone and install CellModeller
WORKDIR /opt
RUN git clone https://github.com/cellmodeller/CellModeller.git && \\
    cd /opt/CellModeller && pip install -e . && \\
    mkdir /opt/data && \\
    chown -R $USER:$USER /opt/data && \\
    echo '[Desktop Entry]\\nName=CellModeller\\nExec=bash -c "/usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py"\\nIcon=applications-science\\nType=Application\\nTerminal=true\\nCategories=Science;' \\
    > /usr/share/applications/cellmodeller.desktop && \\
    chmod 644 /usr/share/applications/cellmodeller.desktop && \\
    update-desktop-database /usr/share/applications''',
                "enabled": True
            }
        }
        
        self.custom_applications = {}
        self.load_custom_applications()
        
        # Configuration
        self.app_selections = {app_id: app_info.get('enabled', False) 
                              for app_id, app_info in self.get_all_applications().items()}
        self.ollama_models = ['gemma4:31b', 'granite3.2-vision']
        self.username = 'aXonian'
        self.password = 'axonpassword'
        self.gpu_enabled = False
        self.gmx_cuda_archs = '70;75;86;89'
        self.gmx_use_cufftmp = True
        self.image_tag = 'axonos:custom'
    
    def load_custom_applications(self):
        """Load custom applications from plugins directory"""
        try:
            for plugin_file in self.plugins_dir.glob("*.yaml"):
                try:
                    with open(plugin_file, 'r') as f:
                        plugin_data = yaml.safe_load(f)
                    
                    if isinstance(plugin_data, dict):
                        for app_id, app_info in plugin_data.items():
                            if self.validate_app_definition(app_info):
                                if "dockerfile_section" in app_info:
                                    app_info["dockerfile_section"] = app_info["dockerfile_section"].replace("\\\\", "\\")
                                self.custom_applications[f"custom_{app_id}"] = {
                                    **app_info,
                                    "enabled": app_info.get("enabled", False),
                                    "source": str(plugin_file)
                                }
                except Exception as e:
                    print(f"Error loading plugin {plugin_file}: {e}")
            
            for plugin_file in self.plugins_dir.glob("*.json"):
                try:
                    with open(plugin_file, 'r') as f:
                        plugin_data = json.load(f)
                    
                    if isinstance(plugin_data, dict):
                        for app_id, app_info in plugin_data.items():
                            if self.validate_app_definition(app_info):
                                if "dockerfile_section" in app_info:
                                    app_info["dockerfile_section"] = app_info["dockerfile_section"].replace("\\\\", "\\")
                                self.custom_applications[f"custom_{app_id}"] = {
                                    **app_info,
                                    "enabled": app_info.get("enabled", False),
                                    "source": str(plugin_file)
                                }
                except Exception as e:
                    print(f"Error loading plugin {plugin_file}: {e}")
        except Exception as e:
            print(f"Error loading custom applications: {e}")
    
    def validate_app_definition(self, app_info):
        """Validate that an application definition has required fields"""
        required_fields = ["name", "description", "dockerfile_section"]
        return all(field in app_info for field in required_fields)
    
    def get_all_applications(self):
        """Get combined dictionary of built-in and custom applications"""
        all_apps = self.applications.copy()
        all_apps.update(self.custom_applications)
        return all_apps
    
    def get_qt_dependencies(self):
        return '''RUN apt update && apt install -y \\
    qtbase5-dev qtchooser qt5-qmake qtbase5-dev-tools \\
    libqt5widgets5 libqt5gui5 libqt5core5a \\
    libqt5opengl5 libqt5opengl5-dev \\
    libxcb1 libxcb-glx0 libxcb-keysyms1 \\
    libxcb-image0 libxcb-shm0 libxcb-icccm4 \\
    libxcb-sync1 libxcb-xfixes0 libxcb-shape0 \\
    libxcb-randr0 libxcb-render-util0 \\
    libxkbcommon-x11-0 libxkbcommon0 \\
    libxcb-xinerama0 libxcb-cursor0 \\
    mesa-utils x11-apps && apt clean'''
    
    def is_default_configuration(self):
        """Check if current configuration matches defaults"""
        builtin_defaults_match = all(
            self.app_selections.get(app_id, False) == self.applications[app_id]['enabled'] 
            for app_id in self.applications.keys()
        )
        
        custom_apps_enabled = any(
            self.app_selections.get(app_id, False)
            for app_id in self.custom_applications.keys()
        )
        
        default_models = self.ollama_models == ['gemma4:31b', 'granite3.2-vision']
        default_user = self.username == 'aXonian'
        default_password = self.password == 'axonpassword'
        default_cuda_archs = self.gmx_cuda_archs == '70;75;86;89'
        default_cufftmp = self.gmx_use_cufftmp is True
        
        return builtin_defaults_match and not custom_apps_enabled and default_models and default_user and default_password and default_cuda_archs and default_cufftmp
    
    def generate_dockerfile(self, output_path='Dockerfile.custom'):
        """Generate custom Dockerfile"""
        if self.is_default_configuration():
            print("✅ Using original Dockerfile (default configuration)")
            print("💡 You can build directly without generating a custom Dockerfile!")
            return
        
        # Read the original Dockerfile
        with open('Dockerfile', 'r') as f:
            original_content = f.read()
        
        lines = original_content.split('\n')
        
        # Find section boundaries
        optional_section_start = None
        mandatory_section_start = None
        
        for i, line in enumerate(lines):
            if 'Install pip for system Python' in line:
                optional_section_start = i + 2
            elif 'Install AxonOS Assistant' in line:
                mandatory_section_start = i
                break
        
        # Generate new Dockerfile content
        new_content = []
        
        # Add base section (everything before optional apps) but exclude JupyterLab
        base_end = optional_section_start - 2 if optional_section_start else len(lines)
        for i, line in enumerate(lines[:base_end]):
            if 'pip install --no-cache-dir jupyterlab' in line:
                continue
            new_content.append(line)
        
        # Add mandatory python3-pip installation
        new_content.append("\n# Install pip for system Python (mandatory)")
        new_content.append("RUN apt update && apt install -y python3-pip")
        
        # Add essential Qt dependencies
        new_content.append("\n# Essential GUI dependencies for Qt/X11 applications")
        new_content.append(self.get_qt_dependencies())
        
        # Add selected applications
        all_apps = self.get_all_applications()
        for app_id, selected in self.app_selections.items():
            if selected and app_id in all_apps:
                app_info = all_apps[app_id]
                new_content.append(f"\n# {app_info['name']}")
                if app_id == "cellmodeller":
                    new_content.append('''# CellModeller
# Install Qt5 and X11 dependencies for CellModeller GUI
''' + self.get_qt_dependencies() + '''

# Clone and install CellModeller
WORKDIR /opt
RUN git clone https://github.com/cellmodeller/CellModeller.git && \\
    cd /opt/CellModeller && pip install -e . && \\
    mkdir /opt/data && \\
    chown -R $USER:$USER /opt/data && \\
    echo '[Desktop Entry]\\nName=CellModeller\\nExec=bash -c "/usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py"\\nIcon=applications-science\\nType=Application\\nTerminal=true\\nCategories=Science;' \\
    > /usr/share/applications/cellmodeller.desktop && \\
    chmod 644 /usr/share/applications/cellmodeller.desktop && \\
    update-desktop-database /usr/share/applications''')
                else:
                    new_content.append(app_info['dockerfile_section'])
        
        # Add mandatory AxonOS Assistant section
        new_content.append('''
# Install AxonOS Assistant
WORKDIR /opt
COPY axonos_assistant /opt/axonos_assistant
RUN cd /opt/axonos_assistant && \\
    /usr/bin/python3 -m pip install --break-system-packages -r requirements.txt && \\
    chmod +x main.py && \\
    cp axonos-assistant.desktop /usr/share/applications/ && \\
    chown -R $USER:$USER /opt/axonos_assistant

# Install Talk to K Assistant
COPY talk_to_k /opt/talk_to_k
RUN cd /opt/talk_to_k && \\
    /usr/bin/python3 -m pip install --break-system-packages -r requirements.txt && \\
    chmod +x main.py && \\
    cp talk-to-k.desktop /usr/share/applications/ && \\
    chown -R $USER:$USER /opt/talk_to_k

# Install AxonOS Assistant font
RUN apt-get update && apt-get install -y wget fontconfig && \\
    mkdir -p /usr/share/fonts/truetype/orbitron && \\
    wget -O /usr/share/fonts/truetype/orbitron/Orbitron.ttf https://github.com/google/fonts/raw/main/ofl/orbitron/Orbitron%5Bwght%5D.ttf && \\
    fc-cache -f -v

# Install IPFS CLI & Desktop (Mandatory)
RUN wget https://dist.ipfs.tech/kubo/v0.24.0/kubo_v0.24.0_linux-amd64.tar.gz && \\
    tar -xzf kubo_v0.24.0_linux-amd64.tar.gz && \\
    cd kubo && \\
    bash install.sh && \\
    cd .. && \\
    rm -rf kubo kubo_v0.24.0_linux-amd64.tar.gz

# Install IPFS Desktop (GUI)
RUN wget https://github.com/ipfs/ipfs-desktop/releases/download/v0.30.2/ipfs-desktop-0.30.2-linux-amd64.deb && \\
    apt install -y ./ipfs-desktop-0.30.2-linux-amd64.deb && \\
    rm ipfs-desktop-0.30.2-linux-amd64.deb

# Configure IPFS for automatic startup
RUN mkdir -p /home/$USER/.ipfs && \\
    chown -R $USER:$USER /home/$USER/.ipfs && \\
    echo 'export IPFS_PATH=/home/aXonian/.ipfs' >> /home/$USER/.bashrc && \\
    echo 'export IPFS_PATH=/home/aXonian/.ipfs' >> /root/.bashrc

# Copy IPFS status checker script
COPY check_ipfs.sh /usr/local/bin/check_ipfs.sh
RUN chmod +x /usr/local/bin/check_ipfs.sh

# Add IPFS status checker desktop entry
COPY ipfs-status.desktop /usr/share/applications/ipfs-status.desktop''')
        
        # Add the rest of the Dockerfile (OpenCL, user setup, etc.)
        for i, line in enumerate(lines):
            if 'OpenCL configuration' in line:
                new_content.extend(lines[i:])
                break
        
        # Update Ollama models section
        if self.ollama_models:
            for i, line in enumerate(new_content):
                if 'ollama pull' in line and 'RUN ollama serve' in line:
                    pull_commands = ' && '.join([f'ollama pull {model}' for model in self.ollama_models])
                    new_content[i] = f'RUN ollama serve & sleep 5 && {pull_commands}'
                    break
        
        # Update user and password
        for i, line in enumerate(new_content):
            if line.startswith('ENV USER='):
                new_content[i] = f'ENV USER={self.username}'
            elif line.startswith('ARG PASSWORD='):
                new_content[i] = f'ARG PASSWORD={self.password}'
            elif line.startswith('ARG GMX_CUDA_ARCHS='):
                new_content[i] = f'ARG GMX_CUDA_ARCHS="{self.gmx_cuda_archs}"'

        # Optionally disable cuFFTMp flags for single-GPU builds
        if not self.gmx_use_cufftmp:
            filtered = []
            for line in new_content:
                if 'GMX_USE_CUFFTMP' in line or 'cuFFTMp_' in line or 'CUFFTMP_' in line:
                    continue
                filtered.append(line)
            new_content = filtered
        
        # Write new Dockerfile
        with open(output_path, 'w') as f:
            f.write('\n'.join(new_content))
    
    def get_config(self):
        """Get current configuration as dictionary"""
        return {
            'applications': self.app_selections,
            'ollama_models': self.ollama_models,
            'username': self.username,
            'password': self.password,
            'image_tag': self.image_tag,
            'gpu_enabled': self.gpu_enabled,
            'gmx_cuda_archs': self.gmx_cuda_archs,
            'gmx_use_cufftmp': self.gmx_use_cufftmp
        }
    
    def load_config(self, config):
        """Load configuration from dictionary"""
        self.app_selections = config.get('applications', self.app_selections)
        self.ollama_models = config.get('ollama_models', self.ollama_models)
        self.username = config.get('username', self.username)
        self.password = config.get('password', self.password)
        self.image_tag = config.get('image_tag', self.image_tag)
        self.gpu_enabled = config.get('gpu_enabled', self.gpu_enabled)
        self.gmx_cuda_archs = config.get('gmx_cuda_archs', self.gmx_cuda_archs)
        self.gmx_use_cufftmp = config.get('gmx_use_cufftmp', self.gmx_use_cufftmp)
    
    def set_password(self, password):
        """Set VNC password"""
        self.password = password

    def set_cuda_archs(self, cuda_archs):
        """Set CUDA architectures for GROMACS build"""
        self.gmx_cuda_archs = cuda_archs

    def set_gmx_cufftmp(self, enabled):
        """Enable/disable cuFFTMp (multi-GPU FFT) for GROMACS"""
        self.gmx_use_cufftmp = bool(enabled)
