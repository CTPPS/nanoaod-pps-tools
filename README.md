# NanoAOD PPS Analysis Tools

Python tools for analyzing CMS Precision Proton Spectrometer (PPS) data in NanoAOD format using ROOT RDataFrame.

## Overview

This toolkit provides efficient analysis of PPS detector data from CMS Run 3, including:
- **Event filtering** (single arm, double arm, specific detectors, xi ranges)
- **Histogram generation** for all PPS and proton reconstruction variables
- **2D correlation plots** (X vs Y track positions)
- **Statistical analysis** of proton tracks and reconstruction

## Requirements

- ROOT 6.32+ with PyROOT support
- Python 3.11+ (compatible with ROOT installation)
- `correctionlib` (schema v2)
- NanoAOD ROOT files with PPS data

The benchmark suite in `test/` additionally needs `uproot`, `awkward` and `matplotlib`; see
`test/environment.yml`.

## Installation

```bash
# Ensure ROOT is properly configured
source /path/to/root/bin/thisroot.sh

# Or use system Python compatible with ROOT
/opt/homebrew/bin/python3  # macOS Homebrew example
```

## Scripts

All analysis code lives in `app/`. Run the scripts from that directory, or with `app/` on
`PYTHONPATH`.

### 1. `app/analyze_root.py` - Data Inspection Tool

Analyzes ROOT NanoAOD files to discover and display PPS variables.

**Features:**
- Automatically finds all PPS and proton-related variables (regex: `*PPS*` or `*proton*`)
- Displays variable types and data for first 5 events
- Supports PPSLocalTrack, Proton_multiRP, and Proton_singleRP collections
- Saves detailed analysis to text file

**Usage:**
```bash
python3 app/analyze_root.py <input_file.root>
```

**Example:**
```bash
python3 app/analyze_root.py examples/test.root
```

**Output:**
- Console: Summary statistics and variable list
- File: `result_with_protons.txt` with detailed analysis

**Discovered Variables (25 total):**
- **HLT Triggers** (4): DiPFJetAve180_PPSMatch, PPSMaxTracksPerArm1, PPSMaxTracksPerRP4, PPSRandom
- **PPSLocalTrack** (9): x, y, decRPId, rpType, multiRPProtonIdx, singleRPProtonIdx, time, timeUnc, nPPSLocalTrack
- **Proton_multiRP** (8): arm, t, thetaX, thetaY, time, timeUnc, xi, nProton_multiRP
- **Proton_singleRP** (4): decRPId, thetaY, xi, nProton_singleRP

---

### 2. `app/analyze_proton_events.py` - RDataFrame Analysis Engine

High-performance analysis tool using ROOT RDataFrame for filtering events and generating histograms.

**Features:**
- **Fast columnar processing** with RDataFrame
- **Event filters**: single arm, double arm, detector-specific (RP 3, 23, 103, 123)
- **Flexible histogram generation** for any PPS variable
- **Automatic 2D histograms** when both X and Y are selected
- **Organized output** - all results saved to `data/`

**Usage:**
```bash
python3 app/analyze_proton_events.py <input_file.root>
```

**Example:**
```bash
python3 app/analyze_proton_events.py examples/test.root
```

**Output Structure:**
```
data/
├── pps_single_arm.root          # ROOT file with histograms
├── pps_single_arm_track_x.png   # X position histogram
├── pps_single_arm_track_y.png   # Y position histogram
└── pps_single_arm_xy.png        # 2D correlation plot
```

---

## Available Filters

### Event Selection Functions

```python
from analyze_proton_events import *   # run from app/, or add it to PYTHONPATH

# Single arm events (only left OR right, not both)
filter_single_arm_events(df)

# Double arm events (both left AND right)
filter_double_arm_events(df)

# Specific Roman Pot detector
filter_detector_specific_events(df, rp_id=23)  # RP IDs: 3, 23, 103, 123

# Detector type (pixel or diamond)
filter_detector_type(df, 'pixel')    # Silicon pixel detectors: RP 3, 23, 103, 123
filter_detector_type(df, 'diamond')  # Diamond timing detectors: RP 16, 22, 116, 122

# Proton xi range
filter_xi_ranged_events(df, xi_min=0.05, xi_max=0.2)
```

### Filter Combinations

Apply multiple filters sequentially:
```python
filters = [
    filter_double_arm_events,
    lambda df: filter_detector_type(df, 'pixel')
]
df_filtered = rdata_analysis("test.root", filters)
```

---

## Available Histogram Types

All 25 PPS variables are available as `PPSHistogramType` enum values:

### Track Variables
- `N_PPS_LOCAL_TRACK` - Number of tracks per event
- `PPS_LOCAL_TRACK_X` - X position [mm]
- `PPS_LOCAL_TRACK_Y` - Y position [mm]
- `PPS_LOCAL_TRACK_DEC_RP_ID` - Decimal Roman Pot ID
- `PPS_LOCAL_TRACK_RP_TYPE` - RP detector type
- `PPS_LOCAL_TRACK_TIME` - Time [ns]
- `PPS_LOCAL_TRACK_TIME_UNC` - Time uncertainty [ns]
- `PPS_LOCAL_TRACK_MULTI_RP_PROTON_IDX` - Multi-RP proton index
- `PPS_LOCAL_TRACK_SINGLE_RP_PROTON_IDX` - Single-RP proton index

### Multi-RP Proton Variables
- `N_PROTON_MULTI_RP` - Number of multi-RP reconstructed protons
- `PROTON_MULTI_RP_ARM` - Arm (0=sector45, 1=sector56)
- `PROTON_MULTI_RP_T` - Mandelstam t [GeV²]
- `PROTON_MULTI_RP_THETA_X` - θ_x angle [rad]
- `PROTON_MULTI_RP_THETA_Y` - θ_y angle [rad]
- `PROTON_MULTI_RP_TIME` - Time [ns]
- `PROTON_MULTI_RP_TIME_UNC` - Time uncertainty [ns]
- `PROTON_MULTI_RP_XI` - Fractional momentum loss ξ

### Single-RP Proton Variables
- `N_PROTON_SINGLE_RP` - Number of single-RP protons
- `PROTON_SINGLE_RP_DEC_RP_ID` - Decimal RP ID
- `PROTON_SINGLE_RP_THETA_Y` - θ_y angle [rad]
- `PROTON_SINGLE_RP_XI` - Fractional momentum loss ξ

### HLT Triggers
- `HLT_PPSMaxTracksPerArm1` - PPS trigger: max tracks per arm = 1
- `HLT_PPSMaxTracksPerRP4` - PPS trigger: max tracks per RP = 4
- `HLT_PPSRandom` - PPS random trigger
- `HLT_DiPFJetAve180_PPSMatch_Xi0p3_QuadJet_Max2ProtPerRP` - Combined jet+PPS trigger

---

## Customization Examples

### Example 1: Custom Filter + Specific Histograms

Edit the `main()` function in `analyze_proton_events.py`:

```python
def main():
    # ... setup code ...
    
    # Custom analysis: double arm events only
    filters = [filter_double_arm_events]
    df_filtered = rdata_analysis(file_path, filters)
    
    # Generate only proton xi histograms
    histogram_types = [
        PPSHistogramType.PROTON_MULTI_RP_XI,
        PPSHistogramType.PROTON_SINGLE_RP_XI,
    ]
    
    create_histograms_and_plots(
        df_filtered, 
        histogram_types, 
        output_prefix="pps_double_arm_xi"
    )
```

### Example 2: All Variables Analysis

```python
def main():
    # ... setup code ...
    
    # No filters - all PPS events
    df_filtered = rdata_analysis(file_path)
    
    # Generate ALL 25 histograms
    all_types = list(PPSHistogramType)
    
    create_histograms_and_plots(
        df_filtered, 
        all_types, 
        output_prefix="pps_complete_analysis"
    )
```

### Example 3: Specific Roman Pot Analysis

```python
def main():
    # ... setup code ...
    
    # Only events with RP 23 (sector 45, far station)
    filters = [lambda df: filter_detector_specific_events(df, 23)]
    df_filtered = rdata_analysis(file_path, filters)
    
    # Track position histograms + 2D plot
    histogram_types = [
        PPSHistogramType.PPS_LOCAL_TRACK_X,
        PPSHistogramType.PPS_LOCAL_TRACK_Y,
        PPSHistogramType.PPS_LOCAL_TRACK_DEC_RP_ID,
    ]
    
    create_histograms_and_plots(
        df_filtered, 
        histogram_types, 
        output_prefix="pps_rp23_analysis"
    )
```

### Example 4: Detector Type Filter (Pixel vs Diamond)

```python
def main():
    # ... setup code ...
    
    # Analyze only pixel detector events
    filters = [lambda df: filter_detector_type(df, 'pixel')]
    df_filtered = rdata_analysis(file_path, filters)
    
    # Generate position and RP ID histograms
    histogram_types = [
        PPSHistogramType.PPS_LOCAL_TRACK_X,
        PPSHistogramType.PPS_LOCAL_TRACK_Y,
        PPSHistogramType.PPS_LOCAL_TRACK_DEC_RP_ID,
    ]
    
    create_histograms_and_plots(
        df_filtered, 
        histogram_types, 
        output_prefix="pps_pixel_detectors"
    )
```

### Example 5: Xi Range Selection

```python
def main():
    # ... setup code ...
    
    # Select events with protons in specific xi range
    filters = [
        filter_double_arm_events,
        lambda df: filter_xi_ranged_events(df, 0.05, 0.2)
    ]
    df_filtered = rdata_analysis(file_path, filters)
    
    # Generate xi histograms
    histogram_types = [
        PPSHistogramType.PROTON_MULTI_RP_XI,
        PPSHistogramType.PROTON_SINGLE_RP_XI,
    ]
    
    create_histograms_and_plots(
        df_filtered, 
        histogram_types, 
        output_prefix="pps_xi_range_0.05_0.2"
    )
```

---

## Detector Type Mapping

The `filter_detector_type()` function uses `PPSLocalTrack_rpType` values to filter detector types:

### Pixel Detectors (Silicon tracking, rpType = 4)
Use `filter_detector_type(df, 'pixel')` to select silicon pixel tracking detectors:
- **RP 3** - Sector 45, near station
- **RP 23** - Sector 45, far station
- **RP 103** - Sector 56, near station
- **RP 123** - Sector 56, far station

### Diamond Detectors (Timing, rpType = 5)
Use `filter_detector_type(df, 'diamond')` to select diamond timing detectors:
- **RP 16** - Sector 45, near timing
- **RP 22** - Sector 45, far timing  
- **RP 116** - Sector 56, near timing
- **RP 122** - Sector 56, far timing

**Implementation:**
```python
def filter_detector_type(df, detector_type):
    """
    Filter by detector type using PPSLocalTrack_rpType
    
    Args:
        df: RDataFrame
        detector_type: 'pixel' (rpType=4) or 'diamond' (rpType=5)
    
    Returns:
        Filtered RDataFrame
    """
    detector_type_map = {
        'pixel': 4,    # Silicon pixel detectors
        'diamond': 5   # Diamond timing detectors
    }
    rp_type = detector_type_map[detector_type.lower()]
    return df.Filter(f"ROOT::VecOps::Any(PPSLocalTrack_rpType == {rp_type})")
```

**Usage Example:**
```python
# Analyze only pixel detector tracks (rpType = 4)
df_pixel = filter_detector_type(df, 'pixel')

# Analyze only diamond timing detector tracks (rpType = 5)
df_diamond = filter_detector_type(df, 'diamond')
```

**Note:** If you need to filter by specific RP ID (e.g., only RP 23), use `filter_detector_specific_events(df, rp_id=23)` instead.

---

## Roman Pot Detector IDs

PPS detectors are identified by decimal RP IDs (`PPSLocalTrack_decRPId`):

### Sector 45 (arm 0)
- **RP 3** (rpType=4) - Near horizontal pot, pixel detector
- **RP 16** (rpType=5) - Near timing detector, diamond
- **RP 22** (rpType=5) - Far timing detector, diamond
- **RP 23** (rpType=4) - Far horizontal pot, pixel detector

### Sector 56 (arm 1)  
- **RP 103** (rpType=4) - Near horizontal pot, pixel detector
- **RP 116** (rpType=5) - Near timing detector, diamond
- **RP 122** (rpType=5) - Far timing detector, diamond
- **RP 123** (rpType=4) - Far horizontal pot, pixel detector

**Filter by specific RP ID:**
```python
# Only events with RP 23 (sector 45, far pixel detector)
df_rp23 = filter_detector_specific_events(df, rp_id=23)

# Only events with RP 103 (sector 56, near pixel detector)
df_rp103 = filter_detector_specific_events(df, rp_id=103)
```

---

## Performance Notes

Measured on an exclusive node of the Ares cluster (AMD EPYC 7742, ROOT 6.32.10) with the
benchmark suite in `test/`.

- **Lazy evaluation** means `Filter()` and `Define()` build a graph and nothing runs until a
  result is requested. One pass over the data answers the whole query.
- **Avoid `Count()` after every filter.** Each call materialises the graph and forces another
  pass: five filters cost six passes and 3.4x the time. `Report()` gives the same per-filter
  statistics in a single pass.
- **Memory is independent of dataset size.** 498 MB at 347k events, 747 MB at 11.1 million.
- **Only named branches are read.** A five-filter query touches 2.4% of a 2.71 GB file.
- **Multi-threading via `ROOT.EnableImplicitMT(n)`** helps up to 8-16 threads and degrades
  beyond that: each worker gets its own `TTreeCache`, costing about 240 MB per thread.
- Processing ~350k events with PPS data: **under a second** for a filter chain.

---

## 3. `app/apply_corrections.py` - Calibration & Correction Tool

Apply detector calibration corrections to PPS data using correctionlib JSON files.

**Features:**
- **Standard HEP approach**: correctionlib Python API + RDataFrame C++
- **Flexible corrections**: per-track, per-event, binned, formulas
- **Metadata-driven**: Column names in JSON
- **Before/After comparison**: Histograms for original and corrected data

**Usage:**
```bash
# Single correction file
python3 app/apply_corrections.py <root_file> <correction_json>

# Test all examples
python3 app/apply_corrections.py <root_file>
```

**Examples:**
```bash
# Specific correction
python3 app/apply_corrections.py examples/test.root corrections-examples/x_track_range_correction.json

# All 4 examples automatically
python3 app/apply_corrections.py examples/test.root
```

**Output:**
```
data/
├── pps_original_*.png     # Original data
└── pps_corrected_*.png    # Corrected data
```

### Diamond detector efficiency

`apply_diamond_efficiency_jit()` assigns a per-track efficiency from a `correctionlib` payload
keyed by detector region. `app/efficiency.json` itself is not such a payload: it lists the
efficiencies per run, arm and pot type, and `build_diamond_efficiency_json()`
(`corrections-examples/build_correction.py`) turns one list into the payload first. Key `i` of
the payload is element `i` of the list, i.e. region `i` of `diamond_geometry`, counted from the
end of the sensor row with the smallest x.

```python
from build_correction import build_diamond_efficiency_json   # corrections-examples/ on sys.path
from apply_corrections import apply_diamond_efficiency_jit

payload = build_diamond_efficiency_json(
    "efficiency.json", arm_key="45", run_number=396733,
    out_path="corrections-examples/diamond_efficiency_arm45.json",
    pot_type="box",        # "box" (3 regions) or "cyl" (4 regions)
)
df = apply_diamond_efficiency_jit(
    df,
    rp_id=22,              # diamond RP: 16, 22, 116, 122
    arm_key="45",          # "45" or "56"
    correction_json=payload,
    pot_type="box",
)
```

The payload is evaluated **once per region** at setup and the resulting values are baked into a
`constexpr std::array` inside a JIT-compiled C++ kernel, so the event loop never calls back into
Python. This is what makes the step roughly four times faster than evaluating `correctionlib`
per track on 11 M events (7.6 s against 31.5 s on Ares; T5 in `test/README.md`).

Declared kernels are cached on the `ROOT` module, so re-importing or reloading the module does
not trigger cling redefinition errors in a long-lived notebook session.

### `app/diamond_geometry.py` - Detector geometry

Single source of truth for diamond pot geometry: crystal size, pot centres and rotation angles,
taken from `diamond_alignment_coords_2025.json`.

```python
import diamond_geometry as geo

geo.assign_region(x, y, arm_key="45", pot_type="box")   # -> region index, or -1 outside
geo.build_region_polygons("45")                          # -> polygons for overlay plots
geo.get_cpp_source("45", pot_type="box")                 # -> C++ source for gInterpreter.Declare
```

`get_cpp_source()` generates the C++ equivalent of `assign_region()` so that the Python and the
compiled paths cannot drift apart; `test/validate.py` cross-checks them on 100k random points.

### Correction File Format

Each JSON must include `metadata` with column names:

```json
{
  "schema_version": 2,
  "corrections": [
    {
      "name": "track_position_correction",
      "inputs": [{"name": "x", "type": "real"}],
      "output": {"name": "x_corrected", "type": "real"},
      "data": { /* correction definition */ }
    }
  ],
  "metadata": {
    "input_column": "PPSLocalTrack_x",
    "output_column": "PPSLocalTrack_x_corrected"
  }
}
```

### Available Examples

Four correction examples in `corrections-examples/`:

**1. `x_track_range_correction.json`** - Position-dependent formulas
- `x < 2.0 mm`: `x * 1.5 + 0.5`
- `2.0 ≤ x < 5.0 mm`: `x * 1.2 + 0.3`
- `5.0 ≤ x < 10.0 mm`: `x * 1.05 + 0.1`
- `x ≥ 10.0 mm`: `x * 1.01 - 0.05`

**2. `per_track_direct_values.json`** - Track-by-track offsets
- Track 0: +0.285 mm, Track 1: +0.155 mm, Track 2: +0.171 mm, etc.

**3. `per_track_binned_array.json`** - Binned array lookup by track index

**4. `per_event_per_track.json`** - 2D correction (event + track index)

### How It Works

1. Load JSON and metadata
2. Extract data from RDataFrame
3. Compute corrections with correctionlib
4. Store in C++ for RDataFrame access
5. Add `_corrected` column via `Define()`
6. Generate comparison histograms

### Custom Corrections

1. Create JSON with correctionlib schema v2
2. Add metadata:
   ```json
   "metadata": {
     "input_column": "PPSLocalTrack_y",
     "output_column": "PPSLocalTrack_y_corrected"
   }
   ```
3. Test: `python3 app/apply_corrections.py examples/test.root your_correction.json`
---

## File Structure

```
nanoaod-pps-tools/
├── README.md                     # This file
├── app/                          # Analysis code
│   ├── analyze_root.py              # Data inspection tool
│   ├── analyze_proton_events.py     # RDataFrame analysis engine
│   ├── apply_corrections.py         # Correction application tool
│   ├── diamond_geometry.py          # Detector geometry, Python and C++
│   ├── pps_analysis.ipynb           # Interactive walkthrough
│   ├── efficiency.json              # Diamond efficiencies per run, arm, pot type (not correctionlib)
│   └── diamond_alignment_coords_2025.json
├── test/                         # Benchmark suite: the scripts behind the thesis results
│   ├── README.md                    # Which script produces which result, and how to rerun it
│   ├── slurm_benchmark.sbatch       # Copies of ds_x32 (single core on Ares, file-width control)
│   ├── slurm_run3.sbatch            # Run 3 NanoAOD on Helios (strong scaling, whole node)
│   ├── run_benchmark.sh             # Campaign driver called by both
│   ├── bench_*.py                   # Individual benchmarks
│   ├── impl_*.py                    # RDataFrame, uproot and numpy implementations
│   ├── validate.py                  # Cross-checks the implementations agree
│   ├── inventory_files.py           # Entries and branch sizes of every input file
│   ├── dataset_json.py              # dataset.json: codec, branches, baskets of a set
│   ├── plot_results.py              # Per-set figures and bench.csv
│   ├── plot_real_vs_synthetic.py    # Strong-scaling figure and table (results/comparison)
│   └── plot_memory_control.py       # Memory per thread, full against slim files
├── results/                      # The measured sets used in the thesis (results/README.md)
├── examples/
│   └── test.root                    # Example NanoAOD file (Git LFS)
├── corrections-examples/
│   ├── build_correction.py          # Writes correctionlib payloads, incl. the diamond ones
│   ├── x_track_range_correction.json
│   ├── per_track_direct_values.json
│   ├── per_track_binned_array.json
│   ├── per_event_per_track.json
│   └── diamond_efficiency_*.json    # region_idx -> efficiency, built from app/efficiency.json
└── data/                         # Output directory (auto-created)
    ├── *.root                       # ROOT histogram files
    └── *.png                        # PNG plots
```

**Note:** `examples/test.root` is tracked with Git LFS. A plain `git clone` without
`git lfs install` leaves a 134-byte pointer file in its place.

---

## Benchmarks

The `test/` directory holds a benchmark suite comparing RDataFrame against `uproot`/`awkward`
and a pure Python/numpy baseline: on one core on copies of `ds_x32.root` (query structure and
input size), on ~4.5 TB of Run 3 NanoAOD (strong scaling to 192 threads, and the whole node),
and on full against slim copies of the same events (what the file width costs per thread).

```bash
python test/validate.py                                    # cross-check that implementations agree
sbatch test/slurm_benchmark.sbatch                         # single core, exclusive Ares node
sbatch --export=ALL,JOB=J3 test/slurm_run3.sbatch          # Run 3 whole node, Helios
```

See `test/README.md` for every campaign and `results/README.md` for what each set backs.

---

## Troubleshooting

### Issue: "No module named 'ROOT'"
**Solution:** Ensure ROOT environment is sourced:
```bash
source /path/to/root/bin/thisroot.sh
```

### Issue: "No module named 'correctionlib'"
**Solution:** Install correctionlib:
```bash
pip3 install correctionlib --break-system-packages
```

---

## References

- **CMS PPS**: [CERN-LHCC-2014-021](http://cds.cern.ch/record/1753795)
- **ROOT RDataFrame**: [ROOT Documentation](https://root.cern/doc/master/classROOT_1_1RDataFrame.html)
- **correctionlib**: [GitHub Repository](https://github.com/cms-nanoAOD/correctionlib)
- **NanoAOD Format**: [CMS NanoAOD Documentation](https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookNanoAOD)

---
## Author

Kszemka


Project: CMS PPS NanoAOD Validation  

Purpose: Efficiency analysis of unified data processing model