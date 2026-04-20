import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSMEOW_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"


def _normalize_jid(jid: str) -> str:
    """Strip any device suffix from a JID user portion (e.g. "12345:7@s.whatsapp.net" -> "12345@s.whatsapp.net")."""
    if not jid or '@' not in jid:
        return jid
    user, server = jid.split('@', 1)
    if ':' in user:
        user = user.split(':', 1)[0]
    return f"{user}@{server}"


def _user_part(jid: str) -> str:
    """Return the user portion of a JID (the part before '@'), or the JID unchanged if no '@'."""
    return jid.split('@', 1)[0] if '@' in jid else jid


def _resolve_alt_jid(jid: str) -> Optional[str]:
    """Return the LID↔phone alternate for a JID by consulting whatsmeow's lid map.

    whatsmeow_lid_map stores raw user portions (no server suffix):
      lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL
    """
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
        """Determine if chat is a group based on JID pattern."""
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

def get_sender_name(sender_jid: str) -> str:
    """Resolve a sender JID to a human-readable name.

    Lookup order:
      1. contacts table by exact JID (full_name → first_name → push_name → business_name)
      2. contacts table by the alternate JID (LID↔phone via whatsmeow_lid_map)
      3. chats table by exact JID (direct-chat name)
      4. chats table by the alternate JID
      5. contacts/chats by phone-number substring
      6. the raw JID as a last resort
    """
    normalized = _normalize_jid(sender_jid)
    alt = _resolve_alt_jid(normalized)
    phone_part = _user_part(normalized)

    def _from_contacts(cursor, jid: str) -> Optional[str]:
        cursor.execute(
            "SELECT full_name, first_name, push_name, business_name FROM contacts WHERE jid = ? LIMIT 1",
            (jid,),
        )
        row = cursor.fetchone()
        if row:
            for candidate in row:
                if candidate:
                    return candidate
        return None

    def _from_chats(cursor, jid: str) -> Optional[str]:
        cursor.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (jid,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        return None

    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        for jid in filter(None, [normalized, alt]):
            if name := _from_contacts(cursor, jid):
                return name

        for jid in filter(None, [normalized, alt]):
            if name := _from_chats(cursor, jid):
                return name

        # Fuzzy phone-number match as last resort
        cursor.execute(
            """
            SELECT full_name, first_name, push_name, business_name
            FROM contacts
            WHERE jid LIKE ?
            LIMIT 1
            """,
            (f"%{phone_part}%",),
        )
        row = cursor.fetchone()
        if row:
            for candidate in row:
                if candidate:
                    return candidate

        cursor.execute("SELECT name FROM chats WHERE jid LIKE ? LIMIT 1", (f"%{phone_part}%",))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]

        return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> str:
    """Format a single message. Always includes Message ID and Chat JID so
    downstream tools (react_to_message, download_media, get_message_context)
    can target the message without a second lookup."""
    header = f"[{message.timestamp:%Y-%m-%d %H:%M:%S}]"
    if show_chat_info and message.chat_name:
        header += f" Chat: {message.chat_name}"

    tags = [f"ID: {message.id}", f"Chat JID: {message.chat_jid}"]
    if getattr(message, 'media_type', None):
        tags.append(f"media: {message.media_type}")
    meta = "[" + " | ".join(tags) + "]"

    try:
        sender_name = "Me" if message.is_from_me else get_sender_name(message.sender)
    except Exception as e:
        print(f"Error resolving sender name: {e}")
        sender_name = message.sender

    return f"{header} {meta} From: {sender_name}: {message.content}\n"

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> str:
    if not messages:
        return "No messages to display."
    return "".join(format_message(m, show_chat_info) for m in messages)

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
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
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
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact(jid: str) -> Optional[Contact]:
    """Resolve a single contact by JID using the contacts table, with chats and
    LID↔phone alternates as fallback. The returned Contact keeps the JID the
    caller asked about, but the name is resolved across both identifier forms."""
    if not jid:
        return None

    normalized = _normalize_jid(jid)
    alt = _resolve_alt_jid(normalized)

    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        phone_number = _user_part(normalized)

        for candidate_jid in filter(None, [normalized, alt]):
            cursor.execute(
                "SELECT full_name, first_name, push_name, business_name FROM contacts WHERE jid = ? LIMIT 1",
                (candidate_jid,),
            )
            row = cursor.fetchone()
            if row:
                full_name, first_name, push_name, business_name = row
                name = full_name or first_name or push_name or business_name or None
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
    """Search contacts by name or phone number.

    Unions the dedicated contacts table (populated from the WhatsMeow contact book
    and push-name updates — including group participants) with the chats table.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
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
                      LOWER(jid) LIKE LOWER(?)
                      OR LOWER(COALESCE(full_name, '')) LIKE LOWER(?)
                      OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?)
                      OR LOWER(COALESCE(push_name, '')) LIKE LOWER(?)
                      OR LOWER(COALESCE(business_name, '')) LIKE LOWER(?)
                  )

                UNION

                SELECT jid, name FROM chats
                WHERE jid NOT LIKE '%@g.us'
                  AND (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
            )
            ORDER BY name IS NULL, name, jid
            LIMIT 50
        """, (
            search_pattern, search_pattern, search_pattern, search_pattern, search_pattern,
            search_pattern, search_pattern,
        ))

        result = []
        for jid, name in cursor.fetchall():
            result.append(Contact(
                phone_number=jid.split('@')[0],
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
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
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
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
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
        # Validate input
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
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
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
    """Send an emoji reaction to a previously-received/sent message.

    Args:
        message_id: The ID of the message to react to.
        reaction: The emoji (e.g. "👍"). Pass an empty string to remove an existing reaction.
        chat_jid: Optional — disambiguates if the same message_id exists across chats.
    """
    if not message_id:
        return False, "message_id must be provided"
    try:
        payload = {"message_id": message_id, "reaction": reaction}
        if chat_jid:
            payload["chat_jid"] = chat_jid
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/react", json=payload)
        try:
            result = response.json()
        except json.JSONDecodeError:
            return False, f"Error parsing response: {response.text}"
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
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
