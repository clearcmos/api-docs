#!/usr/bin/env python3

"""
API Docs Fetcher Runner

Discovers all vendor fetch.py scripts dynamically and provides a unified
interface to run one, several, or all of them.

Usage:
    python run.py                      # interactive picker
    python run.py cloudflare okta      # run specific vendors
    python run.py --all                # run all vendors
    python run.py --all --dry-run      # dry-run all vendors
    python run.py --list               # just list discovered vendors
    python run.py terraform -- --provider hashicorp/aws   # vendor-specific args
"""

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Flags forwarded to each vendor's fetch.py
FORWARD_FLAGS = {"--dry-run", "--force", "--verbose"}


def discover_vendors() -> list[str]:
    """Scan for {vendor}/fetch.py and return sorted vendor names."""
    vendors = []
    for entry in os.scandir(SCRIPT_DIR):
        if entry.is_dir() and not entry.name.startswith("."):
            fetch_py = os.path.join(entry.path, "fetch.py")
            if os.path.isfile(fetch_py):
                vendors.append(entry.name)
    return sorted(vendors)


def needs_interactive(vendor: str) -> bool:
    """Check if a vendor's fetch.py needs interactive input or extra args.

    Detected by:
    - '# requires-interactive' comment (vendor handles its own interactive mode)
    - 'required=True' in argparse definitions (mandatory auth/config args)
    """
    fetch_py = os.path.join(SCRIPT_DIR, vendor, "fetch.py")
    try:
        with open(fetch_py, "r") as f:
            content = f.read()
    except OSError:
        return False
    return ("# requires-interactive" in content
            or "required=True" in content
            or "required_group" in content)


def run_vendor(vendor: str, flags: list[str], passthrough: list[str] | None = None,
               suppress_stdin: bool = False) -> int:
    """Run a vendor's fetch.py. Returns exit code."""
    fetch_py = os.path.join(SCRIPT_DIR, vendor, "fetch.py")
    cmd = [sys.executable, fetch_py] + flags
    if passthrough:
        cmd.extend(passthrough)
    stdin = subprocess.DEVNULL if suppress_stdin else None
    result = subprocess.run(cmd, cwd=SCRIPT_DIR, stdin=stdin)
    return result.returncode


def interactive_pick_fzf(vendors: list[str]) -> list[str]:
    """Pick vendors via fzf with multi-select (tab to toggle)."""
    lines = []
    for name in vendors:
        tag = " (interactive)" if needs_interactive(name) else ""
        lines.append(f"{name}{tag}")
    text = "\n".join(lines)

    try:
        result = subprocess.run(
            ["fzf", "--multi", "--prompt", "Vendors> ", "--height=40%", "--reverse",
             "--header", "Tab to multi-select, Enter to confirm"],
            input=text, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [line.split()[0] for line in result.stdout.strip().splitlines()]
        return []
    except FileNotFoundError:
        return None  # signal to fall back


def interactive_pick_basic(vendors: list[str]) -> list[str]:
    """Fallback numbered menu when fzf is not available."""
    print("Available vendors:\n")
    for i, name in enumerate(vendors, 1):
        extra = " (interactive)" if needs_interactive(name) else ""
        print(f"  {i:3d}. {name}{extra}")

    print(f"\nEnter numbers, names, or ranges (e.g. '1 3-5 okta'), or 'all'.")
    print("Press Ctrl+C to cancel.\n")

    try:
        raw = input("> ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return []

    if not raw:
        return []

    if raw.lower() == "all":
        return vendors

    selected: list[str] = []
    for token in raw.split():
        # Range: "3-7"
        if "-" in token and token[0].isdigit():
            parts = token.split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
                for n in range(lo, hi + 1):
                    if 1 <= n <= len(vendors):
                        selected.append(vendors[n - 1])
            except ValueError:
                pass
            continue

        # Single number
        if token.isdigit():
            n = int(token)
            if 1 <= n <= len(vendors):
                selected.append(vendors[n - 1])
            continue

        # Name (exact or prefix match)
        token_lower = token.lower()
        matches = [v for v in vendors if v == token_lower or v.startswith(token_lower)]
        if len(matches) == 1:
            selected.append(matches[0])
        elif len(matches) > 1:
            print(f"  Ambiguous '{token}': {', '.join(matches)}")
        else:
            print(f"  Unknown vendor: {token}")

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for v in selected:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def interactive_pick(vendors: list[str]) -> list[str]:
    """Pick vendors interactively. Tries fzf, falls back to numbered menu."""
    result = interactive_pick_fzf(vendors)
    if result is not None:
        return result
    return interactive_pick_basic(vendors)


def main():
    vendors = discover_vendors()
    if not vendors:
        print("No vendor fetch.py scripts found.", file=sys.stderr)
        sys.exit(1)

    # Split on -- separator: everything after is passthrough to vendor scripts
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        sep = argv.index("--")
        passthrough = argv[sep + 1:]
        argv = argv[:sep]

    run_all = "--all" in argv
    list_only = "--list" in argv
    flags = [a for a in argv if a in FORWARD_FLAGS]
    vendor_args = [a for a in argv if a not in FORWARD_FLAGS and a not in ("--all", "--list")]

    if list_only:
        for v in vendors:
            extra = " *" if needs_interactive(v) else ""
            print(f"  {v}{extra}")
        print(f"\n  {len(vendors)} vendors (* = needs --provider or auth)")
        return

    # Determine which vendors to run
    if run_all:
        selected = vendors
    elif vendor_args:
        selected = []
        for name in vendor_args:
            name_lower = name.lower()
            if name_lower in vendors:
                selected.append(name_lower)
            else:
                matches = [v for v in vendors if v.startswith(name_lower)]
                if len(matches) == 1:
                    selected.append(matches[0])
                else:
                    print(f"Unknown vendor: {name}", file=sys.stderr)
                    sys.exit(1)
    else:
        selected = interactive_pick(vendors)

    if not selected:
        print("Nothing selected.")
        return

    # Run each vendor
    total = len(selected)
    failed = []
    skipped = []

    for i, vendor in enumerate(selected, 1):
        is_interactive_vendor = needs_interactive(vendor)

        # In --all mode, skip interactive vendors (they need manual input)
        if run_all and is_interactive_vendor:
            print(f"\n[{i}/{total}] Skipping {vendor} (needs --provider or auth)")
            skipped.append(vendor)
            continue

        print(f"\n{'=' * 60}")
        print(f"[{i}/{total}] {vendor}")
        print(f"{'=' * 60}")

        rc = run_vendor(vendor, flags, passthrough if passthrough else None)
        if rc != 0:
            failed.append(vendor)
            print(f"\n  {vendor} exited with code {rc}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Done: {total - len(failed) - len(skipped)} succeeded", end="")
    if skipped:
        print(f", {len(skipped)} skipped ({', '.join(skipped)})", end="")
    if failed:
        print(f", {len(failed)} failed ({', '.join(failed)})", end="")
    print()

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
