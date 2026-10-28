# CLFBake

Bake any chain of Nuke nodes into a CLF (or CTF) with a parametric log shaper
and enough metadata to restore, or rebuild, the nodes that made it. Built for
OpenDRT but works with anything between the two bookend nodes. Needs Nuke
15.1 or later and nothing else: the CLF is written with Python's `xml.etree`,
no PyOpenColorIO at bake time.

Contents: [Using it](#using-it) · [What the CLF contains](#what-the-clf-contains)
· [Using the CLF in OCIO](#using-the-clf-in-ocio) · [Testing](#testing)
· [Updating](#updating) · [Nuke gotchas](#nuke-gotchas)

## Using it

Paste `CLFBake.nk` into a Nuke script. It contains two Group nodes:

```
image -> CLFShaper -> [ OpenDRT, looks, anything ] -> CLFGenerate -> viewer
```

**CLFShaper** sits at the head of the chain and normally passes the image
through. While baking it outputs a CMSTestPattern decoded through the inverse
of the chosen shaper, so the LUT lattice is laid out in log and fed to the
chain as linear.

| Knob | Meaning |
|---|---|
| shaper | Log encoding used to lay out the lattice. Presets are the OCIO `LogCameraTransform` parameters from [ociogen](https://github.com/jedypod/ociogen): **Filmlight T-LogE** (default, what the released OpenDRT OCIO config bakes through), T-Log, ACEScct, Arri LogC3, Arri LogC4, Sony S-Log3, Red Log3G10, DaVinci Intermediate, and **XLog**, Jed's parametric log, with its w0 / o / x0 / y0 / k. |
| cube size | Lattice size. 48 matches the released OpenDRT config. 65 halves the worst-case error at 2.5x the file size; the CLF format allows up to 128. |
| show pattern | Preview the shaped test pattern. CLFGenerate toggles this itself while baking. |
| Params tab | Read-only: the XLog derivation and the exact `cameraLinToLog` parameters the CLF will carry. |

**CLFGenerate** sits at the tail.

| Knob | Meaning |
|---|---|
| file | Output path. A `.ctf` extension writes CTF instead of CLF. A directory, or empty (the script's directory), names the file automatically; see below. |
| format | `clf` (default), `ctf`, or `csp` for automatically named files. An explicit extension on a file path wins. |
| interpolation | Recorded in the LUT3D node. Tetrahedral by default. |
| structure | `baked` (default): the LUT3D holds everything up to output code values. `view + display`: the OCIO split, see below. |
| ctf target | `2.1` (default) or `2.4`: which OCIO features a CTF may use. 2.4 writes PQ as the `Lin_TO_PQ` FixedFunction; 2.1 writes a half-domain LUT1D, which Nuke 15 and 16 (OCIO 2.2 and 2.3) can read. |
| levels | `full` (default): 0 to 1 code values in and out, which is what OCIO and the CIF display IDs expect. `legal in`, `legal out`, `legal in and out`: 64 to 940 on 1023, written as **unclamped `noClamp` Range nodes first and/or last in the CLF**, so deleting the first or the last node turns a legal-legal file into full-legal or legal-full exactly. Legal in applies to log input only; a linear input has no video range. The levels appear as an `LL` / `LE` / `EL` / `EE` modifier on the output ID and the CICP `fullRange` flag follows. OCIO applies these nodes, it does not optimise them away, which is why `full` stays the default. |
| log input | Omit the Log node and declare the shaper's log encoding as the input, the way the released OpenDRT config consumes its cubes. Off by default: the shaper is embedded and the CLF takes linear input. |
| embed node script | Also store the baked nodes as a Nuke script in the CLF so From CLF can rebuild them. On by default; the structured metadata is written regardless. |
| domain | **preset**: use the shaper as set. **auto**: probe the chain and replace the preset with a fitted XLog only if the chain still changes beyond the preset's range. **fit**: always fit. See below. |
| Generate | Bake. The status line reports the file, node count, lattice size, and what auto/fit decided. |
| Restore | Apply the knob values recorded in `file` to the selected nodes of the same class, or to the upstream chain when nothing is selected. |
| From CLF | Rebuild the nodes that made `file` from the script it carries, wire them between the upstream CLFShaper and this node, and restore the shaper and log-input setting. Baking again reproduces the file. |

**Domain fitting.** Shapers are static: T-LogE covers −0.0046 to 512 linear
for every input, which is Jed's trade-off in his released config. `auto` and
`fit` probe the chain with seven exposure ramps (neutral, three chromatic,
three single-channel negatives) and find where the output stops changing by
more than a 10-bit step. The fitted XLog gets w0 one stop above that top and k
reaching the negative floor, with the linear toe kept at least five stops under
grey so shadow resolution is not traded away. `auto` keeps the preset when it
already covers the chain, or when the only shortfall is an unbounded negative
floor, which the guard could not satisfy anyway. A fitted XLog is written to
the CLF exactly, like any preset. Measured on OpenDRT with inputs up to 8192
linear, T-LogE clamps at 3.0e-2 worst case regardless of size; the fitted XLog
reaches 1.6e-2 at 48³ and 5.3e-3 at 65³.

**Structure `view + display`.** The OCIO decomposition of a display pipeline
into a view transform that ends in CIE XYZ D65 and a display colorspace that
encodes it. CLFGenerate bakes the OpenDRT node with its EOTF temporarily set
to Linear and its clamp off (restored afterwards), so the LUT3D holds the
render as display-linear RGB in the chosen container, with the node's own
gamut limiting and creative-white handling intact. Then native nodes follow:

```
[Log shaper] [Range codes] LUT3D
  Matrix   container RGB -> CIE XYZ D65, 1.0 = 100 cd/m2      <- ocio:lin_ciexyzd65_display
  Matrix   XYZ -> container RGB (scaled for the encoding)
  Range    the node's 0..1 clamp, at the right level for the encoding
  Exponent basicRev 2.2 / 2.4 / 2.6              (power EOTFs)
  LUT1D halfDomain  or  FixedFunction Lin_TO_PQ   (PQ; FixedFunction with ctf target 2.4)
  FixedFunction Rec2100SurroundRev 1.2 + LUT1D halfDomain OETF   (HLG, CTF only)
  [Matrix conform] [Range legal]
```

The two matrices cancel numerically; the point is the explicit XYZ tap, so a
consumer can take the LUT3D plus the first Matrix as a view transform into
OCIO's `cie_xyz_d65_interchange` and let its own display colorspaces encode.
That is valid within the same container: the baked XYZ still carries the
container's gamut clip and white handling. With EOTF Linear the display part is
omitted and the file ends at XYZ. HLG in this structure is the *standard*
BT.2100 inverse OOTF (system gamma 1.2) plus OETF, which is what the OpenDRT
DCTL does; the Nuke node applies its inverse OOTF through an Expression with
`mix 0.205`, that is 20.5% of it, so the Nuke node's own HLG output differs
from the standard, and from the DCTL, by up to 0.118 code on the test chain.
The harness therefore checks the HLG CTF against the standard formula applied
to the node's display-linear output, not against the node's HLG output. HLG
needs CTF because the inverse OOTF has channel crosstalk and CLF has no
FixedFunction.

Precision: GenerateLUT writes six decimals, which quantises display-linear
values to 1e-6, and a 2.4 power turns that into 1.4e-3 near black. CLFGenerate
therefore bakes display-linear through an internal gain of 2^14 and divides it
out in the first Matrix, exact in float; measured lattice error is 6e-6 for SDR
and 7e-5 for PQ through the half-domain LUT1D. The CTF 2.4 `Lin_TO_PQ` file
agrees with the half-domain CLF to 3.4e-4 under OpenColorIO 2.5.

**Cinespace output.** `csp` is derived from the CLF, never baked separately:
the CLF is written (to a temporary file when only the csp is wanted), then an
in-memory OCIO config is built with a `reference` colorspace that has no
transforms, a `baked` colorspace that applies the CLF `from_scene_reference`,
and a `shaper` NamedTransform holding the same LogCameraTransform in the
lin-to-log direction, and OCIO's `Baker` writes `cinespace` with
`setShaperSpace('shaper')`, `setShaperSize(4096)` and the CLF's cube size.
The Baker samples the shaper's inverse at 4096 uniform codes for the prelut, so
its points sit exactly on the parametric curve, and evaluates the CLF at the
lattice, so the cube equals the CLF's (a conforming scale included). The id,
interop IDs, viewing condition and peak luminance go in the metadata block.
With `log input` there is no shaper. This is the one place the nodes import
PyOpenColorIO, which Nuke bundles. One caveat for OpenColorIO as a consumer: its `.csp` reader resamples the prelut into a uniform 65,536-entry
1D LUT across the prelut's input range, which for a 512-linear top means one
sample per 0.0078 linear and a visible loss in the shadows (measured 3e-3 at
−5 stops, 1e-2 worst case on the harness image). Hosts that interpolate the
prelut on its given points reproduce the shaper to about 1e-7. Give OCIO the
CLF.

**Code range for log input.** A log encoding's own range, codes 0 to 1, tops
out low in linear for some encodings: S-Log3 at 38, LogC3 at 55, ACEScct at
223, LogC4 at 470, T-LogE at 512. A float pipeline carrying those encodings
can hold highlights above code 1.0, and a 1000-nit OpenDRT is still rendering
them. In `log input` mode the encoding is the contract, so instead of swapping
the shaper the lattice spans a code range `[code min, code max]` on
CLFShaper, written to the CLF as an exact `Range` node ahead of the LUT3D and
to the Baker's shaper for csp. `auto` and `fit` set it from the probe: the top
with one stop of margin, never below 1.0 so the encoding's own range is always
covered, capped at 1.5; the floor only when the probe found it bounded. The
input interop ID gains `|codes0to1.19`-style modifier and the file name shows
it. The cost is a proportionally coarser lattice, 19% for a 1.19 top.

Why not an OCIO `AllocationTransform` as the csp shaper: it would bake, but a
lg2 allocation is a different curve from T-LogE, so the Baker would resample
the CLF's lattice through tetrahedral interpolation rather than landing on it.
The NamedTransform carries the exact shaper, and the cube stays equal to the
CLF's. One measured fact about that NamedTransform: with a NamedTransform as
the Baker's shaper space, OCIO 2.2 and 2.3 apply its *forward* transform as
shaper-to-input, so the forward transform must be the decode direction
(codes to linear). Some references state the opposite; the harness checks
the prelut points against the CLF's Log node, which settles it.

**Automatic file names.** When `file` is a directory (or empty), the name is
explicit and deterministic, never random:

```
OpenDRT-v<version>_<look>_<condition>__<input id>__to__<output id>__<size>.clf
OpenDRT-v1.1.0_Standard_video100__lin_egamut2__to__g24_rec709__48.clf
OpenDRT-v1.1.0_LowContrast-6f137a09_cinema1000__lin_ap1__to__pq_rec2020-p3lim-dw148__48.clf
```

Three sections separated by double underscores: the head (version, look,
condition, single underscores; looks may contain hyphens), the input and
output IDs joined by `__to__`, and the size. Interop IDs appear without
namespace or `_scene`/`_display` and without the shaper code-range modifier,
other modifiers joined with hyphens; the full IDs are in the metadata. The
size is prefixed by the shaper's short name when it is not the default T-LogE
(`xlog48`): a T-LogE and a fitted XLog bake of the same node are different
LUTs and neither is better in general (T-LogE is more accurate inside its 512
domain, XLog beyond it), so they must not overwrite each other. `<size>` is the lattice size, prefixed by the shaper's short name
when it is not the default T-LogE (`xlog48`), so bakes with different shapers
do not overwrite each other.

`<look>` is the look preset's name when the node still matches one of its own
look presets exactly (checked against the pulldown's `knobs` commands); else
the tonescale preset's name plus an 8-hex hash when only the tonescale still
matches; else the hash alone. The hash is a UUID5 of the look parameters only,
so changing the display does not change it, and the same look hashes the same
from the standalone pair and the OpenDRT button. Interop IDs lose their
namespace and use `-` for `|`; the condition carries surround and peak nits.
For chains the tool does not recognise the name is `<node>__<shaper><size>`.

**Parameter string.** Every recognised OpenDRT node is also written as one
parseable string, in the FilmLight manner of a DRT whose parameters travel in
its name:

```
opendrt1_1{look_name{'Standard'}input{in_gamut{'Filmlight E-Gamut2'}}tonescale{tn_Lp{100.0}tn_Lg{10.0}
tn_gb{0.13}tn_con{1.66}tn_sh{0.5}tn_toe{0.003}tn_off{0.005}tn_su{'dim'}}highcontrast{…}…
output{eotf{'2.4 Power - Rec.1886'}display_gamut{'Rec.709'}clamp{1}display_name{'…'}}}
clfbake{shaper{'tloge'}size{48}interpolation{'tetrahedral'}embedded{1}}
```

Groups are the Model modules, leaf keys are the parameter ids, numbers are
bare, booleans `0`/`1`, strings and enumeration labels single-quoted with `\'`
and `\\` escapes, lists comma-separated. It is the CLF's `ProcessList id`, it
is repeated in `<Info><CLFBake><Params>`, and the OpenDRT node's CLF Helpers
carry it in a `params` field with **Apply Params**: paste a string from any
CLF's id, or type one, and the node takes those values. The parser flattens
groups, so a hand-written `opendrt1_1{tn_Lp{1000}eotf{'PQ'}}` is valid too.

**OpenDRT.nk** also carries a "CLF Helpers" group next to Jed's DCTL Helpers:
a file knob, **Bake CLF**, **Restore CLF**, and the `params` field with
**Apply Params**. Bake CLF wraps the node in the
T-LogE shaper at 48³, tetrahedral, exactly the recipe of the released OpenDRT
config, and writes the same metadata, so its CLFs restore and rebuild through
CLFGenerate and vice versa. Use the standalone pair for another shaper, size,
domain fitting, or a chain of nodes.

## What the CLF contains

```xml
<ProcessList compCLFversion="3" id="OpenDRT__tloge" name="OpenDRT">
  <InputDescriptor>Linear (Filmlight T-LogE shaper)</InputDescriptor>
  <OutputDescriptor>OpenDRT</OutputDescriptor>
  <Info>
    <CLFBake version="2">
      <Shaper preset="Filmlight T-LogE" shortname="tloge" cube_size="48" embedded="true"
              gs=".." go=".." ls=".." lo=".." lb=".." sl=".."  w0=".." o=".." x0=".." y0=".." k=".."/>
      <Nodes>                                      <!-- generic: every node, every user-facing knob -->
        <Node class="Group" name="OpenDRT">
          <Knob name="tn_Lp">100</Knob>            <!-- Nuke toScript form, restored with fromScript -->
          ...
        </Node>
      </Nodes>
      <Model name="OpenDRT" version="1.1.0" node="OpenDRT" look="Standard">   <!-- recognised models only -->
        <Input in_gamut="Filmlight E-Gamut2" in_gamut_index="10"/>
        <Tonescale tn_Lp="100.0" tn_Lg="10.0" tn_gb="0.13" tn_con="1.66" tn_sh="0.5" tn_toe="0.003"
                   tn_off="0.005" tn_su="dim" tn_su_index="1"/>
        <HighContrast .../> <LowContrast .../> <CreativeWhite cwp="D65" cwp_index="2" cwp_lm="0.25"/>
        <RenderSpace .../> <PurityCompress .../> <PuritySoftclip .../> <MidPurity .../>
        <Brilliance .../> <PostBrilliance .../> <HueContrast .../> <HueShiftRGB .../> <HueShiftCMY .../>
        <Output eotf="2.4 Power - Rec.1886" eotf_index="2" display_gamut="Rec.709" display_gamut_index="0"
                clamp="true" display_name="Rec.1886 - 2.4 Power / Rec.709"/>
      </Model>
      <Mastering>
        <DisplayColorVolume gamut="Rec.709" maxLuminance="100.0" minLuminance="0.0"
                            red="0.64 0.33" green="0.3 0.6" blue="0.15 0.06" white="0.3127 0.329"
                            contentWhite="0.3127 0.329"/>   <!-- white: container; contentWhite: creative white -->
        <Luminance peak="100.0" greyNominal="10" greyMeasured="11.5" diffuseWhiteMeasured="50.5"
                   measuredFrom="chain output for neutral 0.18 and 1.0, decoded with the output EOTF"/>
        <ViewingEnvironment surround="dim" condition="video100" ambient="video, 5 nit surround"/>
        <CreativeWhite whitepoint="D65" limit="0.25" note="..."/>
        <CICP primaries="1" transfer="1" matrix="0" fullRange="true" note="..."/>
      </Mastering>
      <Interop input="lin_ap1_scene" output="g24_rec709_display" namespace="opendrt-v1.1"
               [inputFallback=".." outputFallback=".." outputConform=".."]/>
      <Script>...the baked nodes as a .nk snippet (nuke.nodeCopy), for From CLF; optional...</Script>
      <Generator nuke="16.0v4" date="..." script="..."/>
    </CLFBake>
  </Info>
  <Log inBitDepth="32f" outBitDepth="32f" style="cameraLinToLog">   <!-- absent with "log input" -->
    <LogParams base="2" linSideSlope=".." linSideOffset=".." logSideSlope=".." logSideOffset=".."
               linSideBreak=".." linearSlope=".."/>
  </Log>
  <LUT3D inBitDepth="32f" outBitDepth="32f" interpolation="tetrahedral">
    <Array dim="48 48 48 3">...</Array>                             <!-- blue index fastest -->
  </LUT3D>
</ProcessList>
```

`gs go ls lo lb sl` are the OCIO LogCameraTransform parameters (logSideSlope,
logSideOffset, linSideSlope, linSideOffset, linSideBreak, linearSlope), base 2.
Knob values are Nuke's `toScript` form and are restored with `fromScript`, so
enumerations, arrays, expressions and Link knobs round-trip. The bake path is
never recorded. A CTF differs only in the root attributes (`version="2"`).

**Model** is written for nodes the tool recognises (today: OpenDRT v1.x, by
its knob set). Attribute names are the model's own parameter identifiers, the
same ones the Nuke node, the DCTL's `DEFINE_UI_PARAMS(_<id>, …)` and Jed's
preset strings use, so a Nuke preset, a DCTL preset block or a JSON document
can be generated from it mechanically; enumerations carry the label and
`<id>_index` for integer targets. Values are as set, not defaults-only.

**Mastering** describes the output the transform was authored for:
`DisplayColorVolume` in SMPTE ST 2086 terms (CIE xy primaries and white,
min/max luminance; Rec.2020 (P3 Limited) also states `limitedTo`),
`Luminance` with `greyNominal` (grey luminance with HDR grey boost, before
surround compensation) and `greyMeasured` / `diffuseWhiteMeasured`, which are
sampled from the live chain during the bake for neutral 0.18 and 1.0 and
decoded through the output EOTF (so they are the truth, and dim/bright surround
shows up as grey above nominal), the viewing `surround`, the `CreativeWhite`,
and the ITU-T H.273 `CICP` code points for the output encoding where they are
unambiguous (transfer 1 for 2.4-power Rec.1886 is the broadcast convention).

**Interop** carries ASWF Color Interop IDs for input and output, always. A
published CIF ID is used bare when the encoding is exactly the published one.
Anything OpenDRT-defined, or a published base with modifiers, is an
organisation ID in the `opendrt-v<major.minor>:` namespace per CIF Rec 03,
with `inputFallback` / `outputFallback` naming the published base where one
exists. Modifiers are appended to the base with `|`:

| Modifier | Meaning |
|---|---|
| `p3lim` | Rec.2020 container limited to P3 |
| `d60sim`, `d55sim`, `d93sim`, … | creative white other than D65. OpenDRT adapts the creative white to D65 for the render and D65 to the output container, so a non-D65 container (P3-DCI, P3-D60, X'Y'Z') carries D65 content white unmodified; that is the implied case. |
| `dw<N>` | diffuse white (scene 1.0) luminance in cd/m², measured, HDR encodings only |
| `clamp0` | the clamp knob is off, output may leave [0,1] |
| `EE` | log code values in, full range out (log input mode) |

Examples: `g24_rec709_display`; `opendrt-v1.1:pq_rec2020_display|p3lim|dw148`
(fallback `pq_rec2020_display`); `opendrt-v1.1:g26_p3dci_display`;
`opendrt-v1.1:lin_egamut2_scene` for a camera-gamut input;
`opendrt-v1.1:tloge_egamut2_scene` for a log-input bake.

Two outputs earn their published ID by a conforming `Matrix` appended after
the `LUT3D`, recorded as `outputConform` on the Interop element and noted in
the OutputDescriptor:

- DCDM `g26_xyzd65_display`: OpenDRT encodes X'Y'Z' with D65 content white and
  peak at code 1.0; the published definition has the 52.37 cd/m² headroom with
  48 cd/m² white, so the codes are scaled by (48/52.37)^(1/2.6). Exact, since
  the power function is separable.
- `ocio:lin_ciexyzd65_display`, OpenColorIO's CIE XYZ D65 display interchange,
  which puts 1.0 at 100 cd/m²: set OpenDRT to EOTF Linear and gamut X'Y'Z'
  and the CLF is scaled by peak luminance / 100. The result plugs into any OCIO
  config as a view transform (scene reference to `cie_xyz_d65_interchange`)
  with the config's own display colorspaces doing the encoding.

`pq_xyzd65_display` needs no conform: PQ already encodes absolute nits.
Display P3 at pure 2.2 power is `opendrt-v1.1:g22_p3d65_display` because the
published Display P3 ID means the sRGB piecewise curve.

`ViewingEnvironment` also carries a FilmLight-style `condition` token built
from surround class, gamut and peak: `cinema48`, `video100`, `office100`,
`videowide1000`. Dark surround is `cinema` and implies a wide gamut, so
`cinema1000` rather than `cinemawide1000`. The exact FilmLight vocabulary was
not checked against Baselight; the scheme follows the examples given.

Not yet written: the SMPTE CLF broadcast profile metadata ("CLF4"), which
OpenColorIO implements. That belongs to bakes whose input or output is not
video levels, and is deferred until there is such a bake.

## Using the CLF in OCIO

Any OCIO 2 config can reference the file directly, for example as a display
colorspace in a config whose reference is linear E-Gamut2:

```yaml
- !<ColorSpace>
    name: OpenDRT Rec.1886
    from_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {src: reference, dst: Filmlight - E-Gamut2 - Linear}
        - !<FileTransform> {src: OpenDRT__tloge.clf}
```

With **log input** the FileTransform's input is the shaper's log encoding
instead (`dst: Filmlight - E-Gamut2 - T-LogE`), which is how the released
OpenDRT config is laid out.

## Testing

`test_clfbake.py` is a headless harness. It builds `image -> CLFShaper ->
OpenDRT -> CLFGenerate` from the files in this repo, bakes, and uses
PyOpenColorIO (the one bundled with Nuke) as an independent reader to apply
the CLF and compare against Nuke's own render of the same pixels. Nuke is
launched through rez:

```
cd utilities/nuke
rez-env nuke fxphd -- nuke -t test_clfbake.py                     # current Nuke (16)
rez-env nuke-15.1 fxphd -- nuke -t test_clfbake.py                # the 15.1 / OCIO 2.2 floor
CLFBAKE_PRESET=8 rez-env nuke fxphd -- nuke -t test_clfbake.py    # another shaper preset
```

The preset index is an environment variable because headless Nuke treats a
trailing number on the command line as a frame range. A run takes one to two
minutes, prints one `PASS`/`FAIL` line per check with the measured error and
its tolerance, ends with `ALL PASS` or `FAILED: [...]`, and exits non-zero on
failure. The CLFs it wrote stay in the temp directory it prints.

| Check | What it proves | Gate |
|---|---|---|
| lattice: CLF vs Nuke | On the lattice points the CLF reproduces Nuke exactly, so the Nuke inverse shaper and the CLF Log node are the same curve and the cube transposition is right. | 2e-4 |
| image: CLF vs Nuke | Off-lattice error on an image spanning the shaper's domain is 3D LUT interpolation error only. | 3e-2 |
| log input: CLF vs Nuke | The log-input variant applied to log-encoded pixels matches, and carries no Log node. | 3e-2 |
| restore | Restore into a fresh OpenDRT makes every recorded knob (77) equal the original's. | exact |
| ctf: CLF vs CTF | The CTF loads and evaluates identically. | 1e-6 |
| opendrt button: CLF vs Nuke, restore | The thin buttons on OpenDRT.nk bake and restore the same way, leave the node's input as it was, and record no bake path. | as above |
| auto keeps a sufficient preset | `domain: auto` leaves a preset alone when it already covers the chain. | exact |
| fit: CLF vs Nuke, above top, below floor | A fitted XLog matches within interpolation error, clamping beyond the fitted top costs nothing, and the floor is within tolerance. | 3e-2 / 5e-3 / 3e-2 |
| rebake from CLF (standalone, opendrt button) | From CLF on a fresh bookend pair rebuilds the chain; baking it again gives identical LogParams, all 110,592 LUT rows, and all recorded knobs. | byte-identical |
| auto file names, parameter string, opendrt button auto name | A modified node names by hash, an untouched one by look preset, a tonescale-only change by tonescale name plus hash, and the hash survives a peak-luminance change; the CLF id parses and, applied to a fresh node through Apply Params, reproduces every recorded knob; the OpenDRT button names and hashes identically. | exact |
| csp: structure, vs Nuke, prelut points on the Log node, cube vs CLF lattice | A `.csp` bake has the header, three 4096-point preluts and the id in its metadata; evaluates against Nuke within tolerance through OCIO's reader; every prelut point lies on the CLF's Log node to 1e-5; the Baker's cube equals the CLF's lattice to 1e-5 after reordering. | 3e-2 / 1e-5 / 1e-5 |
| view+display: SDR structure, lattice exact, vs Nuke; PQ CLF structure, lattice, vs Nuke; PQ CTF 2.4 structure; HLG CTF structure, vs standard BT.2100 | The node sequence is exactly as documented for each encoding; the node's EOTF and clamp knobs are restored after the bake; SDR is exact on the lattice and PQ within half-float; both match Nuke off the lattice; the CTF 2.4 file carries version 2.4 and `Lin_TO_PQ` (evaluated only when the running OCIO is 2.4+); the HLG CTF matches the standard formula on the node's display-linear output, and the Nuke node's deviation from the standard is printed. | 2e-4 / 1e-3 / 3e-2 |
| levels LL structure, LL / EL / LE, LL csp | A legal-in-legal-out bake has noClamp Range nodes first and last, `levels{'LL'}` in the id, `-LL` in the name and CICP fullRange false; legal codes in give legal codes out; removing the first Range gives full in and legal out, removing the last gives legal in and full out; the csp carries the same. | 3e-2 |
| log input code range (CLF and csp) | A log-input `fit` bake on a chain that responds above code 1.0 sets code max above 1 (1.188 on the test chain), writes a Range node and no Log node, tags the input ID with `codes0to…`, and both the CLF and the csp match Nuke on an image reaching above code 1.0. | exact / 3e-2 |
| metadata (sdr rec1886, pq rec2020 1000nit, dcdm xyz 2.6, linear xyz 1000nit) | Every Model attribute is a real knob id with the node's value; DisplayColorVolume, measured luminances, CICP, Interop IDs with namespace, modifiers and fallback, and the viewing condition token are right for four configurations; in the dark-surround PQ case measured grey is within 3% of nominal, which also validates the PQ decode; the two conformed outputs match the node times the recorded scale; `embed node script` off omits the script. | exact / 3% / 3e-2 |

Reference numbers on the OpenDRT test chain at 48³: lattice 1e-6, image
7.8e-3, fitted image 9.9e-3. If a change moves these by more than a little,
find out why before loosening a tolerance.

`sweep_clfbake.py` is the end-to-end sweep: it drives OpenDRT through its own
preset pulldowns, every look preset, every tonescale preset and every display
preset, plus module-toggle combinations (all modules off, contrast modules on,
brilliance on with hue shift off, creative white D55 with clamp off, log
input), 35 configurations in all. For each it bakes, checks the CLF against
Nuke's render (95th-percentile error within 3e-2 and max within 6e-2, because
a hard clip with purity compression off puts one lattice cell across a
discontinuity: 3.3e-2 at 48³ falling to 1.8e-2 at 97³, median zero), restores
into a fresh node and compares every recorded knob, re-bakes the restored node
and requires LogParams and the LUT3D array to be byte-identical, and checks the
Model block and the expected Interop output ID with its modifiers. About seven
minutes per Nuke version.

```
rez-env nuke fxphd -- nuke -t sweep_clfbake.py
rez-test nuke_opendrt sweep
```

## Updating

Files in this directory:

| File | Role |
|---|---|
| `build_clfbake.py` | The source of truth. Holds the preset table, the shared button Python, and the node layouts. Run inside Nuke, it writes `CLFBake.nk` and splices the CLF Helpers group into `../../display-transforms/opendrt/OpenDRT.nk`. |
| `CLFBake.nk` | Generated. Do not hand-edit; Nuke escapes the button scripts on the way out. |
| `test_clfbake.py` | The harness above. |

The CLF Helpers block in `OpenDRT.nk` is likewise generated: the builder
replaces everything between `addUserKnob {20 clf_grp` and `addUserKnob {20
endGroup_clf` (or inserts it after the DCTL Helpers' `endGroup_1` if absent),
so Jed's file otherwise stays untouched. When OpenDRT.nk is updated from
upstream, rerun the builder to re-splice.

Workflow for any change:

```
rez-env nuke fxphd -- nuke -t build_clfbake.py
rez-env nuke fxphd -- nuke -t test_clfbake.py
rez-env nuke-15.1 fxphd -- nuke -t test_clfbake.py
CLFBAKE_PRESET=8 rez-env nuke fxphd -- nuke -t test_clfbake.py
git diff --stat                      # OpenDRT.nk should change by the helper block only
```

Commit the builder, the regenerated `.nk`, and `OpenDRT.nk` together.

Where things live in the builder:

- **Presets**: the `PRESETS` table, one tuple per shaper with its OCIO
  LogCameraTransform parameters in base 2 (`linearSlope` `None` means OCIO's
  C1-continuous default, computed by `auto_slope`). To add one, append it
  *before* the XLog entry and take the numbers from ociogen's
  `colorspaces.yaml` or the OCIO source, never from a fit. The preset menu, the
  nested-ternary knob expressions on the Params tab, the shortname list, and
  the XLog index are all derived from the table.
- **Defaults**: cube size and the default preset index in `build_shaper`, the
  `domain` menu order in `build_generate`, the OpenDRT button's fixed recipe in
  `OPENDRT_SHAPER`.
- **Button Python**: `COMMON` (knob serialisation, cube reading, CLF writing,
  node script capture, restore) is prepended to each of `GENERATE`, `RESTORE`,
  `FROM_CLF`, `OPENDRT_BAKE` and `OPENDRT_RESTORE`, and `script()` wraps each
  in a function. `GENERATE` also holds the domain probe and the XLog solve.
- **Metadata schema**: `write_clf`. Bump `CLFBake version` if you change the
  element structure, and keep `read_bake`/`restore_apply` reading old files.
- **Model and mastering metadata**: `OPENDRT_MODEL` (module → parameter ids),
  the `XY`, `CWP_XY`, `GAMUT_SHORT`, `EOTF_SHORT`, `CONTAINER_SHORT`,
  `PUBLISHED_IN`, `PUBLISHED_OUT` (published IDs and their conforming scales),
  `CICP_*` tables, `to_nits` (output EOTF decode), `interop_ids` (namespace,
  modifiers, fallback, conform) and `describe_opendrt`. A new OpenDRT version that adds a
  parameter needs its id added to the module table; a new recognised model
  needs an `is_<model>` test and a describer called from `write_clf`.
- **Probe ramps**: the `ProbeRamp` expressions in `build_shaper` and the
  thresholds (`/ 1023.0`, the 5-stop toe guard `2.0 ** -5`) in `GENERATE`.

## Deploying with rez

`utilities/rez/nuke_opendrt/` is a rez package definition in the same idiom as
the other `nuke_*` packages: `plugin_for = ['nuke']`, `requires =
['nuke-15.1+']`, and one `NUKE_PATH.append` to its `nuke/` directory. The
build (`build.py`, pure Python) copies `display-transforms/opendrt`,
`utilities/nuke`, the DCTLs, LICENSE and README out of the checkout and places
`OpenDRT.nk` and `CLFBake.nk` under `nuke/ToolSets/OpenDRT/`, so they appear
in Nuke under **Nodes > ToolSets > OpenDRT** with no menu Python at all. The
version follows the local date convention (`YY.M.D.N`).

```
cd utilities/rez/nuke_opendrt
rez-build -i                       # build + install to the local packages path
rez-env nuke_opendrt fxphd -- nuke # Nuke with the ToolSets on NUKE_PATH
rez-test nuke_opendrt              # runs test_clfbake.py in the resolved Nuke
```

`rez-test` is the same harness as above, executed from the installed copy, so
a package is verified in the environment artists will actually resolve. To
release facility-wide use `rez-release` from a clean, tagged checkout; that is
deliberate and irreversible, so it is not scripted here. The DCTLs are shipped
for reference only; Resolve still needs them copied into its LUT folder.

## Nuke gotchas

Things that cost time while building this, so you don't pay twice:

- PyScript knobs execute in `__main__`. Any name a button assigns leaks into
  the session (and into a harness). The builder wraps every script in a
  function for that reason.
- `nuke.thisParent()` returns `None` once a `with nuke.thisParent():` block in
  the same script has exited. Derive the parent from a node's `fullName()`
  instead, as `chain_script` does.
- `nuke.nodePaste` and `nuke.nodes.*` connect the new node to the current
  selection, and paste inserts it after a selected node. Deselect first, or
  you rewire the chain you are testing.
- In `nuke -t`, an Expression or Constant whose only content is a negative
  constant can sample as black in some sequences on 15.1. Probe with small
  images, not single pixels.
- `nuke -t script.py 4` treats the `4` as a frame range; pass arguments through
  the environment.
- OCIO's own CLF reader collapses whitespace inside `<Info>` text. Restore
  parses with `xml.etree` instead, which keeps the knob script verbatim.
- `.cube` files are red-fastest; the CLF `LUT3D` array is blue-fastest.
  `read_cube` transposes. The lattice check would catch a regression.
- OpenDRT exposes most of its parameters as Link knobs. `node[name]` resolves
  to the linked target, `node.knobs()[name]` does not; `toScript`/`fromScript`
  on the resolved knob is what makes restore generic.
