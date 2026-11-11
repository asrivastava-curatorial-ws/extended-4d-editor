# Finding rain, fog, fire, bubbles, snow and scent scenes in a video

When a video is analysed, the Editor also looks at **what the picture shows and what the sound
track contains**, and drafts effect cues from it: rain where it rains, a fog burst where a foggy
scene begins, warm air where there is fire, bubbles under water, snow in the snow, a water spray
at a splash, a scent in a forest, at the sea, among flowers.

This document says how that works, what was measured, what was not, and where the models come
from. In one sentence: it is a helpful first draft made on your own computer, not a judgement to
rely on — look at every lane it fills.

## 1. What happens during the analysis

**Picture.** Once a second, one frame goes to a picture model. For 180° and 360° video only the
forward view is used (the middle 100° of the picture, where a visitor mostly looks; the left eye
of a stereo video). The model turns the picture into a list of 512 numbers that describes its
content; the same was done beforehand for short descriptions such as *"heavy rain falling"*,
*"thick fog"*, *"a big fire with flames"*, and for descriptions of what looks similar but is
something else (*"a waterfall"* and *"a person holding an umbrella"* for rain, *"a blurry photo"*
for fog, *"a sunset"* and *"orange and red lights"* for fire). A concept's score is how much
better the picture matches the concept's own descriptions than the look-alikes and 28 neutral
descriptions (*"a photo of an object"*, *"a city street on a clear day"*, …) — a number from 0 to 1.

| Concept | Used for | Number of descriptions: concept / look-alikes |
|---|---|---|
| rain | rain | 6 / 6 |
| fog (fog, mist, smoke) | fog | 7 / 5 |
| fire (flames, explosions, lava) | heat | 9 / 6 |
| bubbles (bubbles, underwater) | bubbles | 6 / 6 |
| snow (falling snow, snow landscapes) | snow | 10 / 7 |
| splash (water splashing) | water spray | 5 / 6 |
| forest, sea, flowers | scent cartridges 1, 2, 3 | 5 / 6, 7 / 5, 5 / 6 |

**Sound.** If the video has a sound track, a second model listens to it in four-second windows,
one second apart, and gives the likelihood of 527 kinds of sound. Eight groups of them are kept:
rain, thunder, fire (fire, crackle), explosion, splash (splash, spray, waterfall, surf), sea,
bubbling, wind.

**From scores to cues.** A concept counts as *seen* from a score of 0.8. Rain and fire also count
when they are half seen (0.35) and clearly heard (0.25). Short flickers are ignored: a passage
must last 1 to 5 seconds, depending on the effect, and passages a few seconds apart are joined.
Passage ends within 0.6 s of a scene cut are moved onto the cut. The table of what is drafted
from what is in [USER_GUIDE.md](USER_GUIDE.md#effects).

Sound alone never drafts an effect. A ride video with loud wind noise "sounds like" sea and
splashing to the sound model for half a minute; without a picture that agrees, nothing is drafted
from it.

What was seen and heard is shown in the Editor's *Effects* tab (*Found in the video*), printed by
`fourdvr extract generate --effects suggest`, and saved in the project, so effects can be
suggested again without a new analysis.

Nothing else is derived from the pictures: no people, faces, places or texts are looked for, and
nothing is stored except the scores of the concepts above, one row a second.

## 2. Everything stays on the computer

Both models are files inside the AppImage and run on the processor with ONNX Runtime. There is no
network access, no account, no service; the video, its pictures and its sound never leave the
machine. The analysis works with the network cable pulled.

One detail deserves saying. Newer versions of ONNX Runtime (1.29 and later) report usage events
to their maker by default. This program requires an older version that has no such function
(the AppImage contains 1.28.0) and sets the documented off-switch besides; an analysis traced with
`strace` makes no network system call ([APPIMAGE.md](APPIMAGE.md) §4).

Scene detection is the smaller part of an analysis. On the two-core test machine the picture
model needed 0.12 s per picture (one picture is taken per second of video) and the sound model
15 s for the sound of a 127-second video — together about half a minute of the four minutes the
whole analysis of that video took. *Effects ▸ Look for … scenes when analysing* (or `--no-scenes`
on the command line) switches it off; the motion analysis is bit for bit the same either way.

## 3. What was measured

**The picture model does what its original does.** The converted model was compared with an
independent implementation of the same network: identical results (cosine similarity ≥ 0.9995 on
test pictures). As a sanity check of the whole conversion it classified 250 photographs of 250
different everyday objects by name: 60 % right at the first guess, 80 % within five guesses (the
model's authors report about 63 % at the first guess on the complete test set of 50 000 pictures).

**The concept scores on still pictures.** 1 052 sample pictures were scored: 52 photographs of
rain, fog and fire from public test sets, and 1 000 photographs of 1 000 different object classes.
With "seen" at 0.8:

| Concept | Found | False alarms among the other pictures |
|---|---|---|
| rain | 7 of 9 photographs of real rain | 1 of 1 032 |
| fog | 11 of 12 fog photographs | 0 |
| fire | 6 of 6 fire photographs | 0 |
| bubbles / underwater | 3 of 3 (diver, coral reef, bubble) | 0 |
| snow | every high-scoring picture really showed snow (ski, snowmobile, snowplough, dog sled, …) | 0 |

The numbers are small. They show that the method works and that false alarms are rare on
everyday pictures; they are not an accuracy figure for films.

**The sound model does what its original does.** On the 13 test recordings that come with it, the
Editor's own processing gives the same likelihoods as the model's reference program (largest
difference 0.0000).

**A test film.** A film of 85 seconds was cut from 37 of the same sample pictures — about ten
seconds each of rain, fire, fog, snow and underwater pictures, with passages of neutral pictures
between them (goose, pizza, library, castle …), no sound — and run through the complete program
with the real models. It tests the path from pictures to cues, not new material:

| Passage in the film | Drafted |
|---|---|
| rain, 8–18 s (5 pictures) | rain cue 10–16 s: three of the five pictures were recognised |
| fire, 26–36 s | heat cue 26–36 s |
| fog, 44–54 s | fog burst at 44 s |
| snow, 60–70 s | snow cue 60–70 s |
| underwater, 70–79 s | bubbles cue 70–77.5 s |
| 36 s of neutral pictures | nothing |

**A real ride video** (127 s, 360°, roller coaster in sunshine): nothing seen, nothing drafted
from scenes — correct. Heard: "sea" 26 s, "splash" 23 s, "wind" 6 s (the wind noise of the ride),
which by design drafts nothing.

## 4. What was not measured, and what to expect

* **No measurement on real films with known content.** How often a rainy scene in a film is
  missed, or an effect drafted where it does not belong, is not known. Stills are easier than
  film: motion blur, darkness, fast cuts, rain that is only visible as wet streets, computer
  graphics and animation will all lower the hit rate.
* **One picture a second.** Something that lasts less than a second or two is not found
  (lightning is found another way, from the brightness of every frame).
* **Fire means fire in the picture** — a campfire, an explosion, lava, a burning building, but
  also a large fireplace. Warm air on the visitor at every candle would be wrong; the limits keep
  it short, your review decides.
* **Fog includes smoke and mist**, snow includes snow landscapes without snowfall, bubbles include
  any underwater scene. Delete what you do not want.
* **360° video:** only the forward view is looked at. Rain behind the visitor is not found.
* **The models were trained on pictures and sounds from the Internet** and carry that material's
  slant: what is common there is recognised better than what is rare.
* **The same video gives the same result on the same computer.** Between different processors the
  scores can differ in the last digits, which can move a borderline passage by a second.

The effects themselves are protected by the limits whatever is drafted
([EFFECTS_GUIDE.md](EFFECTS_GUIDE.md) §5): a wrongly drafted fog cue is still a short fog burst.

## 5. The models

| | Picture model | Sound model |
|---|---|---|
| File | `scene.onnx` (176 MB), `concepts.npz` (the descriptions, already turned into numbers) | `sound.onnx` (27 MB), `sound-labels.csv` |
| Network | CLIP ViT-B/32, image part only | Zipformer, small |
| Trained by | the OpenCLIP authors, on LAION-400M | the k2 / icefall project, on AudioSet |
| Published at | github.com/mlfoundations/open_clip, release `v0.2-weights`, `vit_b_32-quickgelu-laion400m_e32-46683a32.pt` | github.com/k2-fsa/sherpa-onnx, release `audio-tagging-models`, `sherpa-onnx-zipformer-small-audio-tagging-2024-04-15` |
| Licence stated there | MIT (the repository's licence) | Apache-2.0 (the model's README) |
| Changes | weights unchanged, stored as 16-bit numbers; only the image part is included | none (the published 8-bit version) |

The licence texts are in the file `LICENSES.txt` next to the models and in
[THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md).

Where the Editor looks for the models, in this order: the folder named by the environment variable
`FOURDVR_MODELS`; the folder `models` inside the installed `fourdvr_extractor` package (where the
AppImage has them); `~/.local/share/extended-4d-editor/models`. Without models the analysis works
as before and the *Effects* tab says that scenes cannot be looked for.

### Building the models yourself

The model files are not in the source package (203 MB); the AppImage build makes them, and so can
you:

```bash
pip install ".[studio]" onnx                         # onnx is needed for building only
python packaging/models/build_models.py --out ~/.local/share/extended-4d-editor/models
```

The script downloads five files from GitHub (720 MB the first time; kept in
`packaging/models/cache`), checks each against a SHA-256 written in the script, reads the picture
model's weights with a reader that accepts nothing but numbers (the published file is a Python
"pickle", which could otherwise run code), builds `scene.onnx`, computes the descriptions'
numbers, verifies the result against its own reference implementation, and writes the files with
a manifest. PyTorch is not needed. `--offline` uses the cache only.

### Other descriptions, other concepts

`packaging/models/concepts.json` holds the descriptions. Changing a description, or the
look-alikes of a concept, and building again changes how that concept is scored — try it on your
own material before trusting it. A new concept needs a rule in
`src/fourdvr_extractor/suggest.py` as well to become an effect.
