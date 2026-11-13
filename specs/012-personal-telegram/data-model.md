# Data Model: 012 Personal Telegram Account Access

## Configuration: `TelegramUserSection`
```python
class TelegramUserSection(_Strict):
    enabled: bool = False
    api_id_secret: str = Field(default="telegram_api_id", pattern=r"^[a-z0-9_]{0,64}$")
    api_hash_secret: str = Field(default="telegram_api_hash", pattern=r"^[a-z0-9_]{0,64}$")
    session_path: str = "~/.config/assistant/telegram_user.session"
    max_messages: int = Field(default=15, ge=1, le=50)
    timeout_s: float = Field(default=20.0, gt=0, le=120)
```

## Internal Data Structures

### `TelegramDialog`
```python
@dataclass
class TelegramDialog:
    id: int
    title: str
    is_user: bool
    is_group: bool
    is_channel: bool
    unread_count: int
    last_message: str
    last_date: str
    username: str = ""
```

### `TelegramMessage`
```python
@dataclass
class TelegramMessage:
    id: int
    chat_id: int
    chat_title: str
    sender_id: int
    sender_name: str
    date: str
    text: str
    is_outgoing: bool
    media_type: str = ""  # "photo", "document", "voice", ""
```

### `TelegramCredentials`
```python
@dataclass
class TelegramCredentials:
    api_id: int
    api_hash: str
    session_path: str
```
