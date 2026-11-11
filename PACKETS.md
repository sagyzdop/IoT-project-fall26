# Packet format reference

Everything a node needs to build packets the root accepts, and everything the root checks.

Each rule comes from one of two places. Rules marked **task** are defined in [Task.md section V](Task.md#v-task-3-data-generation-and-delivery) and [section V.A](Task.md#va-mandatory-aes-ctr-encryption-and-integrity-checks). Rules marked **agreed** are the team choices recorded in [README.md](README.md#team-agreements) (field widths, key handling, intervals). All multi-byte values are big-endian, so every `struct` format below starts with `>`.

## Contents

- [Layers](#layers)
- [Outer header](#outer-header)
- [Encryption and integrity](#encryption-and-integrity)
- [Advertisement packet](#advertisement-packet)
- [Data packet header](#data-packet-header)
- [Sensor codes and payloads](#sensor-codes-and-payloads)
- [Packing](#packing)
- [Unpacking and validation](#unpacking-and-validation)
- [Sizes](#sizes)

## Layers

Every transmission, advertisement or data, is an ESP-NOW broadcast with this layout:

```
| outer header, 9 B, clear | ciphertext = AES-128-CTR( inner packet | CRC-32, 4 B ) |
```

The envelope adds 13 bytes to every packet. The inner packet is an [advertisement](#advertisement-packet) or a [data packet](#data-packet-header). See [Task.md, outer packet format](Task.md#outer-packet-format) and [integrity value](Task.md#integrity-value).

## Outer header

Format `">BBBHI"`, 9 bytes. Widths are **agreed**, see [README, outer header field widths](README.md#outer-header-field-widths); the field list is **task**.

| field | bytes | value |
| --- | --- | --- |
| version | 1 | 1 |
| key_id | 1 | 1, selects the key in `keychain` ([README, shared PSK](README.md#shared-psk-and-keychain)) |
| sender_id | 1 | the device that is transmitting now (not the origin) |
| run_id | 2 | same on every board for a run, +1 per run ([README, run ID](README.md#run-id)) |
| tx_counter | 4 | per-sender counter, +1 for every new encryption (adverts, own data, relayed data) |

## Encryption and integrity

Task rules: [section V.A](Task.md#va-mandatory-aes-ctr-encryption-and-integrity-checks). Our choices: [README, AES key](README.md#aes-key), [integrity check](README.md#integrity-check) and [counter block layout](README.md#counter-block-layout).

- Key: 16-byte AES-128 key from `keychain[key_id]`, provisioned in a separate `keys.py` and never printed or sent.
- CRC-32: `binascii.crc32(outer_header + plaintext_inner) & 0xFFFFFFFF`, packed as 4 bytes big-endian and appended to the inner packet. Header and inner are covered, so changing either breaks the CRC.
- Counter block, 16 bytes: `run_id (2) | sender_id (1) | tx_counter (4) | block_counter (9)`. The block counter starts at 0 and goes up by 1 per 16-byte block, big-endian, from the last byte. A packet has at most 16 blocks, so only the last byte changes.
- Encrypt `inner_packet | crc` with that counter block. CTR needs no padding.
- Never reuse a counter block with the same key. Keep `tx_counter` in a file (`ctr.txt`) so it survives reboots ([Task.md, run identifier and transmission counter](Task.md#run-identifier-and-transmission-counter)).

On our firmware (MicroPython v1.27.0, generic ESP32) `cryptolib.aes(key, 6, iv)` raises `ValueError: mode`. CTR is built from ECB instead and gives the same output (checked against the NIST SP 800-38A vector):

```python
import cryptolib

def aes_ctr(key, iv, data):
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
```

Sending:

```python
hdr = struct.pack(">BBBHI", 1, KEY_ID, MY_ID, RUN_ID, tx_counter)
crc = struct.pack(">I", crc32(hdr + inner) & 0xFFFFFFFF)
iv  = struct.pack(">HBI", RUN_ID, MY_ID, tx_counter) + bytes(9)
packet = hdr + aes_ctr(key, iv, inner + crc)
```

Receiving, in this order ([Task.md, reception](Task.md#reception)): check length, version, key_id and run_id before decrypting; decrypt; split off the last 4 bytes; recompute the CRC over the received header and decrypted inner packet; compare. On any mismatch discard and count the packet. Only a packet that passes everything may update routing state, the duplicate cache, be forwarded, or be logged at the root.

A relay builds a new envelope: its own outer header, a new CRC and a fresh `tx_counter` ([Task.md, relaying](Task.md#relaying)).

## Advertisement packet

Inner packet type 0, defined in [Task.md, inner-packet formats](Task.md#inner-packet-formats) (**task**). Format `">BBBBB"`, 5 bytes, 18 on air.

| field | bytes | value |
| --- | --- | --- |
| ptype | 1 | 0 |
| dest_id | 1 | 255 |
| src_id | 1 | the advertiser, must equal the outer `sender_id` |
| hop | 1 | hops to the root, 255 = no route, root = 0 |
| parent_id | 1 | selected parent, 255 = none |

Task.md says parent 255 means no parent, including at the root. The team agreed that the root advertises parent 0 ([README, other settings](README.md#other-settings)). Receivers only exclude a neighbor whose parent equals their own ID, so either value works for nodes. How advertisements feed the neighbor table is in [Task.md section III](Task.md#iii-task-1-neighbor-discovery).

## Data packet header

Inner packet type 2 (**task**, [inner-packet formats](Task.md#inner-packet-formats)). The 7-byte header is identical for every sensor. Format `">BBBHBB"`.

| field | bytes | value |
| --- | --- | --- |
| ptype | 1 | 2 |
| dest_id | 1 | DV: ID of the next hop (the sender's parent). Flooding: 255 |
| origin_id | 1 | device that took the measurement, never changed while relaying |
| seq | 2 | device-wide counter, +1 for every new data packet of any sensor type |
| ttl | 1 | transmissions still allowed |
| sensor_code | 1 | 1 to 5, selects the payload |

`(origin_id, seq)` identifies a packet and is the key of the duplicate cache ([Task.md, validation and sequence numbers](Task.md#validation-and-sequence-numbers)).

## Sensor codes and payloads

Fixed by [Task.md, sensor codes and payloads](Task.md#sensor-codes-and-payloads) (**task**).

| code | sensor | payload | format | payload B | inner B | on air B |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | DHT11 | temperature (degrees C), relative humidity (%) | `">ff"` | 8 | 15 | 28 |
| 2 | DS18 series | temperature (degrees C) | `">f"` | 4 | 11 | 24 |
| 3 | GPS | longitude, latitude (decimal degrees, west and south negative) | `">ff"` | 8 | 15 | 28 |
| 4 | Smoke detection | 0 = not detected, 1 = detected | `">B"` | 1 | 8 | 21 |
| 5 | Accelerometer | mean acceleration magnitude (m/s²) | `">f"` | 4 | 11 | 24 |

Floats are IEEE 754 binary32. Codes 0 and 6 to 255 are reserved and must be rejected. Which team uses which sensor is in the [README team table](README.md#teams).

Accelerometer value ([Task.md, accelerometer averaging](Task.md#accelerometer-averaging)): sample x, y and z at a steady rate over a full 2 s window, convert to m/s², and send the mean of the per-sample magnitudes (gravity included):

$$
\bar{a} = \frac{1}{N} \sum_{k=1}^{N} \sqrt{a_{x,k}^2 + a_{y,k}^2 + a_{z,k}^2}
$$

Do not take the magnitude of the averaged axes. For smoke, normalize the detector output to 0 or 1 and document how (digital output level or threshold). A failed sensor read or an invalid GPS fix must not become a packet: count it separately and send nothing.

## Packing

```python
header  = struct.pack(">BBBHBB", 2, dest, MY_ID, seq, ttl, code)
payload = struct.pack(">ff", temp_c, humidity)       # code 1
payload = struct.pack(">f",  temp_c)                 # code 2
payload = struct.pack(">ff", longitude, latitude)    # code 3
payload = bytes([1 if detected else 0])              # code 4
payload = struct.pack(">f",  accel_mean)             # code 5
inner   = header + payload
```

TTL ([Task.md, required delivery behavior](Task.md#required-delivery-behavior)): a sender needs `ttl > 0` and decrements it before each transmission, so a new packet with initial TTL 10 goes out with 9. A relay that receives TTL 0 discards the packet. The root accepts TTL 0.

Worked example: DHT11 from node 5, destination 0, seq 258, ttl 9, 24.5 C, 41.0 %.

```
header   02 00 05 0102 09 01
payload  41c40000 42240000
inner    0200050102090141c4000042240000        (15 bytes)
```

CO detected from node 5, destination 0, seq 259, ttl 9: `0200050103090401` (8 bytes).

## Unpacking and validation

```python
ptype, dest, origin, seq, ttl, code = struct.unpack(">BBBHBB", inner[:7])
payload = inner[7:]
```

Checks, all required by [Task.md section V](Task.md#v-task-3-data-generation-and-delivery):

- `sensor_code` is 1 to 5, and `len(payload)` equals the length in the [table](#sensor-codes-and-payloads) exactly.
- Floats are finite. Humidity is 0 to 100, longitude -180 to 180, latitude -90 to 90, smoke is 0 or 1, acceleration is not negative.
- DV: process a data packet only if `dest_id` is your own ID. Ignore overheard packets for others.
- The `(origin_id, seq)` pair is not already in the duplicate cache. Cache lifetime 60 s, up to 200 entries ([README, intervals and thresholds](README.md#intervals-and-thresholds)).
- A relay keeps `origin_id`, `seq`, `sensor_code` and the payload bytes, sets the new `dest_id` and `ttl`, then builds its own [outer header](#outer-header), a new CRC and a fresh `tx_counter`.

## Sizes

The largest packet is 28 bytes, far below the 250-byte ESP-NOW limit. Of those, 13 bytes are envelope (9 header and 4 CRC), so a smoke packet is 21 bytes with 1 byte of measurement. Choices that keep it small: CRC-32 (4 B) instead of SHA-256 (32 B), a 2-byte `run_id`, and CTR instead of a padded block mode. The 7-byte data header and the float32 payloads are fixed by the task.
