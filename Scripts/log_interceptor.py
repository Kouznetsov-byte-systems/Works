#!/usr/bin/env python3
"""
bridge_collector.py

Lightweight public issue collector and local append pipeline.

Python: 3.8+
Dependencies: standard library only

Architecture:
    GitHubIssueSource
          |
          v
    IssueCollector
          |
          v
    AppendPipeline ---> bridge.txt

The collector only accesses publicly available HTTP endpoints.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT = "bridge.txt"

DEFAULT_TIMEOUT = 15
DEFAULT_POLL_INTERVAL = 300       # five minutes
DEFAULT_MAX_ISSUES = 100

USER_AGENT = "OfflineIssueAuditCollector/1.0"

# Remove ASCII control characters except newline and tab.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Normalize excessive whitespace without destroying normal Markdown content.
_EXCESSIVE_NEWLINES = re.compile(r"\n{4,}")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    """Normalized issue representation."""

    repository: str
    number: int
    title: str
    body: str
    labels: List[str]
    url: str

    def identity(self) -> str:
        """Stable identifier used for local deduplication."""
        return "%s#%d" % (self.repository, self.number)


# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

class TextSanitizer:
    """
    Sanitizes externally supplied text before it enters the local log.

    The goal is not to interpret Markdown, but to make sure arbitrary remote
    text cannot inject malformed control characters into the local file.
    """

    @staticmethod
    def clean(value: Optional[str], max_length: int = 10000) -> str:
        if not value:
            return ""

        value = str(value)

        # Remove terminal/control characters.
        value = _CONTROL_CHARS.sub("", value)

        # Normalize line endings.
        value = value.replace("\r\n", "\n").replace("\r", "\n")

        # Avoid unbounded blank-line growth.
        value = _EXCESSIVE_NEWLINES.sub("\n\n\n", value)

        # Protect the local format from pathological input sizes.
        if len(value) > max_length:
            value = value[:max_length] + "\n[truncated]"

        return value.strip()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class HttpClient:
    """
    Small HTTP client based entirely on urllib.

    The object maintains conditional-request metadata per URL to avoid
    repeatedly downloading unchanged resources.
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._cache_lock = threading.Lock()
        self._validators: Dict[str, Dict[str, str]] = {}

    def get_json(self, url: str):
        request = self._make_request(url)
        status, body, headers = self._request(request)

        if status == 304:
            return None

        if not body:
            return None

        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON returned by %s: %s" % (url, exc))

    def _make_request(self, url: str) -> Request:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "",
        }

        with self._cache_lock:
            validators = self._validators.get(url, {})

        if "ETag" in validators:
            headers["If-None-Match"] = validators["ETag"]

        if "Last-Modified" in validators:
            headers["If-Modified-Since"] = validators["Last-Modified"]

        return Request(url, headers=headers, method="GET")

    def _request(self, request: Request):
        url = request.full_url

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.getcode()

                validators = {}

                etag = response.headers.get("ETag")
                modified = response.headers.get("Last-Modified")

                if etag:
                    validators["ETag"] = etag

                if modified:
                    validators["Last-Modified"] = modified

                if validators:
                    with self._cache_lock:
                        self._validators[url] = validators

                return status, body, response.headers

        except HTTPError as exc:
            # Conditional requests use 304 to signal no change.
            if exc.code == 304:
                return 304, b"", exc.headers

            raise

        except URLError as exc:
            raise ConnectionError(
                "Unable to retrieve %s: %s" % (url, exc.reason)
            )


# ---------------------------------------------------------------------------
# Public GitHub source
# ---------------------------------------------------------------------------

class GitHubIssueSource:
    """
    Retrieves public issues from a GitHub repository.

    Example repository:
        owner/repository

    Pull requests are excluded because GitHub exposes pull requests through
    the same /issues endpoint.
    """

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        repository: str,
        http_client: HttpClient,
        max_issues: int = DEFAULT_MAX_ISSUES,
    ):
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repository):
            raise ValueError("Invalid repository: %s" % repository)

        self.repository = repository
        self.http = http_client
        self.max_issues = max(1, max_issues)

    def fetch(self) -> List[Issue]:
        url = (
            "%s/repos/%s/issues"
            "?state=open"
            "&per_page=%d"
            "&page=1"
        ) % (
            self.API_BASE,
            self.repository,
            min(self.max_issues, 100),
        )

        payload = self.http.get_json(url)

        # 304 / unchanged.
        if payload is None:
            return []

        if not isinstance(payload, list):
            raise ValueError("Unexpected GitHub API response")

        issues = []

        for item in payload[:self.max_issues]:
            # /issues includes pull requests. Skip them.
            if "pull_request" in item:
                continue

            labels = []

            for label in item.get("labels", []):
                if isinstance(label, dict):
                    name = label.get("name")
                    if name is not None:
                        labels.append(str(name))

            issue = Issue(
                repository=self.repository,
                number=int(item.get("number", 0)),
                title=TextSanitizer.clean(item.get("title")),
                body=TextSanitizer.clean(item.get("body")),
                labels=[
                    TextSanitizer.clean(label, max_length=200)
                    for label in labels
                ],
                url=TextSanitizer.clean(item.get("html_url"), max_length=2000),
            )

            issues.append(issue)

        return issues


# ---------------------------------------------------------------------------
# Generic JSON issue-feed source
# ---------------------------------------------------------------------------

class JsonIssueFeedSource:
    """
    Optional adapter for an open issue-tracking feed that exposes JSON.

    Expected top-level format:

        {
            "issues": [
                {
                    "title": "...",
                    "body": "...",
                    "labels": ["bug", "documentation"]
                }
            ]
        }

    It also accepts a top-level JSON array.

    This keeps the collector independent of any particular development
    platform beyond GitHub.
    """

    def __init__(
        self,
        url: str,
        http_client: HttpClient,
        repository_name: str = "external-feed",
    ):
        self.url = url
        self.http = http_client
        self.repository_name = repository_name

    def fetch(self) -> List[Issue]:
        payload = self.http.get_json(self.url)

        if payload is None:
            return []

        if isinstance(payload, dict):
            records = payload.get("issues", [])
        elif isinstance(payload, list):
            records = payload
        else:
            raise ValueError("Unsupported JSON issue-feed format")

        result = []

        for index, item in enumerate(records):
            if not isinstance(item, dict):
                continue

            raw_labels = item.get("labels", [])

            if isinstance(raw_labels, str):
                raw_labels = [raw_labels]

            labels = []

            for label in raw_labels:
                if isinstance(label, dict):
                    label = label.get("name", "")

                if label:
                    labels.append(
                        TextSanitizer.clean(label, max_length=200)
                    )

            result.append(
                Issue(
                    repository=self.repository_name,
                    number=int(item.get("number", index + 1)),
                    title=TextSanitizer.clean(item.get("title")),
                    body=TextSanitizer.clean(item.get("body")),
                    labels=labels,
                    url=TextSanitizer.clean(
                        item.get("url"),
                        max_length=2000,
                    ),
                )
            )

        return result


# ---------------------------------------------------------------------------
# Local append pipeline
# ---------------------------------------------------------------------------

class AppendPipeline:
    """
    Thread-safe append-only writer.

    Existing bridge.txt contents are never replaced. Each issue is written
    as one standardized block.

    A small in-memory set prevents the same issue from being appended on
    every polling cycle.
    """

    BLOCK_SEPARATOR = "\n" + ("-" * 72) + "\n"

    def __init__(
        self,
        path: str = DEFAULT_OUTPUT,
        encoding: str = "utf-8",
    ):
        self.path = path
        self.encoding = encoding
        self._lock = threading.Lock()
        self._known_ids = set()

        self._load_existing_ids()

    def append(self, issue: Issue) -> bool:
        """
        Append one issue.

        Returns:
            True  -> a new block was written
            False -> issue was already recorded
        """

        identity = issue.identity()

        with self._lock:
            if identity in self._known_ids:
                return False

            block = self._format_issue(issue)

            directory = os.path.dirname(os.path.abspath(self.path))

            if not os.path.isdir(directory):
                os.makedirs(directory)

            with open(
                self.path,
                "a",
                encoding=self.encoding,
                newline="\n",
            ) as handle:
                handle.write(block)
                handle.flush()

                # Make the append durable where the platform supports it.
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass

            self._known_ids.add(identity)

        return True

    def append_many(self, issues: Iterable[Issue]) -> int:
        count = 0

        for issue in issues:
            if self.append(issue):
                count += 1

        return count

    def _format_issue(self, issue: Issue) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()

        labels = ", ".join(issue.labels) if issue.labels else "(none)"
        body = issue.body if issue.body else "(empty)"

        return (
            self.BLOCK_SEPARATOR
            + "[TASK]\n"
            + "Repository: %s\n" % TextSanitizer.clean(
                issue.repository,
                max_length=500,
            )
            + "Issue: #%d\n" % issue.number
            + "Title: %s\n" % issue.title
            + "Labels: %s\n" % labels
            + "URL: %s\n" % issue.url
            + "Collected: %s\n" % timestamp
            + "Body:\n"
            + body
            + "\n"
        )

    def _load_existing_ids(self) -> None:
        """
        Recover previously recorded issue identities.

        This allows the process to restart without duplicating everything
        already stored in bridge.txt.
        """

        if not os.path.isfile(self.path):
            return

        try:
            with open(
                self.path,
                "r",
                encoding=self.encoding,
                errors="replace",
            ) as handle:
                for line in handle:
                    if line.startswith("Repository: "):
                        repository = line[len("Repository: "):].strip()

                        # Repository line is followed by Issue: #N.
                        # Parsing the complete identity happens below.
                        current_repository = repository

                    elif line.startswith("Issue: #"):
                        match = re.match(
                            r"Issue:\s+#(\d+)",
                            line,
                        )

                        if match and "current_repository" in locals():
                            self._known_ids.add(
                                "%s#%s" % (
                                    current_repository,
                                    match.group(1),
                                )
                            )

        except OSError:
            # Failure to read an old log should not prevent collection.
            pass


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class IssueCollector:
    """
    Coordinates sources and the local append pipeline.
    """

    def __init__(
        self,
        sources: Iterable,
        pipeline: AppendPipeline,
    ):
        self.sources = list(sources)
        self.pipeline = pipeline

    def collect_once(self) -> int:
        total = 0

        for source in self.sources:
            try:
                issues = source.fetch()
                total += self.pipeline.append_many(issues)

            except Exception as exc:
                # A broken source must not terminate collection from other
                # independent sources.
                print(
                    "[collector] source failed: %s"
                    % exc
                )

        return total


# ---------------------------------------------------------------------------
# Lightweight polling service
# ---------------------------------------------------------------------------

class PollingService:
    """
    Long-running, low-overhead polling loop.

    Only one polling operation is performed at a time, deliberately avoiding
    a thread pool or asynchronous framework on resource-constrained systems.
    """

    def __init__(
        self,
        collector: IssueCollector,
        interval: int = DEFAULT_POLL_INTERVAL,
    ):
        self.collector = collector
        self.interval = max(10, interval)
        self._stop_event = threading.Event()

    def run_forever(self) -> None:
        print("[collector] polling started")

        while not self._stop_event.is_set():
            started = time.monotonic()

            try:
                count = self.collector.collect_once()

                print(
                    "[collector] appended %d new issue(s)"
                    % count
                )

            except Exception as exc:
                print(
                    "[collector] polling error: %s"
                    % exc
                )

            elapsed = time.monotonic() - started
            wait_time = max(1, self.interval - elapsed)

            # Event.wait() allows immediate shutdown instead of sleeping
            # through the entire polling interval.
            self._stop_event.wait(wait_time)

        print("[collector] polling stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Example application
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Configure sources here.

    No authentication is necessary for public GitHub repositories.
    """

    http = HttpClient(timeout=DEFAULT_TIMEOUT)

    sources = [
        GitHubIssueSource(
            repository="python/cpython",
            http_client=http,
            max_issues=25,
        ),

        # Additional public repositories can be added:
        #
        # GitHubIssueSource(
        #     repository="owner/project",
        #     http_client=http,
        #     max_issues=25,
        # ),

        # A compatible public JSON issue feed can also be added:
        #
        # JsonIssueFeedSource(
        #     url="https://example.org/issues.json",
        #     http_client=http,
        #     repository_name="example/project",
        # ),
    ]

    pipeline = AppendPipeline(
        path="bridge.txt",
    )

    collector = IssueCollector(
        sources=sources,
        pipeline=pipeline,
    )

    service = PollingService(
        collector=collector,
        interval=DEFAULT_POLL_INTERVAL,
    )

    try:
        service.run_forever()

    except KeyboardInterrupt:
        print("\n[collector] shutdown requested")
        service.stop()


if __name__ == "__main__":
    main()
