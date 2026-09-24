import json
import threading
import urllib.error
import urllib.request
from typing import Optional


class PublicLedgerBalanceMonitor:
    """
    Lightweight JSON-RPC balance monitor for a public Solana wallet.

    The monitor uses only Python's standard library and performs an
    unauthenticated HTTP JSON-RPC 2.0 request using getBalance.

    Returns:
        float: Balance in SOL on a successful query.
        None: If the request or response cannot be safely processed.
    """

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

        # Protects request state and makes the monitor safe for callers
        # that invoke get_balance() from multiple threads.
        self._lock = threading.Lock()

    def get_balance(self) -> Optional[float]:
        """
        Query the configured public RPC node.

        Returns the wallet balance in SOL, or None when the request fails,
        the server returns an RPC error, or the response has an unexpected
        structure.
        """
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

        # urllib's opener is thread-safe enough for this simple use case,
        # while the lock ensures this monitor performs one RPC operation
        # at a time and keeps its execution profile predictable.
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
        """Validate and normalize a JSON-RPC getBalance response."""
        try:
            document = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None

        if not isinstance(document, dict):
            return None

        # A valid JSON-RPC response should contain the expected version
        # and should not contain an RPC-level error.
        if document.get("jsonrpc") != "2.0":
            return None

        if "error" in document:
            return None

        result = document.get("result")
        if not isinstance(result, dict):
            return None

        value = result.get("value")

        # bool is an int subclass, so explicitly reject it.
        if isinstance(value, bool) or not isinstance(value, int):
            return None

        if value < 0:
            return None

        return value / self.LAMPORTS_PER_SOL

    def get_raw_lamports(self) -> Optional[int]:
        """
        Query the ledger and return the absolute balance in lamports.

        This avoids floating-point conversion when an exact integer value
        is required by an accounting/reporting layer.
        """
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
        """Extract the exact integer balance from an RPC response."""
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
