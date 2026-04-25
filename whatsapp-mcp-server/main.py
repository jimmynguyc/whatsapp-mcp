from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    get_contact as whatsapp_get_contact,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    get_sender_name as whatsapp_get_sender_name,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    send_reaction as whatsapp_send_reaction,
    mark_read as whatsapp_mark_read,
    send_presence as whatsapp_send_presence,
    list_groups as whatsapp_list_groups,
    get_group_info as whatsapp_get_group_info,
    get_group_request_participants as whatsapp_get_group_request_participants,
    download_media as whatsapp_download_media
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")

@mcp.tool()
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts

@mcp.tool()
def get_contact(jid: str) -> Optional[Dict[str, Any]]:
    """Resolve a single WhatsApp contact by JID.

    Returns the contact's phone number, JID, and best-available display name
    (full_name → first_name → push_name → business_name). Useful for turning
    a sender JID from a group message into a human-readable name.

    Args:
        jid: The JID to resolve, e.g. "15551234567@s.whatsapp.net".
    """
    contact = whatsapp_get_contact(jid)
    return contact

@mcp.tool()
def resolve_sender_name(jid: str) -> str:
    """Return the best display name for a sender JID.

    Checks the contacts table (populated from the WhatsMeow contact book and
    per-message push names, including group participants) before falling back
    to the chats table or a fuzzy phone-number match. Returns the raw JID if
    nothing resolves.

    Args:
        jid: The sender JID.
    """
    return whatsapp_get_sender_name(jid)

@mcp.tool()
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
) -> List[Dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return chats

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return context

@mcp.tool()
def send_message(
    recipient: str,
    message: str,
    quoted_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
        quoted_message_id: Optional ID of a prior message in the same chat to quote
                           (threaded reply). The quoted message must already be in
                           the local store (i.e. seen via list_messages).

    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided"
        }

    success, status_message = whatsapp_send_message(recipient, message, quoted_message_id)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)
    
    Returns:
        A dictionary containing success status and a status message
    """
    
    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
    
    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def react_to_message(
    message_id: str,
    reaction: str,
    chat_jid: Optional[str] = None,
) -> Dict[str, Any]:
    """React to a WhatsApp message with an emoji.

    Args:
        message_id: The ID of the target message (as seen in list_messages output).
        reaction: The emoji to apply, e.g. "👍", "❤️", "😂". Pass "" to remove.
        chat_jid: Optional JID of the chat — helpful if the same message_id
                  could appear in multiple chats.

    Returns:
        A dictionary containing success status and a status message.
    """
    success, status_message = whatsapp_send_reaction(message_id, reaction, chat_jid)
    return {"success": success, "message": status_message}

@mcp.tool()
def mark_read(message_ids: List[str], chat_jid: str) -> Dict[str, Any]:
    """Send a read receipt (blue ticks) for one or more messages in a chat.

    Use this after your agent has actually processed an inbound message so the
    customer sees it as read. Only inbound messages need to be marked — own
    messages are filtered out server-side.

    Args:
        message_ids: IDs of the messages to acknowledge (as seen in list_messages).
        chat_jid: The JID of the chat the messages belong to.
    """
    success, status_message = whatsapp_mark_read(message_ids, chat_jid)
    return {"success": success, "message": status_message}


@mcp.tool()
def send_presence(state: str, chat_jid: Optional[str] = None) -> Dict[str, Any]:
    """Send typing/recording or online presence to WhatsApp.

    Use "composing" to show a "typing..." indicator while you compose a reply,
    then either send the message (which implicitly clears it) or send "paused"
    to clear it without sending. "available"/"unavailable" toggle the global
    online indicator.

    Args:
        state: One of "composing", "recording", "paused", "available", "unavailable".
        chat_jid: Required for composing/recording/paused; ignored for available/unavailable.
    """
    success, status_message = whatsapp_send_presence(state, chat_jid)
    return {"success": success, "message": status_message}


@mcp.tool()
def list_groups() -> List[Dict[str, Any]]:
    """List all WhatsApp groups the connected account is a member of.

    Each entry includes the group JID, name, topic, owner, creation time,
    participant count, admin/announce/lock flags, and the full participant list
    with resolved display names.
    """
    return whatsapp_list_groups()


@mcp.tool()
def get_group_info(group_jid: str) -> Optional[Dict[str, Any]]:
    """Fetch full info for a single WhatsApp group, including all participants.

    Each participant entry includes their primary JID, LID and phone-form JID
    (where available), admin status, and a resolved display name from the
    contacts table.

    Args:
        group_jid: The group's JID, e.g. "120363xxx@g.us".
    """
    return whatsapp_get_group_info(group_jid)


@mcp.tool()
def get_group_request_participants(group_jid: str) -> List[Dict[str, Any]]:
    """List pending join requests for a group that has approval enabled.

    Requires the connected account to be an admin of that group. Returns an
    empty list if the group has no pending requests or approval is off.

    Args:
        group_jid: The group's JID.
    """
    return whatsapp_get_group_request_participants(group_jid)


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }

if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')