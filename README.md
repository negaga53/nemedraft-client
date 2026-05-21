# NemeDraft Client

Open-source overlay client for NemeDraft, an AI-assisted MTG Arena draft pick predictor trained on 17Lands trophy data.

This repo holds the client-side code that runs on user machines:

- The overlay that watches Arena's log file and shows pick recommendations
- Shared utilities used by the overlay (data loading, signal calculation, deck-building heuristics)

The inference server lives on a remote server. The client does not run the model locally; it sends pack/pool state to the remote server and renders the response.

## Model performance

The model is a ~5.3M-parameter neural net trained on 17Lands trophy-deck data across seven recent Standard sets. It learns to rank the cards in a pack by how a strong drafter would pick, given the cards already in the pool.

Two numbers below: Top-1 is how often the model's first suggestion matches the human's actual pick. Top-3 is how often the human's pick is anywhere in the model's top three.

Held-out trophy results (picks the model never saw during training):

| Set | Top-1 |
|-----|-------|
| FDN | 71.2% |
| TMT | 70.9% |
| ECL | 70.2% |
| EOE | 69.9% |
| TLA | 68.6% |
| FIN | 65.7% |
| SOS | 64.9% |

Aggregate: 68.9% Top-1, 94.5% Top-3 across 128K test picks. These results can be considered state-of-the-art when comparing to any public version of this problem.

The architecture and training code are not in this repo.

## Install / build from source

```bash
git clone https://github.com/negaga53/nemedraft-client.git
cd nemedraft-client
pip install -e ".[client]"
python scripts/build_overlay.py --clean
# → dist/NemeDraft (Linux), dist/NemeDraft.exe (Windows), dist/NemeDraft.app (macOS)
```

## Run from source (no binary)

```bash
pip install -e ".[client]"
python scripts/run_overlay.py
```

## Data

### Bundled grpId → name map

`client/overlay/data/grpid_to_name.json` is a generated snapshot
(≈530 KB, ≈18k entries) of every MTG Arena card's grpId and English
name, sourced from [MTGJSON's `AllIdentifiers.json`](https://mtgjson.com/).

It exists because Scryfall publishes new sets without `arena_id`
populated for weeks, and the per-installation MTGA SQLite fallback only
works on Windows. On macOS / Linux, this bundled file is the only
fallback that resolves grpIds for fresh sets.

To regenerate manually:

```bash
python scripts/refresh_grpid_map.py
```

A weekly GitHub Actions workflow (`.github/workflows/refresh-grpid-map.yml`)
opens a PR with the refresh.

## Pre-built binaries

Releases are published on this repo's [Releases page](https://github.com/negaga53/nemedraft-client/releases). The overlay auto-updates by polling the GitHub releases API on launch.

## Tests

```bash
QT_QPA_PLATFORM=offscreen pytest tests/
```

## Server / privacy

No raw game data is uploaded; only pack contents, pool contents, and pick number for the active draft.

## License

MIT. See [LICENSE](LICENSE).

## Issues / support

File issues at https://github.com/negaga53/nemedraft-client/issues.
