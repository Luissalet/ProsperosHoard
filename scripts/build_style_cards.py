"""Rebuild the packaged style library (prosperos_hoard/style_library/*.json) from checkouts of the two source libraries.

    python scripts/build_style_cards.py --lemo <checkout of lemo-opuscar> --mg <checkout of mg-styles-15>

Only text is copied (style texts and prompts). Demo films, stills and posters are not read. The commits recorded in
``prosperos_hoard/style_cards.py`` (SOURCES) must match the checkouts; the script says so when they do not.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prosperos_hoard import style_cards as sc  # noqa: E402


def head(folder: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def write(source: str, cards: list, common: dict | None = None) -> None:
    path = sc.LIBRARY_DIR / f"{source}.json"
    data = {"source": source, "cards": cards}
    if common:
        data["common"] = common
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{path.name}: {len(cards)} cards, {path.stat().st_size // 1024} KB")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lemo", type=Path)
    parser.add_argument("--mg", type=Path)
    args = parser.parse_args()
    sc.LIBRARY_DIR.mkdir(exist_ok=True)
    if args.lemo:
        if head(args.lemo) and head(args.lemo) != sc.SOURCES["lemo-opuscar"]["commit"]:
            print("warning: the checkout is at", head(args.lemo), "but SOURCES records", sc.SOURCES["lemo-opuscar"]["commit"])
        write("lemo-opuscar", sc.parse_lemo_folder(args.lemo))
    if args.mg:
        if head(args.mg) and head(args.mg) != sc.SOURCES["mg-styles-15"]["commit"]:
            print("warning: the checkout is at", head(args.mg), "but SOURCES records", sc.SOURCES["mg-styles-15"]["commit"])
        cards, common = sc.parse_mg_folder(args.mg)
        write("mg-styles-15", cards, common)


if __name__ == "__main__":
    main()
