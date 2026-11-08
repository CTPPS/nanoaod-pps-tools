#!/bin/bash -l
#SBATCH --job-name=pps-run3
#SBATCH --account=plgccbmc15-cpu
#SBATCH --partition=plgrid
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=192
#SBATCH --mem=384000
#SBATCH --hint=nomultithread
#SBATCH --time=12:00:00
#SBATCH --exclusive
#SBATCH --output=bench-run3-%j.out
#SBATCH --error=bench-run3-%j.err

# One job of the Run 3 campaign: the 11-filter chain (10 branches) on the full original NanoAOD
# files in $SCRATCH/bench/data3, read cold. JOB picks which one.
#
#   J1  Block F: strong11 on every downloaded file (INPUT_GLOB), 1-192 threads, and r2/r3 at
#       80-192 threads (STRONG11_R2_THREADS, STRONG11_R3_THREADS; "" for none), where a run is
#       about a minute and one slow stretch of Lustre would decide a single-run point
#                                              -> results-helios-run3-chain11
#   J3  Block N: node11, RDataFrame and uproot-pool on core.txt, NODE_THREADS, REPEATS_NODE
#       (default 3) runs each                   -> results-helios-run3-node
#   J4  Block N: node11, python-pool, the same, into the same directory
#
# J1 gets INPUT_GLOB, space-separated wildcard patterns. RDataFrame gets the patterns as they
# are and opens every match; the job first checks that ROOT expands each to the same files as
# Python's glob, that every .root under $DATA_DIR/data matches a pattern, that no match is an
# incomplete download (sizes in $DATA_DIR/run3_sizes.txt, if there), and describes the matches
# itself (inventory.csv in RESULTS: entries for the records, and the compressed size of the
# chain's ten branches). The re-reconstructed 2024 files sit two levels below the version
# (<ver>/2530000/<uuid>.root), the PromptReco ones five (<ver>/000/369/844/00000/<uuid>.root),
# hence two patterns.
#
# J3 and J4 read the Run 3 subset from the lists already in $DATA_DIR: core.txt, local.csv
# (inventory_files.py over those files), sets.json and READY.
#
# The pools take POOL_CHUNK_EVENTS entries of a file per task (default 250000): whole Run 3
# files on 192 Python workers would not fit in the node's memory.
#
# Every job appends with RESUME=1: a resubmitted job runs only what is not yet recorded as ok.
# FRESH=1 moves an existing results directory aside first.
#
#   D=$SCRATCH/bench/data3/data
#   INPUT_GLOB="$D/*/*/NANOAOD/*/*/*.root $D/*/*/NANOAOD/*/*/*/*/*/*.root" \
#       RESULTS=$SCRATCH/bench/results-helios-run3-4tb-full-chain11 \
#       sbatch --partition=plgrid-now --time=12:00:00 --export=ALL,JOB=J1 test/slurm_run3.sbatch
#   sbatch --export=ALL,JOB=J3 test/slurm_run3.sbatch
#   sbatch --export=ALL,JOB=J4 test/slurm_run3.sbatch
#
# CMS data is not public: it stays on scratch; results hold timings and counts only.

set -euo pipefail

JOB="${JOB:?JOB must be J1, J3 or J4}"
export MACHINE="${MACHINE:-helios}"
SCRATCH_BENCH="${SCRATCH}/bench"
export MAMBA_ROOT_PREFIX="${SCRATCH_BENCH}/micromamba"
DATA_DIR="${DATA_DIR:-${SCRATCH_BENCH}/data3}"
STRONG_THREADS="${STRONG_THREADS:-1 4 8 12 16 24 32 48 64 80 96 112 128 144 160 176 192}"
NODE_THREADS="${NODE_THREADS:-192}"
REPEAT_THREADS="80 96 112 128 144 160 176 192"
export STRONG11_R2_THREADS="${STRONG11_R2_THREADS-$REPEAT_THREADS}"
export STRONG11_R3_THREADS="${STRONG11_R3_THREADS-$REPEAT_THREADS}"
export REPEATS_NODE="${REPEATS_NODE:-3}"
export POOL_CHUNK_EVENTS="${POOL_CHUNK_EVENTS:-250000}"
export SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.05}"

case "$JOB" in
    J1) name=chain11 ;;
    J3|J4) name=node ;;
    *) echo "ERROR: JOB must be J1, J3 or J4, not '$JOB'." >&2; exit 1 ;;
esac
RESULTS="${RESULTS:-${SCRATCH_BENCH}/results-helios-run3-${name}}"
INPUT_GLOB="${INPUT_GLOB:-}"

if [[ "$JOB" == J1 && -z "$INPUT_GLOB" ]]; then
    echo "ERROR: J1 needs INPUT_GLOB, the wildcard patterns of the whole Run 3 set." >&2
    exit 1
fi
if [[ -z "$INPUT_GLOB" && ! -f "$DATA_DIR/READY" ]]; then
    echo "ERROR: $DATA_DIR/READY missing: J3 and J4 read core.txt, local.csv and sets.json there." >&2
    exit 1
fi

# See slurm_benchmark.sbatch: the env's deactivate script trips over set -u.
set +u
eval "$("${SCRATCH_BENCH}/micromamba/bin/micromamba" shell hook -s bash)"
micromamba activate pps-bench
set -u
cd "${SCRATCH_BENCH}/nanoaod-pps-tools"
export PY="$(command -v python)"

echo "=== allocation ==="
echo "job                : $JOB -> $RESULTS"
echo "node               : $(hostname) (${MACHINE})"
echo "SLURM_CPUS_PER_TASK: ${SLURM_CPUS_PER_TASK:-unset}"
echo "nproc              : $(nproc)"
expected_cpus="${SLURM_CPUS_PER_TASK:-192}"
if [[ "$(nproc)" -lt "$expected_cpus" ]]; then
    echo "ERROR: nproc=$(nproc) but the job asked for $expected_cpus CPUs." >&2
    exit 1
fi
echo
[[ -z "$INPUT_GLOB" ]] && cat "$DATA_DIR/READY"
echo

if [[ -n "${FRESH:-}" && -f "$RESULTS/raw.jsonl" ]]; then
    mv "$RESULTS" "$RESULTS-archived-$(date +%Y%m%d_%H%M%S)"
fi
mkdir -p "$RESULTS"
if [[ -n "$INPUT_GLOB" ]]; then
    echo "=== input: $INPUT_GLOB [$(date +%H:%M:%S)] ==="
    "$PY" - "$INPUT_GLOB" "$DATA_DIR/run3_sizes.txt" "$DATA_DIR" <<'PYEOF' || exit 1
import glob, os, sys

import ROOT

patterns, sizes_path, data_dir = sys.argv[1].split(), sys.argv[2], sys.argv[3]
files = set()
for pattern in patterns:
    matches = glob.glob(pattern)
    added = ROOT.TChain("Events").Add(pattern)
    print(f"  {pattern}: {len(matches)} files (Python glob), ROOT adds {added}")
    if not matches or added != len(matches):
        sys.exit("ERROR: ROOT does not expand the pattern to the files Python's glob finds.")
    files.update(os.path.normpath(p) for p in matches)
matched, files = files, sorted(files)
# rsync writes into hidden .<name>.XXXXXX files, which are not part of the set.
on_disk = sorted(os.path.normpath(os.path.join(root, name))
                 for root, _, names in os.walk(os.path.join(data_dir, "data"))
                 for name in names if name.endswith(".root") and not name.startswith("."))
missed = [p for p in on_disk if p not in matched]
for p in missed[:5]:
    print(f"  not matched: {p}")
if missed:
    sys.exit(f"ERROR: {len(missed)} of {len(on_disk)} .root files under data/ match no pattern.")
print(f"  {len(files)} files: every .root under data/ matches a pattern")
if os.path.exists(sizes_path):
    prefix = "/eos/cms/store/"
    expected = {}
    with open(sizes_path) as f:
        for line in f:
            path, size = line.split()
            expected[os.path.normpath(os.path.join(data_dir, path[len(prefix):]))] = int(size)
    bad = [p for p in files if p in expected and os.path.getsize(p) != expected[p]]
    for p in bad[:5]:
        print(f"  incomplete: {p}")
    if bad:
        sys.exit(f"ERROR: {len(bad)} matches are incomplete downloads; move them out of the pattern.")
    print(f"  every match listed in {os.path.basename(sizes_path)} has its size")
PYEOF
    if [[ ! -s "$RESULTS/inventory.csv" ]]; then
        PYTHONPATH=test "$PY" test/inventory_files.py --input "$INPUT_GLOB" \
            --output "$RESULTS/inventory.csv.partial" --jobs 96 --threads 1 --no-loop \
            2>/dev/null | tail -n 30
        mv "$RESULTS/inventory.csv.partial" "$RESULTS/inventory.csv"
    fi
    "$PY" - "$RESULTS/inventory.csv" <<'PYEOF' || exit 1
import csv, sys

with open(sys.argv[1]) as f:
    rows = list(csv.DictReader(f))
bad = [r for r in rows if r.get("error") or r.get("missing_columns")]
gb = sum(int(r["size_bytes"] or 0) for r in rows) / 1e9
chain_gb = sum(int(r["chain_zip_bytes"] or 0) for r in rows) / 1e9
events = sum(int(r["entries"] or 0) for r in rows)
print(f"  files {len(rows)}  GB {gb:.1f}  events {events}")
print(f"  chain11 branches: {chain_gb:.1f} GB compressed ({100 * chain_gb / max(gb, 1e-9):.2f}% of the set)")
for r in bad[:5]:
    print(f"  unusable: {r['path']}: {r.get('error') or 'missing ' + r['missing_columns']}")
if bad:
    sys.exit(f"ERROR: {len(bad)} files cannot be read by the chain; move them out of the pattern.")
PYEOF
    if [[ ! -f "$RESULTS/dataset.json" ]]; then
        "$PY" test/dataset_json.py --out "$RESULTS" --input "$INPUT_GLOB" \
            --inventory "$RESULTS/inventory.csv"
    fi
    export DS_CORE="$INPUT_GLOB"
else
    cp "$DATA_DIR/READY" "$DATA_DIR/sets.json" "$RESULTS/"
    if [[ ! -f "$RESULTS/dataset.json" ]]; then
        "$PY" test/dataset_json.py --out "$RESULTS" --input "$DATA_DIR/core.txt" \
            --inventory "$DATA_DIR/local.csv"
    fi
fi

echo "=== correctness [$(date +%H:%M:%S)] ==="
"$PY" test/validate.py --threads 8

export DATASET=real DATA_DIR COLD=1 RESUME=1 RESULTS
# The Run 3 cuts (bench_spec.PERIODS["run3"]): the default period's diamond cuts drop almost
# every event of 2024F-2026D.
export CHAIN_ARGS="${CHAIN_ARGS:---period run3}"
# Event counts for the records (n_events, events_per_s), read after the timed phases, from the
# inventory instead of opening every file after every run. The pools count entries themselves.
export EVENT_COUNTS="$DATA_DIR/local.csv"
[[ -n "$INPUT_GLOB" ]] && export EVENT_COUNTS="$RESULTS/inventory.csv"

case "$JOB" in
J1)
    # One thread on ~4.5 TB is ~4 h, so 6 h per run.
    export RUN_TIMEOUT="${RUN_TIMEOUT:-21600}"
    TESTS=strong11 THREADS_LIST="$STRONG_THREADS" bash test/run_benchmark.sh
    ;;
J3)
    TESTS=node11 NODE_IMPLS="rdf-lazy uproot-pool" NODE_THREADS="$NODE_THREADS" \
        RUN_TIMEOUT="${RUN_TIMEOUT:-10800}" bash test/run_benchmark.sh
    ;;
J4)
    TESTS=node11 NODE_IMPLS=python-pool NODE_THREADS="$NODE_THREADS" \
        RUN_TIMEOUT="${RUN_TIMEOUT:-21600}" bash test/run_benchmark.sh
    ;;
esac

echo "done [$(date +%H:%M:%S)]. Copy $RESULTS to results/run3-strong/ (J1) or results/run3-node/ (J3, J4)."
sacct -j "${SLURM_JOB_ID}" --format=JobID,JobName,State,Elapsed,MaxRSS,NCPUS,ReqMem || true
