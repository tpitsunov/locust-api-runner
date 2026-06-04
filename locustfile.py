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

from locust import HttpUser, between, constant, events, task


_LOG_DIR = os.environ.get("LOAD_TEST_LOG_DIR", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_log_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_LOG_PATH = os.path.join(_LOG_DIR, f"load_test_{_log_timestamp}.log")

_file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logger = logging.getLogger("locust_runner")
logger.setLevel(logging.DEBUG)
logger.addHandler(_file_handler)


class Config:
    HOST = os.environ.get("LOCUST_HOST", "http://localhost:8000")
    TEXT_MIN_LEN = int(os.environ.get("LOAD_TEST_TEXT_MIN_LEN", "50"))
    TEXT_MAX_LEN = int(os.environ.get("LOAD_TEST_TEXT_MAX_LEN", "500"))
    IMAGE_MIN_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_MIN_SIZE", "64"))
    IMAGE_MAX_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_MAX_SIZE", "512"))
    IMAGE_FORMAT = os.environ.get("LOAD_TEST_IMAGE_FORMAT", "png").lower()
    IMAGE_POOL_SIZE = int(os.environ.get("LOAD_TEST_IMAGE_POOL_SIZE", "10"))
    WARMUP_REQUESTS = int(os.environ.get("LOAD_TEST_WARMUP_REQUESTS", "3"))
    PROFILE = os.environ.get("LOAD_TEST_PROFILE", "constant").lower()

    _extra_body_raw = os.environ.get("LOAD_TEST_EXTRA_BODY", "{}")
    try:
        EXTRA_BODY: dict = json.loads(_extra_body_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LOAD_TEST_EXTRA_BODY is not valid JSON: {_extra_body_raw!r} ({exc})"
        ) from exc


class InputType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    TEXT_AND_IMAGE = "text_and_image"


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
    def __init__(self, pool_size: int = Config.IMAGE_POOL_SIZE) -> None:
        self._images: list[str] = []
        self._generate(pool_size)

    def _generate(self, count: int) -> None:
        logger.info("Pre-generating %d images...", count)
        for _ in range(count):
            w = random.randint(Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
            h = random.randint(Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
            img_bytes = _generate_image_bytes(w, h)
            self._images.append(_image_to_base64(img_bytes))
        logger.info("Image pool ready (%d images)", count)

    def random_image(self) -> str:
        return random.choice(self._images)


image_pool = ImagePool()


_warmup_count: int = 0


_start_time: float = 0.0


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    global _warmup_count, _start_time
    _warmup_count = 0
    _start_time = time.time()
    logger.info("=" * 70)
    logger.info("LOAD TEST STARTED")
    logger.info("=" * 70)
    logger.info("Configuration:")
    logger.info("  HOST            = %s", Config.HOST)
    logger.info("  PROFILE         = %s", Config.PROFILE)
    logger.info("  WARMUP_REQUESTS = %d", Config.WARMUP_REQUESTS)
    logger.info("  TEXT_LEN        = %d..%d", Config.TEXT_MIN_LEN, Config.TEXT_MAX_LEN)
    logger.info("  IMAGE_SIZE      = %d..%d", Config.IMAGE_MIN_SIZE, Config.IMAGE_MAX_SIZE)
    logger.info("  IMAGE_POOL_SIZE = %d", Config.IMAGE_POOL_SIZE)
    logger.info("  EXTRA_BODY      = %s", json.dumps(Config.EXTRA_BODY))
    logger.info("=" * 70)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    total = environment.stats.total
    elapsed = time.time() - _start_time if _start_time else 1
    rps = total.num_requests / elapsed if elapsed > 0 else 0
    logger.info("=" * 70)
    logger.info("LOAD TEST FINISHED")
    logger.info("=" * 70)
    logger.info("Summary:")
    logger.info("  Total requests          = %d", total.num_requests)
    logger.info("  HTTP errors             = %d", total.num_failures)
    logger.info("  Warmup requests         = %d", _warmup_count)
    logger.info("  Median response time    = %d ms", int(total.median_response_time))
    logger.info("  Average response time   = %d ms", int(total.avg_response_time))
    logger.info("  Requests/sec            = %.1f", rps)
    logger.info("=" * 70)


class _UserMixin:
    _input_type: InputType = InputType.TEXT
    _warmup_remaining: int = Config.WARMUP_REQUESTS

    def on_start(self) -> None:
        self._warmup_remaining = Config.WARMUP_REQUESTS
        resp = self.client.get("/info", name="/info [discovery]")
        if resp.status_code == 200:
            try:
                self._input_type = InputType(resp.json()["input_type"])
                logger.info("Service input_type: %s", self._input_type.value)
            except (KeyError, ValueError):
                logger.info("Could not parse /info, using default: text")

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
    global _warmup_count

    is_warmup = user._warmup_remaining > 0
    if is_warmup:
        user._warmup_remaining -= 1
        _warmup_count += 1

    payload = _build_run_payload(user._input_type)

    request_name = "/run [warmup]" if is_warmup else "/run"
    user.client.post("/run", name=request_name, json=payload)


_PROFILE_MAP: dict[str, type] = {}


def _profile(cls: type) -> type:
    _PROFILE_MAP[cls.__name__.lower().replace("user", "")] = cls
    return cls


@_profile
class ConstantUser(_UserMixin, HttpUser):
    wait_time = constant(0)

    @task
    def run_service(self) -> None:
        _execute_run(self)


@_profile
class HeavyUser(_UserMixin, HttpUser):
    wait_time = constant(0)

    @task
    def run_service(self) -> None:
        _execute_run(self)


@_profile
class BurstUser(_UserMixin, HttpUser):
    wait_time = between(2.0, 5.0)
    BURST_SIZE = 5

    @task
    def run_service(self) -> None:
        for _ in range(self.BURST_SIZE):
            _execute_run(self)


_ACTIVE_PROFILE = Config.PROFILE
for _name, _cls in _PROFILE_MAP.items():
    if _name != _ACTIVE_PROFILE:
        _cls.abstract = True

if _ACTIVE_PROFILE not in _PROFILE_MAP:
    raise ValueError(
        f"Unknown LOAD_TEST_PROFILE={_ACTIVE_PROFILE!r}. "
        f"Available: {sorted(_PROFILE_MAP.keys())}"
    )
