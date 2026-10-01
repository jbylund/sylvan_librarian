"""`legalities` must decode in a worker that mmapped the archive without importing it.

Its own module, and it spawns a SUBPROCESS on purpose. The format->shift registry
(`FORMAT_SHIFTS`) is a process-global that `reload()` populates as a side effect of interning, so
any test that builds the archive and reads it back in the same interpreter passes whether or not
mapping the archive populates the registry. That is not the deployment: `api/entrypoint.py` starts
multiple Bjoern worker processes and each one mmaps the shm archive some other process wrote.

What it guards: `legality_bits_to_pydict` iterates the registry and yields an EMPTY dict when it is
unpopulated rather than failing. Until `get_mmap` adopted the archive's shifts, the only caller of
`sync_format_shifts` was `bind_and_split_filter`, on the filter path -- so a worker answered
`"legalities": {}` from /cards/named, /cards/:id and /cards/collection until it happened to serve a
search, and those responses are cacheable.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

from card_engine import QueryEngine

_FIXTURE = Path(__file__).parent / "fixtures" / "engine_cards.json"

# Black Lotus in the fixture corpus: banned in more than one format, so a decode that silently
# produced the not_legal default for everything could not pass either.
_SCRYFALL_ID = "b0faa7f2-b547-42c4-a810-839da50dadfe"

# Runs in a FRESH interpreter: mmap only, no reload, no filter query.
_CHILD = textwrap.dedent(
    """
    import json, sys
    from card_engine import QueryEngine
    engine = QueryEngine(shm_path=sys.argv[1])
    row = engine.card_by_scryfall_id(sys.argv[2], ["name", "legalities"])
    json.dump(row, sys.stdout)
    """
)


def test_a_worker_that_only_mmapped_the_archive_still_decodes_legalities() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shm_path = str(Path(tmp) / f"sylvan_legality_{uuid.uuid4().hex}")

        # Parent: build the archive. This populates THIS process's registry, which is exactly why
        # the assertion below has to happen somewhere else.
        QueryEngine(shm_path=shm_path).reload(json.loads(_FIXTURE.read_text()))

        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _CHILD, shm_path, _SCRYFALL_ID],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"child failed: {proc.stderr}"
        row = json.loads(proc.stdout)

    assert row is not None, "the child could not find the card at all"
    legalities = row["legalities"]
    assert legalities, (
        "legalities decoded to {} in a worker that mmapped the archive without importing it -- "
        "the exact shape /cards/named served from a fresh worker process"
    )

    # Values, not just presence: an empty registry gives {}, but a registry carrying the wrong
    # shifts gives a full dict of wrong answers, which a truthiness check alone accepts.
    assert legalities["vintage"] == "restricted"
    assert legalities["duel"] == "banned"
    assert legalities["commander"] == "banned"
