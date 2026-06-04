import base64
import io
import os
import random
import string
from enum import Enum

from locust import HttpUser, between, task


class InputType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    TEXT_AND_IMAGE = "text_and_image"


RANDOM_SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Artificial intelligence is transforming every industry.",
    "Hello, world! This is a load test.",
    "Machine learning models require large amounts of data.",
    "Natural language processing enables computers to understand text.",
    "Deep learning is a subset of machine learning.",
    "Computer vision allows machines to interpret visual information.",
    "Reinforcement learning trains agents through rewards and penalties.",
    "Transfer learning reuses knowledge from one task to another.",
    "Generative models create new content from learned distributions.",
]


def _generate_image_base64(width: int = 100, height: int = 100) -> str:
    from PIL import Image

    color = (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _random_text(length: int = 120) -> str:
    if random.random() < 0.7:
        return random.choice(RANDOM_SENTENCES)
    return "".join(random.choices(string.ascii_letters + " ", k=length))


class APIUser(HttpUser):
    wait_time = between(0.5, 2.0)
    host: str = os.environ.get("LOCUST_HOST", "http://localhost:8000")

    _input_type: InputType = InputType.TEXT

    def on_start(self) -> None:
        resp = self.client.get("/info")
        if resp.status_code == 200:
            data = resp.json()
            self._input_type = InputType(data["input_type"])

    def _build_run_payload(self) -> dict:
        if self._input_type == InputType.TEXT:
            return {
                "content": _random_text(),
                "extra_body": {},
            }
        elif self._input_type == InputType.IMAGE:
            return {
                "content": [
                    {"type": "image", "image": _generate_image_base64()},
                ],
                "extra_body": {},
            }
        else:
            return {
                "content": [
                    {"type": "text", "text": _random_text()},
                    {"type": "image", "image": _generate_image_base64()},
                ],
                "extra_body": {},
            }

    @task
    def run_service(self) -> None:
        self.client.post("/run", json=self._build_run_payload())
