# IoT-project-fall26

## Overview

This is a group project aimed at creating an IoT network using routing and flooding and comparing them. The assignment is in [Task.md](Task.md) (also as [Task.pdf](Task.pdf)); the packet formats we use are in [PACKETS.md](PACKETS.md).

- [Hardware and software](#hardware-and-software)
- [Team agreements](#team-agreements)
- [Sensors and teams](#sensors-and-teams)
- [Code](#code)

## Hardware and software

We all have ESP-WROOM-32 (Wi-Fi, Bluetooth). We use Thonny and MicroPython for the development.

## Team agreements

[Task.md](Task.md) leaves some items open-ended. Here is what we somewhat agreed on. The packet layouts that follow from these choices are in [PACKETS.md](PACKETS.md).

### AES key

AES-128, 16 bytes, CTR mode. The PSK is pre-shared, so we pre-share it: the keychain dictionary `key_id => key` lives in a separate `keys.py` (see [shared PSK and keychain](#shared-psk-and-keychain)). Task.md asks for this in [section V.A](Task.md#va-mandatory-aes-ctr-encryption-and-integrity-checks).

### Integrity check

CRC-32, 4 bytes (`from binascii import crc32`), computed over outer header + plaintext inner packet, appended big-endian to the inner packet, and then both are encrypted together. See [Task.md, integrity value](Task.md#integrity-value) and [PACKETS.md, encryption and integrity](PACKETS.md#encryption-and-integrity).

### Outer header field widths

All big-endian, 9 bytes in total:

`version(1) | key_id(1) | sender_id(1) | run_id(2) | tx_counter(4) | ciphertext`

4 bytes for the run ID seems too much: 65536 run IDs from 2 bytes is more than enough. Layout details: [PACKETS.md, outer header](PACKETS.md#outer-header).

### Counter block layout

16 bytes:

`run_id(2) | sender_id(1) | tx_counter(4) | block_counter(9)`

The block counter starts at 0 and goes up by 1 for every 16-byte block, big-endian, incremented from the last byte. This is how MicroPython's `cryptolib` CTR mode 6 does it, so the block counter goes at the end, not in the middle. The maximum packet is 250 bytes (the ESP-NOW maximum), which is 16 blocks, so only the last byte ever changes.

Heads up: on our firmware (MicroPython v1.27.0, generic ESP32) `cryptolib.aes(key, 6, iv)` raises `ValueError: mode`, so mode 6 does not exist there. Only ECB (1) and CBC (2) work. The code builds CTR on top of ECB with the same counter block layout and the same increment rule, so the packets are identical to what mode 6 would give. It reproduces the NIST SP 800-38A AES-128 CTR test vector on the board. The code is in [PACKETS.md, encryption and integrity](PACKETS.md#encryption-and-integrity).

### Shared PSK and keychain

We use a keychain, so we can later switch the key in use without changing anything else (this is how most routing protocols do it in practice). The keys are pregenerated randomly and shared here:

```python
keychain = {
    1: bytes.fromhex("86d365dd9a3afe64003b2a4e9b141a10"),
    2: bytes.fromhex("e596ad6a68df0a37d10eaa885cf230ff"),
    3: bytes.fromhex("496655eb7d9e86073163200a4bae2c1f"),
    4: bytes.fromhex("5f02074c35682a4a753b3c03c9dcb8b2")
}
```

`key_id` = 1 is the one in use.

### Run ID

`run_id` is hardcoded. It counts our attempts: each attempt we add 1 manually. It must be the same on all boards, so someone announces it before every run (if your `run_id` differs, all your packets get dropped). Don't delete `ctr.txt` on your board: it keeps `tx_counter` between reboots so counter blocks never repeat ([Task.md, run identifier and transmission counter](Task.md#run-identifier-and-transmission-counter)).

### Intervals and thresholds

Task.md suggests starting values in [section II](Task.md#ii-equipment-and-configuration); ours:

- Advertisement: 5 s + 0–10% jitter (root too). The instructor mentioned 10 s for the root, but we use 5 s for everyone.
- Data: 10 s + 0–10% jitter.
- Neighbor timeout: 15 s, checked every 1 s.
- RSS threshold: −85 dBm, EMA with $\alpha = 0.3$ (see [Task.md section III](Task.md#iii-task-1-neighbor-discovery)).
- Parent switch margin: 5 dB (same hop only, see [section IV](Task.md#iv-task-2-parent-selection)).
- TTL: 10, decremented before sending, so own packets go out with 9.
- Duplicate cache: `(origin_id, seq)`, 60 s / 200 entries. The cache size is up to the team; the lifetime of an entry must be long enough to outlive the oldest packet, so 60 s seems fine.
- Flooding relay delay: random 50–150 ms. This is also up to the team ([Task.md section VII](Task.md#vii-task-5-flooding-baseline)).

### Other settings

- Same Wi-Fi channel for everyone: channel 1 for now.
- Version = 1. Root is ID 0, teams use IDs 1–254.
- Every node advertises even with no route (hop 255, parent 255). The root has hop 0. Task.md says the root's parent is 255, but we agreed the root advertises parent 0, and the code does that.
- Everyone uses the same mode when testing (DV or flood).
- No extra smoothing of the RSSI estimate beyond the EMA above.
- We measure only PDR, PDR per device and startup convergence time (DV), see [Task.md section VIII](Task.md#viii-task-6-experimental-comparison).
- One lock (mutex) for the neighbor table handled by ESP-NOW. We use 1 rx thread and 1 tx thread, so no lock is needed for the radio: only rx calls `recv` and only tx calls `send`. Even though the radio is half-duplex, ESP-NOW handles it, so the board just doesn't hear anything while it sends, so tx has priority. See [Task.md section VI](Task.md#vi-task-4-threaded-operation).
- Everybody has to tell the instructor their payload length. The lengths are in [PACKETS.md, sensor codes and payloads](PACKETS.md#sensor-codes-and-payloads).

## Sensors and teams

The sensors we have are:

- MPU-6050 (3x)
- GPS (2x)
- DS18B20 (2x)
- CO (2x)
- DHT11 (2x)

The team number is also the device ID. Sensor codes and payloads for each type are in [PACKETS.md](PACKETS.md#sensor-codes-and-payloads).

### Teams

| No  | Team member 1       | Team member 2         | Team member 3       | Team member 4        | Sensor   |
| --- | ------------------- | --------------------- | ------------------- | -------------------- | -------- |
| 1   | Amina Aidyn         | Aisana Abdrayeva      | Diana Sovetova      |                      | MPU-6050 |
| 2   | Nurkhan Berikboluly | Zhandaulet Kuan       | Bibol Assetbek      | Yernur Kenzhaly      | GPS      |
| 3   | Kamila Nurzhanova   | Siti Nur Dania        | Marjona Meliboyeva  |                      | DS18B20  |
| 4   | Amir Sakhipkereyev  | Anuar Akimbekov       | Miras Nametov       |                      | MPU-6050 |
| 5   | Bakhtiyar Yesbolsyn | Ruslan Nagimov        | Aitzhan Kadyrov     | Gulnur Meirimkhanova | CO       |
| 6   | Turar Turakbay      | Arlan Nauryzgali      | Kamila Tokubayeva   | Islam Mukhambetkali  | DHT11    |
| 7   | Adil Molzhigitov    | Yerassyl Gaziz        | Yeradil Tleugazyyev | Alen Kazhymukan      | GPS      |
| 8   | Sultan Yershuak     | Bekzat Torekhan       | Miras Tuleubayev    |                      | MPU-6050 |
| 9   | Dauren Nabi         | Ayan Saparali         |                     |                      | DS18B20  |
| 12  | Adil Tatiyev        | Alua Dosbergenova     |                     |                      | CO       |
| 13  | Tagir Zamaletdinov  | Botakoz Toleugaliyeva | Korkemay Oserbay    |                      | DHT11    |

## Code

Two programs, one per board, each a single file with a `MODE` constant at the top ("dv" or "flood", see [Task.md section VII](Task.md#vii-task-5-flooding-baseline)):

- `root.py`: the root, ID 0. In `"dv"` mode it advertises hop 0 with parent 0 and logs every unique data packet it receives. In `"flood"` mode it only receives.
- `Team-5-CO/node.py`: a non-root device with the CO sensor. It advertises, picks a parent, forwards for others and sends its own samples. `DEVICE_ID` and `MODE` are set at the top (our team ID is 5).
- `keys.py`: the keychain from [above](#shared-psk-and-keychain), copied to every board next to the program.

Both programs have comments that point to the Task.md section each part implements. `MODE` and `RUN_ID` must be identical on all boards. Both use the rx thread for receiving and one worker thread for sending, so the radio never has two callers ([Task.md section VI](Task.md#vi-task-4-threaded-operation)).

### Reading the root output

```
[  83s] DELIVERED  node 5   seq 12    SMOKE (code 4)  smoke=0 (not detected)
[  90s] STATS      delivered=9 (node 5: 9) | adverts heard=18 sent=18 | dropped: none
```

`DELIVERED` appears once per unique packet that passed decryption, the CRC check and validation ([Task.md, reception](Task.md#reception)). It shows the origin node, its sequence number, the sensor and the decoded reading with units, as the task asks for the root log ([required delivery behavior](Task.md#required-delivery-behavior)). The number in brackets is seconds since the root started. `STATS` is printed every 10 s: unique packets per node, advertisements received and sent, and every drop reason that is not zero (`crc`, `dup`, `dest`, `code`, `payload`, `run`, ...).

### Putting it on a board

1. Stop whatever is running (Ctrl-C in Thonny). If the board has an old lab `main.py` that connects to Wi-Fi, rename it, otherwise it blocks the REPL and fights ESP-NOW for the radio.
2. Save `keys.py` on the board.
3. Save `root.py` or `node.py` on the board as `main.py`.
4. Press RST. The program starts by itself on every reset or power-up.

Don't delete `ctr.txt`, it keeps the tx counter across reboots ([run ID](#run-id)).

### CO sensor (FC-22, MQ-9)

Sensor code 4 ([PACKETS.md, sensor codes](PACKETS.md#sensor-codes-and-payloads)). The module has four pins: VCC to VIN (5 V), GND to GND, DO to GPIO 14. AO is not used, so leave it unconnected. The heater smells a bit and gets hot for the first few minutes, that is normal.

DO is pulled up to the module supply, so on 5 V it can be above the 3.3 V the ESP32 pin is rated for. Measure it with no gas present, and if it reads about 5 V put a divider in (10k from DO to the pin, 20k from the pin to GND).

DO goes low when CO is above the level set by the potentiometer. Low is reported as `smoke=1`, high as `smoke=0`. This is our digital-output interpretation, which [Task.md section II](Task.md#ii-equipment-and-configuration) asks us to document.

### Tested so far

DV mode with one root and one node, boards next to each other. The node found the root, chose it as parent (`ROUTE parent 255->0 hop 255->1`), the RSSI estimate updated, and the root logged every packet once with no drops. The first sample, generated before the route existed, is dropped as "no route", as [Task.md section V](Task.md#v-task-3-data-generation-and-delivery) asks.

Not tested yet: flooding mode, two-hop paths, parent switching, neighbor expiry, and the other four sensor types.
