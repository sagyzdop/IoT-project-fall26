# IoT-project-fall26

## Overview

This is a group project aimed at creating a IoT network using routing and flooding and comparing them.

## Hardware $ Software

We all have ESP-WROOM-32 (Wi-Fi, Bluetooth). We use Thonny and MicroPython for the development.

---

We need to reach an agreement on the items left open-ended in the technical specifications. Here is what we somewhat agreed on.

## AES key: shall we use AES-128 (16 bytes)?

Yes AES-128 16 bytes, CTR mode (PSK is pre shared key, so we will pre share them, we did the keychain dictionary with key_id => key in a separate keys.py)

## Integrity check: shall we use CRC-32 or SHA-256?

Lets use CRC-32 4 bytes (from binascii import crc32), computed over outer header + plaintext inner packet, appended big-endian to the inner packet and then both are encrypted together

## Outer header field widths (big-endian)

`version(1) | key_id(1) | sender_id(1) | run_id(2) | tx_counter(4) | ciphertext`

All big-endian, 9 bytes total (4 bytes for run id seems too much, 65536 run ids from 2 bytes is more than enough. 

## Counter block layout (16 bytes)

`run_id(2) | sender_id(1) | tx_counter(4) | block_counter(9)`

Block_counter starts at 0, +1 for every 16-byte block, big-endian, incremented from the last byte (this is just how micropython cryptolib CTR mode 6 does it, so put the block counter at the end, not in the middle). max packet is 250 bytes (max of ESPNOW) = 16 blocks so only the last byte ever changes

Heads up: on our firmware (MicroPython v1.27.0, generic ESP32) `cryptolib.aes(key, 6, iv)` raises `ValueError: mode`, so mode 6 does not exist there. Only ECB (1) and CBC (2) work. The code builds CTR on top of ECB with the same counter block layout and the same increment rule, so the packets are identical to what mode 6 would give. It reproduces the NIST SP 800-38A AES-128 CTR test vector on the board.


## A single shared PSK: who generates it, and how do we distribute it?

Keychain with keys, later we can just change the used key from the keychain (this is how it is done in most routing protocols irl). We can come up with any keys (just pregenerate them randomly) and share them here:

```python
keychain = {
    1: bytes.fromhex("86d365dd9a3afe64003b2a4e9b141a10"),
    2: bytes.fromhex("e596ad6a68df0a37d10eaa885cf230ff"),
    3: bytes.fromhex("496655eb7d9e86073163200a4bae2c1f"),
    4: bytes.fromhex("5f02074c35682a4a753b3c03c9dcb8b2")
}
```

## `run_id`: who sets it, and how do we change it between runs?

`run_id` is just hardcoded, it is like attempts we have, each attempt we add +1 manually. but it must be the same on all boards, so someone announces it before every run (if your run_id is different all your packets get dropped). and dont delete ctr.txt on your board, it keeps tx_counter between reboots so counter blocks never repeat

## Intervals: advertisement (10 s for the root, as per the instructor; 5 or 10 s for the others?), neighbor timeout, data interval, RSS threshold of -85, margin of 5 dB, TTL 10.

- Advertisement: 5 s + 0–10% jitter (root too)
- Data: 10 s + 0–10% jitter
- Neighbor timeout: 15 s, checked every 1 s
- RSS threshold: −85 dBm, EMA α = 0.3
- Parent switch margin: 5 dB (same hop only)
- TTL: 10, decremented before sending (own packets go out with 9)
- Duplicate cache: (origin_id, seq), 60 s / 200 entries (I guess cache size is up to the team, lifetime of cache entry must be big enough to outlive oldest packet, so 60s seems fine)
- Flooding relay delay: random 50–150 ms ( I guess this is also up to the team, this is what we will use)

---

Same wifi channel for everyone, lets use channel 1 for now, version = 1, root is ID 0, teams ID 1-254, every node advertises even with no route (hop 255, parent 255), root always hop 0 parent 255, and everyone uses the same mode when testing (dv or flood)

- `key_id` = 1
- Root advertises parent of 0
- Don't consider smoother RSSI estimate
- Measure only PDR, PDR per device, Startup convergence time (DV)
- Implement lock (mutex) for neighbor table handled by ESP-NOW. We can 1 rx_thread and 1 tx_thread. So like no lock needed for radio. Onlt rx will cal rcv and only tx will call tx. Even though it is half-duplex, it is handled by ESP-NOW so it will just not hear anything while we send, so priority to tx.
- Everybody has to tell me their payload length

---

The sensors we have are:
- MPU-6050 (3x)
- GPS (2x)
- DS18B20 (2x)
- CO (2x)
- DHT11 (2x)

---

## Code

Everything is in `prototypes/`. Two programs, one per board, each a single file with a `MODE` constant at the top:

- `root.py`: the root, ID 0. In `"dv"` mode it advertises hop 0 with parent 0 and logs every unique data packet it receives. In `"flood"` mode it only receives.
- `node.py`: a non-root device with the CO sensor. It advertises, picks a parent, forwards for others and sends its own samples. `DEVICE_ID` and `MODE` are set at the top (our team ID is 5).
- `keys.py`: the keychain from above. It is not committed. Create it yourself from the `keychain` block in this README and copy it to every board.

`MODE` and `RUN_ID` must be identical on all boards. Both programs use the rx thread for receiving and one worker thread for sending, so the radio never has two callers.

### Putting it on a board

1. Stop whatever is running (Ctrl-C in Thonny). If the board has an old lab `main.py` that connects to Wi-Fi, rename it, otherwise it blocks the REPL and fights ESP-NOW for the radio.
2. Save `keys.py` on the board.
3. Save `root.py` or `node.py` on the board as `main.py`.
4. Press RST. The program starts by itself on every reset or power-up.

Don't delete `ctr.txt`, it keeps the tx counter across reboots.

### CO sensor (FC-22, MQ-9)

Sensor code 4. The module has four pins: VCC to VIN (5 V), GND to GND, DO to GPIO 14. AO is not used, so leave it unconnected. The heater smells a bit and gets hot for the first few minutes, that is normal.

DO is pulled up to the module supply, so on 5 V it can be above the 3.3 V the ESP32 pin is rated for. Measure it with no gas present, and if it reads about 5 V put a divider in (10k from DO to the pin, 20k from the pin to GND).

DO goes low when CO is above the level set by the potentiometer. Low is reported as `smoke=1`, high as `smoke=0`.

### Tested so far

DV mode with one root and one node, boards next to each other. The node found the root, chose it as parent (`ROUTE parent 255->0 hop 255->1`), the RSSI estimate updated, and the root logged every packet once with no drops. The first sample, generated before the route existed, is dropped as "no route", as the spec asks.

Not tested yet: flooding mode, two-hop paths, parent switching, neighbor expiry, and the other four sensor types.
