"""Normalise the per-modality pricing shapes of the Lightning model catalog into one comparable form.

``GET /v1/models`` returns different price fields per modality (per-token ``pricing.prompt``/``completion`` for text,
``pricing.image`` per image, ``pricing.request`` per second/clip/model for media, ...). ``price_of`` flattens one
catalog entry into a :class:`Price` with a unit and a headline rate so models can be listed and sorted together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Price:
    kind: str                      # text, image, video, audio, embedding, realtime, 3d
    unit: str                      # "1M tokens", "image", "second", "1k chars", "model", ...
    input: Optional[float]         # USD per `unit` (input side for token-billed models)
    output: Optional[float]        # USD per `unit` for output tokens (None when not separately billed)

    @property
    def headline(self) -> float:
        """The rate used to sort models of the same kind (input rate, else output, else 0)."""
        for v in (self.input, self.output):
            if v is not None:
                return v
        return 0.0

    @property
    def free(self) -> bool:
        return not (self.input or self.output)

    def label(self) -> str:
        if self.free:
            return "free"
        if self.output:
            return f"{_usd(self.input or 0)} in / {_usd(self.output)} out per {self.unit}"
        return f"{_usd(self.headline)} per {self.unit}"


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None  # drop NaN and negatives


def _usd(v: float) -> str:
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.2f}"
    whole, _, frac = f"{v:.6f}".partition(".")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"${whole}.{frac}"


def kind_of(m: Dict[str, Any]) -> str:
    t = str(m.get("type") or "").lower()
    if t:
        return t
    outs = [str(x).lower() for x in (m.get("output_modalities") or [])]
    for k in ("image", "video", "audio", "3d", "embedding"):
        if k in outs:
            return k
    return "text"


def price_of(m: Dict[str, Any]) -> Price:
    kind = kind_of(m)
    p = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    prompt, completion = _num(p.get("prompt")), _num(p.get("completion"))
    request, image = _num(p.get("request")), _num(p.get("image"))

    if kind in ("text", "embedding") or (kind == "realtime" and (prompt or completion)):
        i = prompt * 1_000_000 if prompt is not None else None
        o = completion * 1_000_000 if completion else None
        return Price(kind, "1M tokens", i, o)
    if kind == "image":
        return Price(kind, "image", image if image is not None else request, None)
    if kind == "video":
        return Price(kind, "second", request, None)
    if kind == "audio":
        unit = {"input_chars": "1k chars", "input_bytes": "1M bytes", "input_seconds": "second"}.get(
            str(m.get("billing_unit") or ""), "second")
        return Price(kind, unit, request, None)
    if kind == "realtime":
        return Price(kind, "second", request, None)
    if kind == "3d":
        return Price(kind, "model", request, None)
    return Price(kind, "request", request, None)


def rows(models: List[Dict[str, Any]], *, kind: Optional[str] = None, search: Optional[str] = None,
         free: bool = False, max_price: Optional[float] = None, sort: str = "price") -> List[Dict[str, Any]]:
    """Filter and order catalog entries; returns dicts ready for tables and ``--json``."""
    out = []
    s = (search or "").lower()
    for m in models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        pr = price_of(m)
        if kind and pr.kind != kind:
            continue
        if s and s not in str(m["id"]).lower() and s not in str(m.get("name") or "").lower():
            continue
        if free and not pr.free:
            continue
        if max_price is not None and pr.headline > max_price:
            continue
        out.append({"id": m["id"], "name": m.get("name"), "kind": pr.kind, "unit": pr.unit,
                    "input": pr.input, "output": pr.output, "price": pr.label(), "free": pr.free})
    if sort == "name":
        out.sort(key=lambda r: (r["kind"], str(r["id"]).lower()))
    else:
        out.sort(key=lambda r: (r["kind"], r["input"] if r["input"] is not None else (r["output"] or 0.0), str(r["id"]).lower()))
    return out
