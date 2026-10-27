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
| file | Output path. A `.ctf` extension writes CTF instead of CLF. Empty: next to the script, named `<node>__<shaper>.clf`. |
| interpolation | Recorded in the LUT3D node. Tetrahedral by default. |
| log input | Omit the Log node and declare the shaper's log encoding as the input, the way the released OpenDRT config consumes its cubes. Off by default: the shaper is embedded and the CLF takes linear input. |
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

**OpenDRT.nk** also carries a "CLF Helpers" group next to Jed's DCTL Helpers:
a file knob, **Bake CLF** and **Restore CLF**. Bake CLF wraps the node in the
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
    <NukeBake version="1">
      <Shaper preset="Filmlight T-LogE" shortname="tloge" cube_size="48" embedded="true"
              gs=".." go=".." ls=".." lo=".." lb=".." sl=".."  w0=".." o=".." x0=".." y0=".." k=".."/>
      <Nodes>
        <Node class="Group" name="OpenDRT">
          <Knob name="tn_Lp">100</Knob>          <!-- every user-facing knob, via toScript -->
          ...
        </Node>
      </Nodes>
      <Script>...the baked nodes as a .nk snippet (nuke.nodeCopy), for From CLF...</Script>
      <Generator nuke="16.0v4" date="..." script="..."/>
    </NukeBake>
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

Reference numbers on the OpenDRT test chain at 48³: lattice 1e-6, image
7.8e-3, fitted image 9.9e-3. If a change moves these by more than a little,
find out why before loosening a tolerance.

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
- **Metadata schema**: `write_clf`. Bump `NukeBake version` if you change the
  element structure, and keep `read_bake`/`restore_apply` reading old files.
- **Probe ramps**: the `ProbeRamp` expressions in `build_shaper` and the
  thresholds (`/ 1023.0`, the 5-stop toe guard `2.0 ** -5`) in `GENERATE`.

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
