#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MLBB TOOLS — CHAT TEMAN (PID 11167)  [TELEGRAM BOT EDITION]
============================================================
Kontrol penuh via Telegram. Stop button tersedia untuk hentikan blast.
Setelah stop, otomatis kembali ke menu utama.
"""

import os, sys, time, socket, struct, zlib, random, re, json, datetime, threading
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from collections import defaultdict

import urllib3
import zstandard as zstd
from Crypto.Cipher import AES
import telebot
from telebot import types

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ======================================================================
# BOT CONFIG
# ======================================================================
BOT_TOKEN = "8824575468:AAGWZRWE41AVnl7n7tbyzp3dah1xbm1cQ-0"
ADMIN_IDS = {7601958159}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


# ======================================================================
# GAME CONFIG
# ======================================================================
AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV  = b'\x00' * 16

LOGIN_SERVER_HOST = 'login.ml.youngjoygame.com'
LOGIN_SERVER_PORT = 30021

TIMEOUT         = 1.2
LOGIN_TIMEOUT   = 3

CHAT_SEND_PID    = 11167
CHAT_SEND_RESP   = 11168
CHAT_RECV_PID    = 11169

CHAT_WORKERS   = 500
SCAN_WORKERS   = 120
LOOKUP_WORKERS = 40

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ======================================================================
# SDP PROTOCOL
# ======================================================================
class SdpDataType(Enum):
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT            = 2
    DOUBLE           = 3
    STRING           = 4
    LIST             = 5
    DICT             = 6
    STRUCT_BEGIN     = 7
    STRUCT_END       = 8


class SdpException(Exception):
    pass


class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data:
            return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value):
        r = bytearray()
        while value >= 0x80:
            r.append((value & 0x7F) | 0x80)
            value >>= 7
        r.append(value & 0x7F)
        return bytes(r)

    def _read_number(self):
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

    def _pack_header(self, tag, data_type):
        if tag < 15:
            self.data += bytes([(data_type.value << 4) | tag])
        else:
            self.data += bytes([(data_type.value << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag, value):
        if isinstance(value, bool):
            self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpDataType.DOUBLE)
            p = struct.pack("<d", value)
            self.data += self._write_number(len(p))
            self.data += p
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpDataType.STRING)
            enc = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(enc))
            self.data += enc
        elif isinstance(value, list):
            self._pack_header(tag, SdpDataType.LIST)
            self.data += self._write_number(len(value))
            for i in value:
                self._pack(0, i)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()):
                    self._pack(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise SdpException(f"Unsupported type: {type(value)}")

    def _unpack(self):
        try:
            if self.offset >= len(self.data):
                return 0, None
            h = self.data[self.offset]
            tag = h & 0xF
            dt = SdpDataType(h >> 4)
            self.offset += 1
            if tag == 15:
                tag = self._read_number()
            if dt == SdpDataType.INTEGER_POSITIVE:
                return tag, self._read_number()
            elif dt == SdpDataType.INTEGER_NEGATIVE:
                return tag, -self._read_number()
            elif dt == SdpDataType.FLOAT:
                v = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", v)[0]
            elif dt == SdpDataType.DOUBLE:
                v = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", v)[0]
            elif dt == SdpDataType.STRING:
                length = self._read_number()
                try:
                    v = self.data[self.offset:self.offset + length].decode('utf-8')
                except UnicodeDecodeError:
                    v = self.data[self.offset:self.offset + length]
                self.offset += length
                return tag, v
            elif dt == SdpDataType.LIST:
                length = self._read_number()
                v = []
                for _ in range(length):
                    _, it = self._unpack()
                    v.append(it)
                return tag, v
            elif dt == SdpDataType.DICT:
                length = self._read_number()
                v = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, val = self._unpack()
                    v[k] = val
                return tag, v
            elif dt == SdpDataType.STRUCT_BEGIN:
                sd = {}
                while True:
                    st, sv = self._unpack()
                    if isinstance(sv, SdpDataType) and sv == SdpDataType.STRUCT_END:
                        break
                    sd[st] = sv
                return tag, SdpStruct(sd)
            elif dt == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else:
                raise SdpException("Unknown")
        except Exception as e:
            raise SdpException(f"unpack err: {e}")

    def copy(self):
        return SdpStruct(super().copy())

    def update(self, other):
        super().update(other)
        self._pack_to_binary()


# ======================================================================
# BASE CONNECTION
# ======================================================================
class BaseConnection:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sequence = 1
        self.socket = None
        self.queue_data = b''
        self.last_header_size = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.cleanup()

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(TIMEOUT)

    def cleanup(self):
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.sequence = 1
            self.socket = None

    def send_data(self, id, sdp):
        packet = SdpStruct({0: id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, 'big') + buf
        self.socket.sendall(buf)
        self.sequence += 1

    def recv_data(self):
        try:
            while len(self.queue_data) < 4:
                data = self.socket.recv(8192)
                if not data:
                    return None, None
                self.queue_data += data
            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 0xFFFFFF
            ct = flags >> 24
            self.last_header_size = size
            while len(self.queue_data) < size:
                data = self.socket.recv(8192)
                if not data:
                    return None, None
                self.queue_data += data
            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]
            if ct == 1:
                data = zlib.decompress(data)
            elif ct == 16:
                data = zstd.decompress(data)
            elif ct in (2, 3, 18):
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                dec = cipher.decrypt(data[:-1] if len(data) % 16 != 0 else data)
                data = dec.rstrip(b'\x00')
                if ct == 3:
                    data = zlib.decompress(data)
                elif ct == 18:
                    data = zstd.decompress(data)
            result = SdpStruct(data)
            pid = result[0]
            if pid is None:
                return None, None
            res = result.get(6)
            if not res or not isinstance(res, bytes):
                res = result.get(5)
                if not res or not isinstance(res, bytes):
                    return pid, None
            return pid, SdpStruct(res)
        except socket.timeout:
            return -1, None
        except Exception:
            return None, None


# ======================================================================
# GAME CONNECTION
# ======================================================================
class GameConnection(BaseConnection):
    def __init__(self, device_id, device_model=None):
        super().__init__(LOGIN_SERVER_HOST, LOGIN_SERVER_PORT)
        self.device_id = device_id
        self.device_model = device_model or "Xiaomi:Redmi Note 12"

        parts = self.device_id.split('_')
        if len(parts) >= 2:
            di = parts[1]
            if len(parts) >= 3 and len(di) < 32:
                di = di + "_" + parts[2]
            if len(di) >= 32:
                self.imei_md5 = di[:32]
                if len(di) >= 48:
                    self.android_id = di[32:48]
                    self.advertising_id = di[48:] if len(di) > 48 else ""
                else:
                    self.android_id = ""
                    self.advertising_id = ""
            else:
                self.imei_md5 = di
                self.android_id = ""
                self.advertising_id = ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""

        self.channel = 'and_usa'
        self.client_version = '2.2.16.1232.1'
        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_server_host = ''
        self.game_server_port = 0
        self.creation_ts = 0

    def _login_packet(self):
        return SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
            2: self.client_version,
            3: self.channel,
            4: 'en'
        })

    def login_to_login_server(self):
        if self.host != LOGIN_SERVER_HOST or self.port != LOGIN_SERVER_PORT:
            self.cleanup()
            self.host = LOGIN_SERVER_HOST
            self.port = LOGIN_SERVER_PORT
            self.connect()
        self.send_data(1, self._login_packet())
        pid, res = self.recv_data()
        if pid == 2 and res:
            self.account_id = res.get(0)
            self.session_key = res[1]
            zr = res.get(2)
            if isinstance(zr, list) and zr:
                self.zone_id = zr[0] if not isinstance(zr[0], dict) else zr[0].get(0, 0)
            elif isinstance(zr, dict):
                self.zone_id = zr.get(0, 0)
            else:
                self.zone_id = zr
            self.creation_ts = res.get(19, 0)
            return True
        return False

    def get_game_server(self):
        self.send_data(5, SdpStruct({
            0: self.account_id, 1: self.session_key,
            2: self.client_version, 5: self.zone_id, 6: self.channel
        }))
        pid, res = self.recv_data()
        if pid == 6 and res:
            gs = res[1]
            self.game_server_host, self.game_server_port = gs.split(':')
            self.game_server_port = int(self.game_server_port)
            return True
        return False

    def connect_to_game_server(self):
        self.cleanup()
        self.host = self.game_server_host
        self.port = self.game_server_port
        self.connect()

        self.send_data(10001, SdpStruct({
            0: self.account_id,
            1: self.session_key,
            2: self.zone_id,
            4: self.client_version,
            13: self.channel,
            15: self.device_id
        }))
        self.send_data(10101, SdpStruct({0: 0, 2: 2}))

        while True:
            pid, res = self.recv_data()
            if pid is None:
                return False
            elif pid == 10002:
                return True
            elif pid == -1:
                return False
            elif pid == 20001:
                return True

    def lookup_player(self, search_value, search_type="id", server_filter=None):
        if search_type == "id":
            try:
                ld = SdpStruct({1: int(search_value)})
            except ValueError:
                ld = SdpStruct({1: search_value})
        else:
            ld = SdpStruct({0: str(search_value).strip()})
        self.send_data(11153, ld)
        cnt = 0
        while True:
            pid, res = self.recv_data()
            if pid is None:
                return None
            elif pid == -1:
                return None
            elif pid == 11154:
                return res
            elif pid == 20001:
                cnt += 1
                if self.last_header_size < 100 and cnt >= 2:
                    return None

    def get_profile_info(self, role_id, zone_id):
        self.send_data(10143, SdpStruct({0: int(role_id), 1: int(zone_id)}))
        to = 0
        while to < 3:
            pid, res = self.recv_data()
            if pid is None:
                break
            elif pid == -1:
                to += 1
            elif pid == 10144:
                return res
            elif pid == 20001:
                continue
        return None

    def send_chat(self, target_user_id, target_zone, message):
        self.send_data(CHAT_SEND_PID, SdpStruct({
            0: int(target_user_id),
            1: int(target_zone),
            2: "",
            3: str(message),
            6: "",
            10: "",
        }))
        to = 0
        while to < 5:
            pid, res = self.recv_data()
            if pid is None:
                return False
            elif pid == -1:
                to += 1
                continue
            elif pid == CHAT_SEND_RESP:
                return True
            elif pid == 20001:
                continue
        return False

    def __enter__(self):
        super().__enter__()
        if not self.login_to_login_server():
            raise ConnectionError("LOGIN_FAILED")
        if not self.get_game_server():
            raise ConnectionError("SERVER_SELECTION_FAILED")
        return self


# ======================================================================
# EXTRACT DATA
# ======================================================================
def extract_target_info(result):
    if not result or not result.get(0) or len(result[0]) == 0:
        return None
    try:
        pd = result[0][0]
        return {
            "player_id": pd.get(0),
            "zone_id":   pd.get(1),
            "nickname":  pd.get(2, "Unknown"),
            "level":     pd.get(3, 0),
        }
    except Exception:
        return None


def extract_search_results(result):
    players = []
    if not result:
        return players

    def _add_from_dict(item):
        if not isinstance(item, dict):
            return
        pid = item.get(0)
        zone = item.get(1)
        if pid is None:
            return
        players.append({
            "player_id": pid,
            "zone_id":   zone,
            "nickname":  item.get(2, "Unknown"),
            "level":     item.get(3, 0),
        })

    try:
        raw = result.get(0)
        if isinstance(raw, list):
            for item in raw:
                _add_from_dict(item)
        elif isinstance(raw, dict):
            _add_from_dict(raw)
        else:
            for k, v in result.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    for item in v:
                        _add_from_dict(item)
                    if players:
                        break
    except Exception:
        pass
    return players


# ======================================================================
# DEVICE POOL
# ======================================================================
DEVICE_ID_RE = re.compile(r"(?i)(?:and_|ios_)[A-Za-z0-9_\-]+")


def extract_device_ids_from_text(text: str):
    found = DEVICE_ID_RE.findall(text or "")
    out = []
    for d in found:
        d = d.rstrip(".,;:!?\"'")
        if d:
            out.append(d)
    return out


# ======================================================================
# WORKER FUNCTIONS
# ======================================================================
def chat_one_device(device_id, target_user_id, target_zone, message, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return ("stop", device_id, "stopped")
    try:
        with GameConnection(device_id=device_id) as conn:
            if stop_event is not None and stop_event.is_set():
                return ("stop", device_id, "stopped")
            if not conn.connect_to_game_server():
                return ("fail", device_id, "game_connect_failed")
            if conn.send_chat(target_user_id, target_zone, message):
                return ("ok", device_id)
            else:
                return ("fail", device_id, "chat_rejected")
    except ConnectionError as e:
        return ("fail", device_id, f"conn:{e}")
    except socket.timeout:
        return ("fail", device_id, "timeout")
    except Exception as e:
        return ("fail", device_id, f"{type(e).__name__}: {e}")


def _probe_lookup(device_id, nickname):
    try:
        with GameConnection(device_id=device_id) as conn:
            if not conn.connect_to_game_server():
                return None
            result = conn.lookup_player(nickname, "nickname", server_filter=None)
            if not result:
                return None
            players = extract_search_results(result)
            return players if players else None
    except Exception:
        return None


def _parallel_lookup(device_pool, nickname, max_devices=20, workers=LOOKUP_WORKERS):
    probe = device_pool[:max_devices]
    if not probe:
        return None
    with ThreadPoolExecutor(max_workers=min(workers, len(probe))) as ex:
        futs = [ex.submit(_probe_lookup, dev, nickname) for dev in probe]
        result = None
        try:
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    result = r
                    break
        finally:
            for f in futs:
                f.cancel()
        return result


# ======================================================================
# BOT STATE
# ======================================================================
CHAT_STATE = defaultdict(lambda: {
    "devs": [],
    "path": None,
    "pending": None,
    "busy": False,
    "stop_event": None,
    "blast_msg_id": None,
})
STATE_LOCK = threading.Lock()


def _state(chat_id):
    return CHAT_STATE[chat_id]


def _pool_info_text(chat_id):
    st = _state(chat_id)
    if not st["devs"]:
        return "📦 <b>Device pool:</b> <i>kosong</i>"
    return (
        f"📦 <b>Device pool:</b> <b>{len(st['devs'])}</b> device\n"
        f"📄 File: <code>{st['path'] or '?'}</code>"
    )


def _main_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚀 BLAST CHAT", callback_data="menu_blast"),
        types.InlineKeyboardButton("📦 POOL INFO", callback_data="menu_pool"),
    )
    kb.add(
        types.InlineKeyboardButton("🧪 TEST DEVICE", callback_data="menu_test"),
        types.InlineKeyboardButton("🗑 RESET POOL", callback_data="menu_reset"),
    )
    kb.add(
        types.InlineKeyboardButton("📖 CARA PAKAI", callback_data="menu_help"),
    )
    return kb


def _main_menu_text(chat_id):
    st = _state(chat_id)
    status = "🔴 BLAST SEDANG JALAN" if st.get("busy") else "🟢 IDLE"
    return (
        f"🤖 <b>MLBB CHAT BOT — CONTROL PANEL</b>\n"
        f"Status: <b>{status}</b>\n\n"
        f"{_pool_info_text(chat_id)}\n\n"
        f"<b>Menu:</b>\n"
        f"• Kirim file <code>.txt</code> untuk load device pool\n"
        f"• Tekan <b>BLAST CHAT</b> untuk mulai\n"
    )


def _show_main_menu(chat_id, edit_msg_id=None):
    text = _main_menu_text(chat_id)
    kb = _main_menu_kb()
    try:
        if edit_msg_id:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id,
                                  reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            pass


# ======================================================================
# BOT HANDLERS — START / HELP
# ======================================================================
@bot.message_handler(commands=['start', 'menu'])
def cmd_start(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ Not authorized")
        return
    _show_main_menu(msg.chat.id)


@bot.message_handler(commands=['help'])
def cmd_help(msg):
    if not is_admin(msg.from_user.id):
        return
    text = (
        "📖 <b>CARA PAKAI BOT</b>\n\n"
        "<b>1. Upload device pool</b>\n"
        "Kirim file <code>.txt</code> berisi Device ID (format <code>and_...</code> atau <code>ios_...</code>) ke chat ini.\n\n"
        "<b>2. Blast chat</b>\n"
        "Klik <b>🚀 BLAST CHAT</b> atau ketik:\n"
        "<code>/chat &lt;nickname&gt; | &lt;zone&gt; | &lt;pesan&gt;</code>\n\n"
        "Contoh:\n"
        "<code>/chat Theo bhipuri | 8998 | halo bre</code>\n\n"
        "<b>3. Stop blast</b>\n"
        "Klik tombol <b>⛔ STOP</b> saat blast berjalan.\n\n"
        "<b>Command lain:</b>\n"
        "/menu — tampilkan menu utama\n"
        "/pool — lihat device pool\n"
        "/test — test 5 device\n"
        "/reset — reset device pool\n"
        "/cancel — batal blast pending"
    )
    bot.send_message(msg.chat.id, text)


@bot.message_handler(commands=['pool'])
def cmd_pool(msg):
    if not is_admin(msg.from_user.id):
        return
    st = _state(msg.chat.id)
    text = _pool_info_text(msg.chat.id)
    if st["devs"]:
        preview = "\n".join(f"  {i}. <code>{d[:50]}{'...' if len(d) > 50 else ''}</code>"
                            for i, d in enumerate(st["devs"][:5], 1))
        text += f"\n\n<b>Preview 5 pertama:</b>\n{preview}"
    bot.send_message(msg.chat.id, text)


@bot.message_handler(commands=['reset'])
def cmd_reset(msg):
    if not is_admin(msg.from_user.id):
        return
    with STATE_LOCK:
        _state(msg.chat.id)["devs"] = []
        _state(msg.chat.id)["path"] = None
        _state(msg.chat.id)["pending"] = None
    bot.reply_to(msg, "✅ Device pool di-reset.")
    _show_main_menu(msg.chat.id)


@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    if not is_admin(msg.from_user.id):
        return
    _state(msg.chat.id)["pending"] = None
    bot.reply_to(msg, "✅ Pending blast dibatalkan.")
    _show_main_menu(msg.chat.id)


# ======================================================================
# UPLOAD FILE
# ======================================================================
@bot.message_handler(content_types=['document'])
def handle_document(msg):
    if not is_admin(msg.from_user.id):
        return
    doc = msg.document
    if not doc.file_name.lower().endswith(".txt"):
        bot.reply_to(msg, "❌ Kirim file <code>.txt</code> saja.")
        return

    m = bot.reply_to(msg, f"📥 Download {doc.file_name}...")

    def run():
        try:
            file_info = bot.get_file(doc.file_id)
            data = bot.download_file(file_info.file_path)
            text = data.decode("utf-8", errors="ignore")
            devs = extract_device_ids_from_text(text)
            if not devs:
                bot.edit_message_text(
                    "❌ Tidak ada Device ID valid di file.",
                    chat_id=msg.chat.id, message_id=m.message_id
                )
                return
            with STATE_LOCK:
                _state(msg.chat.id)["devs"] = devs
                _state(msg.chat.id)["path"] = doc.file_name
            bot.edit_message_text(
                f"✅ <b>Pool loaded!</b>\n"
                f"Total: <b>{len(devs)}</b> device\n"
                f"File: <code>{doc.file_name}</code>",
                chat_id=msg.chat.id, message_id=m.message_id
            )
            _show_main_menu(msg.chat.id)
        except Exception as e:
            try:
                bot.edit_message_text(
                    f"❌ Gagal load: <code>{e}</code>",
                    chat_id=msg.chat.id, message_id=m.message_id
                )
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


# ======================================================================
# MENU CALLBACK
# ======================================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("menu_"))
def menu_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    st = _state(chat_id)

    if call.data == "menu_pool":
        bot.answer_callback_query(call.id)
        _show_main_menu(chat_id, msg_id)
        return

    if call.data == "menu_reset":
        with STATE_LOCK:
            st["devs"] = []
            st["path"] = None
            st["pending"] = None
        bot.answer_callback_query(call.id, "✅ Pool di-reset")
        _show_main_menu(chat_id, msg_id)
        return

    if call.data == "menu_help":
        bot.answer_callback_query(call.id)
        try:
            bot.send_message(chat_id,
                "📖 Kirim file .txt untuk load pool.\n"
                "Lalu <code>/chat nick | zone | pesan</code>\n"
                "atau klik 🚀 BLAST CHAT."
            )
        except Exception:
            pass
        return

    if call.data == "menu_test":
        bot.answer_callback_query(call.id)
        if not st["devs"]:
            bot.send_message(chat_id, "❌ Device pool kosong. Kirim file .txt dulu.")
            return
        devs = st["devs"][:5]
        m = bot.send_message(chat_id, f"🧪 Test {len(devs)} device pertama...")

        def run():
            def test_dev(d):
                try:
                    with GameConnection(device_id=d) as conn:
                        ok = conn.connect_to_game_server()
                        return d, ok, None
                except Exception as e:
                    return d, False, str(e)

            lines = []
            with ThreadPoolExecutor(max_workers=5) as ex:
                for d, ok, err in ex.map(test_dev, devs):
                    tag = "✅ HIDUP" if ok else "❌ MATI"
                    lines.append(f"{tag} <code>{d[:40]}...</code> {err or ''}")
            try:
                bot.edit_message_text(
                    "🧪 <b>TEST RESULT</b>\n\n" + "\n".join(lines),
                    chat_id=chat_id, message_id=m.message_id
                )
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()
        return

    if call.data == "menu_blast":
        bot.answer_callback_query(call.id)
        if not st["devs"]:
            bot.send_message(chat_id, "❌ Device pool kosong. Kirim file .txt dulu.")
            return
        try:
            bot.edit_message_text(
                "📝 <b>Masukkan target</b>\n\n"
                "Format:\n"
                "<code>/chat &lt;nickname&gt; | &lt;zone&gt; | &lt;pesan&gt;</code>\n\n"
                "Contoh:\n"
                "<code>/chat Theo bhipuri | 8998 | halo bre</code>",
                chat_id=chat_id, message_id=msg_id
            )
        except Exception:
            bot.send_message(chat_id,
                "📝 Format: <code>/chat nick | zone | pesan</code>")
        return


# ======================================================================
# CHAT COMMAND
# ======================================================================
@bot.message_handler(commands=['chat'])
def cmd_chat(msg):
    if not is_admin(msg.from_user.id):
        return
    chat_id = msg.chat.id
    st = _state(chat_id)

    if st.get("busy"):
        bot.reply_to(msg, "⏳ Blast sedang jalan. Tunggu sampai selesai atau tekan STOP.")
        return

    if not st["devs"]:
        bot.reply_to(msg, "❌ Device pool kosong. Kirim file .txt dulu.")
        return

    raw = msg.text.partition(" ")[2].strip()
    if not raw:
        bot.reply_to(
            msg,
            "📝 Format:\n"
            "<code>/chat &lt;nickname&gt; | &lt;zone&gt; | &lt;pesan&gt;</code>\n\n"
            "Contoh:\n"
            "<code>/chat Theo bhipuri | 8998 | halo bre</code>"
        )
        return

    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        bot.reply_to(msg, "❌ Format salah. Harus: <code>nick | zone | pesan</code>")
        return

    nickname, zone_s, message = parts
    try:
        zone = int(zone_s)
    except ValueError:
        bot.reply_to(msg, "❌ Zone ID harus angka.")
        return

    m = bot.reply_to(msg, f"🔍 Lookup <b>{nickname}</b> (zone {zone})...")

    def run():
        results = _parallel_lookup(st["devs"], nickname, max_devices=20,
                                    workers=LOOKUP_WORKERS)
        if not results:
            try:
                bot.edit_message_text(
                    f"❌ Target '<b>{nickname}</b>' tidak ditemukan.\n"
                    f"Coba cek ejaan / zone, atau tambah device pool.",
                    chat_id=chat_id, message_id=m.message_id
                )
            except Exception:
                pass
            return

        matching = []
        for p in results:
            try:
                if int(p.get("zone_id") or 0) == zone:
                    matching.append(p)
            except (ValueError, TypeError):
                pass

        if not matching:
            if len(results) == 1:
                matching = results
            else:
                _show_picker(chat_id, m.message_id, nickname, zone, message, results)
                return

        if len(matching) > 1:
            _show_picker(chat_id, m.message_id, nickname, zone, message, matching)
            return

        chosen = matching[0]
        _show_confirm(chat_id, m.message_id, chosen, message)

    threading.Thread(target=run, daemon=True).start()


def _show_picker(chat_id, message_id, nickname, zone, message, results):
    st = _state(chat_id)
    st["pending"] = {"nick": nickname, "zone": zone, "msg": message,
                     "candidates": results}
    lines = [f"🔍 <b>Hasil lookup '{nickname}'</b>:"]
    kb = types.InlineKeyboardMarkup()
    for i, p in enumerate(results[:10], 1):
        lines.append(
            f"  <b>[{i}]</b> {p['nickname']} | ID <code>{p['player_id']}</code> "
            f"| Zone <code>{p['zone_id']}</code> | Lv {p.get('level',0)}"
        )
        kb.add(types.InlineKeyboardButton(
            f"{i}. {p['nickname'][:20]} (Z{p['zone_id']})",
            callback_data=f"pick:{i-1}"
        ))
    kb.add(types.InlineKeyboardButton("❌ Batal", callback_data="cancel"))
    try:
        bot.edit_message_text(
            "\n".join(lines) + "\n\n👉 Pilih target:",
            chat_id=chat_id, message_id=message_id, reply_markup=kb
        )
    except Exception:
        try:
            bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
        except Exception:
            pass


def _show_confirm(chat_id, message_id, chosen, message):
    st = _state(chat_id)
    st["pending"] = {"pid": chosen["player_id"], "zone": chosen["zone_id"],
                     "nick": chosen["nickname"], "msg": message}
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🚀 BLAST", callback_data="blast"),
        types.InlineKeyboardButton("❌ Batal", callback_data="cancel")
    )
    text = (
        f"🎯 <b>Target ditemukan</b>\n\n"
        f"Nickname: <b>{chosen['nickname']}</b>\n"
        f"User ID: <code>{chosen['player_id']}</code>\n"
        f"Zone: <code>{chosen['zone_id']}</code>\n"
        f"Level: {chosen.get('level', '?')}\n\n"
        f"💬 Pesan: <i>{message}</i>\n"
        f"📦 Pool: <b>{len(st['devs'])}</b> device\n\n"
        f"Lanjut blast?"
    )
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                              reply_markup=kb)
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data in ("blast", "cancel", "stop")
                            or c.data.startswith("pick:"))
def cb_handler(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    chat_id = call.message.chat.id
    st = _state(chat_id)

    # ---- STOP ----
    if call.data == "stop":
        ev = st.get("stop_event")
        if ev is not None:
            ev.set()
            bot.answer_callback_query(call.id, "⛔ Stop signal terkirim...")
        else:
            bot.answer_callback_query(call.id, "Tidak ada blast aktif")
        return

    # ---- CANCEL ----
    if call.data == "cancel":
        st["pending"] = None
        try:
            bot.edit_message_text("❌ Dibatalkan.", chat_id=chat_id,
                                  message_id=call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        _show_main_menu(chat_id)
        return

    # ---- PICK TARGET ----
    if call.data.startswith("pick:"):
        idx = int(call.data.split(":")[1])
        p = st.get("pending")
        if not p or idx >= len(p.get("candidates", [])):
            bot.answer_callback_query(call.id, "❌ Kadaluwarsa")
            return
        chosen = p["candidates"][idx]
        _show_confirm(chat_id, call.message.message_id, chosen, p["msg"])
        bot.answer_callback_query(call.id)
        return

    # ---- BLAST ----
    if call.data == "blast":
        p = st.get("pending")
        if not p or "pid" not in p:
            bot.answer_callback_query(call.id, "❌ Tidak ada target")
            return
        if st.get("busy"):
            bot.answer_callback_query(call.id, "⏳ Blast sedang jalan")
            return

        # reset flag baru
        st["busy"] = True
        st["stop_event"] = threading.Event()
        st["pending"] = None
        bot.answer_callback_query(call.id, "🚀 Mulai blast...")

        target_pid = p["pid"]
        target_zone = p["zone"]
        target_nick = p["nick"]
        message = p["msg"]
        pool = list(st["devs"])

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⛔ STOP", callback_data="stop"))

        m = bot.send_message(
            chat_id,
            f"🚀 <b>BLAST DIMULAI</b>\n"
            f"Target: <b>{target_nick}</b> (<code>{target_pid}</code>)\n"
            f"Pool: <b>{len(pool)}</b> device\n"
            f"Progress: 0/{len(pool)}",
            reply_markup=kb
        )
        st["blast_msg_id"] = m.message_id

        threading.Thread(
            target=run_blast, daemon=True,
            args=(chat_id, m.message_id, pool, target_pid, target_zone,
                  target_nick, message)
        ).start()


# ======================================================================
# BLAST RUNNER (dengan stop support)
# ======================================================================
def run_blast(chat_id, msg_id, pool, target_pid, target_zone, target_nick, message):
    st = _state(chat_id)
    stop_event = st.get("stop_event")
    total = len(pool)
    sukses = 0
    gagal = 0
    stopped = 0
    done = 0
    start = time.time()
    last_edit = 0
    last_done = 0
    was_stopped = False

    try:
        with ThreadPoolExecutor(max_workers=CHAT_WORKERS) as ex:
            futs = {ex.submit(chat_one_device, dev, target_pid, target_zone,
                              message, stop_event): dev
                    for dev in pool}

            pending = set(futs.keys())
            while pending:
                # tunggu futures yang selesai (polling 0.2s) supaya bisa cek stop
                done_set, pending = wait(pending, timeout=0.2,
                                         return_when=FIRST_COMPLETED)

                # kalau stop di-set → batalkan sisa
                if stop_event is not None and stop_event.is_set():
                    was_stopped = True
                    for f in pending:
                        f.cancel()
                    # tunggu sisa yang sudah mulai
                    for f in list(pending):
                        try:
                            if f.done():
                                done_set.add(f)
                        except Exception:
                            pass
                    pending = set()

                # proses futures yang selesai
                for fut in done_set:
                    done += 1
                    try:
                        res = fut.result()
                        status = res[0] if isinstance(res, tuple) else "fail"
                    except Exception:
                        status = "fail"
                    if status == "ok":
                        sukses += 1
                    elif status == "stop":
                        stopped += 1
                    else:
                        gagal += 1

                # update progress throttled
                now = time.time()
                if (now - last_edit >= 3 and done - last_done >= 20) or done == total:
                    elapsed = now - start
                    rate = done / elapsed if elapsed > 0 else 0
                    kb = types.InlineKeyboardMarkup()
                    kb.add(types.InlineKeyboardButton("⛔ STOP", callback_data="stop"))
                    try:
                        bot.edit_message_text(
                            f"🚀 <b>BLAST PROGRESS</b>\n"
                            f"Target: <b>{target_nick}</b> (<code>{target_pid}</code>)\n\n"
                            f"📊 <b>{done}</b>/{total}\n"
                            f"✅ OK: <b>{sukses}</b>\n"
                            f"❌ Fail: <b>{gagal}</b>\n"
                            f"⚡ Speed: <b>{rate:.1f}</b> dev/s",
                            chat_id=chat_id, message_id=msg_id,
                            reply_markup=kb
                        )
                    except Exception:
                        pass
                    last_edit = now
                    last_done = done

        elapsed = time.time() - start
        speed = done / elapsed if elapsed > 0 else 0

        if was_stopped:
            try:
                bot.edit_message_text(
                    f"⛔ <b>BLAST DIHENTIKAN</b>\n\n"
                    f"🎯 Target: <b>{target_nick}</b> (<code>{target_pid}</code>)\n"
                    f"💬 Pesan: <i>{message}</i>\n\n"
                    f"📊 Selesai: <b>{done}</b>/{total}\n"
                    f"✅ OK: <b>{sukses}</b>\n"
                    f"❌ Fail: <b>{gagal}</b>\n"
                    f"🛑 Stopped: <b>{stopped}</b>\n"
                    f"⏱️ Waktu: <b>{elapsed:.1f}s</b>",
                    chat_id=chat_id, message_id=msg_id
                )
            except Exception:
                pass
            try:
                bot.send_message(chat_id, "🔙 Kembali ke menu utama...")
            except Exception:
                pass
        else:
            try:
                bot.edit_message_text(
                    f"✅ <b>BLAST SELESAI</b>\n\n"
                    f"🎯 Target: <b>{target_nick}</b> (<code>{target_pid}</code>)\n"
                    f"💬 Pesan: <i>{message}</i>\n\n"
                    f"📊 Total: <b>{total}</b>\n"
                    f"✅ OK: <b>{sukses}</b>\n"
                    f"❌ Fail: <b>{gagal}</b>\n"
                    f"⏱️ Waktu: <b>{elapsed:.1f}s</b>\n"
                    f"⚡ Speed: <b>{speed:.1f}</b> dev/s",
                    chat_id=chat_id, message_id=msg_id
                )
            except Exception:
                pass

    except Exception as e:
        try:
            bot.send_message(chat_id, f"❌ Error blast: <code>{e}</code>")
        except Exception:
            pass
    finally:
        st["busy"] = False
        st["stop_event"] = None
        st["blast_msg_id"] = None
        # otomatis kembali ke menu utama
        _show_main_menu(chat_id)


# ======================================================================
# MAIN
# ======================================================================
def main():
    print("[*] MLBB Telegram Bot started.")
    print(f"[*] Admin IDs: {ADMIN_IDS}")
    print("[*] Polling... Ctrl+C to stop.")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Program dihentikan.")
