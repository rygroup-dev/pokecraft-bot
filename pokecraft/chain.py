"""Read-only Solana lookups: SOL and POKECRAFT balance of the bot wallet."""
import logging

import httpx

log = logging.getLogger("chain")

RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://solana.drpc.org",
]


class Chain:
    def __init__(self, extra_rpc=None):
        self.rpcs = ([extra_rpc] if extra_rpc else []) + RPCS
        self.http = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self.http.aclose()

    async def _call(self, method, params):
        last = None
        for url in self.rpcs:
            try:
                r = await self.http.post(url, json={"jsonrpc": "2.0", "id": 1,
                                                     "method": method, "params": params})
                j = r.json()
                if "result" in j:
                    if url != self.rpcs[0]:
                        # remember the RPC that works on this network
                        self.rpcs.remove(url)
                        self.rpcs.insert(0, url)
                    return j["result"]
                last = j.get("error")
            except (httpx.HTTPError, ValueError) as e:
                last = e
        raise RuntimeError(f"semua RPC Solana gagal: {last}")

    async def balances(self, owner, mint, decimals=6):
        """Returns (sol, token) or (None, None) when no RPC is reachable."""
        try:
            sol = (await self._call("getBalance", [owner]))["value"] / 1e9
            res = await self._call("getTokenAccountsByOwner",
                                   [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
            tok = 0.0
            for acc in res.get("value", []):
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
                tok += int(amt["amount"]) / 10 ** decimals
            return sol, tok
        except Exception as e:  # noqa: BLE001 - dashboard must not crash on RPC trouble
            log.warning("balance lookup failed: %s", e)
            return None, None
