#!/usr/bin/env python3
"""
Strong scaling of one chain on one or more ~1 TB sets, on the same machine: one figure and one
table for all of them.

    plot_real_vs_synthetic.py --out results/comparison --amdahl \
        --set "results/run3-strong:chain11:Run 3, strong scaling"

--set DIR:TEST:LABEL is a results directory, the test field of its strong sweep (chain11 for
the 11-filter chain) and the name the set goes by; a directory without raw.jsonl or without
that sweep is skipped with a warning. --figure LABEL puts a set in the figure (every set
without any --figure).

--amdahl fits Amdahl's law S = 1/(s + (1-s)/N) to each strong curve, by least squares of the
relative error of S, draws it dotted on the speedup panel and prints s, 1/s and the
Karp-Flatt serial fraction e(N) = (1/S - 1/N)/(1 - 1/N) of every point.

Writes into --out:
  full_files_chain11.png  loop time, speedup against one thread and peak RSS against the thread
                          count, each with the min-max of the repeats
  sets_summary.csv        every column below, plus the events passing the chain
and prints the table. Per set: events; size on disk; codec, branches and baskets per file
(dataset.json, when the job wrote one); bytes read in the event loop at one thread, in GB and as
a share of the set; the fastest point (S_max and its thread count); the 5% plateau, the fewest
threads within 5% of the fastest loop; E at 96 threads; MB per thread (slope of peak RSS); and
GB/s read at the fastest point.

Loop times are medians of the repeats; a run slower than OUTLIER_FACTOR (default 1.5) times
the median of its configuration (at least three ok runs) is left out as disturbed.

Needs only matplotlib.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

# A linear thread axis: the last third of the node, where the curves flatten, gets a third of it.
THREAD_TICKS = list(range(0, 193, 32))
STYLES = [
    {"color": "#1b7837", "marker": "o"},
    {"color": "#d73027", "marker": "^"},
    {"color": "#4575b4", "marker": "s"},
    {"color": "#762a83", "marker": "D"},
    {"color": "black", "marker": "v"},
]


def style_of(label, index):
    """A fixed colour for Run 2, Run 3 and slim sets across all comparison figures, else the next one."""
    for position, key in enumerate(("Run 2", "Run 3", "slim")):
        if key in label:
            return STYLES[position]
    return STYLES[index % len(STYLES)]


PLATEAU = 0.05
EFFICIENCY_AT = 96
FIELDS = ["set", "test", "events", "input_tb", "codec", "branches", "baskets_per_file",
          "read_gb", "read_pct", "loop_s_t1", "loop_min_s", "threads_at_min", "max_speedup",
          "plateau_threads", "efficiency_96", "mb_per_thread", "gb_per_s_at_min",
          "events_passed"]
OUTLIER_FACTOR = float(os.environ.get("OUTLIER_FACTOR", "1.5"))


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def fit_linear(xs, ys):
    """Least-squares intercept and slope."""
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
    return mean_y - slope * mean_x, slope


def load_records(results_dir):
    path = os.path.join(results_dir, "raw.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def dedupe_labels(records):
    """
    The last record per label. A repeated label means the directory was appended to (a job
    that died partway and was resubmitted with RESUME=1); the run that completed wrote the
    later record.
    """
    last_index = {record.get("label"): index for index, record in enumerate(records)}
    return [r for i, r in enumerate(records) if last_index[r.get("label")] == i]


def flag_disturbed(records, factor=OUTLIER_FACTOR):
    """
    Marks runs slowed down by the machine rather than by the code, as status "disturbed".

    On the shared Lustre of Helios a run occasionally takes two to three times as long as its
    repeats while reading the same bytes. A configuration (the label without its r<N>_ prefix)
    needs at least three ok runs, since with two there is no telling which one is off, and a
    run is disturbed when its loop time exceeds `factor` times the configuration's median.
    """
    if factor <= 0:
        return
    groups = defaultdict(list)
    for record in records:
        match = re.match(r"r\d+_(.+)", str(record.get("label", "")))
        if match and record.get("status") == "ok" and record.get("wall_loop") is not None:
            groups[match.group(1)].append(record)
    for runs in groups.values():
        if len(runs) < 3:
            continue
        typical = median([r["wall_loop"] for r in runs])
        for r in runs:
            if r["wall_loop"] > factor * typical:
                r["status"] = "disturbed"


def parse_set(text):
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(f"--set takes DIR:TEST:LABEL, not '{text}'")
    return parts


def sweep(results_dir, test):
    """Per thread count of the strong sweep (r<N>_strong_*): median loop time, peak RSS, runs."""
    records = dedupe_labels(load_records(results_dir))
    flag_disturbed(records)
    strong = re.compile(r"r\d+_strong_")
    runs = [r for r in records
            if r.get("status") == "ok" and strong.match(str(r.get("label", "")))
            and r.get("test") == test and r.get("threads") and r.get("wall_loop") is not None]
    by_threads = {}
    for r in runs:
        by_threads.setdefault(r["threads"], []).append(r)
    points = []
    for n in sorted(by_threads):
        group = by_threads[n]
        rss = [r["peak_rss_kb"] / 1024 ** 2 for r in group if r.get("peak_rss_kb")]
        points.append({
            "threads": n,
            "loop": median([r["wall_loop"] for r in group]),
            "loop_lo": min(r["wall_loop"] for r in group),
            "loop_hi": max(r["wall_loop"] for r in group),
            "rss_gb": median(rss) if rss else None,
            # The peak is a high-water mark and moves with how the tasks happened to be
            # scheduled; the spread across repeats is what a step between neighbours is
            # compared against.
            "rss_lo": min(rss) if rss else None,
            "rss_hi": max(rss) if rss else None,
            "runs": group,
        })
    return points


def dataset_info(results_dir):
    path = os.path.join(results_dir, "dataset.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def summarise(name, test, points, info):
    first = points[0]
    one = first["runs"] if first["threads"] == 1 else []
    best = min(points, key=lambda p: p["loop"])
    runs = [r for p in points for r in p["runs"]]
    events = median([r["n_events"] for r in runs if r.get("n_events")])
    input_bytes = median([r["input_bytes"] for r in runs if r.get("input_bytes")])
    # One thread reads each basket once; more threads re-read a few at task boundaries.
    read = median([r["bytes_loop"] for r in (one or best["runs"])
                   if r.get("bytes_loop") is not None])
    read_best = median([r["bytes_loop"] for r in best["runs"] if r.get("bytes_loop") is not None])
    passed = sorted({(r.get("checksums") or {}).get("events_passed") for r in runs} - {None})
    plateau = min(p["threads"] for p in points if p["loop"] <= (1 + PLATEAU) * best["loop"])
    at_e = next((p for p in points if p["threads"] == EFFICIENCY_AT), None)
    rss = [(p["threads"], p["rss_gb"] * 1024) for p in points if p["rss_gb"]]
    return {
        "set": name,
        "test": test,
        "events": events,
        "input_tb": input_bytes / 1e12 if input_bytes else None,
        "codec": info.get("codec", ""),
        "branches": info.get("branches_median", info.get("branches", "")),
        "baskets_per_file": info.get("baskets_per_file"),
        "read_gb": read / 1e9 if read is not None else None,
        "read_pct": 100 * read / input_bytes if read is not None and input_bytes else None,
        "loop_s_t1": first["loop"] if one else None,
        "loop_min_s": best["loop"],
        "threads_at_min": best["threads"],
        "max_speedup": first["loop"] / best["loop"] if one else None,
        "plateau_threads": plateau,
        "efficiency_96": first["loop"] / at_e["loop"] / EFFICIENCY_AT if one and at_e else None,
        "mb_per_thread": mb_slope(rss),
        "gb_per_s_at_min": read_best / best["loop"] / 1e9 if read_best is not None else None,
        # More than one value would mean the thread count changed the answer.
        "events_passed": " ".join(str(v) for v in passed),
    }


def amdahl(n, s):
    return 1 / (s + (1 - s) / n)


def fit_amdahl(points):
    """Serial fraction s minimising the squared relative error of Amdahl's speedup, to 1e-7."""
    measured = [(p["threads"], points[0]["loop"] / p["loop"]) for p in points]

    def cost(s):
        return sum((amdahl(n, s) / speedup - 1) ** 2 for n, speedup in measured)

    best = min((i * 1e-5 for i in range(5001)), key=cost)
    return min((best + i * 1e-7 for i in range(-100, 101) if best + i * 1e-7 >= 0), key=cost)


def karp_flatt(points):
    """(threads, e) for every point above one thread."""
    return [(p["threads"], (p["loop"] / points[0]["loop"] - 1 / p["threads"])
             / (1 - 1 / p["threads"])) for p in points if p["threads"] > 1]


def mb_slope(points):
    """Slope of a straight line through (threads, MB); None below 2 thread counts."""
    return fit_linear(*zip(*points))[1] if len({n for n, _ in points}) >= 2 else None


def fmt(value, spec):
    return "-" if value is None or value == "" else format(value, spec)


TABLE = [
    # (console head, key, format)
    ("events", "events", ".3g"),
    ("TB", "input_tb", ".2f"),
    ("codec", "codec", "s"),
    ("branches", "branches", ""),
    ("baskets", "baskets_per_file", ","),
    ("read GB", "read_gb", ".1f"),
    ("read %", "read_pct", ".1f"),
    ("T(1) s", "loop_s_t1", ".0f"),
    ("Tmin s", "loop_min_s", ".1f"),
    ("at n", "threads_at_min", "d"),
    ("max S", "max_speedup", ".1f"),
    ("5% at", "plateau_threads", "d"),
    ("E(96)", "efficiency_96", ".2f"),
    ("MB/thr", "mb_per_thread", ".0f"),
    ("GB/s", "gb_per_s_at_min", ".2f"),
]


def cell(row, key, spec):
    value = row[key]
    if key == "events" and value:
        value = value / 1e9
    return fmt(value, spec)


def print_table(rows):
    print(f"{'':>9s}  " + "  ".join(f"{row['set'][:22]:>22s}" for row in rows))
    for head, key, spec in TABLE:
        print(f"{head:>9s}  " + "  ".join(f"{cell(row, key, spec):>22s}" for row in rows))
    print("  T = event-loop time; S = T(1)/T(n); E = S/n; read at 1 thread; "
          "MB/thr = slope of a straight line through peak RSS")


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v)
                             for k, v in row.items()})


def plot(sets, tests, path, sizes_tb=(), fits=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_t, ax_s, ax_m) = plt.subplots(1, 3, figsize=(15, 4.6))
    ticks = sorted({p["threads"] for points in sets.values() for p in points})
    for index, (name, points) in enumerate(sets.items()):
        style = dict(style_of(name, index), ls="-")
        xs = [p["threads"] for p in points]
        ax_t.errorbar(xs, [p["loop"] for p in points],
                      yerr=[[p["loop"] - p["loop_lo"] for p in points],
                            [p["loop_hi"] - p["loop"] for p in points]],
                      capsize=3, label=name, **style)
        if points[0]["threads"] == 1:
            ax_s.plot(xs, [points[0]["loop"] / p["loop"] for p in points], label=name, **style)
        if name in (fits or {}):
            ns = range(1, ticks[-1] + 1)
            ax_s.plot(ns, [amdahl(n, fits[name]) for n in ns], ls=":", lw=1.5,
                      color=style["color"], label=f"Amdahl, s = {fits[name]:.4f}")
        with_rss = [p for p in points if p["rss_gb"]]
        if with_rss:
            ax_m.errorbar([p["threads"] for p in with_rss], [p["rss_gb"] for p in with_rss],
                          yerr=[[p["rss_gb"] - p["rss_lo"] for p in with_rss],
                                [p["rss_hi"] - p["rss_gb"] for p in with_rss]],
                          capsize=3, label=name, **style)
    ax_s.plot([0, ticks[-1]], [0, ticks[-1]], ls="--", color="grey", lw=0.8, label="ideal")
    for ax in (ax_t, ax_s, ax_m):
        ax.set_xlim(0, ticks[-1] * 1.04)
        ax.set_xticks(THREAD_TICKS)
        ax.set_xlabel("threads")
        ax.grid(alpha=0.3, which="both")
        ax.legend(fontsize=8)
    ax_t.set_yscale("log")
    ax_s.set_ylim(0, ticks[-1] * 1.04)
    ax_s.set_yticks(THREAD_TICKS)
    ax_t.set_ylabel("event-loop time [s]")
    ax_s.set_ylabel("speedup against 1 thread")
    ax_m.set_ylabel("peak RSS [GB]")
    ax_t.set_title("event loop")
    ax_s.set_title("speedup")
    ax_m.set_title("memory")
    chain = {"chain11": "11-filter chain (10 branches)", "chain": "5-filter chain (4 branches)"}
    title = " / ".join(chain.get(t, t) for t in sorted(set(tests)))
    if sizes_tb:
        low, high = min(sizes_tb), max(sizes_tb)
        title += f", {high:.1f} TB" if f"{low:.1f}" == f"{high:.1f}" else f", {low:.1f}-{high:.1f} TB per set"
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", dest="sets", type=parse_set, action="append", required=True,
                        metavar="DIR:TEST:LABEL", help="a results directory (repeatable)")
    parser.add_argument("--figure", action="append", default=[], metavar="LABEL",
                        help="a set to draw (repeatable; default: every set)")
    parser.add_argument("--amdahl", action="store_true",
                        help="fit and draw Amdahl's law for every strong curve")
    parser.add_argument("--out", default=".", help="directory for the figure and the table")
    args = parser.parse_args()

    labels = [label for _, _, label in args.sets]
    unknown = [label for label in args.figure if label not in labels]
    if unknown:
        print(f"ERROR: --figure {', '.join(unknown)} is not the label of any --set",
              file=sys.stderr)
        return 1

    sets, tests, rows = {}, {}, []
    for results_dir, test, label in args.sets:
        points = sweep(results_dir, test) if os.path.isdir(results_dir) else []
        if not points:
            print(f"WARNING: skipping '{label}': no {test} strong sweep (r<N>_strong_*) in "
                  f"{results_dir}", file=sys.stderr)
            continue
        sets[label], tests[label] = points, test
        rows.append(summarise(label, test, points, dataset_info(results_dir)))
    if not rows:
        print("ERROR: no set with a strong sweep", file=sys.stderr)
        return 1

    print_table(rows)
    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "sets_summary.csv")
    write_csv(rows, csv_path)
    print(f"  {csv_path}")

    drawn = {label: sets[label] for label in (args.figure or labels) if label in sets}
    fits = {}
    for label, points in drawn.items():
        if not args.amdahl or points[0]["threads"] != 1:
            continue
        fits[label] = fit_amdahl(points)
        print(f"  Amdahl {label}: s = {fits[label]:.5f}, 1/s = {1 / fits[label]:.0f}, "
              f"s T(1) = {fits[label] * points[0]['loop']:.1f} s")
        loops = {p["threads"]: p["loop"] for p in points}
        for n, e in karp_flatt(points):
            measured = points[0]["loop"] / loops[n]
            print(f"    N = {n:4d}  S = {measured:6.1f}  Amdahl = {amdahl(n, fits[label]):6.1f}"
                  f"  Karp-Flatt e = {e:.5f}")
    if not drawn:
        print("no set of --figure has results yet -- figure skipped", file=sys.stderr)
        return 0
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("matplotlib not installed -- table written, figure skipped", file=sys.stderr)
        return 0
    png_path = os.path.join(args.out, "full_files_chain11.png")
    sizes_tb = [row["input_tb"] for row in rows if row["set"] in drawn and row["input_tb"]]
    plot(drawn, [tests[label] for label in drawn], png_path, sizes_tb, fits)
    print(f"  {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
