"""4D effects for the 4D VR Theatre: effect tracks on the programme timeline, venue fixture patch,
DMX output (sACN / Art-Net / USB), safety limits and bass (subwoofer / bass-shaker) audio.

Effects are optional everywhere: a programme without an effects section, or a controller without an
effects patch, runs motion exactly as before.

Modules
    model   effects document (tracks, cues, curves, bass track), validation, evaluation
    embed   reading/writing the document inside a .motion file header or as a standalone .fx.json
    dmx     DMX universes and outputs (sACN E1.31, Art-Net, Enttec DMX USB Pro), packet codecs, receiver
    patch   venue configuration: outputs, fixtures, limits, audio
    engine  evaluation of the tracks into DMX frames with gating and safety limits
    audio   bass track encoding/resampling, vibration synthesis, position-locked playback
"""

from .model import (
    BUILTIN_TYPES,
    FORMAT_NAME,
    FORMAT_VERSION,
    BassTrack,
    Cue,
    EffectsDocument,
    EffectsError,
    Track,
    effect_type,
)

__all__ = [
    "BUILTIN_TYPES",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "BassTrack",
    "Cue",
    "EffectsDocument",
    "EffectsError",
    "Track",
    "effect_type",
]
