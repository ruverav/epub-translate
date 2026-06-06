# epub-translate

Translate EPUBs from one language to another using the public Google Translate API,
preserving HTML markup, links, footnotes and the complete book structure.

## Requirements

- Python 3.6+
- `requests` (`pip install -r requirements.txt`)

## Installation

```bash
git clone git@github.com:ruverav/epub-translate.git
cd epub-translate
pip install -r requirements.txt
```

## Usage

```bash
# Translate to Spanish (auto-detects source language)
python3 epub-translate.py book.epub --dest es

# Specify languages
python3 epub-translate.py book.epub --src en --dest fr
python3 epub-translate.py book.epub -s it -d es

# Already-extracted EPUB directory
python3 epub-translate.py ./epub_folder/ --dest pt

# Custom output
python3 epub-translate.py book.epub -d de -o book_german.epub
```

## Options

| Argument        | Description                                  | Default     |
|-----------------|----------------------------------------------|-------------|
| `input`         | EPUB or extracted directory                  | —           |
| `--src, -s`     | Source language (auto-detected if omitted)   | auto        |
| `--dest, -d`    | Target language (required)                   | —           |
| `--output, -o`  | Output file                                  | `{input}_{dest}.epub` |
| `--sleep`       | Pause between HTTP requests in seconds       | 0.12        |

## How it works

1. Reads `META-INF/container.xml` to locate `content.opf`
2. Extracts the EPUB with **safe extraction** (validates no path escapes the directory)
3. Locates the NCX from the OPF manifest (`media-type="application/x-dtbncx+xml"`)
4. Detects source language from HTML `lang` attributes
5. **Pre-count**: scans all XHTML/HTML files and counts translatable groups with `_SegmentFinder`
6. **Global progress bar**: `[████████────] 50% 123/246  file.xhtml`
   - On interactive terminals updates in-place with `\r`
   - On non-interactive output prints a summary every 5 %
   - Uses Unicode chars (`█`/`─`) if encoding supports it; ASCII fallback (`#`/`-`)
7. Walks files grouping text by blocks (`p`, `div`, `h1`–`h6`, `li`…) and translates each block individually
8. Skips non-translatable tags: `script`, `style`, `pre`, `code`, `kbd`, `samp`, `svg`
9. Rebuilds the EPUB (with `mimetype` stored uncompressed, first in the ZIP)

## Security

- **ZIP traversal**: member-by-member extraction verifying with `relative_to()` that no path escapes the target directory
- **OPF/NCX paths**: `_safe_join()` resolves and validates that `full-path` in `container.xml` and `href` in the manifest do not point outside the EPUB
- **Corrupt EPUB**: `testzip()` detects damaged files before extracting

## Limitations

- Requires an internet connection
- Google Translate's free API has usage limits; very large books
  may hit rate-limiting
- Machine translation: does not replace human review
- Does not handle DRM or encrypted formats
- stdlib `html.parser` is correct but modest; EPUBs with very irregular
  HTML may need `beautifulsoup4`

## License

MIT
