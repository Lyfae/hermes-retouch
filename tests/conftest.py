import random
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def noise(size, seed=0, mode="RGB"):
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", size, bytes(rnd.getrandbits(8) for _ in range(size[0] * size[1] * 3)))
    if mode == "RGBA":
        img = img.convert("RGBA")
        img.putalpha(Image.frombytes("L", size, bytes(rnd.getrandbits(8) for _ in range(size[0] * size[1]))))
    return img


@pytest.fixture
def photo():
    return noise((320, 240), seed=1)


@pytest.fixture
def rgba_photo():
    return noise((200, 150), seed=2, mode="RGBA")
