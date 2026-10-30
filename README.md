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
