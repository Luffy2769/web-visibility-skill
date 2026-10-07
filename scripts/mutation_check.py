"""Revert each audit fix in a scratch copy of ``src/`` and check the test suite notices.

Every entry below undoes one fix from the independent audits (#1: C1-L2,
#2: N1-N8). A fix whose reversal still passes the tests is unprotected, so the
script exits non-zero if any mutant survives. CI runs it on every push.

Usage::

    python scripts/mutation_check.py            # all mutants
    python scripts/mutation_check.py N2 M5      # only mutants whose name contains these
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (file under src/web_visibility, original text, mutated text)
Edit = tuple[str, str, str]

MUTANTS: dict[str, list[Edit]] = {
    "C1 ignore crawler-specific robots group": [("robots.py", "        if specific:\n", "        if False:\n")],
    "C2 disable shell detection": [("parsing.py", 'return {"little-visible-text", "script-driven"} <= kinds and', "return False and")],
    "C2 disable render-dependent hedging": [("analyzers/__init__.py", "    if not shells:\n        return issue", "    if True:\n        return issue")],
    "H1 unbounded decompression output": [("fetch.py", "out += self._d.decompress(data, budget - len(out))", "out += self._d.decompress(data)")],
    "H1 no raw-byte cap": [("fetch.py", "if len(out) > limit or raw_total > limit:", "if len(out) > limit:")],
    "H1 analyze truncated pages": [("crawler.py", "        if result.truncated:\n", "        if False:\n")],
    "H2 ignore googlebot/bingbot noindex": [("models/page.py", 'INDEXING_AGENTS = ("googlebot", "bingbot")', "INDEXING_AGENTS = ()")],
    "H2 X-Robots-Tag scope dropped": [("models/page.py", "agent, part = prefix, rest.strip()", "agent, part = None, rest.strip()")],
    "H3 N/A counted as scored": [("scoring.py", 'if cov.status in ("scored", "partial"):', "if True:")],
    "H5 guessed sitemap is a defect": [("analyzers/crawlability.py", "if file.guessed and not _guess_is_evidence(file):", "if False:")],
    "M1 robots 5xx/unavailable -> allow": [("models/robots.py", '    {"rate-limited", "server-error", "unavailable"}\n', "    set()\n")],
    "M2 body canonical treated as head": [("parsing.py", '                source = "body"', '                source = "head"')],
    "M3 sitemap complete despite file limit": [("models/sitemap.py", "if not self.checked or self.url_limit_reached or self.file_limit_reached:", "if not self.checked:")],
    "M4 XML DTD/entities allowed": [("sitemap.py", "forbid_dtd=True, forbid_entities=True, forbid_external=True", "forbid_dtd=False, forbid_entities=False, forbid_external=False")],
    "M5 no DNS pinning": [("fetch.py", "_pinned_request(current, target.address)", "_pinned_request(current, None)")],
    "M5 redirect hops unchecked": [("fetch.py", "current, allow_private=self.config.allow_private, timeout", "current, allow_private=self.config.allow_private or bool(chain), timeout")],
    "M8 Retry-After ignored": [("fetch.py", "wait = None if retried_after else _retry_after(result)", "wait = None")],
    "M8 Crawl-delay ignored": [("crawler.py", "        self._fetcher.set_min_delay(min(delay, self.config.max_crawl_delay))", "        pass")],
    "M8 429 does not stop crawl": [("crawler.py", '            if page.status_code == 429:\n                run.stop_reason = "rate-limited"\n                break', "            pass")],
    "L1 same_site adopts any subdomain": [("urls.py", 'return host_a.removeprefix("www.") == host_b.removeprefix("www.")', 'return host_a.split(".")[-2:] == host_b.split(".")[-2:]')],
    "L2 no robots path normalization": [("robots.py", "        target = normalize_robots_path(target)\n", "")],
    "N1 urljoin outside the guard": [("urls.py", "    try:\n        # urljoin/urlsplit raise", "    if base is not None:\n        url = urljoin(base, url)\n    try:\n        # urljoin/urlsplit raise")],
    "N1 malformed Location raises": [("fetch.py", "    except ValueError:\n        return None\n\n\ndef _strip_fragment", "    except ValueError:\n        raise\n\n\ndef _strip_fragment")],
    "N1 unusable charset trusted": [("models/page.py", "return name if _is_text_codec(name) else None", "return name")],
    "N1 no CLI safety net": [("cli.py", "    except Exception as exc:  # last resort", "    except ZeroDivisionError as exc:  # last resort")],
    "N2 unretrieved pages count as audited": [("models/page.py", "return self.error is not None or self.status_code in UNRETRIEVED_STATUSES", "return False")],
    "N2 unverifiable links count as checked": [("analyzers/base.py", 'f"{key}_ok" if status.definitive else f"{key}_unv"', 'f"{key}_ok"')],
    "N2 minority of measured items still scored": [("scoring.py", "    if checked < unmeasured:\n", "    if False:\n")],
    "N2 score from a corner of the model": [("scoring.py", "    if scored_weight < MIN_SCORED_WEIGHT:\n", "    if False:\n")],
    "N3 backtracking robots matcher": [("robots.py", "        return wildcard_match(self.path, target)", '        import re; return re.match(".*".join(re.escape(p) for p in self.path.removesuffix("$").split("*")) + ("$" if self.path.endswith("$") else ""), target) is not None')],
    "N5 unlimited XML nesting": [("sitemap.py", "            if depth > MAX_XML_DEPTH:\n", "            if False:\n")],
    "N6 budget spent inside svg": [("parsing.py", "        if node.name == \"svg\":\n", "        if False:\n")],
    "N8 link checks despite Crawl-delay": [("crawler.py", "if budget <= 0 or run.stop_reason in _STOP_REQUESTING:", 'if budget <= 0 or run.stop_reason == "rate-limited":')],
    "N8 sitemaps despite Crawl-delay": [("crawler.py", 'if host_down or run.stop_reason == "crawl-delay-too-large":', "if host_down:")],
}  # fmt: skip
# Not listed: unwrapping IPv4-mapped IPv6 in safety.py is an *equivalent* mutant,
# because ipaddress already classifies ::ffff:0:0/96 as non-global on every
# supported Python; the end-to-end SSRF tests still cover the behaviour.


def run(edits: list[Edit], work: Path) -> tuple[bool, str]:
    """Apply ``edits`` to a fresh copy of src/ and run the suite. ``(caught, detail)``."""
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(REPO / "src", work / "src")
    for rel, old, new in edits:
        path = work / "src" / "web_visibility" / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return False, f"PATCH DID NOT APPLY in {rel} (update this script)"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(work / "src")}
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--no-header"]
    proc = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=1800)
    lines = proc.stdout.strip().splitlines()
    failed = next((line for line in lines if line.startswith("FAILED")), lines[-1] if lines else "")
    return proc.returncode != 0, failed[:150]


def main(filters: list[str]) -> int:
    selected = {n: e for n, e in MUTANTS.items() if not filters or any(f in n for f in filters)}
    survivors = []
    with tempfile.TemporaryDirectory(prefix="wv-mutants-") as tmp:
        for name, edits in selected.items():
            caught, detail = run(edits, Path(tmp) / "work")
            print(f"{'CAUGHT  ' if caught else 'SURVIVED'} {name:45} {detail}", flush=True)
            if not caught:
                survivors.append(name)
    print(f"\n{len(selected) - len(survivors)}/{len(selected)} mutants caught.")
    if survivors:
        print("Unprotected fixes: " + ", ".join(survivors))
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
