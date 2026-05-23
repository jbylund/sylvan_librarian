"""Client for interacting with Scryfall Tagger GraphQL API."""

import collections
import logging
import re
import time

import requests
import tenacity
from cachebox import TTLCache, cached

logger = logging.getLogger(__name__)


class TaggerClient:
    """Client for interacting with Scryfall Tagger GraphQL API."""

    def __init__(self) -> None:
        """Initialize the TaggerClient."""
        self.session = requests.Session()
        self.csrf_token: str | None = None
        self.base_url = "https://tagger.scryfall.com"

        # Set up default headers
        self.session.headers.update(
            {
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Accept-Language": "en-US,en;q=0.5",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "DNT": "1",
                "Origin": self.base_url,
                "Pragma": "no-cache",
                "Priority": "u=4",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Sec-GPC": "1",
                "TE": "trailers",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:142.0) Gecko/20100101 Firefox/142.0",
            },
        )
        self._auth_cache = TTLCache(maxsize=2**10, ttl=10 * 60)
        self._request_timestamps = collections.deque(maxlen=500)

    def _get_csrf_token_from_meta(self, html_content: str) -> str | None:
        """Extract CSRF token from HTML meta tags."""
        pattern = r'<meta name="csrf-token" content="([^"]+)"'
        match = re.search(pattern, html_content)
        return match.group(1) if match else None

    @cached(
        cache=lambda self: self._auth_cache,
    )
    def authenticate(self) -> bool:
        """Authenticate with Scryfall tagger by fetching CSRF token and session cookie.

        Returns:
            bool: True if authentication successful, False otherwise.
        """
        # First, visit the main tagger page to get session cookie and CSRF token
        response = self.session.get(f"{self.base_url}/", timeout=30)
        response.raise_for_status()

        # Try to extract CSRF token from meta tags
        self.csrf_token = self._get_csrf_token_from_meta(response.text)

        if self.csrf_token:
            # Add CSRF token to headers
            self.session.headers["X-CSRF-Token"] = self.csrf_token
            logger.info("Successfully authenticated. CSRF token: %s...", self.csrf_token[:20])
            return True
        msg = "Could not extract CSRF token. Requests may fail."
        raise ValueError(msg)

    def fetch_tag(self, tag: str, *, page: int = 1, descendants: bool = True, include_taggings: bool = True) -> dict:
        """Fetch tag information from Scryfall tagger GraphQL API.

        Args:
            tag: The tag slug to fetch
            page: Page number for pagination (default: 1)
            descendants: Whether to include descendant tags (default: True)
            include_taggings: Whether to include taggings (card associations) (default: True)

        Returns:
            dict: GraphQL response data

        Raises:
            requests.RequestException: If the request fails
            ValueError: If authentication is required but not performed
        """
        self.authenticate()

        # Set referer to the specific tag page
        self.session.headers["Referer"] = f"{self.base_url}/tags/card/{tag}"

        variables = {
            "slug": tag,
            "type": "ORACLE_CARD_TAG",
        }

        tag_attrs = """
            fragment TagAttrs on Tag {
                description
                name
                namespace
                slug
            }
        """

        # Build query dynamically based on whether we want taggings
        if include_taggings:
            query = (
                """
                query FetchTag(
                    $type: TagType!
                    $slug: String!
                    $page: Int = 1
                    $descendants: Boolean = false
                ) {
                    tag: tagBySlug(type: $type, slug: $slug, aliasing: true) {
                        ...TagAttrs
                        scryfallUrl
                        description
                        ancestry {
                            tag {
                                ...TagAttrs
                            }
                        }
                        childTags {
                            ...TagAttrs
                        }
                        taggings(page: $page, descendants: $descendants) {
                            page
                            perPage
                            total
                            results {
                                relatedId
                                ...TaggingAttrs
                                card {
                                    ...CardAttrs
                                }
                            }
                        }
                    }
                }

                fragment CardAttrs on Card {
                    name
                }
                fragment TaggingAttrs on Tagging {
                    id
                    name
                }
                """
                + tag_attrs
            )
            variables.update(
                {
                    "descendants": descendants,
                    "page": page,
                },
            )
        else:
            query = (
                """
                query FetchTag(
                    $type: TagType!
                    $slug: String!
                ) {
                    tag: tagBySlug(type: $type, slug: $slug, aliasing: true) {
                        ...TagAttrs
                        scryfallUrl
                        description
                        ancestry {
                            tag {
                                ...TagAttrs
                            }
                        }
                        childTags {
                            ...TagAttrs
                        }
                    }
                }

                """
                + tag_attrs
            )

        def before_sleep_fn(retry_state: tenacity.RetryCallState) -> None:
            exc = retry_state.outcome.exception()
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")
                logger.warning(
                    "Tagger API request failed (attempt %s, status %s, retry-after: %s)",
                    retry_state.attempt_number,
                    exc.response.status_code,
                    retry_after,
                )
            else:
                logger.warning("Tagger API request failed (attempt %s): %s", retry_state.attempt_number, exc)

        _exp_wait = tenacity.wait_exponential(multiplier=0.1, min=0.1, max=10)

        def wait_fn(retry_state: tenacity.RetryCallState) -> float:
            exc = retry_state.outcome.exception()
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                header = exc.response.headers.get("Retry-After")
                if header is not None:
                    try:
                        return float(header)
                    except (ValueError, TypeError):
                        pass
            return _exp_wait(retry_state)

        retryer = tenacity.retry(
            wait=wait_fn,
            reraise=True,
            stop=tenacity.stop_after_attempt(7),
            before_sleep=before_sleep_fn,
        )

        def get_response() -> requests.Response:
            time.sleep(0.51)
            response = retryer(self.session.post)(
                f"{self.base_url}/graphql",
                json={
                    "query": query,
                    "variables": variables,
                    "operationName": "FetchTag",
                },
                timeout=30,
            )

            response.raise_for_status()
            self._request_timestamps.append(time.monotonic())
            return response

        response = retryer(get_response)()

        num_requests = len(self._request_timestamps)
        if num_requests > 1:
            oldest_request = self._request_timestamps[0]
            newest_request = self._request_timestamps[-1]
            duration = newest_request - oldest_request
            rate = num_requests / duration
            logger.info("Request rate is %.2f (inverse rate: %.3f)", rate, 1 / rate)
        parsed = response.json()

        # Check for GraphQL errors
        if "errors" in parsed:
            msg = f"GraphQL errors: {parsed['errors']}"
            raise ValueError(msg)

        # Check for data field
        if "data" not in parsed:
            msg = f"No data field in response: {parsed}"
            raise ValueError(msg)

        data = parsed["data"]
        if "tag" not in data:
            msg = f"No tag field in data: {data}"
            raise ValueError(msg)

        return data["tag"]

    def search_tags(self, *, name: str | None = None, page: int = 1) -> dict:
        """Search for tags using Scryfall tagger GraphQL API.

        Args:
            name: Optional tag name filter (default: None for all tags)
            page: Page number for pagination (default: 1)

        Returns:
            dict: GraphQL response data containing tags list

        Raises:
            requests.RequestException: If the request fails
            ValueError: If authentication is required but not performed
        """
        self.authenticate()

        # Set referer to the tags page
        self.session.headers["Referer"] = f"{self.base_url}/tags"

        query = """
            query SearchTags($input: TagSearchInput!) {
                tags(input: $input) {
                    page
                    perPage
                    results {
                        ...TagAttrs
                        taggingCount
                    }
                    total
                }
            }

            fragment TagAttrs on Tag {
                category
                createdAt
                creatorId
                id
                name
                namespace
                pendingRevisions
                slug
                status
                type
                hasExemplaryTagging
                description
            }
        """

        variables = {
            "input": {
                "name": name,
                "page": page,
            },
        }

        response = self.session.post(
            f"{self.base_url}/graphql",
            json={
                "query": query,
                "variables": variables,
                "operationName": "SearchTags",
            },
            timeout=30,
        )

        response.raise_for_status()
        parsed = response.json()

        # Check for GraphQL errors
        if "errors" in parsed:
            msg = f"GraphQL errors: {parsed['errors']}"
            raise ValueError(msg)

        # Check for data field
        if "data" not in parsed:
            msg = f"No data field in response: {parsed}"
            raise ValueError(msg)

        data = parsed["data"]
        if "tags" not in data:
            msg = f"No tags field in data: {data}"
            raise ValueError(msg)

        return data["tags"]
