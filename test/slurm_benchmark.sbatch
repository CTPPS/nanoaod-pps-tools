#!/bin/bash -l
#SBATCH --job-name=pps-bench
#SBATCH --account=plgccbmc15-cpu
#SBATCH --partition=plgrid
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=48
#SBATCH --mem=0
#SBATCH --hint=nomultithread
#SBATCH --time=08:00:00
#SBATCH --exclusive
#SBATCH --output=bench-ares-%j.out
#SBATCH --error=bench-ares-%j.err

# Block A (Ares, one core) and the file-width control (Helios) on copies of ds_x32.root
# (DATASET=big, run_benchmark.sh): validate.py, then the measurements, read cold from Lustre.
#
# --exclusive is not optional: a shared node makes every scaling number a measurement of the
# neighbours' load. --mem=0 (the whole node): the cgroup limit applies to page cache too.
# --hint=nomultithread so that N threads means N physical cores.
#
# Block A, two jobs into the same directory (never both at once: they would append to the same
# raw.jsonl and share Lustre bandwidth, which is what the cold reads measure):
#   sbatch --time=06:00:00 test/slurm_benchmark.sbatch                       # qstruct, size
#   sbatch --time=12:00:00 --dependency=afterany:<id> \
#       --export=ALL,TESTS=sizepy,RESUME=1 test/slurm_benchmark.sbatch       # Python series
#
# File-width control on Helios (192 cores; --mem=0 is rejected there, MaxMemPerNode=384000):
#   H="--cpus-per-task=192 --mem=384000 --output=bench-helios-%j.out --error=bench-helios-%j.err"
#   export MACHINE=helios TESTS=strong11 RUN_TIMEOUT=86400 \
#       THREADS_LIST="1 4 8 12 16 24 32 48 64 80 96 112 128 144 160 176 192" \
#       STRONG11_R2_THREADS="96 112 128 144 160 176 192" STRONG11_R3_THREADS="144 176 192" \
#       STRONG11_R4_THREADS="144 176"
#   # full copies: ds_1..ds_96.root in $SCRATCH/bench/data
#   sbatch $H -t 40:00:00 --export=ALL,RESULTS=$SCRATCH/bench/results-helios-full11 test/slurm_benchmark.sbatch
#   # slim copies (only the chain's 10 branches) in $SCRATCH/bench/slim11, count in copies.txt
#   sbatch $H -t 40:00:00 --export=ALL,DATA_DIR=$SCRATCH/bench/slim11,RESULTS=$SCRATCH/bench/results-helios-slim11 \
#       test/slurm_benchmark.sbatch
#
# RESUME=1 restarts a job that ran out of wall clock: only the missing runs go.

set -euo pipefail

export MACHINE="${MACHINE:-ares}"
export SCRATCH_BENCH="${SCRATCH}/bench"
# Without this micromamba looks for environments under $HOME instead of the one on scratch.
export MAMBA_ROOT_PREFIX="${SCRATCH_BENCH}/micromamba"

# `set -u` off across the activation only. The job inherits the submitting shell's environment,
# so activate first deactivates whatever was already loaded, and the env's own
# deactivate-root.sh reads CONDA_BACKUP_ROOTSYS unconditionally -- unset on a fresh job, which
# under -u kills the script before the first benchmark.
set +u
eval "$("${SCRATCH_BENCH}/micromamba/bin/micromamba" shell hook -s bash)"
micromamba activate pps-bench
set -u

cd "${SCRATCH_BENCH}/nanoaod-pps-tools"

echo "=== allocation ==="
echo "node               : $(hostname) (${MACHINE})"
lscpu | grep -E 'Model name|^CPU\(s\)|Thread\(s\) per core|Core\(s\) per socket|Socket|NUMA node\(s\)'
numactl -H 2>/dev/null || true
echo "SLURM_CPUS_ON_NODE : ${SLURM_CPUS_ON_NODE:-unset}"
echo "SLURM_CPUS_PER_TASK: ${SLURM_CPUS_PER_TASK:-unset}"
echo "nproc              : $(nproc)"
taskset -cp $$ || true

# A benchmark that quietly runs 48 ROOT threads on 4 cores still exits 0 and still produces a
# plot; it just produces a wrong one. Hard stop instead.
expected_cpus="${SLURM_CPUS_PER_TASK:-48}"
if [[ "$(nproc)" -lt "$expected_cpus" ]]; then
    echo "ERROR: nproc=$(nproc) but the job asked for $expected_cpus CPUs." >&2
    echo "Refusing to run: every scaling number would be a measurement of the allocation." >&2
    exit 1
fi
echo

export PY="$(command -v python)"
export DATASET=big
export THREADS_LIST="${THREADS_LIST:-1 2 4 8 12 16 24 32 48}"
# ds_1..ds_96.root next to ds_x32.root.
export DATA_DIR="${DATA_DIR:-${SCRATCH_BENCH}/data}"
export RESULTS="${RESULTS:-${SCRATCH_BENCH}/results-${MACHINE}-big}"
# The longest run is the Python chain on 9 copies, ~2.5 h plus its cold read.
export RUN_TIMEOUT="${RUN_TIMEOUT:-14400}"

# run_benchmark.sh appends to $RESULTS/raw.jsonl, so a directory left over from an earlier
# campaign would quietly mix its records in. Start a new directory and keep the old one --
# unless RESUME=1, whose whole point is to add to it.
if [[ -z "${RESUME:-}" && -f "${RESULTS}/raw.jsonl" ]]; then
    previous="${RESULTS}-archived-$(date +%Y%m%d_%H%M%S)"
    echo "moving previous results to ${previous}"
    mv "${RESULTS}" "${previous}"
fi
mkdir -p "${RESULTS}"

# dataset.json: the codec, branches and baskets per file of the set, read by
# plot_real_vs_synthetic.py.
if [[ ! -f "${RESULTS}/dataset.json" ]]; then
    "$PY" test/dataset_json.py --out "${RESULTS}" --input "${DATASET_JSON_INPUT:-${DATA_DIR}/ds_1.root}"
fi

# validate.py runs on it, and every benchmark compiles its kernel on it during warmup.
if head -c 40 examples/test.root | grep -q 'git-lfs'; then
    echo "ERROR: examples/test.root is a Git LFS pointer; run: git lfs install && git lfs pull" >&2
    exit 1
fi
echo "=== correctness [$(date +%H:%M:%S)] ==="
"$PY" test/validate.py --threads 8

# No srun. Since Slurm 22.05 a job step does not inherit --cpus-per-task from the batch
# allocation, so `srun` here could hand the whole campaign a single CPU -- which fails
# silently, as a flat speedup curve.
echo "=== measurements [$(date +%H:%M:%S)] ==="
bash test/run_benchmark.sh

# Independent confirmation of the numbers the script collected itself.
sacct -j "${SLURM_JOB_ID}" --format=JobID,JobName,State,Elapsed,MaxRSS,AveCPU,NCPUS,ReqMem || true
