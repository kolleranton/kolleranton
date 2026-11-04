"""Сховище родового дерева: data/tree.json (люди, зв'язки, факти, джерела, зачіпки)."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
RESEARCH_FILE = ROOT / "research.yaml"
TREE_FILE = ROOT / "data" / "tree.json"

FAMILY_SOURCE_ID = "family"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def slug(text: str) -> str:
    return re.sub(r"[^\w]+", "_", text.lower()).strip("_")[:60] or "x"


class Tree:
    def __init__(self, data: dict) -> None:
        self.data = data

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path = TREE_FILE) -> "Tree":
        if path.exists():
            tree = cls(json.loads(path.read_text(encoding="utf-8")))
        else:
            tree = cls({"people": {}, "relations": [], "sources": {}, "leads": [], "log": []})
        tree.merge_research()
        return tree

    def save(self, path: Path = TREE_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = _now()
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def merge_research(self, path: Path = RESEARCH_FILE) -> None:
        """Додає людей і зв'язки з research.yaml (не перезаписуючи знайдене агентом)."""
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.data["archive"] = cfg.get("archive", {})
        self.data["questions"] = cfg.get("questions", [])
        self.data["sources"].setdefault(FAMILY_SOURCE_ID, {
            "id": FAMILY_SOURCE_ID, "title": "Родинні відомості", "url": None, "archive_ref": None,
        })
        for p in cfg.get("people", []):
            pid = p["id"]
            person = self.data["people"].setdefault(pid, {"id": pid, "facts": []})
            for key in ("name", "sex", "nationality", "inferred", "note"):
                if key in p and key not in person:
                    person[key] = p[key]
            for key in ("name_variants", "occupations"):
                person.setdefault(key, [])
                for v in p.get(key, []):
                    if v not in person[key]:
                        person[key].append(v)
            for event in ("birth", "death"):
                if event in p and event not in person:
                    person[event] = {**p[event], "source": FAMILY_SOURCE_ID}
        for r in cfg.get("relations", []):
            if r["type"] == "parent":
                self.add_relation("parent", r["parent"], r["child"], FAMILY_SOURCE_ID,
                                  "inferred" if r.get("inferred") else "family")
            else:
                self.add_relation(r["type"], r["a"], r["b"], FAMILY_SOURCE_ID, "family")

    # ------------------------------------------------------------------ mutations
    def add_source(self, title: str, url: str | None = None, archive_ref: str | None = None) -> str:
        for sid, s in self.data["sources"].items():
            if (url and s.get("url") == url) or (s["title"] == title and s.get("archive_ref") == archive_ref):
                if archive_ref and not s.get("archive_ref"):
                    s["archive_ref"] = archive_ref
                return sid
        sid = f"S{len(self.data['sources'])}"
        self.data["sources"][sid] = {"id": sid, "title": title, "url": url, "archive_ref": archive_ref}
        return sid

    def upsert_person(self, pid: str, **fields) -> dict:
        person = self.data["people"].setdefault(pid, {"id": pid, "facts": [], "name_variants": [], "occupations": []})
        for key, value in fields.items():
            if value in (None, "", []):
                continue
            if key in ("name_variants", "occupations"):
                for v in value:
                    if v not in person.setdefault(key, []):
                        person[key].append(v)
            elif key in ("birth", "death"):
                person[key] = {**person.get(key, {}), **{k: v for k, v in value.items() if v}}
            else:
                person[key] = value
        return person

    def add_fact(self, pid: str, fact: dict) -> None:
        person = self.data["people"][pid]
        if not any(f["text"] == fact["text"] for f in person["facts"]):
            person["facts"].append(fact)

    def add_relation(self, rtype: str, a: str, b: str, source: str | None, confidence: str) -> bool:
        for r in self.data["relations"]:
            same = (r["a"], r["b"]) == (a, b) or (rtype != "parent" and (r["a"], r["b"]) == (b, a))
            if r["type"] == rtype and same:
                return False
        self.data["relations"].append({"type": rtype, "a": a, "b": b, "source": source, "confidence": confidence})
        return True

    def add_lead(self, lead: dict) -> None:
        key = (lead.get("archive_ref") or "", lead["text"])
        if not any((l.get("archive_ref") or "", l["text"]) == key for l in self.data["leads"]):
            self.data["leads"].append(lead)

    def log(self, text: str) -> None:
        self.data["log"].append({"time": _now(), "text": text})

    # ------------------------------------------------------------------ views
    def summary(self) -> str:
        """Короткий текстовий стан дерева — для промпта агента."""
        lines = ["ЛЮДИ:"]
        for p in self.data["people"].values():
            bits = [f"[{p['id']}] {p.get('name', '?')}"]
            if p.get("birth"):
                bits.append("нар. " + ", ".join(str(v) for k, v in p["birth"].items() if k in ("date", "place")))
            if p.get("occupations"):
                bits.append("занять: " + "; ".join(p["occupations"]))
            lines.append(" — ".join(bits))
            for f in p["facts"]:
                lines.append(f"    • {f['text']} ({f.get('confidence', '?')}, джерело {f.get('source')})")
        lines.append("ЗВ'ЯЗКИ:")
        for r in self.data["relations"]:
            lines.append(f"  {r['type']}: {r['a']} → {r['b']} ({r['confidence']})")
        if self.data["leads"]:
            lines.append("ЗАЧІПКИ (справи для замовлення):")
            for l in self.data["leads"]:
                lines.append(f"  - {l.get('archive_ref') or ''} {l['text']}")
        return "\n".join(lines)
