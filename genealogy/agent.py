"""Агент-генеалог: шукає родичів у розділі «Архів:ДАОО» на uk.wikisource.org
і записує знахідки в data/tree.json.

Запуск:
    export ANTHROPIC_API_KEY=...
    python genealogy/agent.py                 # повний пошук
    python genealogy/agent.py --focus "Зоя Русіна"   # зосередитись на питанні
    python genealogy/agent.py --max-steps 30
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path

import anthropic
from anthropic import beta_tool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree import ROOT, Tree, slug  # noqa: E402
from wikisource import WikiSource  # noqa: E402

MODEL = "claude-opus-5-5"
REPORT_FILE = ROOT / "data" / "report.md"
MAX_TOOL_CHARS = 15000

SYSTEM_PROMPT = """\
Ти — досвідчений архівіст-генеалог, який працює з фондами Державного архіву Одеської області (ДАОО).
Твоє джерело — розділ «Архів:ДАОО» на українській Вікіджерелах (uk.wikisource.org): там опубліковані
переліки фондів, описи та назви справ (іноді з прізвищами — особові справи, списки працівників,
актові записи, метричні книги тощо).

Мета: знайти відомості про родину користувача і зібрати їх у родове дерево, а також скласти список
конкретних справ (фонд / опис / справа), які варто замовити в читальному залі архіву чи онлайн.

Як працювати:
- Починай з кореневої сторінки «Архів:ДАОО» і переліку підсторінок, щоб зрозуміти, як організовані фонди.
- Шукай за прізвищами в усіх варіантах написання (укр./рос./нім.: Коллер, Колер, Коллеръ, Koller, Köhler,
  Келер; Русіна, Русина, Русин, Русинъ) та за установами: електростанції Одеси й області, енергетичні
  трести, організації, де працювали креслярі; органи РАЦС/ЗАГС; парафії (лютеранські, католицькі,
  православні), метричні книги 1880–1910-х; списки німців, виборчі списки, перепис, справи репресованих.
- Великі описи не читай цілком — використовуй find_in_page з регулярним виразом.
- Записуй ЛИШЕ те, що підтверджено текстом сторінки, з посиланням на неї та шифром справи. Якщо збіг
  можливий, але не певний (поширене прізвище, інші ініціали) — став confidence "possible" і поясни чому.
- Шифр справи пиши у форматі «ДАОО, ф. Р-1234, оп. 1, спр. 56» (як на сторінці).
- Особова справа чи документ, який сам не опублікований, але його назва вказує на родича, — це lead
  (зачіпка): запиши через record_lead, щоб користувач міг її замовити.
- Нових родичів (батьків, братів/сестер, дітей, інших подружжів) додавай через record_person та
  record_relation, лише якщо джерело на це вказує.
- Коли пошук вичерпано, напиши підсумковий звіт українською: що знайдено, що ні, які справи замовити
  і з якими запитами варто звернутися до архіву.
"""


def clip(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + "\n…[обрізано — звузь запит або використай offset]"
    return text


def build_tools(wiki: WikiSource, tree: Tree):
    def safe(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # повертаємо помилку моделі, а не падаємо
                return f"Помилка: {type(e).__name__}: {e}"
        return wrapper

    @beta_tool
    @safe
    def search_archive(query: str, prefix: str = "Архів:ДАОО", offset: int = 0) -> str:
        """Повнотекстовий пошук на uk.wikisource.org.

        Args:
            query: Пошуковий запит (прізвище, установа, тип документа). Підтримує "лапки" та OR.
            prefix: Обмежити пошук сторінками з цим префіксом. Порожній рядок — шукати по всіх Вікіджерелах.
            offset: Зсув для наступної сторінки результатів.
        """
        return clip(wiki.search(query, prefix=prefix or None, offset=offset))

    @beta_tool
    @safe
    def list_subpages(prefix: str, continue_from: str = "") -> str:
        """Перелік сторінок, назви яких починаються з prefix (наприклад «Архів:ДАОО/»).

        Args:
            prefix: Префікс назви сторінки з простором назв.
            continue_from: Значення continue з попередньої відповіді для продовження списку.
        """
        return clip(wiki.subpages(prefix, cont=continue_from or None))

    @beta_tool
    @safe
    def read_page(title: str, offset: int = 0) -> str:
        """Прочитати вікітекст сторінки частинами (до 12000 символів).

        Args:
            title: Точна назва сторінки.
            offset: Позиція в тексті, з якої читати (next_offset з попередньої відповіді).
        """
        return clip(wiki.read(title, offset=offset))

    @beta_tool
    @safe
    def find_in_page(title: str, pattern: str) -> str:
        """Знайти рядки сторінки за регулярним виразом (без урахування регістру), з сусідніми рядками.

        Args:
            title: Точна назва сторінки.
            pattern: Регулярний вираз, наприклад "Кол+ер|Koller|Русин".
        """
        return clip(wiki.grep(title, pattern))

    @beta_tool
    @safe
    def record_person(person_id: str, name: str, sex: str = "", birth_date: str = "", birth_place: str = "",
                      death_date: str = "", death_place: str = "", name_variants: list[str] | None = None,
                      occupations: list[str] | None = None, nationality: str = "") -> str:
        """Додати нову людину або доповнити наявну (за person_id). Існуючі id видно в стані дерева.

        Args:
            person_id: Латинський ідентифікатор, напр. "ernest_koller". Для нової людини — новий id.
            name: ПІБ українською.
            sex: "M" або "F".
            birth_date: Дата/рік народження як у джерелі.
            birth_place: Місце народження.
            death_date: Дата смерті.
            death_place: Місце смерті.
            name_variants: Інші написання імені з джерел.
            occupations: Посади / місця роботи.
            nationality: Національність, якщо вказана.
        """
        pid = slug(person_id)
        tree.upsert_person(
            pid, name=name, sex=sex or None, nationality=nationality or None,
            birth={"date": birth_date, "place": birth_place} if (birth_date or birth_place) else None,
            death={"date": death_date, "place": death_place} if (death_date or death_place) else None,
            name_variants=name_variants or [], occupations=occupations or [],
        )
        tree.save()
        return f"OK: {pid}"

    @beta_tool
    @safe
    def record_fact(person_id: str, text: str, source_title: str, source_url: str = "", archive_ref: str = "",
                    date: str = "", place: str = "", confidence: str = "confirmed") -> str:
        """Записати факт про людину з посиланням на джерело.

        Args:
            person_id: Ідентифікатор людини з дерева.
            text: Факт українською, коротко (напр. "1931 — кресляр Одеської ЦЕС").
            source_title: Назва сторінки Вікіджерел або документа.
            source_url: URL сторінки.
            archive_ref: Шифр: ДАОО, ф. …, оп. …, спр. …
            date: Дата/рік події.
            place: Місце події.
            confidence: "confirmed" (явно про цю людину) або "possible" (ймовірний збіг).
        """
        pid = slug(person_id)
        if pid not in tree.data["people"]:
            return f"Помилка: немає людини {pid}; спершу record_person"
        sid = tree.add_source(source_title, source_url or None, archive_ref or None)
        tree.add_fact(pid, {"text": text, "date": date or None, "place": place or None,
                            "source": sid, "confidence": confidence})
        tree.save()
        return f"OK: факт додано ({sid})"

    @beta_tool
    @safe
    def record_relation(relation: str, person_a: str, person_b: str, source_title: str, source_url: str = "",
                        archive_ref: str = "", confidence: str = "confirmed") -> str:
        """Записати родинний зв'язок.

        Args:
            relation: "parent" (person_a — батько/мати person_b), "spouse" або "sibling".
            person_a: id першої людини.
            person_b: id другої людини.
            source_title: Джерело.
            source_url: URL джерела.
            archive_ref: Шифр справи.
            confidence: "confirmed" або "possible".
        """
        a, b = slug(person_a), slug(person_b)
        if relation not in ("parent", "spouse", "sibling"):
            return "Помилка: relation має бути parent, spouse або sibling"
        missing = [x for x in (a, b) if x not in tree.data["people"]]
        if missing:
            return f"Помилка: немає людей {missing}; спершу record_person"
        sid = tree.add_source(source_title, source_url or None, archive_ref or None)
        added = tree.add_relation(relation, a, b, sid, confidence)
        tree.save()
        return "OK" if added else "Вже є"

    @beta_tool
    @safe
    def record_lead(text: str, archive_ref: str, person_ids: list[str], source_url: str = "",
                    why: str = "") -> str:
        """Записати справу, яку варто замовити в архіві (особова справа, актовий запис тощо).

        Args:
            text: Назва справи як в описі.
            archive_ref: Шифр: ДАОО, ф. …, оп. …, спр. …
            person_ids: id людей, яких вона стосується.
            source_url: URL сторінки опису.
            why: Чому ця справа може містити відомості.
        """
        tree.add_lead({"text": text, "archive_ref": archive_ref, "person_ids": [slug(p) for p in person_ids],
                       "url": source_url or None, "why": why or None})
        tree.save()
        return "OK"

    @beta_tool
    def show_tree() -> str:
        """Показати поточний стан дерева (люди, факти, зв'язки, зачіпки)."""
        return tree.summary()

    return [search_archive, list_subpages, read_page, find_in_page,
            record_person, record_fact, record_relation, record_lead, show_tree]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--focus", default="", help="Додаткове завдання / питання для цього запуску")
    ap.add_argument("--max-steps", type=int, default=80, help="Максимум кроків агента (викликів моделі)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = ap.parse_args()

    tree = Tree.load()
    tree.save()
    wiki = WikiSource()
    client = anthropic.Anthropic()

    questions = "\n".join(f"- {q}" for q in tree.data.get("questions", []))
    task = (
        f"Поточний стан родового дерева:\n{tree.summary()}\n\n"
        f"Питання дослідження:\n{questions}\n"
        + (f"\nОсобливий фокус цього запуску: {args.focus}\n" if args.focus else "")
        + "\nПочни пошук в «Архів:ДАОО». Записуй знахідки інструментами record_*. "
          "Наприкінці дай підсумковий звіт."
    )

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        max_iterations=args.max_steps,
        system=SYSTEM_PROMPT,
        tools=build_tools(wiki, tree),
        messages=[{"role": "user", "content": task}],
        thinking={"type": "adaptive"},
        output_config={"effort": args.effort},
        # Прибирає старі результати інструментів, щоб довгий пошук не переповнював контекст.
        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        # Якщо запит відхилено фільтром безпеки — сервер повторить його на резервній моделі.
        fallbacks="default",
        betas=["context-management-2025-06-27", "server-side-fallback-2026-07-01"],
    )

    final_text = ""
    last = None
    for message in runner:
        last = message
        for block in message.content:
            if block.type == "text" and block.text.strip():
                print(block.text.strip(), flush=True)
                final_text = block.text.strip()
            elif block.type == "tool_use":
                print(f"  → {block.name}({json.dumps(block.input, ensure_ascii=False)[:200]})", flush=True)

    if last is not None and last.stop_reason == "refusal":
        print("Модель відмовилась продовжувати (refusal).", file=sys.stderr)
    if last is not None and last.stop_reason == "tool_use":
        final_text += "\n\n_(Досягнуто ліміту кроків — запустіть агента ще раз, щоб продовжити.)_"

    if final_text:
        REPORT_FILE.write_text(f"# Звіт агента ({tree.data.get('updated', '')})\n\n{final_text}\n", encoding="utf-8")
    tree.log(f"Запуск агента завершено: {last.stop_reason if last else 'no output'}"
             + (f"; фокус: {args.focus}" if args.focus else ""))
    tree.save()
    print(f"\nЗбережено: {Path('genealogy/data/tree.json')}, {REPORT_FILE.relative_to(ROOT.parent)}")


if __name__ == "__main__":
    main()
