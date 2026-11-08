#!/usr/bin/env python3
"""
Shared plumbing for the benchmarks: argument parsing, phase timing, memory and I/O counters,
and the JSON record written to stdout for run_benchmark.sh to collect.

Timing is split into four phases, not two, because the costs behave completely differently with
dataset size:

  setup  - opening the file, reading TTree metadata, building the correction JSON
  warmup - a throwaway pass over one entry, which is where cling compiles the kernel
  jit    - building the computation graph
  loop   - triggering it; the only phase that scales with the number of events

The split is not cosmetic. Under the old two-phase version, `rdf-report` minus `rdf-lazy` was
a constant ~1.08 s across chain lengths 1..5, independent of both event count and bytes read,
that the record attributed to the event loop. With the phases separated, that cost appears in
`wall_jit` and the two implementations' event loops come out equal.
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

# Bumped whenever a field changes meaning rather than just being added. Records from different
# versions must not be averaged together.
SCHEMA_VERSION = 3

from bench_spec import (  # noqa: F401 -- re-exported, the benchmarks use bc.<name>
    CHAIN_COLUMNS, CHAIN_STEPS, CHAINS, DEFAULT_ARM, DEFAULT_PERIOD, DEFAULT_POT,
    DEFAULT_RP_ID, EFFICIENCY_COLUMNS, LONG_CHAIN_STEPS, MAX_CHAIN_LEN, PERIODS, REPO_ROOT,
    TRACK_CUTS, TRACK_OPS, XI_RANGE, chain_columns, chain_cuts, chain_steps, input_files,
    is_file_list, is_glob,
)

import ROOT  # noqa: E402

ROOT.gROOT.SetBatch(True)
ROOT.gErrorIgnoreLevel = ROOT.kWarning

WARMUP_INPUT = os.path.join(REPO_ROOT, "examples", "test.root")


def build_parser(description, impls, modes=("vector", "loop")):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--input", required=True,
                        help="benchmark .root file, or a .txt list of them (one path per line)")
    parser.add_argument("--impl", required=True, choices=impls)
    parser.add_argument("--mode", default=modes[0], choices=modes,
                        help="python implementation only: numpy over the columns, or plain loops")
    parser.add_argument("--threads", type=int, default=0, help="0 disables ImplicitMT")
    parser.add_argument("--period", default=DEFAULT_PERIOD, choices=sorted(PERIODS),
                        help="detector configuration the chain's cuts are written for")
    parser.add_argument("--rp-id", type=int, default=None,
                        help="default: the period's RP (22 for 2026, 23 for run3, 3 for 2016)")
    parser.add_argument("--arm", default=DEFAULT_ARM)
    parser.add_argument("--pot-type", default=DEFAULT_POT, choices=["box", "cyl"])
    parser.add_argument("--tag", default="", help="free-form label copied into the output record")
    return parser


def setup_root(threads):
    if threads and threads > 0:
        ROOT.EnableImplicitMT(threads)
    return threads or 1


def warmup(args, build_and_trigger, tree="Events"):
    """
    Runs the same graph over a single entry so cling compiles the filter expressions and the
    efficiency kernel before the timed phase starts.

    The ds_xN series are copies of examples/test.root, so for them the small source has the
    same column types and the warmup does not touch the multi-GB input. Real-data lists and
    patterns warm up on their own first file instead: a different CMSSW release can store a column with a
    different type, and a kernel compiled for the wrong types would push the real compilation
    back into the timed phases. Must run before EnableImplicitMT: Range() is not supported
    under implicit multi-threading.
    """
    if is_file_list(args.input) or is_glob(args.input):
        source = input_files(args.input)[0]
    else:
        source = WARMUP_INPUT if os.path.exists(WARMUP_INPUT) else args.input
    build_and_trigger(ROOT.RDataFrame(tree, source).Range(1))


def tree_layout(path, tree="Events"):
    """
    Entries, clusters, branches, compressed bytes per branch and file-level codec of one file,
    from metadata only.

    Clusters are what ImplicitMT divides the work by, so they are reported wherever a dataset
    is described. The codec matters as much: CMS production NanoAOD is commonly LZMA while the
    ds_xN series is ZSTD, and decompression is most of the per-event cost. The algorithm is
    ROOT's enum value (1 zlib, 2 LZMA, 4 LZ4, 5 ZSTD).
    """
    f = ROOT.TFile.Open(path)
    if not f or f.IsZombie():
        raise OSError(f"cannot open {path}")
    t = f.Get(tree)
    if not t:
        f.Close()
        raise OSError(f"no {tree} tree in {path}")
    entries = int(t.GetEntries())
    iterator = t.GetClusterIterator(0)
    clusters = 0
    while iterator.Next() < entries:
        clusters += 1
    branches = list(t.GetListOfBranches())
    layout = {
        "entries": entries,
        "clusters": clusters,
        "branches": [b.GetName() for b in branches],
        # Every TTree opened on the file holds these baskets' index for every branch, read or not.
        "baskets": sum(int(b.GetWriteBasket()) for b in branches),
        "zip_bytes": {b.GetName(): int(b.GetZipBytes("*")) for b in branches},
        "compression_algorithm": int(f.GetCompressionAlgorithm()),
        "compression_level": int(f.GetCompressionLevel()),
        "size_bytes": int(f.GetSize()),
    }
    f.Close()
    return layout


def _inventory_entries():
    """File name -> entries from the inventory CSV in $EVENT_COUNTS, or {} without one."""
    path = os.environ.get("EVENT_COUNTS")
    if not path or not os.path.exists(path):
        return {}
    import csv

    with open(path) as f:
        return {row["name"]: int(row["entries"]) for row in csv.DictReader(f)
                if row.get("entries") and not row.get("error")}


def count_events(args, tree="Events"):
    """
    Entry count straight from the files' metadata, for the record only (n_events, events_per_s):
    bench_chain.py calls it after the timed phases, and no implementation is given it.

    A Count() action would be an entire event loop -- on a 14 M event file that was ~150 s per
    run. Opening each file is not free either: ~0.2 s for a ~2000-branch NanoAOD on Lustre,
    ~5 min per run over the 1453 Run 3 files. With $EVENT_COUNTS (an
    inventory_files.py CSV) the counts come from there, and the files are opened only if
    one of them is not in it.
    """
    return sum(file_entries(input_files(args.input), tree))


def file_entries(files, tree="Events"):
    """Entries of each file, from $EVENT_COUNTS where it has them all (see count_events)."""
    known = _inventory_entries()
    if known and all(os.path.basename(path) in known for path in files):
        return [known[os.path.basename(path)] for path in files]
    entries = []
    for path in files:
        f = ROOT.TFile.Open(path)
        entries.append(int(f.Get(tree).GetEntries()))
        f.Close()
    return entries


def make_dataframe(args, tree="Events"):
    # Patterns go to RDataFrame as they are: ROOT expands each and opens every match.
    if is_glob(args.input):
        patterns = args.input.split()
        if len(patterns) == 1:
            return ROOT.RDataFrame(tree, patterns[0])
        return ROOT.RDataFrame(tree, ROOT.std.vector["std::string"](patterns))
    files = input_files(args.input)
    if len(files) == 1:
        return ROOT.RDataFrame(tree, files[0])
    return ROOT.RDataFrame(tree, ROOT.std.vector["std::string"](files))


def build_efficiency_json(arm_key, pot_type, out_dir="/tmp"):
    """Writes the region_idx -> efficiency correctionlib JSON. Setup only, never timed."""
    from build_correction import build_diamond_efficiency_json

    efficiency_file = os.path.join(REPO_ROOT, "app", "efficiency.json")
    with open(efficiency_file) as f:
        run_number = int(next(iter(json.load(f))))
    out_path = os.path.join(out_dir, f"bench_eff_{pot_type}_{arm_key}.json")
    return build_diamond_efficiency_json(
        efficiency_file, arm_key, run_number, out_path, pot_type=pot_type
    )


def peak_rss_kb():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kB, macOS reports bytes.
    return rss // 1024 if sys.platform == "darwin" else rss


_PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4


def current_rss_kb():
    """
    Resident set size right now, not the peak.

    getrusage reports the high-water mark, so a trace built from it can only ever go up; it is
    the fallback off Linux, and the record flags which source was used.
    """
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * _PAGE_KB
    except OSError:
        return peak_rss_kb()


def proc_io():
    """
    The kernel's read counters for this process, or None off Linux.

    rchar is every byte handed to read()/pread(), whatever file system served it; read_bytes is
    what reached a block device, which Lustre clients may not account at all. They cross-check
    TFile::GetFileBytesRead from outside ROOT.
    """
    try:
        with open("/proc/self/io") as f:
            fields = dict(line.split(":", 1) for line in f if ":" in line)
    except OSError:
        return None
    return {"rchar": int(fields["rchar"]), "read_bytes": int(fields["read_bytes"])}


# Run as a separate interpreter, without ROOT: argv = pid, trace path, interval. Stops when the
# measured process is gone. The phase comes from <trace>.phase, which the measured process
# replaces atomically, so a read sees the old name or the new one.
#
# rss_kb is the measured process plus its descendants: bench_pool.py's worker processes hold
# the memory of uproot-pool and python-pool. The sampler is a child of the measured process
# itself and leaves itself out; a single-process benchmark has no other children, so its trace
# is the process alone, as before.
_RSS_SAMPLER = r"""
import os, sys, time
pid, path, interval = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
me = os.getpid()

def rss_kb(p):
    with open(f"/proc/{p}/statm") as f:
        return int(f.read().split()[1]) * page_kb

def children_by_ppid(p):
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                if int(f.read().rsplit(")", 1)[1].split()[1]) == p:
                    out.append(int(entry))
        except (OSError, ValueError, IndexError):
            pass
    return out

# Without CONFIG_PROC_CHILDREN the kernel has no children files, and the tree is found by PPID.
has_children_files = os.path.exists(f"/proc/{me}/task/{me}/children")

def children(p):
    if not has_children_files:
        return children_by_ppid(p)
    out = []
    try:
        tasks = os.listdir(f"/proc/{p}/task")
    except OSError:
        return out
    for task in tasks:
        try:
            with open(f"/proc/{p}/task/{task}/children") as f:
                out.extend(int(c) for c in f.read().split())
        except (OSError, ValueError):
            pass
    return out

def tree_rss_kb(root):
    total, todo = rss_kb(root), children(root)
    while todo:
        p = todo.pop()
        if p == me:
            continue
        try:
            total += rss_kb(p)
        except (OSError, ValueError, IndexError):
            continue
        todo.extend(children(p))
    return total

phase, start = "start", time.perf_counter()
with open(path, "w", buffering=1) as out:
    out.write("t_s,rss_kb,phase\n")
    while True:
        try:
            rss = tree_rss_kb(pid)
        except (OSError, ValueError, IndexError):
            break
        try:
            with open(path + ".phase") as f:
                phase = f.read().strip() or phase
        except OSError:
            pass
        out.write(f"{time.perf_counter() - start:.3f},{rss},{phase}\n")
        time.sleep(interval)
try:
    os.remove(path + ".phase")
except OSError:
    pass
"""


def start_rss_trace(interval=0.1):
    """
    Samples this process's RSS into $RSS_TRACE, tagged with the phase it was taken in.
    Returns the function that sets the phase, or None without $RSS_TRACE.

    Started from the measured process rather than by the runner: the runner launches the
    benchmark behind `timeout` and `/usr/bin/time`, so the pid it could see was the wrapper's.
    On Linux the sampling itself is a separate process: PyROOT keeps the GIL through the C++
    event loop, so a sampler thread in this interpreter took no sample from the start of the
    loop to its end. Off Linux it is such a thread, on getrusage.
    """
    path = os.environ.get("RSS_TRACE")
    if not path:
        return None

    if os.path.exists(f"/proc/{os.getpid()}/statm"):
        phase_path = path + ".phase"

        def set_phase(name):
            with open(phase_path + ".tmp", "w") as f:
                f.write(name)
            os.replace(phase_path + ".tmp", phase_path)

        set_phase("start")
        subprocess.Popen([sys.executable, "-c", _RSS_SAMPLER, str(os.getpid()), path,
                          str(interval)], stdin=subprocess.DEVNULL, close_fds=True)
        return set_phase

    handle = open(path, "w", buffering=1)
    handle.write("t_s,rss_kb,phase\n")
    state = {"phase": "start"}
    start = time.perf_counter()

    def sample():
        while True:
            handle.write(
                f"{time.perf_counter() - start:.3f},{current_rss_kb()},{state['phase']}\n"
            )
            time.sleep(interval)

    threading.Thread(target=sample, daemon=True).start()
    return lambda name: state.update(phase=name)


class Bench:
    """Collects one measurement record. Use `with bench.phase("loop"):` around timed work."""

    def __init__(self, args, test_name):
        if args.rp_id is None:
            args.rp_id = PERIODS[args.period]["rp_id"]
        self.args = args
        self._trace = start_rss_trace(float(os.environ.get("SAMPLE_INTERVAL", 0.1)))
        self.record = {
            "schema_version": SCHEMA_VERSION,
            "test": test_name,
            "impl": args.impl,
            "mode": args.mode,
            "threads": args.threads,
            "period": args.period,
            # The 11-filter chain with every cut of the period, whatever the test: the campaign
            # scripts set results of another chain aside by it.
            "chain_cuts": chain_cuts(args.period, args.rp_id),
            "rp_id": args.rp_id,
            "arm": args.arm,
            "pot_type": args.pot_type,
            "tag": args.tag,
            "input": os.path.basename(args.input),
            # On disk, compressed: the size a reader would quote for the dataset.
            "input_bytes": sum(os.path.getsize(f) for f in input_files(args.input)),
            "machine": os.environ.get("MACHINE", "local"),
            # The cluster's node, so that an odd point can be traced to a different node of
            # the same partition. None outside Slurm.
            "node": os.environ.get("SLURMD_NODENAME"),
            "storage": os.environ.get("STORAGE", "lustre"),
            # "cold" means run_benchmark.sh evicted the inputs' pages before this run, so the
            # loop read them from the file system rather than from RAM.
            "cache": os.environ.get("CACHE", "warm"),
            "root_version": ROOT.gROOT.GetVersion(),
            # Everything resident before any measurement started: the interpreter, PyROOT and
            # numpy -- ~470 MB, which on small inputs is most of the peak.
            "rss_baseline_kb": current_rss_kb(),
            # "statm" in older records: a sampler thread, starved through the event loop.
            "rss_trace_source": ("statm-process" if os.path.exists("/proc/self/statm")
                                 else "getrusage"),
        }

    @contextmanager
    def phase(self, name):
        """
        Times a phase. Re-entering the same phase accumulates rather than overwrites, because
        setup legitimately comes in two parts: what must precede the cling warmup (building the
        correction JSON) and what must follow it (EnableImplicitMT, which makes Range() -- and
        therefore the warmup -- unavailable).
        """
        if self._trace is not None:
            self._trace(name)
        bytes_before = ROOT.TFile.GetFileBytesRead()
        io_before = proc_io()
        start = time.perf_counter()
        # Process CPU time summed over all threads. GNU time's CPU% averages over the whole
        # process, whose first ~5 s are single-threaded setup, so it cannot say how many cores
        # the event loop itself kept busy.
        cpu_start = time.process_time()
        yield
        elapsed = time.perf_counter() - start
        cpu = time.process_time() - cpu_start
        read = int(ROOT.TFile.GetFileBytesRead() - bytes_before)
        self.record[f"wall_{name}"] = self.record.get(f"wall_{name}", 0.0) + elapsed
        self.record[f"cpu_{name}"] = self.record.get(f"cpu_{name}", 0.0) + cpu
        self.record[f"bytes_{name}"] = self.record.get(f"bytes_{name}", 0) + read
        io_after = proc_io()
        if io_before and io_after:
            for counter in ("rchar", "read_bytes"):
                key = f"io_{counter}_{name}"
                self.record[key] = (self.record.get(key, 0)
                                    + io_after[counter] - io_before[counter])
        if self._trace is not None:
            self._trace(f"after_{name}")

    def override_bytes(self, name, value):
        """
        Replaces a phase's byte count with one reported by the implementation itself.

        Needed for the uproot path: TFile::GetFileBytesRead only sees I/O that went through
        ROOT, so without this every uproot run claims to have read zero bytes.
        """
        self.record[f"bytes_{name}"] = int(value)
        self.record["bytes_source"] = "implementation"

    def finish(self, checksums, n_events=None, n_tracks=None):
        self.record["checksums"] = checksums
        self.record["n_events"] = n_events
        self.record["n_tracks"] = n_tracks
        self.record["peak_rss_kb"] = peak_rss_kb()
        self.record["peak_rss_net_kb"] = peak_rss_kb() - self.record["rss_baseline_kb"]
        self.record["bytes_total"] = int(ROOT.TFile.GetFileBytesRead())

        # What a real analysis pays once per process, as opposed to once per event.
        self.record["wall_fixed"] = sum(
            self.record.get(f"wall_{name}", 0.0) for name in ("setup", "warmup", "jit")
        )

        loop = self.record.get("wall_loop", 0.0)
        if n_events and loop > 0:
            self.record["events_per_s"] = n_events / loop
        if n_tracks and loop > 0:
            self.record["tracks_per_s"] = n_tracks / loop
        if loop > 0 and "cpu_loop" in self.record:
            self.record["cores_busy_loop"] = self.record["cpu_loop"] / loop

        # Threads alive at the end, not just Python's: thread pools (ImplicitMT's, uproot's)
        # outlive the loop, so a run that claims to be single-threaded can be checked. Linux only.
        try:
            self.record["os_threads"] = len(os.listdir("/proc/self/task"))
        except OSError:
            self.record["os_threads"] = None
        self.record["cpus_allowed"] = (len(os.sched_getaffinity(0))
                                       if hasattr(os, "sched_getaffinity") else None)
        uproot = sys.modules.get("uproot")
        self.record["uproot_version"] = getattr(uproot, "__version__", None)
        print("BENCH " + json.dumps(self.record))
        return self.record
