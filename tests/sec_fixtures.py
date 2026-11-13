"""Shared helpers: serve the real SEC fixtures in tests/fixtures/sec through respx."""
from pathlib import Path

import httpx
import respx

FIX = Path(__file__).parent / "fixtures" / "sec"
UA_ENV = {"SEC_USER_AGENT": "Signal Miner tests@example.com"}


def fixture_text(name: str) -> str:
    return (FIX / name).read_text()


def mock_sec(router: respx.Router) -> None:
    """Route daily indexes, Form D XML and submissions JSON to fixture files; anything else 404s."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/index.json") and "daily-index" in path:
            return httpx.Response(200, content=(FIX / "index.json").read_bytes())
        if "daily-index" in path:
            f = FIX / path.rsplit("/", 1)[1]
        elif path.endswith("/primary_doc.xml"):
            parts = path.split("/")  # /Archives/edgar/data/{cik}/{acc}/primary_doc.xml
            f = FIX / f"{parts[-3]}_{parts[-2]}.xml"
        elif path.startswith("/submissions/"):
            f = FIX / path.rsplit("/", 1)[1]
        else:
            f = None
        if f is None or not f.exists():
            return httpx.Response(404)
        return httpx.Response(200, content=f.read_bytes())

    router.route(host__in=["www.sec.gov", "data.sec.gov"]).mock(side_effect=handler)
