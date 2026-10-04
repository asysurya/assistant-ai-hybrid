# ============================================================
# AssistantAI — tests/test_cloud_client.py
# ------------------------------------------------------------
# Test ROTASI API KEY pada CloudClient — memakai SDK PALSU, tanpa
# panggilan jaringan sungguhan.
#
# Skenario:
#   - key pertama kena 429 -> otomatis lanjut key kedua -> sukses
#   - semua key 429        -> CloudRateLimited
#   - key invalid (401)    -> CloudAuthError
#   - pool kosong          -> CloudClientError dengan pesan jelas
#
# Jalankan:
#   python -m pytest tests/test_cloud_client.py -v
#   python tests/test_cloud_client.py
# ============================================================

"""Test suite CloudClient: rotasi 429, key invalid, pool kosong."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

openai = pytest.importorskip("openai")      # test ini butuh paket openai (kelas exception)
httpx = pytest.importorskip("httpx")

from core.cloud_client import (  # noqa: E402
    CloudAuthError,
    CloudClient,
    CloudClientError,
    CloudRateLimited,
)
from core.key_pool import KeyPool  # noqa: E402

URL = "https://ollama.com/v1/chat/completions"


# ------------------------------------------------------------
# Pembangun exception openai "sungguhan" (perlu response httpx)
# ------------------------------------------------------------
def buat_429() -> Exception:
    resp = httpx.Response(429, request=httpx.Request("POST", URL))
    return openai.RateLimitError("rate limited", response=resp, body=None)


def buat_401() -> Exception:
    resp = httpx.Response(401, request=httpx.Request("POST", URL))
    return openai.AuthenticationError("invalid key", response=resp, body=None)


# ------------------------------------------------------------
# SDK palsu — meniru potongan openai SDK yang dipakai CloudClient
# ------------------------------------------------------------
class SdkPalsu:
    """Client palsu: perilaku per panggilan (list of str / Exception)."""

    def __init__(self, perilaku: list):
        self._perilaku = list(perilaku)
        self._panggilan = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        idx = min(self._panggilan, len(self._perilaku) - 1)
        self._panggilan += 1
        item = self._perilaku[idx]
        if isinstance(item, Exception):
            raise item
        # bentuk respons minimal yang dibaca CloudClient
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))])


# ------------------------------------------------------------
# Test
# ------------------------------------------------------------
def test_rotasi_429_lalu_sukses_dengan_key_berikutnya():
    sdk = SdkPalsu([buat_429(), "Jawaban dari key kedua"])
    pool = KeyPool(["key-aaaa1111", "key-bbbb2222"], cooldown_seconds=60)
    cloud = CloudClient(key_pool=pool, client=sdk)

    hasil = cloud.chat([{"role": "user", "content": "halo"}], model="model-uji")

    assert hasil == "Jawaban dari key kedua"
    assert pool.available_count() == 1              # key pertama cooldown, kedua sehat


def test_sukses_menandai_key_sehat():
    sdk = SdkPalsu(["OKE"])
    pool = KeyPool(["key-aaaa1111"], cooldown_seconds=60)
    cloud = CloudClient(key_pool=pool, client=sdk)
    cloud.chat([{"role": "user", "content": "halo"}], model="model-uji")
    assert pool.available_count() == 1


def test_semua_key_429_raise_cloud_rate_limited():
    sdk = SdkPalsu([buat_429()])                    # 429 terus-menerus
    pool = KeyPool(["key-aaaa1111", "key-bbbb2222"], cooldown_seconds=60)
    cloud = CloudClient(key_pool=pool, client=sdk)
    with pytest.raises(CloudRateLimited):
        cloud.chat([{"role": "user", "content": "x"}], model="model-uji")


def test_key_invalid_401_raise_cloud_auth_error():
    sdk = SdkPalsu([buat_401()])
    pool = KeyPool(["key-aaaa1111"], cooldown_seconds=60)
    cloud = CloudClient(key_pool=pool, client=sdk)
    with pytest.raises(CloudAuthError):
        cloud.chat([{"role": "user", "content": "x"}], model="model-uji")


def test_pool_kosong_pesan_jelas():
    cloud = CloudClient(key_pool=KeyPool([]), client=SdkPalsu(["x"]))
    with pytest.raises(CloudClientError) as info:
        cloud.chat([{"role": "user", "content": "x"}], model="model-uji")
    assert "API key" in str(info.value)


# ------------------------------------------------------------
# Runner tanpa pytest
# ------------------------------------------------------------
if __name__ == "__main__":
    daftar = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    gagal = 0
    for fn in daftar:
        try:
            fn()
            print(f"LULUS  : {fn.__name__}")
        except AssertionError as exc:
            gagal += 1
            print(f"GAGAL  : {fn.__name__} -> {exc}")
    print(f"\n{len(daftar) - gagal}/{len(daftar)} test lulus.")
    sys.exit(1 if gagal else 0)
