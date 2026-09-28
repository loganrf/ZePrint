"""Parsers for Zebra host status (``~HS``) and host identification (``~HI``)."""

from __future__ import annotations

import re

_FRAME = re.compile(rb"\x02([^\x02\x03]*)\x03")
DPMM_TO_DPI = {6: 152, 8: 203, 12: 300, 24: 600}


def frames(raw: bytes) -> list[str]:
    return [f.decode("ascii", "replace").strip() for f in _FRAME.findall(raw or b"")]


def parse_host_status(raw: bytes, media: str = "direct") -> dict:
    """
    ``~HS`` answers with three STX..ETX frames:

      1: aaa,b,c,dddd,eee,f,g,h,iii,j,k,l
         b paper out, c paused, dddd label length (dots), eee formats in the
         receive buffer, f buffer full, k under temperature, l over temperature
      2: mmm,n,o,p,q,r,s,t,uuuuuuuu,v,www
         o head up, p ribbon out, q thermal transfer mode, r print mode,
         t label waiting, uuuuuuuu labels remaining in batch
      3: xxxx,y (password, static RAM) - ignored
    """
    fr = frames(raw)
    if len(fr) < 2:
        raise ValueError("incomplete ~HS response")
    a = fr[0].split(",")
    b = fr[1].split(",")
    if len(a) < 12 or len(b) < 9:
        raise ValueError("unexpected ~HS format")

    def flag(v):
        return v.strip() == "1"

    def num(v):
        try:
            return int(v)
        except ValueError:
            return None

    s = {
        "paper_out": flag(a[1]),
        "paused": flag(a[2]),
        "label_length_dots": num(a[3]),
        "formats_in_buffer": num(a[4]),
        "buffer_full": flag(a[5]),
        "under_temperature": flag(a[10]),
        "over_temperature": flag(a[11]),
        "head_open": flag(b[2]),
        # in direct-thermal mode there is no ribbon to run out of
        "ribbon_out": flag(b[3]) and media == "ribbon",
        "thermal_transfer": flag(b[4]),
        "label_waiting": flag(b[7]),
        "labels_remaining": num(b[8]),
    }
    s["state"] = summarize(s)
    return s


def summarize(s: dict) -> str:
    for key, state in (("head_open", "head_open"), ("paper_out", "paper_out"),
                       ("ribbon_out", "ribbon_out"), ("over_temperature", "over_temperature"),
                       ("under_temperature", "under_temperature"),
                       ("buffer_full", "buffer_full"), ("paused", "paused")):
        if s.get(key):
            return state
    if (s.get("formats_in_buffer") or 0) > 0 or (s.get("labels_remaining") or 0) > 0:
        return "printing"
    return "ready"


def parse_host_identification(raw: bytes) -> dict:
    """``~HI`` -> ``model,firmware,dots/mm,memory[,options]``."""
    fr = frames(raw)
    if not fr:
        raise ValueError("empty ~HI response")
    parts = [p.strip() for p in fr[0].split(",")]
    out = {"model": parts[0] if parts else None,
           "firmware": parts[1] if len(parts) > 1 else None}
    if len(parts) > 2:
        try:
            dpmm = int(parts[2])
            out["dots_per_mm"] = dpmm
            out["dpi"] = DPMM_TO_DPI.get(dpmm, round(dpmm * 25.4))
        except ValueError:
            pass
    if len(parts) > 3:
        out["memory"] = parts[3]
    return out
