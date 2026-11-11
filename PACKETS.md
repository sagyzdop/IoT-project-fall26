# Packet formats

## Where the lengths come from

The lengths were not inferred from the sensors. Task.md section V fixes the data header (7 bytes) and the payload of each sensor code, so that one root can decode every team's packets. The payload sizes follow from the field types: a float32 is 4 bytes, so DHT11 (two floats) is 8, DS18 and the accelerometer (one float) are 4, GPS (two floats) is 8, and smoke is a single byte. Section V.A adds the encryption envelope, and the field widths of that envelope are the team's choice from the README.

A packet has two layers:

```
on air:  | outer header (9 B, clear) | ciphertext = AES-CTR( inner packet | CRC-32 (4 B) ) |
```

The outer header and CRC add 13 bytes to every packet, whatever the sensor.

## Inner data packet (Task.md section V)

The 7-byte header is the same for every sensor. Only `sensor_code` and the payload change.

| field       | bytes | meaning                                                      |
| ----------- | ----- | ------------------------------------------------------------ |
| ptype       | 1     | 2 for data                                                   |
| dest_id     | 1     | DV: next hop (the parent). Flooding: 255                     |
| origin_id   | 1     | node that took the measurement, never changed while relaying |
| seq         | 2     | device-wide sample counter, shared by all sensor types       |
| ttl         | 1     | transmissions still allowed                                  |
| sensor_code | 1     | 1 to 5, selects the payload below                            |

Python format string for the header: `">BBBHBB"`. The `>` means big-endian (network byte order), which the task requires on every device.

## Payload per sensor

| code | sensor     | node IDs | payload                            | format  | payload bytes | inner bytes | on air bytes |
| ---- | ---------- | -------- | ---------------------------------- | ------- | ------------- | ----------- | ------------ |
| 1    | DHT11      | 6, 13    | temperature (C), humidity (%)      | `">ff"` | 8             | 15          | 28           |
| 2    | DS18B20    | 3, 9     | temperature (C)                    | `">f"`  | 4             | 11          | 24           |
| 3    | GPS        | 2, 7     | longitude, latitude (degrees)      | `">ff"` | 8             | 15          | 28           |
| 4    | CO / smoke | 5, 12    | 0 not detected, 1 detected         | `">B"`  | 1             | 8           | 21           |
| 5    | MPU-6050   | 1, 4, 8  | mean acceleration magnitude (m/s2) | `">f"`  | 4             | 11          | 24           |

The advertisement (type 0) has no sensor part: `ptype | dest_id | src_id | hop | parent_id`, 5 bytes, format `">BBBBB"`, 18 bytes on air. The code 4 row is our node's payload length: 1 byte payload, 8 inner, 21 on air.

## Packing on the node

```python
header  = struct.pack(">BBBHBB", 2, dest, ME, seq, ttl, code)
inner   = header + payload
packet  = env.seal(ME, inner)      # appends CRC-32, encrypts with AES-CTR, adds the outer header
radio.send(packet)
```

Only the payload line differs per sensor:

```python
payload = struct.pack(">ff", temp_c, humidity)          # code 1, DHT11
payload = struct.pack(">f",  temp_c)                    # code 2, DS18B20
payload = struct.pack(">ff", longitude, latitude)       # code 3, GPS (west and south negative)
payload = bytes([1 if co_detected else 0])              # code 4, CO / smoke
payload = struct.pack(">f",  accel_mean)                # code 5, MPU-6050
```

For the MPU-6050, sample all three axes over a full 2 s window, convert to m/s2, take the magnitude `sqrt(x*x + y*y + z*z)` of each sample, and send the mean of those magnitudes (gravity included).

In `node.py` this happens in `generate()`: it builds `payload` (line `payload = bytes([read_co()])`) and calls `send_data(ME, seq, 4, ...)`, where 4 is the sensor code. Another sensor needs its own read function, its payload line and its code in those two places.

## Unpacking at the root

The root does this in `root_process()`, in this order:

1. `env.open()` checks length, version, key ID and run ID, decrypts, and compares the CRC-32 over the outer header plus the decrypted inner packet. A mismatch is counted as `drop_crc` and the packet is thrown away.
2. `check_data()` reads the 7-byte header with `struct.unpack(">BBBHBB", ...)`, looks the sensor code up in `SENSORS` (codes 0 and 6 to 255 are rejected as `drop_code`), and requires the payload length to match exactly (`drop_payload`).
3. `decode_payload()` unpacks the payload with the format from the table (for example `struct.unpack(">ff", payload)` for DHT11) and rejects values that are not valid: non-finite floats, humidity outside 0 to 100, longitude outside -180 to 180, latitude outside -90 to 90, smoke other than 0 or 1, negative acceleration.
4. `root_process()` checks the destination (0 in DV mode, 255 in flooding), drops a repeated `(origin_id, seq)`, then prints the line.

## Size

These formats are mostly fixed by the task, so there is little room to shrink them. What we chose to keep them small:

- CRC-32 (4 bytes) instead of SHA-256 (32 bytes) saves 28 bytes per packet.
- A 2-byte `run_id` instead of 4 saves 2 bytes.
- AES-CTR needs no padding. The ECB mode from Lab 6 pads each message up to a multiple of 16 bytes, so up to 15 bytes extra.
- Smoke is one byte, not a float.

What cannot change without breaking the spec: the 7-byte header and the float32 payloads. The DHT11 only measures whole degrees and whole percent, but the format still carries them as float32. The largest packet is 28 bytes, far below the 250-byte ESP-NOW limit. The fixed 13 bytes of envelope are the main cost: 13 of the 21 bytes of a smoke packet, 13 of 28 for DHT11 or GPS.
