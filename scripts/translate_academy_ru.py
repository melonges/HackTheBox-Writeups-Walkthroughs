#!/usr/bin/env python3
"""
Translate HTB Academy markdown notes to Russian while preserving technical content.

Key behavior:
- Mirrors directory structure from source to destination.
- Creates destination filenames with ` (RU)` suffix.
- Rewrites Obsidian wiki links to point at RU note names.
- Preserves code blocks, inline code, URLs, markdown links, and image embeds.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from deep_translator import GoogleTranslator


SOURCE_DEFAULT = Path("Write-ups/Academy")
DEST_DEFAULT = Path("Write-ups/Academy-RU")

WIKI_LINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
ALPHA_RE = re.compile(r"[A-Za-z]")
CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
FILE_EXT_RE = re.compile(
    r"\.(md|png|jpe?g|gif|svg|webp|zip|pdf|txt|csv|json|ya?ml|xml|mp4|mov)$",
    flags=re.IGNORECASE,
)

# Keep these labels consistent across notes.
LABEL_TRANSLATIONS = {
    "Tags:": "Теги:",
    "Related to:": "Связано с:",
    "See also:": "См. также:",
    "Previous:": "Предыдущее:",
}

# These regexes are protected before translation and restored after.
PROTECTED_PATTERNS = [
    re.compile(r"`[^`\n]+`"),  # inline code
    re.compile(r"!\[\[[^\]]+\]\]"),  # obsidian image embed
    re.compile(r"\[\[[^\]]+\]\]"),  # obsidian wiki link
    re.compile(r"!\[[^\]]*\]\([^)]+\)"),  # markdown image
    re.compile(r"\[[^\]]*\]\([^)]+\)"),  # markdown link
    re.compile(r"https?://[^\s)>\]]+"),  # raw URL
    re.compile(r"<[^>\n]+>"),  # inline html tags
]


@dataclass(frozen=True)
class LinkIndex:
    rel_to_ru: dict[str, str]
    stem_to_ru: dict[str, str]
    ambiguous_stems: set[str]


class TranslationContext:
    def __init__(self, translator: object):
        self.translator = translator
        self.cache: dict[str, str] = {}

    def translate_text(self, text: str) -> str:
        if not text:
            return text
        cached = self.cache.get(text)
        if cached is not None:
            return cached
        translated = self._translate_with_retry(text)
        self.cache[text] = translated
        return translated

    def _translate_with_retry(self, text: str) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                translated = self.translator.translate(text)
                if translated is None:
                    raise RuntimeError("Translator returned empty result")
                return translated
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1.0 + attempt)
        raise RuntimeError(f"Translation failed after retries: {last_error}") from last_error


def build_link_index(markdown_files: list[Path], source_root: Path) -> LinkIndex:
    rel_to_ru: dict[str, str] = {}
    stems: dict[str, list[str]] = {}
    for src in markdown_files:
        rel = src.relative_to(source_root)
        rel_no_ext = rel.with_suffix("").as_posix()
        ru_rel = rel.with_name(f"{src.stem} (RU)").with_suffix("").as_posix()
        rel_to_ru[rel_no_ext] = ru_rel
        stems.setdefault(src.stem, []).append(rel_no_ext)

    stem_to_ru = {}
    ambiguous = set()
    for stem, rels in stems.items():
        if len(rels) == 1:
            stem_to_ru[stem] = f"{stem} (RU)"
        else:
            ambiguous.add(stem)

    return LinkIndex(rel_to_ru=rel_to_ru, stem_to_ru=stem_to_ru, ambiguous_stems=ambiguous)


def normalize_target(target: str) -> str:
    normalized = target.strip().replace("\\", "/")
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    normalized = normalized.lstrip("./")
    normalized = normalized.strip("/")
    return normalized


def resolve_note_target(target: str, current_rel: Path, index: LinkIndex) -> str | None:
    candidate = normalize_target(target)
    if not candidate:
        return None

    candidates: list[str] = []

    def add(item: str) -> None:
        item = normalize_target(item)
        if item and item not in candidates:
            candidates.append(item)

    add(candidate)

    if candidate.startswith("Write-ups/Academy/"):
        add(candidate[len("Write-ups/Academy/") :])
    if candidate.startswith("Academy/"):
        add(candidate[len("Academy/") :])
    if "/Academy/" in candidate:
        add(candidate.split("/Academy/", 1)[1])

    # Relative resolution for path-style links
    if "/" in candidate:
        current_dir = current_rel.parent.as_posix()
        joined = posixpath.normpath(posixpath.join(current_dir, candidate))
        if not joined.startswith(".."):
            add(joined)

    for item in candidates:
        if item in index.rel_to_ru:
            return index.rel_to_ru[item]

    # For bare links, only rewrite when unambiguous.
    if "/" not in candidate and candidate in index.stem_to_ru:
        return index.stem_to_ru[candidate]

    return None


def should_skip_link_target(target: str) -> bool:
    raw = target.strip()
    if not raw:
        return True
    if raw.startswith(":") or raw.endswith(":"):
        return True
    if raw.startswith("#"):
        return True
    if re.match(r"^[a-zA-Z]+://", raw):
        return True
    if FILE_EXT_RE.search(raw):
        return True
    return False


def rewrite_wiki_links(line: str, current_rel: Path, index: LinkIndex) -> str:
    def replacer(match: re.Match[str]) -> str:
        inner = match.group(1)
        if "|" in inner:
            target_part, alias = inner.split("|", 1)
        else:
            target_part, alias = inner, ""

        if "#" in target_part:
            base_target, heading = target_part.split("#", 1)
            heading_suffix = f"#{heading}"
        else:
            base_target = target_part
            heading_suffix = ""

        if should_skip_link_target(base_target):
            return match.group(0)

        resolved = resolve_note_target(base_target, current_rel=current_rel, index=index)
        if not resolved:
            return match.group(0)

        rewritten = f"{resolved}{heading_suffix}"
        if alias:
            rewritten = f"{rewritten}|{alias}"
        return f"[[{rewritten}]]"

    return WIKI_LINK_RE.sub(replacer, line)


def protect_tokens(text: str) -> tuple[str, dict[str, str]]:
    tokens: dict[str, str] = {}
    protected = text
    token_id = 0

    for pattern in PROTECTED_PATTERNS:
        while True:
            match = pattern.search(protected)
            if not match:
                break
            original = match.group(0)
            placeholder = f"@@CODXPH{token_id:06d}@@"
            token_id += 1
            tokens[placeholder] = original
            protected = f"{protected[:match.start()]}{placeholder}{protected[match.end():]}"

    return protected, tokens


def restore_tokens(text: str, tokens: dict[str, str]) -> str:
    restored = text
    for placeholder, original in tokens.items():
        restored = restored.replace(placeholder, original)
    return restored


def is_visual_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.fullmatch(r"(\*\s*){3,}", stripped):
        return True
    if re.fullmatch(r"[-=_]{3,}", stripped):
        return True
    return False


def translate_fragment(text: str, ctx: TranslationContext) -> str:
    if not text or not ALPHA_RE.search(text):
        return text

    protected, tokens = protect_tokens(text)
    if not ALPHA_RE.search(protected):
        return restore_tokens(protected, tokens)

    leading_ws = re.match(r"^\s*", protected).group(0)
    trailing_ws = re.search(r"\s*$", protected).group(0)
    core = protected[len(leading_ws) : len(protected) - len(trailing_ws)] if protected else ""
    if not core:
        return text

    translated_core = ctx.translate_text(core)
    translated = f"{leading_ws}{translated_core}{trailing_ws}"
    return restore_tokens(translated, tokens)


def translate_table_line(line: str, ctx: TranslationContext) -> str:
    # Keep markdown table separators untouched.
    if re.fullmatch(r"\s*\|?[\s:\-]+\|[\s:\-|]*\s*", line):
        return line
    parts = line.split("|")
    return "|".join(translate_fragment(part, ctx) for part in parts)


def maybe_translate_label(line: str) -> str:
    for source, target in LABEL_TRANSLATIONS.items():
        if line.startswith(source):
            return f"{target}{line[len(source):]}"
    return line


def translate_markdown(text: str, current_rel: Path, index: LinkIndex, ctx: TranslationContext) -> str:
    lines = text.splitlines(keepends=True)
    translated_lines: list[str] = []
    in_fenced_block = False
    active_fence = ""

    for line in lines:
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line

        if CODE_FENCE_RE.match(body):
            fence = CODE_FENCE_RE.match(body).group(1)
            if not in_fenced_block:
                in_fenced_block = True
                active_fence = fence
            elif fence == active_fence:
                in_fenced_block = False
                active_fence = ""
            translated_lines.append(line)
            continue

        if in_fenced_block:
            translated_lines.append(line)
            continue

        rewritten = rewrite_wiki_links(body, current_rel=current_rel, index=index)
        rewritten = maybe_translate_label(rewritten)

        if not rewritten.strip():
            translated_lines.append(rewritten + newline)
            continue

        if is_visual_separator(rewritten):
            translated_lines.append(rewritten + newline)
            continue

        stripped = rewritten.lstrip()
        if stripped.startswith("![[") or stripped.startswith("!["):
            translated_lines.append(rewritten + newline)
            continue

        if rewritten.strip().startswith("|") and rewritten.count("|") >= 2:
            translated_lines.append(translate_table_line(rewritten, ctx) + newline)
            continue

        translated_lines.append(translate_fragment(rewritten, ctx) + newline)

    return "".join(translated_lines)


def destination_for(source_file: Path, source_root: Path, dest_root: Path) -> Path:
    rel = source_file.relative_to(source_root)
    return dest_root / rel.with_name(f"{source_file.stem} (RU){source_file.suffix}")


def build_translator(from_code: str, to_code: str) -> object:
    return GoogleTranslator(source=from_code, target=to_code)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Translate Academy markdown notes to Russian RU copies."
    )
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT, help="Source Academy root.")
    parser.add_argument("--dest", type=Path, default=DEST_DEFAULT, help="Destination RU root.")
    parser.add_argument("--from-code", default="en", help="Source language code.")
    parser.add_argument("--to-code", default="ru", help="Target language code.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned actions only.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip destination files that already exist.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Process only first N markdown files (0 = all).",
    )
    args = parser.parse_args()

    source_root = args.source.resolve()
    dest_root = args.dest.resolve()
    if not source_root.exists():
        print(f"Source path does not exist: {source_root}", file=sys.stderr)
        return 2

    markdown_files = sorted(source_root.rglob("*.md"))
    if args.max_files > 0:
        markdown_files = markdown_files[: args.max_files]

    if not markdown_files:
        print("No markdown files found to process.", file=sys.stderr)
        return 1

    link_index = build_link_index(markdown_files, source_root=source_root)

    if args.dry_run:
        print(f"Source: {source_root}")
        print(f"Dest:   {dest_root}")
        print(f"Files:  {len(markdown_files)}")
        for i, src in enumerate(markdown_files, start=1):
            dst = destination_for(src, source_root=source_root, dest_root=dest_root)
            state = "skip (exists)" if args.resume and dst.exists() else "write"
            print(f"[{i:03d}] {state} {src.relative_to(source_root)} -> {dst.relative_to(dest_root)}")
        return 0

    translator = build_translator(from_code=args.from_code, to_code=args.to_code)
    ctx = TranslationContext(translator)

    total = len(markdown_files)
    written = 0
    skipped = 0

    for idx, src in enumerate(markdown_files, start=1):
        dst = destination_for(src, source_root=source_root, dest_root=dest_root)
        if args.resume and dst.exists():
            skipped += 1
            print(f"[{idx}/{total}] skip {src.relative_to(source_root)}")
            continue

        src_text = src.read_text(encoding="utf-8")
        translated_text = translate_markdown(
            src_text,
            current_rel=src.relative_to(source_root).with_suffix(""),
            index=link_index,
            ctx=ctx,
        )

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(translated_text, encoding="utf-8")
        written += 1
        print(f"[{idx}/{total}] wrote {dst.relative_to(dest_root)}")

    print(f"Done. written={written}, skipped={skipped}, total={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
