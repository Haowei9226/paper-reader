"""Render the shared vector artwork as a Windows icon at common Explorer sizes."""
from pathlib import Path
import struct

import pymupdf


def main():
    root = Path(__file__).resolve().parent.parent
    sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
    parts, images = [struct.pack("<HHH", 0, 1, len(sizes))], []
    offset = 6 + 16 * len(sizes)
    with pymupdf.open(root / "web" / "public" / "icon.svg") as doc:
        page = doc[0]
        for size in sizes:
            scale = size / page.rect.width
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
            assert (pixmap.width, pixmap.height) == (size, size)
            png = pixmap.tobytes("png")
            parts.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset))
            images.append(png)
            offset += len(png)
    output = root / "assets" / "reader.ico"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"".join(parts + images))


if __name__ == "__main__":
    main()
