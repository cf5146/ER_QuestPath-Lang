#!/usr/bin/env python3
"""Validate QuestPath translation files against lang/english.json.

Usage:
  python validate.py                        validate every lang/*.json
  python validate.py lang/german.json ...   validate only these files
  python validate.py --summary              just the coverage table
  python validate.py --todo lang/german.json
                                            print the keys that still need
                                            work (tab-separated english
                                            source), ready to hand to a
                                            translator
  python validate.py --json                 machine-readable report
  python validate.py lang/german.json --apply new.tsv
                                            write finished translations back

Options:
  --english PATH   reference file (default: lang/english.json)
  --since REV      also flag keys whose english text changed since a git
                   revision -- the english wording moved on, so the existing
                   translation is stale even though the key is present
  --todo FILE      emit a work list for FILE (missing + stale + drifted keys)
                   as TSV: key <TAB> reason <TAB> english <TAB> current
  --apply PATCH    merge a `key <TAB> translation` TSV ('-' for stdin) back
                   into a translation file and rewrite it in english.json key
                   order; add --prune to drop keys english.json dropped
  --json           emit the whole report as JSON on stdout
  --summary        suppress per-finding output, print only the table
  --strict         make warnings fail the run too (default: only errors do)
  --max N          show at most N findings per category (0 = all, default 25)
  --no-color       never emit ANSI colors

Checks
  errors (exit code 1):
    E001 file does not parse as JSON / is not UTF-8
    E002 top-level value is not an object
    E003 duplicate key in the file (json silently keeps the last one)
    E004 value is not a string
    E005 placeholder (%s/%d) set or order differs from english
    E006 malformed placeholder (%S, % s, %1$s, ...)
    E007 value is empty or whitespace only
  warnings (exit code 0 unless --strict):
    W101 key missing (falls back to english in-game)
    W102 key no longer in english.json (stale)
    W103 step renumbering detected in a quest -- the surviving translations of
         that quest are probably attached to the wrong step
    W104 value identical to english (looks untranslated)
    W105 leading/trailing whitespace or newline count differs from english
    W106 length wildly out of line with the rest of this same file
    W107 english text changed since --since REV (translation is stale)

Every finding points at the exact line in the file it belongs to so it can be
opened straight from the terminal.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# The placeholders the mod's formatter understands.
PLACEHOLDER_RE = re.compile(r"%[sd%]")
# Anything that looks like it was *meant* to be a placeholder but isn't one the
# formatter accepts. %% is a literal percent and is fine.
BAD_PLACEHOLDER_RE = re.compile(r"%(?![sd%])\S?|%\s+[sd]\b|%\d+\$[sd]")
# Matches a top-level `"key":` at the start of a line in a flat JSON object.
KEY_LINE_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:')
# `q19.s7.next` -> group("q19"), group("s7"), group("next")
STEP_KEY_RE = re.compile(r"^(q\d+)\.(s\d+)\.(\w+)$")

# Keys whose english value is legitimately the same in every language
# (proper nouns, gamepad button faces, the mod's own name).
UNTRANSLATED_OK = (
    re.compile(r"^ui\.btn\."),
    re.compile(r"^ui\.title$"),
    re.compile(r"^loc\."),
    re.compile(r"\.character$"),
    re.compile(r"^ui\.opt\.sample\."),
)
# Below this many words an identical string is far more likely to be a name or
# a one-word label than a forgotten translation.
UNTRANSLATED_MIN_WORDS = 4
# A value this many times off the file's own median english->translation length
# ratio is almost always the wrong text pasted in.
LENGTH_RATIO_TOLERANCE = 3.0
LENGTH_RATIO_MIN_CHARS = 40


# --------------------------------------------------------------------------
# terminal plumbing
# --------------------------------------------------------------------------
def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
    except Exception:
        pass


_enable_windows_ansi()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _color(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def red(t): return _color("31", t)
def yellow(t): return _color("33", t)
def green(t): return _color("32", t)
def cyan(t): return _color("36", t)
def bold(t): return _color("1", t)
def dim(t): return _color("2", t)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
class LangFile:
    """A parsed translation file plus everything needed to point at lines."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.data = {}
        self.raw = ""
        self.lines = {}        # key -> (lineno, raw line)
        self.duplicates = []   # (key, [linenos])
        self.error = None
        self.had_bom = False
        self._load()

    def _load(self):
        try:
            blob = self.path.read_bytes()
        except OSError as e:
            self.error = f"could not read file: {e}"
            return
        if blob.startswith(b"\xef\xbb\xbf"):
            self.had_bom = True
            blob = blob[3:]
        try:
            self.raw = blob.decode("utf-8")
        except UnicodeDecodeError as e:
            self.error = (f"file is not valid UTF-8 (byte {e.start}); "
                          f"re-save it as UTF-8")
            return

        seen = defaultdict(int)

        def hook(pairs):
            for k, _ in pairs:
                seen[k] += 1
            return dict(pairs)

        try:
            self.data = json.loads(self.raw, object_pairs_hook=hook)
        except json.JSONDecodeError as e:
            self.error = (f"invalid JSON at line {e.lineno}, "
                          f"column {e.colno}: {e.msg}")
            return

        self.lines = self._build_line_map()
        self.duplicates = sorted(k for k, n in seen.items() if n > 1)

    def _build_line_map(self):
        line_map = {}
        for lineno, line in enumerate(self.raw.splitlines(), start=1):
            m = KEY_LINE_RE.match(line)
            if m and m.group(1) not in line_map:
                line_map[m.group(1)] = (lineno, line.rstrip("\n"))
        return line_map

    def lineno(self, key):
        got = self.lines.get(key)
        return got[0] if got else None

    def loc(self, key):
        lineno = self.lineno(key)
        return f"{self.name}:{lineno}" if lineno else self.name

    def all_linenos(self, key):
        """Every line a key appears on (for duplicate reporting)."""
        out = []
        for lineno, line in enumerate(self.raw.splitlines(), start=1):
            m = KEY_LINE_RE.match(line)
            if m and m.group(1) == key:
                out.append(lineno)
        return out


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def placeholders(value: str):
    return PLACEHOLDER_RE.findall(value)


def bad_placeholders(value: str):
    return [m.group(0) for m in BAD_PLACEHOLDER_RE.finditer(value)]


def quest_of(key: str):
    m = STEP_KEY_RE.match(key)
    return m.group(1) if m else None


def step_of(key: str):
    m = STEP_KEY_RE.match(key)
    return int(m.group(2)[1:]) if m else None


# s99 is the questline's terminal "how it ended" pseudo-step. It is always
# last, so adding or removing it never shifts anything.
TERMINAL_STEP = 99


def looks_untranslated(key: str, value: str) -> bool:
    if any(p.search(key) for p in UNTRANSLATED_OK):
        return False
    return len(value.split()) >= UNTRANSLATED_MIN_WORDS


def whitespace_signature(value: str):
    return (value[:1].isspace(), value[-1:].isspace(), value.count("\n"))


def english_changed_since(rev: str, english_path: Path, current: dict):
    """Keys whose english text is different at REV -> stale translations."""
    repo = english_path.resolve().parent.parent
    rel = english_path.resolve().relative_to(repo).as_posix()
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", f"{rev}:{rel}"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8")
        old = json.loads(out)
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as e:
        print(f"[{yellow('WARN')}] --since {rev}: could not read {rel} at that "
              f"revision ({e.__class__.__name__}); skipping the staleness check")
        return set()

    def norm(s):
        # Punctuation-only edits (';' -> '.', '-' -> '--') do not invalidate a
        # translation, so they must not be reported as stale.
        return re.sub(r"[^0-9a-z]+", "", s.lower())

    return {k for k, v in current.items()
            if k in old and norm(old[k]) != norm(v)}


class Finding:
    def __init__(self, code, level, key, message, lineno=None, extra=None):
        self.code = code
        self.level = level          # "error" | "warn"
        self.key = key
        self.message = message
        self.lineno = lineno
        self.extra = extra or {}

    def as_dict(self):
        d = {"code": self.code, "level": self.level, "key": self.key,
             "line": self.lineno, "message": self.message}
        d.update(self.extra)
        return d


def validate_file(english: LangFile, target: LangFile, stale_keys=None):
    """Returns (findings, stats)."""
    findings = []
    stale_keys = stale_keys or set()

    def add(code, level, key, msg, lineno=None, **extra):
        findings.append(Finding(code, level, key, msg, lineno, extra))

    if target.error:
        add("E001", "error", None, target.error)
        return findings, {"keys": 0, "translated": 0, "coverage": 0.0}
    if not isinstance(target.data, dict):
        add("E002", "error", None, "top-level JSON value must be an object")
        return findings, {"keys": 0, "translated": 0, "coverage": 0.0}

    if target.had_bom:
        add("W105", "warn", None,
            "file starts with a UTF-8 BOM; save it as UTF-8 without BOM")

    for key in target.duplicates:
        linenos = target.all_linenos(key)
        add("E003", "error", key,
            f"duplicate key (lines {', '.join(map(str, linenos))}); "
            f"JSON keeps only the last one",
            lineno=linenos[0] if linenos else None)

    en_keys = list(english.data)
    en_set = set(en_keys)
    tr_set = set(target.data)
    missing = [k for k in en_keys if k not in tr_set]
    extra = [k for k in target.data if k not in en_set]

    # --- W103: step renumbering -------------------------------------------
    # When a quest gains or loses steps, everything after the insertion point
    # shifts: q19.s7 in the translation is now the english q19.s8. The keys
    # still line up so nothing else here can see it, but the *text* is wrong.
    # A whole step appearing or disappearing is what shifts the numbering, and
    # a step exists exactly when it has a `.title`. An added `.warn` on its own
    # renumbers nothing, so it must not drag its questline in here.
    first_affected = {}
    for key in missing + extra:
        q, step = quest_of(key), step_of(key)
        if q is None or step == TERMINAL_STEP or not key.endswith(".title"):
            continue
        if step < first_affected.get(q, 1 << 30):
            first_affected[q] = step

    drifted = {}
    for q, first in first_affected.items():
        suspects = sorted(
            (k for k in tr_set & en_set
             if quest_of(k) == q and first <= step_of(k) < TERMINAL_STEP),
            key=lambda k: (step_of(k), k))
        if suspects:
            drifted[q] = (first, suspects)

    for key in missing:
        add("W101", "warn", key, "not translated yet (falls back to english)",
            lineno=english.lineno(key), english=english.data[key])

    for key in extra:
        hint = ""
        q = quest_of(key)
        if q:
            suffix = key.rsplit(".", 1)[-1]
            candidates = [m for m in missing
                          if quest_of(m) == q and m.endswith("." + suffix)]
            if candidates:
                hint = (f" -- {q} was renumbered; this text probably belongs "
                        f"on one of: {', '.join(candidates)}")
        add("W102", "warn", key,
            "not in english.json any more (stale)" + hint,
            lineno=target.lineno(key))

    for q, (first, suspects) in sorted(drifted.items(),
                                       key=lambda kv: int(kv[0][1:])):
        add("W103", "warn", q,
            f"a step was inserted or dropped at s{first}, so the {len(suspects)} "
            f"already-translated key(s) after it are probably attached to the "
            f"wrong step now -- re-read them against english.json",
            lineno=None, quest=q, first_affected_step=first, suspects=suspects)

    # --- per-key checks ----------------------------------------------------
    ratios = []
    shared = [k for k in en_keys if k in tr_set]
    for key in shared:
        tr_value = target.data[key]
        if isinstance(tr_value, str) and tr_value.strip():
            en_len = len(english.data[key])
            if en_len >= LENGTH_RATIO_MIN_CHARS:
                ratios.append(len(tr_value) / en_len)
    median_ratio = statistics.median(ratios) if len(ratios) >= 20 else None

    translated = 0
    for key in shared:
        en_value = english.data[key]
        tr_value = target.data[key]
        lineno = target.lineno(key)

        if not isinstance(tr_value, str):
            add("E004", "error", key,
                f"value is not a string (got {type(tr_value).__name__})",
                lineno=lineno)
            continue
        if not tr_value.strip():
            add("E007", "error", key, "value is empty", lineno=lineno)
            continue

        translated += 1

        en_ph, tr_ph = placeholders(en_value), placeholders(tr_value)
        if en_ph != tr_ph:
            add("E005", "error", key,
                f"placeholder mismatch (english has {en_ph or '[]'}, "
                f"translation has {tr_ph or '[]'})",
                lineno=lineno, english=en_value, translation=tr_value,
                english_line=english.lineno(key))

        bad = bad_placeholders(tr_value)
        if bad and not bad_placeholders(en_value):
            add("E006", "error", key,
                f"malformed placeholder(s) {bad}: only %s, %d and %% are "
                f"understood", lineno=lineno, translation=tr_value)

        if tr_value == en_value and looks_untranslated(key, en_value):
            add("W104", "warn", key, "identical to english (untranslated?)",
                lineno=lineno)
            translated -= 1

        if whitespace_signature(en_value) != whitespace_signature(tr_value):
            add("W105", "warn", key,
                "leading/trailing whitespace or line-break count differs "
                "from english", lineno=lineno)

        if median_ratio and len(en_value) >= LENGTH_RATIO_MIN_CHARS:
            ratio = len(tr_value) / len(en_value)
            off = (ratio / median_ratio if ratio > median_ratio
                   else median_ratio / ratio)
            if off >= LENGTH_RATIO_TOLERANCE:
                add("W106", "warn", key,
                    f"length is {ratio:.2f}x the english text where this file "
                    f"averages {median_ratio:.2f}x -- wrong text pasted in?",
                    lineno=lineno)

        if key in stale_keys:
            add("W107", "warn", key,
                "the english text changed since the revision given to "
                "--since; this translation needs a second look", lineno=lineno)

    stats = {
        "keys": len(en_keys),
        "translated": translated,
        "coverage": (translated / len(en_keys) * 100) if en_keys else 0.0,
        "missing": len(missing),
        "extra": len(extra),
    }
    return findings, stats


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
LEVEL_TAG = {"error": red("ERROR"), "warn": yellow("WARN")}
CATEGORY_TITLE = {
    "E001": "unreadable file",
    "E002": "bad top-level value",
    "E003": "duplicate keys",
    "E004": "non-string values",
    "E005": "placeholder mismatches",
    "E006": "malformed placeholders",
    "E007": "empty values",
    "W101": "not translated yet",
    "W102": "stale keys (no longer in english.json)",
    "W103": "renumbered quests -- re-check the surviving text",
    "W104": "identical to english",
    "W105": "whitespace / line-break differences",
    "W106": "suspicious length",
    "W107": "english changed since --since revision",
}


def source_line(lineno, line, highlight=False, color=red):
    if lineno is None or line is None:
        return None
    out = [f"        {dim(f'{lineno:>5} |')} {line}"]
    if highlight:
        carets, last_end = [], 0
        for m in PLACEHOLDER_RE.finditer(line):
            carets.append(" " * (m.start() - last_end)
                          + color("^" * (m.end() - m.start())))
            last_end = m.end()
        if carets:
            out.append(" " * len(f"        {lineno:>5} | ") + "".join(carets))
    return "\n".join(out)


def print_findings(english: LangFile, target: LangFile, findings, stats,
                   max_items: int):
    if not findings:
        print(f"[{green('OK')}] {bold(target.name)}: fully in sync with "
              f"{english.name} ({stats['keys']} keys)")
        return

    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]
    head = f"{bold(target.name)}: {len(errors)} error(s), {len(warns)} warning(s)"
    print(f"[{red('FAIL') if errors else green('OK')}] {head}")

    by_code = defaultdict(list)
    for f in findings:
        by_code[f.code].append(f)

    for code in sorted(by_code, key=lambda c: (c[0] != "E", c)):
        items = by_code[code]
        tag = LEVEL_TAG[items[0].level]
        print(f"  [{tag} {code}] {CATEGORY_TITLE.get(code, code)} "
              f"({len(items)}):")
        shown = items if max_items == 0 else items[:max_items]
        for f in shown:
            where = ""
            if f.lineno is not None:
                where = dim(f"  ({target.name}:{f.lineno})")
                if code == "W101":
                    where = dim(f"  ({english.name}:{f.lineno})")
            label = cyan(f.key) if f.key else ""
            print(f"    - {label}{' ' if label else ''}{f.message}{where}")
            if code == "W101":
                line = source_line(f.lineno,
                                   english.lines.get(f.key, (None, None))[1])
                if line:
                    print(line)
            elif code == "W103":
                suspects = f.extra.get("suspects", ())
                print(dim("        " + " ".join(suspects)))
            elif code == "E005":
                en_line = english.lines.get(f.key, (None, None))
                tr_line = target.lines.get(f.key, (None, None))
                if en_line[1]:
                    print(f"      {dim('english:')}")
                    print(source_line(en_line[0], en_line[1], True, green))
                if tr_line[1]:
                    print(f"      {dim('translation:')}")
                    print(source_line(tr_line[0], tr_line[1], True, red))
            elif f.lineno is not None:
                line = source_line(f.lineno,
                                   target.lines.get(f.key, (None, None))[1])
                if line:
                    print(line)
        if max_items and len(items) > max_items:
            print(dim(f"    ... and {len(items) - max_items} more "
                      f"(use --max 0 to see all)"))


def rjust(text, width, colorize=None):
    """Right-align on the *visible* width, then colorize."""
    pad = " " * max(0, width - len(text))
    return pad + (colorize(text) if colorize else text)


def print_table(rows):
    if not rows:
        return
    name_w = max(len(r["file"]) for r in rows) + 2
    print(bold("  coverage summary"))
    print(dim(f"  {'file'.ljust(name_w)}{'translated':>12}{'coverage':>10}"
              f"{'missing':>9}{'stale':>7}{'errors':>8}{'warnings':>10}"))
    for r in rows:
        cov_color = (green if r["coverage"] >= 99.5
                     else yellow if r["coverage"] >= 90 else red)
        print(f"  {r['file'].ljust(name_w)}"
              + rjust(f"{r['translated']}/{r['keys']}", 12)
              + rjust(f"{r['coverage']:.1f}%", 10, cov_color)
              + rjust(str(r["missing"]), 9)
              + rjust(str(r["extra"]), 7)
              + rjust(str(r["errors"]), 8, red if r["errors"] else green)
              + rjust(str(r["warnings"]), 10,
                      yellow if r["warnings"] else green))


def apply_patch(english: LangFile, target: LangFile, patch_path: Path,
                prune: bool):
    """Merge a `key<TAB>translation` TSV into a translation file.

    The result is always rewritten in english.json's key order, which keeps
    diffs between languages readable and makes the next --todo pass line up.
    """
    if target.error:
        print(f"[{red('ERROR')}] {target.name}: {target.error}")
        return 1

    updates = {}
    text = (sys.stdin.read() if str(patch_path) == "-"
            else patch_path.read_text(encoding="utf-8"))
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            print(f"[{red('ERROR')}] {patch_path}:{lineno}: expected "
                  f"'key<TAB>translation'")
            return 1
        key, value = parts[0].strip(), parts[1]
        if key not in english.data:
            print(f"[{yellow('WARN')}] {patch_path}:{lineno}: '{key}' is not "
                  f"in {english.name}; skipped")
            continue
        updates[key] = value

    merged = dict(target.data)
    merged.update(updates)

    ordered = {k: merged[k] for k in english.data if k in merged}
    dropped = [k for k in merged if k not in english.data]
    if not prune:
        for k in dropped:
            ordered[k] = merged[k]

    body = json.dumps(ordered, ensure_ascii=False, indent=2)
    target.path.write_text(body + "\n", encoding="utf-8", newline="\n")

    print(f"[{green('OK')}] {target.name}: {len(updates)} key(s) written, "
          f"reordered to match {english.name}"
          + (f", {len(dropped)} stale key(s) pruned" if prune and dropped
             else ""))
    if dropped and not prune:
        print(dim(f"       {len(dropped)} stale key(s) kept "
                  f"(pass --prune to drop them): {', '.join(dropped)}"))
    return 0


def emit_todo(english: LangFile, target: LangFile, findings, out):
    """TSV work list: everything a translator has to touch, in english order."""
    needs = {}
    for f in findings:
        if f.code == "W101":
            needs[f.key] = "missing"
        elif f.code == "W104":
            needs[f.key] = "untranslated"
        elif f.code == "W107":
            needs[f.key] = "english-changed"
        elif f.code == "W106":
            needs.setdefault(f.key, "suspicious-length")
        elif f.code == "W103":
            for k in f.extra.get("suspects", ()):
                needs.setdefault(k, "recheck-renumbered")
    order = {k: i for i, k in enumerate(english.data)}
    out.write("# key\treason\tenglish\tcurrent\n")
    for key in sorted(needs, key=lambda k: order.get(k, 1 << 30)):
        cur = target.data.get(key, "")
        if not isinstance(cur, str):
            cur = ""
        en = english.data.get(key, "")
        out.write(f"{key}\t{needs[key]}\t{en}\t{cur}\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Validate QuestPath translation files.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="translation files (default: every lang/*.json)")
    p.add_argument("--english", type=Path, default=None,
                   help="reference file (default: lang/english.json)")
    p.add_argument("--since", metavar="REV",
                   help="flag keys whose english text changed since git REV")
    p.add_argument("--todo", metavar="FILE", type=Path,
                   help="print a TSV work list for FILE and exit")
    p.add_argument("--apply", metavar="PATCH.tsv", type=Path,
                   help="merge a 'key<TAB>translation' TSV ('-' for stdin) "
                        "into the given translation file, rewriting it in "
                        "english.json key order")
    p.add_argument("--prune", action="store_true",
                   help="with --apply, also drop keys english.json no longer "
                        "has")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="machine-readable report on stdout")
    p.add_argument("--summary", action="store_true",
                   help="only print the coverage table")
    p.add_argument("--strict", action="store_true",
                   help="warnings fail the run too")
    p.add_argument("--max", type=int, default=25,
                   help="max findings shown per category (0 = all)")
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    global USE_COLOR
    if args.no_color or args.as_json:
        USE_COLOR = False

    root = Path(__file__).resolve().parent
    lang_dir = root / "lang"
    english_path = args.english or (lang_dir / "english.json")

    # Back-compat with the old positional form:
    #   validate.py lang/english.json lang/german.json
    files = list(args.files)
    if (args.english is None and len(files) >= 2
            and files[0].name == english_path.name):
        english_path, files = files[0], files[1:]

    if args.todo:
        targets = [args.todo]
    elif files:
        targets = files
    else:
        targets = sorted(p for p in lang_dir.glob("*.json")
                         if p.resolve() != english_path.resolve())
        if not targets:
            print(f"No translation files found next to {english_path}.")
            return 0

    english = LangFile(english_path)
    if english.error:
        print(f"[{red('ERROR')}] {english_path}: {english.error}")
        return 1

    if args.apply:
        if len(targets) != 1:
            print("--apply takes exactly one translation file")
            return 2
        return apply_patch(english, LangFile(targets[0]), args.apply,
                           args.prune)

    stale_keys = set()
    if args.since:
        stale_keys = english_changed_since(args.since, english_path,
                                           english.data)

    rows, report, any_error, any_warn = [], [], False, False
    for target_path in targets:
        target = LangFile(target_path)
        findings, stats = validate_file(english, target, stale_keys)

        if args.todo:
            emit_todo(english, target, findings, sys.stdout)
            return 0

        errors = sum(1 for f in findings if f.level == "error")
        warnings = sum(1 for f in findings if f.level == "warn")
        any_error |= errors > 0
        any_warn |= warnings > 0

        rows.append({"file": target.name, "errors": errors,
                     "warnings": warnings, **stats})
        report.append({"file": str(target_path), "stats": stats,
                       "findings": [f.as_dict() for f in findings]})

        if not args.as_json and not args.summary:
            print_findings(english, target, findings, stats, args.max)
            print()

    if args.as_json:
        json.dump({"english": str(english_path), "files": report},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_table(rows)

    if any_error:
        return 1
    return 1 if (args.strict and any_warn) else 0


if __name__ == "__main__":
    sys.exit(main())
