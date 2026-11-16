"""Local OCR first; optional cloud OCR requires an explicit engine choice."""

import base64
import hashlib
from importlib.resources import files
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

from config import ANTHROPIC_API_KEY, DATA_DIR, EVAL_MODEL


def available_engine():
    if sys.platform == "darwin" and shutil.which("xcrun"):
        return "apple-vision"
    if shutil.which("tesseract"):
        return "tesseract"
    return "unavailable"


def _native_binary():
    source = files("boss_local_assets").joinpath("ocr.swift").read_bytes()
    signature = hashlib.sha256(source).hexdigest()[:16]
    cache = Path(DATA_DIR) / "ocr-cache"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / ("recognize-" + signature)
    if not binary.exists():
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            swift = Path(temporary) / "ocr.swift"
            output = Path(temporary) / "recognize"
            swift.write_bytes(source)
            result = subprocess.run(["xcrun", "swiftc", "-O", str(swift), "-o", str(output)], capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError("Apple Vision OCR 编译失败：" + result.stderr[-1000:])
            os.replace(output, binary)
    return binary


def ocr_image(image_bytes: bytes, engine="auto") -> str:
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.verify()
    if engine == "auto":
        engine = available_engine()
    if engine == "anthropic":
        if not ANTHROPIC_API_KEY or not EVAL_MODEL:
            raise RuntimeError("云端 OCR 需要配置 ANTHROPIC_API_KEY 和 BOSS_OCR_MODEL")
        from anthropic import Anthropic
        result = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=EVAL_MODEL, max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": "提取图片中的文字，保持段落，不添加评价。"},
            ]}],
        )
        return "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")
    if engine not in ("apple-vision", "tesseract"):
        raise RuntimeError("未发现本地 OCR。macOS 请安装 Xcode Command Line Tools；其他平台可安装 Tesseract。")
    descriptor, filename = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(image_bytes)
        command = [str(_native_binary()), filename] if engine == "apple-vision" else ["tesseract", filename, "stdout", "-l", os.getenv("BOSS_OCR_LANG", "eng")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("OCR 识别失败：" + result.stderr[-1000:])
        return json.loads(result.stdout)["text"] if engine == "apple-vision" else result.stdout
    finally:
        os.unlink(filename)
