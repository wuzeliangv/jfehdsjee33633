"""SQLite 数据访问层 - NodePool 主控。

- 单连接 + threading.RLock 保护,因为 stdlib sqlite3 在多线程下需要显式同步。
- isolation_level=None,所有写操作 autocommit;靠 RLock 提供互斥与隔离。
- 节点指纹规则见 ``compute_fingerprint``,用于跨被控去重。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
  agent_id      TEXT PRIMARY KEY,
  name          TEXT,
  token_hash    TEXT NOT NULL,
  registered_at REAL NOT NULL,
  last_seen     REAL,
  last_ip       TEXT,
  enabled       INTEGER DEFAULT 1,
  stats_json    TEXT,
  command_queue TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS nodes (
  fingerprint       TEXT PRIMARY KEY,
  host              TEXT NOT NULL,
  port              INTEGER NOT NULL,
  proto             TEXT,
  ip                TEXT,
  country           TEXT,
  country_code      TEXT,
  config_text       TEXT NOT NULL,
  probe_status      TEXT DEFAULT 'unknown',
  last_probe_at     REAL,
  consecutive_fails INTEGER DEFAULT 0,
  handshake_ms      INTEGER,
  score             REAL DEFAULT 0,
  first_seen        REAL NOT NULL,
  last_updated      REAL NOT NULL,
  source_agent_id   TEXT,
  upload_count      INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_nodes_country ON nodes(country_code, probe_status, score DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_probe   ON nodes(probe_status, last_probe_at);

CREATE TABLE IF NOT EXISTS node_reports (
  fingerprint  TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  last_test_at REAL NOT NULL,
  latency_ms   INTEGER,
  speed_kbps   INTEGER,
  success      INTEGER,
  PRIMARY KEY (fingerprint, agent_id)
);

CREATE TABLE IF NOT EXISTS feedback (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint  TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  reason       TEXT,
  reported_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_fp ON feedback(fingerprint, reported_at);
"""


_CA_RE = re.compile(r"<ca>(.*?)</ca>", re.DOTALL | re.IGNORECASE)


def _ca_fingerprint(config_text: str) -> str:
    """提取配置中的 CA 证书并 hash;若无内联 CA 则退回整文本 hash。"""
    m = _CA_RE.search(config_text or "")
    if m:
        ca = m.group(1).strip()
        return hashlib.sha256(ca.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256((config_text or "").encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(host: str, port: int, proto: str, config_text: str) -> str:
    """节点指纹:host:port:proto + CA 证书 hash 的 16 字符前缀。

    这样同 host:port 不同证书会被视为不同节点(VPN Gate 节点常见情况),
    而 config_text 中无关 whitespace/注释差异不会产生假性重复。
    """
    h = (host or "").strip().lower()
    p = int(port or 0)
    pr = (proto or "udp").strip().lower()
    ca_fp = _ca_fingerprint(config_text or "")
    return hashlib.sha256(f"{h}:{p}:{pr}:{ca_fp}".encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class MasterDB:
    """主控 SQLite 数据访问对象。所有方法线程安全。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True, parents=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        
        # 自动迁移旧数据表，增加 command_queue 字段
        try:
            cur = self.conn.execute("PRAGMA table_info(agents)")
            columns = [row["name"] for row in cur.fetchall()]
            if "command_queue" not in columns:
                self.conn.execute("ALTER TABLE agents ADD COLUMN command_queue TEXT DEFAULT '[]'")
        except Exception as e:
            print(f"[db] 迁移 command_queue 失败: {e}")

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ─── agents ──────────────────────────────────────────────────

    def register_agent(
        self,
        name: str,
        client_ip: str,
        fixed_agent_id: str | None = None,
    ) -> tuple[str, str]:
        """注册一个 agent。返回 (agent_id, plaintext_token)。

        若提供的 fixed_agent_id 已存在,视为 token 轮换:更新 token_hash、保留 agent_id。
        token 明文只在本次返回中提供,DB 中只存 SHA256。
        """
        with self.lock:
            now = time.time()
            agent_id = fixed_agent_id or str(uuid.uuid4())
            plain_token = secrets.token_urlsafe(32)
            th = hash_token(plain_token)
            existing = self.conn.execute(
                "SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE agents SET name = COALESCE(NULLIF(?, ''), name), "
                    "  token_hash = ?, last_ip = ?, last_seen = ?, enabled = 1 "
                    "WHERE agent_id = ?",
                    (name or "", th, client_ip, now, agent_id),
                )
            else:
                self.conn.execute(
                    "INSERT INTO agents (agent_id, name, token_hash, registered_at, "
                    "  last_seen, last_ip, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (agent_id, name or "", th, now, now, client_ip),
                )
            return agent_id, plain_token

    def authenticate_agent(self, agent_id: str, token: str) -> dict | None:
        if not agent_id or not token:
            return None
        th = hash_token(token)
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM agents WHERE agent_id = ? AND enabled = 1", (agent_id,)
            ).fetchone()
            if not row:
                return None
            if not secrets.compare_digest(row["token_hash"], th):
                return None
            return dict(row)

    def update_agent_heartbeat(
        self, agent_id: str, client_ip: str, stats: dict
    ) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE agents SET last_seen = ?, last_ip = ?, stats_json = ? "
                "WHERE agent_id = ?",
                (
                    time.time(),
                    client_ip,
                    json.dumps(stats or {}, ensure_ascii=False),
                    agent_id,
                ),
            )

    def list_agents(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT agent_id, name, registered_at, last_seen, last_ip, enabled, "
                "  stats_json FROM agents ORDER BY registered_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_agent_enabled(self, agent_id: str, enabled: bool) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE agents SET enabled = ? WHERE agent_id = ?",
                (1 if enabled else 0, agent_id),
            )
        return cur.rowcount > 0

    def delete_agent(self, agent_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM agents WHERE agent_id = ?", (agent_id,)
            )
        return cur.rowcount > 0

    def enqueue_command(self, agent_id: str, command: str) -> bool:
        with self.lock:
            try:
                row = self.conn.execute("SELECT command_queue FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            except sqlite3.OperationalError:
                # Column might be missing if migration failed on startup due to locking
                try:
                    self.conn.execute("ALTER TABLE agents ADD COLUMN command_queue TEXT DEFAULT '[]'")
                    row = self.conn.execute("SELECT command_queue FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                except Exception as e:
                    print(f"[db] enqueue_command alter table failed: {e}")
                    return False

            if not row:
                return False
            try:
                queue = json.loads(row["command_queue"] or "[]")
            except Exception:
                queue = []
            if command not in queue:
                queue.append(command)
            cur = self.conn.execute(
                "UPDATE agents SET command_queue = ? WHERE agent_id = ?",
                (json.dumps(queue), agent_id)
            )
            return cur.rowcount > 0

    def pop_commands(self, agent_id: str) -> list[str]:
        with self.lock:
            try:
                row = self.conn.execute("SELECT command_queue FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            except sqlite3.OperationalError:
                try:
                    self.conn.execute("ALTER TABLE agents ADD COLUMN command_queue TEXT DEFAULT '[]'")
                    row = self.conn.execute("SELECT command_queue FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                except Exception as e:
                    print(f"[db] pop_commands alter table failed: {e}")
                    return []
                    
            if not row:
                return []
            try:
                queue = json.loads(row["command_queue"] or "[]")
            except Exception:
                queue = []
            if queue:
                self.conn.execute(
                    "UPDATE agents SET command_queue = '[]' WHERE agent_id = ?",
                    (agent_id,)
                )
            return queue

    # ─── nodes ──────────────────────────────────────────────────

    def upsert_node(self, node: dict, source_agent_id: str) -> tuple[str, bool]:
        """UPSERT 节点。返回 (fingerprint, is_new)。

        is_new=True 表示是首次出现的节点;False 表示已有,本次只更新元信息和上传计数。
        """
        host = str(
            node.get("host") or node.get("remote_host") or node.get("ip") or ""
        ).strip()
        port = int(node.get("port") or node.get("remote_port") or 0)
        proto = str(node.get("proto") or "udp").strip().lower()
        config_text = str(node.get("config_text") or "")
        if not host or port <= 0 or not config_text:
            raise ValueError("节点缺少 host/port/config_text")
        fp = compute_fingerprint(host, port, proto, config_text)
        now = time.time()
        with self.lock:
            existing = self.conn.execute(
                "SELECT fingerprint FROM nodes WHERE fingerprint = ?", (fp,)
            ).fetchone()
            if existing:
                self.conn.execute(
                    """UPDATE nodes SET
                         last_updated = ?,
                         upload_count = upload_count + 1,
                         country      = COALESCE(NULLIF(?, ''), country),
                         country_code = COALESCE(NULLIF(?, ''), country_code),
                         ip           = COALESCE(NULLIF(?, ''), ip)
                       WHERE fingerprint = ?""",
                    (
                        now,
                        node.get("country") or "",
                        (node.get("country_code") or "").upper(),
                        node.get("ip") or "",
                        fp,
                    ),
                )
                is_new = False
            else:
                self.conn.execute(
                    """INSERT INTO nodes
                       (fingerprint, host, port, proto, ip, country, country_code,
                        config_text, probe_status, last_probe_at, consecutive_fails,
                        handshake_ms, score, first_seen, last_updated,
                        source_agent_id, upload_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', NULL, 0, NULL,
                               0, ?, ?, ?, 1)""",
                    (
                        fp,
                        host,
                        port,
                        proto,
                        node.get("ip") or "",
                        node.get("country") or "",
                        (node.get("country_code") or "").upper(),
                        config_text,
                        now,
                        now,
                        source_agent_id,
                    ),
                )
                is_new = True
        return fp, is_new

    def record_node_report(
        self,
        fingerprint: str,
        agent_id: str,
        latency_ms: int,
        speed_kbps: int,
        success: bool,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO node_reports
                   (fingerprint, agent_id, last_test_at, latency_ms, speed_kbps, success)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint, agent_id) DO UPDATE SET
                     last_test_at = excluded.last_test_at,
                     latency_ms   = excluded.latency_ms,
                     speed_kbps   = excluded.speed_kbps,
                     success      = excluded.success""",
                (
                    fingerprint,
                    agent_id,
                    time.time(),
                    int(latency_ms or 0),
                    int(speed_kbps or 0),
                    1 if success else 0,
                ),
            )

    def query_nodes_for_agent(
        self,
        country_code: str | None,
        limit: int,
        exclude_fingerprints: list[str],
    ) -> list[dict]:
        """按 country 过滤,按 score 排序,返回主控判定 alive 的节点。"""
        params: list[Any] = []
        where = ["probe_status = 'alive'"]
        if country_code:
            where.append("country_code = ?")
            params.append(country_code.upper())
        if exclude_fingerprints:
            placeholders = ",".join("?" * len(exclude_fingerprints))
            where.append(f"fingerprint NOT IN ({placeholders})")
            params.extend(exclude_fingerprints)
        sql = (
            "SELECT fingerprint, host, port, proto, ip, country, country_code, "
            "       config_text, handshake_ms, score, last_probe_at, first_seen "
            "FROM nodes WHERE " + " AND ".join(where) +
            " ORDER BY score DESC, COALESCE(handshake_ms, 999999) ASC LIMIT ?"
        )
        params.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def record_feedback(
        self, fingerprint: str, agent_id: str, reason: str
    ) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO feedback (fingerprint, agent_id, reason, reported_at) "
                "VALUES (?, ?, ?, ?)",
                (fingerprint, agent_id, (reason or "")[:200], time.time()),
            )
            # 被控反馈即刻降权,但不直接标 dead(交给探活 worker 判定)
            self.conn.execute(
                "UPDATE nodes SET consecutive_fails = consecutive_fails + 1, "
                "  score = score - 5 WHERE fingerprint = ?",
                (fingerprint,),
            )

    # ─── 探活 worker 使用 ────────────────────────────────────────

    def pick_probe_batch(self, batch_size: int, stale_seconds: int) -> list[dict]:
        """挑选要测活的节点:从未探过 或 距上次探活超过 stale_seconds 的。

        优先级:从未探过(last_probe_at IS NULL)> 上次探活最久的。
        """
        cutoff = time.time() - stale_seconds
        with self.lock:
            rows = self.conn.execute(
                """SELECT fingerprint, host, port, proto, config_text
                   FROM nodes
                   WHERE last_probe_at IS NULL OR last_probe_at < ?
                   ORDER BY (CASE WHEN last_probe_at IS NULL THEN 0 ELSE 1 END),
                            last_probe_at ASC
                   LIMIT ?""",
                (cutoff, batch_size),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_probe_result(
        self, fingerprint: str, alive: bool, handshake_ms: int | None
    ) -> None:
        now = time.time()
        with self.lock:
            if alive:
                self.conn.execute(
                    """UPDATE nodes SET
                         probe_status      = 'alive',
                         last_probe_at     = ?,
                         consecutive_fails = 0,
                         handshake_ms      = ?,
                         score             = MIN(100, score + 2)
                       WHERE fingerprint = ?""",
                    (now, handshake_ms, fingerprint),
                )
            else:
                # 任何一次失败都立即标记为 dead
                self.conn.execute(
                    """UPDATE nodes SET
                         consecutive_fails = consecutive_fails + 1,
                         last_probe_at     = ?,
                         probe_status      = 'dead',
                         score             = score - 5
                       WHERE fingerprint = ?""",
                    (now, fingerprint),
                )
                # 死亡节点立即清理
                self.conn.execute(
                    "DELETE FROM nodes WHERE fingerprint = ? AND probe_status = 'dead'",
                    (fingerprint,),
                )

    def delete_dead_nodes(self, dead_for_seconds: int) -> int:
        # 未知/未测活状态的节点，如果超过 10 分钟仍未成功，则立即清理
        cutoff = time.time() - 600
        with self.lock:
            cur = self.conn.execute(
                """DELETE FROM nodes 
                   WHERE probe_status = 'dead' 
                      OR ((probe_status = 'unknown' OR probe_status IS NULL) AND last_updated < ?)""",
                (cutoff,)
            )
        return cur.rowcount

    def delete_old_feedback(self, older_than_seconds: int) -> int:
        cutoff = time.time() - older_than_seconds
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM feedback WHERE reported_at < ?", (cutoff,)
            )
        return cur.rowcount

    # ─── 管理统计 ────────────────────────────────────────────────

    def stats(self) -> dict:
        with self.lock:
            total = self.conn.execute(
                "SELECT COUNT(*) AS c FROM nodes"
            ).fetchone()["c"]
            alive = self.conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE probe_status = 'alive'"
            ).fetchone()["c"]
            dead = self.conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE probe_status = 'dead'"
            ).fetchone()["c"]
            unknown = self.conn.execute(
                "SELECT COUNT(*) AS c FROM nodes "
                "WHERE probe_status = 'unknown' OR probe_status IS NULL"
            ).fetchone()["c"]
            agents = self.conn.execute(
                "SELECT COUNT(*) AS c FROM agents WHERE enabled = 1"
            ).fetchone()["c"]
            by_country = self.conn.execute(
                "SELECT country_code, COUNT(*) AS total, "
                "  SUM(CASE WHEN probe_status = 'alive' THEN 1 ELSE 0 END) AS alive "
                "FROM nodes GROUP BY country_code ORDER BY total DESC LIMIT 50"
            ).fetchall()
        return {
            "nodes": {"total": total, "alive": alive, "dead": dead, "unknown": unknown},
            "agents": agents,
            "by_country": [dict(r) for r in by_country],
        }
