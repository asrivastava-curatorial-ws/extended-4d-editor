# Effects document format — `4D-VR-THEATRE-EFFECTS` 1.0

The effects of a programme: what happens when, independent of any venue. A document lives in one
of two places:

* **inside a `.motion` file**, as the value of the header key `effects`
  ([MOTION_FORMAT.md](MOTION_FORMAT.md)). This is how programmes are distributed: the Control
  Server stores and delivers the `.motion` file as one unit, and every reader of `.motion` 1.0
  ignores header keys it does not know — a file with effects is still a valid motion file for
  software that has never heard of effects. A file without the key is a motion-only programme.
* **standalone**, as a JSON file (`.fx.json`), for exchange between programmes and people
  (`fourdvr fx extract` / `embed`, the Editor's *Import* / *Export effects*).

The reference implementation is `src/fourdvr_fx/model.py` (reading, checking, writing) and
`embed.py`.

## Document

```json
{
 "format": "4D-VR-THEATRE-EFFECTS",
 "version": "1.0.0",
 "duration_s": 126.72,
 "meta": {"generator": "Extended-4D-Editor", "generator_version": "3.2.0", "suggested": ["wind"]},
 "tracks": [
  {"id": "wind",   "type": "wind",   "label": "Wind",
   "points": [[0, 0], [4.1, 0], [4.8, 0.26], [126.72, 0]]},
  {"id": "water",  "type": "water",  "label": "Water spray",
   "cues": [{"t": 31, "d": 0.4, "level": 1}]},
  {"id": "fog",    "type": "fog",    "label": "Fog",
   "cues": [{"t": 70, "d": 8, "level": 1, "fade_in": 0.5, "fade_out": 1, "label": "tunnel"}]},
  {"id": "scent",  "type": "scent",  "label": "Scent", "params": {"slot": 2},
   "cues": [{"t": 100, "d": 4, "level": 1, "params": {"slot": 2}}]},
  {"id": "strobe", "type": "strobe", "label": "Strobe / lightning",
   "cues": [{"t": 55.2, "d": 1, "level": 1, "params": {"rate_hz": 3}}]},
  {"id": "dmx",    "type": "dmx",    "label": "House lights", "universe": 3, "channel": 40,
   "points": [[0, 1], [5, 0], [126.72, 0]]}
 ],
 "bass": {"encoding": "s16le-zlib-base64", "sample_rate_hz": 2000, "sample_count": 253440,
          "gain_db": 0, "cutoff_hz": 120, "source": "sound track (stereo), low-pass 120 Hz, level +9.3 dB",
          "sha256": "…", "data": "eNoBUACv/wAAMATqB8UKcQy+…"}
}
```

| Key | Type | Meaning |
|---|---|---|
| `format` | string | exactly `4D-VR-THEATRE-EFFECTS` |
| `version` | string | `major.minor.patch`. A reader accepts every `1.x.y` and ignores keys it does not know; it refuses another major version |
| `duration_s` | number | length of the programme in seconds (0.01 … 86400); equals the motion's duration |
| `meta` | object | free information (generator, which tracks were suggested automatically); not interpreted |
| `tracks` | array | at most 64 tracks, unique `id`s |
| `bass` | object | optional bass track (below) |

All times are seconds from the start of the programme, on the same timeline as the motion samples
and the video.

## Track

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | — | unique in the document; `^[a-z0-9][a-z0-9_-]{0,31}$` |
| `type` | string | — | effect type (same pattern). Built-in: `wind`, `water`, `rain`, `air`, `fog`, `heat`, `bubbles`, `snow`, `strobe`, `scent`, `vibration`, `dmx`. Any other name is a venue-defined effect: it is output where a venue patches a fixture with that `effect` name, and ignored elsewhere. (`rain`, `heat`, `bubbles` and `snow` are built in since Extended 4D Editor 3.0 — they have a label, a colour and default limits there; the format did not change, and a 2.0 reader treats them as venue-defined names) |
| `label` | string | type name | name shown in the editor |
| `enabled` | boolean | true | a disabled track is kept but not output |
| `cues` | array | `[]` | bursts (below), at most 20000 |
| `points` | array | `[]` | level curve: `[time, level]` pairs with level 0 … 1, sorted by time, at most 20000 |
| `params` | object | `{}` | default parameters for the track's cues (numbers), e.g. `{"slot": 2}` |
| `universe`, `channel` | integer | — | type `dmx` only: the DMX universe (≥ 1) and channel (1 … 512) the track drives |

**Level of a track at time *t*** = the larger of

* the curve: linear interpolation between neighbouring points; before the first point the first
  level, after the last point the last level; 0 for a track without points;
* every cue that is active at *t*.

Several enabled tracks of one type: the largest level wins.

## Cue

| Key | Type | Default | Meaning |
|---|---|---|---|
| `t` | number | — | start (≥ 0) |
| `d` | number | — | duration (> 0); the cue is active in [`t`, `t` + `d`) |
| `level` | number | 1 | 0 … 1 |
| `fade_in`, `fade_out` | number | 0 | seconds, inside the duration: the level rises linearly from 0 over `fade_in` and falls to 0 over the last `fade_out` (`fade_in` + `fade_out` ≤ `d`) |
| `params` | object | `{}` | parameters of this cue (numbers); they override the track's `params` |
| `label` | string | `""` | note shown on the cue |

Parameters of the built-in types:

| Type | Parameter | Meaning |
|---|---|---|
| `strobe` | `rate_hz` (0.5 … 25, default 3) | flashes per second asked for. The venue caps it. Flashes are counted from the start of the cue: the first flash is at `t`, so a cue shorter than one period is a single flash |
| `scent` | `slot` (1 … 16) | cartridge. A venue's scent fixtures react only to cues whose `slot` matches theirs |

When several cues of a type overlap, the parameters of the strongest one apply.

## Bass track

Low-frequency audio for subwoofers and bass shakers: mono, signed 16-bit little-endian PCM at a
low sample rate, compressed with zlib and encoded in base64.

| Key | Type | Meaning |
|---|---|---|
| `encoding` | string | exactly `s16le-zlib-base64` |
| `sample_rate_hz` | integer | 500 … 8000 (the Editor writes 2000; 1000 or 500 for very long programmes) |
| `sample_count` | integer | number of samples after decompression (at most four hours); a reader unpacks no more than this |
| `gain_db` | number | gain to apply at playback |
| `cutoff_hz` | number | low-pass frequency that was applied (information) |
| `source` | string | where it came from (information) |
| `sha256` | string | SHA-256 of the raw PCM bytes; a reader verifies it |
| `data` | string | base64(zlib(PCM)) |

Sample *n* belongs to programme time *n* / `sample_rate_hz`. The player resamples to the rate of
its sound device and keeps the output locked to the show timeline (it re-synchronises with a
short cross-fade if it drifts by more than 30 ms).

## Size

The `.motion` header may be at most 16 MiB. Curves and cues are small (a two-minute programme with
six tracks: a few kilobytes). The bass track dominates: about 5 kB per second at 2 kHz after
compression and base64 (two minutes ≈ 0.6 MB; twenty minutes ≈ 6 MB). Embedding is refused with
a clear message if the header would exceed the limit; lower the bass sample rate then.

## What a player must do

* Treat the document as optional: no `effects` key, or a venue without effect devices → motion only.
* A document that cannot be parsed (wrong `format`, unsupported major version, invalid track) is
  ignored as a whole and reported; **the motion of the file still plays**.
* Never output effects outside the playing state of the timeline (not before the start, not while
  paused, stopped or faulted).
* Apply the venue's limits (longest activation, pause, duty, total per run, flash-rate ceiling)
  after evaluating the document — the document itself guarantees nothing about what is safe for a
  particular device ([EFFECTS_GUIDE.md](EFFECTS_GUIDE.md) §5).
* Loop with the programme: time wraps at `duration_s` when the programme loops.

## Compatibility rules

* Minor versions (1.1, 1.2 …) may add keys, effect types and parameters; readers ignore unknown
  keys, treat unknown types as venue-defined level effects and ignore unknown parameters.
* A change that an old reader would misinterpret gets a new major version.
* Writers keep `duration_s` equal to the duration of the motion file they embed into.
