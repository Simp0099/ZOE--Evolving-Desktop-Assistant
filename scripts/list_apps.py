#!/usr/bin/env python3
"""List the macOS applications installed on this machine, as config.yaml entries.

The allowlist in `applications.aliases` is the security boundary and it stays
where it is. This script exists so filling it in is not the painful part: it
prints the `aliases:` block you would paste in, so adding an app is copy-paste
rather than remembering whether it is "Google Chrome" or "Chrome".

It **prints**. It never writes to config.yaml, never edits anything, and never
launches anything. Reading it and pasting the output is a deliberate human step,
which is what keeps the allowlist a decision rather than a side effect.

Usage:
    python scripts/list_apps.py                 # every installed bundle
    python scripts/list_apps.py --filter chrom  # only bundles matching a prefix
    python scripts/list_apps.py --dirs          # list the directories scanned
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from zoe.logger import configure_logging  # noqa: E402
from zoe.tools import apps  # noqa: E402


def format_aliases(names: list[str]) -> str:
    """The `aliases:` block to paste into config.yaml.

    The command is the literal `["open", "-a", "<Name>"]` -- an argument list,
    which is the only form `zoe/tools/apps.py` will ever run. Note it carries
    no Windows or Linux entry: discovery is a macOS feature, and an app the
    user approved by confirming it on this machine need not be launchable from
    another one.
    """
    lines = ["  aliases:"]
    for name in names:
        # Double quotes inside a double-quoted YAML scalar: a bundle name with a
        # quote in it is vanishingly rare, but emitting invalid YAML silently
        # would be worse than emitting a visible placeholder.
        safe = name.replace('"', "'")
        lines.append(f'    {safe}: {{Darwin: ["open", "-a", "{safe}"]}}')
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--filter", default="",
                        help="only bundles whose name starts with this "
                             "(case-insensitive)")
    parser.add_argument("--dirs", action="store_true",
                        help="print the directories scanned and exit")
    args = parser.parse_args(argv)

    configure_logging(debug=False)

    if args.dirs:
        print("Directories scanned:")
        for directory in apps.APP_BUNDLE_DIRS:
            print(f"  {directory}")
        return 0

    if platform.system() != "Darwin":
        print(f"Application discovery is macOS-only; this is "
              f"{platform.system()}. Nothing to list.")
        print("On macOS this prints the .app bundles in "
              "/Applications, /System/Applications and ~/Applications.")
        return 0

    names = apps.installed_apps()
    if args.filter:
        wanted = args.filter.strip().casefold()
        names = [n for n in names if n.casefold().startswith(wanted)]

    if not names:
        print("No application bundles found in the scanned directories.")
        print(f"Scanned: {', '.join(apps.APP_BUNDLE_DIRS)}")
        return 0

    print(f"# {len(names)} application bundle(s) found. Paste the block below into")
    print("# applications: in config.yaml, then review it. Only keep what you")
    print("# actually want the assistant to be able to open.")
    print(format_aliases(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())