package main

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"

	"bytes"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages
	db, err := sql.Open("sqlite3", "file:store/messages.db?_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);

		CREATE TABLE IF NOT EXISTS contacts (
			jid TEXT PRIMARY KEY,
			first_name TEXT,
			full_name TEXT,
			push_name TEXT,
			business_name TEXT,
			updated_at TIMESTAMP
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	// Add sender_jid column to messages for existing installs (idempotent).
	// Stores the full sender JID with server suffix (@s.whatsapp.net or @lid) so
	// reaction/revoke flows can reconstruct types.JID without guessing.
	if _, err := db.Exec(`ALTER TABLE messages ADD COLUMN sender_jid TEXT`); err != nil &&
		!strings.Contains(err.Error(), "duplicate column name") {
		db.Close()
		return nil, fmt.Errorf("failed to add sender_jid column: %v", err)
	}

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store or upsert a contact. Empty string fields do not overwrite existing non-empty values.
func (store *MessageStore) StoreContact(jid, firstName, fullName, pushName, businessName string) error {
	if jid == "" {
		return nil
	}
	_, err := store.db.Exec(
		`INSERT INTO contacts (jid, first_name, full_name, push_name, business_name, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?)
		 ON CONFLICT(jid) DO UPDATE SET
		   first_name    = CASE WHEN excluded.first_name    != '' THEN excluded.first_name    ELSE contacts.first_name    END,
		   full_name     = CASE WHEN excluded.full_name     != '' THEN excluded.full_name     ELSE contacts.full_name     END,
		   push_name     = CASE WHEN excluded.push_name     != '' THEN excluded.push_name     ELSE contacts.push_name     END,
		   business_name = CASE WHEN excluded.business_name != '' THEN excluded.business_name ELSE contacts.business_name END,
		   updated_at    = excluded.updated_at`,
		jid, firstName, fullName, pushName, businessName, time.Now(),
	)
	return err
}

// Resolve a display name for a contact JID. Returns "" if no name found.
// Priority: full_name → first_name → push_name → business_name.
func (store *MessageStore) GetContactName(jid string) string {
	var firstName, fullName, pushName, businessName sql.NullString
	err := store.db.QueryRow(
		"SELECT first_name, full_name, push_name, business_name FROM contacts WHERE jid = ?",
		jid,
	).Scan(&firstName, &fullName, &pushName, &businessName)
	if err != nil {
		return ""
	}
	if fullName.Valid && fullName.String != "" {
		return fullName.String
	}
	if firstName.Valid && firstName.String != "" {
		return firstName.String
	}
	if pushName.Valid && pushName.String != "" {
		return pushName.String
	}
	if businessName.Valid && businessName.String != "" {
		return businessName.String
	}
	return ""
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, senderJID, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages
		(id, chat_jid, sender, sender_jid, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, senderJID, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
	)
	return err
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}

	// For now, we're ignoring non-text messages
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient       string `json:"recipient"`
	Message         string `json:"message"`
	MediaPath       string `json:"media_path,omitempty"`
	QuotedMessageID string `json:"quoted_message_id,omitempty"`
}

// groupParticipantOut is the JSON shape of a single group participant.
type groupParticipantOut struct {
	JID          string `json:"jid"`
	LID          string `json:"lid,omitempty"`
	PhoneNumber  string `json:"phone_number,omitempty"`
	DisplayName  string `json:"display_name,omitempty"`
	ResolvedName string `json:"resolved_name,omitempty"`
	IsAdmin      bool   `json:"is_admin,omitempty"`
	IsSuperAdmin bool   `json:"is_super_admin,omitempty"`
}

// groupInfoOut is the JSON shape of a single group's info.
type groupInfoOut struct {
	JID                    string                `json:"jid"`
	Name                   string                `json:"name,omitempty"`
	Topic                  string                `json:"topic,omitempty"`
	Owner                  string                `json:"owner,omitempty"`
	Created                time.Time             `json:"created"`
	IsAnnounce             bool                  `json:"is_announce,omitempty"`
	IsLocked               bool                  `json:"is_locked,omitempty"`
	IsEphemeral            bool                  `json:"is_ephemeral,omitempty"`
	DisappearingTimer      uint32                `json:"disappearing_timer,omitempty"`
	IsJoinApprovalRequired bool                  `json:"is_join_approval_required,omitempty"`
	ParticipantCount       int                   `json:"participant_count"`
	Participants           []groupParticipantOut `json:"participants"`
}

// groupRequestOut is the JSON shape of a pending join request.
type groupRequestOut struct {
	JID          string    `json:"jid"`
	ResolvedName string    `json:"resolved_name,omitempty"`
	RequestedAt  time.Time `json:"requested_at"`
}

// formatGroupInfo flattens whatsmeow's GroupInfo struct for JSON output and
// enriches each participant with a resolved display name from the contacts table.
func formatGroupInfo(g *types.GroupInfo, messageStore *MessageStore) groupInfoOut {
	parts := make([]groupParticipantOut, 0, len(g.Participants))
	for _, p := range g.Participants {
		// Try resolving by primary JID first, then phone, then LID — first match wins.
		resolved := ""
		for _, candidate := range []string{p.JID.String(), p.PhoneNumber.String(), p.LID.String()} {
			if candidate == "" {
				continue
			}
			if name := messageStore.GetContactName(candidate); name != "" && name != candidate {
				resolved = name
				break
			}
		}
		parts = append(parts, groupParticipantOut{
			JID:          p.JID.String(),
			LID:          jidOrEmpty(p.LID),
			PhoneNumber:  jidOrEmpty(p.PhoneNumber),
			DisplayName:  p.DisplayName,
			ResolvedName: resolved,
			IsAdmin:      p.IsAdmin,
			IsSuperAdmin: p.IsSuperAdmin,
		})
	}
	owner := ""
	if !g.OwnerJID.IsEmpty() {
		owner = g.OwnerJID.String()
	} else if !g.OwnerPN.IsEmpty() {
		owner = g.OwnerPN.String()
	}
	return groupInfoOut{
		JID:                    g.JID.String(),
		Name:                   g.Name,
		Topic:                  g.Topic,
		Owner:                  owner,
		Created:                g.GroupCreated,
		IsAnnounce:             g.IsAnnounce,
		IsLocked:               g.IsLocked,
		IsEphemeral:            g.IsEphemeral,
		DisappearingTimer:      g.DisappearingTimer,
		IsJoinApprovalRequired: g.IsJoinApprovalRequired,
		// GetJoinedGroups returns ParticipantCount=0 even when participants are populated;
		// trust the actual list length when the count looks unset.
		ParticipantCount: pickInt(g.ParticipantCount, len(g.Participants)),
		Participants:     parts,
	}
}

func jidOrEmpty(j types.JID) string {
	if j.IsEmpty() {
		return ""
	}
	return j.String()
}

func pickInt(primary, fallback int) int {
	if primary > 0 {
		return primary
	}
	return fallback
}

// MarkReadRequest is the body for POST /api/mark-read.
type MarkReadRequest struct {
	MessageIDs []string `json:"message_ids"`
	ChatJID    string   `json:"chat_jid"`
}

// PresenceRequest is the body for POST /api/presence.
// State must be one of: "composing" (typing), "recording" (voice), "paused",
// "available" (global online), "unavailable" (global offline).
// ChatJID is required for composing/recording/paused; ignored for available/unavailable.
type PresenceRequest struct {
	ChatJID string `json:"chat_jid,omitempty"`
	State   string `json:"state"`
}

// buildQuoteContext looks up the quoted message in the local store and returns
// a ContextInfo suitable for attaching to an outbound Message. Returns nil if
// the message cannot be found — caller decides whether that is a hard error.
func buildQuoteContext(messageStore *MessageStore, quotedMessageID, chatJID string) (*waProto.ContextInfo, error) {
	if quotedMessageID == "" {
		return nil, nil
	}
	var content, senderJID sql.NullString
	var isFromMe bool
	err := messageStore.db.QueryRow(
		"SELECT content, sender_jid, is_from_me FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1",
		quotedMessageID, chatJID,
	).Scan(&content, &senderJID, &isFromMe)
	if err != nil {
		return nil, fmt.Errorf("quoted message %s not found in chat %s: %w", quotedMessageID, chatJID, err)
	}

	participant := senderJID.String
	if isFromMe || participant == "" {
		// Our own message or pre-migration row: fall back to our own JID.
		if ownJID := ownJIDString(); ownJID != "" {
			participant = ownJID
		}
	}

	ctx := &waProto.ContextInfo{
		StanzaID: proto.String(quotedMessageID),
		QuotedMessage: &waProto.Message{
			Conversation: proto.String(content.String),
		},
	}
	if participant != "" {
		ctx.Participant = proto.String(participant)
	}
	return ctx, nil
}

// ownJIDString returns the connected account's bare JID as a string, or "" if unknown.
// Package-level state; set in main() once login completes.
var ownJIDStringVal string

func ownJIDString() string { return ownJIDStringVal }

// Function to send a WhatsApp message
func sendWhatsAppMessage(client *whatsmeow.Client, messageStore *MessageStore, recipient string, message string, mediaPath string, quotedMessageID string) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err)
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	// Resolve optional quoted-message context from the local store.
	var quoteCtx *waProto.ContextInfo
	if quotedMessageID != "" {
		quoteCtx, err = buildQuoteContext(messageStore, quotedMessageID, recipientJID.String())
		if err != nil {
			return false, fmt.Sprintf("Cannot quote: %v", err)
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err)
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err)
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				ContextInfo:   quoteCtx,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err)
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
				ContextInfo:   quoteCtx,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				ContextInfo:   quoteCtx,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				ContextInfo:   quoteCtx,
			}
		}
	} else {
		if quoteCtx != nil {
			// Quoted replies must use ExtendedTextMessage; Conversation cannot carry ContextInfo.
			msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
				Text:        proto.String(message),
				ContextInfo: quoteCtx,
			}
		} else {
			msg.Conversation = proto.String(message)
		}
	}

	// Send message
	_, err = client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err)
	}

	return true, fmt.Sprintf("Message sent to %s", recipient)
}

// markMessagesRead marks one or more messages as read, emitting a read receipt
// to the sender. For group chats, the receipt must name the original sender JID
// of each message — we look that up from the local store. Messages from
// different senders within the same call are dispatched in separate receipts.
func markMessagesRead(client *whatsmeow.Client, messageStore *MessageStore, req MarkReadRequest) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}
	if len(req.MessageIDs) == 0 {
		return false, "message_ids is required"
	}
	if req.ChatJID == "" {
		return false, "chat_jid is required"
	}
	chatJID, err := types.ParseJID(req.ChatJID)
	if err != nil {
		return false, fmt.Sprintf("Invalid chat_jid %q: %v", req.ChatJID, err)
	}

	// Group message IDs by sender so each receipt carries the right Participant.
	bySender := map[string][]types.MessageID{}
	for _, id := range req.MessageIDs {
		var senderJIDStr sql.NullString
		var isFromMe bool
		err := messageStore.db.QueryRow(
			"SELECT sender_jid, is_from_me FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1",
			id, req.ChatJID,
		).Scan(&senderJIDStr, &isFromMe)
		if err != nil {
			return false, fmt.Sprintf("Message %s not found in chat %s: %v", id, req.ChatJID, err)
		}
		if isFromMe {
			// Own messages don't need a read receipt from us.
			continue
		}
		sender := senderJIDStr.String
		if sender == "" {
			// Pre-migration 1:1 row: sender == chat partner.
			if chatJID.Server == types.GroupServer {
				return false, fmt.Sprintf("Cannot mark group message %s read: sender unknown (pre-migration row)", id)
			}
			sender = chatJID.String()
		}
		bySender[sender] = append(bySender[sender], types.MessageID(id))
	}

	if len(bySender) == 0 {
		return true, "No inbound messages to mark"
	}

	for senderStr, ids := range bySender {
		senderJID, err := types.ParseJID(senderStr)
		if err != nil {
			return false, fmt.Sprintf("Invalid sender JID %q: %v", senderStr, err)
		}
		if err := client.MarkRead(context.Background(), ids, time.Now(), chatJID, senderJID); err != nil {
			return false, fmt.Sprintf("Failed to mark read: %v", err)
		}
	}
	return true, fmt.Sprintf("Marked %d message(s) read", len(req.MessageIDs))
}

// sendPresence dispatches either a global presence (available/unavailable) or
// a per-chat presence (composing/recording/paused). Global presence may require
// the account to have opted into presence reporting; per-chat works always.
func sendPresence(client *whatsmeow.Client, req PresenceRequest) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}
	switch req.State {
	case "available":
		if err := client.SendPresence(context.Background(), types.PresenceAvailable); err != nil {
			return false, fmt.Sprintf("Failed to send presence: %v", err)
		}
		return true, "Presence: available"
	case "unavailable":
		if err := client.SendPresence(context.Background(), types.PresenceUnavailable); err != nil {
			return false, fmt.Sprintf("Failed to send presence: %v", err)
		}
		return true, "Presence: unavailable"
	case "composing", "recording", "paused":
		if req.ChatJID == "" {
			return false, "chat_jid is required for composing/recording/paused"
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			return false, fmt.Sprintf("Invalid chat_jid %q: %v", req.ChatJID, err)
		}
		var state types.ChatPresence
		var media types.ChatPresenceMedia
		switch req.State {
		case "composing":
			state = types.ChatPresenceComposing
			media = types.ChatPresenceMediaText
		case "recording":
			state = types.ChatPresenceComposing
			media = types.ChatPresenceMediaAudio
		case "paused":
			state = types.ChatPresencePaused
		}
		if err := client.SendChatPresence(context.Background(), chatJID, state, media); err != nil {
			return false, fmt.Sprintf("Failed to send chat presence: %v", err)
		}
		return true, fmt.Sprintf("Chat presence: %s", req.State)
	default:
		return false, fmt.Sprintf("Unknown state %q (expected composing/recording/paused/available/unavailable)", req.State)
	}
}

// ReactToMessageRequest is the body for POST /api/react.
type ReactToMessageRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid,omitempty"`
	Reaction  string `json:"reaction"` // Empty string removes an existing reaction.
}

// reactToMessage sends a reaction to a previously-seen message. It looks up the
// chat/sender from the messages table so callers only need the message ID; an
// explicit ChatJID in the request takes precedence when both are stored.
func reactToMessage(client *whatsmeow.Client, messageStore *MessageStore, req ReactToMessageRequest) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}
	if req.MessageID == "" {
		return false, "message_id is required"
	}

	var chatJIDStr, senderJIDStr sql.NullString
	var isFromMe bool
	query := "SELECT chat_jid, sender_jid, is_from_me FROM messages WHERE id = ?"
	args := []any{req.MessageID}
	if req.ChatJID != "" {
		query += " AND chat_jid = ?"
		args = append(args, req.ChatJID)
	}
	query += " LIMIT 1"

	if err := messageStore.db.QueryRow(query, args...).Scan(&chatJIDStr, &senderJIDStr, &isFromMe); err != nil {
		if err == sql.ErrNoRows {
			return false, "Message not found in local store"
		}
		return false, fmt.Sprintf("Failed to look up message: %v", err)
	}

	chatJID, err := types.ParseJID(chatJIDStr.String)
	if err != nil {
		return false, fmt.Sprintf("Invalid chat JID %q: %v", chatJIDStr.String, err)
	}

	// Determine the reaction's sender arg for BuildReaction.
	// For our own messages, pass an empty JID (whatsmeow sets FromMe=true).
	// Otherwise pass the original sender — this is what actually identifies the target message.
	var senderJID types.JID
	if !isFromMe {
		if senderJIDStr.Valid && senderJIDStr.String != "" {
			senderJID, err = types.ParseJID(senderJIDStr.String)
			if err != nil {
				return false, fmt.Sprintf("Invalid sender JID %q: %v", senderJIDStr.String, err)
			}
		} else if chatJID.Server != types.GroupServer {
			// Pre-migration 1:1 row with no sender_jid: the chat partner is the sender.
			senderJID = chatJID
		} else {
			return false, "Cannot react: original sender unknown for this group message (pre-migration row)"
		}
	}

	reactionMsg := client.BuildReaction(chatJID, senderJID, req.MessageID, req.Reaction)
	if _, err := client.SendMessage(context.Background(), chatJID, reactionMsg); err != nil {
		return false, fmt.Sprintf("Failed to send reaction: %v", err)
	}

	if req.Reaction == "" {
		return true, "Reaction removed"
	}
	return true, fmt.Sprintf("Reacted with %s", req.Reaction)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User

	// Capture push name for the sender — this is our best source of truth for group participants
	// whose full/first name may not be in our contact book. Mirror onto the alt JID so that
	// lookups by either @lid or @s.whatsapp.net resolve.
	if msg.Info.PushName != "" && !msg.Info.IsFromMe {
		if err := storeContactWithAlt(client, messageStore, msg.Info.Sender, "", "", msg.Info.PushName, ""); err != nil {
			logger.Warnf("Failed to store push name for %s: %v", msg.Info.Sender.String(), err)
		}
	}

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// Store message in database
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		msg.Info.Sender.ToNonAD().String(),
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Log based on message type
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := fmt.Sprintf("store/%s", strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	localPath = fmt.Sprintf("%s/%s", chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, filename, absPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int) {
	// Handler for sending messages
	http.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}

		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		fmt.Println("Received request to send message", req.Message, req.MediaPath)

		// Send the message
		success, message := sendWhatsAppMessage(client, messageStore, req.Recipient, req.Message, req.MediaPath, req.QuotedMessageID)
		fmt.Println("Message sent", success, message)
		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Set appropriate status code
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}

		// Send response
		json.NewEncoder(w).Encode(SendMessageResponse{
			Success: success,
			Message: message,
		})
	})

	// Handler for downloading media
	http.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// Handler for marking messages as read (outbound read receipts)
	http.HandleFunc("/api/mark-read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req MarkReadRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		success, message := markMessagesRead(client, messageStore, req)
		w.Header().Set("Content-Type", "application/json")
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		json.NewEncoder(w).Encode(SendMessageResponse{Success: success, Message: message})
	})

	// Handler for typing/online presence
	http.HandleFunc("/api/presence", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req PresenceRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		success, message := sendPresence(client, req)
		w.Header().Set("Content-Type", "application/json")
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		json.NewEncoder(w).Encode(SendMessageResponse{Success: success, Message: message})
	})

	// Handler for reacting to messages (emoji reaction)
	http.HandleFunc("/api/react", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req ReactToMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		success, message := reactToMessage(client, messageStore, req)
		w.Header().Set("Content-Type", "application/json")
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		json.NewEncoder(w).Encode(SendMessageResponse{Success: success, Message: message})
	})

	// Handler for looking up contact info
	http.HandleFunc("/api/contacts", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		jidParam := r.URL.Query().Get("jid")
		queryParam := r.URL.Query().Get("q")
		w.Header().Set("Content-Type", "application/json")

		type contactRow struct {
			JID          string `json:"jid"`
			FirstName    string `json:"first_name,omitempty"`
			FullName     string `json:"full_name,omitempty"`
			PushName     string `json:"push_name,omitempty"`
			BusinessName string `json:"business_name,omitempty"`
			DisplayName  string `json:"display_name,omitempty"`
		}

		scanRow := func(rows *sql.Rows) (contactRow, error) {
			var c contactRow
			var first, full, push, business sql.NullString
			if err := rows.Scan(&c.JID, &first, &full, &push, &business); err != nil {
				return c, err
			}
			c.FirstName = first.String
			c.FullName = full.String
			c.PushName = push.String
			c.BusinessName = business.String
			c.DisplayName = messageStore.GetContactName(c.JID)
			return c, nil
		}

		if jidParam != "" {
			rows, err := messageStore.db.Query(
				"SELECT jid, first_name, full_name, push_name, business_name FROM contacts WHERE jid = ?",
				jidParam,
			)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			defer rows.Close()
			if rows.Next() {
				c, err := scanRow(rows)
				if err != nil {
					http.Error(w, err.Error(), http.StatusInternalServerError)
					return
				}
				json.NewEncoder(w).Encode(c)
				return
			}
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]string{"error": "not found"})
			return
		}

		// Search mode: query by name substring
		like := "%" + queryParam + "%"
		rows, err := messageStore.db.Query(
			`SELECT jid, first_name, full_name, push_name, business_name FROM contacts
			 WHERE ? = '' OR jid LIKE ? OR first_name LIKE ? OR full_name LIKE ? OR push_name LIKE ? OR business_name LIKE ?
			 ORDER BY COALESCE(NULLIF(full_name,''), NULLIF(first_name,''), NULLIF(push_name,''), NULLIF(business_name,''), jid)
			 LIMIT 100`,
			queryParam, like, like, like, like, like,
		)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer rows.Close()
		out := []contactRow{}
		for rows.Next() {
			c, err := scanRow(rows)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			out = append(out, c)
		}
		json.NewEncoder(w).Encode(out)
	})

	// Handler for groups: list joined / get one / list pending join requests
	http.HandleFunc("/api/groups", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")

		mode := r.URL.Query().Get("mode") // "list" (default), "info", "requests"
		if mode == "" {
			mode = "list"
		}

		switch mode {
		case "list":
			groups, err := client.GetJoinedGroups(context.Background())
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
				return
			}
			out := make([]groupInfoOut, 0, len(groups))
			for _, g := range groups {
				out = append(out, formatGroupInfo(g, messageStore))
			}
			json.NewEncoder(w).Encode(out)
			return

		case "info":
			jidStr := r.URL.Query().Get("jid")
			if jidStr == "" {
				http.Error(w, "jid is required", http.StatusBadRequest)
				return
			}
			jid, err := types.ParseJID(jidStr)
			if err != nil {
				http.Error(w, fmt.Sprintf("invalid jid: %v", err), http.StatusBadRequest)
				return
			}
			info, err := client.GetGroupInfo(context.Background(), jid)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
				return
			}
			json.NewEncoder(w).Encode(formatGroupInfo(info, messageStore))
			return

		case "requests":
			jidStr := r.URL.Query().Get("jid")
			if jidStr == "" {
				http.Error(w, "jid is required", http.StatusBadRequest)
				return
			}
			jid, err := types.ParseJID(jidStr)
			if err != nil {
				http.Error(w, fmt.Sprintf("invalid jid: %v", err), http.StatusBadRequest)
				return
			}
			requests, err := client.GetGroupRequestParticipants(context.Background(), jid)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
				return
			}
			out := make([]groupRequestOut, 0, len(requests))
			for _, p := range requests {
				out = append(out, groupRequestOut{
					JID:          p.JID.String(),
					ResolvedName: messageStore.GetContactName(p.JID.String()),
					RequestedAt:  p.RequestedAt,
				})
			}
			json.NewEncoder(w).Encode(out)
			return

		default:
			http.Error(w, fmt.Sprintf("unknown mode %q (expected list/info/requests)", mode), http.StatusBadRequest)
			return
		}
	})

	// Start the server
	serverAddr := fmt.Sprintf(":%d", port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, nil); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

func main() {
	// Set up logger
	logger := waLog.Stdout("Client", "INFO", true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Set device name shown in WhatsApp linked devices list
	hostname := os.Getenv("WHATSAPP_BRIDGE_HOSTNAME")
	if hostname == "" {
		hostname, _ = os.Hostname()
	}
	deviceName := "WhatsApp MCP Bridge (" + hostname + ")"
	store.SetOSInfo(deviceName, [3]uint32{2, 2413, 9})

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")

		case *events.PushName:
			// Sender's push name changed — update our contacts table.
			if err := storeContactWithAlt(client, messageStore, v.JID, "", "", v.NewPushName, ""); err != nil {
				logger.Warnf("Failed to persist push name update: %v", err)
			}

		case *events.BusinessName:
			if err := storeContactWithAlt(client, messageStore, v.JID, "", "", "", v.NewBusinessName); err != nil {
				logger.Warnf("Failed to persist business name update: %v", err)
			}

		case *events.Contact:
			// App-state contact action (add/rename from another linked device).
			if v.Action != nil {
				if err := storeContactWithAlt(client, messageStore, v.JID, v.Action.GetFirstName(), v.Action.GetFullName(), "", ""); err != nil {
					logger.Warnf("Failed to persist contact update: %v", err)
				}
			}

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")
		}
	})

	// Create channel to track connection success
	connected := make(chan bool, 1)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		// No ID stored, this is a new client, need to pair with phone
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}

		// Print QR code for pairing with phone
		for evt := range qrChan {
			if evt.Event == "code" {
				fmt.Println("\nScan this QR code with your WhatsApp app:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			} else if evt.Event == "success" {
				connected <- true
				break
			}
		}

		// Wait for connection
		select {
		case <-connected:
			fmt.Println("\nSuccessfully connected and authenticated!")
		case <-time.After(3 * time.Minute):
			logger.Errorf("Timeout waiting for QR code scan")
			return
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		connected <- true
	}

	// Wait a moment for connection to stabilize
	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}

	fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")

	if client.Store.ID != nil {
		ownJIDStringVal = client.Store.ID.ToNonAD().String()
	}

	// Populate contacts table from whatsmeow's store
	syncContactsFromWhatsmeow(client, messageStore, logger)

	// Start REST API server
	startRESTServer(client, messageStore, 8080)

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

// storeContactWithAlt persists contact info under the given JID and, if an alternate
// JID exists (LID ↔ phone), mirrors the same record there. This makes lookups robust
// regardless of which identifier the caller has in hand — group messages typically
// surface the @lid form while the actual contact name lives under the phone JID.
func storeContactWithAlt(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, firstName, fullName, pushName, businessName string) error {
	if jid.IsEmpty() {
		return nil
	}
	primary := jid.ToNonAD()
	if err := messageStore.StoreContact(primary.String(), firstName, fullName, pushName, businessName); err != nil {
		return err
	}
	if client == nil || client.Store == nil {
		return nil
	}
	altJID, err := client.Store.GetAltJID(context.Background(), primary)
	if err != nil || altJID.IsEmpty() {
		return nil
	}
	return messageStore.StoreContact(altJID.String(), firstName, fullName, pushName, businessName)
}

// syncContactsFromWhatsmeow pulls every contact from whatsmeow's store into our contacts table,
// mirroring each entry under its LID alternate (and vice versa) so lookups by either JID succeed.
func syncContactsFromWhatsmeow(client *whatsmeow.Client, messageStore *MessageStore, logger waLog.Logger) {
	if client == nil || client.Store == nil || client.Store.Contacts == nil {
		return
	}
	contacts, err := client.Store.Contacts.GetAllContacts(context.Background())
	if err != nil {
		logger.Warnf("Failed to load contacts from whatsmeow store: %v", err)
		return
	}
	count := 0
	for jid, info := range contacts {
		if err := storeContactWithAlt(client, messageStore, jid, info.FirstName, info.FullName, info.PushName, info.BusinessName); err != nil {
			logger.Warnf("Failed to store contact %s: %v", jid.String(), err)
			continue
		}
		count++
	}
	logger.Infof("Synced %d contacts from whatsmeow store", count)
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that
		logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		// Prefer our contacts table (kept fresh by syncContactsFromWhatsmeow + push-name updates)
		if resolved := messageStore.GetContactName(chatJID); resolved != "" {
			name = resolved
		} else if contact, err := client.Store.Contacts.GetContact(context.Background(), jid); err == nil {
			// Fall back to whatsmeow's in-memory contact store
			if contact.FullName != "" {
				name = contact.FullName
			} else if contact.FirstName != "" {
				name = contact.FirstName
			} else if contact.PushName != "" {
				name = contact.PushName
			} else if contact.BusinessName != "" {
				name = contact.BusinessName
			}
			// Opportunistically persist (mirrors onto the LID alt JID too)
			_ = storeContactWithAlt(client, messageStore, jid, contact.FirstName, contact.FullName, contact.PushName, contact.BusinessName)
		}

		if name == "" {
			if sender != "" {
				name = sender
			} else {
				name = jid.User
			}
		}

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Extract text content
				var content string
				if msg.Message.Message != nil {
					if conv := msg.Message.Message.GetConversation(); conv != "" {
						content = conv
					} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
						content = ext.GetText()
					}
				}

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
				}

				// Log the message content for debugging
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender (user portion) and full sender JID (with server suffix).
				var sender, senderJID string
				isFromMe := false
				if msg.Message.Key != nil {
					if msg.Message.Key.FromMe != nil {
						isFromMe = *msg.Message.Key.FromMe
					}
					if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
						senderJID = *msg.Message.Key.Participant
						if pjid, perr := types.ParseJID(senderJID); perr == nil {
							sender = pjid.User
							senderJID = pjid.ToNonAD().String()
						} else {
							sender = senderJID
						}
					} else if isFromMe && client.Store.ID != nil {
						sender = client.Store.ID.User
						senderJID = client.Store.ID.ToNonAD().String()
					} else {
						sender = jid.User
						senderJID = jid.ToNonAD().String()
					}
				} else {
					sender = jid.User
					senderJID = jid.ToNonAD().String()
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					senderJID,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					// Log successful message storage
					if mediaType != "" {
						logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
					} else {
						logger.Infof("Stored message: [%s] %s -> %s: %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, content)
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// Request history sync from the server
func requestHistorySync(client *whatsmeow.Client) {
	if client == nil {
		fmt.Println("Client is not initialized. Cannot request history sync.")
		return
	}

	if !client.IsConnected() {
		fmt.Println("Client is not connected. Please ensure you are connected to WhatsApp first.")
		return
	}

	if client.Store.ID == nil {
		fmt.Println("Client is not logged in. Please scan the QR code first.")
		return
	}

	// Build and send a history sync request
	historyMsg := client.BuildHistorySyncRequest(nil, 100)
	if historyMsg == nil {
		fmt.Println("Failed to build history sync request.")
		return
	}

	_, err := client.SendMessage(context.Background(), types.JID{
		Server: "s.whatsapp.net",
		User:   "status",
	}, historyMsg)

	if err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
	} else {
		fmt.Println("History sync requested. Waiting for server response...")
	}
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
