"""Plugin hook entrypoint; stdout is reserved for Codex's JSON decision."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_context_compacto.hook import main

if __name__ == "__main__":
    raise SystemExit(main())
