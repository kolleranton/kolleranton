"""Мінімальний клієнт MediaWiki API для uk.wikisource.org (розділ «Архів:ДАОО»)."""

from __future__ import annotations

import re
import time

import requests

API_URL = "https://uk.wikisource.org/w/api.php"
SITE_URL = "https://uk.wikisource.org/wiki/"
# Wikimedia вимагає описовий User-Agent для ботів.
USER_AGENT = "kolleranton-genealogy-agent/1.0 (https://github.com/kolleranton/kolleranton)"


class WikiSource:
    def __init__(self, api_url: str = API_URL, delay: float = 0.5) -> None:
        self.api_url = api_url
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._namespaces: dict[str, int] | None = None
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------ low level
    def _get(self, **params) -> dict:
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        for attempt in range(4):
            try:
                r = self.session.get(self.api_url, params=params, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                time.sleep(self.delay)
                data = r.json()
                if "error" in data:
                    raise RuntimeError(data["error"].get("info", str(data["error"])))
                return data
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError("unreachable")

    @staticmethod
    def url(title: str) -> str:
        return SITE_URL + title.replace(" ", "_")

    def namespaces(self) -> dict[str, int]:
        if self._namespaces is None:
            data = self._get(action="query", meta="siteinfo", siprop="namespaces|namespacealiases")
            ns: dict[str, int] = {}
            for item in data["query"]["namespaces"].values():
                if item.get("name"):
                    ns[item["name"]] = item["id"]
                if item.get("canonical"):
                    ns[item["canonical"]] = item["id"]
            for alias in data["query"].get("namespacealiases", []):
                ns[alias["alias"]] = alias["id"]
            self._namespaces = ns
        return self._namespaces

    def split_title(self, title: str) -> tuple[int, str]:
        if ":" in title:
            prefix, rest = title.split(":", 1)
            ns = self.namespaces().get(prefix)
            if ns is not None:
                return ns, rest
        return 0, title

    # ------------------------------------------------------------------ public API
    def search(self, query: str, prefix: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """Повнотекстовий пошук (CirrusSearch). prefix обмежує пошук підсторінками."""
        q = f"{query} prefix:{prefix}" if prefix else query
        data = self._get(
            action="query", list="search", srsearch=q, srnamespace="*",
            srlimit=min(limit, 50), sroffset=offset, srprop="snippet|size|titlesnippet",
        )
        hits = []
        for h in data["query"]["search"]:
            snippet = re.sub(r"<[^>]+>", "", h.get("snippet", ""))
            hits.append({"title": h["title"], "url": self.url(h["title"]), "size": h.get("size"), "snippet": snippet})
        total = data["query"].get("searchinfo", {}).get("totalhits")
        return {"total": total, "hits": hits, "next_offset": data.get("continue", {}).get("sroffset")}

    def wikitext(self, title: str) -> str:
        if title not in self._cache:
            data = self._get(
                action="query", prop="revisions", titles=title, rvprop="content",
                rvslots="main", redirects=1,
            )
            page = data["query"]["pages"][0]
            if page.get("missing"):
                raise KeyError(f"Сторінку «{title}» не знайдено")
            self._cache[title] = page["revisions"][0]["slots"]["main"]["content"]
        return self._cache[title]

    def read(self, title: str, offset: int = 0, max_chars: int = 12000) -> dict:
        text = self.wikitext(title)
        chunk = text[offset: offset + max_chars]
        nxt = offset + max_chars if offset + max_chars < len(text) else None
        return {"title": title, "url": self.url(title), "length": len(text), "offset": offset,
                "next_offset": nxt, "text": chunk}

    def grep(self, title: str, pattern: str, context: int = 1, max_hits: int = 60) -> dict:
        """Рядки сторінки, що відповідають регулярному виразу (без урахування регістру)."""
        lines = self.wikitext(title).splitlines()
        rx = re.compile(pattern, re.IGNORECASE)
        hits = []
        for i, line in enumerate(lines):
            if rx.search(line):
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                hits.append({"line": i + 1, "text": "\n".join(lines[lo:hi])})
                if len(hits) >= max_hits:
                    break
        return {"title": title, "url": self.url(title), "pattern": pattern, "hits": hits}

    def subpages(self, prefix: str, limit: int = 200, cont: str | None = None) -> dict:
        ns, rest = self.split_title(prefix)
        params = dict(action="query", list="allpages", apnamespace=ns, apprefix=rest, aplimit=min(limit, 500))
        if cont:
            params["apcontinue"] = cont
        data = self._get(**params)
        titles = [p["title"] for p in data["query"]["allpages"]]
        return {"prefix": prefix, "titles": titles, "continue": data.get("continue", {}).get("apcontinue")}
