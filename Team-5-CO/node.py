# node.py - non-root device with FC-22 (MQ-9) CO sensor. Set MODE below to "dv" or "flood". Needs keys.py.
# this file - CO detector module (MQ-7 style, digital output) -> sensor code 4.
#
# Wiring (ESP32 DevKit):
#   module VCC -> VIN (5 V)   [heater needs 5 V]
#   module GND -> GND
#   module DO  -> GPIO 14
#   AO not used.
# DO is pulled up to the module's VCC (5 V), but ESP32 pins are 3.3 V max:
# put a divider between DO and GPIO 34 (DO -- 10k --+-- GPIO34, +-- 20k -- GND).
#
# Interpretation: the on-board comparator drives DO LOW when CO is above the
# level set by the blue potentiometer (and the DO LED lights). DO LOW -> smoke=1,
# DO HIGH -> smoke=0 (payload: one unsigned byte). Let the module warm up
# (heater) for a few minutes before trusting readings.
import struct
import time
import _thread
import urandom
import network
import espnow
import cryptolib
from binascii import crc32
from keys import keychain
from machine import Pin

# ---- config ----
# Task.md section II: "Provide configuration parameters for device ID, root/non-root
# role, routing mode (DV or flooding), enabled sensor types, advertisement interval,
# data interval, RSS threshold, and initial TTL". The values are the ones the team
# agreed on in README.md. All times are in milliseconds.
DEVICE_ID = 5            # your team ID (1-254)
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
# ---- constants fixed by Task.md ----
# Section II: every transmission is an ESP-NOW broadcast (BCAST), device ID 255 means
# "everyone", the root is always ID 0. Packet types: 0 = advertisement, 2 = data (section V).
BCAST = b"\xff" * 6
BROADCAST_ID = 255
ROOT_ID = 0
PT_ADV = 0
PT_DATA = 2

# Section V.A outer packet: version | key_id | sender_id | run_id | tx_counter | ciphertext.
# Widths and byte order (big-endian) are the team choice from README.md: 1+1+1+2+4 = 9 bytes.
# CRC_LEN is the 4-byte CRC-32 that is appended to the inner packet before encryption.
# MAX_LEN is the ESP-NOW payload limit the task asks us to stay under.
HDR_FMT = ">BBBHI"          # version | key_id | sender_id | run_id | tx_counter
HDR_LEN = 9
CRC_LEN = 4
MAX_LEN = 250
# Section V data inner header, 7 bytes: ptype | dest_id | origin_id | seq(2) | ttl | sensor_code.
DATA_HDR_FMT = ">BBBHBB"    # ptype | dest | origin | seq | ttl | sensor_code
DATA_HDR_LEN = 7
# Section V sensor codes and the exact payload length each one must have:
# 1 DHT11 (2 x float32), 2 DS18 (float32), 3 GPS (2 x float32), 4 smoke (1 byte), 5 accel (float32).
# Codes 0 and 6-255 are reserved: a packet that uses them is rejected (see check_data).
SENSORS = {1: ("DHT11", 8), 2: ("DS18", 4), 3: ("GPS", 8), 4: ("SMOKE", 1), 5: ("ACCEL", 4)}
# Section V.A: the tx counter must survive reboots so counter blocks are never reused.
CTR_FILE = "ctr.txt"


def ticks():
    return time.ticks_ms()


def elapsed(since):
    return time.ticks_diff(time.ticks_ms(), since)


# Section VI: counters are shared between threads, so every update takes a lock.
# Section VIII needs them to compute PDR and overhead (sent / dropped / delivered).
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


# Section II: broadcast-only ESP-NOW on one channel. The only peer ever registered is
# the broadcast address, no per-neighbor MAC table. Section VI: one thread calls recv(),
# one thread calls send(), so the radio itself needs no lock.
# recv() also looks up the RSSI of the sender in peers_table (section III), which is the
# only place a MAC address is used.
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


# Section V.A: mandatory AES-CTR. cryptolib mode 6 is missing on our firmware, so CTR is
# built from AES-ECB: encrypt the counter block, XOR with the data, add 1 to the counter
# (big-endian, from the last byte). Same output as real CTR (checked against NIST SP 800-38A).
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


# Section V.A: the encryption envelope used for every packet, advertisements included.
# seal() builds an outer packet, open() checks and decrypts one.
class Envelope:
    def __init__(self):
        self.key = keychain[KEY_ID]
        self.lock = _thread.allocate_lock()
        try:
            with open(CTR_FILE) as f:
                self.ctr = int(f.read().strip())
        except (OSError, ValueError):
            self.ctr = 0

    # Section V.A: one tx_counter per encryption (adverts, own data, relayed data). It is
    # written to ctr.txt every time, so a reboot never repeats a counter block.
    def _next_ctr(self):
        with self.lock:
            if self.ctr >= 0xFFFFFFFF:
                raise RuntimeError("tx_counter exhausted")
            self.ctr += 1
            with open(CTR_FILE, "w") as f:
                f.write(str(self.ctr))
            return self.ctr

    # Section V.A counter block (16 bytes): run_id(2) | sender_id(1) | tx_counter(4) | block
    # counter(9). The block counter starts at 0 and is the part aes_ctr increments.
    @staticmethod
    def _iv(run_id, sender, txc):
        # run_id(2) | sender_id(1) | tx_counter(4) | block_counter(9), starts at 0
        return struct.pack(">HBI", run_id, sender, txc) + bytes(9)

    # Section V.A sending side: CRC-32 over (outer header + plaintext inner packet), append it
    # big-endian to the inner packet, then encrypt both together. Header stays in clear.
    def seal(self, sender, inner):
        txc = self._next_ctr()
        hdr = struct.pack(HDR_FMT, VERSION, KEY_ID, sender, RUN_ID, txc)
        crc = struct.pack(">I", crc32(hdr + inner) & 0xFFFFFFFF)
        enc = aes_ctr(self.key, self._iv(RUN_ID, sender, txc), inner + crc)
        return hdr + enc

    # Section V.A receiving side, in the order the task gives: check length, version, key ID and
    # run ID BEFORE decrypting, then decrypt, split off the CRC, recompute it over header +
    # plaintext and compare. Any failure returns a counter name and the caller drops the packet,
    # so nothing touches routing state, the duplicate cache or the delivered log.
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


# Section V: bounded duplicate cache keyed by (origin_id, seq). Lifetime 60 s (longer than a
# packet can live), at most 200 entries; the oldest entry is evicted when full.
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


# Section V validation of the measurement itself: finite floats, humidity 0-100, longitude
# -180..180, latitude -90..90, smoke 0 or 1, acceleration >= 0. Returns text for the log or
# None if the value is not valid (the packet is then discarded and counted).
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
            return "smoke=%d (%s)" % (p[0], "CO detected" if p[0] else "not detected")
    elif code == 5:
        (a,) = struct.unpack(">f", p)
        if _finite(a) and a >= 0:
            return "accel=%.3f m/s2" % a
    return None


# Section V: validate a decrypted data packet. Rejects reserved sensor codes (0, 6-255), a
# payload whose length does not match the sensor code, and invalid values.
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



# ---- program ----
# Section V code 4 (smoke): the FC-22 / MQ-9 digital output is normalized to 1 byte,
# 0 = not detected, 1 = detected. DO is low when CO is above the potentiometer threshold.
DO_PIN = 14
ACTIVE_LOW = True
_pin = Pin(DO_PIN, Pin.IN)


def read_co():
    """Return 0 (not detected) or 1 (detected)."""
    v = _pin.value()
    return 1 if (v == 0) == ACTIVE_LOW else 0

# Section VI: the main thread receives (rx), the worker thread sends (tx). Shared state is
# protected by locks, and the worker never holds a lock while sleeping or sending.
ME = DEVICE_ID
NONE = 255

radio = Radio()
env = Envelope()
cnt = Counters()
dup = DupCache()

# Section III/IV shared state: neighbor table and (parent, hop) are always read and written
# together under one lock, so the route is one consistent value (section IV).
state_lock = _thread.allocate_lock()   # guards neighbors and route
neighbors = {}                         # id -> [hop, parent, rssi, ema, last_ms]
route = {"parent": NONE, "hop": NONE}

# Section VI relay queue, bounded to FWD_QUEUE_MAX entries; overflow is counted as drop_queue.
fq_lock = _thread.allocate_lock()      # guards the relay queue
fq = []                                # (due_ms, origin, seq, code, ttl, payload)


def rnd(n):
    return urandom.getrandbits(16) % n


def jittered(base):
    return base + rnd(base // 10 + 1)


# ---- routing (call with state_lock held; returns log text or None) -----------
# Section IV parent selection, called with state_lock held.
# 1) exclude stale neighbors, unknown routes (hop 255), myself, neighbors whose parent is me,
#    links below the RSS threshold and hop values that would reach 255;
# 2) prefer the smallest hop (neighbor hop + 1);
# 3) same hop: prefer the strongest smoothed RSS, then the smaller ID;
# 4) hysteresis: keep a still-eligible parent unless a shorter route exists or a same-hop
#    alternative is stronger by more than RSS_MARGIN dB.
# No candidate -> parent 255 and hop 255. Returns a log line (old/new parent, hop, RSS, reason).
def recompute(now):
    cands = []
    for i, n in neighbors.items():
        if time.ticks_diff(now, n[4]) > NEIGHBOR_TIMEOUT_MS:
            continue                       # stale
        if i == ME or n[0] >= NONE or n[1] == ME:
            continue                       # self, unknown route, or child of mine
        if n[3] < RSS_THRESHOLD or n[0] + 1 >= NONE:
            continue
        cands.append((n[0] + 1, -n[3], i))  # smaller = better hop, stronger RSS, lower ID
    old_p, old_h = route["parent"], route["hop"]
    cur = None
    for c in cands:
        if c[2] == old_p:
            cur = c
    best = min(cands) if cands else None
    if best is None:
        new, why = None, "no eligible candidate"
    elif cur is None:
        new, why = best, "first route" if old_p == NONE else "parent ineligible"
    elif best[0] < cur[0]:
        new, why = best, "shorter route"
    elif best[0] == cur[0] and best[2] != cur[2] and cur[1] - best[1] > RSS_MARGIN:
        new, why = best, "same hop, RSS better by > %d dB" % RSS_MARGIN
    else:
        new, why = cur, None
    p, h = (NONE, NONE) if new is None else (new[2], new[0])
    route["parent"], route["hop"] = p, h
    if p != old_p or h != old_h:
        rss = "-" if new is None else "%.1f" % -new[1]
        return "ROUTE parent %d->%d hop %d->%d rss=%s reason=%s" % (old_p, p, old_h, h, rss, why or "hop update")
    return None


# Section III: a valid advertisement creates or updates the neighbor entry (hop, parent,
# latest RSSI, smoothed RSSI, last-heard time). Smoothing is an exponential moving average
# with RSS_ALPHA = 0.3. The inner source must equal the outer sender (section V.A check).
def on_advert(rssi, inner, sender):
    if len(inner) != 5 or inner[2] != sender:
        cnt.bump("drop_inner")
        return
    src, hop, par = inner[2], inner[3], inner[4]
    if src == ME or rssi is None:
        return
    cnt.bump("adv_rx")
    now = ticks()
    msg = None
    with state_lock:
        n = neighbors.get(src)
        if n is None:
            neighbors[src] = [hop, par, rssi, float(rssi), now]
            msg = "NEIGHBOR NEW id=%d hop=%d rssi=%d" % (src, hop, rssi)
        else:
            n[0], n[1], n[2], n[4] = hop, par, rssi, now
            n[3] = RSS_ALPHA * rssi + (1 - RSS_ALPHA) * n[3]
        change = recompute(now)
    if msg:
        print(msg)
    if change:
        print(change)


# Section III: remove neighbors not heard within NEIGHBOR_TIMEOUT_MS (time.ticks_diff is used
# for every comparison) and re-run parent selection so a stale parent is dropped at once.
def expire():
    now = ticks()
    gone = []
    with state_lock:
        for i in list(neighbors):
            if time.ticks_diff(now, neighbors[i][4]) > NEIGHBOR_TIMEOUT_MS:
                del neighbors[i]
                gone.append(i)
        change = recompute(now) if gone else None
    for i in gone:
        print("NEIGHBOR EXPIRED id=%d" % i)
    if change:
        print(change)


# Section III demonstration: periodic printout of the neighbor table.
def print_table():
    now = ticks()
    with state_lock:
        rows = sorted((i, n[0], n[1], n[2], n[3], time.ticks_diff(now, n[4])) for i, n in neighbors.items())
        p, h = route["parent"], route["hop"]
    print("TABLE me=%d parent=%d hop=%d" % (ME, p, h))
    for r in rows:
        print("  id=%d hop=%d parent=%d rssi=%d ema=%.1f age=%dms" % r)


# Section V/VII receive path for data. DV: only the node named in dest_id processes it.
# Flooding: dest is 255 and everyone processes it. Then: relay TTL 0 is discarded, the
# duplicate cache suppresses repeats, and the packet is queued for the worker thread to
# send (flooding adds a random 50-150 ms delay first).
def on_data(inner):
    f, text = check_data(inner)
    if f is None:
        cnt.bump(text)
        return
    dest, origin, seq, ttl, code, payload = f
    if dest != (ME if MODE == "dv" else BROADCAST_ID):
        cnt.bump("drop_dest")
        return
    if ttl == 0:
        cnt.bump("drop_ttl")               # relay must discard TTL 0
        return
    if dup.seen((origin, seq)):
        cnt.bump("drop_dup")
        return
    due = ticks()
    if MODE == "flood":
        due = time.ticks_add(due, FLOOD_DELAY_MS[0] + rnd(FLOOD_DELAY_MS[1] - FLOOD_DELAY_MS[0] + 1))
    with fq_lock:
        if len(fq) >= FWD_QUEUE_MAX:
            full = True
        else:
            fq.append((due, origin, seq, code, ttl - 1, payload))
            full = False
    if full:
        cnt.bump("drop_queue")
        print("DROP queue full origin=%d seq=%d" % (origin, seq))


# ---- tx side (worker thread only) ---------------------------------------------
# ---- tx side: only the worker thread calls these ----
# Section III: advertisements carry my current hop and parent (255/255 when I have no route).
def send_adv():
    with state_lock:
        p, h = route["parent"], route["hop"]
    inner = struct.pack(">BBBBB", PT_ADV, BROADCAST_ID, ME, h, p)
    radio.send(env.seal(ME, inner))
    cnt.bump("adv_tx")


# Section V: build and broadcast a data packet. DV: dest = current parent (no route -> count
# and drop, no buffering, no retries). Flooding: dest = 255. Origin, seq, sensor code and
# payload are kept as received; only dest and ttl change. ttl arrives already decremented.
def send_data(origin, seq, code, ttl, payload, local):
    """ttl is the value to put on the air (already decremented)."""
    if MODE == "dv":
        with state_lock:
            dest = route["parent"]
        if dest == NONE:
            cnt.bump("drop_noroute")
            print("DROP no route origin=%d seq=%d" % (origin, seq))
            return
    else:
        dest = BROADCAST_ID
    inner = struct.pack(DATA_HDR_FMT, PT_DATA, dest, origin, seq, ttl, code) + payload
    radio.send(env.seal(ME, inner))
    cnt.bump("data_tx_own" if local else "data_tx_relay")


# Section V: one device-wide sequence number for every new data packet (all sensor types).
seq_counter = 0   # device-wide, touched only by the worker thread


# Section V data generation: read the sensor, count it as generated, then send. A failed read
# is counted as acq_fail and produces no packet. TTL starts at TTL_INIT and is decremented
# before sending, so my own packets go out with TTL_INIT - 1. The packet is marked as seen
# first (section VII) so my own broadcast is ignored if it comes back.
def generate():
    global seq_counter
    try:
        payload = bytes([read_co()])
    except Exception as ex:
        cnt.bump("acq_fail")
        print("ACQ FAIL co:", ex)
        return                              # no packet on failed acquisition
    seq = seq_counter
    seq_counter = (seq_counter + 1) & 0xFFFF
    cnt.bump("generated")
    ttl = TTL_INIT
    if ttl <= 0:
        return
    ttl -= 1                                # own packets go out with 9
    dup.seen((ME, seq))                     # mark own packet as seen first
    print("GENERATE seq=%d smoke=%d" % (seq, payload[0]))
    send_data(ME, seq, 4, ttl, payload, True)


# Section VI worker thread: schedules advertisements (DV only) and own data with jitter,
# and sends queued relay packets when they are due. A short sleep avoids a busy loop.
def worker():
    now = ticks()
    next_adv = time.ticks_add(now, jittered(ADV_INTERVAL_MS))
    next_data = time.ticks_add(now, jittered(DATA_INTERVAL_MS))
    while True:
        now = ticks()
        if MODE == "dv" and time.ticks_diff(now, next_adv) >= 0:
            send_adv()
            next_adv = time.ticks_add(now, jittered(ADV_INTERVAL_MS))
        if time.ticks_diff(now, next_data) >= 0:
            generate()
            next_data = time.ticks_add(now, jittered(DATA_INTERVAL_MS))
        item = None
        with fq_lock:
            for k, it in enumerate(fq):
                if time.ticks_diff(now, it[0]) >= 0:
                    item = fq.pop(k)
                    break
        if item:
            send_data(item[1], item[2], item[3], item[4], item[5], False)
        else:
            time.sleep_ms(10)


_thread.start_new_thread(worker, ())

# Section VI main/receive thread: the only thread that calls recv(). It decrypts and checks
# every packet (section V.A), updates neighbors (III), selects the parent (IV), expires
# neighbors every second, and prints the neighbor table and counters every 10 s.
print("node %d mode=%s run=%d" % (ME, MODE, RUN_ID))
last_exp = last_tab = last_cnt = ticks()
while True:
    rssi, msg = radio.recv(100)
    if msg:
        cnt.bump("rx_total")
        sender, inner = env.open(msg)
        if sender is None:
            cnt.bump(inner)
        elif inner[0] == PT_ADV:
            if MODE == "dv":
                on_advert(rssi, inner, sender)
        elif inner[0] == PT_DATA:
            on_data(inner)
        else:
            cnt.bump("drop_inner")
    if elapsed(last_exp) >= 1000:
        last_exp = ticks()
        if MODE == "dv":
            expire()
    if elapsed(last_tab) >= 10000:
        last_tab = ticks()
        if MODE == "dv":
            print_table()
    if elapsed(last_cnt) >= 10000:
        last_cnt = ticks()
        print("COUNTERS", cnt.snapshot())
