#!/bin/bash
#SBATCH --job-name=upgrade-vllm
#SBATCH --partition=hopper-prod
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=48
#SBATCH --time=01:00:00
#SBATCH --output=serve/logs/slurm/upgrade-vllm-%j.out
#SBATCH --error=serve/logs/slurm/upgrade-vllm-%j.err

set -euo pipefail

source "$HOME/.bashrc"
module use /admin/opt/modulefiles
module load glibc/2.38 cuda/12.9

echo "CUDA version: $(nvcc --version | grep release)"
echo "glibc: $(ldd --version | head -1)"

cd /fsx/joel_niklaus/projects/open-dirac

VENV_PYTHON="/fsx/joel_niklaus/projects/open-dirac/.venv/bin/python3.13"
echo "venv python: $VENV_PYTHON"

# Remove the broken editable install (its _C.abi3.so needs glibc 2.34)
uv pip uninstall vllm --python "$VENV_PYTHON" 2>/dev/null || true

# Install prebuilt vllm wheel using manylinux_2_34 platform tag
# (the glibc/2.38 module makes this compatible at runtime)
uv pip install -U vllm \
  --prerelease=allow \
  --python-platform x86_64-manylinux_2_34 \
  --torch-backend=cu129 \
  --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
  --python "$VENV_PYTHON"

# Patch the venv Python binary to use the loaded glibc 2.38
# (must not be run through the same Python process - use bash directly)
glibc-fix "$VENV_PYTHON"

echo "=== vllm version ==="
"$VENV_PYTHON" -c "import vllm; print(vllm.__version__)"
echo "=== DONE ==="
