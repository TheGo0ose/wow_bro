"""result.json and the console summary."""

from __future__ import annotations

import json
from pathlib import Path

from coverage.cleaning import DropReason
from coverage.grid import Level
from coverage.pipeline import Analysis

DROP_REASON_LABELS = {
    DropReason.EMPTY_ROW: "пустые строки",
    DropReason.WRONG_COLUMN_COUNT: "неверное число колонок",
    DropReason.MISSING_VALUE: "пропущенные значения",
    DropReason.NOT_A_NUMBER: "нечисловые значения / NaN / Infinity",
    DropReason.BAD_TIMESTAMP: "неверное время",
    DropReason.RSRP_OUT_OF_RANGE: "RSRP вне -140...-44 дБм",
    DropReason.OUT_OF_BOUNDS: "координаты вне района",
    DropReason.DUPLICATE: "дубли",
}


def build_result(analysis: Analysis) -> dict:
    return {
        "worst_cells": [
            {"cell_id": cell.cell_id, "rsrp_median": cell.median_dbm} for cell in analysis.worst
        ],
        "best_candidates": list(analysis.placement.site_ids),
        "dropped_rows": analysis.cleaning.dropped_rows,
        "covered_weak_cells": analysis.placement.covered_count,
    }


def write_result(result: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def format_summary(analysis: Analysis) -> str:
    cleaning = analysis.cleaning
    grid = analysis.grid
    placement = analysis.placement
    levels = grid.level_counts()

    lines = [
        f"Замеров в файле: {cleaning.total_rows}",
        f"Отброшено строк: {cleaning.dropped_rows}",
    ]
    for reason in DropReason:
        if cleaning.dropped[reason]:
            lines.append(f"  - {DROP_REASON_LABELS[reason]}: {cleaning.dropped[reason]}")
    lines += [
        f"Осталось замеров: {len(cleaning.measurements)}",
        "",
        f"Клеток с замерами: {len(grid.cells) + len(grid.sparse_cells)}, "
        f"из них меньше 5 замеров (не учитываются): {len(grid.sparse_cells)}",
        f"Зелёных: {levels[Level.GOOD]}, жёлтых: {levels[Level.FAIR]}, "
        f"красных (слабых): {levels[Level.WEAK]}",
        "Худшие клетки:",
    ]
    for rank, cell in enumerate(analysis.worst, start=1):
        lines.append(f"  {rank}. {cell.cell_id}: {cell.median_dbm:g} дБм ({cell.samples} замеров)")

    rejected = ", ".join(
        f"{cid} ({dist:.0f} м)" for cid, dist in sorted(analysis.eligibility.rejected.items())
    )
    lines += [
        "",
        f"Площадок-кандидатов: {len(analysis.candidates)}, "
        f"допустимых: {len(analysis.eligibility.allowed)}",
        f"Отклонены (ближе 500 м к БС): {rejected or 'нет'}",
        f"Проверено пар: {placement.pairs_checked}",
        f"Лучшая пара: {' + '.join(placement.site_ids) or 'нет'} -> "
        f"покрывает {placement.covered_count} из {levels[Level.WEAK]} красных клеток",
    ]
    return "\n".join(lines)
