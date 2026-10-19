"""OCR boundary (QC text_ocr_match / text_artifact, design §10.1, [가정] #12).

Engine: PaddleOCR model family, executed through rapidocr_onnxruntime
(ONNX Runtime backend — the same PP-OCR models without the heavy
paddlepaddle dependency):
- det: ch_PP-OCRv4_det — script-agnostic text detection, ships inside the
  rapidocr_onnxruntime wheel.
- rec: korean_PP-OCRv5_mobile_rec — official PaddlePaddle ONNX export,
  11,945-char dictionary covering Hangul + Latin + digits + symbols,
  so Korean and English are recognized by one model.

First use downloads the rec model (~13MB tar) from the official
PaddlePaddle model ecology host into ``~/.cache/ai-studio/ocr/``
(override with ``OCR_MODELS_DIR``); the tar SHA-256 is verified against
the pinned value below. The det model is referenced in place from the
installed wheel. Subsequent runs are fully offline.

``recognize(path, *, sample_interval_sec=1.0)`` returns
``[{"text": str, "confidence": float, "at_sec": float}]``:
- image input: a single pass, ``at_sec=0.0``.
- video input: ffmpeg extracts a frame every ``sample_interval_sec``
  (design §10.2: 1s sampling), each frame OCR'd with its timestamp.

Results are NOT confidence-filtered here: the engine is constructed with
``text_score=0.0`` so low-confidence detections survive. The 0.5 split
from design §10.1 lives in the QC checks (text_artifact flags
confidence < 0.5; text_ocr_match only trusts >= 0.5).

Failure contract: import problems keep ``AVAILABLE=False`` (QC checks
already degrade to documented stubs). ``recognize()`` raises
``OCRError`` — a plain RuntimeError subclass — with a human-readable
message; raw onnxruntime tracebacks never escape.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tarfile
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

ENGINE = "paddleocr"  # model family; runtime is rapidocr_onnxruntime (ONNX)

# Official PaddlePaddle ONNX export of the Korean PP-OCRv5 mobile recognizer.
_REC_TAR_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
    "paddle3.0.0/korean_PP-OCRv5_mobile_rec_onnx_infer.tar"
)
_REC_TAR_SHA256 = (
    "568ed8b43a260adc9f484d92105e425ea8cddf8ce16940c177bc12864cfb0eb0"
)
_REC_TAR_MEMBER = "korean_PP-OCRv5_mobile_rec_onnx_infer/inference.onnx"
_REC_YML_MEMBER = "korean_PP-OCRv5_mobile_rec_onnx_infer/inference.yml"
_REC_ONNX_NAME = "korean_PP-OCRv5_mobile_rec.onnx"
_DICT_NAME = "korean_dict.txt"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class OCRError(RuntimeError):
    """Predictable OCR failure (engine/model/ffmpeg problem)."""


def _models_dir() -> Path:
    override = os.environ.get("OCR_MODELS_DIR")
    base = Path(override) if override else Path.home() / ".cache" / "ai-studio" / "ocr"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _wheel_det_model() -> Path:
    """Detection model shipped inside the rapidocr_onnxruntime wheel."""
    try:
        import rapidocr_onnxruntime
    except ImportError as e:
        raise OCRError(
            "rapidocr_onnxruntime is not installed; QC text checks run as stubs."
        ) from e
    det = (
        Path(rapidocr_onnxruntime.__file__).parent
        / "models"
        / "ch_PP-OCRv4_det_infer.onnx"
    )
    if not det.is_file():
        raise OCRError(f"Detection model missing from wheel: {det}")
    return det


def _ensure_rec_model(d: Path) -> tuple[Path, Path]:
    """Download + verify the Korean rec model and build its dict file."""
    onnx_path = d / _REC_ONNX_NAME
    dict_path = d / _DICT_NAME
    if onnx_path.is_file() and dict_path.is_file():
        return onnx_path, dict_path

    log.info("OCR: downloading Korean rec model (~13MB) on first use …")
    tmp = d / "korean_rec.tar"
    try:
        with urllib.request.urlopen(_REC_TAR_URL, timeout=120) as r, open(tmp, "wb") as f:
            f.write(r.read())
    except Exception as e:
        raise OCRError(f"Could not download OCR rec model: {e}") from e
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != _REC_TAR_SHA256:
        tmp.unlink(missing_ok=True)
        raise OCRError("OCR rec model checksum mismatch; refusing to use it.")
    try:
        with tarfile.open(tmp) as tar:
            tar.extract(_REC_TAR_MEMBER, path=d, filter="data")
            tar.extract(_REC_YML_MEMBER, path=d, filter="data")
    except Exception as e:
        raise OCRError(f"Could not unpack OCR rec model: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)

    src_dir = d / "korean_PP-OCRv5_mobile_rec_onnx_infer"
    (src_dir / "inference.onnx").replace(onnx_path)
    # Build dict.txt from the character list under PostProcess.character_dict
    # in inference.yml (one char per line, order preserved).
    chars: list[str] = []
    in_dict = False
    with open(src_dir / "inference.yml", encoding="utf-8") as f:
        for line in f:
            if line.startswith("  character_dict:"):
                in_dict = True
                continue
            if in_dict:
                if line.startswith("  - "):
                    chars.append(line[4:].rstrip("\n"))
                elif line.strip() and not line.startswith(" "):
                    break  # next top-level key
    if not chars:
        raise OCRError("Could not read OCR character dict from inference.yml.")
    with open(dict_path, "w", encoding="utf-8") as f:
        for ch in chars:
            f.write(f"{ch}\n")
    # Clean up the extracted tree.
    for p in sorted(src_dir.rglob("*"), reverse=True):
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            p.rmdir()
    src_dir.rmdir()
    log.info("OCR: Korean rec model ready (%d chars).", len(chars))
    return onnx_path, dict_path


try:
    import rapidocr_onnxruntime  # noqa: F401

    AVAILABLE = True
except ImportError:
    AVAILABLE = False

_engine = None

# In-run cache: (abspath, sample_interval_sec, mtime_ns, size) -> hits.
# QC runs both text checks over the same shot media; outputs are immutable
# (PROJECT_ARCHITECTURE.md), so keying on mtime+size is safe and avoids
# running the same OCR pass twice in one QC run.
_recognize_cache: dict[tuple, list[dict]] = {}


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        d = _models_dir()
        det = _wheel_det_model()
        rec_onnx, dict_path = _ensure_rec_model(d)
        try:
            _engine = RapidOCR(
                det_model_path=str(det),
                rec_model_path=str(rec_onnx),
                rec_keys_path=str(dict_path),
                text_score=0.0,  # keep low-confidence detections; checks split at 0.5
            )
        except Exception as e:
            raise OCRError(f"Could not initialize OCR engine: {e}") from e
    return _engine


def _ocr_image(engine, image_path: str, at_sec: float) -> list[dict]:
    try:
        out = engine(image_path)
    except Exception as e:
        raise OCRError(f"OCR failed on {image_path}: {e}") from e
    result = out[0] if isinstance(out, tuple) else out
    hits = []
    for item in result or []:
        try:
            _box, text, score = item
        except (TypeError, ValueError):
            continue
        text = (text or "").strip()
        if not text:
            continue
        hits.append({"text": text, "confidence": float(score), "at_sec": at_sec})
    return hits


def recognize(path: str, *, sample_interval_sec: float = 1.0) -> list[dict]:
    """Recognize on-screen text in an image or video.

    Returns [{"text", "confidence", "at_sec"}] ordered by at_sec.
    Video is sampled every ``sample_interval_sec`` (design §10.2).
    Raises OCRError on engine/model/ffmpeg failure; never leaks raw
    third-party tracebacks.
    """
    if not AVAILABLE:
        raise OCRError(
            "OCR engine not installed (pip install rapidocr_onnxruntime). "
            "QC text checks run as documented stubs."
        )
    p = Path(path)
    if not p.is_file():
        raise OCRError(f"OCR input not found: {path}")
    try:
        stat = p.stat()
        cache_key = (str(p.resolve()), sample_interval_sec, stat.st_mtime_ns, stat.st_size)
    except OSError as e:
        raise OCRError(f"OCR input not readable: {path}: {e}") from e
    cached = _recognize_cache.get(cache_key)
    if cached is not None:
        return [dict(h) for h in cached]

    engine = _get_engine()

    if p.suffix.lower() in _IMAGE_EXTS:
        hits = _ocr_image(engine, str(p), 0.0)
        _recognize_cache[cache_key] = hits
        return [dict(h) for h in hits]

    # Video: sample frames with ffmpeg.
    from app.media import ffmpeg_ops

    try:
        info = ffmpeg_ops.probe(str(p))
        duration = float(info.get("duration_sec") or 0)
    except Exception as e:
        raise OCRError(f"Could not probe video for OCR: {e}") from e
    if duration <= 0:
        raise OCRError(f"OCR: video has no duration: {path}")

    hits: list[dict] = []
    t = 0.0
    with tempfile.TemporaryDirectory(prefix="ocr_frames_") as tmpdir:
        while t < duration:
            frame = str(Path(tmpdir) / f"f_{t:.2f}.png")
            try:
                ffmpeg_ops.extract_frame(str(p), frame, at_sec=t)
                hits.extend(_ocr_image(engine, frame, round(t, 2)))
            except OCRError:
                raise
            except Exception as e:
                raise OCRError(f"OCR frame extraction failed at {t:.1f}s: {e}") from e
            t += sample_interval_sec
    _recognize_cache[cache_key] = hits
    return [dict(h) for h in hits]


def clear_cache() -> None:
    """Drop the in-run recognize() cache (tests, or after media replacement)."""
    _recognize_cache.clear()
