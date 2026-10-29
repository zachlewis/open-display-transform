# CLFBake

Bake any chain of Nuke nodes into a CLF (or CTF, or Cinespace) with a
parametric log shaper and enough metadata to restore, or rebuild, the nodes
that made it. Built for OpenDRT, works with anything between the two bookend
nodes. Needs Nuke 15.1 or later; the CLF is written with Python's `xml.etree`.

## Using it

Paste `CLFBake.nk` into a script. It holds two Group nodes:

```
image -> CLFShaper -> [ OpenDRT, looks, anything ] -> CLFGenerate -> viewer
```

**CLFShaper** sits at the head and passes the image through. While baking it
outputs a CMSTestPattern decoded through the inverse of the shaper, so the
lattice is laid out in log and fed to the chain as linear.

| Knob | Meaning |
|---|---|
| shaper | Log encoding for the lattice, as OCIO `LogCameraTransform` parameters from [ociogen](https://github.com/jedypod/ociogen): **Filmlight T-LogE** (default, as in the released OpenDRT OCIO config), T-Log, ACEScct, LogC3, LogC4, S-Log3, Log3G10, DaVinci Intermediate, and Jed's parametric **XLog**. |
| cube size | 48 (default) matches the released config. 65 halves the error at 2.5x the file size. |
| code range | Shaper codes the lattice spans, `[0, 1]` by default. T-LogE tops out at scene 512; a higher top covers brighter content. Written as an exact `Range` node. |
| show pattern | Preview the shaped test pattern. |

**CLFGenerate** sits at the tail.

| Knob | Meaning |
|---|---|
| file | Output path. A directory, or empty, names the file automatically (below). |
| format | `clf` (default), `ctf` or `csp`. An explicit extension on the path wins. |
| interpolation | Recorded in the LUT3D. Tetrahedral by default. |
| output | `display codes` (default), or a linear display target: `lin_ciexyzd65_display`, `lin_rec709_display`, `lin_p3d65_display` (below). |
| ctf target | `2.1` (default) or `2.4`. 2.4 writes the PQ decode as the `PQ_TO_Lin` FixedFunction; 2.1 writes a half-domain LUT1D that OCIO 2.2 reads. |
| levels | `full` (default), or `legal in` / `legal out` / both, written as unclamped `noClamp` Range nodes first and last, so deleting one converts the file. Legal in needs log input. Tagged `LL`/`LE`/`EL`/`EE` on the output ID; the CICP range flag follows. |
| log input | Omit the Log node: the file takes the shaper's log codes, the way the released config consumes its cubes. |
| embed node script | Also store the baked nodes as a `.nk` snippet so From CLF can rebuild them. |
| domain | **preset**: use CLFShaper as set. **auto**: probe the chain and, if it still changes above the shaper's top, extend the code range to cover it with a stop of margin. The curve stays the same. |
| Generate / Restore / From CLF | Bake; apply the knob values in `file` to the selected (or upstream) nodes; rebuild the baked nodes from the script in `file`. |

**OpenDRT.nk** carries a "CLF Helpers" group beside the DCTL Helpers: a file
knob, **Bake CLF** (T-LogE at 48³, the released config's recipe), **Restore
CLF**, and a `params` field with **Apply Params**. Its CLFs and the standalone
pair's are interchangeable.

### Domain

T-LogE covers scene −0.0046 to 512, which holds camera footage. OpenDRT keeps
rendering above that (a 100-nit SDR bake still changes up to about 7,000, a
1000-nit PQ bake to 14,000), so CG or other very bright content clips in a
preset bake. `auto` finds the top with seven exposure ramps (neutral, three
chromatic, three negative) and extends the code range: on the test chain
a 200-nit bake extends T-LogE to code 1.19 (scene 5,350) and matches Nuke
within 1e-2 up to there. The cost is a coarser lattice below 512, 1.5 to 1.7x
the error, so `preset` stays the default.

### Linear outputs

The cube is always baked code value to code value; linear targets are exact
decode nodes after it:

```
[Log] [Range] LUT3D (display code values)
  Exponent basicFwd 2.2 / 2.4 / 2.6                power EOTFs
  LUT1D halfDomain  or  FixedFunction PQ_TO_Lin     PQ
  Matrix  container RGB -> XYZ -> [Bradford render white -> D65] -> target   (omitted when identity)
```

- **Scale** is the CIF display scale: code 1.0 decodes to 1.0 for the power
  EOTFs, PQ to 1.0 = 100 cd/m², a Linear EOTF to peak/100. DCDM X'Y'Z' goes
  through `g26_xyzd65_display`, so the file is a bare 2.6 Exponent with the
  48 cd/m² white at Y = 1.
- **White.** OpenDRT encodes every P3 container with the P3-D65 matrix, so all
  decode with it. The render white (the LUT's white corner, decoded) is
  Bradford-adapted to D65, so a white patch is D65 in the target whatever the
  container or creative white, OCIO's convention. Greys drift slightly with
  intensity (OpenDRT's creative white limit, `cwp_lm`); that is the look.
- **HLG** is refused. The Nuke node applies 20.5% of the BT.2100 inverse OOTF
  (`mix 0.205`), the DCTL all of it; they differ by up to 0.118 code, and the
  partial operator has no standard inverse.

### Cinespace

`csp` is baked by OCIO's `Baker` from the CLF, through an in-memory config with
the shaper as a 4096-point prelut, so prelut and cube match the CLF exactly.
Caveat: OCIO's own csp reader resamples the prelut uniformly over its input
range, which loses shadows (3e-3 at T-LogE's 512 top, 0.26 code with an
extended top). For OCIO, use a **log input** csp with the T-LogE in the config
and a tetrahedral FileTransform, which equals the CLF exactly, or use the CLF.

### File names

```
OpenDRT-v<version>_<look>_<condition>__<input id>__to__<output id>__<size>.clf
OpenDRT-v1.1.0_Standard_video100__lin_egamut2__to__g24_rec709__48.clf
OpenDRT-v1.1.0_LowContrast-6f137a09_cinema1000__lin_ap1__to__pq_rec2020-p3lim-dw148__48-to28k.clf
```

`<look>` is the look preset when the node still matches it exactly, else the
tonescale preset plus an 8-hex hash of the look parameters, else the hash.
IDs drop their namespace and `_scene`/`_display`, modifiers joined by `-`.
`<size>` gains the shaper's short name when it is not T-LogE (`xlog48`) and
`-to<top>`, the scene value the extended code range reaches. Other chains are named
`<node>__<shaper><size>`.

### Parameter string

Every recognised OpenDRT node is written as one parseable string, the CLF's
`id`:

```
opendrt1_1{look_name{'Standard'}input{in_gamut{'Filmlight E-Gamut2'}}tonescale{tn_Lp{100.0}…}…}
clfbake{shaper{'tloge'}size{48}interpolation{'tetrahedral'}embedded{1}…}
```

Numbers bare, booleans `0`/`1`, strings single-quoted. Paste one into the
OpenDRT node's `params` and press Apply Params; groups are optional, so
`opendrt1_1{tn_Lp{1000}eotf{'PQ'}}` works too.

## What the CLF contains

```xml
<ProcessList compCLFversion="3" id="opendrt1_1{…}clfbake{…}" name="OpenDRT">
  <Info>
    <CLFBake version="2">
      <Shaper preset="Filmlight T-LogE" shortname="tloge" cube_size="48" code_min="0" code_max="1" …/>
      <Nodes>…every node, every user-facing knob, in toScript form…</Nodes>
      <Model name="OpenDRT" version="1.1.0" look="Standard">…one element per module…</Model>
      <Mastering>
        <DisplayColorVolume gamut="Rec.709" maxLuminance="100.0" minLuminance="0.0" red=… white=…/>
        <Luminance peak="100.0" greyNominal="10" greyMeasured="11.5" diffuseWhiteMeasured="50.5"/>
        <ViewingEnvironment surround="dim" condition="video100"/>
        <CreativeWhite whitepoint="D65" limit="0.25"/>
        <CICP primaries="1" transfer="1" matrix="0" fullRange="true"/>
      </Mastering>
      <Interop input="lin_ap1_scene" output="g24_rec709_display" namespace="opendrt-v1.1"/>
      <Script>…the baked nodes, for From CLF…</Script>
    </CLFBake>
  </Info>
  <Log style="cameraLinToLog">…</Log>            <!-- absent with log input -->
  <LUT3D interpolation="tetrahedral"><Array dim="48 48 48 3">…</Array></LUT3D>
</ProcessList>
```

- **Model**: attributes are OpenDRT's own parameter ids, the ones the Nuke
  node and the DCTL share, so a DCTL preset or JSON can be generated from it;
  enumerations also carry `<id>_index`.
- **Mastering**: ST 2086 display volume, grey and diffuse white measured from
  the live chain, surround and a FilmLight-style condition token (`cinema48`,
  `video100`, `videowide1000`), creative white, and H.273 code points.
- **Interop**: ASWF Color Interop IDs. Published CIF IDs bare when exact,
  otherwise `opendrt-v1.1:<base>|<modifiers>` with a fallback to the published
  base. Modifiers: `p3lim` (Rec.2020 limited to P3), `d60sim` and friends
  (creative white), `dw<N>` (measured diffuse white nits, HDR), `clamp0`
  (clamp off), `EE`/`LL`… (levels), `codes0to1.19` (log input code range).
- Two outputs earn their published ID by a conforming Matrix after the LUT3D,
  recorded as `outputConform`: `g26_xyzd65_display` (DCDM's 48/52.37
  headroom) and `ocio:lin_ciexyzd65_display` (EOTF Linear, X'Y'Z', peak/100).

Not written yet: the SMPTE CLF broadcast-profile metadata, for when a bake is
not video levels in and out.

## Using the CLF in OCIO

```yaml
- !<ColorSpace>
    name: OpenDRT Rec.1886
    from_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {src: reference, dst: Filmlight - E-Gamut2 - Linear}
        - !<FileTransform> {src: OpenDRT-v1.1.0_Standard_video100__lin_egamut2__to__g24_rec709__48.clf}
```

With log input, `dst` is the T-LogE colorspace instead, as in the released
config.

## Testing

Headless, with PyOpenColorIO (bundled with Nuke) as the independent reader:

```
cd utilities/nuke
nuke -t test_clfbake.py                          # ~2 minutes; run on 15.1 too
CLFBAKE_PRESET=8 nuke -t test_clfbake.py         # another shaper preset (env: nuke -t eats numbers)
nuke -t sweep_clfbake.py                         # every look, tonescale and display preset, ~7 minutes
CLFBAKE_DOMAIN=auto nuke -t sweep_clfbake.py     # the same with the auto code range
```

Each check prints `PASS`/`FAIL` with its error and tolerance; the run ends
with `ALL PASS` or exits non-zero. The harness proves: lattice points match
Nuke exactly (2e-4); off-lattice error is interpolation only (3e-2);
log input, CTF and csp evaluate the same; Restore, Apply Params and From CLF
reproduce every knob, and a rebuilt chain re-bakes byte-identical; `auto`
keeps a covering shaper and otherwise extends it, matching Nuke above the old
top; linear decodes match an independent implementation and a white decodes
to D65 within 1e-4; levels nodes convert exactly; and the metadata is right
for four display configurations. The sweep gates the 95th percentile at 3e-2
and the maximum at 6e-2: with purity compression off the chain changes too
fast across a lattice cell outside the display gamut (3.3e-2 at 48³).

## Updating

`build_clfbake.py` is the source of truth. Run in Nuke, it writes `CLFBake.nk`
(never hand-edit it) and re-splices the CLF Helpers block into
`display-transforms/opendrt/OpenDRT.nk`, between `clf_grp` and `endGroup_clf`,
leaving the rest of Jed's file untouched. Rerun it whenever OpenDRT.nk changes
upstream.

```
nuke -t build_clfbake.py && nuke -t test_clfbake.py
git diff --stat                                  # OpenDRT.nk: the helper block only
```

- **Shaper presets**: the `PRESETS` table, before the XLog entry, with numbers
  from ociogen or the OCIO source.
- **Button Python**: `COMMON` is prepended to `GENERATE`, `RESTORE`,
  `FROM_CLF` and the OpenDRT buttons; `script()` wraps each in a function.
- **Metadata**: `write_clf`, `describe_opendrt`, `interop_ids`, and the
  `OPENDRT_MODEL` / `PUBLISHED_*` / `CICP_*` tables. A new OpenDRT parameter
  needs its id in `OPENDRT_MODEL`. Bump `CLFBake version` on structural change.

## Design notes

Measured on the OpenDRT test chain, so they need not be re-tried:

- A linear-light cube doubles the error; so does storing BT.709 codes and
  unwrapping after. sRGB, log or gamma stores, and gamma-wrapped PQ, are 1.2
  to 1.6x worse than storing the display codes. The error left is
  interpolation, not quantisation.
- A fitted XLog and an extended T-LogE range tie above 512; the range keeps one
  encoding. A curvature-equalised shaper behind a T-LogE remap buys about 15%
  at p99 and loses beyond that. 65³ is the lever.
- Moving OpenDRT's clamp after the cube does not help the worst case; it is
  out-of-gamut nonlinearity, which purity compression handles.

## Nuke gotchas

- PyScript knobs run in `__main__`; the builder wraps every script in a
  function so names don't leak.
- `nuke.thisParent()` is `None` after a `with nuke.thisParent():` block in the
  same script; derive the parent from `fullName()`.
- `nuke.nodePaste` and `nuke.nodes.*` connect to the current selection.
- In `nuke -t`, sourceless constant images can sample black on 15.1, and
  knob-driven re-renders can be stale: sample each node once first.
- OCIO's CLF reader collapses whitespace in `<Info>`; Restore parses with
  `xml.etree`.
- `.cube` is red-fastest, the CLF `LUT3D` blue-fastest.
- OpenDRT's parameters are mostly Link knobs: `node[name]` resolves them,
  `node.knobs()[name]` does not.
