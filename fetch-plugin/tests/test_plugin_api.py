import importlib.util
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

_p = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
_spec = importlib.util.spec_from_file_location("fetch_plugin_api_test", _p)
api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = api
_spec.loader.exec_module(api)


class _FakeClient:
    def __init__(self, *, needs=False): self.needs = needs
    async def get_attest_challenge(self): return "ch123"
    async def register_device(self, **kw):
        if self.needs and not kw.get("attestation"):
            raise api._relay.NeedsAttestation("need it")
    async def unregister_device(self, **kw): pass


def _client(fake):
    api._relay.relay_client = lambda: fake          # monkeypatch the factory
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_challenge_proxy():
    c = _client(_FakeClient())
    assert c.get("/attest/challenge").json() == {"challenge": "ch123"}


def test_register_returns_428_when_attestation_needed():
    c = _client(_FakeClient(needs=True))
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch"})
    assert res.status_code == 428


def test_register_ok_with_attestation():
    c = _client(_FakeClient(needs=True))
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch",
                                    "attestation": "AAAA", "key_id": "k1", "challenge": "ch123"})
    assert res.status_code == 200


def test_register_422_on_partial_attestation():
    c = _client(_FakeClient())
    res = c.post("/register", json={"token": "t", "environment": "sandbox",
                                    "bundle_id": "com.brentwarner.fetch", "attestation": "AAAA"})  # missing key_id+challenge
    assert res.status_code == 422
