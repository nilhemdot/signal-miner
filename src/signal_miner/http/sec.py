"""SEC EDGAR client. Fair access: declared User-Agent, at most 10 requests/s across sec.gov hosts."""
import os
from collections.abc import Mapping

from signal_miner.http.client import HttpClient

SEC_HOSTS = ("www.sec.gov", "data.sec.gov", "efts.sec.gov")
SEC_INTERVAL_S = 0.15


class MissingUserAgent(RuntimeError):
    pass


def make_sec_client(env: Mapping[str, str] | None = None, clock=None, transport=None) -> HttpClient:
    env = os.environ if env is None else env
    ua = (env.get("SEC_USER_AGENT") or "").strip()
    if not ua or "@" not in ua:
        raise MissingUserAgent("SEC_USER_AGENT must be set to '<Name> <email>' before any sec.gov call")
    return HttpClient(
        user_agent=ua,
        clock=clock,
        host_intervals={"sec": SEC_INTERVAL_S},
        host_groups={h: "sec" for h in SEC_HOSTS},
        transport=transport,
    )
