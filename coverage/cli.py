"""Command-line entry point: python main.py [--data DIR] [--out DIR]."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from coverage.errors import DataError
from coverage.pipeline import InputPaths, run_analysis
from coverage.report import build_result, format_summary, write_result

RESULT_FILENAME = "result.json"
MAP_FILENAME = "coverage_map.html"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Карта покрытия района: очистка замеров, сетка 200 м, выбор площадок для новых БС."
    )
    parser.add_argument("--data", type=Path, default=Path("data"),
                        help="папка с measurements.csv, towers.csv, candidates.csv, bounds.json")
    parser.add_argument("--measurements", type=Path, help="путь к measurements.csv (вместо --data)")
    parser.add_argument("--towers", type=Path, help="путь к towers.csv (вместо --data)")
    parser.add_argument("--candidates", type=Path, help="путь к candidates.csv (вместо --data)")
    parser.add_argument("--bounds", type=Path, help="путь к bounds.json (вместо --data)")
    parser.add_argument("--out", type=Path, default=Path("output"),
                        help="папка для result.json и coverage_map.html")
    parser.add_argument("--no-map", action="store_true", help="не строить HTML-карту")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return parser


def resolve_paths(args: argparse.Namespace) -> InputPaths:
    defaults = InputPaths.from_dir(args.data)
    return InputPaths(
        measurements=args.measurements or defaults.measurements,
        towers=args.towers or defaults.towers,
        candidates=args.candidates or defaults.candidates,
        bounds=args.bounds or defaults.bounds,
    )


def main(argv: list[str] | None = None) -> int:
    # Always emit UTF-8 so Cyrillic survives pipes and non-UTF-8 Windows code pages.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    try:
        analysis = run_analysis(resolve_paths(args))
    except DataError as exc:
        print(f"Ошибка входных данных: {exc}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    result_path = args.out / RESULT_FILENAME
    write_result(build_result(analysis), result_path)

    print(format_summary(analysis))
    print()
    print(f"result.json: {result_path}")

    if not args.no_map:
        # Imported lazily so result.json is produced even if folium is unavailable.
        from coverage.mapviz import render_map

        map_path = args.out / MAP_FILENAME
        render_map(analysis, map_path)
        print(f"Карта: {map_path}")
    return 0
