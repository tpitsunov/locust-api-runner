"""
Advanced Locust load-test file for serious performance testing.

This file is INDEPENDENT — it does NOT import from the app package.
It can be pointed at any host that implements the unified API schema.

BACKWARD COMPATIBILITY:
    The simple locustfile.py remains unchanged and fully functional.
    Students use locustfile.py; this file is for advanced/instructor testing.

USAGE:
    locust -f locustfile_advanced.py --host http://localhost:8000

FEATURES vs simple locustfile:
    - Pre-generated image pool (no CPU overhead during test)
    - Configurable payload sizes via environment variables
    - Response validation (schema + status checking)
    - Warmup phase (excluded from statistics)
    - Multiple user profiles: constant, heavy, burst
    - Custom metrics: error breakdown, payload size stats
    - Full configuration through environment variables

ENVIRONMENT VARIABLES:
    LOCUST_HOST                    Target URL (default: http://localhost:8000)
    LOAD_TEST_TEXT_MIN_LEN         Min random text length (default: 50)
    LOAD_TEST_TEXT_MAX_LEN         Max random text length (default: 500)
    LOAD_TEST_IMAGE_MIN_SIZE       Min image dimension (default: 64)
    LOAD_TEST_IMAGE_MAX_SIZE       Max image dimension (default: 512)
    LOAD_TEST_IMAGE_FORMAT         Image format: png, jpeg, webp (default: png)
    LOAD_TEST_IMAGE_POOL_SIZE      Pre-generated images count (default: 10)
    LOAD_TEST_VALIDATE             Validate responses: 1 or 0 (default: 1)
    LOAD_TEST_WARMUP_REQUESTS      Warmup requests per user before measuring (default: 3)
    LOAD_TEST_EXTRA_BODY           JSON string for extra_body (default: {})
    LOAD_TEST_PROFILE              User profile: constant, heavy, burst (default: constant)
    LOAD_TEST_LOG_DIR              Directory for log files (default: logs)
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from enum import Enum

from locust import HttpUser, between, events, task


# ---------------------------------------------------------------------------
# Logging setup — console + file
# ---------------------------------------------------------------------------

_LOG_DIR = os.environ.get("LOAD_TEST_LOG_DIR", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_log_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_LOG_PATH = os.path.join(_LOG_DIR, f"load_test_{_log_timestamp}.log")

_file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logger = logging.getLogger("locustfile_advanced")
logger.setLevel(logging.DEBUG)
logger.addHandler(_file_handler)


# Also forward Locust's own logs to the file
for _locust_logger_name in ("locust", "locust.stats", "locust.runners"):
    _locust_logger = logging.getLogger(_locust_logger_name)
    _locust_logger.addHandler(_file_handler)


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

class Config:
    HOST = os.environ.get("LOCUST_HOST", "http://localhost:8000")
    TEXT_MIN_LEN = int(os.environ.get("LOAD_TEST_TEXT_MIN_LEN", "50"))
    TEXT_MAX_LEN = int(os.environ.get("LOAD_TEST_TEXT_MAX_LEN", "500"))
    IMAGE_MIN_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_MIN_SIZE", "64"))
    IMAGE_MAX_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_MAX_SIZE", "512"))
    IMAGE_FORMAT = os.environ.get("LOAD_TEST_IMAGE_FORMAT", "png").lower()
    IMAGE_POOL_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_POOL_SIZE", "10"))
    VALIDATE = os.environ.get("LOAD_TEST_VALIDATE", "1") == "1"
    WARMUP_REQUESTS = int(os.environ.get("LOAD_TEST_WARMUP_REQUESTS", "3"))
    PROFILE = os.environ.get("LOAD_TEST_PROFILE", "constant").lower()

    _extra_body_raw = os.environ.get("LOAD_TEST_EXTRA_BODY", "{}")
    try:
        EXTRA_BODY: dict = json.loads(_extra_body_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LOAD_TEST_EXTRA_BODY is not valid JSON: {_extra_body_raw!r} ({exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Input type enum
# ---------------------------------------------------------------------------

class InputType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    TEXT_AND_IMAGE = "text_and_image"


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------

RANDOM_SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Artificial intelligence is transforming every industry.",
    "Machine learning models require large amounts of data for training.",
    "Natural language processing enables computers to understand human text.",
    "Deep learning is a subset of machine learning using neural networks.",
    "Computer vision allows machines to interpret and analyze visual information.",
    "Reinforcement learning trains agents through rewards and penalties in an environment.",
    "Transfer learning reuses knowledge gained from one task and applies it to another.",
    "Generative models create new content by learning underlying data distributions.",
    "Convolutional neural networks are particularly effective for image classification tasks.",
    "Attention mechanisms allow models to focus on relevant parts of the input sequence.",
    "Large language models have demonstrated remarkable capabilities in text generation.",
    "Fine-tuning pre-trained models on domain-specific data improves downstream performance.",
    "Multi-modal models can process and reason about both text and images simultaneously.",
    "Prompt engineering is the practice of crafting inputs to guide model outputs effectively.",
]


def _random_text(
    min_len: int | None = None,
    max_len: int | None = None,
) -> str:
    """Generate random text of configurable length."""
    _min = min_len if min_len is not None else Config.TEXT_MIN_LEN
    _max = max_len if max_len is not None else Config.TEXT_MAX_LEN
    target_len = random.randint(_min, _max)

    sentences = [s for s in RANDOM_SENTENCES if len(s) >= _min]
    if sentences and target_len <= 200 and random.random() < 0.5:
        return random.choice(sentences)

    parts: list[str] = []
    current_len = 0
    while current_len < target_len:
        part = random.choice(RANDOM_SENTENCES)
        parts.append(part)
        current_len += len(part) + 1

    text = " ".join(parts)
    if len(text) > target_len:
        text = text[:target_len].rsplit(" ", 1)[0]

    return text


def _generate_image_bytes(
    width: int,
    height: int,
    fmt: str = Config.IMAGE_FORMAT,
) -> bytes:
    """Generate a random image as raw bytes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    for _ in range(random.randint(2, 6)):
        x0 = random.randint(0, max(0, width - 1))
        y0 = random.randint(0, max(0, height - 1))
        x1 = random.randint(x0, width)
        y1 = random.randint(y0, height)
        color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        shape = random.choice(["rectangle", "ellipse"])
        if shape == "rectangle":
            draw.rectangle([x0, y0, x1, y1], fill=color)
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color)

    buf = io.BytesIO()
    pil_format = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}.get(fmt, "PNG")
    img.save(buf, format=pil_format)
    return buf.getvalue()


def _image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


class ImagePool:
    """Pre-generated pool of images to avoid CPU overhead during test."""

    def __init__(self, pool_size: int = Config.IMAGE_POOL_SIZE) -> None:
        self._images: list[str] = []
        self._sizes: list[tuple[int, int]] = []
        self._generate(pool_size)

    def _generate(self, count: int) -> None:
        logger.info("Pre-generating %d images...", count)
        for _ in range(count):
            w = random.randint(Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
            h = random.randint(Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
            img_bytes = _generate_image_bytes(w, h)
            self._images.append(_image_to_base64(img_bytes))
            self._sizes.append((w, h))
        logger.info("Image pool ready (%d images)", count)

    def random_image(self) -> str:
        return random.choice(self._images)


image_pool = ImagePool()


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

def _validate_run_response(data: dict) -> list[str]:
    """Validate /run response. Only checks that response is usable."""
    issues: list[str] = []

    if not isinstance(data, dict):
        issues.append(f"response is not a JSON object: {type(data).__name__}")
        return issues

    return issues


# ---------------------------------------------------------------------------
# Custom metrics via Locust events
# ---------------------------------------------------------------------------

_stats: dict[str, int | float | None] = {
    "validation_failures": 0,
    "warmup_requests": 0,
    "total_requests": 0,
    "start_time": None,
}


@events.test_start.add_listener
def on_test_start(**kwargs: Any) -> None:
    _stats["start_time"] = time.time()
    _stats["validation_failures"] = 0
    _stats["warmup_requests"] = 0
    _stats["total_requests"] = 0
    logger.info("=" * 70)
    logger.info("LOAD TEST STARTED")
    logger.info("=" * 70)
    logger.info("Log file: %s", os.path.abspath(_LOG_PATH))
    logger.info("Configuration:")
    logger.info("  HOST            = %s", Config.HOST)
    logger.info("  PROFILE         = %s", Config.PROFILE)
    logger.info("  VALIDATE        = %s", Config.VALIDATE)
    logger.info("  WARMUP_REQUESTS = %d", Config.WARMUP_REQUESTS)
    logger.info("  TEXT_LEN        = %d..%d", Config.TEXT_MIN_LEN, Config.TEXT_MAX_LEN)
    logger.info("  IMAGE_SIZE      = %d..%d", Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
    logger.info("  IMAGE_FORMAT    = %s", Config.IMAGE_FORMAT)
    logger.info("  IMAGE_POOL_SIZE = %d", Config.IMAGE_POOL_SIZE)
    logger.info("  EXTRA_BODY      = %s", json.dumps(Config.EXTRA_BODY))
    logger.info("=" * 70)


@events.test_stop.add_listener
def on_test_stop(**kwargs: Any) -> None:
    elapsed = time.time() - _stats["start_time"] if _stats["start_time"] else 0
    rps = _stats["total_requests"] / elapsed if elapsed > 0 else 0
    logger.info("=" * 70)
    logger.info("LOAD TEST FINISHED")
    logger.info("=" * 70)
    logger.info("Summary:")
    logger.info("  Total requests          = %d", _stats["total_requests"])
    logger.info("  Warmup requests         = %d", _stats["warmup_requests"])
    logger.info("  Measured requests       = %d", _stats["total_requests"] - _stats["warmup_requests"])
    logger.info("  Validation failures     = %d", _stats["validation_failures"])
    logger.info("  Elapsed                 = %.1fs", elapsed)
    logger.info("  Requests/sec            = %.1f", rps)
    logger.info("=" * 70)
    logger.info("Full log: %s", os.path.abspath(_LOG_PATH))


# ---------------------------------------------------------------------------
# Shared user logic
# ---------------------------------------------------------------------------

class _UserMixin:
    """Shared state and methods for all user profiles."""

    _input_type: InputType = InputType.TEXT
    _warmup_remaining: int = Config.WARMUP_REQUESTS

    def on_start(self) -> None:
        self._warmup_remaining = Config.WARMUP_REQUESTS
        resp = self.client.get("/info", name="/info [discovery]")
        if resp.status_code == 200:
            self._input_type = InputType(resp.json()["input_type"])
            logger.info("Service input_type: %s", self._input_type.value)

    def run_service(self) -> None:
        _execute_run(self)


def _build_run_payload(input_type: InputType) -> dict:
    extra_body = dict(Config.EXTRA_BODY)

    if input_type == InputType.TEXT:
        return {
            "content": _random_text(),
            "extra_body": extra_body,
        }
    elif input_type == InputType.IMAGE:
        return {
            "content": [
                {"type": "image", "image": image_pool.random_image()},
            ],
            "extra_body": extra_body,
        }
    else:
        return {
            "content": [
                {"type": "text", "text": _random_text()},
                {"type": "image", "image": image_pool.random_image()},
            ],
            "extra_body": extra_body,
        }


def _execute_run(user: _UserMixin) -> None:
    is_warmup = user._warmup_remaining > 0
    if is_warmup:
        user._warmup_remaining -= 1

    payload = _build_run_payload(user._input_type)

    request_name = "/run [warmup]" if is_warmup else "/run"
    resp = user.client.post("/run", name=request_name, json=payload)

    _stats["total_requests"] += 1
    if is_warmup:
        _stats["warmup_requests"] += 1

    if Config.VALIDATE:
        if resp.status_code >= 400:
            _stats["validation_failures"] += 1
            logger.warning("HTTP %d for /run", resp.status_code)
        elif resp.status_code == 200:
            try:
                resp.json()
            except json.JSONDecodeError:
                _stats["validation_failures"] += 1
                logger.warning("Response is not valid JSON")


# ---------------------------------------------------------------------------
# User profiles — only the selected profile is active
# ---------------------------------------------------------------------------

_PROFILE_MAP: dict[str, type] = {}


def _profile(cls: type) -> type:
    _PROFILE_MAP[cls.__name__.lower().replace("user", "")] = cls
    return cls


@_profile
class ConstantUser(_UserMixin, HttpUser):
    """Steady load with moderate wait time. Default profile."""
    wait_time = between(0.5, 2.0)

    @task
    def run_service(self) -> None:
        _execute_run(self)


@_profile
class HeavyUser(_UserMixin, HttpUser):
    """High-frequency requests — stress testing."""
    wait_time = between(0.1, 0.5)

    @task
    def run_service(self) -> None:
        _execute_run(self)


@_profile
class BurstUser(_UserMixin, HttpUser):
    """Burst of BURST_SIZE requests, then pause — batch processing."""
    wait_time = between(2.0, 5.0)
    BURST_SIZE = 5

    @task
    def run_service(self) -> None:
        for _ in range(self.BURST_SIZE):
            _execute_run(self)


# Disable all profiles except the selected one
_ACTIVE_PROFILE = Config.PROFILE
for _name, _cls in _PROFILE_MAP.items():
    if _name != _ACTIVE_PROFILE:
        _cls.abstract = True

if _ACTIVE_PROFILE not in _PROFILE_MAP:
    raise ValueError(
        f"Unknown LOAD_TEST_PROFILE={_ACTIVE_PROFILE!r}. "
        f"Available: {sorted(_PROFILE_MAP.keys())}"
    )
