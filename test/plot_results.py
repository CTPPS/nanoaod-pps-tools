#!/usr/bin/env python3
"""
Turns results/raw.jsonl into a flat CSV and the plots of the benchmark campaign.

Repeats are aggregated, never overwritten: every curve goes through a median, and the spread
across repeats is kept for the error bars. Runs that failed or hit the timeout stay in the CSV
with status "failed", and runs disturbed by the machine (see flag_disturbed) with "disturbed".

Needs only matplotlib, not ROOT, so it runs on a laptop over a results directory copied off the
cluster.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

CSV_FIELDS = [
    "label", "status", "schema_version", "machine", "storage", "cache", "test", "impl", "mode", "threads",
    "chain", "chain_len", "tag", "input", "input_bytes", "n_events",
    "n_tracks", "wall_setup", "wall_warmup", "wall_jit", "wall_loop", "wall_fixed",
    "bytes_setup", "bytes_warmup", "bytes_jit", "bytes_loop", "bytes_total",
    "io_rchar_loop", "io_read_bytes_loop",
    "peak_rss_kb", "rss_baseline_kb",
    "peak_rss_net_kb", "time_maxrss_kb", "cpu_percent", "elapsed_s", "cpu_loop",
    "cores_busy_loop", "events_per_s", "tracks_per_s", "event_loops", "exit_code", "timeout_s",
    "pinned_core", "cpus_allowed", "os_threads", "root_version", "uproot_version", "node",
    "workers", "cpu_loop_workers", "peak_rss_tree_kb",
]


def load_records(results_dir):
    path = os.path.join(results_dir, "raw.jsonl")
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def dedupe_labels(records):
    """
    Keeps the last record per label when a results directory holds more than one campaign.

    A label names one configuration within one run of the harness, so a repeated label means
    the directory was appended to -- typically a job that died partway and was resubmitted.
    The abandoned attempts are the early ones and they ran against a cold page cache: in the
    thread sweep the discarded r1_*_t1 runs came in at 5.5 s against 4.1 s for the same label
    in the run that finished. Taking the median across all of them raises the single-thread
    baseline and inflates every speedup computed from it.

    Last rather than first: the run that completed wrote the later records. Repeated warm-ups
    are not counted as superseded: every job of a RESUME=1 campaign runs its own.
    """
    last_index = {}
    for index, record in enumerate(records):
        last_index[record.get("label")] = index
    keep = [r for i, r in enumerate(records) if last_index[r.get("label")] == i]
    superseded = [r for i, r in enumerate(records) if last_index[r.get("label")] != i
                  and not str(r.get("label", "")).startswith("warmup_")]
    return keep, len(superseded)


# A run whose event loop takes more than this many times the median of its configuration is
# left out of every median, range and fit. OUTLIER_FACTOR=0 keeps all runs.
OUTLIER_FACTOR = float(os.environ.get("OUTLIER_FACTOR", "1.5"))


def flag_disturbed(records, factor=OUTLIER_FACTOR):
    """
    Marks runs slowed down by the machine rather than by the code, as status "disturbed".

    On the shared Lustre of Helios a run occasionally takes two to three times as long as its
    repeats while reading the same bytes, its threads spinning on data that does not arrive.
    The rule is fixed in advance and applies to every campaign alike: a configuration (the
    label without its r<N>_ prefix) needs at least three ok runs, since with two there is no
    telling which one is off, and a run is disturbed when its loop time exceeds `factor` times
    the configuration's median. Slower runs within that bound are ordinary spread and stay.
    """
    if factor <= 0:
        return []
    groups = defaultdict(list)
    for record in records:
        match = re.match(r"r\d+_(.+)", str(record.get("label", "")))
        if match and record.get("status") == "ok" and record.get("wall_loop") is not None:
            groups[match.group(1)].append(record)
    flagged = []
    for runs in groups.values():
        if len(runs) < 3:
            continue
        typical = median([r["wall_loop"] for r in runs])
        for r in runs:
            if r["wall_loop"] > factor * typical:
                r["status"] = "disturbed"
                flagged.append((r["label"], r["wall_loop"], typical))
    return sorted(flagged)


def write_csv(records, results_dir):
    out_path = os.path.join(results_dir, "bench.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS + ["checksums"], extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["checksums"] = json.dumps(record.get("checksums"))
            writer.writerow(row)
    return out_path


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def aggregate(records, key_fields, value_field):
    """
    Median across repeats, with the range kept alongside it.

    Returns key -> (median, min, max, n). The spread is not decoration: it is the only thing
    that distinguishes a real difference between two implementations from run-to-run noise on
    a shared machine, and it is what the error bars on the figures are drawn from.
    """
    buckets = defaultdict(list)
    for record in records:
        if record.get("status") != "ok" or record.get(value_field) is None:
            continue
        key = tuple(record.get(field) for field in key_fields)
        buckets[key].append(record[value_field])
    return {
        key: (median(values), min(values), max(values), len(values))
        for key, values in buckets.items()
    }


def series(records, group_fields, x_field, value_field):
    """Aggregated curves: group key -> sorted [(x, median, min, max)]."""
    values = aggregate(records, tuple(group_fields) + (x_field,), value_field)
    out = defaultdict(list)
    for key, (mid, low, high, _) in values.items():
        out[key[:-1]].append((key[-1], mid, low, high))
    return {key: sorted(points) for key, points in out.items() if all(p[0] is not None for p in points)}


def plot_series(ax, points, label, **kwargs):
    """Draws a curve with min/max whiskers, so the reader sees the spread across repeats."""
    xs = [p[0] for p in points]
    mids = [p[1] for p in points]
    lows = [p[1] - p[2] for p in points]
    highs = [p[3] - p[1] for p in points]
    ax.errorbar(xs, mids, yerr=[lows, highs], marker="o", capsize=3, label=label, **kwargs)


def plot_rows(ax, rows, field, label, **kwargs):
    """plot_series for scalability_rows output: whiskers from field_lo / field_hi."""
    points = [(r["threads"], r[field], r[f"{field}_lo"], r[f"{field}_hi"]) for r in rows]
    plot_series(ax, points, label, lw=2, **kwargs)


def total_seconds(record):
    """
    Wall time of the whole process, start-up to exit, from GNU time.

    Records written before the parser fix lost the hours of GNU time's h:mm:ss, so a run over an
    hour reads as its minutes and seconds only. The process outlives its own measured phases
    (setup, warm-up, JIT, loop) by the interpreter start and the teardown, seconds rather than
    an hour, so the missing hours are the smallest number that brings it back above them.
    Without GNU time, falls back to the sum of the phases.
    """
    phases = (record.get("wall_fixed") or 0.0) + (record.get("wall_loop") or 0.0)
    elapsed = record.get("elapsed_s")
    if elapsed is None:
        return phases or None
    while elapsed < phases:
        elapsed += 3600
    return elapsed


def cores_busy(record):
    """
    Average number of cores the event loop kept busy.

    Measured directly as cpu_loop / wall_loop when the record has it. Older records only have
    GNU time's whole-process CPU%, and for those the non-loop part of the run is assumed to
    have used one core -- true of setup, warmup and JIT, which are all single-threaded.
    """
    if record.get("cores_busy_loop") is not None:
        return record["cores_busy_loop"]
    cpu, elapsed, loop = record.get("cpu_percent"), total_seconds(record), record.get("wall_loop")
    if not (cpu and elapsed and loop):
        return None
    return (cpu / 100 * elapsed - (elapsed - loop)) / loop


def fit_linear(xs, ys):
    """Least-squares intercept and slope."""
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
    return mean_y - slope * mean_x, slope


def _normalised(value):
    # Float sums are reduced in a thread-dependent order under ImplicitMT, so they agree to
    # rounding rather than bit for bit.
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, dict):
        return {k: _normalised(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalised(v) for v in value]
    return value


def check_results_agree(records):
    """
    Every run over the same events must produce the same answer, whatever the thread count
    and whichever of the core input and its slim copy it read. Returns {test: number of
    distinct results}.
    """
    distinct = defaultdict(set)
    for r in records:
        distinct[r.get("test")].add(json.dumps(_normalised(r.get("checksums")), sort_keys=True))
    return {test: len(values) for test, values in distinct.items()}


# The integer every implementation of a test reports. Per-step counts and float sums are left
# out: only some implementations report the former, and the latter differ in float32/float64
# accumulation between RDataFrame and numpy.
IMPL_ANSWER = {"filter": "events_passed", "chain": "events_passed", "chain11": "events_passed",
               "efficiency": "eff_hits"}


def check_impls_agree(records):
    """
    T5 on the impl input: {test: {implementation: answer}} for every test where they differ.

    validate.py checks agreement on examples/test.root; this repeats it on the data the
    comparison was actually measured on, at no extra cost.
    """
    answers = defaultdict(dict)
    for r in records:
        field = IMPL_ANSWER.get(r.get("test"))
        value = (r.get("checksums") or {}).get(field)
        if value is not None:
            answers[r["test"]][f"{r.get('impl')}/{r.get('mode')}"] = value
    return {test: values for test, values in answers.items() if len(set(values.values())) > 1}


# Experiments are identified by label prefix rather than by test/impl/threads: T1 and T2 share
# test, impl and thread count, and selecting on the columns would median them together.
CORE_TEST_LABEL = {
    "filter": "single filter",
    "chain": "5-filter chain",
    "chain11": "10-branch chain",
    "efficiency": "per-track correction",
}
CORE_TEST_COLOUR = {"filter": "#4575b4", "chain": "#1b7837", "chain11": "#762a83",
                    "efficiency": "#d73027"}

# Thread counts labelled on a log2 axis. Every measured count keeps its tick, but between 64
# and 192 the labels of 80, 88, ..., 176 would run into each other.
LABELLED_THREADS = {1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384}


def thread_labels(ticks):
    return [str(t) if t in LABELLED_THREADS else "" for t in ticks]


def core_rows(records, experiment):
    """Records from one experiment, matched on the r<N>_<experiment>_ label prefix."""
    pattern = re.compile(rf"r\d+_{experiment}_")
    return [r for r in records if pattern.match(str(r.get("label", "")))]


def curve(records, x_field, value_field):
    """Median of value_field per x, as sorted ([x], [y]). Empty when nothing qualifies."""
    buckets = defaultdict(list)
    for r in records:
        if r.get(x_field) is not None and r.get(value_field) is not None:
            buckets[r[x_field]].append(r[value_field])
    xs = sorted(buckets)
    return xs, [median(buckets[x]) for x in xs]


def measured_serial_fraction(records):
    """
    Serial fraction read off the phase timings at the lowest thread count, not fitted.

    Setup and cling JIT do not parallelise, so they are the serial region the fitted s should
    correspond to. Two numbers that disagree mean the degradation has a cause outside the
    code's own serial part.
    """
    threaded = [r for r in records if r.get("threads") and r.get("wall_loop") is not None]
    if not threaded:
        return None
    lowest = min(r["threads"] for r in threaded)
    at_one = [r for r in threaded if r["threads"] == lowest]
    fixed = median([(r.get("wall_setup") or 0) + (r.get("wall_jit") or 0) for r in at_one])
    loop = median([r["wall_loop"] for r in at_one])
    total = fixed + loop
    return fixed / total if total else None


def plot_core(ok, results_dir, plt, save, outputs, records_all=()):
    """
    The core campaign: strong scaling, weak scaling, query structure, implementations, and
    the file-width experiment.

    Ordered by what the reader needs first rather than by how the runs happened: the two
    scaling laws lead, memory follows them because it is the cost that the speedup curve
    cannot show, the two single-threaded experiments come next, and the figures that explain
    the shape of the scaling curves close.
    """
    for r in ok:
        busy = cores_busy(r)
        if busy is not None:
            r["cores_busy"] = busy

    strong_all = core_rows(ok, "strong")
    slim_all = core_rows(ok, "slim")
    # t0 (ImplicitMT off) is a baseline, not a point on a log thread axis.
    strong = [r for r in strong_all if r.get("threads")]
    slim = [r for r in slim_all if r.get("threads")]
    weak = core_rows(ok, "weak")
    weak_slim = core_rows(ok, "weakslim")
    qstruct = core_rows(ok, "qstruct")
    impls = core_rows(ok, "impl")
    sized = core_rows(ok, "size")
    # The 1 TB campaign has no T5 of its own: its implementation comparison is the largest
    # point of the size series, which is the same set of variants on one core.
    impls_from_size = not impls and bool(sized)
    if impls_from_size:
        largest = max(r.get("input_bytes") or 0 for r in sized)
        impls = [r for r in sized if (r.get("input_bytes") or 0) == largest]
    # uproot with its own thread pool is a separate point, not a repeat of the pinned one.
    for r in impls:
        if r.get("tag") == "own-threads":
            r["impl"] = f"{r['impl']} (own threads)"

    distinct = check_results_agree(strong_all + slim_all)
    for test, count in sorted(distinct.items()):
        status = "identical" if count == 1 else f"WARNING: {count} different results"
        print(f"  {test}: results across thread counts and full/slim input {status}")
    if impls:
        disagree = check_impls_agree(impls)
        for test, values in sorted(disagree.items()):
            print(f"  WARNING: T5 {test} implementations disagree: {values}")
        if not disagree:
            print("  T5: every implementation gives the same answer")

    # What each extra ImplicitMT worker costs. On the full schema every worker builds its own
    # ~2000-branch tree; on the slim copy the same slopes should be close to zero.
    for name, rows_set in (("full schema", strong), ("slim", slim)):
        for test in CORE_TEST_LABEL:
            cost = per_thread_cost(rows_set, test)
            if cost:
                print(f"  per extra thread [{name}] {test}: "
                      f"{cost.get('rss_mb', float('nan')):+.1f} MB RSS, "
                      f"{cost.get('bytes_mb', float('nan')):+.2f} MB read, "
                      f"{cost.get('cpu_s', float('nan')):+.3f} CPU-s in the loop")

    # The 1 TB slim campaign is sized by what its chain reads, so that is checked on every run.
    for test in CORE_TEST_LABEL:
        read = [r["bytes_loop"] for r in strong if r.get("test") == test and r.get("bytes_loop")]
        if read:
            print(f"  read in the loop [{test}]: {min(read) / 1e9:.1f}-{max(read) / 1e9:.1f} GB "
                  f"over {len(read)} runs")

    # T5's RDataFrame runs are the same configuration as T1's t0 runs. A gap between the two is
    # a property of when T5 ran, not of the implementation.
    rdf_impl = {"filter": "rdf", "chain": "rdf-lazy", "efficiency": "jit"}
    for test, impl in ({} if impls_from_size else rdf_impl).items():
        t5 = [r["wall_loop"] for r in impls if r.get("test") == test and r.get("impl") == impl]
        t0 = serial_loop([r for r in strong_all if r.get("impl") == impl], test)
        if t5 and t0:
            print(f"  T5 {test}/{impl}: loop {median(t5):.2f} s (n={len(t5)}) against "
                  f"T1 t0 {t0:.2f} s ({(median(t5) / t0 - 1) * 100:+.0f}%)")

    ticks = sorted({r["threads"] for r in strong + weak + weak_slim + slim}) or [1]

    # Ideal scaling is the diagonal on log-log and on linear-linear alike, but on a log thread
    # axis the 1-64 range -- where every curve still follows the diagonal and there is nothing
    # to read -- takes 79% of the width and leaves the plateau above 100 threads with 12%.
    # Sweeps that stop at 48 threads keep the log axis: their maximum sits low enough that a
    # linear axis would squash it against the left edge instead.
    thread_scale = os.environ.get("THREAD_SCALE", "linear" if max(ticks) >= 96 else "log")

    def even_ticks(hi):
        step = next(s for s in (1, 2, 4, 8, 16, 32, 64) if hi / s <= 8)
        return list(range(0, int(hi) + 1, step))

    def thread_axis(ax, marks=None):
        marks = marks or ticks
        if thread_scale == "linear":
            ax.set_xscale("linear")
            ax.set_xticks(even_ticks(max(marks)))
            ax.set_xlim(0, max(marks) * 1.02)
        else:
            ax.set_xscale("log", base=2)
            ax.set_xticks(marks)
            ax.set_xticklabels(thread_labels(marks))
            ax.minorticks_off()
        ax.set_xlabel("threads")

    # Must run after the data is plotted: on a linear axis the ideal line is excluded from the
    # range, or it would stretch the axis to 192 while the measured speedup peaks near 60.
    def speedup_axis(ax, marks):
        if thread_scale == "linear":
            ax.set_yscale("linear")
            measured = [float(v) for line in ax.get_lines()
                        if line.get_color() != "#bbbbbb"
                        for v in line.get_ydata() if v is not None and float(v) == float(v)]
            if measured:
                top = max(measured) * 1.08
                ax.set_ylim(0, top)
                ax.set_yticks(even_ticks(top))
        else:
            ax.set_yscale("log", base=2)
            ax.set_yticks(marks)
            ax.set_yticklabels(thread_labels(marks))
        ax.set_ylabel(r"speedup $S(n) = T(1)\,/\,T(n)$")

    # (1) Strong scaling: the same events, more threads. Amdahl applies here and only
    # here. Monotonic in n for any serial fraction, it has no way to express a curve that turns
    # back down; where it misses the measured points, the loss grows with the thread count.
    rows = {test: scalability_rows(strong, test) for test in CORE_TEST_LABEL}
    if any(rows.values()):
        fig, ax = plt.subplots(figsize=(8.5, 5.2))
        for test, pretty in CORE_TEST_LABEL.items():
            if rows[test]:
                plot_rows(ax, rows[test], "speedup", pretty, color=CORE_TEST_COLOUR[test])
        ax.plot(ticks, ticks, ls="-", lw=1, color="#bbbbbb", label="ideal ($S = n$)")

        # The models are fitted to one curve: the 5-filter chain, or the 10-branch one in a
        # campaign that ran only that.
        fit_test = "chain" if rows["chain"] else "chain11"
        if rows[fit_test]:
            measured = [(r["threads"], r["speedup"]) for r in rows[fit_test]]
            grid = [t for t in range(1, max(ticks) + 1)]
            lowest = min(n for n, _ in measured)
        if rows[fit_test] and lowest > 1:
            # The model is anchored at S(1) = 1. A sweep that starts at 64 threads (run2, the
            # refinement of the plateau) normalises to S(64) = 1 instead, and the fit then
            # describes the wrong curve. Merge such a sweep with the one holding the low-thread
            # points before fitting.
            print(f"  no 1-thread point (lowest is {lowest}): skipping the Amdahl fit")
        elif rows[fit_test]:
            fitted_s = fit_amdahl(measured)
            ax.plot(grid, [1 / (fitted_s + (1 - fitted_s) / n) for n in grid], ls="--", lw=1.6,
                    color="#888888", label=f"Amdahl, $s$={fitted_s:.3f}")
            print(f"  Amdahl serial fraction s = {fitted_s:.4f} (ceiling {1 / fitted_s:.2f}x)")

            observed = measured_serial_fraction(strong)
            if observed is not None:
                ax.text(0.02, 0.97,
                        f"fitted $s$ = {fitted_s:.3f}\n"
                        f"measured setup+JIT share at 1 thread = {observed:.3f}",
                        transform=ax.transAxes, va="top", fontsize=8)
                print(f"  measured serial share (setup+JIT) = {observed:.4f}")

        thread_axis(ax)
        speedup_axis(ax, ticks)
        ax.set_title("Strong scaling: fixed problem size, more threads")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, which="both")
        save(fig, "01_speedup_amdahl.png")

        # (2) The same data as a fraction of the threads paid for. A speedup of 5 is a
        # different statement at 8 threads than at 48, and only this axis says which.
        fig, ax = plt.subplots(figsize=(8, 4.8))
        for test, pretty in CORE_TEST_LABEL.items():
            if rows[test]:
                percent = [{**r, **{k: r[k] * 100 for k in
                                    ("efficiency", "efficiency_lo", "efficiency_hi")}}
                           for r in rows[test]]
                plot_rows(ax, percent, "efficiency", pretty, color=CORE_TEST_COLOUR[test])
        ax.axhline(100, color="#bbbbbb", lw=1)
        thread_axis(ax)
        ax.set_ylabel("parallel efficiency [%]")
        ax.set_ylim(0, 105)
        ax.set_title("Parallel efficiency: speedup per thread")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, which="both")
        save(fig, "02_parallel_efficiency.png")

    # (3) Weak scaling: N threads against N units of work (ds_xN or weak_N.txt), so work per
    # thread is constant. Ideal is a
    # flat wall-time line, and the question is the opposite of T1's -- not "is the same job
    # faster" but "does a job that grows with the machine still finish in the same time".
    #
    # Gustafson's law is not drawn: with the serial fraction taken from the T1 fit it predicts
    # ~24x at 32 threads against a measured 3-5x, because what grows here is not a serial
    # region but a cost paid once per worker thread. The straight-line fit t = w + c*N reads
    # that cost off directly.
    #
    # T2S (dashed) is the same series on slim copies. A unit of work on the full files is
    # 0.24-0.52 s of loop per thread, less than the ~1 s of CPU a worker spends building its
    # tree, so the solid curves are mostly that setup and the dashed ones are the work.
    if weak:
        fig, (ax_t, ax_s) = plt.subplots(1, 2, figsize=(12, 4.8))
        for test, pretty in CORE_TEST_LABEL.items():
            colour = CORE_TEST_COLOUR[test]
            for rows_set, style, suffix in ((weak, "-", ""), (weak_slim, "--", ", control file")):
                sub = [r for r in rows_set if r.get("test") == test]
                xs, ys = curve(sub, "threads", "wall_loop")
                if len(xs) < 2:
                    continue
                intercept, slope = fit_linear(xs, ys)
                label = f"{pretty}{suffix}: {slope * 1000:.0f} ms per extra thread"
                for _, points in series(sub, ("test",), "threads", "wall_loop").items():
                    plot_series(ax_t, points, label, lw=2, ls=style, color=colour)
                if not suffix:
                    ax_t.plot(xs, [intercept + slope * n for n in xs], ls=":", lw=1,
                              color=colour)
                print(f"  weak{suffix} {test}: loop = {intercept:.3f} s + "
                      f"{slope * 1000:.1f} ms x threads")
                ax_s.plot(xs, [n * ys[0] / y for n, y in zip(xs, ys)], marker="o", lw=2,
                          ls=style, color=colour, label=pretty + suffix)
        weak_ticks = sorted({r["threads"] for r in weak + weak_slim if r.get("threads")}) or ticks
        ax_s.plot(weak_ticks, weak_ticks, lw=1, color="#bbbbbb", label="ideal ($S = n$)")
        for ax in (ax_t, ax_s):
            thread_axis(ax, weak_ticks)
            ax.set_xlabel("threads (N threads on N units of work)")
            ax.legend(fontsize=8)
            ax.grid(alpha=0.3, which="both")
        ax_t.set_ylabel("event-loop wall time [s]")
        ax_t.set_title("Constant work per thread: flat is ideal")
        speedup_axis(ax_s, weak_ticks)
        ax_s.set_ylabel("scaled speedup")
        ax_s.set_title("Scaled speedup" + (" (dashed: control files)" if weak_slim else ""))
        save(fig, "03_weak_scaling.png")

    # (4) What the speedup curve cannot show. Every worker gets its own TTreeCache and its own
    # decompression buffers, which is why node memory rather than core count is what ends up
    # limiting the usable thread count.
    if strong:
        fig, ax = plt.subplots(figsize=(8, 4.8))
        for test, pretty in CORE_TEST_LABEL.items():
            sub = [r for r in strong if r.get("test") == test]
            for key, points in sorted(series(sub, ("test",), "threads", "peak_rss_kb").items()):
                plot_series(ax, [(x, m / 1024, lo / 1024, hi / 1024) for x, m, lo, hi in points],
                            pretty, color=CORE_TEST_COLOUR[test])
        thread_axis(ax)
        ax.set_ylabel("peak RSS [MB]")
        ax.set_title("Memory against thread count (fixed problem size)")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, which="both")
        save(fig, "04_rss_vs_threads.png")

    # (5) Memory along the weak-scaling series. Work per thread is constant here, so anything
    # steeper than linear in the thread count is the cost of coordination rather than of data.
    if weak:
        fig, ax = plt.subplots(figsize=(8, 4.8))
        for test, pretty in CORE_TEST_LABEL.items():
            xs, ys = curve([r for r in weak if r.get("test") == test], "n_events", "peak_rss_kb")
            if len(xs) < 2:
                continue
            ax.plot(xs, [y / 1024 for y in ys], marker="o", lw=2,
                    color=CORE_TEST_COLOUR[test], label=pretty)
        ax.set_xscale("log")
        ax.set_xlabel("events (growing with the thread count)")
        ax.set_ylabel("peak RSS [MB]")
        ax.set_title("Memory along the weak-scaling series")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, which="both")
        save(fig, "05_rss_vs_size.png")

    # (6) RSS over time, restricted to the strong sweep so the curves differ in one variable.
    outputs.extend(plot_rss_profile(results_dir, plt, name="06_rss_over_time.png",
                                    prefix="r1_strong_"))

    # (7) Query structure. Single-threaded on purpose: the number of event loops is a property
    # of how the query was written, not of the machine it ran on.
    if qstruct:
        fig, (ax_loops, ax_time) = plt.subplots(1, 2, figsize=(11.5, 4.6))
        for key, points in sorted(series(qstruct, ("impl",), "chain_len", "event_loops").items()):
            ax_loops.plot([p[0] for p in points], [p[1] for p in points], marker="o",
                          label=key[0])
        for key, points in sorted(series(qstruct, ("impl",), "chain_len", "wall_loop").items()):
            plot_series(ax_time, points, key[0])
        for ax in (ax_loops, ax_time):
            ax.set_xlabel("filters in chain")
            ax.set_xticks(sorted({r["chain_len"] for r in qstruct if r.get("chain_len")}))
            ax.legend(fontsize=8)
            ax.grid(alpha=0.3)
        ax_loops.set_ylabel("event loops over the dataset")
        ax_loops.set_title("Counted, not measured")
        ax_time.set_ylabel("event-loop wall time [s]")
        ax_time.set_title("...and what it costs")
        save(fig, "07_query_structure.png")

    # (8) Implementations, and where their time goes. Same file, same codec, same events for
    # every bar -- the comparison a reader is most likely to challenge is the one that must
    # not straddle two datasets.
    #
    # Events/s rather than tracks/s: only the efficiency test reports a track count, and a
    # figure keyed on it silently dropped the filter and chain rows.
    # Only single-core runs: uproot with its own thread pool is a different resource budget and
    # does not belong on a per-core comparison. Python is represented by its faster, loop mode:
    # the vector mode still loops over per-event objects to flatten them and only adds copies.
    # correctionlib is part of the RDataFrame solution, not a competing implementation.
    one_core = [r for r in impls if r.get("tag") != "own-threads"
                and r.get("impl") != "correctionlib"
                and not (r.get("impl") == "python" and r.get("mode") == "vector")]
    impl_tests = [t for t in CORE_TEST_LABEL if any(r.get("test") == t for r in one_core)]
    if impl_tests:
        def impl_name(key):
            return SIZE_IMPL_LABEL.get(key[0], key[0])

        per_test = {}
        for test in impl_tests:
            sub = [r for r in one_core if r.get("test") == test]
            values = aggregate(sub, ("impl", "mode"), "events_per_s")
            per_test[test] = (sub, values, sorted(values, key=lambda k: values[k][0]))
        heights = [max(len(per_test[t][2]), 1) for t in impl_tests]
        fig, axes = plt.subplots(len(impl_tests), 3, figsize=(17, 1.4 + 0.5 * sum(heights)),
                                 gridspec_kw={"height_ratios": heights,
                                              "width_ratios": (1, 1.3, 0.8)}, squeeze=False)
        # The warmup pass is where cling compiles the kernel, so together with graph building it
        # is RDataFrame's compilation cost -- the one-off price uproot and NumPy do not pay.
        for r in one_core:
            r["wall_compile"] = (r.get("wall_warmup") or 0.0) + (r.get("wall_jit") or 0.0)
            rss_kb = r.get("time_maxrss_kb") or r.get("peak_rss_kb")
            r["rss_gb"] = rss_kb * 1024 / 1e9 if rss_kb else None
        phase_names = ("wall_setup", "wall_compile", "wall_loop")
        phase_labels = ("setup", "JIT compilation", "event loop")
        phase_colours = ("#4575b4", "#1a9850", "#d73027")
        for row, test in enumerate(impl_tests):
            sub, values, keys = per_test[test]
            ax_tp, ax_ph, ax_rss = axes[row]
            names = [impl_name(k) for k in keys]
            rates = [values[k][0] for k in keys]
            ax_tp.barh(names, [rate / 1e6 for rate in rates], color=CORE_TEST_COLOUR[test])
            ax_tp.set_xlim(0, max(rates) / 1e6 * 1.25)
            for index, rate in enumerate(rates):
                ax_tp.text(rate / 1e6, index, f"  {rate / 1e6:.2f}", va="center", fontsize=7)
            ax_tp.set_ylabel(CORE_TEST_LABEL[test])
            ax_tp.grid(alpha=0.3, axis="x")

            rss = aggregate(sub, ("impl", "mode"), "rss_gb")
            peaks = [rss.get(k, (0.0,))[0] or 0.0 for k in keys]
            ax_rss.barh(names, peaks, color=[SIZE_IMPL_COLOUR.get(n, "#888888") for n in names])
            ax_rss.set_xlim(0, max(peaks) * 1.3)
            for index, peak in enumerate(peaks):
                ax_rss.text(peak, index, f"  {peak:.1f}", va="center", fontsize=7)
            ax_rss.set_yticklabels([])
            ax_rss.grid(alpha=0.3, axis="x")

            phases = {name: aggregate(sub, ("impl", "mode"), name) for name in phase_names}
            lefts = [0.0] * len(keys)
            for name, label, colour in zip(phase_names, phase_labels, phase_colours):
                widths = [phases[name].get(k, (0.0,))[0] or 0.0 for k in keys]
                ax_ph.barh(names, widths, left=lefts, color=colour,
                           label=label if row == 0 else None)
                lefts = [a + b for a, b in zip(lefts, widths)]
            # Linear, because on a log axis stacked segments are not proportional to time: the
            # ~1 s setup took as much width as a 20 s loop. The scale follows the compiled
            # implementations; the interpreted ones run off it and are labelled with their total.
            compiled = [t for n, t in zip(names, lefts) if not n.startswith("Python")]
            right = max(compiled or lefts) * 1.6
            ax_ph.set_xlim(0, right)
            for index, total in enumerate(lefts):
                label = f"{total:.1f} s" if total <= right else f"{total:.0f} s \u2192"
                ax_ph.text(min(total, right * 0.985), index, f"  {label}" if total <= right
                           else label, va="center", fontsize=7,
                           ha="left" if total <= right else "right",
                           color="black" if total <= right else "white")
            ax_ph.grid(alpha=0.3, axis="x")
        impl_events = impls[0].get("n_events")
        impl_input = (f"{impl_events / 1e6:.1f} million events" if impl_events
                      else impls[0].get("input", "one input"))
        axes[0][0].set_title(f"Event-loop throughput on {impl_input}, one core")
        axes[0][1].set_title("Wall time per query")
        axes[0][2].set_title("Peak memory")
        axes[-1][1].legend(*axes[0][1].get_legend_handles_labels(), fontsize=8,
                           loc="upper center", bbox_to_anchor=(0.5, -0.45), ncol=3)
        axes[-1][0].set_xlabel("million events / s")
        axes[-1][1].set_xlabel("wall time [s]")
        axes[-1][2].set_xlabel("peak RSS [GB]")
        save(fig, "08_implementations.png")

    # (9) Two baselines for the same speedup. ImplicitMT(1) is the right reference for how well
    # the parallel machinery scales; ImplicitMT off is the right one for what a user gains by
    # turning it on, and it is the faster of the two.
    serial = {test: serial_loop(strong_all, test) for test in CORE_TEST_LABEL}
    if strong and any(serial.values()):
        fig, ax = plt.subplots(figsize=(8.5, 5.2))
        for test, pretty in CORE_TEST_LABEL.items():
            colour = CORE_TEST_COLOUR[test]
            vs_mt1 = scalability_rows(strong, test)
            if vs_mt1:
                plot_rows(ax, vs_mt1, "speedup", f"{pretty} vs ImplicitMT(1)", color=colour)
            if serial[test] and vs_mt1:
                vs_off = scalability_rows(strong, test, baseline=serial[test])
                plot_rows(ax, vs_off, "speedup", f"{pretty} vs ImplicitMT off",
                          color=colour, ls="--", alpha=0.7)
                best = max(vs_off, key=lambda r: r["speedup"])
                print(f"  {test}: ImplicitMT(1) loop {vs_mt1[0]['seconds']:.2f} s, "
                      f"off {serial[test]:.2f} s; best speedup vs off "
                      f"{best['speedup']:.2f}x at {best['threads']} threads")
        ax.plot(ticks, ticks, lw=1, color="#bbbbbb", label="ideal ($S = n$)")
        thread_axis(ax)
        speedup_axis(ax, ticks)
        ax.set_ylabel("speedup")
        ax.set_title("Speedup against both serial baselines")
        ax.legend(fontsize=7)
        ax.grid(alpha=0.3, which="both")
        save(fig, "09_speedup_baselines.png")

    # (10) How many cores the event loop actually kept busy. The answer to "why does ImplicitMT
    # look single-threaded": whole-process CPU% averages the loop with ~5 s of serial setup.
    busy_sets = [(strong, "-", "")] + ([(slim, "--", ", control file")] if slim else [])
    if any(r.get("cores_busy") is not None for r in strong):
        fig, ax = plt.subplots(figsize=(8.5, 5.2))
        for test, pretty in CORE_TEST_LABEL.items():
            for rows_set, style, suffix in busy_sets:
                sub = [r for r in rows_set if r.get("test") == test]
                for _, points in series(sub, ("test",), "threads", "cores_busy").items():
                    plot_series(ax, points, pretty + suffix, lw=2, ls=style,
                                color=CORE_TEST_COLOUR[test])
        ax.plot(ticks, ticks, lw=1, color="#bbbbbb", label="all threads busy")
        thread_axis(ax)
        speedup_axis(ax, ticks)
        ax.set_ylabel("cores busy during the event loop")
        if any(r.get("cores_busy_loop") is None for r in strong + slim):
            ax.text(0.02, 0.97, "estimated from whole-process CPU% (no cpu_loop in records)",
                    transform=ax.transAxes, va="top", fontsize=8, style="italic")
        ax.set_title("Event-loop CPU utilisation")
        ax.legend(fontsize=7)
        ax.grid(alpha=0.3, which="both")
        save(fig, "10_cores_busy.png")

    # (11) T6: the file-width experiment. The two files hold the same events with the same
    # codec and cluster size and differ only in branch count, so whatever changes between the
    # solid and dashed curves is the cost of the schema every worker thread sets up.
    if slim:
        fig, (ax_t, ax_s, ax_m) = plt.subplots(1, 3, figsize=(16, 4.8))
        for test, pretty in CORE_TEST_LABEL.items():
            colour = CORE_TEST_COLOUR[test]
            for rows_set, rows_all, style, name in ((strong, strong_all, "-", "full schema"),
                                                   (slim, slim_all, "--", "control file")):
                sub = [r for r in rows_set if r.get("test") == test]
                if not sub:
                    continue
                label = f"{pretty}, {name}"
                for _, points in series(sub, ("test",), "threads", "wall_loop").items():
                    plot_series(ax_t, points, label, lw=2, ls=style, color=colour)
                speedups = scalability_rows(sub, test, baseline=serial_loop(rows_all, test))
                plot_rows(ax_s, speedups, "speedup", label, ls=style, color=colour)
                for _, points in series(sub, ("test",), "threads", "peak_rss_kb").items():
                    plot_series(ax_m, [(x, m / 1024, lo / 1024, hi / 1024)
                                       for x, m, lo, hi in points],
                                label, lw=2, ls=style, color=colour)
                xs, rss = curve(sub, "threads", "peak_rss_kb")
                best = max(speedups, key=lambda r: r["speedup"])
                per_thread = fit_linear(xs, rss)[1] / 1024 if len(xs) > 1 else float("nan")
                print(f"  {test} [{name}]: best {best['speedup']:.2f}x at {best['threads']} "
                      f"threads (vs ImplicitMT off), {per_thread:.0f} MB RSS per thread")
        ax_s.plot(ticks, ticks, lw=1, color="#bbbbbb", label="ideal ($S = n$)")
        for ax in (ax_t, ax_s, ax_m):
            thread_axis(ax)
            ax.grid(alpha=0.3, which="both")
        speedup_axis(ax_s, ticks)
        ax_s.set_ylabel("speedup vs ImplicitMT off, same file")
        ax_t.set_ylabel("event-loop wall time [s]")
        ax_m.set_ylabel("peak RSS [MB]")
        ax_t.set_title("Event-loop time")
        ax_s.set_title("Speedup")
        ax_m.set_title("Peak memory")
        ax_m.legend(fontsize=7)
        save(fig, "11_file_width.png")

    # (13) T5 against input size, from the size and sizepy series of the 1 TB campaign.
    if sized:
        plot_input_size(sized, plt, save)

    # (17) The whole node on the whole input, RDataFrame against the process pools.
    node = [r for r in records_all if re.match(r"r\d+_node_", str(r.get("label", "")))]
    if node:
        plot_whole_node(node, plt, save)


NODE_IMPL_LABEL = {"rdf-lazy": "RDataFrame\n(ImplicitMT)", "uproot-pool": "uproot\n(process pool)",
                   "python-pool": "Python (AsNumpy)\n(process pool)"}
NODE_IMPL_COLOUR = {"rdf-lazy": "#1b7837", "uproot-pool": "#762a83", "python-pool": "#e08214"}


def plot_whole_node(records, plt, save):
    """
    Time, CPU and memory of each implementation given the whole node and the whole input.

    Total time rather than the loop alone: the pools pay for starting their processes, as
    RDataFrame pays for its JIT, and a user waits for both. Memory is the whole process tree's,
    since the pools hold their data in the workers. A run that failed keeps its place on the
    axis, marked, rather than leaving a gap that reads as "not measured".
    """
    parsed = []
    for r in records:
        match = re.match(r"r\d+_node_\w+?_(rdf-lazy|uproot-pool|python-pool)_t(\d+)$",
                         str(r.get("label", "")))
        if match:
            parsed.append((match.group(1), int(match.group(2)), r))
    if not parsed:
        return
    impls = [i for i in NODE_IMPL_LABEL if any(p[0] == i for p in parsed)]
    threads = sorted({p[1] for p in parsed})
    width = 0.8 / len(threads)

    def value(r, field):
        if field == "wall_total":
            return total_seconds(r) / 60 if total_seconds(r) else None
        if field == "cpu_h":
            return r["cpu_loop"] / 3600 if r.get("cpu_loop") is not None else None
        rss = r.get("peak_rss_tree_kb") or r.get("time_maxrss_kb") or r.get("peak_rss_kb")
        return rss / 1024 ** 2 if rss else None

    panels = (("wall_total", "total time [min]"), ("cpu_h", "CPU in the event loop [core-h]"),
              ("rss", "peak RSS, all processes [GB]"))
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    answers = {}
    for ax, (field, ylabel) in zip(axes, panels):
        for k, n in enumerate(threads):
            for i, impl in enumerate(impls):
                runs = [r for p_impl, p_n, r in parsed if p_impl == impl and p_n == n]
                ok_values = [value(r, field) for r in runs if r.get("status") == "ok"]
                ok_values = [v for v in ok_values if v is not None]
                x = i + (k - (len(threads) - 1) / 2) * width
                if ok_values:
                    height = median(ok_values)
                    ax.bar(x, height, width * 0.9, color=NODE_IMPL_COLOUR[impl],
                           alpha=1.0 if k == len(threads) - 1 else 0.6)
                    ax.text(x, height, f"{height:.3g}", ha="center", va="bottom", fontsize=8)
                elif runs:
                    ax.text(x, 0, "failed", ha="center", va="bottom", fontsize=8,
                            color="#b2182b", rotation=90)
                for r in runs:
                    passed = (r.get("checksums") or {}).get("events_passed")
                    if r.get("status") == "ok" and passed is not None:
                        answers[(impl, n)] = passed
        ax.set_xticks(range(len(impls)))
        ax.set_xticklabels([NODE_IMPL_LABEL[i] for i in impls], fontsize=8)
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3, axis="y")
    for ax in axes:
        ax.margins(y=0.12)
    if len(set(answers.values())) > 1:
        print(f"  WARNING: whole-node runs disagree on events passed: {answers}")
    elif answers:
        print(f"  whole node: every implementation passes {next(iter(answers.values()))} events")
    for impl, n in sorted(answers):
        runs = [r for p_impl, p_n, r in parsed if p_impl == impl and p_n == n
                and r.get("status") == "ok"]
        total = median([v for v in (value(r, "wall_total") for r in runs) if v is not None])
        rss = median([v for v in (value(r, "rss") for r in runs) if v is not None])
        print(f"  whole node {impl} x{n}: total "
              f"{'n/a' if total is None else f'{total:.1f} min'}, "
              f"{'n/a' if rss is None else f'{rss:.1f} GB'} peak")
    events = next((r.get("n_events") for _, _, r in parsed if r.get("n_events")), None)
    suffix = f", {events / 1e6:.0f} M events" if events else ""
    fig.suptitle(f"Whole node, whole input: {', '.join(map(str, threads))} "
                 f"threads or processes{suffix}")
    save(fig, "17_whole_node.png")


def whole_node_table(records):
    """
    LaTeX tabular of the whole-node runs, one column per implementation: medians of the ok runs
    at the highest thread count, with the factor against RDataFrame where it has runs. None
    without whole-node records.
    """
    runs = defaultdict(list)
    for r in records:
        match = re.match(r"r\d+_node_\w+?_(rdf-lazy|uproot-pool|python-pool)_t(\d+)$",
                         str(r.get("label", "")))
        if match and r.get("status") == "ok":
            runs[(match.group(1), int(match.group(2)))].append(r)
    if not runs:
        return None
    top = max(n for _, n in runs)
    impls = [i for i in NODE_IMPL_LABEL if (i, top) in runs]

    def med(impl, get):
        values = [v for v in (get(r) for r in runs[(impl, top)]) if v is not None]
        return median(values) if values else None

    def gb(field):
        return lambda r: r[field] / 1e9 if r.get(field) is not None else None

    rows = [
        ("Runs", lambda r: 1, "{:.0f}", False),
        ("Event loop [s]", lambda r: r.get("wall_loop"), "{:.1f}", True),
        ("Whole process [s]", total_seconds, "{:.1f}", True),
        ("CPU in the loop [core-h]",
         lambda r: r["cpu_loop"] / 3600 if r.get("cpu_loop") is not None else None, "{:.2f}", True),
        ("Cores busy in the loop", lambda r: r.get("cores_busy_loop"), "{:.0f}", False),
        ("Requested by the reader [GB]", gb("bytes_loop"), "{:.1f}", False),
        ("Passed to \\texttt{read()} [GB]", gb("io_rchar_loop"), "{:.0f}", False),
        ("Read from storage [GB]", gb("io_read_bytes_loop"), "{:.0f}", False),
        ("Peak RSS, all processes [GB]",
         lambda r: (r.get("peak_rss_tree_kb") or r.get("time_maxrss_kb") or r.get("peak_rss_kb")
                    or 0) / 1024 ** 2 or None, "{:.1f}", True),
        ("Events per second [$10^6$]",
         lambda r: r["events_per_s"] / 1e6 if r.get("events_per_s") else None, "{:.1f}", False),
    ]
    lines = ["\\begin{tabular}{l" + "r" * len(impls) + "}", "\\toprule",
             " & " + " & ".join(NODE_IMPL_LABEL[i].split("\n")[0] for i in impls) + " \\\\",
             "\\midrule"]
    for name, get, fmt, relative in rows:
        cells = []
        base = med("rdf-lazy", get) if "rdf-lazy" in impls else None
        for impl in impls:
            if name == "Runs":
                cells.append(str(len(runs[(impl, top)])))
                continue
            value = med(impl, get)
            cell = "-" if value is None else fmt.format(value)
            if relative and base and value is not None and impl != "rdf-lazy":
                cell += f" ({value / base:.1f}$\\times$)"
            cells.append(cell)
        lines.append(f"{name} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", ""]
    return "\n".join(lines)


SIZE_IMPL_LABEL = {"rdf": "RDataFrame", "rdf-lazy": "RDataFrame", "jit": "RDataFrame",
                   "uproot": "uproot", "python": "Python (AsNumpy)"}
SIZE_IMPL_COLOUR = {"RDataFrame": "#1b7837", "uproot": "#762a83", "Python (AsNumpy)": "#e08214"}


def plot_input_size(records, plt, save):
    """
    Peak memory and total time against input size, per implementation, on one core.

    Total rather than loop time, because the loop alone hides the cost RDataFrame pays and the
    others do not: setup plus cling JIT, once per process. Failed runs have no record here, so a
    line that stops short is where that implementation ran out of memory or time.
    """
    tests = {t: pretty for t, pretty in CORE_TEST_LABEL.items()
             if any(r.get("test") == t for r in records)}
    if not tests:
        return
    fig, axes = plt.subplots(2, len(tests), figsize=(5 * len(tests), 8), squeeze=False)
    for column, (test, pretty) in enumerate(tests.items()):
        by_impl = defaultdict(list)
        for r in records:
            if r.get("test") != test or not r.get("input_bytes"):
                continue
            rss_kb = r.get("time_maxrss_kb") or r.get("peak_rss_kb")
            r["input_gb"] = r["input_bytes"] / 1e9
            r["rss_gb"] = rss_kb * 1024 / 1e9 if rss_kb else None
            r["wall_total"] = (r.get("wall_fixed") or 0.0) + (r.get("wall_loop") or 0.0)
            by_impl[SIZE_IMPL_LABEL.get(r.get("impl"), r.get("impl"))].append(r)

        totals = {}
        for name, rows in sorted(by_impl.items()):
            xs_m, rss = curve(rows, "input_gb", "rss_gb")
            xs_t, total = curve(rows, "input_gb", "wall_total")
            colour = SIZE_IMPL_COLOUR.get(name)
            axes[0][column].plot(xs_m, rss, marker="o", lw=2, color=colour, label=name)
            axes[1][column].plot(xs_t, total, marker="o", lw=2, color=colour, label=name)
            totals[name] = dict(zip(xs_t, total))
            if len(xs_m) > 1 and len(xs_t) > 1:
                print(f"  size {test} [{name}]: {fit_linear(xs_m, rss)[1]:.3f} GB of RSS and "
                      f"{fit_linear(xs_t, total)[1]:.2f} s per GB of input, "
                      f"measured up to {max(xs_t):.0f} GB")

        rdf, up = totals.get("RDataFrame", {}), totals.get("uproot", {})
        common = sorted(set(rdf) & set(up))
        if common:
            faster = [gb for gb in common if rdf[gb] < up[gb]]
            print(f"  size {test}: RDataFrame faster than uproot in total from {faster[0]:.0f} GB"
                  if faster else
                  f"  size {test}: uproot faster than RDataFrame in total up to {common[-1]:.0f} GB")

        axes[0][column].set_title(pretty)
        axes[1][column].set_xlabel("input size on disk [GB]")
        for ax in (axes[0][column], axes[1][column]):
            ax.set_xlim(left=0)
            ax.set_ylim(bottom=0)
            ax.grid(alpha=0.3)
    axes[0][0].set_ylabel("peak RSS [GB]")
    axes[1][0].set_ylabel("total time: setup + JIT + event loop [s]")
    axes[0][0].legend(fontsize=8)
    fig.suptitle("Memory and time against input size, one core")
    save(fig, "13_input_size.png")


def plot(records, results_dir):
    try:
        import matplotlib
    except ImportError:
        print("matplotlib not installed -- CSV written, plots skipped "
              "(conda install -c conda-forge matplotlib)", file=sys.stderr)
        return []

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ok = [r for r in records if r.get("status") == "ok"]
    outputs = []

    def save(fig, name):
        path = os.path.join(results_dir, name)
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        outputs.append(path)

    plot_core(ok, results_dir, plt, save, outputs, records)
    return outputs


def read_rss_trace(path):
    times, rss = [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                times.append(float(row["t_s"]))
                rss.append(float(row["rss_kb"]) / 1024)
            except (ValueError, KeyError):
                continue
    return times, rss


def plot_rss_profile(results_dir, plt, name="12_rss_over_time.png", prefix=None):
    """
    Memory over time for one benchmark family, one curve per thread count.

    Picks the family with the most thread variants, and among ties the one with the most
    samples -- traces are taken every 0.1 s, so a run of a few seconds leaves under a dozen
    points and a sparse family would draw a misleadingly angular profile.

    prefix restricts the choice to one experiment. Without it the core campaign's weak sweep
    can win the tie-break, and its curves differ in dataset as well as in thread count.
    """
    families = defaultdict(dict)
    for name_csv in os.listdir(results_dir):
        if not (name_csv.startswith("rss_") and name_csv.endswith(".csv") and "_t" in name_csv):
            continue
        if prefix and not name_csv.startswith(f"rss_{prefix}"):
            continue
        base, _, tail = name_csv[4:-4].rpartition("_t")
        if not tail.isdigit() or int(tail) == 0:
            continue
        families[base][int(tail)] = os.path.join(results_dir, name_csv)
    if not families:
        return []

    def family_weight(item):
        base, members = item
        return (len(members), sum(len(read_rss_trace(p)[0]) for p in members.values()))

    base, members = max(families.items(), key=family_weight)
    traces = []
    for threads in sorted(members):
        times, rss = read_rss_trace(members[threads])
        # A flat trace is the wrapper-pid bug -- the sampler followed `timeout`, not the
        # benchmark -- and a near-empty one cannot show a profile either way.
        if len(times) < 3 or max(rss) - min(rss) < 1:
            continue
        traces.append((threads, times, rss))
    if len(traces) < 2:
        return []

    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    colours = plt.get_cmap("viridis")
    # Traces with rss_trace_source "statm" came from a Python thread, which the event loop
    # starved of the GIL: no sample between its start and its end. "statm-process" traces are
    # sampled from outside the interpreter and cover the loop. Solid means consecutive samples,
    # dashed means interpolation across a gap, so the old figures cannot be read as a measured
    # ramp where nothing was measured.
    gap = 1.0
    for index, (threads, times, rss) in enumerate(traces):
        colour = colours(index / max(len(traces) - 1, 1))
        label = f"{threads} thread" + ("" if threads == 1 else "s")
        for point in range(len(times) - 1):
            ax.plot(times[point:point + 2], rss[point:point + 2], lw=1.8, color=colour,
                    ls="--" if times[point + 1] - times[point] > gap else "-",
                    label=label if point == 0 else None)
        ax.plot(times, rss, "o", color=colour, markersize=3.5)
    ax.set_xlabel("time since process start [s]")
    ax.set_ylabel("resident memory [MB]")
    ax.set_title(f"Memory profile over the run, by thread count ({base})")
    ax.legend(title="ImplicitMT", fontsize=8, ncol=2)
    ax.grid(alpha=0.3)
    ax.text(0.99, 0.02,
            "dots: samples   solid: consecutive   dashed: interpolated across an unsampled gap",
            transform=ax.transAxes, ha="right", fontsize=7, style="italic", color="#555555")
    fig.tight_layout()
    path = os.path.join(results_dir, name)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return [path]


def fit_amdahl(points):
    """
    Serial fraction s minimising relative error of S(n) = 1 / (s + (1 - s) / n).

    Coarse-to-fine: the quoted ceiling is 1/s, so a step that is harmless for s = 0.26 moves
    the ceiling by several speedup units once s falls to ~0.009 on the 1 TB sweeps.
    """
    def error_of(s):
        return sum((1 / (s + (1 - s) / n) / measured - 1) ** 2 for n, measured in points)

    step = 1 / 2000
    best = min((error_of(i * step), i * step) for i in range(1, 2000))
    s = best[1]
    for _ in range(3):
        lo = max(step / 50, s - step)
        step /= 50
        best = min([(error_of(s), s)] + [(error_of(lo + i * step), lo + i * step)
                                         for i in range(101)])
        s = best[1]
    return s


def per_thread_cost(records, test):
    """
    Slopes against the thread count of the medians of peak RSS [MB], bytes read in the loop
    [MB] and CPU spent in the loop [s], over the ImplicitMT runs of one test.

    CPU is what the threads burned, not wall time: a cost that every worker pays in parallel
    still shows up here, which is what separates "each thread does extra work" from "threads
    wait for each other". None when there are fewer than two thread counts.
    """
    buckets = defaultdict(list)
    for r in records:
        if r.get("test") != test or not r.get("threads") or r.get("wall_loop") is None:
            continue
        cpu = r.get("cpu_loop")
        if cpu is None and r.get("cores_busy") is not None:
            cpu = r["cores_busy"] * r["wall_loop"]
        buckets[r["threads"]].append((r.get("peak_rss_kb"), r.get("bytes_loop"), cpu))
    if len(buckets) < 2:
        return None
    xs = sorted(buckets)
    out = {}
    for index, (name, scale) in enumerate((("rss_mb", 1024), ("bytes_mb", 1e6), ("cpu_s", 1))):
        points = [(x, median([v[index] for v in buckets[x] if v[index] is not None]))
                  for x in xs]
        points = [(x, y / scale) for x, y in points if y is not None]
        if len(points) > 1:
            out[name] = fit_linear([p[0] for p in points], [p[1] for p in points])[1]
    return out


def serial_loop(records, test):
    """Median loop time with ImplicitMT off (threads == 0), or None if there are no such runs."""
    loops = [r["wall_loop"] for r in records
             if r.get("test") == test and r.get("threads") == 0 and r.get("wall_loop")]
    return median(loops) if loops else None


def scalability_rows(records, test, baseline=None):
    """
    Speedup and efficiency per thread count for one benchmark, with min-max whiskers.

    baseline=None measures speedup against the lowest thread count, i.e. ImplicitMT(1). Passing
    serial_loop() instead measures it against ImplicitMT off, which is the faster of the two:
    ImplicitMT(1) still goes through the task-based path. The whiskers take the loop time's
    spread across repeats at each thread count; the baseline's own spread is not propagated.
    """
    runs = defaultdict(list)
    for r in records:
        if r.get("status") == "ok" and r.get("test") == test and r.get("threads"):
            runs[r["threads"]].append(r)
    if not runs:
        return []
    if baseline is None:
        baseline = median([x["wall_loop"] for x in runs[min(runs)]])
    rows = []
    for threads in sorted(runs):
        loops = [x["wall_loop"] for x in runs[threads]]
        seconds = median(loops)
        totals = [t for t in (total_seconds(x) for x in runs[threads]) if t is not None]
        rows.append({
            "threads": threads,
            "runs": len(loops),
            "seconds": seconds,
            "seconds_lo": min(loops),
            "seconds_hi": max(loops),
            "total": median(totals) if totals else None,
            "fixed": median([x.get("wall_fixed") or 0.0 for x in runs[threads]]),
            "speedup": baseline / seconds,
            "speedup_lo": baseline / max(loops),
            "speedup_hi": baseline / min(loops),
            "efficiency": baseline / seconds / threads,
            "efficiency_lo": baseline / max(loops) / threads,
            "efficiency_hi": baseline / min(loops) / threads,
        })
    return rows


# 80-176 are the refinement sweep of the plateau: dropping them hides the region it resolves.
DEFAULT_TABLE_THREADS = "1 2 4 8 12 16 24 32 48 64 80 88 96 104 112 128 144 160 176 192"


def latex_escape(text):
    return str(text).replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%")


def scalability_table(records, test):
    """
    The thesis's scalability table (tab:scalability) for one benchmark of the strong sweep.

    Same definitions as the hand-written original, so a new campaign drops in unchanged: loop is
    the event-loop wall time under ImplicitMT(n), speedup and efficiency are against
    ImplicitMT(1), and throughput is events over that loop time. Total is the whole process
    (total_seconds), what a user waits for, so the fixed cost of opening the files and compiling
    the kernels stays visible next to the part that parallelises. Cores busy is the event loop's
    own CPU time over its wall time, the same window as the time column, so it is directly
    comparable to the thread count. The whole-process CPU figure is deliberately not shown: it
    also counts the single-threaded setup and JIT, so on 1 TB it reads ~48 s against a ~43 s
    loop at 48 threads, less than half of what the loop actually used. Returns None when the
    sweep has no records for this test.

    Rows are limited to TABLE_THREADS (environment, space-separated) plus the fastest point,
    so a dense sweep around the maximum does not turn the table into twenty lines.
    """
    rows = scalability_rows(records, test)
    if not rows:
        return None
    best = max(rows, key=lambda row: row["speedup"])
    shown = {int(n) for n in os.environ.get("TABLE_THREADS", DEFAULT_TABLE_THREADS).split()}
    rows = [row for row in rows if row["threads"] in shown or row is best]
    ok = [r for r in records if r.get("status") == "ok" and r.get("test") == test]
    n_events = median([r["n_events"] for r in ok if r.get("n_events")])
    loop_cpu = defaultdict(list)
    for r in ok:
        busy = cores_busy(r)
        if busy is not None and r.get("threads"):
            loop_cpu[r["threads"]].append(100 * busy)
    machines = sorted({r.get("machine") for r in ok if r.get("machine")})
    counts = [v[3] for k, v in aggregate(ok, ("threads",), "wall_loop").items() if k[0]]
    fewest, repeats = min(counts), max(counts)
    cold = any(r.get("cache") == "cold" for r in ok)

    def bold(row, text):
        return rf"\textbf{{{text}}}" if row is best else text

    body = []
    for row in rows:
        seconds, threads = row["seconds"], row["threads"]
        throughput = n_events / seconds / 1e6 if n_events else None
        busy_text = f"{median(loop_cpu[threads]) / 100:.1f}" if loop_cpu.get(threads) else "--"
        body.append(" & ".join([
            f"{threads:>2}",
            bold(row, f"{seconds:.2f}"),
            f"{row['total']:.1f}" if row["total"] is not None else "--",
            bold(row, f"{throughput:.2f}") if throughput is not None else "--",
            bold(row, f"{row['speedup']:.2f}"),
            f"{row['efficiency'] * 100:.0f}\\%",
            busy_text,
        ]) + r" \\")

    where = (f"measured on {latex_escape(machines[0].capitalize())}" if len(machines) == 1
             else "measured")
    if not n_events:
        events = "the core input"
    elif n_events >= 1e9:
        events = f"${n_events / 1e9:.2f}$ billion events"
    else:
        events = f"${n_events / 1e6:.1f}$ million events"
    if repeats == 1:
        median_note = "single run, "
    elif fewest == repeats:
        median_note = f"median of {repeats} repeats, "
    else:
        median_note = f"median of {fewest}--{repeats} repeats per point, "
    cache_note = "inputs evicted from the page cache before every run, " if cold else ""
    read = [r["bytes_loop"] for r in ok if r.get("threads") and r.get("bytes_loop")]
    read_note = ""
    if read:
        unit, scale = ("TB", 1e12) if max(read) >= 1e12 else ("GB", 1e9)
        low, high = min(read) / scale, max(read) / scale
        span = f"{low:.2f}" if f"{low:.2f}" == f"{high:.2f}" else f"{low:.2f}--{high:.2f}"
        read_note = f" Each run reads {span}~{unit} from disk in the event loop."
    caption = (
        f"Parallel scalability metrics for the {CORE_TEST_LABEL[test].replace('5-', 'five-')} "
        f"on {events}, {where} "
        f"({median_note}{cache_note}speedup against \\texttt{{ImplicitMT}} with one thread). "
        f"Loop is the event loop's wall time, from which throughput, speedup and efficiency are "
        f"computed; total is the whole process, including start-up, opening the files and "
        f"compiling the analysis. "
        f"Cores busy is the event loop's CPU time over its wall time, measured in the same "
        f"window as the loop column, so it is directly comparable to the thread count. "
        f"The speedup maximum at "
        f"{best['threads']} threads coincides with a parallel efficiency of only "
        f"${best['efficiency'] * 100:.0f}\\%$.{read_note}"
    )
    return "\n".join([
        r"\begin{table}[htbp]",
        r"\centering",
        rf"\caption{{{caption}}}",
        rf"\label{{tab:scalability-{test}}}",
        r"\setlength{\tabcolsep}{5pt}",
        r"\begin{tabular}{rrrrrrr}",
        r"\toprule",
        r"Threads & Loop [s] & Total [s] & Throughput [$10^6$ ev/s] & Speedup $S$ & Efficiency $E$ "
        r"& Cores busy \\",
        r"\midrule",
        *body,
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
    ])


SUMMARY_FIELDS = ["threads", "runs", "loop_s", "loop_min_s", "loop_max_s", "total_s", "fixed_s",
                  "throughput_mev_s", "speedup", "efficiency", "cores_busy"]


def strong_summary(records, test):
    """
    One row per thread count of the strong sweep, every count rather than TABLE_THREADS.

    Loop times are the median over the runs left after flag_disturbed, with their range; total
    is the whole process (total_seconds) and fixed the per-process setup, warm-up and JIT.
    """
    rows = scalability_rows(records, test)
    ok = [r for r in records if r.get("status") == "ok" and r.get("test") == test]
    n_events = median([r["n_events"] for r in ok if r.get("n_events")])
    busy = defaultdict(list)
    for r in ok:
        value = cores_busy(r)
        if value is not None and r.get("threads"):
            busy[r["threads"]].append(value)
    out = []
    for row in rows:
        threads = row["threads"]
        out.append({
            "threads": threads,
            "runs": row["runs"],
            "loop_s": f"{row['seconds']:.2f}",
            "loop_min_s": f"{row['seconds_lo']:.2f}",
            "loop_max_s": f"{row['seconds_hi']:.2f}",
            "total_s": f"{row['total']:.1f}" if row["total"] is not None else "",
            "fixed_s": f"{row['fixed']:.1f}",
            "throughput_mev_s": f"{n_events / row['seconds'] / 1e6:.2f}" if n_events else "",
            "speedup": f"{row['speedup']:.2f}",
            "efficiency": f"{row['efficiency']:.3f}",
            "cores_busy": f"{median(busy[threads]):.1f}" if busy.get(threads) else "",
        })
    return out


def write_scalability_tables(records, results_dir):
    """
    table_scalability_<test>.tex and summary_strong_<test>.csv for every benchmark of the
    strong sweep that has records.
    """
    strong = core_rows(records, "strong")
    paths = []
    for test in CORE_TEST_LABEL:
        table = scalability_table(strong, test)
        if table is None:
            continue
        path = os.path.join(results_dir, f"table_scalability_{test}.tex")
        with open(path, "w") as f:
            f.write(table)
        paths.append(path)
        path = os.path.join(results_dir, f"summary_strong_{test}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            writer.writeheader()
            writer.writerows(strong_summary(strong, test))
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    parser.add_argument("--csv-only", action="store_true")
    args = parser.parse_args()

    records = load_records(args.results)
    if not records:
        print(f"No records in {args.results}/raw.jsonl", file=sys.stderr)
        return 1

    records, superseded = dedupe_labels(records)
    if superseded:
        print(f"WARNING: ignoring {superseded} superseded records (duplicate labels from a "
              f"restarted job; the earlier attempts ran on a cold page cache)")

    disturbed = flag_disturbed(records)
    for label, loop, typical in disturbed:
        print(f"DISTURBED: {label} left out, loop {loop:.1f} s against a median of {typical:.1f} s "
              f"(> {OUTLIER_FACTOR:g}x)")

    print(f"{len(records)} records -> {write_csv(records, args.results)}")
    failed = [r for r in records if r.get("status") not in ("ok", "disturbed")]
    if failed:
        print(f"NOTE: {len(failed)} runs failed or timed out: "
              f"{', '.join(sorted(r['label'] for r in failed)[:10])}")

    for path in write_scalability_tables(records, args.results):
        print(f"  {path}")
    table = whole_node_table(records)
    if table is not None:
        path = os.path.join(args.results, "table_whole_node.tex")
        with open(path, "w") as f:
            f.write(table)
        print(f"  {path}")
    if not args.csv_only:
        for path in plot(records, args.results):
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
