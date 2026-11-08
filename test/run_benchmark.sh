#!/usr/bin/env bash
#
# Runs the benchmark measurements behind the thesis and collects timing, memory, CPU and I/O
# metrics into $RESULTS/raw.jsonl, then runs plot_results.py. Two datasets:
#
#   DATASET=big (default)  ds_1..ds_96.root, plain copies of ds_x32.root in DATA_DIR, read cold
#                          (COLD=1), one repeat each: Block A (qstruct, size, sizepy) and the
#                          file-width control (strong11, on the full copies or on the slim set)
#   DATASET=real           the Run 3 NanoAOD files: core.txt or wildcard patterns in DS_CORE
#                          (slurm_run3.sbatch): Block F (strong11) and Block N (node11)
#
# TESTS selects the experiments, in the order below; names are the ones in the labels:
#   strong11  thread sweep of the 11-filter chain (bench_chain --chain long) on the core input
#   node11    the same chain on the whole node: RDataFrame on NODE_THREADS threads, uproot and
#             Python in a pool of NODE_THREADS processes (bench_pool.py)
#   qstruct   lazy vs eager vs Report() x chain length, 1 thread (DATASET=big, ~100 GB impl input)
#   size      RDataFrame and uproot pinned to one core on 1..9 copies (DATASET=big)
#   sizepy    Python on the same points, smallest first (DATASET=big)
#
# Each measurement runs in its own process, so the cling JIT cost cannot be amortised across
# repeats; the benchmarks move it into their own "setup"/"jit" phases instead, and only "loop"
# is comparable between implementations.
#
# Usage:
#   MACHINE=ares DATASET=big DATA_DIR=$SCRATCH/bench/data ./test/run_benchmark.sh
#   DATASET=real TESTS=node11 DATA_DIR=$SCRATCH/bench/data3 ./test/run_benchmark.sh
#   DRY_RUN=1 ./test/run_benchmark.sh    # list the runs without executing them
#
# RESUME=1 appends to an existing $RESULTS/raw.jsonl and skips every label already recorded
# there as ok: a job that ran out of wall clock, or a second job adding TESTS=sizepy to the
# first one's results, runs only what is missing. Failed runs are retried; warm-ups always run.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$TEST_DIR")"

# Prefer the repo venv over whatever python3 is on PATH: PyROOT usually lives in exactly one
# interpreter, and picking the wrong one fails late and confusingly.
if [[ -z "${PY:-}" && -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PY="$REPO_ROOT/.venv/bin/python"
fi
PY="${PY:-python3}"

RESULTS="${RESULTS:-$TEST_DIR/results}"
DATA_DIR="${DATA_DIR:-$TEST_DIR/data}"
MACHINE="${MACHINE:-local}"
THREADS_LIST="${THREADS_LIST:-1 2 4 8 12 16 24 32 48}"
CHAIN_LENS="${CHAIN_LENS:-1 3 5}"

DATASET="${DATASET:-big}"
case "$DATASET" in
    big)
        # One pass over ~1 TB rather than a campaign that can afford repeats.
        REPEATS_QSTRUCT="${REPEATS_QSTRUCT:-1}"
        COLD="${COLD:-1}"
        #   qstruct  on impl.txt, ~100 GB: rdf-eager reads its input up to six times, which on the
        #            whole set would be 6 TB;
        #   size     the implementations as a series of sizes: the Python and uproot paths
        #            materialise whole columns, so on 1 TB they would die on memory;
        #   sizepy   the Python half of that series, ~9 h and the only runs that can run out of
        #            memory, so it is a second job (TESTS=sizepy RESUME=1) rather than part of this.
        TESTS="${TESTS:-qstruct size}"
        ;;
    real)
        TESTS="${TESTS:-strong11}"
        ;;
    *)
        echo "ERROR: DATASET must be 'big' or 'real', not '$DATASET'" >&2
        exit 1
        ;;
esac
# Copies per point of the size and sizepy series.
SIZE_SERIES="${SIZE_SERIES:-1 2 4 6 8 9}"
RESUME="${RESUME:-}"
# The discarded warm-up runs absorb the first process's cold ROOT libraries. Without them that
# cost lands on the first measured run.
WARMUP_RUNS="${WARMUP_RUNS:-1}"
REPEATS_QSTRUCT="${REPEATS_QSTRUCT:-2}"
# The single-core series compares implementations on one core. uproot starts its own threads
# however its executors are set (442-444% CPU on Ares with TrivialExecutor), so the core is
# enforced from outside.
PIN_CORE="${PIN_CORE:-2}"
# A timeout does not slow anything down -- it only decides whether a slow run is recorded or
# thrown away.
RUN_TIMEOUT="${RUN_TIMEOUT:-2400}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.1}"
# COLD=1 drops the inputs' pages from the page cache before every run, so each run reads them
# from the file system instead of from RAM. Only meaningful on a set of distinct files: the
# 1 TB input is copies rather than one file named many times, which would be read from the
# page cache after the first time.
COLD="${COLD:-}"
# Extra bench_chain.py (and bench_pool.py) arguments for strong11, node11 and the chain11
# warm-up, e.g. "--period run3".
CHAIN_ARGS="${CHAIN_ARGS:-}"
read -r -a chain_args <<< "$CHAIN_ARGS"

# has_test <name>
has_test() {
    [[ " $TESTS " == *" $1 "* ]]
}
for requested in $TESTS; do
    case "$requested" in
        strong11|node11) ;;
        qstruct|size|sizepy)
            if [[ "$DATASET" != big ]]; then
                echo "ERROR: TESTS=$requested needs DATASET=big (the ds_N.root copies)." >&2
                exit 1
            fi
            ;;
        *) echo "ERROR: unknown test '$requested' in TESTS." >&2; exit 1 ;;
    esac
done

# The slim set holds only the 11-filter chain's branches, so nothing else can run on it, and a
# Run 3 campaign of the chain alone skips the other warm-ups for the same reason.
ONLY_CHAIN11=1
for requested in $TESTS; do
    [[ "$requested" == strong11 || "$requested" == node11 ]] || ONLY_CHAIN11=""
done
# node11: what runs, and with how many threads (RDataFrame) or processes (the pools).
NODE_IMPLS="${NODE_IMPLS:-rdf-lazy uproot-pool python-pool}"
NODE_THREADS="${NODE_THREADS:-192}"
REPEATS_NODE="${REPEATS_NODE:-1}"
# Entries per task of the pools (bench_pool.py --chunk-events); 0 gives one task per file.
POOL_CHUNK_EVENTS="${POOL_CHUNK_EVENTS:-250000}"
for impl in $NODE_IMPLS; do
    case "$impl" in
        rdf-lazy|uproot-pool|python-pool) ;;
        *) echo "ERROR: NODE_IMPLS: unknown implementation '$impl'." >&2; exit 1 ;;
    esac
done
# The strong11 sweep: no t0 and no t2, which on 1 TB costs half a t1. STRONG11_R2_THREADS are
# measured a second time, after the whole first sweep, so the repeat sees the node at a
# different moment. STRONG11_R3_THREADS give a third run where the first two disagree, so the
# median no longer averages an outlier. STRONG11_R4_THREADS give a fourth where two of three
# runs were slowed down.
STRONG11_THREADS="${STRONG11_THREADS:-$(for t in $THREADS_LIST; do
    [[ "$t" == 0 || "$t" == 2 ]] || printf '%s ' "$t"; done)}"
STRONG11_R2_THREADS="${STRONG11_R2_THREADS:-}"
STRONG11_R3_THREADS="${STRONG11_R3_THREADS:-}"
STRONG11_R4_THREADS="${STRONG11_R4_THREADS:-}"

# Inputs are a .root file, a .txt list of them or space-separated wildcard patterns; every
# benchmark accepts all three. Each variable can still be overridden on its own.
if [[ "$DATASET" == real ]]; then
    DS_CORE="${DS_CORE:-$DATA_DIR/core.txt}"
else
    # ds_1.root .. ds_$BIG_COPIES.root in DATA_DIR. The lists over them are written below into
    # lists/. The slim set's build job wrote copies.txt: its count came from a measurement.
    BIG_COPIES="${BIG_COPIES:-$(cat "$DATA_DIR/copies.txt" 2>/dev/null || echo 96)}"
    IMPL_COPIES="${IMPL_COPIES:-9}"
    DS_CORE="${DS_CORE:-$DATA_DIR/lists/core.txt}"
    DS_IMPL="${DS_IMPL:-$DATA_DIR/lists/impl.txt}"
    SIZE_PATTERN="${SIZE_PATTERN:-lists/size_%s.txt}"
    for n in $IMPL_COPIES $SIZE_SERIES; do
        if [[ "$n" -gt "$BIG_COPIES" ]]; then
            echo "ERROR: $n copies asked for, BIG_COPIES is $BIG_COPIES." >&2
            exit 1
        fi
    done
fi

size_input() {
    printf "%s/${SIZE_PATTERN:-}" "$DATA_DIR" "$1"
}

# write_big_list <name> <copies>: lists/<name> over the first <copies> of ds_N.root. Entries are
# relative to the list (../ds_N.root), the rule bench_common.input_files applies, so the set
# stays valid wherever DATA_DIR is mounted.
write_big_list() {
    local path="$DATA_DIR/lists/$1" count="$2" i
    for ((i = 1; i <= count; i++)); do
        printf '../ds_%d.root\n' "$i"
    done >"${path}.partial"
    mv "${path}.partial" "$path"
}

if [[ "$DATASET" == big && -z "${DRY_RUN:-}" ]]; then
    mkdir -p "$DATA_DIR/lists"
    write_big_list core.txt "$BIG_COPIES"
    write_big_list impl.txt "$IMPL_COPIES"
    for n in $SIZE_SERIES; do
        write_big_list "size_${n}.txt" "$n"
    done
    echo "lists: $DATA_DIR/lists (core $BIG_COPIES, impl $IMPL_COPIES, size N copies)"
fi

# The .root files behind an input: the file itself, the files space-separated wildcard patterns
# match, or a list's entries with relative paths resolved against the list's directory (the rule
# bench_common.input_files applies).
list_files() {
    local path="$1" base line
    if [[ "$path" == *[*?[]* ]]; then
        "$PY" -c 'import glob, sys
print("\n".join(sorted({f for p in sys.argv[1].split() for f in glob.glob(p)})))' "$path"
        return
    fi
    if [[ "$path" != *.txt ]]; then
        echo "$path"
        return
    fi
    base="$(cd "$(dirname "$path")" && pwd)"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" == /* ]]; then echo "$line"; else echo "$base/$line"; fi
    done <"$path"
}

# Only ImplicitMT is allowed to introduce parallelism. Without this the single-threaded Python
# paths were measured at 107-236% CPU, because numpy's BLAS does not ask permission.
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export MACHINE SAMPLE_INTERVAL

# drop_pages <file>...
#
# POSIX_FADV_DONTNEED evicts a file's clean pages from the page cache. It needs no privileges
# (unlike /proc/sys/vm/drop_caches, which is root-only and would also throw away ROOT's
# libraries), and it is per-file, so it touches nothing but the inputs.
drop_pages() {
    [[ $# -eq 0 ]] && return 0
    "$PY" - "$@" <<'PYEOF'
import os
import sys

for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
PYEOF
}

# posix_fadvise is Linux-only (it is absent on macOS), so a cold campaign asked for on a laptop
# would otherwise fail on its first run. Downgrade to warm reads instead and say so: the one
# thing that must not happen is a campaign that labels warm reads "cold".
if [[ -n "$COLD" ]] \
    && ! "$PY" -c 'import os, sys; sys.exit(0 if hasattr(os, "posix_fadvise") else 1)'; then
    echo "WARNING: no posix_fadvise on this platform; running with warm reads (COLD ignored)." >&2
    COLD=""
fi
# Every record says which of the two it is, so a cold campaign can never be averaged into a
# warm one by mistake.
CACHE="${COLD:+cold}"
export CACHE="${CACHE:-warm}"

if [[ -z "${DRY_RUN:-}" ]] && ! "$PY" -c "import ROOT" 2>/dev/null; then
    echo "ERROR: '$PY' cannot import ROOT. Set PY=/path/to/python with PyROOT." >&2
    exit 1
fi

# Every experiment needs its files, and a missing point would otherwise just vanish from the
# curve.
INPUTS=("$DS_CORE")
has_test qstruct && INPUTS+=("$DS_IMPL")
if has_test size || has_test sizepy; then
    for n in $SIZE_SERIES; do
        INPUTS+=("$(size_input "$n")")
    done
fi
missing=0
for input in "${INPUTS[@]}"; do
    if [[ "$input" == *[*?[]* ]]; then
        if [[ -z "$(list_files "$input" | head -1)" ]]; then
            echo "ERROR: no file matches $input" >&2
            missing=1
        fi
        continue
    fi
    if [[ ! -f "$input" ]]; then
        echo "ERROR: missing $input" >&2
        missing=1
        continue
    fi
    while IFS= read -r file; do
        if [[ ! -f "$file" ]]; then
            echo "ERROR: missing $file (listed in $input)" >&2
            missing=1
        fi
    done < <(list_files "$input")
done
if [[ "$missing" -ne 0 && "$DATASET" == big ]]; then
    echo "       The input is $BIG_COPIES plain copies of ds_x32.root, ds_1.root .. ds_$BIG_COPIES.root," \
        "in $DATA_DIR (cp, not hadd: hadd changes the cluster boundaries)." >&2
fi
[[ "$missing" -eq 0 || -n "${DRY_RUN:-}" ]] || exit 1

mkdir -p "$RESULTS"
RAW="$RESULTS/raw.jsonl"

# Labels already measured, one per line, for RESUME. A file rather than an associative array
# because macOS still ships bash 3.2.
DONE_LABELS="$(mktemp)"
trap 'rm -f "$DONE_LABELS"' EXIT
if [[ -n "$RESUME" && -f "$RAW" ]]; then
    "$PY" - "$RAW" >"$DONE_LABELS" <<'PYEOF'
import json
import sys

with open(sys.argv[1]) as f:
    for line in f:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("status") == "ok" and record.get("label"):
            print(record["label"])
PYEOF
    echo "RESUME: $(wc -l <"$DONE_LABELS" | tr -d ' ') runs already in $RAW are skipped"
fi

TIME_BIN=""
for candidate in /usr/bin/time /usr/local/bin/gtime "$(command -v gtime 2>/dev/null)"; do
    if [[ -x "$candidate" ]] && "$candidate" -v true >/dev/null 2>&1; then
        TIME_BIN="$candidate"
        break
    fi
done
if [[ -z "$TIME_BIN" ]]; then
    echo "WARNING: GNU time -v not found; peak RSS and CPU% come from getrusage only." >&2
fi

TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

TASKSET_BIN="$(command -v taskset || true)"
if [[ -n "$TASKSET_BIN" ]] && ! "$TASKSET_BIN" -c "$PIN_CORE" true 2>/dev/null; then
    echo "WARNING: cannot pin to core $PIN_CORE; the single-core series runs unpinned." >&2
    TASKSET_BIN=""
elif [[ -z "$TASKSET_BIN" && -z "${DRY_RUN:-}" ]]; then
    echo "WARNING: taskset not found; the single-core series runs unpinned and uproot may use several cores." >&2
fi

# run_one <label> <script> [args...]
#
# The RSS trace is written by the benchmark process itself, into $RSS_TRACE: the command is
# wrapped in `timeout` and `/usr/bin/time`, so a pid seen from here would be the wrapper's.
# With PINNED=1 the benchmark process (and every thread it starts) is confined to PIN_CORE.
# Returns 1 when the run failed or timed out, 0 otherwise (including a run RESUME skipped).
run_one() {
    local label="$1" script="$2"
    shift 2
    local pinned=""
    [[ -n "${PINNED:-}" && -n "$TASKSET_BIN" ]] && pinned="$PIN_CORE"

    if [[ "$label" != warmup_* ]] && grep -Fxq "$label" "$DONE_LABELS"; then
        echo "  == $label (already recorded, skipped)"
        return 0
    fi

    if [[ -n "${DRY_RUN:-}" ]]; then
        DRY_RUN_COUNT=$((${DRY_RUN_COUNT:-0} + 1))
        printf '  [%3d] %-44s %s %s%s\n' "$DRY_RUN_COUNT" "$label" "$script" "$*" \
            "${pinned:+  [core $pinned]}"
        return
    fi

    # Whatever this run is about to read, minus the page cache. The input is taken from the
    # run's own --input so that a run on one set does not evict another.
    if [[ -n "$COLD" ]]; then
        local -a inputs=()
        local i
        for ((i = 1; i <= $#; i++)); do
            if [[ "${!i}" == "--input" ]]; then
                local next=$((i + 1))
                while IFS= read -r file; do inputs+=("$file"); done \
                    < <(list_files "${!next}")
            fi
        done
        [[ "${#inputs[@]}" -gt 0 ]] && drop_pages "${inputs[@]}"
    fi

    local time_file stdout_file
    time_file="$(mktemp)"
    stdout_file="$(mktemp)"
    export RSS_TRACE="$RESULTS/rss_${label}.csv"

    local -a cmd=("$PY" "$TEST_DIR/$script" "$@")
    [[ -n "$pinned" ]] && cmd=("$TASKSET_BIN" -c "$pinned" "${cmd[@]}")
    [[ -n "$TIME_BIN" ]] && cmd=("$TIME_BIN" -v -o "$time_file" "${cmd[@]}")
    [[ -n "$TIMEOUT_BIN" ]] && cmd=("$TIMEOUT_BIN" "$RUN_TIMEOUT" "${cmd[@]}")

    echo "  -> $label"
    "${cmd[@]}" >"$stdout_file" 2>/dev/null
    local exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        echo "     FAILED (exit $exit_code) -- recorded as a limit, not dropped" >&2
        printf '{"label":"%s","status":"failed","exit_code":%d,"machine":"%s","timeout_s":%s}\n' \
            "$label" "$exit_code" "$MACHINE" "$RUN_TIMEOUT" >>"$RAW"
        rm -f "$time_file" "$stdout_file"
        return 1
    fi

    "$PY" - "$stdout_file" "$time_file" "$label" "$pinned" >>"$RAW" <<'PYEOF'
import json, re, sys

stdout_path, time_path, label, pinned = sys.argv[1:5]
record = {}
with open(stdout_path) as f:
    for line in f:
        if line.startswith("BENCH "):
            record = json.loads(line[6:])
record["label"] = label
record["status"] = "ok"
record["pinned_core"] = int(pinned) if pinned else None

patterns = {
    "time_maxrss_kb": (r"Maximum resident set size \(kbytes\):\s*(\d+)", int),
    "cpu_percent": (r"Percent of CPU this job got:\s*(\d+)%", int),
    "elapsed_s": (r"Elapsed \(wall clock\) time[^)]*\):\s*(?:(\d+):)?(\d+):([\d.]+)", None),
    "ctx_voluntary": (r"Voluntary context switches:\s*(\d+)", int),
    "ctx_involuntary": (r"Involuntary context switches:\s*(\d+)", int),
}
try:
    text = open(time_path).read()
except OSError:
    text = ""
for key, (pattern, cast) in patterns.items():
    match = re.search(pattern, text)
    if not match:
        continue
    if key == "elapsed_s":
        hours, minutes, seconds = match.groups()
        record[key] = int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    else:
        record[key] = cast(match.group(1))
print(json.dumps(record))
PYEOF
    rm -f "$time_file" "$stdout_file"
}

if [[ -z "${DRY_RUN:-}" ]]; then
    # With COLD the next thing run_one does is throw these pages away again, and on the 1 TB
    # set reading everything first would cost ~1 h for nothing. Instead the campaign checks
    # that eviction works at all, because if it silently did not, every "cold" number would be
    # a warm one. fincore is util-linux >= 2.31 and is not everywhere, hence the fallback.
    if [[ -n "$COLD" ]]; then
        echo "=== cold reads: checking that the page cache can be dropped ==="
        probe="$(list_files "$DS_CORE" | head -1)"
        head -c 65536 "$probe" >/dev/null
        drop_pages "$probe"
        if command -v fincore >/dev/null; then
            resident="$(fincore --bytes --output PAGES --noheadings "$probe" | tr -d ' ')"
            echo "  $(basename "$probe"): ${resident} pages resident after eviction"
            if [[ "${resident:-0}" -ne 0 ]]; then
                echo "ERROR: pages stayed in the cache, so the reads would not be cold." >&2
                echo "       Run with COLD= to measure warm reads instead." >&2
                exit 1
            fi
        else
            echo "  WARNING: fincore not found; eviction not verified." >&2
        fi
    else
        echo "=== warming the page cache ==="
        for input in "${INPUTS[@]}"; do
            list_files "$input"
        done | sort -u | while IFS= read -r file; do
            cat "$file" >/dev/null
        done
    fi
fi

# The page cache is not the only cold thing: the first process of the job also loads ROOT's
# libraries and PCH off Lustre. These runs absorb that and are never plotted (the plots select
# on the r<N>_ prefix).
#
# Under COLD they run on a single file: what they are for is the libraries and the PCH, and
# reading 1 TB three times to get them would cost hours.
WARMUP_INPUT_SET="$DS_CORE"
if [[ -n "$COLD" && -z "${DRY_RUN:-}" ]]; then
    WARMUP_INPUT_SET="$(list_files "$DS_CORE" | head -1)"
fi
if [[ "$WARMUP_RUNS" -eq 1 ]]; then
    echo "=== discarded warm-up runs ==="
    if [[ -z "$ONLY_CHAIN11" ]]; then
        run_one "warmup_filter_rdf" bench_filter.py --input "$WARMUP_INPUT_SET" --impl rdf
        run_one "warmup_chain_rdf-lazy" bench_chain.py --input "$WARMUP_INPUT_SET" --impl rdf-lazy
        run_one "warmup_eff_jit" bench_efficiency.py --input "$WARMUP_INPUT_SET" --impl jit
    fi
    if has_test strong11 || has_test node11; then
        run_one "warmup_chain11_rdf-lazy" bench_chain.py --input "$WARMUP_INPUT_SET" \
            --impl rdf-lazy --chain long ${chain_args[@]+"${chain_args[@]}"}
    fi
fi

# Strong scaling of the 11-filter chain: Run 3 (Block F), and the full and slim copies of the
# file-width control.
if has_test strong11; then
echo "=== strong scaling, 11-filter chain ${CHAIN_ARGS:+($CHAIN_ARGS) }on $(basename "$DS_CORE"): $STRONG11_THREADS ==="
for threads in $STRONG11_THREADS; do
    run_one "r1_strong_chain11_rdf-lazy_t${threads}" bench_chain.py \
        --input "$DS_CORE" --impl rdf-lazy --chain long --threads "$threads" \
        ${chain_args[@]+"${chain_args[@]}"}
done
for threads in $STRONG11_R2_THREADS; do
    run_one "r2_strong_chain11_rdf-lazy_t${threads}" bench_chain.py \
        --input "$DS_CORE" --impl rdf-lazy --chain long --threads "$threads" \
        ${chain_args[@]+"${chain_args[@]}"}
done
for threads in $STRONG11_R3_THREADS; do
    run_one "r3_strong_chain11_rdf-lazy_t${threads}" bench_chain.py \
        --input "$DS_CORE" --impl rdf-lazy --chain long --threads "$threads" \
        ${chain_args[@]+"${chain_args[@]}"}
done
for threads in $STRONG11_R4_THREADS; do
    run_one "r4_strong_chain11_rdf-lazy_t${threads}" bench_chain.py \
        --input "$DS_CORE" --impl rdf-lazy --chain long --threads "$threads" \
        ${chain_args[@]+"${chain_args[@]}"}
done
fi

# Block N: the whole core input, the whole node, each implementation the way it uses a node:
# ImplicitMT threads for RDataFrame, a pool of single-threaded processes over the files for
# uproot and Python. Repeats are the outer loop, so a slow stretch of Lustre lands on every
# implementation once. A run that hit RUN_TIMEOUT (exit 124, in this job or one before it) is
# not repeated: the next one would take as long and end the same way.
node_timed_out() {
    grep -sqE "\"label\":\"r[0-9]+_node_chain11_$1_t$2\",\"status\":\"failed\",\"exit_code\":124," \
        "$RAW"
}
if has_test node11; then
echo "=== whole node, 11-filter chain, on $(basename "$DS_CORE"): $NODE_IMPLS x $NODE_THREADS, $REPEATS_NODE repeat(s), pool chunks of $POOL_CHUNK_EVENTS ==="
for repeat in $(seq 1 "$REPEATS_NODE"); do
    for impl in $NODE_IMPLS; do
        for threads in $NODE_THREADS; do
            label="r${repeat}_node_chain11_${impl}_t${threads}"
            if ! grep -Fxq "$label" "$DONE_LABELS" && node_timed_out "$impl" "$threads"; then
                echo "  == $label (an earlier run of $impl timed out, skipped)"
                continue
            fi
            if [[ "$impl" == rdf-lazy ]]; then
                run_one "$label" bench_chain.py \
                    --input "$DS_CORE" --impl rdf-lazy --chain long --threads "$threads" \
                    ${chain_args[@]+"${chain_args[@]}"}
            else
                run_one "$label" bench_pool.py \
                    --input "$DS_CORE" --impl "$impl" --chain long --threads "$threads" \
                    --chunk-events "$POOL_CHUNK_EVENTS" ${chain_args[@]+"${chain_args[@]}"}
            fi
        done
    done
done
fi

# Query structure: single-threaded, because the number of event loops is a property of how the
# query was written rather than of the machine.
if has_test qstruct; then
    echo "=== query structure: chain lengths $CHAIN_LENS ==="
    for repeat in $(seq 1 "$REPEATS_QSTRUCT"); do
        for len in $CHAIN_LENS; do
            for impl in rdf-lazy rdf-eager rdf-report; do
                run_one "r${repeat}_qstruct_chain_${impl}_l${len}" bench_chain.py \
                --input "$DS_IMPL" --impl "$impl" --chain-len "$len"
        done
    done
done
fi

# The implementations against input size, pinned to one core, on 1..9 copies. What one input
# cannot show is how memory grows with the data: RDataFrame streams cluster by cluster, while
# uproot and AsNumpy hold whole columns, and RDataFrame's fixed JIT cost is amortised only past
# some size. The last point is ~100 GB.
if has_test size; then
echo "=== against input size, RDataFrame and uproot: $SIZE_SERIES copies${TASKSET_BIN:+, pinned to core $PIN_CORE} ==="
export PINNED=1
for n in $SIZE_SERIES; do
    size_ds="$(size_input "$n")"
    run_one "r1_size_filter_rdf_c${n}" bench_filter.py --input "$size_ds" --impl rdf
    run_one "r1_size_filter_uproot_c${n}" bench_filter.py --input "$size_ds" --impl uproot
    run_one "r1_size_chain_rdf-lazy_c${n}" bench_chain.py --input "$size_ds" --impl rdf-lazy
    run_one "r1_size_chain_uproot_c${n}" bench_chain.py --input "$size_ds" --impl uproot
    run_one "r1_size_eff_jit_c${n}" bench_efficiency.py --input "$size_ds" --impl jit
    run_one "r1_size_eff_uproot_c${n}" bench_efficiency.py --input "$size_ds" --impl uproot
done
unset PINNED
fi

# The Python half of the series, last, smallest first, and the chain (~16 min and ~16.6 GB of
# RSS per copy) after everything else: on 8-9 copies it needs ~133-150 GB of a ~184 GB node,
# and if the kernel kills it every other result is already on disk. A variant that failed at N
# copies is not started on more: it would fail the same way after up to ~2 h of reading, so the
# larger points are recorded as failed with skipped_after = N.
py_failed_filter=""
py_failed_eff=""
py_failed_chain=""

# size_py <variant> <copies> <label> <script> [args...]
size_py() {
    local variant="$1" n="$2" label="$3"
    shift 3
    local failed_var="py_failed_${variant}"
    local failed_at="${!failed_var}"
    if [[ -n "$failed_at" ]]; then
        echo "  -> $label: not run, the same variant failed at $failed_at copies" >&2
        printf '{"label":"%s","status":"failed","exit_code":null,"machine":"%s","skipped_after":%d}\n' \
            "$label" "$MACHINE" "$failed_at" >>"$RAW"
        return
    fi
    run_one "$label" "$@" || printf -v "$failed_var" '%s' "$n"
}

if has_test sizepy; then
echo "=== against input size, Python: $SIZE_SERIES copies${TASKSET_BIN:+, pinned to core $PIN_CORE} ==="
export PINNED=1
for n in $SIZE_SERIES; do
    size_ds="$(size_input "$n")"
    size_py filter "$n" "r1_size_filter_python_loop_c${n}" bench_filter.py \
        --input "$size_ds" --impl python --mode loop
    size_py eff "$n" "r1_size_eff_python_loop_c${n}" bench_efficiency.py \
        --input "$size_ds" --impl python --mode loop
done
for n in $SIZE_SERIES; do
    size_py chain "$n" "r1_size_chain_python_c${n}" bench_chain.py \
        --input "$(size_input "$n")" --impl python
done
unset PINNED
fi

if [[ -n "${DRY_RUN:-}" ]]; then
    echo
    echo "dry run: ${DRY_RUN_COUNT:-0} runs, each capped at ${RUN_TIMEOUT}s"
else
    echo "Records in $RAW"
    "$PY" "$TEST_DIR/plot_results.py" --results "$RESULTS"
fi
