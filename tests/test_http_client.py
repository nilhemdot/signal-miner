"""AC 1: SEC client user agent, pacing and Retry-After; shared retry behavior."""
import httpx
import pytest
import respx

from signal_miner.http.client import FakeClock, HttpClient, RetryExhausted
from signal_miner.http.sec import MissingUserAgent, make_sec_client

UA = "Signal Miner ops@example.com"


@respx.mock
def test_sec_client_without_user_agent_fails_and_makes_zero_calls():
    route = respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/index.json")
    with pytest.raises(MissingUserAgent):
        make_sec_client(env={})
    with pytest.raises(MissingUserAgent):
        make_sec_client(env={"SEC_USER_AGENT": "   "})
    with pytest.raises(MissingUserAgent):
        make_sec_client(env={"SEC_USER_AGENT": "no-email-here"})
    assert route.call_count == 0


@respx.mock
def test_sec_client_sends_user_agent_and_gzip():
    route = respx.get("https://www.sec.gov/x").mock(return_value=httpx.Response(200))
    client = make_sec_client(env={"SEC_USER_AGENT": UA}, clock=FakeClock())
    client.get("https://www.sec.gov/x")
    req = route.calls.last.request
    assert req.headers["User-Agent"] == UA
    assert "gzip" in req.headers["Accept-Encoding"]


@respx.mock
def test_twenty_sec_calls_take_at_least_2_8_seconds():
    respx.get(url__regex=r"https://(www|data)\.sec\.gov/.*").mock(return_value=httpx.Response(200))
    clock = FakeClock()
    client = make_sec_client(env={"SEC_USER_AGENT": UA}, clock=clock)
    start = clock.monotonic()
    for i in range(20):
        host = "www" if i % 2 else "data"
        client.get(f"https://{host}.sec.gov/{i}")
    assert clock.monotonic() - start >= 2.8


@respx.mock
def test_retry_after_is_honored_on_429():
    route = respx.get("https://www.sec.gov/y").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    clock = FakeClock()
    client = make_sec_client(env={"SEC_USER_AGENT": UA}, clock=clock)
    assert client.get("https://www.sec.gov/y").status_code == 200
    assert route.call_count == 2
    assert 7 in clock.sleeps


@respx.mock
def test_5xx_retries_then_raises_after_five_tries():
    route = respx.get("https://api.example.com/z").mock(return_value=httpx.Response(503))
    client = HttpClient(user_agent=UA, clock=FakeClock())
    with pytest.raises(RetryExhausted):
        client.get("https://api.example.com/z")
    assert route.call_count == 5


@respx.mock
def test_404_is_returned_without_retry():
    route = respx.get("https://api.example.com/missing").mock(return_value=httpx.Response(404))
    client = HttpClient(user_agent=UA, clock=FakeClock())
    assert client.get("https://api.example.com/missing").status_code == 404
    assert route.call_count == 1


@respx.mock
def test_hosts_are_paced_independently():
    respx.get(url__regex=r"https://(a|b)\.example\.com/.*").mock(return_value=httpx.Response(200))
    clock = FakeClock()
    client = HttpClient(user_agent=UA, clock=clock, host_intervals={"a.example.com": 1.0, "b.example.com": 1.0})
    client.get("https://a.example.com/1")
    client.get("https://b.example.com/1")
    assert clock.monotonic() == 0  # different hosts do not wait on each other
    client.get("https://a.example.com/2")
    assert clock.monotonic() == pytest.approx(1.0)


def test_client_has_explicit_timeouts():
    client = HttpClient(user_agent=UA, clock=FakeClock())
    t = client.timeout
    assert t.connect is not None and t.read is not None


def test_retry_after_http_date_and_cap():
    from signal_miner.http.client import parse_retry_after

    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now_epoch=1445412480) == pytest.approx(10.0)
    assert parse_retry_after("100000") == 120.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None
