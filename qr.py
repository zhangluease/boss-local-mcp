"""QR decoding with Homebrew zbar discovery on Apple Silicon."""

import io
from pathlib import Path
import sys

from PIL import Image


def _load_decoder():
    import pyzbar.zbar_library as library

    if sys.platform == "darwin" and not library.find_library("zbar"):
        for path in (Path("/opt/homebrew/lib/libzbar.dylib"), Path("/usr/local/lib/libzbar.dylib")):
            if path.is_file():
                original = library.find_library
                library.find_library = lambda name, path=str(path): path if name == "zbar" else original(name)
                break
    from pyzbar.pyzbar import decode
    return decode


try:
    decode_qr = _load_decoder()
    HAS_QR_DECODER = True
except (ImportError, OSError):
    decode_qr = None
    HAS_QR_DECODER = False


def decode_image(data: bytes) -> str:
    if not decode_qr:
        raise RuntimeError("二维码解码不可用：macOS 请安装 brew install zbar；Ubuntu 请安装 libzbar0。")
    with Image.open(io.BytesIO(data)) as image:
        for result in decode_qr(image):
            if result.type == "QRCODE":
                return result.data.decode("utf-8")
    return ""
