"""Eval — замер точности локализатора F1 (golden-set + IoU).

Идея: набор эталонных кейсов «(video_id, запрос) → ожидаемый (start,end)».
Гоняем retrieval.locate по каждому, меряем IoU таймкодов (пересечение/объединение).
Регресс-гейт: поменял промпт/логику → прогнал → видно, лучше стало или сломалось.

Эталоны (golden.json) должны быть ВЕРИФИЦИРОВАНЫ человеком (посмотреть ролик),
а не взяты из вывода модели — иначе меряем стабильность, а не правоту. Сид ниже
помечен note='baseline, verify' — это стартовая привязка, её надо выверить глазами.

Мета берётся из кэша (cache/{video_id}.meta.json) — прогон оффлайн, без Mac.
Запуск: python -m goldcut.eval
"""

from __future__ import annotations

import json
from pathlib import Path

from goldcut.config import Config
from goldcut.models import Request, VideoMeta
from goldcut.retrieval import locate
from goldcut.transcript import mmss

GOLDEN = Path(__file__).parent / "golden.json"


def iou(s1: float, e1: float, s2: float, e2: float) -> float:
    """IoU двух временных интервалов (0..1)."""
    inter = max(0.0, min(e1, e2) - max(s1, s2))
    union = (e1 - s1) + (e2 - s2) - inter
    return inter / union if union > 0 else 0.0


def _load_meta(video_id: str, cache_dir: Path) -> VideoMeta | None:
    p = cache_dir / f"{video_id}.meta.json"
    if not p.exists():
        return None
    return VideoMeta.model_validate_json(p.read_text(encoding="utf-8"))


def run(
    golden_path: Path = GOLDEN,
    cache_dir: str | Path = "/root/goldcut-dev/cache",
    *,
    hit_threshold: float = 0.5,
    cfg: Config | None = None,
) -> dict:
    cfg = cfg or Config.from_env("/root/goldcut-dev/.env")
    cache_dir = Path(cache_dir)
    cases = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    rows, skipped = [], []
    for c in cases:
        meta = _load_meta(c["video_id"], cache_dir)
        if not meta:
            skipped.append(c["video_id"])
            continue
        req = Request(mode="locate", topic=c.get("topic"), time_anchor_s=c.get("time_anchor_s"))
        cands = locate(meta, req, cfg=cfg)
        if not cands:
            rows.append({**c, "got": None, "iou": 0.0})
            continue
        g = cands[0]
        score = iou(c["expected_start_s"], c["expected_end_s"], g.start_s, g.end_s)
        rows.append({**c, "got_start": g.start_s, "got_end": g.end_s,
                     "conf": g.confidence, "iou": round(score, 3)})
    scored = [r for r in rows if "iou" in r]
    mean_iou = round(sum(r["iou"] for r in scored) / len(scored), 3) if scored else 0.0
    hit_rate = round(sum(r["iou"] >= hit_threshold for r in scored) / len(scored), 3) if scored else 0.0
    return {"rows": rows, "mean_iou": mean_iou, "hit_rate": hit_rate,
            "n": len(scored), "skipped": skipped}


def main() -> None:
    res = run()
    print(f"=== goldcut eval: {res['n']} кейсов ===")
    for r in res["rows"]:
        exp = f"{mmss(r['expected_start_s'])}–{mmss(r['expected_end_s'])}"
        if r.get("got_start") is not None:
            got = f"{mmss(r['got_start'])}–{mmss(r['got_end'])} conf={r.get('conf')}"
        else:
            got = "НЕ НАЙДЕНО"
        mark = "✅" if r["iou"] >= 0.5 else "⚠️" if r["iou"] > 0 else "❌"
        print(f"{mark} IoU={r['iou']:.2f}  «{(r.get('topic') or '')[:40]}»\n     ожид {exp} | got {got}")
    if res["skipped"]:
        print(f"\nпропущено (нет кэша meta): {res['skipped']}")
    print(f"\nСРЕДНИЙ IoU = {res['mean_iou']}  |  hit-rate@0.5 = {res['hit_rate']}  (n={res['n']})")


if __name__ == "__main__":
    main()
