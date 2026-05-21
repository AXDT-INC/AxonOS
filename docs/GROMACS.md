# GROMACS on AxonOS

This guide walks through the **lysozyme-in-water tutorial** on an AxonOS GPU desktop and documents how to **validate single- and multi-GPU usage**. It reflects the actual AxonOS image build (release-2026, CUDA, library MPI via OpenMPI).

Reference tutorial: [Lysozyme in Water](http://www.mdtutorials.com/gmx/lysozyme/)

---

## AxonOS build (what you get)

| Setting | Value |
|---------|--------|
| Binary | `gmx_mpi` (symlinked as `gmx` and `gmx_mpi`) |
| MPI | **Library MPI** (`GMX_MPI=ON`) with GPU-aware OpenMPI |
| OpenMP | Enabled (`-ntomp`) |
| GPU | CUDA + cuFFTMp (NVHPC) |
| OpenMPI BTL | `vader,self,tcp` (set in `Dockerfile`; avoids `sm` BTL failures in Docker) |

### Library MPI vs tutorials

Most online tutorials assume **thread-MPI** and use `mdrun -ntmpi N`. **That flag does not work on AxonOS** — you will see:

```text
Fatal error:
Setting the number of thread-MPI ranks is only supported with thread-MPI and
GROMACS was compiled without thread-MPI
```

| Tutorial command | On AxonOS |
|------------------|-----------|
| `gmx mdrun -ntmpi 1 -ntomp 4` | `gmx_mpi mdrun -ntomp 4 -gpu_id 0` |
| `gmx mdrun -ntmpi 8 ...` | `gmx_mpi mdrun -ntomp 2 -gpu_id 0,1,2,3,4,5,6,7` or `mpirun -np 8 gmx_mpi mdrun ...` |
| `mpirun -np 8 gmx mdrun` | `mpirun -np 8 gmx_mpi mdrun ...` |

**Do not use `-ntmpi`.** Use `-ntomp`, `-gpu_id`, and/or `mpirun -np N` instead.

---

## Prerequisites

### GPU session profile

Session containers receive only the GPUs allocated by your profile:

| Profile | GPUs |
|---------|------|
| `small` | 1 |
| `medium` | 2 |
| `large` | 4 |
| `max` | 8 |

Inside the desktop:

```bash
echo "Assigned GPUs: $AXGT_ASSIGNED_GPU_IDS"
nvidia-smi -L
```

Multi-GPU benchmarks require a session where `nvidia-smi -L` lists every GPU you intend to use.

### Shared memory (`/dev/shm`)

AxonOS session containers use a large `/dev/shm` (typically 32G). Verify if MPI or GLX misbehaves:

```bash
df -h /dev/shm
```

You should see tens of GB available, not ~64M.

### Shell environment

Every new terminal:

```bash
source /opt/gromacs/bin/GMXRC
```

OpenMPI MCA variables are set **image-wide** via `Dockerfile` `ENV` (`OMPI_MCA_btl=vader,self,tcp`). You should not need to export them manually on a current image. To confirm:

```bash
echo $OMPI_MCA_btl
# expected: vader,self,tcp
```

---

## Step 0 — Validate MPI and GPU-aware build

Run before the tutorial. Confirms OpenMPI starts without the legacy `sm` BTL error.

```bash
source /opt/gromacs/bin/GMXRC

gmx_mpi --version
# Look for: MPI library: MPI (GPU-aware: CUDA)

mpirun -np 2 gmx_mpi --version
# Should complete with no "sm BTL initialization" warning
```

Check executables:

```bash
which gmx_mpi mpirun
# /opt/gromacs/bin/gmx_mpi
# /opt/openmpi/bin/mpirun
```

---

## Step 1 — Tutorial setup (structure → solvation)

```bash
mkdir -p ~/gmx-tutorial && cd ~/gmx-tutorial
source /opt/gromacs/bin/GMXRC

# Example structure (RCSB)
curl -fsSL -o 1aki.pdb https://files.rcsb.org/download/1AKI.pdb

# Topology (interactive: pick force field + water model; tutorial uses CHARMM36)
gmx_mpi pdb2gmx -f 1aki.pdb -o processed.gro -water spce

# Box and solvate
gmx_mpi editconf -f processed.gro -o newbox.gro -c -d 1.0 -bt cubic
gmx_mpi solvate -cp newbox.gro -cs spc216.gro -o solv.gro -p topol.top
```

After `solvate`, `solv.gro` and `topol.top` are in the working directory.

---

## Step 2 — Download MDP parameter files

The ions / minimization / equilibration / production stages need `.mdp` files from the tutorial. **Download them before `grompp`:**

```bash
cd ~/gmx-tutorial

curl -fsSL -o ions.mdp \
  http://www.mdtutorials.com/gmx/lysozyme/Files/ions.mdp

curl -fsSL -o minim.mdp \
  http://www.mdtutorials.com/gmx/lysozyme/Files/minim.mdp

curl -fsSL -o nvt.mdp \
  http://www.mdtutorials.com/gmx/lysozyme/Files/nvt.mdp

curl -fsSL -o npt.mdp \
  http://www.mdtutorials.com/gmx/lysozyme/Files/npt.mdp

curl -fsSL -o md.mdp \
  http://www.mdtutorials.com/gmx/lysozyme/Files/md.mdp

ls -l *.mdp
```

These MDP files match the tutorial’s **CHARMM36** force-field choice from `pdb2gmx`.

---

## Step 3 — Ions, minimization, equilibration

### Add ions (`genion`, not `mdrun`)

```bash
gmx_mpi grompp -f ions.mdp -c solv.gro -p topol.top -o ions.tpr -maxwarn 1

# When prompted, choose the SOL group (often group 13)
gmx_mpi genion -s ions.tpr -o solv_ions.gro -p topol.top -pname NA -nname CL -neutral
```

### Energy minimization

```bash
gmx_mpi grompp -f minim.mdp -c solv_ions.gro -p topol.top -o em.tpr
gmx_mpi mdrun -v -deffnm em -ntomp 4 -gpu_id 0
```

Expect: `Steepest Descents converged to Fmax < 1000`.

### NVT equilibration

```bash
gmx_mpi grompp -f nvt.mdp -c em.gro -r em.gro -p topol.top -o nvt.tpr
gmx_mpi mdrun -deffnm nvt -ntomp 4 -gpu_id 0
```

### NPT equilibration

```bash
gmx_mpi grompp -f npt.mdp -c nvt.gro -r nvt.gro -t nvt.cpt -p topol.top -o npt.tpr
gmx_mpi mdrun -deffnm npt -ntomp 4 -gpu_id 0
```

If `grompp` complains about a missing `.cpt`, omit `-t nvt.cpt` on the first attempt for that stage.

### Production `.tpr`

```bash
gmx_mpi grompp -f md.mdp -c npt.gro -t npt.cpt -p topol.top -o md.tpr
```

---

## Step 4 — Validate single-GPU usage

Production MD on **one GPU**:

```bash
gmx_mpi mdrun -deffnm md -ntomp 8 -gpu_id 0 -pin on
```

Adjust `-ntomp` to your CPU thread count.

### What to look for in `mdrun` output

```text
1 GPU selected for this run.
Mapping of GPU IDs to the 2 GPU tasks in the 1 rank on this node:
  PP:0,PME:0
Using 1 MPI process
Using N OpenMP threads per MPI process
```

### Monitor with `nvidia-smi`

In a **second terminal** while `mdrun` runs:

```bash
watch -n 1 nvidia-smi
```

For a single-GPU run on GPU 0:

- **GPU-Util** should be well above idle on that device
- **Memory-Usage** typically ~2–3 GiB for lysozyme (system-dependent)
- Other assigned GPUs may show 0% if unused

The **Processes** table at the bottom of `nvidia-smi` is often empty inside Docker; GPU util and memory still confirm usage.

Optional:

```bash
nvidia-smi dmon -s pucvmet -d 1
```

### Performance from log

After a segment finishes (or after stopping with Ctrl+C once steady state is reached):

```bash
grep -E 'Performance|ns/day' md.log | tail -5
```

Save this as your **1-GPU baseline** for comparisons.

---

## Step 5 — Validate multi-GPU usage (benchmark)

Lysozyme is **too small** to scale efficiently across many GPUs; use this step to confirm **platform plumbing** (all GPUs visible, MPI ranks, GPU-direct comm), not peak scientific throughput.

Requires a session with multiple GPUs visible (`nvidia-smi -L`).

### 8-GPU run

```bash
# Optional: separate output prefix so you keep the 1-GPU md.log
gmx_mpi mdrun -deffnm md_8gpu -s md.tpr -ntomp 2 -gpu_id 0,1,2,3,4,5,6,7 -pin on
```

Alternative with explicit `mpirun`:

```bash
mpirun -np 8 gmx_mpi mdrun -deffnm md_8gpu -s md.tpr -ntomp 2 -gpu_id 0,1,2,3,4,5,6,7 -pin on
```

### Expected `mdrun` output (8 GPUs)

```text
8 GPUs selected for this run.
Mapping of GPU IDs to the 8 GPU tasks in the 8 ranks on this node:
  PP:0,PP:1,PP:2,PP:3,PP:4,PP:5,PP:6,PP:7
Using 8 MPI processes
GPU direct communication will be used between MPI ranks.
```

### Monitor all GPUs

```bash
watch -n 1 nvidia-smi
```

On 8× V100 with lysozyme you may see **~18–20% util** and **~2.5 GiB** on each GPU — that indicates all ranks are active, not that the hardware is underperforming. Communication overhead dominates on small systems.

### Compare 1-GPU vs 8-GPU

```bash
grep -E 'Performance|ns/day' md.log md_8gpu.log 2>/dev/null
```

For lysozyme, **1 GPU often reports higher ns/day** than 8 GPUs. That is expected.

---

## Step 6 — Troubleshooting

### `sm BTL initialization` (OpenMPI)

**Symptom:**

```text
A system call failed during sm BTL initialization...
```

**Fix:** Rebuild/pull an image that includes:

```dockerfile
ENV OMPI_MCA_btl=vader,self,tcp
ENV OMPI_MCA_btl_base_warn_component_unused=0
```

Temporary workaround in a shell (pre-image):

```bash
export OMPI_MCA_btl=vader,self,tcp
export OMPI_MCA_btl_base_warn_component_unused=0
```

### `-ntmpi` fatal error

See [Library MPI vs tutorials](#library-mpi-vs-tutorials). Remove `-ntmpi` from all commands.

### `File 'ions.mdp' does not exist`

Run the [curl commands](#step-2--download-mdp-parameter-files) in `~/gmx-tutorial` before `grompp`.

### Container / desktop disappears

The `sm BTL` warning alone rarely stops the container. Common causes:

- **OOM** from `mpirun -np $(nproc)` or oversized runs — check host `dmesg | grep -i oom`
- **Session ended** (credits, timeout) — AxonOS stops `axgt-session-*` containers with `--rm`
- **GPU / Xorg failure** — desktop dies but container may still be `Up`; check `docker ps` on the host

### Low GPU utilization on multi-GPU lysozyme run

Normal for this system size. For meaningful scaling benchmarks, use a **much larger** simulation (more atoms, longer `-nsteps`).

---

## Quick reference

```bash
# Environment
source /opt/gromacs/bin/GMXRC

# MPI smoke test
mpirun -np 2 gmx_mpi --version

# Single GPU MD
gmx_mpi mdrun -deffnm md -ntomp 8 -gpu_id 0

# Multi-GPU MD (N = number of GPUs)
gmx_mpi mdrun -deffnm md -ntomp 2 -gpu_id 0,1,2,3,4,5,6,7

# Live GPU monitor
watch -n 1 nvidia-smi
```

---

## Related AxonOS docs

- [HOST_LAUNCHER.md](./HOST_LAUNCHER.md) — session containers, GPU assignment, `--shm-size`
- [TOKENOMICS.md](./TOKENOMICS.md) — billing scales with GPU count × wall time when using multi-GPU profiles
