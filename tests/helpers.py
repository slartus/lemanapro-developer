from __future__ import annotations

import sqlite3


def list_reviews(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT review.*, mr.iid, mr.title, project.path_with_namespace
        FROM review
        JOIN mr ON mr.id = review.mr_id
        JOIN project ON project.id = mr.project_id
        ORDER BY review.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
