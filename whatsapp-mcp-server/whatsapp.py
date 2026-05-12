import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
import os.path
import requests
import json
import audio

STORE_DIR = os.environ.get('WHATSAPP_STORE_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store'))
MESSAGES_DB_PATH = os.path.join(STORE_DIR, 'messages.db')
WHATSMEOW_DB_PATH = os.path.join(STORE_DIR, 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

_indexes_ensured = False

def _ensure_indexes():
    """Create indexes once using a writable connection."""
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_jid ON messages(chat_jid);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
            CREATE INDEX IF NOT EXISTS idx_chats_last_msg ON chats(last_message_time);
        """)
        conn.close()
        _indexes_ensured = True
    except sqlite3.OperationalError as e:
        print(f"Warning: could not create indexes: {e}")
        _indexes_ensured = True


def _get_conn():
    """Get a readonly DB connection."""
    _ensure_indexes()
    conn = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _normalize_ts(ts: str) -> Optional[str]:
    """Convert any ISO-8601 timestamp to the format stored in the DB: '2026-05-12 10:00:00+00:00'."""
    try:
        # Handle 'Z' suffix
        ts = ts.replace('Z', '+00:00')
        dt = datetime.fromisoformat(ts)
        return dt.strftime('%Y-%m-%d %H:%M:%S+00:00')
    except (ValueError, TypeError):
        return None


_phone_cache: Dict[str, str] = {}


def _resolve_phone(jid: str) -> str:
    """Extract phone number from a JID. For @s.whatsapp.net, it's the user part.
    For @lid, resolve via whatsmeow_lid_map. Returns empty string if unresolvable."""
    if not jid or '@' not in jid:
        return ""
    if jid in _phone_cache:
        return _phone_cache[jid]
    user, server = jid.split('@', 1)
    if ':' in user:
        user = user.split(':', 1)[0]
    if server == 's.whatsapp.net':
        _phone_cache[jid] = user
        return user
    if server == 'lid':
        alt = _resolve_alt_jid(f"{user}@lid")
        if alt:
            phone = alt.split('@', 1)[0]
            _phone_cache[jid] = phone
            return phone
    return ""


def _bulk_resolve_phones(jids: List[str]) -> Dict[str, str]:
    """Resolve phone numbers for multiple JIDs in batch."""
    result = {}
    lid_users = []
    for jid in jids:
        if jid in _phone_cache:
            result[jid] = _phone_cache[jid]
            continue
        if '@' not in jid:
            continue
        user, server = jid.split('@', 1)
        if ':' in user:
            user = user.split(':', 1)[0]
        if server == 's.whatsapp.net':
            result[jid] = user
            _phone_cache[jid] = user
        elif server == 'lid':
            lid_users.append((jid, user))

    if lid_users:
        try:
            conn = sqlite3.connect(WHATSMEOW_DB_PATH)
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(lid_users))
            users = [u for _, u in lid_users]
            cursor.execute(f"SELECT lid, pn FROM whatsmeow_lid_map WHERE lid IN ({placeholders})", users)
            lid_to_pn = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()
            for jid, user in lid_users:
                if user in lid_to_pn:
                    result[jid] = lid_to_pn[user]
                    _phone_cache[jid] = lid_to_pn[user]
        except sqlite3.Error:
            pass

    return result


def _normalize_jid(jid: str) -> str:
    if not jid or '@' not in jid:
        return jid
    user, server = jid.split('@', 1)
    if ':' in user:
        user = user.split(':', 1)[0]
    return f"{user}@{server}"


def _user_part(jid: str) -> str:
    return jid.split('@', 1)[0] if '@' in jid else jid


def _resolve_alt_jid(jid: str) -> Optional[str]:
    if not jid or '@' not in jid:
        return None
    user, server = jid.split('@', 1)
    try:
        conn = sqlite3.connect(WHATSMEOW_DB_PATH)
        cursor = conn.cursor()
        if server == 'lid':
            cursor.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ? LIMIT 1", (user,))
            row = cursor.fetchone()
            if row and row[0]:
                return f"{row[0]}@s.whatsapp.net"
        elif server == 's.whatsapp.net':
            cursor.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ? LIMIT 1", (user,))
            row = cursor.fetchone()
            if row and row[0]:
                return f"{row[0]}@lid"
        return None
    except sqlite3.Error:
        return None
    finally:
        if 'conn' in locals():
            conn.close()

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]


# --- Name resolution cache ---
_name_cache: Dict[str, str] = {}

def _bulk_resolve_names(sender_jids: List[str], conn) -> Dict[str, str]:
    """Resolve multiple sender JIDs to display names in batch."""
    result = {}
    unknown = []
    for jid in sender_jids:
        if jid in _name_cache:
            result[jid] = _name_cache[jid]
        else:
            unknown.append(jid)

    if not unknown:
        return result

    cursor = conn.cursor()
    placeholders = ','.join('?' * len(unknown))

    # Batch contacts lookup
    cursor.execute(
        f"SELECT jid, full_name, first_name, push_name, business_name FROM contacts WHERE jid IN ({placeholders})",
        unknown,
    )
    for row in cursor.fetchall():
        jid = row[0]
        name = row[1] or row[2] or row[3] or row[4]
        if name:
            result[jid] = name
            _name_cache[jid] = name

    still_unknown = [j for j in unknown if j not in result]
    if still_unknown:
        placeholders = ','.join('?' * len(still_unknown))
        cursor.execute(
            f"SELECT jid, name FROM chats WHERE jid IN ({placeholders})",
            still_unknown,
        )
        for row in cursor.fetchall():
            if row[1]:
                result[row[0]] = row[1]
                _name_cache[row[0]] = row[1]

    return result


def get_sender_name(sender_jid: str) -> str:
    normalized = _normalize_jid(sender_jid)
    if normalized in _name_cache:
        return _name_cache[normalized]

    alt = _resolve_alt_jid(normalized)
    phone_part = _user_part(normalized)

    try:
        conn = _get_conn()
        cursor = conn.cursor()

        for jid in filter(None, [normalized, alt]):
            cursor.execute(
                "SELECT full_name, first_name, push_name, business_name FROM contacts WHERE jid = ? LIMIT 1",
                (jid,),
            )
            row = cursor.fetchone()
            if row:
                for candidate in row:
                    if candidate:
                        _name_cache[normalized] = candidate
                        return candidate

        for jid in filter(None, [normalized, alt]):
            cursor.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (jid,))
            row = cursor.fetchone()
            if row and row[0]:
                _name_cache[normalized] = row[0]
                return row[0]

        cursor.execute(
            "SELECT full_name, first_name, push_name, business_name FROM contacts WHERE jid LIKE ? LIMIT 1",
            (f"%{phone_part}%",),
        )
        row = cursor.fetchone()
        if row:
            for candidate in row:
                if candidate:
                    _name_cache[normalized] = candidate
                    return candidate

        cursor.execute("SELECT name FROM chats WHERE jid LIKE ? LIMIT 1", (f"%{phone_part}%",))
        row = cursor.fetchone()
        if row and row[0]:
            _name_cache[normalized] = row[0]
            return row[0]

        return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()


def _format_message(message: Message, name_map: Dict[str, str] = None, phone_map: Dict[str, str] = None, show_chat_info: bool = True) -> str:
    header = f"[{message.timestamp:%Y-%m-%d %H:%M:%S}]"
    if show_chat_info and message.chat_name:
        header += f" Chat: {message.chat_name}"

    tags = [f"ID: {message.id}", f"Chat JID: {message.chat_jid}"]
    if message.media_type:
        tags.append(f"media: {message.media_type}")
    meta = "[" + " | ".join(tags) + "]"

    if message.is_from_me:
        sender_str = "Me"
    else:
        if name_map and message.sender in name_map:
            sender_name = name_map[message.sender]
        else:
            try:
                sender_name = get_sender_name(message.sender)
            except Exception:
                sender_name = message.sender
        phone = (phone_map or {}).get(message.sender) or _resolve_phone(message.sender)
        sender_str = f"{sender_name} ({phone})" if phone else sender_name

    return f"{header} {meta} From: {sender_str}: {message.content}\n"


# Keep old name for external callers
def format_message(message: Message, show_chat_info: bool = True) -> str:
    return _format_message(message, show_chat_info=show_chat_info)


def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> str:
    if not messages:
        return "No messages to display."

    # Batch resolve all sender names and phone numbers
    unique_senders = list({m.sender for m in messages if not m.is_from_me})
    try:
        conn = _get_conn()
        name_map = _bulk_resolve_names(unique_senders, conn)
        conn.close()
    except sqlite3.Error:
        name_map = {}
    phone_map = _bulk_resolve_phones(unique_senders)

    return "".join(_format_message(m, name_map, phone_map, show_chat_info) for m in messages)


def _parse_msg_row(row) -> Message:
    return Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=row[2],
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7],
    )


_MSG_COLS = "m.timestamp, m.sender, c.name, m.content, m.is_from_me, c.jid, m.id, m.media_type"


def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> str:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        where_clauses = []
        params = []

        if after:
            after = _normalize_ts(after)
            if after is None:
                return f"Error: 'after' must be an ISO-8601 timestamp (e.g. '2026-05-12T10:00:00+00:00')"
            where_clauses.append("m.timestamp > ?")
            params.append(after)
        if before:
            before = _normalize_ts(before)
            if before is None:
                return f"Error: 'before' must be an ISO-8601 timestamp (e.g. '2026-05-12T10:00:00+00:00')"
            where_clauses.append("m.timestamp < ?")
            params.append(before)
        if sender_phone_number:
            where_clauses.append("m.sender = ?")
            params.append(sender_phone_number)
        if chat_jid:
            where_clauses.append("m.chat_jid = ?")
            params.append(chat_jid)
        if query:
            where_clauses.append("m.content LIKE ?")
            params.append(f"%{query}%")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        offset = page * limit

        sql = f"""
            SELECT {_MSG_COLS}
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            {where_sql}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor.execute(sql, tuple(params))
        messages = [_parse_msg_row(row) for row in cursor.fetchall()]

        if not messages:
            return "No messages to display."

        if include_context and messages:
            # Batch fetch context using window functions
            msg_ids = [(m.id, m.chat_jid) for m in messages]
            all_messages = _batch_context(cursor, msg_ids, context_before, context_after)
            return format_messages_list(all_messages, show_chat_info=True)

        return format_messages_list(messages, show_chat_info=True)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return f"Database error: {e}"
    finally:
        if 'conn' in locals():
            conn.close()


def _batch_context(cursor, msg_ids: List[Tuple[str, str]], ctx_before: int, ctx_after: int) -> List[Message]:
    """Fetch context for multiple messages in minimal queries, grouped by chat."""
    from collections import defaultdict

    by_chat = defaultdict(list)
    for mid, chat_jid in msg_ids:
        by_chat[chat_jid].append(mid)

    all_msgs = []
    seen = set()

    for chat_jid, mids in by_chat.items():
        placeholders = ','.join('?' * len(mids))

        # Get timestamps of target messages
        cursor.execute(
            f"SELECT id, timestamp FROM messages WHERE chat_jid = ? AND id IN ({placeholders})",
            [chat_jid] + mids,
        )
        targets = cursor.fetchall()
        if not targets:
            continue

        timestamps = [t[1] for t in targets]
        min_ts = min(timestamps)
        max_ts = max(timestamps)

        # Get lower bound timestamp (ctx_before messages before earliest target)
        cursor.execute("""
            SELECT m.timestamp FROM messages m
            WHERE m.chat_jid = ? AND m.timestamp < ?
            ORDER BY m.timestamp DESC LIMIT 1 OFFSET ?
        """, (chat_jid, min_ts, ctx_before - 1))
        row = cursor.fetchone()
        lower_ts = row[0] if row else min_ts

        # Get upper bound timestamp (ctx_after messages after latest target)
        cursor.execute("""
            SELECT m.timestamp FROM messages m
            WHERE m.chat_jid = ? AND m.timestamp > ?
            ORDER BY m.timestamp ASC LIMIT 1 OFFSET ?
        """, (chat_jid, max_ts, ctx_after - 1))
        row = cursor.fetchone()
        upper_ts = row[0] if row else max_ts

        cursor.execute(f"""
            SELECT {_MSG_COLS}
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ?
              AND m.timestamp >= ? AND m.timestamp <= ?
            ORDER BY m.timestamp ASC
        """, (chat_jid, lower_ts, upper_ts))

        for row in cursor.fetchall():
            msg = _parse_msg_row(row)
            key = (msg.id, msg.chat_jid)
            if key not in seen:
                seen.add(key)
                all_msgs.append(msg)

    all_msgs.sort(key=lambda m: m.timestamp, reverse=True)
    return all_msgs


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT {_MSG_COLS}, m.chat_jid
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = _parse_msg_row(msg_data)
        chat_jid = msg_data[8]
        ts = msg_data[0]

        cursor.execute(f"""
            SELECT {_MSG_COLS}
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ? AND m.timestamp < ?
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, (chat_jid, ts, before))
        before_messages = [_parse_msg_row(row) for row in cursor.fetchall()]

        cursor.execute(f"""
            SELECT {_MSG_COLS}
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ? AND m.timestamp > ?
            ORDER BY m.timestamp ASC
            LIMIT ?
        """, (chat_jid, ts, after))
        after_messages = [_parse_msg_row(row) for row in cursor.fetchall()]

        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        if include_last_message:
            base = """
                SELECT c.jid, c.name, c.last_message_time,
                       m.content, m.sender, m.is_from_me
                FROM chats c
                LEFT JOIN messages m ON c.jid = m.chat_jid
                    AND c.last_message_time = m.timestamp
            """
        else:
            base = """
                SELECT c.jid, c.name, c.last_message_time,
                       NULL, NULL, NULL
                FROM chats c
            """

        where_clauses = []
        params = []

        if query:
            where_clauses.append("(c.name LIKE ? OR c.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        order = "c.last_message_time DESC" if sort_by == "last_active" else "c.name"
        offset = page * limit

        sql = f"{base} {where_sql} ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(sql, tuple(params))

        result = []
        for row in cursor.fetchall():
            result.append(Chat(
                jid=row[0],
                name=row[1],
                last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
                last_message=row[3],
                last_sender=row[4],
                last_is_from_me=row[5],
            ))
        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact(jid: str) -> Optional[Contact]:
    if not jid:
        return None

    normalized = _normalize_jid(jid)
    alt = _resolve_alt_jid(normalized)

    try:
        conn = _get_conn()
        cursor = conn.cursor()
        phone_number = _resolve_phone(normalized) or _user_part(normalized)

        for candidate_jid in filter(None, [normalized, alt]):
            cursor.execute(
                "SELECT full_name, first_name, push_name, business_name FROM contacts WHERE jid = ? LIMIT 1",
                (candidate_jid,),
            )
            row = cursor.fetchone()
            if row:
                name = row[0] or row[1] or row[2] or row[3]
                if name:
                    return Contact(phone_number=phone_number, name=name, jid=normalized)

        for candidate_jid in filter(None, [normalized, alt]):
            cursor.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (candidate_jid,))
            row = cursor.fetchone()
            if row and row[0]:
                return Contact(phone_number=phone_number, name=row[0], jid=normalized)

        return None
    except sqlite3.Error as e:
        print(f"Database error in get_contact: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        search_pattern = f"%{query}%"

        cursor.execute("""
            SELECT jid, name FROM (
                SELECT
                    jid,
                    COALESCE(
                        NULLIF(full_name, ''),
                        NULLIF(first_name, ''),
                        NULLIF(push_name, ''),
                        NULLIF(business_name, '')
                    ) AS name
                FROM contacts
                WHERE jid NOT LIKE '%@g.us'
                  AND (
                      jid LIKE ?
                      OR COALESCE(full_name, '') LIKE ?
                      OR COALESCE(first_name, '') LIKE ?
                      OR COALESCE(push_name, '') LIKE ?
                      OR COALESCE(business_name, '') LIKE ?
                  )

                UNION

                SELECT jid, name FROM chats
                WHERE jid NOT LIKE '%@g.us'
                  AND (name LIKE ? OR jid LIKE ?)
            )
            ORDER BY name IS NULL, name, jid
            LIMIT 50
        """, (
            search_pattern, search_pattern, search_pattern, search_pattern, search_pattern,
            search_pattern, search_pattern,
        ))

        rows = cursor.fetchall()
        jids = [row[0] for row in rows]
        phone_map = _bulk_resolve_phones(jids)

        result = []
        for jid, name in rows:
            result.append(Contact(
                phone_number=phone_map.get(jid, jid.split('@')[0]),
                name=name,
                jid=jid,
            ))
        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        # Use two targeted queries instead of OR (which prevents index use)
        cursor.execute("""
            SELECT DISTINCT chat_jid FROM messages
            WHERE sender = ?
            UNION
            SELECT ? AS chat_jid
        """, (jid, jid))
        chat_jids = [row[0] for row in cursor.fetchall()]

        if not chat_jids:
            return []

        placeholders = ','.join('?' * len(chat_jids))
        cursor.execute(f"""
            SELECT c.jid, c.name, c.last_message_time,
                   m.content, m.sender, m.is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid IN ({placeholders})
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, chat_jids + [limit, page * limit])

        result = []
        for row in cursor.fetchall():
            result.append(Chat(
                jid=row[0],
                name=row[1],
                last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
                last_message=row[3],
                last_sender=row[4],
                last_is_from_me=row[5],
            ))
        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        # Two targeted queries instead of OR scan
        cursor.execute(f"""
            SELECT {_MSG_COLS} FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ?
            ORDER BY m.timestamp DESC LIMIT 1
        """, (jid,))
        by_sender = cursor.fetchone()

        cursor.execute(f"""
            SELECT {_MSG_COLS} FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ?
            ORDER BY m.timestamp DESC LIMIT 1
        """, (jid,))
        by_chat = cursor.fetchone()

        candidates = [r for r in [by_sender, by_chat] if r]
        if not candidates:
            return None

        best = max(candidates, key=lambda r: r[0])
        message = _parse_msg_row(best)
        return format_message(message)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        if include_last_message:
            cursor.execute("""
                SELECT c.jid, c.name, c.last_message_time,
                       m.content, m.sender, m.is_from_me
                FROM chats c
                LEFT JOIN messages m ON c.jid = m.chat_jid
                    AND c.last_message_time = m.timestamp
                WHERE c.jid = ?
            """, (chat_jid,))
        else:
            cursor.execute("""
                SELECT c.jid, c.name, c.last_message_time,
                       NULL, NULL, NULL
                FROM chats c
                WHERE c.jid = ?
            """, (chat_jid,))

        row = cursor.fetchone()
        if not row:
            return None

        return Chat(
            jid=row[0],
            name=row[1],
            last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
            last_message=row[3],
            last_sender=row[4],
            last_is_from_me=row[5],
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT c.jid, c.name, c.last_message_time,
                   m.content, m.sender, m.is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))

        row = cursor.fetchone()
        if not row:
            return None

        return Chat(
            jid=row[0],
            name=row[1],
            last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
            last_message=row[3],
            last_sender=row[4],
            last_is_from_me=row[5],
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def send_message(recipient: str, message: str, quoted_message_id: Optional[str] = None) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "message": message}
        if quoted_message_id:
            payload["quoted_message_id"] = quoted_message_id

        response = requests.post(url, json=payload, timeout=30)

        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        if not media_path:
            return False, "Media path must be provided"
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/send",
            json={"recipient": recipient, "media_path": media_path},
            timeout=60,
        )

        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        if not media_path:
            return False, "Media path must be provided"
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"

        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/send",
            json={"recipient": recipient, "media_path": media_path},
            timeout=60,
        )

        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_reaction(message_id: str, reaction: str, chat_jid: Optional[str] = None) -> Tuple[bool, str]:
    if not message_id:
        return False, "message_id must be provided"
    try:
        payload = {"message_id": message_id, "reaction": reaction}
        if chat_jid:
            payload["chat_jid"] = chat_jid
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/react", json=payload, timeout=30)
        try:
            result = response.json()
        except json.JSONDecodeError:
            return False, f"Error parsing response: {response.text}"
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"

def mark_read(message_ids: List[str], chat_jid: str) -> Tuple[bool, str]:
    if not message_ids:
        return False, "message_ids must be provided"
    if not chat_jid:
        return False, "chat_jid must be provided"
    try:
        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/mark-read",
            json={"message_ids": message_ids, "chat_jid": chat_jid},
            timeout=30,
        )
        try:
            result = response.json()
        except json.JSONDecodeError:
            return False, f"Error parsing response: {response.text}"
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def mark_chat(chat_jid: str, read: bool) -> Tuple[bool, str]:
    if not chat_jid:
        return False, "chat_jid must be provided"
    try:
        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/mark-chat",
            json={"chat_jid": chat_jid, "read": read},
            timeout=30,
        )
        try:
            result = response.json()
        except json.JSONDecodeError:
            return False, f"Error parsing response: {response.text}"
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def send_presence(state: str, chat_jid: Optional[str] = None) -> Tuple[bool, str]:
    if state not in ("composing", "recording", "paused", "available", "unavailable"):
        return False, "Invalid state"
    if state in ("composing", "recording", "paused") and not chat_jid:
        return False, "chat_jid is required for composing/recording/paused"
    try:
        payload = {"state": state}
        if chat_jid:
            payload["chat_jid"] = chat_jid
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/presence", json=payload, timeout=30)
        try:
            result = response.json()
        except json.JSONDecodeError:
            return False, f"Error parsing response: {response.text}"
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def list_groups() -> List[Dict[str, Any]]:
    try:
        response = requests.get(f"{WHATSAPP_API_BASE_URL}/groups", params={"mode": "list"}, timeout=30)
        if response.status_code != 200:
            return []
        return response.json() or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def get_group_info(group_jid: str) -> Optional[Dict[str, Any]]:
    if not group_jid:
        return None
    try:
        response = requests.get(
            f"{WHATSAPP_API_BASE_URL}/groups",
            params={"mode": "info", "jid": group_jid},
            timeout=30,
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None


def get_group_request_participants(group_jid: str) -> List[Dict[str, Any]]:
    if not group_jid:
        return []
    try:
        response = requests.get(
            f"{WHATSAPP_API_BASE_URL}/groups",
            params={"mode": "requests", "jid": group_jid},
            timeout=30,
        )
        if response.status_code != 200:
            return []
        return response.json() or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def get_group_invite_link(group_jid: str, reset: bool = False) -> Optional[str]:
    if not group_jid:
        return None
    try:
        response = requests.get(
            f"{WHATSAPP_API_BASE_URL}/groups",
            params={
                "mode": "invitelink",
                "jid": group_jid,
                "reset": "true" if reset else "false",
            },
            timeout=30,
        )
        if response.status_code != 200:
            return None
        return response.json().get("link")
    except (requests.RequestException, json.JSONDecodeError):
        return None


def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    try:
        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/download",
            json={"message_id": message_id, "chat_jid": chat_jid},
            timeout=60,
        )

        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                return result.get("path")
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None

    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
