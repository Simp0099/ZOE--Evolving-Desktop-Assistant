#!/usr/bin/env python3
"""Read barge-in decisions out of the Zoe log.

A barge-in decision is logged (`zoe/voice_loop.py`) as one stable line:

    [BARGE] kind=interrupt turn=turn_002 state=speaking barge_in=True \
elapsed_s=0.328 rms_raw=0.0323 rms_post=0.0245 gain=0.000 \
reduction=0.000 lag_ms=0.0 guard_residual=0.000602 guard_playback=0.000000 \
guard_streak=3 bar=0.088 blocked=False

Every field describes the frame that was decided, not a remembered earlier one.
That distinction is the entire reason this line exists: the previous diagnostic
carried `rms_raw` forward from an `[AUDIO] callback` line emitted on a 5-second
cadence, and read `residual_energy`/`playback_energy` from the last sub-frame the
guard happened to score -- which read 0.000000 while the reply was demonstrably
playing. See docs/barge_in_diagnosis.md section 4.

`kind` is one of:

* `frame`     -- DEBUG, every barge-in frame (12.5/s while speaking). A useful
                 window around a cut; noise everywhere else.
* `interrupt` -- INFO, the speech-start frame that took the floor. **Read these.**
* `declined`  -- INFO, the speech start was dropped by the barge-in grace window
                 (`blocked=True`) and nothing was interrupted.

How to read a cut:

* `rms_post` close to `rms_raw` with `gain=0.000` -> the canceller found no
  reference and subtracted nothing. The room beat it.
* `guard_playback=0.000000` while `elapsed_s` is small -> the guard had no
  aligned reference for the frame it scored.
* `sqrt(guard_residual)` far below `bar` -> nothing could have tripped the
  detector on that frame either, so the cut came from an earlier one.

Run:
    python scripts/analyse_barge_in.py [log ...]   # default: data/zoe.log*
    python scripts/analyse_barge_in.py --self-test
    python scripts/analyse_barge_in.py --kind frame # also show per-frame lines

Nothing here changes behaviour; it only reads the log the voice loop writes.
"""

from __future__ import annotations

import argparse
import glob
import math
import re
import sys
from collections import Counter

#: The tag. The field set after it is `key=value` pairs, parsed generically so a
#: field added later is picked up here without a code change.
BARGE = re.compile(r"\[BARGE\]\s+(?P<body>.+)$")

#: The older cut line, kept so a log written before this instrumentation still
#: reports a cut count rather than silently reporting nothing.
CUT = re.compile(r"reply cut short turn=(\S+) reason=barge-in chunks_played=(\d+)")

#: Fields rendered as floats; everything else is kept as its literal text so a
#: future field is never dropped by a type assumption.
NUMERIC = frozenset({
    "elapsed_s", "rms_raw", "rms_post", "gain", "reduction", "lag_ms",
    "guard_residual", "guard_playback", "guard_streak",
    "guard_ratio", "guard_ratio_bar", "bar",
})


def default_logs() -> list[str]:
    return sorted(glob.glob("data/zoe.log*"))


def parse_line(line: str) -> dict | None:
    """One `[BARGE]` record from a log line, or None if it is not one.

    Parsed as key=value pairs rather than by field position, so reordering or
    adding a field cannot silently shift a value into the wrong column.
    """
    match = BARGE.search(line)
    if not match:
        return None
    record: dict = {}
    for token in match.group("body").split():
        key, sep, value = token.partition("=")
        if not sep:
            continue
        if key in NUMERIC:
            try:
                record[key] = float(value)
            except ValueError:
                record[key] = value
        else:
            record[key] = value
    return record or None


def parse(paths: list[str]) -> tuple[list[dict], list[dict]]:
    """(decision records, legacy cut records) across the given logs."""
    decisions: list[dict] = []
    cuts: list[dict] = []
    for path in paths:
        try:
            with open(path, errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            record = parse_line(line)
            if record is not None:
                record["file"] = path
                decisions.append(record)
            match = CUT.search(line)
            if match:
                cuts.append({"file": path, "turn": match.group(1),
                             "played": int(match.group(2))})
    return decisions, cuts


def _fmt(record: dict, key: str, spec: str = ".4f") -> str:
    value = record.get(key)
    if not isinstance(value, float) and not isinstance(value, int):
        return "?"
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return "?"


def report(paths: list[str], kinds: tuple[str, ...]) -> int:
    if not paths:
        print("no log files found (looked for data/zoe.log*)")
        return 1
    decisions, cuts = parse(paths)

    print(f"log files ({len(paths)}):")
    for path in paths:
        print(f"  {path}")
    print()

    if not decisions:
        print("no [BARGE] decision lines found.")
        print("Either these logs predate the instrumentation, or barge-in never")
        print("fired. Run the assistant in voice mode, interrupt it once, and")
        print("re-run this against the fresh data/zoe.log.")
        if cuts:
            print(f"\nlegacy cut lines present: {len(cuts)} (pre-instrumentation)")
        return 0

    counts = Counter(d.get("kind", "?") for d in decisions)
    print("decisions by kind:")
    for kind, n in sorted(counts.items()):
        print(f"  {kind:<10} {n}")
    print(f"  {'total':<10} {len(decisions)}")
    print(f"\nlegacy '[VOICE] reply cut short' lines: {len(cuts)}")
    print()

    shown = [d for d in decisions if d.get("kind", "?") in kinds]
    if not shown:
        print(f"no decisions of kind {', '.join(kinds)}. Try --kind frame.")
        return 0

    print(f"decisions ({len(shown)}), most recent last:")
    print(f"  {'file':<14} {'kind':<10} {'turn':<10} {'state':<10} {'elapsed_s':>9} "
          f"{'rms_raw':>8} {'rms_post':>8} {'gain':>6} {'red':>6} {'lag_ms':>7} "
          f"{'g_resid':>9} {'g_play':>9} {'streak':>6} {'bar':>7} {'blk':>5}")
    for d in shown:
        print(f"  {d['file'][-14:]:<14} {d.get('kind', '?'):<10} "
              f"{d.get('turn', '-'):<10} {d.get('state', '?'):<10} "
              f"{_fmt(d, 'elapsed_s', '.3f'):>9} {_fmt(d, 'rms_raw'):>8} "
              f"{_fmt(d, 'rms_post'):>8} {_fmt(d, 'gain', '.3f'):>6} "
              f"{_fmt(d, 'reduction', '.3f'):>6} {_fmt(d, 'lag_ms', '.1f'):>7} "
              f"{_fmt(d, 'guard_residual', '.6f'):>9} "
              f"{_fmt(d, 'guard_playback', '.6f'):>9} "
              f"{_fmt(d, 'guard_streak', '.0f'):>6} {_fmt(d, 'bar'):>7} "
              f"{str(d.get('blocked', '?')):>5}")

    # -- interpretation ----------------------------------------------------
    cuts_only = [d for d in shown if d.get("kind") == "interrupt"]
    if not cuts_only:
        return 0

    print()
    print("what the cuts say:")
    no_reference = sum(1 for d in cuts_only if d.get("gain", 0) == 0)
    inert = sum(1 for d in cuts_only
                if isinstance(d.get("rms_raw"), float)
                and isinstance(d.get("rms_post"), float)
                and abs(d["rms_raw"] - d["rms_post"]) < 1e-6)
    silent_guard = sum(1 for d in cuts_only if d.get("guard_playback", 1) == 0)
    below_bar = 0
    for d in cuts_only:
        residual, bar = d.get("guard_residual"), d.get("bar")
        if isinstance(residual, (int, float)) and isinstance(bar, (int, float)) \
                and math.sqrt(max(residual, 0.0)) < bar:
            below_bar += 1

    n = len(cuts_only)
    print(f"  {no_reference}/{n} had gain=0.000 (canceller found no reference)")
    print(f"  {inert}/{n} left rms_post == rms_raw (nothing was subtracted)")
    print(f"  {silent_guard}/{n} had guard_playback=0.000000 "
          f"(guard had no aligned reference)")
    print(f"  {below_bar}/{n} had sqrt(guard_residual) BELOW the bar "
          f"(that frame could not have tripped the detector)")
    print()
    if inert:
        print("  -> the canceller removed nothing on these frames. A threshold")
        print("     change cannot fix that; the reference path needs work.")
    if silent_guard:
        print("  -> the echo guard scored silence while audio was playing.")
    if below_bar == n:
        print("  -> every cut's own frame was under the bar: the interrupt was")
        print("     decided by an earlier frame in the streak. Look at the")
        print("     --kind frame window immediately preceding the cut.")
    return 0


def self_test() -> int:
    sample = (
        "x [AUDIO] callback frames=0 rms_raw=0.0007 rms_post_echo=0.0007 state=listening\n"
        "x [BARGE] kind=frame turn=- state=speaking barge_in=True elapsed_s=0.240 "
        "rms_raw=0.0323 rms_post=0.0245 gain=0.412 reduction=0.431 lag_ms=24.0 "
        "guard_residual=0.000602 guard_playback=0.004120 guard_streak=2 "
        "bar=0.0880 blocked=False\n"
        "x [BARGE] kind=interrupt turn=turn_002 state=speaking barge_in=True "
        "elapsed_s=0.328 rms_raw=0.0323 rms_post=0.0245 gain=0.000 "
        "reduction=0.000 lag_ms=0.0 guard_residual=0.000602 "
        "guard_playback=0.000000 guard_streak=3 bar=0.0880 blocked=False\n"
        "x [BARGE] kind=declined turn=- state=speaking barge_in=True "
        "elapsed_s=0.100 rms_raw=0.0100 rms_post=0.0100 gain=0.000 "
        "reduction=0.000 lag_ms=0.0 guard_residual=0.000000 "
        "guard_playback=0.000000 guard_streak=0 bar=0.0880 blocked=True\n"
        "x [VOICE] reply cut short turn=turn_002 reason=barge-in chunks_played=9\n"
        "x [VOICE] reply cut short turn=turn_003 reason=shutdown chunks_played=7\n"
    )
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".log")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(sample)
        decisions, cuts = parse([path])
    finally:
        os.unlink(path)

    assert len(decisions) == 3, decisions
    assert [d["kind"] for d in decisions] == ["frame", "interrupt", "declined"]

    frame = decisions[0]
    assert frame["gain"] == 0.412 and frame["lag_ms"] == 24.0, frame
    assert frame["guard_streak"] == 2 and frame["bar"] == 0.088, frame
    assert frame["barge_in"] == "True" and frame["blocked"] == "False", frame

    cut = decisions[1]
    assert cut["turn"] == "turn_002" and cut["elapsed_s"] == 0.328, cut
    assert cut["gain"] == 0.0 and cut["guard_playback"] == 0.0, cut
    assert cut["guard_residual"] == 0.000602 and cut["bar"] == 0.088, cut

    assert decisions[2]["blocked"] == "True", decisions[2]

    # `elapsed_s=inf` must survive as a number rather than killing the line, and
    # must render as "inf" rather than "?" or an empty column.
    inf = parse_line("[BARGE] kind=frame elapsed_s=inf rms_raw=0.0 bar=0.02 "
                     "guard_streak=0 blocked=False\n")
    assert inf["elapsed_s"] == float("inf"), inf
    assert _fmt(inf, "elapsed_s", ".3f") == "inf", _fmt(inf, "elapsed_s", ".3f")

    # A non-[BARGE] line is not a decision.
    assert parse_line("x [AUDIO] callback rms_raw=0.0007\n") is None

    # Legacy cut lines still counted; `reason=shutdown` is excluded by the regex.
    assert len(cuts) == 1 and cuts[0]["turn"] == "turn_002", cuts
    assert cuts[0]["played"] == 9, cuts

    print("self-test ok: 3 decisions (frame/interrupt/declined), all fields "
          "parsed, inf elapsed survives, non-BARGE lines ignored, legacy cuts "
          "parsed with shutdown excluded")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", help="log files (default: data/zoe.log*)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in check")
    ap.add_argument("--kind", default="interrupt,declined",
                    help="comma-separated kinds to show (default: "
                         "interrupt,declined; 'all' shows every frame line)")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    kinds = ("frame", "interrupt", "declined") if args.kind == "all" \
        else tuple(k.strip() for k in args.kind.split(",") if k.strip())
    return report(args.logs or default_logs(), kinds)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))