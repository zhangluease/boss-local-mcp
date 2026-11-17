import io
from pathlib import Path
import sys

from PIL import Image, ImageDraw
import pytest
import qrcode

from evaluator import CandidateEvaluator
from filters import filter_loaded_candidates
from qr import decode_image


@pytest.mark.parametrize("arguments,expected", [
    ({}, 2),
    ({"city": "北京"}, 1),
    ({"city": "北京市"}, 1),
    ({"experience": "5年以上"}, 1),
    ({"experience": "1年以内"}, 0),
    ({"salary": "20-30K"}, 1),
    ({"salary": "40-50K"}, 0),
])
def test_loaded_filters(arguments, expected):
    candidates = [{"expectCity": "北京", "experience": "5年", "salary": "20-30K·13薪"}, {"expectCity": "上海", "experience": "3年", "salary": "10-15K·24薪"}]
    assert len(filter_loaded_candidates(candidates, **arguments)) == expected


@pytest.mark.parametrize("arguments", [{"experience": "unknown"}, {"salary": "20-30"}])
def test_invalid_filters_are_not_silently_ignored(arguments):
    with pytest.raises(ValueError):
        filter_loaded_candidates([], **arguments)


def test_evaluation_uses_the_requested_job():
    evaluator = CandidateEvaluator()
    resume = {"name": "无关姓名", "skills": ["Python", "SQL"]}
    assert evaluator.evaluate(resume, "Python SQL")["score"] > evaluator.evaluate(resume, "Java Kubernetes")["score"]


def test_qr_decoding():
    output = io.BytesIO()
    qrcode.make("https://zpurl.cn/local-fixture").save(output, format="PNG")
    assert decode_image(output.getvalue()) == "https://zpurl.cn/local-fixture"


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is a macOS system framework")
def test_local_ocr_without_external_api():
    from ocr import ocr_image
    image = Image.new("RGB", (1000, 200), "white")
    drawing = ImageDraw.Draw(image)
    from PIL import ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 50)
    drawing.text((30, 50), "Python SQL local OCR", fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    text = ocr_image(output.getvalue())
    assert "Python" in text and "SQL" in text


def test_ocr_rejects_non_images_and_explicit_cloud_without_credentials(monkeypatch):
    import ocr
    with pytest.raises(Exception):
        ocr.ocr_image(b"not an image")
    monkeypatch.setattr(ocr, "ANTHROPIC_API_KEY", "")
    output = io.BytesIO()
    Image.new("RGB", (20, 20)).save(output, format="PNG")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ocr.ocr_image(output.getvalue(), engine="anthropic")


def test_local_ocr_preserves_utf8_process_output(monkeypatch):
    import ocr
    run = ocr.subprocess.run

    def synthetic_tesseract(command, **options):
        return run([sys.executable, "-c", "import sys;sys.stdout.buffer.write('候选人简历 Python'.encode('utf-8'))"], **options)

    monkeypatch.setattr(ocr.subprocess, "run", synthetic_tesseract)
    output = io.BytesIO()
    Image.new("RGB", (20, 20)).save(output, format="PNG")
    assert ocr.ocr_image(output.getvalue(), engine="tesseract") == "候选人简历 Python"
