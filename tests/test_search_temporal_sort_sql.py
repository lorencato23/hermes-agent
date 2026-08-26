"""Guards for the two-phase SQL used by ``sort='newest'`` session search.

FTS5 feeds rows out in insertion order, so SQLite's bounded sorter can
reject a row before materialising it — unless the ordering is descending
timestamp, where every arriving row beats the current page and gets
materialised, ``snippet()`` and all. Every match paid for a snippet that
LIMIT then discarded.

``_build_fts_search_sql`` answers that one ordering with a two-phase
statement: sort a thin rowid-only inner query, hydrate only the survivors.
These tests pin both halves of the decision — the two-phase form must be
used for descending order and must NOT be used anywhere else (it is slower
there), and every ordering must return exactly what the original
single-phase statement returned.
"""

import pytest

from hermes_state import SessionDB


# The pre-optimization statement, kept verbatim as a behavioural oracle: the
# two-phase rewrite is only worth anything if it is indistinguishable from
# this one in its results.
LEGACY_SQL = """
    SELECT
        m.id,
        m.session_id,
        m.role,
        snippet(messages_fts, -1, '>>>', '<<<', '...', 40) AS snippet,
        m.timestamp,
        m.tool_name,
        s.source,
        s.model,
        s.started_at AS session_started
    FROM messages_fts
    JOIN messages m ON m.id = messages_fts.rowid
    JOIN sessions s ON s.id = m.session_id
    WHERE messages_fts MATCH ? AND (m.active = 1 OR m.compacted = 1)
    {order_by}
    LIMIT ? OFFSET ?
"""

SORT_ORDER_SQL = {
    None: "ORDER BY rank",
    "newest": "ORDER BY m.timestamp DESC, rank",
    "oldest": "ORDER BY m.timestamp ASC, rank",
}

def build_sql(**kwargs):
    """Call the SQL builder under test."""
    return SessionDB._build_fts_search_sql(**kwargs)


RESULT_FIELDS = (
    "id",
    "session_id",
    "role",
    "snippet",
    "timestamp",
    "tool_name",
    "source",
    "model",
    "session_started",
)


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    if not d._fts_enabled:
        pytest.skip("FTS5 unavailable in this SQLite build")
    # Three sources so source_filter / exclude_sources have something to bite
    # on, and enough rows that a LIMIT genuinely discards matches.
    for idx, source in enumerate(("cli", "telegram", "cron")):
        session_id = f"s{idx}"
        d.create_session(session_id=session_id, source=source, model="m")
        for n in range(40):
            role = ("user", "assistant", "tool")[n % 3]
            d.append_message(
                session_id,
                role=role,
                content=f"deploy the kernel step {n} in {source}",
                tool_name="bash" if role == "tool" else None,
                tool_call_id=f"c{idx}_{n}" if role == "tool" else None,
            )
    yield d
    d.close()


def _legacy_rows(db, sort, limit, offset):
    sql = LEGACY_SQL.format(order_by=SORT_ORDER_SQL[sort])
    with db._read_ctx() as conn:
        return [
            tuple(row) for row in conn.execute(sql, ("deploy", limit, offset))
        ]


def _new_rows(db, sort, limit, offset):
    return [
        tuple(row[field] for field in RESULT_FIELDS)
        for row in db.search_messages(
            "deploy",
            limit=limit,
            offset=offset,
            sort=sort,
            fields=RESULT_FIELDS,
        )
    ]


# ── Shape of the emitted SQL ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "order_by", ["ORDER BY rank", "ORDER BY m.timestamp ASC, rank"]
)
def test_non_descending_orderings_stay_single_phase(order_by):
    """Everything but descending order must NOT pay for the CTE.

    Rank ordering never sorts, and ascending order lets the bounded sorter
    reject rows before they are materialised — both already avoid the wasted
    snippets. Wrapping them costs a second MATCH and measured 25-75% slower.
    This test is what stops a well-meaning "apply it everywhere" follow-up.
    """
    sql, params = build_sql(
        table="messages_fts",
        where_clauses=["messages_fts MATCH ?"],
        params=["deploy"],
        order_by_sql=order_by,
        limit=20,
        offset=0,
    )
    assert "WITH page AS" not in sql
    assert sql.count("MATCH ?") == 1
    assert params == ["deploy", 20, 0]


def test_descending_ordering_is_two_phase():
    order_by = "ORDER BY m.timestamp DESC, rank"
    sql, params = build_sql(
        table="messages_fts",
        where_clauses=["messages_fts MATCH ?"],
        params=["deploy"],
        order_by_sql=order_by,
        limit=20,
        offset=5,
    )
    assert "WITH page AS" in sql
    # snippet() is evaluated exactly once, in the outer (post-LIMIT) query.
    assert sql.count("snippet(") == 1
    assert sql.index("LIMIT ? OFFSET ?") < sql.index("snippet(")
    # The outer MATCH re-binds the query so FTS5 auxiliary functions resolve.
    assert sql.count("MATCH ?") == 2
    assert params == ["deploy", 20, 5, "deploy"]


def test_inner_query_always_joins_sessions():
    """The inner query must select from the same row set as the outer one.

    The outer query INNER JOINs `sessions` to project source/model. If the
    inner query skipped that join, a message whose `sessions` row is missing
    would consume a LIMIT/OFFSET slot in the CTE and then be dropped by the
    outer join — a silently short (or empty) page. Both phases join it.
    """
    for where in (
        ["messages_fts MATCH ?", "(m.active = 1 OR m.compacted = 1)"],
        ["messages_fts MATCH ?", "s.source IN (?)"],
    ):
        sql, _ = build_sql(
            table="messages_fts",
            where_clauses=where,
            params=["deploy", "cli"][: len(where)],
            order_by_sql="ORDER BY m.timestamp DESC, rank",
            limit=20,
            offset=0,
        )
        assert sql.count("JOIN sessions") == 2, where


def test_orphan_message_does_not_shorten_the_page(tmp_path):
    """A message with no `sessions` row must not eat a slot in the page.

    This is the concrete failure the unconditional inner join prevents: with
    the join skipped, the orphan filled the CTE's LIMIT and the outer join
    discarded it, returning 0 rows where 10 were expected.
    """
    d = SessionDB(db_path=tmp_path / "state.db")
    if not d._fts_enabled:
        pytest.skip("FTS5 unavailable in this SQLite build")
    try:
        for idx in ("keep", "orphan"):
            d.create_session(session_id=idx, source="cli", model="m")
            for n in range(10):
                d.append_message(idx, role="user", content=f"deploy step {n}")
        # Drop the newer session's row with the FK disabled — the state an
        # FK-off migration window or a partial restore can leave behind.
        with d._read_ctx() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM sessions WHERE id = 'orphan'")
            conn.commit()

        rows = d.search_messages("deploy", limit=10, sort="newest", fields=RESULT_FIELDS)
        assert len(rows) == 10, "orphaned rows must not consume page slots"
        assert {r["session_id"] for r in rows} == {"keep"}
    finally:
        d.close()


@pytest.mark.parametrize(
    "order_by,expected",
    [
        ("ORDER BY rank", False),
        ("ORDER BY m.timestamp ASC, rank", False),
        ("ORDER BY m.timestamp DESC, rank", True),
    ],
)
def test_order_needs_late_hydration(order_by, expected):
    assert SessionDB._order_needs_late_hydration(order_by) is expected


# ── Behavioural equivalence against the pre-optimization statement ────────


@pytest.mark.parametrize("sort", [None, "newest", "oldest"])
@pytest.mark.parametrize("limit,offset", [(5, 0), (20, 0), (20, 7), (300, 0)])
def test_results_match_legacy_sql(db, sort, limit, offset):
    """Same rows, same order, same snippets — the whole point of the rewrite."""
    assert _new_rows(db, sort, limit, offset) == _legacy_rows(db, sort, limit, offset)


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_temporal_sort_respects_filters(db, sort):
    rows = db.search_messages(
        "deploy",
        limit=50,
        sort=sort,
        source_filter=["cli"],
        role_filter=["user", "assistant"],
        fields=RESULT_FIELDS,
    )
    assert rows, "filtered temporal search must still return matches"
    assert {r["source"] for r in rows} == {"cli"}
    assert {r["role"] for r in rows} <= {"user", "assistant"}
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps, reverse=(sort == "newest"))


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_temporal_sort_excludes_sources(db, sort):
    rows = db.search_messages(
        "deploy", limit=200, sort=sort, exclude_sources=["cron"], fields=RESULT_FIELDS
    )
    assert rows
    assert "cron" not in {r["source"] for r in rows}


@pytest.mark.parametrize("sort", [None, "newest", "oldest"])
def test_pagination_is_a_partition(db, sort):
    """Offset pages must tile the result set without gaps or repeats."""
    whole = _new_rows(db, sort, 40, 0)
    paged = _new_rows(db, sort, 20, 0) + _new_rows(db, sort, 20, 20)
    assert paged == whole
    assert len({row[0] for row in paged}) == len(paged)


def test_snippet_markers_survive_the_rewrite(db):
    rows = db.search_messages("kernel", limit=5, sort="newest", fields=RESULT_FIELDS)
    assert rows
    assert all(">>>kernel<<<" in row["snippet"] for row in rows)


def test_empty_result_set(db):
    assert db.search_messages("zzzunmatchedzzz", limit=10, sort="newest") == []
