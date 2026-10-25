# HactoberFest-Challenges-Week-3  
# 🦥 sloth-sorter

A lazy, fully local file tidier. A small open-weight model (via Ollama) reads each new file in your Downloads folder, picks a category, gives it a sensible name, and files it away. Nothing leaves your machine. Every move is logged so you can undo it.

## Setup (CPU-only is fine)
```bash
# 1. Install Ollama (https://ollama.com), then:
ollama pull qwen2.5:3b          # ~2 GB, runs on CPU. Try qwen2.5:1.5b if it's slow.
# 2. Optional, for PDF text:
pip install pypdf
# 3. Edit config.toml (folders, categories, plain-English rules)
```
Needs Python 3.11+. No other dependencies.

## Use
```bash
python sloth_sorter.py plan              # dry run (default), moves nothing
python sloth_sorter.py apply             # do it
python sloth_sorter.py undo              # reverse the last batch
python sloth_sorter.py watch --interval 600   # tidy every 10 minutes
python sloth_sorter.py bench --models qwen2.5:1.5b,qwen2.5:3b,llama3.2:3b
```

## How it stays safe
- Dry run is the default; `apply` is explicit.
- Skips hidden files, partial downloads (`.crdownload`, `.part`...) and anything modified in the last `settle_seconds`.
- Never overwrites: name collisions get `-2`, `-3`...
- SQLite log of every move; `undo` restores originals.
- If the model returns junk or Ollama hiccups, it falls back to extension-based sorting instead of crashing.

## Change behavior without code
Edit `rules` in `config.toml`, e.g. `"Anything from my thesis goes in documents."`. Swap `model` for any Ollama model.
