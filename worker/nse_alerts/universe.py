from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Stock:
    symbol: str
    company: str
    industry: str
    isin: str


class Universe:
    def __init__(self, stocks: list[Stock]):
        self.stocks = stocks
        self.symbols = {s.symbol for s in stocks}
        self.by_symbol = {s.symbol: s for s in stocks}

    @classmethod
    def load(cls, path: Path | str) -> "Universe":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return cls(
            [
                Stock(
                    symbol=r["Symbol"].strip(),
                    company=r["Company Name"].strip(),
                    industry=r.get("Industry", "").strip(),
                    isin=r.get("ISIN Code", "").strip(),
                )
                for r in rows
            ]
        )

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.symbols
