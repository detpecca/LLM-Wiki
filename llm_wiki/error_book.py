"""Error Book (paper §3.3): persistent self-correction store.

Persisted as error_book.yaml. Each entry:
  id, type, phenomenon, root_cause, constraint_rule, verify_method,
  status (open|closed), first_seen, last_seen, occurrences

Five-stage lifecycle: Discover -> Attribute -> Constrain -> Inject ->
Verify & Close. Discover/Inject/Verify are code; Attribute/Constrain use
the LLM (see attribute_and_constrain).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import yaml

from .validators import WikiError

ATTRIBUTE_PROMPT = """A Wiki compilation system produced these systematic errors:

{errors}

For EACH error, give:
1. root_cause: why the LLM likely produced it (one sentence)
2. constraint_rule: a natural-language rule to add to the compilation prompt
   so the error does not recur (imperative, e.g. "NEVER create a link to a
   page not present in _index.md")

Reply strictly as YAML list:
- id: {first_id}
  root_cause: ...
  constraint_rule: ...
- id: ..."""


class ErrorBook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[dict] = []
        self._ids = itertools.count(1)
        self.load()

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        if self.path.is_file():
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                data = None
            if isinstance(data, dict):
                self.entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
            max_id = max((e.get("id", 0) for e in self.entries), default=0)
            self._ids = itertools.count(max_id + 1)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump({"entries": self.entries}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ----------------------------------------------------------- Discover (1)
    def discover(self, errors: list[WikiError], today: str) -> list[dict]:
        """Record detected errors; merge recurrences of the same pattern."""
        new_entries = []
        for err in errors:
            existing = self._find_open(err.type, err.page)
            if existing:
                existing["occurrences"] = existing.get("occurrences", 1) + 1
                existing["last_seen"] = today
                existing["phenomenon"] = err.detail
            else:
                entry = {
                    "id": next(self._ids),
                    "type": err.type,
                    "page": err.page,
                    "phenomenon": err.detail,
                    "root_cause": "",
                    "constraint_rule": "",
                    "verify_method": "re-run structural validation on affected page",
                    "status": "open",
                    "first_seen": today,
                    "last_seen": today,
                    "occurrences": 1,
                }
                self.entries.append(entry)
                new_entries.append(entry)
        self.save()
        return new_entries

    def _find_open(self, type_: str, page: str) -> dict | None:
        for e in self.entries:
            if e["status"] == "open" and e["type"] == type_ and e["page"] == page:
                return e
        return None

    # ------------------------------------------- Attribute (2) + Constrain (3)
    def attribute_and_constrain(self, llm, entries: list[dict]) -> None:
        """Use the LLM to fill root_cause and constraint_rule for new entries."""
        pending = [e for e in entries if not e.get("constraint_rule")]
        if not pending:
            return
        err_text = "\n".join(f"- id: {e['id']} [{e['type']}] {e['page']}: {e['phenomenon']}" for e in pending)
        reply = llm.chat([{"role": "user", "content": ATTRIBUTE_PROMPT.format(
            errors=err_text, first_id=pending[0]["id"])}])
        try:
            parsed = yaml.safe_load(reply[reply.find("-"):]) or []
        except yaml.YAMLError:
            parsed = []
        by_id = {e["id"]: e for e in pending}
        for item in parsed:
            if isinstance(item, dict) and item.get("id") in by_id:
                e = by_id[item["id"]]
                e["root_cause"] = str(item.get("root_cause", ""))
                e["constraint_rule"] = str(item.get("constraint_rule", ""))
        for e in pending:  # fallback so injection never breaks
            if not e["constraint_rule"]:
                e["constraint_rule"] = f"Avoid {e['type']}: {e['phenomenon']}"
        self.save()

    # --------------------------------------------------------------- Inject (4)
    def active_constraints(self) -> list[str]:
        """ActiveConstraints(B): all open constraint rules, for prompt injection."""
        return [e["constraint_rule"] for e in self.entries
                if e.get("status") == "open" and e.get("constraint_rule")]

    # -------------------------------------------------------- Verify & Close (5)
    def verify_and_close(self, still_failing: list[WikiError]) -> list[dict]:
        """Close entries whose error no longer appears after re-validation."""
        failing = {(e.type, e.page) for e in still_failing}
        closed = []
        for e in self.entries:
            if e["status"] == "open" and (e["type"], e["page"]) not in failing:
                # only close entries that have been through at least one
                # constraint-injection cycle
                if e.get("constraint_rule"):
                    e["status"] = "closed"
                    closed.append(e)
        if closed:
            self.save()
        return closed

    def open_entries(self) -> list[dict]:
        return [e for e in self.entries if e.get("status") == "open"]

    def close_for_pages(self, pages: list[str]) -> list[dict]:
        """Close open entries located on pages that no longer exist (e.g.
        removed by document deletion). Code-only, no LLM, no re-verification:
        the page is gone, so the error can no longer be meaningfully open."""
        gone = set(pages)
        closed = [e for e in self.entries if e.get("status") == "open" and e.get("page") in gone]
        for e in closed:
            e["status"] = "closed"
        if closed:
            self.save()
        return closed
