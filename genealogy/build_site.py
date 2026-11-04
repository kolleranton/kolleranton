"""Генерує статичний сайт родового дерева з data/tree.json → site/index.html.

    python genealogy/build_site.py            # створить site/index.html
    python genealogy/build_site.py --out docs # інша папка
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree import ROOT, Tree  # noqa: E402

TEMPLATE = (ROOT / "site_template.html").read_text(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "site"))
    args = ap.parse_args()

    tree = Tree.load()
    report = ROOT / "data" / "report.md"
    payload = dict(tree.data)
    payload["report"] = report.read_text(encoding="utf-8") if report.exists() else ""
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(TEMPLATE.replace("/*__DATA__*/null", data), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Сайт: {out / 'index.html'}  (людей: {len(tree.data['people'])}, "
          f"зв'язків: {len(tree.data['relations'])}, справ: {len(tree.data['leads'])})")


if __name__ == "__main__":
    main()
