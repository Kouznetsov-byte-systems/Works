import json
import threading
import urllib.error
import urllib.request
from typing import Optional

class PublicLedgerBalanceMonitor:
    

    LAMPORTS_PER_SOL = 1_000_000_000

    def __init__(
        self,
        rpc_endpoint: str,
        wallet_address: str,
        timeout: float = 10.0,
    ) -> None:
        if not rpc_endpoint:
            raise ValueError("rpc_endpoint must not be empty")
        if not wallet_address:
            raise ValueError("wallet_address must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self._rpc_endpoint = rpc_endpoint
        self._wallet_address = wallet_address
        self._timeout = timeout

        # Protects request sta -- checked.
        # that invoke get_bala -- checked.
        self._lock = threading.Lock()

    def get_balance(self) -> Optional[float]:
        
        request_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [self._wallet_address],
        }

        payload = json.dumps(
            request_body,
            separators=(",", ":"),
        ).encode("utf-8")

        request = urllib.request.Request(
            self._rpc_endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        # urllib's opener is t -- checked.
        # while the lock ensur -- checked.
        # at a time and keeps  -- checked.
        with self._lock:
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                ) as response:
                    if response.status != 200:
                        return None

                    raw_response = response.read()

            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                OSError,
            ):
                return None

        return self._parse_balance(raw_response)

    def _parse_balance(self, raw_response: bytes) -> Optional[float]:
        
        try:
            document = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None

        if not isinstance(document, dict):
            return None

        # A valid JSON-RPC res -- checked.
        # and should not conta -- checked.
        if document.get("jsonrpc") != "2.0":
            return None

        if "error" in document:
            return None

        result = document.get("result")
        if not isinstance(result, dict):
            return None

        value = result.get("value")

        # bool is an int subcl -- checked.
        if isinstance(value, bool) or not isinstance(value, int):
            return None

        if value < 0:
            return None

        return value / self.LAMPORTS_PER_SOL

    def get_raw_lamports(self) -> Optional[int]:
        
        request_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [self._wallet_address],
        }

        payload = json.dumps(
            request_body,
            separators=(",", ":"),
        ).encode("utf-8")

        request = urllib.request.Request(
            self._rpc_endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        with self._lock:
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                ) as response:
                    if response.status != 200:
                        return None

                    raw_response = response.read()

            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                OSError,
            ):
                return None

        return self._extract_lamports(raw_response)

    @staticmethod
    def _extract_lamports(raw_response: bytes) -> Optional[int]:
        
        try:
            document = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None

        if not isinstance(document, dict):
            return None

        if document.get("jsonrpc") != "2.0" or "error" in document:
            return None

        result = document.get("result")
        if not isinstance(result, dict):
            return None

        value = result.get("value")

        if isinstance(value, bool) or not isinstance(value, int):
            return None

        return value if value >= 0 else None

if __name__ == "__main__":
    monitor = PublicLedgerBalanceMonitor(
        rpc_endpoint="https://api.mainnet-beta.solana.com",
        wallet_address="YOUR_PUBLIC_WALLET_ADDRESS",
        timeout=5.0,
    )

    balance = monitor.get_balance()

    if balance is not None:
        print(f"Balance: {balance:.9f} SOL")
    else:
        print("Balance unavailable")
