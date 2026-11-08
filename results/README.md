# Wyniki

Tylko zbiory, z których pochodzą rysunki i liczby w pracy. Każdy katalog ma `raw.jsonl`
(rekord na bieg, z `test/run_benchmark.sh`), `bench.csv` i ślady RSS `rss_<label>.csv`.
Wszystkie wejścia są czytane z dysku po wyrzuceniu z page cache. Jak powtórzyć pomiar:
`test/README.md`.

| katalog | blok | dane | w pracy |
|---|---|---|---|
| `single-core-ares/` | A, Ares, 1 rdzeń | 1–9 kopii `ds_x32` (11,1–99,9 mln zdarzeń), ZSTD:5 | `08_implementations.png` (`bench_implementations_100m`), tabela query structure |
| `run3-strong/` | F, Helios | cały pobrany Run 3: 5939 plików Run2024C–Run2026D, 3,15 mld zdarzeń, 4,47 TB, LZMA:9; `inventory.csv` | przez `comparison/`: rysunek pełnych plików, 125×, s = 0,0023, 66 MB na wątek |
| `run3-node/` | N, Helios | podzbiór Run 3 `core.txt` (1,80 TB); RDF na 192 wątkach, `uproot-pool` i `python-pool` na 192 procesach, po 3 biegi | `17_whole_node.png` (`bench_whole_node`); `run3_chain11_gate.txt`: zdarzenia po każdym filtrze w każdej erze |
| `width-full/` | kontrola szerokości, Helios | 96 kopii `ds_x32` (1984 gałęzie), 1,07 mld zdarzeń, 1,04 TB | nieużywany w pracy (szczyt RSS 24,7 GB przy 96 i 34,8 GB przy 192 wątkach) |
| `width-slim/` | kontrola szerokości, Helios | 96 plików slim (10 gałęzi łańcucha), 29,8 mld zdarzeń, 1,54 TB | wiersz „slim control” w tabeli eksperymentów i porównanie z Run 3 w H3: 16 MB na wątek wobec 66 MB (`comparison/memory_control.csv`) |
| `comparison/` | F, kontrola szerokości | z `run3-strong`, `width-full`, `width-slim` | `full_files_chain11.png` (`bench_full_files`), `sets_summary.csv`, `memory_control.csv` |

Rysunek i tabele porównania (tylko matplotlib):

```bash
python test/plot_real_vs_synthetic.py --out results/comparison --amdahl \
    --set "results/run3-strong:chain11:Run 3, strong scaling"
cp results/comparison/full_files_chain11.png doc/Images/bench_full_files.png

python test/plot_memory_control.py --out results/comparison \
    --set "results/width-full:chain11:artificial, full files" \
    --set "results/width-slim:chain11:artificial, slim files" \
    --profile "artificial, full files" --files "artificial, full files:96"
```

`plot_memory_control.py` pisze też `memory_control.png`, którego praca nie używa.

`08_implementations.png` i `17_whole_node.png`:

```bash
python test/plot_results.py --results results/single-core-ares   # 08_implementations.png
python test/plot_results.py --results results/run3-node          # 17_whole_node.png
```
