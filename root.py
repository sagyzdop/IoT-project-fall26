# root.py - root (ID 0). Set MODE below to "dv" or "flood". Single file; needs keys.py on the board.
import struct
import time
import _thread
import urandom
import network
import espnow
import cryptolib
from binascii import crc32
from keys import keychain

# ---- config ----
MODE = "dv"              # "dv" or "flood" - must be the same on root and node

CHANNEL = 1
VERSION = 1
KEY_ID = 1
RUN_ID = 1               # +1 per run, must be identical on all boards

ADV_INTERVAL_MS = 5000   # + 0-10% jitter
DATA_INTERVAL_MS = 10000  # + 0-10% jitter
NEIGHBOR_TIMEOUT_MS = 15000
RSS_THRESHOLD = -85      # dBm
RSS_ALPHA = 0.3          # EMA smoothing of RSSI
RSS_MARGIN = 5           # dB, same-hop parent switch margin
TTL_INIT = 10
DUP_LIFETIME_MS = 60000
DUP_MAX = 200
FLOOD_DELAY_MS = (50, 150)
FWD_QUEUE_MAX = 20

# ---- shared code ----
BCAST = b"\xff" * 6
BROADCAST_ID = 255
ROOT_ID = 0
PT_ADV = 0
PT_DATA = 2

HDR_FMT = ">BBBHI"          # version | key_id | sender_id | run_id | tx_counter
HDR_LEN = 9
CRC_LEN = 4
MAX_LEN = 250
DATA_HDR_FMT = ">BBBHBB"    # ptype | dest | origin | seq | ttl | sensor_code
DATA_HDR_LEN = 7
SENSORS = {1: ("DHT11", 8), 2: ("DS18", 4), 3: ("GPS", 8), 4: ("SMOKE", 1), 5: ("ACCEL", 4)}
CTR_FILE = "ctr.txt"


def ticks():
    return time.ticks_ms()


def elapsed(since):
    return time.ticks_diff(time.ticks_ms(), since)


class Counters:
    def __init__(self):
        self.lock = _thread.allocate_lock()
        self.d = {}

    def bump(self, name):
        with self.lock:
            self.d[name] = self.d.get(name, 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(self.d)


class Radio:
    """Broadcast-only ESP-NOW. Only the rx thread calls recv, only the tx thread calls send."""

    def __init__(self):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.config(channel=CHANNEL)
        self.e = espnow.ESPNow()
        self.e.active(True)
        self.e.add_peer(BCAST)

    def send(self, data):
        self.e.send(BCAST, data)

    def recv(self, timeout_ms):
        """Return (rssi, msg) or (None, None) on timeout."""
        mac, msg = self.e.recv(timeout_ms)
        if msg is None:
            return None, None
        try:
            rssi = self.e.peers_table[mac][0]
        except KeyError:
            rssi = None
        return rssi, bytes(msg)


def aes_ctr(key, iv, data):
    """AES-128-CTR built on ECB: this firmware's cryptolib has no mode 6.
    Counter block = iv; incremented as a big-endian integer from the last byte."""
    ecb = cryptolib.aes(key, 1)
    out = bytearray(len(data))
    ctr = bytearray(iv)
    for off in range(0, len(data), 16):
        ks = ecb.encrypt(bytes(ctr))
        chunk = data[off:off + 16]
        for i in range(len(chunk)):
            out[off + i] = chunk[i] ^ ks[i]
        j = 15
        while j >= 0:
            ctr[j] = (ctr[j] + 1) & 0xFF
            if ctr[j]:
                break
            j -= 1
    return bytes(out)


class Envelope:
    def __init__(self):
        self.key = keychain[KEY_ID]
        self.lock = _thread.allocate_lock()
        try:
            with open(CTR_FILE) as f:
                self.ctr = int(f.read().strip())
        except (OSError, ValueError):
            self.ctr = 0

    def _next_ctr(self):
        with self.lock:
            if self.ctr >= 0xFFFFFFFF:
                raise RuntimeError("tx_counter exhausted")
            self.ctr += 1
            with open(CTR_FILE, "w") as f:
                f.write(str(self.ctr))
            return self.ctr

    @staticmethod
    def _iv(run_id, sender, txc):
        # run_id(2) | sender_id(1) | tx_counter(4) | block_counter(9), starts at 0
        return struct.pack(">HBI", run_id, sender, txc) + bytes(9)

    def seal(self, sender, inner):
        txc = self._next_ctr()
        hdr = struct.pack(HDR_FMT, VERSION, KEY_ID, sender, RUN_ID, txc)
        crc = struct.pack(">I", crc32(hdr + inner) & 0xFFFFFFFF)
        enc = aes_ctr(self.key, self._iv(RUN_ID, sender, txc), inner + crc)
        return hdr + enc

    def open(self, msg):
        """Return (sender, inner) or (None, counter_name)."""
        if len(msg) < HDR_LEN + CRC_LEN + 5 or len(msg) > MAX_LEN:
            return None, "drop_len"
        ver, key_id, sender, run_id, txc = struct.unpack(HDR_FMT, msg[:HDR_LEN])
        if ver != VERSION:
            return None, "drop_ver"
        key = keychain.get(key_id)
        if key is None:
            return None, "drop_key"
        if run_id != RUN_ID:
            return None, "drop_run"
        plain = aes_ctr(key, self._iv(run_id, sender, txc), msg[HDR_LEN:])
        inner, got = plain[:-CRC_LEN], plain[-CRC_LEN:]
        if got != struct.pack(">I", crc32(msg[:HDR_LEN] + inner) & 0xFFFFFFFF):
            return None, "drop_crc"
        return sender, inner


class DupCache:
    def __init__(self):
        self.lock = _thread.allocate_lock()
        self.d = {}

    def seen(self, key):
        """True if key is already cached; otherwise record it and return False."""
        now = ticks()
        with self.lock:
            t = self.d.get(key)
            if t is not None and time.ticks_diff(now, t) < DUP_LIFETIME_MS:
                return True
            if len(self.d) >= DUP_MAX:
                for k in [k for k, v in self.d.items() if time.ticks_diff(now, v) >= DUP_LIFETIME_MS]:
                    del self.d[k]
                if len(self.d) >= DUP_MAX:
                    del self.d[max(self.d, key=lambda k: time.ticks_diff(now, self.d[k]))]
            self.d[key] = now
            return False


def _finite(x):
    return x - x == 0


def decode_payload(code, p):
    """Printable measurement with units, or None if invalid."""
    if code == 1:
        t, h = struct.unpack(">ff", p)
        if _finite(t) and _finite(h) and 0 <= h <= 100:
            return "temp=%.2f C hum=%.1f %%" % (t, h)
    elif code == 2:
        (t,) = struct.unpack(">f", p)
        if _finite(t):
            return "temp=%.2f C" % t
    elif code == 3:
        lon, lat = struct.unpack(">ff", p)
        if _finite(lon) and _finite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            return "lon=%.6f deg lat=%.6f deg" % (lon, lat)
    elif code == 4:
        if p[0] in (0, 1):
            return "smoke=%d" % p[0]
    elif code == 5:
        (a,) = struct.unpack(">f", p)
        if _finite(a) and a >= 0:
            return "accel=%.3f m/s2" % a
    return None


def check_data(inner):
    """Return ((dest, origin, seq, ttl, code, payload), text) or (None, counter_name)."""
    if len(inner) < DATA_HDR_LEN:
        return None, "drop_inner"
    _, dest, origin, seq, ttl, code = struct.unpack(DATA_HDR_FMT, inner[:DATA_HDR_LEN])
    spec = SENSORS.get(code)
    if spec is None:
        return None, "drop_code"
    payload = inner[DATA_HDR_LEN:]
    if len(payload) != spec[1]:
        return None, "drop_payload"
    text = decode_payload(code, payload)
    if text is None:
        return None, "drop_payload"
    return (dest, origin, seq, ttl, code, payload), text


def root_process(msg, mode, env, cnt, dup):
    """Root receive path. Prints one line per unique delivered packet."""
    cnt.bump("rx_total")
    sender, inner = env.open(msg)
    if sender is None:
        cnt.bump(inner)
        return
    if inner[0] == PT_ADV:
        if len(inner) != 5 or inner[2] != sender:
            cnt.bump("drop_inner")
        else:
            cnt.bump("adv_rx")
        return
    if inner[0] != PT_DATA:
        cnt.bump("drop_inner")
        return
    f, text = check_data(inner)
    if f is None:
        cnt.bump(text)
        return
    dest, origin, seq, ttl, code, payload = f
    if dest != (ROOT_ID if mode == "dv" else BROADCAST_ID):
        cnt.bump("drop_dest")      # overheard DV data for another device
        return
    if dup.seen((origin, seq)):    # ttl 0 is fine at the root
        cnt.bump("drop_dup")
        return
    cnt.bump("delivered")
    print("DELIVERED origin=%d seq=%d sensor=%s %s" % (origin, seq, SENSORS[code][0], text))

# ---- program ----
ROOT_HOP = 0
NO_PARENT = 0   # root advertises parent 0 (team agreement)

radio = Radio()
env = Envelope()
cnt = Counters()
dup = DupCache()


def adv_worker():
    while True:
        inner = struct.pack(">BBBBB", PT_ADV, BROADCAST_ID, ROOT_ID, ROOT_HOP, NO_PARENT)
        radio.send(env.seal(ROOT_ID, inner))
        cnt.bump("adv_tx")
        time.sleep_ms(ADV_INTERVAL_MS + urandom.getrandbits(16) % (ADV_INTERVAL_MS // 10 + 1))


if MODE == "dv":    # flooding needs no advertisements
    _thread.start_new_thread(adv_worker, ())

last = ticks()
while True:
    rssi, msg = radio.recv(500)
    if msg:
        root_process(msg, MODE, env, cnt, dup)
    if elapsed(last) >= 10000:
        last = ticks()
        print("COUNTERS", cnt.snapshot())
