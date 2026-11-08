#!/usr/bin/env python3
"""
Memory per thread on the same events stored two ways: the artificial full files (96 copies of
ds_x32, 1984 branches) against the slim files (the same events, the ten branches of the chain).

    plot_memory_control.py --out results/comparison \
        --set "results/width-full:chain11:artificial, full files" \
        --set "results/width-slim:chain11:artificial, slim files" \
        --profile "artificial, full files" --files "artificial, full files:96"

--set DIR:TEST:LABEL as in plot_real_vs_synthetic.py. --profile LABEL draws that set's RSS over
the event loop at PROFILE_THREADS (default: the first set). --files LABEL:N also fits that set's
RSS at the end of the loop against max(T, min(2T, N)) open trees for T threads on N files: two
per thread while there are files enough, one per file once 2T reaches N, one per thread once T
exceeds N and threads share files.

Peak RSS is a high-water mark. On the full copies it comes from a transient early in the loop
up to about 112 threads and from the steady level above, so it rises in steps. The RSS at the
end of the loop (the highest sample of the last END_SHARE of the loop phase in
rss_<label>.csv) follows the steady level. A set whose traces hold fewer than MIN_SAMPLES loop
samples (the slim campaign's sampler stopped before the loop) gets the peak only.

Writes into --out:
  memory_control.png  peak RSS and RSS at the end of the loop against the thread count, and
                      the RSS profile over the loop
  memory_control.csv  per set and statistic: MB per thread (slope of a straight line; for
                      --files, MB per open tree), the intercept, the thread counts fitted and
                      the rms deviation of the fit

Needs only matplotlib.
"""

import argparse
import csv
import os
import sys

from plot_real_vs_synthetic import STYLES, fit_linear, median, parse_set, sweep

PROFILE_THREADS = (48, 96, 128, 192)
END_SHARE = 0.10
MIN_SAMPLES = 20


def loop_trace(results_dir, label):
    """(seconds since the loop started, RSS in GB) of the loop phase, or None."""
    path = os.path.join(results_dir, f"rss_{label}.csv")
    if not os.path.exists(path):
        return None
    times, rss = [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("phase") != "loop":
                continue
            try:
                times.append(float(row["t_s"]))
                rss.append(float(row["rss_kb"]) / 1024 ** 2)
            except (ValueError, KeyError):
                continue
    if len(times) < MIN_SAMPLES:
        return None
    return [t - times[0] for t in times], rss


def end_of_loop(trace):
    _, rss = trace
    return max(rss[-max(1, int(len(rss) * END_SHARE)):])


def points_of(results_dir, test):
    """Per thread count: median, min and max of peak RSS and of RSS at the end of the loop."""
    out = []
    for p in sweep(results_dir, test):
        traces = [loop_trace(results_dir, r["label"]) for r in p["runs"]]
        ends = [end_of_loop(t) for t in traces if t]
        out.append({
            "threads": p["threads"],
            "peak": (p["rss_gb"], p["rss_lo"], p["rss_hi"]),
            "end": (median(ends), min(ends), max(ends)) if ends else None,
            "traces": [(r["label"], t) for r, t in zip(p["runs"], traces) if t],
        })
    return out


def open_trees(threads, files):
    return max(threads, min(2 * threads, files))


def slope(points, key, files=None):
    shown = [p for p in points if p[key] and p[key][0] is not None]
    if len({p["threads"] for p in shown}) < 2:
        return None
    xs = [open_trees(p["threads"], files) if files else p["threads"] for p in shown]
    ys = [p[key][0] * 1024 for p in shown]
    intercept, mb = fit_linear(xs, ys)
    rms = (sum((y - intercept - mb * x) ** 2 for x, y in zip(xs, ys)) / len(xs)) ** 0.5
    return {"statistic": f"{key}, open trees on {files} files" if files else key,
            "mb_per_thread": mb, "intercept_mb": intercept,
            "threads": f"{shown[0]['threads']}-{shown[-1]['threads']}", "points": len(xs),
            "rms_mb": rms}


def parse_files(text):
    label, _, n = text.rpartition(":")
    if not label or not n.isdigit():
        raise argparse.ArgumentTypeError(f"--files takes LABEL:N, not '{text}'")
    return label, int(n)


def plot(sets, profile, path, models):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_p, ax_e, ax_t) = plt.subplots(1, 3, figsize=(15, 4.6))
    for (name, points), style in zip(sets.items(), STYLES * len(sets)):
        for ax, key in ((ax_p, "peak"), (ax_e, "end")):
            shown = [p for p in points if p[key] and p[key][0] is not None]
            if not shown:
                continue
            ax.errorbar([p["threads"] for p in shown], [p[key][0] for p in shown],
                        yerr=[[p[key][0] - p[key][1] for p in shown],
                              [p[key][2] - p[key][0] for p in shown]],
                        capsize=3, ls="-", label=name, **style)
        if name in models:
            files, fit = models[name]
            ts = range(1, max(p["threads"] for p in points) + 1)
            ax_e.plot(ts, [(fit["intercept_mb"] + fit["mb_per_thread"] * open_trees(t, files))
                           / 1024 for t in ts], ls="--", color="grey", lw=1,
                      label=f"{fit['mb_per_thread']:.0f} MB x max(T, min(2T, {files}))")
    colours = plt.get_cmap("viridis")
    by_threads = {p["threads"]: p for p in sets.get(profile, [])}
    wanted = [n for n in PROFILE_THREADS if n in by_threads and by_threads[n]["traces"]]
    for i, n in enumerate(wanted):
        _, (times, rss) = by_threads[n]["traces"][0]
        ax_t.plot(times, rss, color=colours(i / max(1, len(wanted) - 1)), lw=1.2,
                  label=f"{n} threads")
    for ax in (ax_p, ax_e):
        ax.set_xlabel("threads")
        ax.set_xlim(0, max(p["threads"] for pts in sets.values() for p in pts) * 1.04)
    ax_t.set_xlabel("time since the event loop started [s]")
    for ax in (ax_p, ax_e, ax_t):
        ax.set_ylabel("resident memory [GB]")
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    ax_p.set_title("peak RSS")
    ax_e.set_title(f"RSS at the end of the loop (last {END_SHARE:.0%})")
    ax_t.set_title(f"RSS over the loop: {profile}")
    fig.suptitle("Memory against threads: the same events in files of 1984 and of 10 branches")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", dest="sets", type=parse_set, action="append", required=True,
                        metavar="DIR:TEST:LABEL", help="a results directory (repeatable)")
    parser.add_argument("--profile", metavar="LABEL",
                        help="the set whose RSS over the loop is drawn (default: the first)")
    parser.add_argument("--files", type=parse_files, action="append", default=[],
                        metavar="LABEL:N", help="fit that set against open trees on N files")
    parser.add_argument("--out", default=".", help="directory for the figure and the table")
    args = parser.parse_args()

    labels = [label for _, _, label in args.sets]
    profile = args.profile or labels[0]
    files = dict(args.files)
    unknown = [label for label in [profile, *files] if label not in labels]
    if unknown:
        print(f"ERROR: {', '.join(unknown)} is not the label of any --set", file=sys.stderr)
        return 1

    sets, rows, models = {}, [], {}
    for results_dir, test, label in args.sets:
        points = points_of(results_dir, test) if os.path.isdir(results_dir) else []
        if not points:
            print(f"WARNING: skipping '{label}': no {test} strong sweep in {results_dir}",
                  file=sys.stderr)
            continue
        sets[label] = points
        for key in ("peak", "end"):
            fit = slope(points, key)
            if fit:
                rows.append({"set": label, **fit})
        if label in files:
            fit = slope(points, "end", files[label])
            if fit:
                rows.append({"set": label, **fit})
                models[label] = (files[label], fit)
    if not sets:
        print("ERROR: no set with a strong sweep", file=sys.stderr)
        return 1

    for name, points in sets.items():
        print(name)
        print(f"  {'threads':>7s}  {'peak GB':>8s}  {'end GB':>8s}")
        for p in points:
            end = f"{p['end'][0]:8.1f}" if p["end"] else f"{'-':>8s}"
            print(f"  {p['threads']:7d}  {p['peak'][0]:8.1f}  {end}")
    for row in rows:
        print(f"  {row['set']}, {row['statistic']}: {row['mb_per_thread']:.1f} MB per unit "
              f"(+{row['intercept_mb']:.0f} MB, {row['threads']} threads, "
              f"rms {row['rms_mb']:.0f} MB)")

    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "memory_control.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["set", "statistic", "mb_per_thread",
                                               "intercept_mb", "threads", "points", "rms_mb"])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v)
                             for k, v in row.items()})
    print(f"  {csv_path}")
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("matplotlib not installed -- table written, figure skipped", file=sys.stderr)
        return 0
    png_path = os.path.join(args.out, "memory_control.png")
    plot(sets, profile, png_path, models)
    print(f"  {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
