FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV USER=aXonian
ARG PASSWORD=axonpassword

# Basic system setup
RUN apt update && apt install -y \
    xfce4 xfce4-goodies tightvncserver \
    novnc websockify python3 python-is-python3 python3-pip python3-websockify \
    xterm curl sudo git wget supervisor \
    dbus-x11 gvfs policykit-1 thunar \
    software-properties-common gnupg2 \
    zstd \
    bzip2 \
    libgl1-mesa-glx libglib2.0-0 \
    libsm6 libxrender1 libxext6 \
    libglvnd0 \
    libgl1 \
    libglx0 \
    libegl1 \
    mesa-utils \
    ocl-icd-libopencl1 \
    opencl-headers \
    clinfo lshw \
    freeglut3-dev \
    python3-gi \
    gir1.2-gtk-3.0 \
    gir1.2-notify-0.7 \
    x11vnc \
    xvfb \
    gir1.2-webkit2-4.0 \
    cmake \
    pkg-config \
    build-essential \
    libgtk-3-dev \
    libwebkit2gtk-4.0-dev \
    libnotify-dev \
    libglib2.0-dev \
    libgtk-3-dev \
    fonts-noto-color-emoji \
    fonts-symbola \
    adwaita-icon-theme \
    hicolor-icon-theme \
    gnome-icon-theme \
    gnome-icon-theme-symbolic \
    libgtk-3-bin \
    xdotool \
    xclip \
    x11-xserver-utils \
    xautomation \
    scrot \
    imagemagick \
    gnome-screenshot \
    x11-apps \
    && apt clean

# Warm icon caches for desktop icons
RUN gtk-update-icon-cache -f /usr/share/icons/Adwaita || true && \
    gtk-update-icon-cache -f /usr/share/icons/hicolor || true && \
    gtk-update-icon-cache -f /usr/share/icons/gnome || true

# Install Firefox ESR (non-snap) for Ubuntu base image
RUN apt update && apt install -y ca-certificates gnupg && \
    install -d -m 0755 /etc/apt/keyrings && \
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
      | gpg --dearmor -o /etc/apt/keyrings/mozilla.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/mozilla.gpg] https://packages.mozilla.org/apt mozilla main" \
      > /etc/apt/sources.list.d/mozilla.list && \
    apt update && apt install -y firefox-esr && \
    apt clean && rm -rf /var/lib/apt/lists/*

# Set up OS identification
RUN echo 'NAME="AxonOS"\n\
VERSION="0.1"\n\
ID=axonos\n\
ID_LIKE=ubuntu\n\
PRETTY_NAME="AxonOS"\n\
VERSION_ID="0.1"\n\
SUPPORT_URL="https://github.com/AxonDAO-AXGT/AxonOS/issues"\n\
BUG_REPORT_URL="https://github.com/AxonDAO-AXGT/AxonOS/issues"' > /etc/os-release && \
    echo 'AxonOS' > /etc/hostname && \
    mv /bin/uname /bin/uname.real && \
    echo '#!/bin/sh\nif [ "$1" = "-a" ]; then\n  echo -n "AxonOS " && /bin/uname.real -a\nelse\n  /bin/uname.real "$@"\nfi' > /bin/uname && \
    chmod +x /bin/uname

# Install Ollama (supply-chain hardening: optional SHA256 verification of install script)
# Provide OLLAMA_INSTALL_SHA256 to verify the downloaded script before execution.
ARG OLLAMA_INSTALL_SHA256=""
RUN curl --proto '=https' --tlsv1.2 -fsSL https://ollama.com/install.sh -o /tmp/ollama_install.sh && \
    if [ -n "$OLLAMA_INSTALL_SHA256" ]; then echo "$OLLAMA_INSTALL_SHA256  /tmp/ollama_install.sh" | sha256sum -c - ; fi && \
    sh /tmp/ollama_install.sh && \
    rm -f /tmp/ollama_install.sh

# Pull the gemma4:31b model
RUN ollama serve & sleep 5 && ollama pull granite3-guardian && ollama pull gemma4:31b && ollama pull granite3.2-vision

# Create user and set password
RUN useradd -ms /bin/bash $USER && echo "$USER:$PASSWORD" | chpasswd && adduser $USER sudo

# Configure bash prompt and hostname for the user
RUN echo 'export PS1="\[\033[01;32m\]$USER@AxonOS\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ "\n\
# Set hostname in current shell\n\
if [ -z "$HOSTNAME" ] || [[ "$HOSTNAME" =~ ^[0-9a-f]{12}$ ]]; then\n\
    export HOSTNAME=AxonOS\n\
fi' >> /home/$USER/.bashrc && \
    chown $USER:$USER /home/$USER/.bashrc

# OpenCode CLI: install as desktop user (not root) and expose on PATH for XFCE terminals
RUN su - $USER -c 'curl -fsSL https://opencode.ai/install | bash' && \
    ln -sf /home/$USER/.opencode/bin/opencode /usr/local/bin/opencode && \
    echo 'export PATH="/home/'"$USER"'/.opencode/bin:$PATH"' > /etc/profile.d/opencode.sh && \
    echo 'export PATH="/home/'"$USER"'/.opencode/bin:$PATH"' >> /home/$USER/.bashrc && \
    echo 'export PATH="/home/'"$USER"'/.opencode/bin:$PATH"' >> /home/$USER/.profile

# Install JupyterLab and other global Python tools with default pip
RUN pip install --no-cache-dir jupyterlab


# Install R for Ubuntu 22.04 (jammy)
RUN apt update -qq && \
    apt install --no-install-recommends -y ca-certificates curl gnupg && \
    install -d -m 0755 /etc/apt/keyrings && \
    curl -fsSL https://cloud.r-project.org/bin/linux/ubuntu/marutter_pubkey.asc \
      | gpg --dearmor -o /etc/apt/keyrings/cran.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/cran.gpg] https://cloud.r-project.org/bin/linux/ubuntu jammy-cran40/" \
      > /etc/apt/sources.list.d/cran.list && \
    apt update -qq && \
    apt install --no-install-recommends -y r-base

# Install RStudio Desktop (Open Source)
RUN apt update && apt install -y gdebi-core && \
    wget https://download1.rstudio.org/electron/jammy/amd64/rstudio-2025.05.0-496-amd64.deb && \
    gdebi -n rstudio-2025.05.0-496-amd64.deb && \
    rm rstudio-2025.05.0-496-amd64.deb && \
    echo '[Desktop Entry]\nName=RStudio\nExec=rstudio --no-sandbox\nIcon=rstudio\nType=Application\nCategories=Development;' \
    > /usr/share/applications/rstudio.desktop

# Install Spyder (Scientific Python IDE)
# Ubuntu apt matplotlib is built against NumPy 1.x; pip NumPy 2.x breaks Spyder kernels (_ARRAY_API).
RUN pip install --no-cache-dir 'numpy>=1.24.0,<2' matplotlib spyder

# Install UGENE (Bioinformatics suite)
RUN wget https://github.com/ugeneunipro/ugene/releases/download/52.1/ugene-52.1-linux-x86-64.tar.gz && \
    tar -xzf ugene-52.1-linux-x86-64.tar.gz -C /opt && \
    rm ugene-52.1-linux-x86-64.tar.gz && \
    ln -s /opt/ugene-52.1/ugene /usr/local/bin/ugene && \
    echo '[Desktop Entry]\nName=UGENE\nExec=ugene -ui\nIcon=/opt/ugene-52.1/ugene.png\nType=Application\nCategories=Science;' \
    > /usr/share/applications/ugene.desktop

# Install GNU Octave (Matlab-like)
RUN apt update && apt install -y octave

# Install Fiji (ImageJ) with bundled JDK
RUN apt update && apt install -y unzip && \
    wget https://mirrors.pasteur.fr/fiji/downloads/stable/fiji-stable-linux64-jdk.zip && \
    unzip fiji-stable-linux64-jdk.zip -d /opt && \
    rm fiji-stable-linux64-jdk.zip && \
    chown $USER:$USER -R /opt/Fiji.app && \
    chmod +x /opt/Fiji.app/fiji-linux-x64 && \
    ln -s /opt/Fiji.app /opt/Fiji && \
    echo 'alias fiji=/opt/Fiji.app/fiji-linux-x64' >> /home/$USER/.bashrc && \
    echo '[Desktop Entry]\nName=Fiji\nExec=bash -c "cd /opt/Fiji.app && ./fiji"\nIcon=applications-science\nType=Application\nCategories=Science;' \
    > /usr/share/applications/fiji.desktop

# Install Nextflow
RUN apt-get update && apt-get install -y openjdk-17-jre-headless && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    curl -s https://get.nextflow.io | bash && \
    mv /nextflow /usr/bin/nextflow && \
    chmod +x /usr/bin/nextflow && \
    chown $USER:$USER /usr/bin/nextflow

# Install QGIS and GRASS GIS 8
RUN apt update && apt install -y qgis qgis-plugin-grass grass && \
    if [ -f /usr/share/applications/grass.desktop ]; then \
      sed -i 's|^Exec=grass$|Exec=bash -c "export GRASS_PYTHON=/usr/bin/python3; grass"|' /usr/share/applications/grass.desktop; \
    elif [ -f /usr/share/applications/grass82.desktop ]; then \
      sed -i 's|^Exec=grass$|Exec=bash -c "export GRASS_PYTHON=/usr/bin/python3; grass"|' /usr/share/applications/grass82.desktop; \
    fi && \
    echo 'export GRASS_PYTHON=/usr/bin/python3' >> /home/$USER/.bashrc && \
    echo 'export GRASS_PYTHON=/usr/bin/python3' >> /root/.bashrc && \
    update-desktop-database /usr/share/applications

# Install IPFS CLI
RUN wget https://dist.ipfs.tech/kubo/v0.24.0/kubo_v0.24.0_linux-amd64.tar.gz && \
    tar -xzf kubo_v0.24.0_linux-amd64.tar.gz && \
    cd kubo && \
    bash install.sh && \
    cd .. && \
    rm -rf kubo kubo_v0.24.0_linux-amd64.tar.gz

# Install IPFS Desktop (GUI)
RUN wget https://github.com/ipfs/ipfs-desktop/releases/download/v0.30.2/ipfs-desktop-0.30.2-linux-amd64.deb && \
    apt install -y ./ipfs-desktop-0.30.2-linux-amd64.deb && \
    rm ipfs-desktop-0.30.2-linux-amd64.deb

# Configure IPFS for automatic startup
RUN mkdir -p /home/$USER/.ipfs && \
    chown -R $USER:$USER /home/$USER/.ipfs && \
    echo 'export IPFS_PATH=/home/aXonian/.ipfs' >> /home/$USER/.bashrc && \
    echo 'export IPFS_PATH=/home/aXonian/.ipfs' >> /root/.bashrc

# Copy IPFS status checker script
COPY check_ipfs.sh /usr/local/bin/check_ipfs.sh
RUN chmod +x /usr/local/bin/check_ipfs.sh

# Add IPFS status checker desktop entry
COPY ipfs-status.desktop /usr/share/applications/ipfs-status.desktop

# Syncthing (GUI)
RUN apt update && apt install -y syncthing


# EtherCalc (via Browser)
RUN echo '[Desktop Entry]\nName=EtherCalc\nExec=firefox https://calc.domainepublic.net\nIcon=applications-office\nType=Application\nCategories=Office;' \
    > /usr/share/applications/ethercalc.desktop
# BeakerX for JupyterLab (multi-language kernel extension)
RUN pip install --no-cache-dir beakerx && \
    beakerx install
    
# NGL Viewer (via Browser)
RUN echo '[Desktop Entry]\nName=NGL Viewer\nExec=firefox https://nglviewer.org/ngl\nIcon=applications-science\nType=Application\nCategories=Science;' \
    > /usr/share/applications/nglviewer.desktop

# Remix IDE (via Browser)
RUN echo '[Desktop Entry]\nName=Remix IDE\nExec=firefox https://remix.ethereum.org\nIcon=applications-development\nType=Application\nCategories=Development;' \
    > /usr/share/applications/remix-ide.desktop

# Nault (Nano wallet via Browser)
RUN echo '[Desktop Entry]\nName=Nault\nExec=firefox https://nault.cc\nIcon=applications-finance\nType=Application\nCategories=Finance;' \
    > /usr/share/applications/nault.desktop

# Clone and install CellModeller
WORKDIR /opt
RUN git clone https://github.com/cellmodeller/CellModeller.git && \
    cd /opt/CellModeller && pip install -e . && \
    mkdir /opt/data && \
    chown -R $USER:$USER /opt/data && \
    echo '[Desktop Entry]\nName=CellModeller\nExec=bash -c "/usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py"\nIcon=applications-science\nType=Application\nTerminal=true\nCategories=Science;' \
    > /usr/share/applications/cellmodeller.desktop && \
    chmod 644 /usr/share/applications/cellmodeller.desktop && \
    update-desktop-database /usr/share/applications

# Install newer CMake (GROMACS 2026 requires >= 3.28)
RUN apt-get remove -y cmake && \
    apt-get autoremove -y && \
    wget -O /tmp/cmake.sh https://github.com/Kitware/CMake/releases/download/v4.2.3/cmake-4.2.3-linux-x86_64.sh && \
    chmod +x /tmp/cmake.sh && \
    /tmp/cmake.sh --skip-license --prefix=/usr/local && \
    rm -f /tmp/cmake.sh

ARG GMX_CUDA_ARCHS="70;75;86;89"

# Install CUDA-aware UCX + OpenMPI
RUN apt update && apt install -y \
    autoconf \
    automake \
    libevent-dev \
    libhwloc-dev \
    libibverbs-dev \
    libnuma-dev \
    libpciaccess-dev \
    librdmacm-dev \
    libtool \
    libtool-bin \
    m4 \
    flex \
    bison \
    perl \
    file \
    && apt clean && \
    git clone --depth 1 https://github.com/openucx/ucx.git /opt/ucx-src && \
    cd /opt/ucx-src && \
    ./autogen.sh && \
    ./configure --prefix=/opt/ucx --enable-mt --enable-cuda --with-cuda=/usr/local/cuda && \
    make -j"$(nproc)" && \
    make install && \
    git init /opt/ompi-src && \
    cd /opt/ompi-src && \
    git remote add origin https://github.com/open-mpi/ompi.git && \
    git fetch --depth 1 origin fc067265a0c66d9ea71837c5ea9ffc37a0435079 && \
    git checkout FETCH_HEAD && \
    git submodule update --init --recursive --depth 1 && \
    ./autogen.pl && \
    ./configure --prefix=/opt/openmpi --with-ucx=/opt/ucx --with-cuda=/usr/local/cuda --enable-mpirun-prefix-by-default && \
    make -j"$(nproc)" && \
    make install && \
    rm -rf /opt/ucx-src /opt/ompi-src

# Install NVIDIA HPC SDK (cuFFTMp + NVSHMEM) — repo + small deps first
RUN curl -fsSL https://developer.download.nvidia.com/hpc-sdk/ubuntu/DEB-GPG-KEY-NVIDIA-HPC-SDK | \
    gpg --dearmor -o /usr/share/keyrings/nvidia-hpcsdk-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-hpcsdk-archive-keyring.gpg] https://developer.download.nvidia.com/hpc-sdk/ubuntu/amd64 /" \
    > /etc/apt/sources.list.d/nvhpc.list && \
    apt update -y && \
    apt install -y --no-install-recommends gfortran gfortran-11 libgfortran-11-dev && \
    apt clean && rm -rf /var/lib/apt/lists/*

# Install NVHPC 26.1 + CUDA multi package (includes cuFFTMp)
RUN apt-get update -y && \
    apt-get -o APT::Status-Fd=2 -o Debug::pkgAcquire::Progress=1 -o DPKG::Progress-Fancy=1 install -y --no-install-recommends nvhpc-26-1-cuda-multi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV UCX_HOME=/opt/ucx
ENV OMPI_HOME=/opt/openmpi
ENV NVHPC_ROOT=/opt/nvidia/hpc_sdk
ENV NVHPC_COMM_LIBS=/opt/nvidia/hpc_sdk/Linux_x86_64/26.1/comm_libs
ENV PATH=/opt/openmpi/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/openmpi/lib:/opt/ucx/lib:${NVHPC_COMM_LIBS}/nvshmem_cufftmp_compat/lib:${NVHPC_COMM_LIBS}/12.2/nvshmem_cufftmp_compat/lib:${NVHPC_COMM_LIBS}/12.9/nvshmem_cufftmp_compat/lib:${NVHPC_COMM_LIBS}/nvshmem/lib:${NVHPC_COMM_LIBS}/12.2/nvshmem/lib:${NVHPC_COMM_LIBS}/12.9/nvshmem/lib:$LD_LIBRARY_PATH
# sm BTL fails in Docker; vader provides on-node shared memory instead.
ENV OMPI_MCA_btl=vader,self,tcp
ENV OMPI_MCA_btl_base_warn_component_unused=0

# XFCE is started by supervisord with environment= (subset only); desktop shells do not inherit Docker ENV.
RUN echo 'export OMPI_MCA_btl=vader,self,tcp' > /etc/profile.d/openmpi-mca.sh && \
    echo 'export OMPI_MCA_btl_base_warn_component_unused=0' >> /etc/profile.d/openmpi-mca.sh && \
    echo 'export OMPI_MCA_btl=vader,self,tcp' >> /home/aXonian/.bashrc && \
    echo 'export OMPI_MCA_btl_base_warn_component_unused=0' >> /home/aXonian/.bashrc && \
    echo 'export OMPI_MCA_btl=vader,self,tcp' >> /home/aXonian/.profile && \
    echo 'export OMPI_MCA_btl_base_warn_component_unused=0' >> /home/aXonian/.profile

# Ensure NVSHMEM runtime libraries are discoverable (libnvshmem_host.so.*)
# Add all nvshmem lib dirs so gmx works in desktop terminals (e.g. 12.2 vs 12.9).
RUN set -e; \
  NVSHMEM_DIRS="$(ls -d \
    /opt/nvidia/hpc_sdk/Linux_x86_64/*/comm_libs/nvshmem*/lib \
    /opt/nvidia/hpc_sdk/*/comm_libs/nvshmem*/lib \
    /opt/nvidia/hpc_sdk/*/comm_libs/*/nvshmem*/lib 2>/dev/null | sort -u)" && \
  if [ -n "$NVSHMEM_DIRS" ]; then \
    echo "$NVSHMEM_DIRS" > /etc/ld.so.conf.d/nvshmem.conf && \
    ldconfig && \
    NVSHMEM_LD_PATH="$(echo "$NVSHMEM_DIRS" | tr '\n' ':')"; \
    NVSHMEM_LD_PATH="${NVSHMEM_LD_PATH%:}"; \
    printf '%s\n' "export LD_LIBRARY_PATH=\"${NVSHMEM_LD_PATH}:\$LD_LIBRARY_PATH\"" > /etc/profile.d/nvshmem.sh && \
    printf '%s\n' "export LD_LIBRARY_PATH=\"${NVSHMEM_LD_PATH}:\$LD_LIBRARY_PATH\"" >> /home/aXonian/.bashrc && \
    printf '%s\n' "export LD_LIBRARY_PATH=\"${NVSHMEM_LD_PATH}:\$LD_LIBRARY_PATH\"" >> /home/aXonian/.profile; \
  else \
    echo "WARNING: NVSHMEM lib dir not found under /opt/nvidia/hpc_sdk" >&2; \
  fi

# Ensure nvcc (CUDA) is in PATH for desktop terminals (profile.d + .bashrc + .profile)
RUN echo 'export PATH="/usr/local/cuda/bin:$PATH"' > /etc/profile.d/cuda.sh && \
    echo 'export PATH="/usr/local/cuda/bin:$PATH"' >> /home/aXonian/.bashrc && \
    echo 'export PATH="/usr/local/cuda/bin:$PATH"' >> /home/aXonian/.profile

# Install GROMACS (release-2026, MPI-enabled)
RUN apt update && apt install -y \
    && apt clean && \
    git clone --branch release-2026 --depth 1 https://github.com/gromacs/gromacs.git /opt/gromacs-src && \
    CUFFTMP_INCLUDE="$(find /opt/nvidia/hpc_sdk /usr/local/cuda -type f -iname 'cufft*mp*.h' 2>/dev/null | head -n 1)" && \
    CUFFTMP_LIBRARY="$(find /opt/nvidia/hpc_sdk /usr/local/cuda -type f -iname 'libcufft*mp*.so*' 2>/dev/null | head -n 1)" && \
    CUFFTMP_ROOT="$(dirname "${CUFFTMP_INCLUDE}")/.." && \
    if [ -z "$CUFFTMP_ROOT" ] || [ -z "$CUFFTMP_INCLUDE" ] || [ -z "$CUFFTMP_LIBRARY" ]; then \
      echo "cuFFTMp not found under /opt/nvidia/hpc_sdk; check NVHPC install" >&2; \
      find /opt/nvidia/hpc_sdk -maxdepth 4 -type d 2>/dev/null || true; \
      exit 1; \
    fi && \
    cmake -S /opt/gromacs-src -B /opt/gromacs-build \
      -DGMX_BUILD_OWN_FFTW=ON \
      -DREGRESSIONTEST_DOWNLOAD=OFF \
      -DGMX_GPU=CUDA \
      # Library MPI (OpenMPI): use mpirun / -gpu_id for multi-rank; mdrun -ntmpi is invalid.
      -DGMX_MPI=ON \
      -DGMX_OPENMP=ON \
      -DGMX_USE_CUFFTMP=ON \
      -DcuFFTMp_ROOT="${CUFFTMP_ROOT}" \
      -DcuFFTMp_INCLUDE_DIR="$(dirname "${CUFFTMP_INCLUDE}")" \
      -DcuFFTMp_LIBRARY="${CUFFTMP_LIBRARY}" \
      -DCUDAToolkit_ROOT=/usr/local/cuda \
      -DCMAKE_CUDA_ARCHITECTURES="${GMX_CUDA_ARCHS}" \
      -DCMAKE_INSTALL_PREFIX=/opt/gromacs && \
    cmake --build /opt/gromacs-build -j"$(nproc)" && \
    cmake --install /opt/gromacs-build && \
    echo "/opt/gromacs/lib" > /etc/ld.so.conf.d/gromacs.conf && ldconfig && \
    ln -s /opt/gromacs/bin/gmx_mpi /usr/local/bin/gmx && \
    ln -s /opt/gromacs/bin/gmx_mpi /usr/local/bin/gmx_mpi && \
    echo 'source /opt/gromacs/bin/GMXRC' > /etc/profile.d/gromacs.sh && \
    echo 'source /opt/gromacs/bin/GMXRC' >> /home/$USER/.bashrc && \
    rm -rf /opt/gromacs-src /opt/gromacs-build && \
    echo '[Desktop Entry]\nName=GROMACS (MPI)\nExec=bash -lc "gmx"\nIcon=applications-science\nType=Application\nTerminal=true\nCategories=Science;' \
    > /usr/share/applications/gromacs.desktop    

# Install PyMOL (open-source from conda-forge; commercial use permitted under its license)
# See docs/PYMOL_LICENSE.md and LEGAL.md for notice and trademark.
RUN apt update && apt install -y wget && \
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p /opt/conda && \
    rm /tmp/miniconda.sh && \
    /opt/conda/bin/conda install --override-channels -c conda-forge -y pymol-open-source && \
    ln -sf /opt/conda/bin/pymol /usr/local/bin/pymol && \
    echo 'export PATH="/opt/conda/bin:$PATH"' > /etc/profile.d/conda.sh && \
    echo 'export PATH="/opt/conda/bin:$PATH"' >> /home/$USER/.bashrc && \
    echo '[Desktop Entry]\nName=PyMOL (open-source)\nComment=Molecular visualization (includes PyMOL(TM) source code)\nExec=pymol\nIcon=applications-science\nType=Application\nCategories=Science;Chemistry;\nStartupNotify=true' \
    > /usr/share/applications/pymol.desktop && \
    chmod 644 /usr/share/applications/pymol.desktop && \
    mkdir -p /usr/share/doc/pymol-open-source && \
    apt clean && rm -rf /var/lib/apt/lists/*
COPY docs/PYMOL_LICENSE.md /usr/share/doc/pymol-open-source/LICENSE

# Install AxonOS Assistant
WORKDIR /opt
COPY axonos_assistant /opt/axonos_assistant
RUN cd /opt/axonos_assistant && \
    /usr/bin/python3 -m pip install -r requirements.txt && \
    chmod +x main.py && \
    cp axonos-assistant.desktop /usr/share/applications/ && \
    chown -R $USER:$USER /opt/axonos_assistant

# Install Talk to K Assistant
COPY talk_to_k /opt/talk_to_k
RUN cd /opt/talk_to_k && \
    /usr/bin/python3 -m pip install -r requirements.txt && \
    chmod +x main.py && \
    cp talk-to-k.desktop /usr/share/applications/ && \
    chown -R $USER:$USER /opt/talk_to_k

# Copy launcher icons for panel
RUN mkdir -p /usr/share/pixmaps && \
    chmod 755 /usr/share/pixmaps
COPY novnc-theme/axonos_assistant.png /usr/share/pixmaps/axonos_assistant.png
COPY novnc-theme/talk_to_k.png /usr/share/pixmaps/talk_to_k.png
RUN chmod 644 /usr/share/pixmaps/axonos_assistant.png /usr/share/pixmaps/talk_to_k.png

# Install AxonOS Assistant font
RUN apt-get update && apt-get install -y wget fontconfig && \
    mkdir -p /usr/share/fonts/truetype/orbitron && \
    wget -O /usr/share/fonts/truetype/orbitron/Orbitron.ttf https://github.com/google/fonts/raw/main/ofl/orbitron/Orbitron%5Bwght%5D.ttf && \
    fc-cache -f -v

# OpenCL configuration
RUN mkdir -p /etc/OpenCL/vendors && \
    echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd
RUN ln -s /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 /usr/lib/libOpenCL.so
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute,display
ENV __GLX_VENDOR_LIBRARY_NAME=nvidia
ENV LIBGL_DRI3_DISABLE=1

# VirtualGL: GPU-accelerated OpenGL for apps (e.g. PyMOL) over VNC. Uses X :0 with nvidia driver.
# PackageCloud has no jammy repo; install from SourceForge .deb (3.0.2).
RUN apt update && apt install -y wget libxv1 && \
    wget -q "https://downloads.sourceforge.net/project/virtualgl/3.0.2/virtualgl_3.0.2_amd64.deb" -O /tmp/virtualgl.deb && \
    apt install -y /tmp/virtualgl.deb && \
    rm /tmp/virtualgl.deb && \
    apt clean && rm -rf /var/lib/apt/lists/*
COPY xorg.conf.nvidia /etc/X11/xorg.conf.nvidia
COPY scripts/start-xorg-nvidia.sh /usr/local/bin/start-xorg-nvidia.sh
COPY scripts/resolve-nvidia-driver-pkg-version.sh /usr/local/bin/resolve-nvidia-driver-pkg-version.sh
COPY scripts/install-nvidia-xorg-userspace.sh /usr/local/bin/install-nvidia-xorg-userspace.sh
RUN chmod +x /usr/local/bin/start-xorg-nvidia.sh /usr/local/bin/resolve-nvidia-driver-pkg-version.sh /usr/local/bin/install-nvidia-xorg-userspace.sh && \
    echo 'export VGL_DISPLAY=:0' > /etc/profile.d/virtualgl.sh && \
    echo 'export __GLX_VENDOR_LIBRARY_NAME=nvidia' >> /etc/profile.d/virtualgl.sh && \
    echo 'export LIBGL_DRI3_DISABLE=1' >> /etc/profile.d/virtualgl.sh && \
    echo 'export VGL_DISPLAY=:0' >> /home/$USER/.bashrc && \
    echo 'export __GLX_VENDOR_LIBRARY_NAME=nvidia' >> /home/$USER/.bashrc && \
    echo 'export LIBGL_DRI3_DISABLE=1' >> /home/$USER/.bashrc

# PyMOL desktop: use vglrun so OpenGL runs on GPU (X :0) when container is run with --gpus all
RUN sed -i 's#^Exec=pymol$#Exec=bash -c "vglrun pymol 2>/dev/null || pymol"#' /usr/share/applications/pymol.desktop

# Install Terminator (in universe; enable repo + update in same layer)
RUN apt-get update && apt-get install -y terminator && rm -rf /var/lib/apt/lists/*

# OpenSSH server for the direct-SSH session toggle (AXGT_SSH_ENABLED=true). Such
# sessions skip the X desktop/WebRTC capture entirely and expose only sshd;
# startup.sh writes the user's authorized_keys and (persistent) host keys at
# runtime. Baked-in default host keys are removed so each deployment generates
# its own rather than shipping a shared, publicly-known fingerprint.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && \
    mkdir -p /var/run/sshd && \
    rm -f /etc/ssh/ssh_host_* && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# AxonOS Console login banner. The Ubuntu MOTD ("Welcome to Ubuntu…", "This
# system has been minimized…") is emitted by pam_motd from /etc/update-motd.d on
# SSH login (sshd's PrintMotd no does not suppress it — PAM does). Remove that
# dynamic boilerplate + the legal notice and ship a static AxonOS banner.
COPY scripts/axonos-motd /etc/motd
RUN rm -f /etc/update-motd.d/* /etc/legal

# Switch to aXonian user
USER $USER
WORKDIR /home/$USER

# Disable session saving to avoid stale session hang
RUN mkdir -p /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml && \
    echo -e '<channel name="xfce4-session" version="1.0">\n  <property name="General">\n    <property name="SaveOnExit" type="bool" value="false"/>\n  </property>\n</channel>' \
    > /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml

# Default terminal emulator: Terminator (Open in Terminal, keyboard shortcut, etc.)
RUN mkdir -p /home/$USER/.config/xfce4 && \
    printf '%s\n' '[Terminal Emulator]' 'TerminalEmulator=terminator' > /home/$USER/.config/xfce4/helpers.rc

# Configure VNC
RUN mkdir -p /home/$USER/.vnc && \
    echo -e '#!/bin/bash\nexport VGL_DISPLAY=:0\nxrdb $HOME/.Xresources\nstartxfce4 &' > /home/$USER/.vnc/xstartup && \
    chmod +x /home/$USER/.vnc/xstartup && \
    echo "$PASSWORD" | vncpasswd -f > /home/$USER/.vnc/passwd && \
    chmod 600 /home/$USER/.vnc/passwd && \
    touch /home/$USER/.Xresources && \
    chown -R $USER:$USER /home/$USER/.vnc /home/$USER/.Xresources /home/$USER/.config

# Switch back to root for final setup
USER root

# Install WhiteSur GTK Theme (macOS-like theme)
# NOTE: WhiteSur's install.sh runs under `set -Eeo pipefail` with an ERR trap, so a
# single benign non-zero command aborts the whole script even though the theme builds
# fine. Two things matter here:
#   1. We pre-install libglib2.0-dev-bin (provides glib-compile-resources). Otherwise
#      install.sh tries to auto-install it at runtime via prepare_install_apt_packages,
#      which has an upstream bug: it returns 1 *after a successful* apt install (its last
#      statement is `[[ "$status" == "100" ]]`), tripping the ERR trap and killing the
#      build before any theme is produced.
#   2. USER/HOME are set because the build has no login session; install.sh's `logname`
#      fallback resolves to an empty $USER otherwise, failing under pipefail.
# Even with deps satisfied, install.sh can still exit non-zero on a harmless late step,
# so we don't gate on its exit code — we gate on the theme files actually existing.
RUN apt update && apt install -y \
    sassc optipng inkscape libcanberra-gtk-module libcanberra-gtk3-module \
    gtk2-engines-murrine gtk2-engines-pixbuf libxml2-utils libglib2.0-dev-bin git && \
    git clone https://github.com/vinceliuice/WhiteSur-gtk-theme.git --depth=1 /tmp/WhiteSur-gtk-theme && \
    cd /tmp/WhiteSur-gtk-theme && \
    chmod +x install.sh && \
    { USER=root HOME=/root DEBIAN_FRONTEND=noninteractive ./install.sh --silent-mode -d /usr/share/themes -n WhiteSur -c Dark -o normal -a normal || true; } && \
    test -f /usr/share/themes/WhiteSur-Dark/gtk-3.0/gtk.css && \
    ls -la /usr/share/themes/ | grep -i white && \
    cd / && \
    rm -rf /tmp/WhiteSur-gtk-theme && \
    apt remove -y sassc optipng inkscape libxml2-utils libglib2.0-dev-bin && \
    apt autoremove -y && \
    apt clean

# Set WhiteSur theme as default for XFCE (without changing wallpaper)
RUN mkdir -p /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml && \
    echo -e '<?xml version="1.0" encoding="UTF-8"?>\n<channel name="xfce4-desktop" version="1.0">\n  <property name="backdrop" type="empty">\n    <property name="screen0" type="empty">\n      <property name="monitor0" type="empty">\n        <property name="workspace0" type="empty">\n          <property name="color-style" type="int" value="0"/>\n          <property name="image-style" type="int" value="5"/>\n          <property name="last-image" type="string" value="/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg"/>\n        </property>\n      </property>\n    </property>\n  </property>\n</channel>' \
    > /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
    echo -e '<?xml version="1.0" encoding="UTF-8"?>\n<channel name="xfwm4" version="1.0">\n  <property name="general" type="empty">\n    <property name="theme" type="string" value="WhiteSur-Dark"/>\n  </property>\n</channel>' \
    > /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml && \
    echo -e '<?xml version="1.0" encoding="UTF-8"?>\n<channel name="xsettings" version="1.0">\n  <property name="Net" type="empty">\n    <property name="ThemeName" type="string" value="WhiteSur-Dark"/>\n    <property name="IconThemeName" type="string" value="Adwaita"/>\n  </property>\n</channel>' \
    > /home/$USER/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml && \
    chown -R $USER:$USER /home/$USER/.config

# Startup and Supervisor
COPY startup.sh /startup.sh
RUN chmod +x /startup.sh
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY os.svg /usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg

# Set clean default XFCE panel layout (no power manager plugin)
COPY xfce4-panel.xml /etc/xdg/xfce4/panel/default.xml

# Copy GTK CSS for tooltip positioning (ensures tooltips appear above panel)
RUN mkdir -p /home/$USER/.config/gtk-3.0
COPY gtk-tooltip.css /home/$USER/.config/gtk-3.0/gtk.css
RUN chown -R $USER:$USER /home/$USER/.config/gtk-3.0

# Expose ports for noVNC, AXGT API, and IPFS
EXPOSE 6080
EXPOSE 8889

# Expose IPFS swarm port
EXPOSE 4001/tcp
# Expose IPFS swarm port (UDP)
EXPOSE 4001/udp
# Expose IPFS API port
EXPOSE 5001/tcp
# Expose IPFS Gateway port
EXPOSE 8080/tcp
# Expose IPFS Web UI port
EXPOSE 9090/tcp

# Apply AxonOS noVNC Theme
COPY novnc-theme/axonos-theme.css /usr/share/novnc/app/styles/
COPY novnc-theme/vnc.html /usr/share/novnc/
COPY novnc-theme/ui.js /usr/share/novnc/app/
COPY novnc-theme/app/fonts/ /usr/share/novnc/app/fonts/
COPY novnc-theme/app/webrtc/axonos-webrtc.js /usr/share/novnc/app/webrtc/axonos-webrtc.js
COPY novnc-theme/app/files/axonos-files.js /usr/share/novnc/app/files/axonos-files.js
COPY novnc-theme/icons/* /usr/share/novnc/app/images/icons/
COPY novnc-theme/icon.png /usr/share/novnc/icon.png
COPY novnc-theme/images/linux.svg /usr/share/novnc/app/images/linux.svg
COPY novnc-theme/telemetry.html /usr/share/novnc/

# Install AXGT Gate
COPY axonos_gate/ /axonos_gate/
RUN /usr/bin/python3 -m pip install -r /axonos_gate/requirements.txt
# Later pip layers may upgrade to NumPy 2.x; re-pin so apt/system matplotlib + Spyder stay compatible.
RUN pip install --no-cache-dir 'numpy>=1.24.0,<2' matplotlib
RUN chmod +x /axonos_gate/*.py

# AXGT / gate configuration is provided via environment variables at runtime.

# Copy theme application script for manual testing
COPY apply_theme.sh /usr/local/bin/apply_theme.sh
RUN chmod +x /usr/local/bin/apply_theme.sh
COPY scripts/post_deploy_theme.sh /usr/local/bin/post_deploy_theme.sh
RUN chmod +x /usr/local/bin/post_deploy_theme.sh
COPY scripts/apply_theme_session.sh /usr/local/bin/apply_theme_session.sh
RUN chmod +x /usr/local/bin/apply_theme_session.sh
COPY scripts/reset_session.sh /usr/local/bin/reset_session.sh
RUN chmod +x /usr/local/bin/reset_session.sh
COPY scripts/fix-libglx-nvidia-symlink.sh /usr/local/bin/fix-libglx-nvidia-symlink.sh
RUN chmod +x /usr/local/bin/fix-libglx-nvidia-symlink.sh
RUN mkdir -p /home/aXonian/.config/autostart
COPY scripts/axonos-theme.desktop /home/aXonian/.config/autostart/axonos-theme.desktop
RUN chown -R aXonian:aXonian /home/aXonian/.config/autostart

# Install NVIDIA Xorg/OpenGL userspace driver (for GPU-backed Xorg :0)
# Keep this late in the Dockerfile to preserve cache for heavy build steps.
# NVIDIA_DRIVER_VERSION: major branch (e.g. 535) matching host `nvidia-smi`.
ARG NVIDIA_DRIVER_VERSION=580
# NVIDIA_DRIVER_PKG_VERSION: pin ALL of these Ubuntu restricted packages together (same madison version):
#   xserver-xorg-video-nvidia-*, libnvidia-gl-*, libnvidia-cfg1-*, libnvidia-common-*
# The CUDA base image often preinstalls newer libnvidia-cfg1 (e.g. 535.309) — without pinning cfg1/common,
# apt cannot downgrade to match host (e.g. 535.288). Use --allow-downgrades in the install line.
# If unset, apt picks latest 535.x in Ubuntu — which can be NEWER than the host kernel driver
# (e.g. lib 535.309 vs host 535.288). Then Xorg logs "NVIDIA GLX Module ..." vs
# "NVIDIA dlloader X Driver ..." mismatched and often SIGSEGVs at "Enabling 2D acceleration".
# Set via docker compose build arg / .env (see env.example).
ARG NVIDIA_DRIVER_PKG_VERSION=
# Only install the Xorg + GL userspace pieces needed for GPU-backed Xorg :0.
# Avoid nvidia-utils to prevent overlayfs hardlink backup failures.
# install-nvidia-xorg-userspace.sh resolves a 4/4-package version (see resolve-nvidia-driver-pkg-version.sh).
RUN NVIDIA_DRIVER_VERSION="${NVIDIA_DRIVER_VERSION}" NVIDIA_DRIVER_PKG_VERSION="${NVIDIA_DRIVER_PKG_VERSION}" \
    /usr/local/bin/install-nvidia-xorg-userspace.sh

# Fail fast if the NVIDIA GLX server module never landed (fix-libglx would be pointless).
# Jammy+ splits modules: xserver-xorg-video-nvidia-* ships nvidia_drv.so; libglxserver_nvidia is
# in libnvidia-gl-* — only checking the xserver package is a false negative. No shell $PKG / $$ here.
RUN ( dpkg -L "xserver-xorg-video-nvidia-${NVIDIA_DRIVER_VERSION}"; \
      dpkg -L "libnvidia-gl-${NVIDIA_DRIVER_VERSION}" ) 2>/dev/null | grep -q libglxserver_nvidia || \
    { echo "axonos: libglxserver_nvidia not in xserver-xorg-video-nvidia / libnvidia-gl (${NVIDIA_DRIVER_VERSION})"; \
      dpkg -l | grep -iE 'nvidia|xorg|mesa' || true; \
      echo "axonos: xserver-xorg-video-nvidia file list (tail):"; \
      dpkg -L "xserver-xorg-video-nvidia-${NVIDIA_DRIVER_VERSION}" 2>/dev/null | tail -20 || true; \
      echo "axonos: libnvidia-gl file list (tail):"; \
      dpkg -L "libnvidia-gl-${NVIDIA_DRIVER_VERSION}" 2>/dev/null | tail -20 || true; \
      exit 1; }

# Mesa's libglx.so can remain the default after early apt layers; Xorg then loads two GLX vendors
# ("Another vendor is already registered for screen 0") and SIGSEGVs. Script avoids Dockerfile RUN "$$VAR"
# (sh expands $$ to PID, e.g. 1GLX_EXT).
RUN /usr/local/bin/fix-libglx-nvidia-symlink.sh

ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute,display,video

# WebRTC NVENC capture (late layer — keeps rebuilds fast when only app code changes above).
RUN apt-get update && apt-get install -y --no-install-recommends libxtst6 ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Optional WebRTC native NvFBC capture helper.
# The Capture SDK archive is not committed; place NVIDIA_Capture_SDK_7_1_9.tgz
# under vendor/ before building to enable WEBRTC_CAPTURE_BACKEND=nvfbc.
COPY tools/nvfbc_nvenc_streamer.c /tmp/nvfbc_nvenc_streamer.c
COPY vendor/NVIDIA_Capture_SDK_7_1_9.tgz /tmp/NVIDIA_Capture_SDK_7_1_9.tgz
RUN mkdir -p /tmp/nvfbc-sdk && \
    tar -xzf /tmp/NVIDIA_Capture_SDK_7_1_9.tgz -C /tmp/nvfbc-sdk && \
    gcc -O2 -Wall \
        -I/tmp/nvfbc-sdk/Capture_Linux_v7.1.9/NvFBC/inc \
        /tmp/nvfbc_nvenc_streamer.c -lGL -lX11 -ldl \
        -o /usr/local/bin/nvfbc_nvenc_streamer && \
    rm -rf /tmp/nvfbc-sdk /tmp/NVIDIA_Capture_SDK_7_1_9.tgz /tmp/nvfbc_nvenc_streamer.c

# ---------------------------------------------------------------------------
# Environment templates (kept as late layers so the heavy scientific builds
# above retain their build cache). These back the landing-page template picker:
# each session auto-opens its hero app via apply_session_template.sh.
# ---------------------------------------------------------------------------

# PyTorch AI Lab: CUDA-enabled PyTorch stack. cu121 wheels run on the CUDA 12.2
# runtime base. JupyterLab is already installed above; add TensorBoard + Pandas.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
        torch torchvision torchaudio && \
    pip install --no-cache-dir tensorboard pandas

# Quantum ESPRESSO: DFT electronic-structure suite (provides pw.x), plus the
# XCrySDen visualizer and gnuplot referenced by the template card.
RUN apt-get update && apt-get install -y --no-install-recommends \
        quantum-espresso xcrysden gnuplot && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    printf '%s\n' \
        '[Desktop Entry]' \
        'Name=Quantum ESPRESSO' \
        'Comment=DFT electronic-structure suite (pw.x)' \
        'Exec=terminator -x bash -lc "echo Quantum ESPRESSO ready. Run pw.x to start.; exec bash"' \
        'Icon=applications-science' \
        'Type=Application' \
        'Terminal=false' \
        'Categories=Science;' \
        > /usr/share/applications/quantum-espresso.desktop

# Desktop launcher for JupyterLab (hero app for PyTorch AI Lab and BeakerX).
COPY scripts/open-jupyterlab.sh /usr/local/bin/open-jupyterlab.sh
RUN chmod +x /usr/local/bin/open-jupyterlab.sh && \
    printf '%s\n' \
        '[Desktop Entry]' \
        'Name=JupyterLab' \
        'Comment=Browser-based notebooks (PyTorch, BeakerX kernels)' \
        'Exec=/usr/local/bin/open-jupyterlab.sh' \
        'Icon=applications-development' \
        'Type=Application' \
        'Terminal=false' \
        'Categories=Development;Science;' \
        > /usr/share/applications/jupyterlab.desktop

# UGENE ships a CLI binary (ugenecl) alongside the GUI; expose it on PATH so the
# template card's `ugenecl` command works.
RUN if [ -x /opt/ugene-52.1/ugenecl ]; then ln -sf /opt/ugene-52.1/ugenecl /usr/local/bin/ugenecl; fi

# Per-session template launcher. Invoked from startup.sh after the desktop is up
# (not via XDG autostart — ~/.config/autostart is on the persistent home volume,
# which would shadow any baked-in entry).
COPY scripts/apply_session_template.sh /usr/local/bin/apply_session_template.sh
RUN chmod +x /usr/local/bin/apply_session_template.sh

# Desktop audio: headless PulseAudio null sink (no audio hardware in the
# container). Desktop apps render into axonos_out via /etc/pulse/client.conf;
# the WebRTC agent captures axonos_out.monitor (see [program:pulseaudio] in
# supervisord.conf and docs/WEBRTC.md "Audio").
RUN apt-get update && apt-get install -y --no-install-recommends \
        pulseaudio pulseaudio-utils libspeechd2 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
COPY pulse-default.pa /etc/pulse/axonos-default.pa
COPY pulse-client.conf /etc/pulse/client.conf

# Start services
CMD ["/startup.sh"]
