"""A stand-in for the Tesseract CLI in tests: `fake_tesseract.py <mode> <count file> <image> stdout -l <languages>`.

It checks that it was given a binary PGM page image, counts its calls, and prints text like Tesseract
would. Modes: `text` (prints a line), `empty` (prints nothing), `fail` (exits with status 1).
"""
from pathlib import Path
import sys

mode, count_file, image, destination, flag, languages = sys.argv[1:7]
data = Path(image).read_bytes()
if data.startswith(b"P5"):  # a rendered PDF page
    header = data[:32].split()
    assert int(header[1]) > 0 and int(header[2]) > 0 and header[3] == b"255", header
else:  # an image source, passed as it is
    assert data.startswith((b"\x89PNG", b"\xff\xd8")), data[:8]
assert destination == "stdout" and flag == "-l"
with open(count_file, "a", encoding="utf-8") as count:
    count.write(languages + "\n")
if mode == "fail":
    sys.exit(1)
if mode == "text":
    print(f"Scanned revenue was 42. ({languages})")
