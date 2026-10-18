"""Regenerate site/data/*.json and site/img/* from reports/ and docs/.

The static site under site/ is published on Vercel. Every number it shows comes
from the files this script reads; nothing is typed into the page by hand.
Run from the repository root:  python3 scripts/build_site.py
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DOCS = ROOT / "docs"
SITE = ROOT / "site"
DATA = SITE / "data"
IMG = SITE / "img"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def num(s: str) -> float | int | str:
    if s is None or s == "":
        return ""
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return s


def typed(rows: list[dict[str, str]]) -> list[dict]:
    return [{k: num(v) for k, v in r.items()} for r in rows]


# ----------------------------------------------------------------------------
# A small Markdown-to-HTML converter: headings, paragraphs, lists, tables,
# fenced code, inline code, bold, italics, links. Enough for docs/memo.md.
# ----------------------------------------------------------------------------

def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            if len(para) > 1 and all(re.match(r"^[A-Z][a-z]+: ", s) for s in para):
                out.append("<p>" + "<br>".join(inline(s.strip()) for s in para) + "</p>")
            else:
                out.append("<p>" + inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_para()
            j = i + 1
            code: list[str] = []
            while j < len(lines) and not lines[j].startswith("```"):
                code.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1]):
            flush_para()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            j = i + 2
            body: list[list[str]] = []
            while j < len(lines) and lines[j].startswith("|"):
                body.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            out.append('<div class="tablewrap"><table><thead><tr>' + "".join(f"<th>{inline(h)}</th>" for h in header) + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table></div>")
            i = j
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            flush_para()
            ordered = m.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            j = i
            while j < len(lines):
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[j])
                if mm:
                    items.append(mm.group(3))
                    j += 1
                elif lines[j].startswith("  ") and items:
                    items[-1] += " " + lines[j].strip()
                    j += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{inline(it)}</li>" for it in items) + f"</{tag}>")
            i = j
            continue
        if line.startswith("!["):
            i += 1
            continue
        if not line.strip():
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out)


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def readme_intro() -> str:
    text = (ROOT / "README.md").read_text()
    body = text.split("\n", 1)[1].strip()
    first = body.split("\n\n", 1)[0]
    return " ".join(s.strip() for s in first.splitlines())


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)

    metrics = json.loads((REPORTS / "failure_model_metrics.json").read_text())
    parts_summary = json.loads((REPORTS / "parts_forecast_summary.json").read_text())
    repeat_summary = json.loads((REPORTS / "repeat_dispatch_summary.json").read_text())
    deflection = json.loads((REPORTS / "case_deflection_metrics.json").read_text())

    curve = typed(read_csv(REPORTS / "dispatch_budget_curve.csv"))
    budget = typed(read_csv(REPORTS / "dispatch_budget.csv"))
    reliability = typed(read_csv(REPORTS / "reliability.csv"))
    parts_backtest = typed(read_csv(REPORTS / "parts_forecast_backtest.csv"))
    repeat = typed(read_csv(REPORTS / "repeat_dispatch.csv"))
    pr_table = typed(read_csv(REPORTS / "case_deflection_pr_table.csv"))

    models = ["heuristic", "logistic", "lightgbm"]
    curve_by_model = {
        m: [
            {k: r[k] for k in ("daily_budget", "caught_per_day", "wasted_per_day", "recall", "precision")}
            for r in curve
            if r["model"] == m and (r["daily_budget"] <= 100 or r["daily_budget"] % 10 == 0)
        ]
        for m in models
    }

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "intro": readme_intro(),
        "failure_model": {
            "config": {
                "horizon_days": metrics["config"]["horizon_days"],
                "train_prediction_dates": metrics["config"]["train_prediction_dates"],
                "train_labels_through": metrics["config"]["train_labels_through"],
                "test_prediction_dates": metrics["config"]["test_prediction_dates"],
                "test_labels_through": metrics["config"]["test_labels_through"],
            },
            "counts": metrics["counts"],
            "test": metrics["test"],
            "feature_importance": metrics["lightgbm_feature_importance_gain"],
            "fleet_scoring": metrics.get("fleet_scoring", {}),
            "runtime_seconds": metrics.get("runtime_seconds"),
        },
        "dispatch_curve": curve_by_model,
        "dispatch_budget": budget,
        "reliability": reliability,
        "parts": {
            "summary": parts_summary,
            "region_part": [r for r in parts_backtest if r["level"] == "region_part" and r["key"] == "all"],
            "by_part": [r for r in parts_backtest if r["level"] == "by_part"],
        },
        "repeat": {
            "summary": repeat_summary,
            "by_category": [r for r in repeat if r["dimension"] == "case_category"],
            "by_family": [r for r in repeat if r["dimension"] == "product_family"],
            "by_region": [r for r in repeat if r["dimension"] == "region"],
        },
        "deflection": {
            "cases": deflection["cases"],
            "runbooks": deflection["runbooks"],
            "split": deflection["split"],
            "deflectable_in_truth": deflection["deflectable_in_truth"],
            "variants": {k: v["threshold_on_tune"] for k, v in deflection["variants"].items()},
            "runbook_hit_rate": deflection["runbook_hit_rate_report_half"],
            "by_category": deflection["by_category_report_half"],
            "pr_table": pr_table,
            "caveat": deflection["caveat"],
        },
    }
    (DATA / "site.json").write_text(json.dumps(out, separators=(",", ":")))

    memo_html = md_to_html((DOCS / "memo.md").read_text())
    (DATA / "memo.html").write_text(memo_html)

    for stale in IMG.glob("*.png"):
        stale.unlink()
    shutil.copyfile(REPORTS / "dashboard.png", IMG / "dashboard.png")

    print(f"wrote {DATA / 'site.json'} ({(DATA / 'site.json').stat().st_size} bytes), memo.html, {len(list(IMG.iterdir()))} images")


if __name__ == "__main__":
    main()
