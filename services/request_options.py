"""Strict parsing of original Host text; no identity or permission is granted."""
from dataclasses import dataclass
from pathlib import Path
import re
import sys

if __package__:
    from ..runtime.catalogue_text import validate_catalogue_text
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
    from catalogue_text import validate_catalogue_text
    sys.path.pop(0)

# Only slash commands belong to Command; Tool supplies structured fields.
COVER_COMMAND_PATTERN = r'\A/翻唱(?:[ \t]+[^\r\n]*)?[ \t]*\Z'
_FLAG_RE = re.compile(r'(?<!\S)(?:--\S+|-[A-Za-z]\S*)(?=\s|$)')
_USAGE = '请用 /翻唱 准确歌名 - 艺人名'


@dataclass(frozen=True)
class RequestOptions:
    query: str
    entry_kind: str
    render_mode: str
    delivery_mode: str
    instrumental: bool
    album: str = ''
    source_id: str = ''
    model_selector: str = ''
    auto_reply: bool = True
    legacy_auto_reply_flag: bool = False
    title: str = ''
    artist: str = ''


MAX_DIRECT_TEXT_COMPONENTS = 32
MAX_DIRECT_TEXT_LENGTH = 2048


def validate_direct_host_text(message: dict, text: str) -> None:
    """Accept only serialized direct text, never quote/media-derived Host text.

    TextComponent is {type: text, data: str}; Host joins with one space.
    Never flatten nested data or normalize the original text.
    """
    raw = message.get('raw_message')
    if (message.get('is_notify') is not False or message.get('reply_to') is not None
            or message.get('is_at', False) is not False
            or message.get('is_mentioned', False) is not False
            or not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DIRECT_TEXT_COMPONENTS):
        raise ValueError('只接受未引用、未通知的直接文字消息')
    parts = []
    length = len(raw) - 1
    for component in raw:
        if (not isinstance(component, dict) or set(component) != {'type', 'data'}
                or component['type'] != 'text' or not isinstance(component['data'], str)):
            raise ValueError('只接受宿主直接 text 组件')
        length += len(component['data'])
        if length > MAX_DIRECT_TEXT_LENGTH:
            raise ValueError('直接文字消息过长')
        parts.append(component['data'])
    joined = ' '.join(parts)
    if not joined.strip() or joined != text or joined != message.get('processed_plain_text'):
        raise ValueError('直接文字与宿主处理文本不一致')


def parse_request_text(raw_text: str) -> RequestOptions:
    """Reject contradictory/repeated/unknown flags; defaults are independent.

    Slash command defaults: full/instrumental/file.
    Explicit trusted requests authorize delivery without a flag. A single final
    --auto-reply remains a redundant legacy spelling. --album values may contain
    spaces; --source-id and -v may not. Callers must authenticate the
    Host message first, and never pass Tool arguments or matched_groups here.
    """
    if (not isinstance(raw_text, str) or not raw_text.strip()
            or len(raw_text) > 2048
            or any(ord(char) < 32 and char != '\t' for char in raw_text)
            or '\x7f' in raw_text):
        raise ValueError('缺少或无效的宿主原始请求文本')
    text = raw_text.rstrip(' \t')
    command = re.fullmatch(r'/翻唱(?:[ \t]+(?P<body>.*))?', text)
    if command is None:
        raise ValueError(_USAGE)
    entry_kind = 'command'
    body = command.group('body') or ''
    # Options only start after both structured catalogue fields. A leading
    # hyphen in a title or the first artist token remains literal label data.
    matches = [match for match in _FLAG_RE.finditer(body)
               if ' - ' in body[:match.start()]
               and body[:match.start()].rsplit(' - ', 1)[1].strip()]
    query = body[:matches[0].start()].strip() if matches else body.strip()
    option_text = body[matches[0].start():] if matches else ''
    if '《' in query or '》' in query or ' - ' not in query:
        raise ValueError(_USAGE + '；必须明确歌名和艺人，选项放在曲目之后')
    title_part, artist_part = (part.strip() for part in query.rsplit(' - ', 1))
    if not title_part or not artist_part:
        raise ValueError(_USAGE + '；必须明确歌名和艺人，选项放在曲目之后')

    values = {
        'render_mode': 'full',
        'delivery_mode': 'file',
        'instrumental': True,
        'album': '', 'source_id': '', 'model_selector': '', 'auto_reply': True,
    }
    flags = {
        '--full': ('render_mode', 'full'),
        '--excerpt': ('render_mode', 'excerpt'),
        '--with-instrumental': ('instrumental', True),
        '--without-instrumental': ('instrumental', False),
        '--file': ('delivery_mode', 'file'),
        '--voice': ('delivery_mode', 'voice'),
        '--auto-reply': ('auto_reply', True),
    }
    selectors = {'--album': 'album', '--source-id': 'source_id', '-v': 'model_selector'}
    matches = list(_FLAG_RE.finditer(option_text))
    if option_text and (not matches or matches[0].start() != 0):
        raise ValueError('曲目之后只能添加明确选项')
    seen = set()
    for index, match in enumerate(matches):
        flag = match.group()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(option_text)
        value = option_text[match.end():end].strip()
        if flag in flags:
            field, selected = flags[flag]
            if value:
                raise ValueError(f'{flag} 不接受附加值')
            if flag == '--auto-reply' and index != len(matches) - 1:
                raise ValueError('--auto-reply 只能出现一次并放在原消息最后')
        elif flag in selectors:
            field, selected = selectors[flag], value
            if not value:
                raise ValueError(f'{flag} 缺少值')
            if flag == '--source-id' and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('--source-id 必须是明确的候选曲目 ID')
            if flag == '-v' and re.search(r'\s', value):
                raise ValueError('-v 只能指定一个管理员固定音色')
        else:
            raise ValueError('未知选项；支持 --full/--excerpt、--with-instrumental/--without-instrumental、--file/--voice、--album、--source-id、-v、--auto-reply')
        if field in seen:
            raise ValueError('矛盾或重复选项；每个选项维度只能指定一次')
        seen.add(field)
        values[field] = selected
        if flag == '--auto-reply':
            values['legacy_auto_reply_flag'] = True
    title_part = validate_catalogue_text(title_part)
    artist_part = validate_catalogue_text(artist_part)
    if values['album']:
        values['album'] = validate_catalogue_text(values['album'])
    return RequestOptions(query=query, entry_kind=entry_kind,
                          title=title_part, artist=artist_part, **values)
