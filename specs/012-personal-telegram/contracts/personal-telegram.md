# Contracts: 012 Personal Telegram Account Tools

## Tool Definitions

### 1. `tg_user__inbox`
- **Description**: List unread dialogs and recent incoming messages across personal Telegram chats.
- **Parameters**:
  - `unread_only` (boolean, default=True): Only return dialogs with unread messages.
  - `limit` (integer, default=10, max=30): Maximum number of dialogs to return.
- **Output**: Formatted list of dialogs with chat name, unread count, and last message snippet.

### 2. `tg_user__search`
- **Description**: Search personal Telegram messages across chats or within a specific chat.
- **Parameters**:
  - `query` (string, required): Keyword or phrase to search for.
  - `chat` (string, optional): Target chat username, title, ID, or "me" for Saved Messages. If omitted, searches globally.
  - `limit` (integer, default=10, max=30): Maximum number of matching messages.
- **Output**: Formatted list of matching messages wrapped in untrusted data envelopes.

### 3. `tg_user__read`
- **Description**: Read recent message history from a specific personal chat, group, or channel.
- **Parameters**:
  - `chat` (string, required): Username (@username), chat title, phone number, chat ID, or "me" for Saved Messages.
  - `limit` (integer, default=15, max=50): Number of recent messages to retrieve.
- **Output**: Chronological transcript of messages with sender name, timestamp, and envelope-wrapped content.

### 4. `tg_user__send`
- **Description**: Send a message to a person, group, or Saved Messages from your personal Telegram account. Requires operator approval.
- **Parameters**:
  - `recipient` (string, required): Username (@user), phone number (+...), chat title, or "me" for Saved Messages.
  - `message` (string, required): The exact text message to send.
- **Output**: Confirmation with message ID and recipient name.
- **Policy**: Requires approval button on the assistant Telegram bot.

### 5. `tg_user__save`
- **Description**: Save a note, snippet, or reminder to your personal Telegram "Saved Messages" chat (`me`).
- **Parameters**:
  - `text` (string, required): Content to store in Saved Messages.
- **Output**: Confirmation that message was saved.
