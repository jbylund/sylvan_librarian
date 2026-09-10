"""Import Magic: The Gathering card data directly from Gatherer."""

import json
import re
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# HTTP status codes
HTTP_NOT_FOUND = 404

# (connect, read) seconds. Without a timeout a stalled Gatherer connection hangs the fetch forever;
# the Retry adapter only covers requests that fail, not ones that never return.
REQUEST_TIMEOUT = (10, 120)

# Upper bound on pages walked for one set or for the set list. Gatherer pages are dozens of items
# each and no set has thousands of cards, so reaching this means the paging contract changed (a 200
# that never turns into a 404 or an empty page) and the fetch should fail loudly rather than spin.
MAX_PAGES = 500

# Set codes come back from Gatherer and are spliced into a URL path and a filename. Anything outside
# this set (a slash, a dot, whitespace) is not a set code and must not reach either.
SET_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# The end of the JavaScript/JSON string literal the escaped items array sits in: the first double
# quote that is preceded by an even number of backslashes (so \" and \\\" stay inside the literal).
_STRING_LITERAL_END = re.compile(r'(?<!\\)(?:\\\\)*"')


def validate_set_code(set_code: str) -> str:
    """Return `set_code` if it is safe to use in a URL path and a filename, else raise ValueError."""
    if not isinstance(set_code, str) or not SET_CODE_RE.fullmatch(set_code):
        msg = f"Invalid set code: {set_code!r}"
        raise ValueError(msg)
    return set_code


class GathererFetcher:
    """Fetches card data from Gatherer website."""

    def __init__(self) -> None:
        """Initialize the fetcher with session configuration."""
        self.base_url = "https://gatherer.wizards.com"
        self.session = requests.Session()

        retries = Retry(
            total=3,
            backoff_factor=0.1,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount("http://", HTTPAdapter(max_retries=retries))

    def _extract_items_from_response(self, page_text: str) -> list:
        """Extract the items array from a Gatherer HTML response.

        The page embeds its data as JSON inside a JSON string, so the array arrives with every
        quote escaped. Rather than count brackets over the escaped text (a `]` inside a card's
        rules text would end the array early), unescape the string literal and let a real JSON
        decoder read exactly one array off the front of it.

        Args:
            page_text: The HTML response text from Gatherer

        Returns:
            A list of items parsed from the embedded JSON in the response

        Raises:
            ValueError: If the items array cannot be found or parsed
        """
        _, _, remainder = page_text.partition(r",\"items\":")
        if not remainder:
            msg = "No items array found in response"
            raise ValueError(msg)

        # Everything up to the end of the enclosing string literal is escaped JSON text; the array
        # is at its start, followed by the rest of the outer object, which raw_decode ignores.
        end = _STRING_LITERAL_END.search(remainder)
        escaped = remainder[: end.end() - 1] if end else remainder
        try:
            unescaped = json.loads(f'"{escaped}"')
            # raw_decode reads one value from the front and does not skip leading whitespace itself.
            items, _ = json.JSONDecoder().raw_decode(unescaped.lstrip())
        except ValueError as exc:
            msg = "Could not find end of items array"
            raise ValueError(msg) from exc
        if not isinstance(items, list):
            msg = "Items value is not an array"
            raise ValueError(msg)
        return items

    def _get_page(self, url: str, page: int) -> requests.Response | None:
        """GET one page; None once Gatherer answers 404, which is how it ends a listing."""
        response = self.session.get(url, params={"page": page}, timeout=REQUEST_TIMEOUT)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            if e.response.status_code == HTTP_NOT_FOUND:
                return None
            raise
        return response

    def fetch_all_sets(self) -> list:
        """Fetch the list of all set codes from Gatherer."""
        url = f"{self.base_url}/sets"
        # https://gatherer.wizards.com/sets?page=2
        all_sets = []

        for page in range(1, MAX_PAGES + 1):
            response = self._get_page(url, page)
            if response is None:
                break

            try:
                sets_array = self._extract_items_from_response(response.text)
            except ValueError:
                # No more items, we've reached the end
                break

            if not sets_array:
                break

            all_sets.extend(sets_array)
        else:
            msg = f"Set list did not end within {MAX_PAGES} pages"
            raise RuntimeError(msg)

        return [r["setCode"] for r in all_sets]

    def fetch_set(self, set_name: str) -> list:
        """Fetch all cards from a specific set."""
        set_name = validate_set_code(set_name)
        url = f"{self.base_url}/sets/{set_name}"
        set_cards = []
        for page in range(1, MAX_PAGES + 1):
            response = self._get_page(url, page)
            if response is None:
                break

            cards_array = self._extract_items_from_response(response.text)
            # A 200 with an empty page is the other way a listing ends; without this check the loop
            # would request page after page forever.
            if not cards_array:
                break
            set_cards.extend(cards_array)
        else:
            msg = f"Set {set_name} did not end within {MAX_PAGES} pages"
            raise RuntimeError(msg)
        return set_cards

    def save_set_to_json(self, set_name: str, output_dir: str = "gatherer_data") -> Path:
        """Fetch a set and save it to a JSON file."""
        set_name = validate_set_code(set_name)
        cards = self.fetch_set(set_name)

        # Create output directory if it doesn't exist
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Save to file
        output_file = output_path / f"{set_name}.json"
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(cards, f, indent=2, ensure_ascii=False)

        return output_file


def main() -> None:
    """Example usage: fetch TDM set."""
    fetcher = GathererFetcher()
    fetcher.fetch_all_sets()


if __name__ == "__main__":
    main()
