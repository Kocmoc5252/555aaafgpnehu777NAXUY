from __future__ import annotations

import logging
import random
import textwrap
import time
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .brain import clean_text

try:  # pilmoji дает нормальные emoji поверх Pillow, если установлен и есть сеть/кэш.
    from pilmoji import Pilmoji  # type: ignore
except Exception:  # noqa: BLE001
    Pilmoji = None  # type: ignore

log = logging.getLogger(__name__)

TEMPLATE_PATTERNS = [
    "meme*.jpg",
    "meme*.jpeg",
    "meme*.png",
    "mem*.jpg",
    "mem*.jpeg",
    "mem*.png",
]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
]


class MemeGenerator:
    def __init__(self, *, project_root: Path, memes_dir: Path, output_dir: Path, font_path: str | None = None) -> None:
        self.project_root = project_root
        self.memes_dir = memes_dir
        self.output_dir = output_dir
        self.font_path = font_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def find_templates(self) -> list[Path]:
        roots = [self.project_root, self.memes_dir]
        found: dict[Path, None] = {}
        for root in roots:
            if not root.exists():
                continue
            for pattern in TEMPLATE_PATTERNS:
                for path in root.glob(pattern):
                    if path.is_file():
                        found[path.resolve()] = None
        return list(found.keys())

    def random_template(self) -> Path | None:
        templates = self.find_templates()
        return random.choice(templates) if templates else None

    def random_poll_media(self) -> Path | None:
        if random.random() > 0.7:
            return None
        return self.random_template()

    def generate(self, text: str, *, position: str | None = None) -> Path:
        template = self.random_template()
        if template:
            image = Image.open(template)
            image = ImageOps.exif_transpose(image).convert("RGB")
        else:
            image = Image.new("RGB", (1080, 1080), (35, 35, 35))

        image = self._limit_size(image)
        text = self._prepare_text(text)
        position = position if position in {"top", "bottom"} else random.choice(["top", "bottom"])
        self._draw_meme_text(image, text, position=position)

        output = self.output_dir / f"meme_{int(time.time())}_{random.randint(1000, 9999)}.jpg"
        image.save(output, "JPEG", quality=92, optimize=True)
        return output

    def _limit_size(self, image: Image.Image) -> Image.Image:
        max_side = 1600
        width, height = image.size
        if max(width, height) <= max_side:
            return image
        scale = max_side / max(width, height)
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        return image.resize(new_size, Image.Resampling.LANCZOS)

    def _prepare_text(self, text: str) -> str:
        text = clean_text(text)
        if not text:
            raise ValueError("empty meme text")
        if len(text) > 180:
            text = text[:177].rstrip() + "..."
        return text

    def _font_path(self) -> str | None:
        if self.font_path:
            candidate = Path(self.font_path)
            if candidate.exists():
                return str(candidate)
            log.warning("FONT_PATH задан, но файл не найден: %s", self.font_path)
        for candidate in FONT_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        return None

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        path = self._font_path()
        if path:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось загрузить шрифт %s: %s", path, exc)
        return ImageFont.load_default()

    def _wrap_lines(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return [text]
        lines: list[str] = []
        current = ""
        for word in words:
            proposed = word if not current else f"{current} {word}"
            if self._text_width(draw, proposed, font) <= max_width:
                current = proposed
                continue
            if current:
                lines.append(current)
            if self._text_width(draw, word, font) <= max_width:
                current = word
            else:
                lines.extend(self._split_long_word(draw, word, font, max_width))
                current = ""
        if current:
            lines.append(current)
        return lines or [text]

    def _split_long_word(self, draw: ImageDraw.ImageDraw, word: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        chunks: list[str] = []
        current = ""
        for char in word:
            proposed = current + char
            if current and self._text_width(draw, proposed, font) > max_width:
                chunks.append(current)
                current = char
            else:
                current = proposed
        if current:
            chunks.append(current)
        return chunks

    def _text_width(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
        try:
            return int(draw.textlength(text, font=font))
        except Exception:  # noqa: BLE001
            box = draw.textbbox((0, 0), text, font=font)
            return box[2] - box[0]

    def _line_height(self, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
        box = draw.textbbox((0, 0), "Ай|gj🙂", font=font, stroke_width=2)
        return max(18, box[3] - box[1])

    def _fit_text(self, draw: ImageDraw.ImageDraw, text: str, width: int, height: int) -> tuple[ImageFont.ImageFont, list[str], int]:
        max_width = int(width * 0.90)
        max_height = int(height * 0.38)
        start_size = max(28, min(92, int(width / 11)))
        min_size = 22
        for size in range(start_size, min_size - 1, -3):
            font = self._load_font(size)
            lines = self._wrap_lines(draw, text, font, max_width)
            line_height = self._line_height(draw, font)
            total_height = len(lines) * line_height + max(0, len(lines) - 1) * int(size * 0.16)
            if total_height <= max_height and len(lines) <= 5:
                return font, lines, line_height
        font = self._load_font(min_size)
        raw_lines = self._wrap_lines(draw, text, font, max_width)
        lines = raw_lines[:5]
        if len(raw_lines) > 5:
            lines[-1] = textwrap.shorten(lines[-1] + " ...", width=38, placeholder="...")
        return font, lines, self._line_height(draw, font)

    def _draw_meme_text(self, image: Image.Image, text: str, *, position: str) -> None:
        width, height = image.size
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        draw = ImageDraw.Draw(image)

        font, lines, line_height = self._fit_text(draw, text.upper() if random.random() < 0.55 else text, width, height)
        gap = max(4, int(line_height * 0.16))
        block_height = len(lines) * line_height + max(0, len(lines) - 1) * gap
        padding_y = max(18, int(height * 0.028))
        box_height = block_height + padding_y * 2

        if position == "top":
            y0 = 0
            y = padding_y
        else:
            y0 = height - box_height
            y = y0 + padding_y

        overlay_draw.rectangle((0, y0, width, y0 + box_height), fill=(0, 0, 0, 118))
        image_rgba = image.convert("RGBA")
        image_rgba.alpha_composite(overlay)
        image.paste(image_rgba.convert("RGB"))

        for line in lines:
            line_width = self._text_width(draw, line, font)
            x = max(10, int((width - line_width) / 2))
            self._draw_text_with_emoji(image, (x, y), line, font)
            y += line_height + gap

    def _outline_offsets(self, stroke_width: int) -> Iterable[tuple[int, int]]:
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx == 0 and dy == 0:
                    continue
                if dx * dx + dy * dy <= stroke_width * stroke_width + 1:
                    yield dx, dy

    def _draw_text_with_emoji(self, image: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.ImageFont) -> None:
        draw = ImageDraw.Draw(image)
        x, y = xy
        stroke_width = max(2, int(getattr(font, "size", 32) * 0.06))

        if Pilmoji is not None:
            try:
                with Pilmoji(image) as pilmoji:
                    for dx, dy in self._outline_offsets(stroke_width):
                        pilmoji.text((x + dx, y + dy), text, (0, 0, 0), font)
                    pilmoji.text((x, y), text, (255, 255, 255), font)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("Pilmoji fallback to Pillow text: %s", exc)

        draw.text(
            xy,
            text,
            font=font,
            fill=(255, 255, 255),
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0),
        )
