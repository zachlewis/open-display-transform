# CLFBake

Bake any chain of Nuke nodes into a CLF (or CTF) with a parametric log shaper
and enough metadata to restore the nodes' parameters later. Built for OpenDRT
but works with anything between the two bookend nodes.

Paste `CLFBake.nk` into a Nuke 15.1+ script. It contains two nodes:

```
image -> CLFShaper -> [ OpenDRT, looks, anything ] -> CLFGenerate -> viewer
```

- **CLFShaper** sits at the head. Normally it passes the image through. While
  baking it outputs a CMSTestPattern decoded through the inverse of the chosen
  shaper, so the LUT lattice is laid out in log and fed to the chain as linear.
- **CLFGenerate** sits at the tail. Press **Generate**: it bakes the chain
  through Nuke's own GenerateLUT, then writes a CLF whose process list is
  `Log cameraLinToLog` (the exact shaper, parametric) followed by the `LUT3D`.
  The CLF takes linear input. Tick **log input** to omit the Log node and
  declare the shaper's log encoding as the input instead, which is how the
  released OpenDRT OCIO config consumes its cubes.
- **Restore** reads a CLF written this way and applies the recorded knob values
  to the selected nodes of the same class (or to the upstream chain when nothing
  is selected).

Shapers are static: T-LogE covers −0.0046 to 512 linear for every input, which
is Jed's trade-off in the released config, and the default **domain** setting
on CLFGenerate, `preset`, uses it as is. `auto` probes the chain with exposure
ramps (neutral, chromatic, and single-channel negatives) and, only if the
output still changes by more than a 10-bit step beyond the preset's top or
floor, replaces the preset with a fitted XLog: w0 one stop above where the
output stops changing and k reaching the negative floor, with the linear toe
kept at least five stops under grey so shadow resolution is not traded away.
`fit` always does that. The status line reports what was found, and a fitted
XLog is still written to the CLF as an exact parametric Log node. Measured on
OpenDRT, the fit halves the worst-case error for inputs beyond T-LogE's range
at the cost of a little in-domain accuracy at 48³; 65³ recovers it.

The nodes need nothing beyond Nuke's own Python: the CLF is written with
`xml.etree`, so no PyOpenColorIO import happens at bake time.

**OpenDRT.nk** also carries a thin "CLF Helpers" group next to Jed's DCTL
Helpers: a file knob, **Bake CLF** and **Restore CLF**. Bake CLF wraps the node
in the T-LogE shaper at 48³ with tetrahedral interpolation, exactly the recipe
of the released OpenDRT OCIO config, and writes the same metadata, so a CLF
baked there can be restored by CLFGenerate and vice versa. Use the standalone
pair when you want another shaper, size, or to bake a chain of nodes.

Shaper presets are the OCIO `LogCameraTransform` parameters from
[ociogen](https://github.com/jedypod/ociogen): T-LogE (default, what the
OpenDRT OCIO config uses), T-Log, ACEScct, Arri LogC3, Arri LogC4, Sony S-Log3,
Red Log3G10, DaVinci Intermediate, and XLog with its w0 / o / x0 / y0 / k
parameters. Every preset is written to the CLF exactly, not fitted.

Metadata lives in the CLF `<Info>` element:

```xml
<NukeBake version="1">
  <Shaper preset="Filmlight T-LogE" cube_size="48" embedded="true" gs=... go=... ls=... lo=... lb=... sl=... />
  <Nodes>
    <Node class="Group" name="OpenDRT">
      <Knob name="tn_Lp">100</Knob>
      ...
    </Node>
  </Nodes>
  <Generator nuke="16.0v4" ocio="2.3.1" date="..." script="..."/>
</NukeBake>
```

Use the CLF from any OCIO config, for example as a display colorspace:

```yaml
- !<ColorSpace>
    name: OpenDRT Rec.1886
    from_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {src: reference, dst: Filmlight - E-Gamut2 - Linear}
        - !<FileTransform> {src: OpenDRT__tloge.clf}
```

## Development

`CLFBake.nk` and the CLF Helpers group in `OpenDRT.nk` are both generated from
`build_clfbake.py`, which holds the shared Python once: edit it and rebuild,
then run the headless verification, which bakes the OpenDRT node both ways and
compares the CLF applied with PyOpenColorIO (the independent reader) against
Nuke's own render:

```
rez-env nuke fxphd -- nuke -t build_clfbake.py
rez-env nuke fxphd -- nuke -t test_clfbake.py
CLFBAKE_PRESET=8 rez-env nuke fxphd -- nuke -t test_clfbake.py   # other shaper presets
```
