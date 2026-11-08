# Benchmark: RDataFrame vs Python na NanoAOD

W `test/` są tylko skrypty, z których pochodzą wyniki w pracy (`results/README.md`): blok A
(jeden rdzeń, Ares), F (Run 3, strong scaling), N (cały węzeł) i kontrola szerokości pliku.
Skrypty pobierania danych, budowania list i kopii oraz zakończone kampanie są w historii gita
(commit `060513b`, katalog `test/archive/`).

## Pliki

| plik | rola |
|---|---|
| `slurm_benchmark.sbatch` | zadanie na kopiach `ds_x32.root` (`DATASET=big`): blok A i kontrola szerokości; `validate.py`, potem `run_benchmark.sh` |
| `slurm_run3.sbatch` | zadanie na Run 3 (`DATASET=real`, `JOB=J1/J3/J4`): bloki F i N |
| `run_benchmark.sh` | pętla pomiarów (`TESTS=strong11 node11 qstruct size sizepy`), `COLD`, `RESUME`, `DRY_RUN`; zbiera `raw.jsonl`, na końcu woła `plot_results.py` |
| `bench_chain.py`, `bench_filter.py`, `bench_efficiency.py` | pojedynczy pomiar: łańcuch filtrów, filtr, kolumna efektywności |
| `bench_pool.py` | łańcuch na całym węźle w puli procesów: uproot (`uproot-pool`) albo AsNumpy (`python-pool`) |
| `bench_common.py`, `bench_spec.py` | fazy czasu, liczniki bajtów i RSS, rekord `BENCH`; łańcuchy, kolumny, cięcia (`PERIODS`) |
| `impl_rdf.py`, `impl_uproot.py`, `impl_python.py` | implementacje mierzonych operacji |
| `validate.py` | zgodność implementacji, geometria C++ vs Python, 1 vs N wątków |
| `inventory_files.py` | J1: `inventory.csv` (zdarzenia, rozmiar gałęzi łańcucha w każdym pliku) |
| `dataset_json.py` | `dataset.json`: kodek, gałęzie, koszyki na plik |
| `plot_results.py` | `raw.jsonl` → `bench.csv` i wykresy katalogu |
| `plot_real_vs_synthetic.py` | `comparison/full_files_chain11.png`, `sets_summary.csv` (z `--amdahl`) |
| `plot_memory_control.py` | `comparison/memory_control.csv`: MB na wątek, pełne kopie wobec slim |
| `environment.yml` | env `pps-bench` (ROOT, correctionlib, uproot, awkward, matplotlib) |

## Dane (muszą już leżeć na scratchu)

- `$SCRATCH/bench/data`: `ds_x32.root` (32 kopie `examples/test.root`, 11,1 mln zdarzeń,
  10,84 GB) i `ds_1.root` … `ds_96.root`, zwykłe kopie `ds_x32.root` (`cp`, nie `hadd`:
  `hadd` zmienia granice klastrów). Listy `lists/core.txt`, `impl.txt`, `size_N.txt` pisze
  `run_benchmark.sh`.
- `$SCRATCH/bench/slim11`: 96 plików slim `ds_N.root` z samymi 10 gałęziami łańcucha i
  `copies.txt` z ich liczbą.
- `$SCRATCH/bench/data3`: Run 3 NanoAOD w `data/` (dla J1 przez `INPUT_GLOB`), a dla J3/J4
  `core.txt`, `local.csv`, `sets.json` i `READY`.

## Pomiar i wykresy

Każdy pomiar czyta wejścia z dysku (`COLD=1`), a `RESUME=1` dopisuje tylko brakujące biegi.
`DRY_RUN=1 bash test/run_benchmark.sh` wypisuje biegi bez uruchamiania.

**Blok A** (Ares, `results/single-core-ares/`):

```bash
sbatch --time=06:00:00 test/slurm_benchmark.sbatch                                   # qstruct, size
sbatch --time=12:00:00 --dependency=afterany:<id> --export=ALL,TESTS=sizepy,RESUME=1 \
    test/slurm_benchmark.sbatch                                                      # Python
```

**Blok F** (Helios, `results/run3-strong/`):

```bash
D=$SCRATCH/bench/data3/data
INPUT_GLOB="$D/*/*/NANOAOD/*/*/*.root $D/*/*/NANOAOD/*/*/*/*/*/*.root" \
    RESULTS=$SCRATCH/bench/results-helios-run3-4tb-full-chain11 \
    sbatch --partition=plgrid-now --time=12:00:00 --export=ALL,JOB=J1 test/slurm_run3.sbatch
```

**Blok N** (Helios, `results/run3-node/`):

```bash
sbatch --export=ALL,JOB=J3 test/slurm_run3.sbatch    # RDataFrame, uproot-pool
sbatch --export=ALL,JOB=J4 test/slurm_run3.sbatch    # python-pool
```

**Kontrola szerokości** (Helios, `results/width-full/` i `results/width-slim/`): dwa zadania
`TESTS=strong11` z nagłówka `slurm_benchmark.sbatch`, na pełnych kopiach (`DATA_DIR=data`) i na
slim (`DATA_DIR=slim11`).

Wykresy i tabele porównania lokalnie: `results/README.md`.
