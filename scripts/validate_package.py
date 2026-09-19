"""Offline checks for the standalone Git/plugin package; no third-party modules."""

import json
from pathlib import Path
import re
import sys
import tomllib


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    portable = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    legacy = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert portable["name"] == legacy["name"] == package["name"] == "codex-context-compacto"
    assert portable["version"] == legacy["version"] == package["version"]
    assert "hooks" not in legacy  # Compatibility manifest follows the bundled creator validator.
    assert portable["extensions"]["com.openai"]["hooks"] == "./hooks/hooks.json"
    assert portable["extensions"]["com.openai"]["interface"] == legacy["interface"]
    for value in (portable, legacy):
        assert "[TODO" not in json.dumps(value)
    hooks = json.loads((root / "hooks/hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["PreCompact"][0]["hooks"][0]
    assert handler["type"] == "command" and handler["timeout"] <= 600
    assert "${PLUGIN_ROOT}/hooks/precompact.py" in handler["command"]
    assert handler["commandWindows"].startswith("py -3 ")
    skill = (root / "skills/compacto/SKILL.md").read_text(encoding="utf-8")
    assert re.match(r"---\nname: compacto\ndescription: .+\n---", skill)
    for relative in ("LICENSE", "README.md", "scripts/compacto.py", "hooks/precompact.py",
                     "src/codex_context_compacto/cli.py", ".github/workflows/test.yml"):
        assert (root / relative).is_file(), relative
    print("Package metadata, plugin layout, hook entrypoints, and skill links are valid.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError, KeyError) as exc:
        print(f"Package validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
