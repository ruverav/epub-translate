#!/usr/bin/env python3
"""
epub-translate — Translate EPUBs between languages using Google Translate.

Usage:
    python3 epub-translate.py book.epub --dest es
    python3 epub-translate.py book.epub --src it --dest es
    python3 epub-translate.py book.epub --dest fr -o translated.epub
    python3 epub-translate.py extracted_dir/ --dest pt

Requires: Python 3.8+, requests
"""
import argparse
import html.parser
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    print("Error: requires 'requests'.  pip install requests")
    sys.exit(1)

VERSION = ""

# Reusable HTTP session (reduces connection overhead)
SESSION = requests.Session()
# Simple translation cache (repeated text saves API calls)
CACHE = {}
SKIP_TAGS = frozenset({"script", "style", "pre", "code", "kbd", "samp", "svg"})
# Block-level tags: group text segments inside them
BLOCK_TAGS = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "td", "th", "dt", "dd", "caption",
})
LANG_ATTR_RE = re.compile(r'\b(lang|xml:lang)="[a-z]{2,3}(-[a-z]{2,3})?"')


# ──────────────────────── Progress bar ────────────────────────

class ProgressBar:
    """Global progress bar for translation.

    On interactive terminals (TTY) uses ``\\r`` to draw on the same line.
    On non-interactive output only prints when the percentage bracket changes.

    Bar characters are ASCII by default (``#`` / ``-``);
    if the output encoding supports them they switch to Unicode (``█`` / ``─``).
    """
    def __init__(self, total, width=40):
        self.total = total
        self.current = 0
        self.width = width
        self.is_tty = sys.stderr.isatty()
        self._file = ""
        self._last_pct = -1

        # Detect Unicode compatibility
        try:
            '█'.encode(sys.stderr.encoding or 'utf-8')
            self._fill = "█"
            self._empty = "─"
        except (UnicodeEncodeError, TypeError, LookupError):
            self._fill = "#"
            self._empty = "-"

    def set_file(self, name):
        """Indicates which file is currently being processed."""
        self._file = name

    def update(self, n=1):
        """Advances counter and redraws."""
        self.current += n
        self._draw()

    def _draw(self):
        pct = self.current / self.total if self.total else 0
        pct_int = int(pct * 100)
        filled = int(self.width * pct)
        bar = self._fill * filled + self._empty * (self.width - filled)
        label = f"{pct_int}% {self.current}/{self.total}"
        if self.is_tty:
            msg = f"\r  [{bar}] {label}"
            if self._file:
                msg += f"  {self._file}"
            sys.stderr.write(msg)
            sys.stderr.flush()
        else:
            # 5 % brackets → only print when the bracket changes
            bracket = (pct_int // 5) * 5
            if bracket != self._last_pct or self.current in (1, self.total):
                self._last_pct = bracket
                extra = f"  [{self._file}]" if self._file else ""
                print(f"  [{bar}] {label}{extra}")

    def done(self):
        """Closes the progress line."""
        if self.is_tty:
            sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            # Non-TTY: ensure 100 % is shown if not printed yet
            if self._last_pct < 100:
                self._draw()
            print()


def count_groups(html_text):
    """Counts how many translatable groups (blocks) exist in HTML text."""
    from itertools import groupby
    finder = _SegmentFinder(html_text)
    finder.feed(html_text)
    if not finder.segments:
        return 0
    return len([1 for _ in groupby(finder.segments, key=lambda s: s[3])])


# ─────────────────────────────── API ───────────────────────────────

def translate(text, src, dest, retries=3):
    if not text or not text.strip():
        return text

    # Skip fragments without letters (references, numbers, punctuation, etc.)
    # to avoid pointless API calls and HTTP 500 on bare numerics.
    if not any(ch.isalpha() for ch in text):
        return text

    key = (src, dest, text)
    cached = CACHE.get(key)
    if cached is not None:
        return cached
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": src, "tl": dest, "dt": "t", "q": text}
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=60)
            r.raise_for_status()
            result = "".join(item[0] for item in r.json()[0])
            CACHE[key] = result
            return result
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429:
                wait = 5 * (2 ** attempt)
                print(f"\n  [rate-limit] waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"  [http {r.status_code}] {e}")
            CACHE[key] = text
            return text
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                wait = 3 * (2 ** attempt)
                print(f"\n  [network-error] retrying in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  [network-error] {e}")
            CACHE[key] = text
            return text
        except Exception as e:
            print(f"  [error] {e}")
            CACHE[key] = text
            return text
    CACHE[key] = text
    return text


# ──────────────────────── HTML parser (no regex) ────────────────────────

class _SegmentFinder(html.parser.HTMLParser):
    """Walks HTML locating translatable text segments.
    Each segment carries a group_id: segments in the same block
    (p, div, h1–h6, li…) share an id so they can be grouped."""
    def __init__(self, source_html):
        super().__init__(convert_charrefs=False)
        self.source = source_html
        self.segments = []        # (start, end, text, group_id)
        self.skip_depth = 0
        self.block_depth = 0
        self._group = 0
        self._last_block_depth = 0
        self._cursor = 0

    def _skip(self):
        return self.skip_depth > 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.block_depth += 1
            self._group += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.block_depth -= 1
            self._group += 1

    def handle_data(self, data):
        if self._skip() or not data.strip():
            return
        idx = self.source.find(data, self._cursor)
        if idx == -1:
            idx = self.source.find(data)
        if idx >= 0:
            self.segments.append((idx, idx + len(data), data, self._group))
            self._cursor = idx + len(data)

    # We do not translate HTML entities (e.g. &amp; &eacute;)
    # to avoid breaking markup or producing false positives
    # when looking up escaped text in the original HTML.
    def handle_charref(self, name):
        pass

    def handle_entityref(self, name):
        pass


def translate_body(html_text, src, dest, on_group_done=None, sleep_secs=0.12):
    """Translates text nodes grouped by block, in reverse order.

    Each fragment is translated individually. Progress (``on_group_done``)
    advances once per block, when all its fragments are done.
    ``sleep_secs`` pauses between HTTP requests to avoid rate-limiting.
    """
    finder = _SegmentFinder(html_text)
    finder.feed(html_text)

    if not finder.segments:
        return html_text

    from itertools import groupby
    groups = [list(grp) for _, grp in groupby(finder.segments, key=lambda s: s[3])]

    chars = list(html_text)

    for group in reversed(groups):
        fragments = [(s, e, t) for (s, e, t, _) in group]
        for s, e, t in reversed(fragments):
            traducido = translate(t, src, dest)
            chars[s:e] = list(traducido)
        if on_group_done:
            on_group_done()
        time.sleep(sleep_secs)

    return "".join(chars)


# ──────────────────────── Individual files ────────────────────────

def translate_file(path, src, dest, progress=None, sleep_secs=0.12):
    """Translates a single XHTML/HTML file.

    ``progress`` is a ``ProgressBar`` instance (or ``None``).
    """
    if progress is not None:
        progress.set_file(path.name)
    else:
        print(f"\n  › {path.name}")
    text = path.read_text(encoding="utf-8")

    # Update language attributes
    text = LANG_ATTR_RE.sub(lambda m: f'{m.group(1)}="{dest}"', text)

    body_match = re.search(r'<body[^>]*>', text)
    if not body_match:
        if progress is None:
            print("  [skipped] no <body>")
        return text
    body_start = body_match.end()
    body_end = text.find("</body>")
    if body_end == -1:
        if progress is None:
            print("  [error] </body> not found")
        return text

    inner = text[body_start:body_end]
    if progress is not None:
        on_done = progress.update
    else:
        on_done = None

    translated = translate_body(inner, src, dest, on_group_done=on_done,
                                sleep_secs=sleep_secs)
    text = text[:body_start] + translated + text[body_end:]

    path.write_text(text, encoding="utf-8")
    return text


# ──────────────────────── Metadata ────────────────────────

def _safe_join(base, rel_path):
    """Resolves rel_path inside base and validates it does not escape."""
    resolved = (base.resolve() / rel_path).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise ValueError(
            f"Unsafe path: '{rel_path}' escapes '{base}'")
    return resolved


def find_opf(epub_dir):
    """Locates the OPF by reading META-INF/container.xml."""
    container = epub_dir / "META-INF" / "container.xml"
    if not container.exists():
        # Direct search as a fallback
        for name in ("content.opf", "metadata.opf", "package.opf", "book.opf"):
            for f in epub_dir.rglob(name):
                return f
        return None

    try:
        tree = ET.parse(container)
        root = tree.getroot()
        ns = {
            "c": "urn:oasis:names:tc:opendocument:xmlns:container",
            "opf": "http://www.idpf.org/2007/opf",
        }
        rootfile = root.find(".//c:rootfile", ns)
        if rootfile is not None:
            path = rootfile.get("full-path")
            if path:
                return _safe_join(epub_dir, path)
    except ET.ParseError:
        pass
    return None


def update_opf_lang(opf_path, dest):
    """Updates <dc:language> in the OPF."""
    if not opf_path or not opf_path.exists():
        return
    text = opf_path.read_text(encoding="utf-8")
    text = re.sub(r'<dc:language>[^<]*</dc:language>',
                  f"<dc:language>{dest}</dc:language>", text)
    opf_path.write_text(text, encoding="utf-8")
    print(f"  ✓ {opf_path.name}")


def find_ncx(opf_path, epub_dir):
    """Finds the NCX from the OPF manifest or by searching."""
    if opf_path and opf_path.exists():
        try:
            tree = ET.parse(opf_path)
            root = tree.getroot()
            ns = {"opf": "http://www.idpf.org/2007/opf"}
            for item in root.findall(".//opf:item", ns):
                if item.get("media-type") == "application/x-dtbncx+xml":
                    href = item.get("href", "")
                    candidate = (opf_path.parent / href).resolve()
                    # Validate against EPUB root, not just the OPF folder
                    try:
                        candidate.relative_to(epub_dir.resolve())
                    except ValueError:
                        continue
                    if candidate.exists():
                        return candidate
        except (ET.ParseError, ValueError):
            pass

    # Fallback: search for any .ncx file
    for f in sorted(epub_dir.rglob("*.ncx")):
        return f
    return None


def update_ncx_lang(opf_path, epub_dir, dest):
    """Updates xml:lang in the NCX."""
    ncx = find_ncx(opf_path, epub_dir)
    if ncx is None:
        print("  - toc.ncx: not found")
        return
    text = ncx.read_text(encoding="utf-8")
    if 'xml:lang="' not in text:
        print(f"  - toc.ncx: no xml:lang attribute")
        return
    text = re.sub(r'xml:lang="[^"]*"', f'xml:lang="{dest}"', text)
    ncx.write_text(text, encoding="utf-8")
    print(f"  ✓ {ncx.relative_to(epub_dir)}")


# ──────────────────────── Source language ────────────────────────

def detect_lang_from_files(epub_dir):
    """Detects language from XHTML files."""
    for f in sorted(epub_dir.rglob("*.xhtml")) + sorted(epub_dir.rglob("*.html")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'lang="([a-z]{2,3}(?:-[a-z]{2,3})?)"', text)
        if m:
            return m.group(1).split("-")[0]
    return None


# ──────────────────────── EPUB ────────────────────────

def _safe_extract(zf, dst_dir):
    """Extracts ZIP members while checking none escape the target directory."""
    dst = Path(dst_dir).resolve()
    for info in zf.infolist():
        dest_path = (dst / info.filename).resolve()
        try:
            dest_path.relative_to(dst)
        except ValueError:
            raise ValueError(
                f"Unsafe path in EPUB: {info.filename}")
        zf.extract(info, dst_dir)


def extract_epub(epub, dst_dir):
    """Extracts EPUB to a directory.  Raises ValueError if invalid."""
    if not zipfile.is_zipfile(epub):
        raise ValueError("File is not a valid ZIP/EPUB")
    try:
        with zipfile.ZipFile(epub, "r") as z:
            bad = z.testzip()
            if bad:
                raise ValueError(f"Corrupt EPUB: first bad file is {bad}")
            _safe_extract(z, dst_dir)
    except zipfile.BadZipFile as e:
        raise ValueError(f"Invalid EPUB: {e}")


def build_epub(src_dir, output):
    """Rebuilds EPUB from a directory."""
    mime = src_dir / "mimetype"
    if not mime.exists():
        raise ValueError(
            "mimetype not found; directory does not look like a valid EPUB")
    entries = ["mimetype"]

    src_str = str(src_dir)
    for root, _dirs, files in os.walk(src_dir):
        for f in sorted(files):
            abspath = os.path.join(root, f)
            entry = os.path.relpath(abspath, src_str).replace("\\", "/")
            if entry not in entries:
                entries.append(entry)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            abspath = src_dir / entry
            compress = zipfile.ZIP_STORED if entry == "mimetype" else zipfile.ZIP_DEFLATED
            zf.write(abspath, entry, compress_type=compress)

    print(f"\n  EPUB → {output}  ({output.stat().st_size / 1024:.0f} KB)")


# ──────────────────────── Main flow ────────────────────────

def _count_total_groups(files, dest):
    """Counts total translatable groups across a list of files."""
    total = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        text = LANG_ATTR_RE.sub(lambda m: f'{m.group(1)}="{dest}"', text)
        body_match = re.search(r'<body[^>]*>', text)
        if not body_match:
            continue
        body_end = text.find("</body>")
        if body_end == -1:
            continue
        body_start = body_match.end()
        total += count_groups(text[body_start:body_end])
    return total


def translate_epub(input_path, src, dest, output, sleep_secs=0.12):
    """Translates a complete EPUB."""
    print(f"  Source:  {input_path}")
    print(f"  Target:  {dest}")
    tmp = Path(tempfile.mkdtemp(prefix="epub-trans-"))
    try:
        extract_epub(input_path, tmp)

        # Locate OPF
        opf = find_opf(tmp)
        if opf:
            print(f"  OPF:     {opf.relative_to(tmp)}")
        else:
            print("  OPF:     not found")

        # Files to translate
        xhtml_files = sorted(tmp.rglob("*.xhtml")) + sorted(tmp.rglob("*.html"))
        if not xhtml_files:
            print("  No XHTML/HTML files found.")
            return

        # Detect source language
        src_lang = src or detect_lang_from_files(tmp) or "auto"
        print(f"  Language: {src_lang or 'auto'} → {dest}")

        # Count groups and show global progress bar
        total = _count_total_groups(xhtml_files, dest)
        pb = ProgressBar(total) if total else None
        if pb:
            print(f"  Groups:  {total} blocks  (sleep={sleep_secs}s)")
            print()

        try:
            for f in xhtml_files:
                translate_file(f, src_lang, dest, progress=pb,
                               sleep_secs=sleep_secs)
        finally:
            if pb:
                pb.done()

        update_opf_lang(opf, dest)
        update_ncx_lang(opf, tmp, dest)

        build_epub(tmp, output)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def translate_directory(dir_path, src, dest, output, sleep_secs=0.12):
    """Translates an already-extracted EPUB directory."""
    work = Path(dir_path)

    opf = find_opf(work)
    xhtml_files = sorted(work.rglob("*.xhtml")) + sorted(work.rglob("*.html"))

    src_lang = src or detect_lang_from_files(work) or "auto"
    print(f"  Language: {src_lang} → {dest}")

    total = _count_total_groups(xhtml_files, dest)
    pb = ProgressBar(total) if total else None
    if pb:
        print(f"  Groups:  {total} blocks  (sleep={sleep_secs}s)")
        print()

    try:
        for f in xhtml_files:
            translate_file(f, src_lang, dest, progress=pb,
                           sleep_secs=sleep_secs)
    finally:
        if pb:
            pb.done()

    update_opf_lang(opf, dest)
    update_ncx_lang(opf, work, dest)

    if output:
        build_epub(work, output)
    else:
        print("  Directory translated in-place.")


# ──────────────────────── CLI ────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="epub-translate – Translate EPUBs between languages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="EPUB or extracted EPUB directory")
    parser.add_argument("--src", "-s", default=None,
                        help="Source language (auto if omitted)")
    parser.add_argument("--dest", "-d", required=True,
                        help="Target language (e.g. es, fr, de, pt, en)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file (default: {input}_{dest}.epub)")
    parser.add_argument("--sleep", type=float, default=0.12,
                        help="Pause between HTTP requests in seconds (default 0.12)")
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        print(f"Error: '{inp}' does not exist.")
        sys.exit(1)

    output = args.output
    if output is None and inp.suffix.lower() in (".epub", ".zip"):
        stem = inp.stem.replace(" ", "_")
        output = str(inp.with_name(f"{stem}_{args.dest}.epub"))
    elif output is None:
        output = str(inp / f"translated_{args.dest}")

    try:
        sleep_secs = max(0.0, args.sleep)
        if inp.is_file() and inp.suffix.lower() in (".epub", ".zip"):
            translate_epub(inp, args.src, args.dest, Path(output), sleep_secs)
        elif inp.is_dir():
            translate_directory(inp, args.src, args.dest, Path(output), sleep_secs)
        else:
            print(f"Error: '{inp}' must be an EPUB or directory.")
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
