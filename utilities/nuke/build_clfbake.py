"""Build CLFBake.nk: the CLFShaper and CLFGenerate nodes.

Run inside Nuke so the knob scripts are escaped by Nuke itself:

    rez-env nuke fxphd -- nuke -t build_clfbake.py [out.nk]

The resulting .nk is self-contained: paste it into any Nuke 15.1+ script.
"""
import math
import os
import sys

import nuke

# ---------------------------------------------------------------------------
# Shaper presets. OCIO LogCameraTransform parameters, base 2, as used by Jed's
# ociogen (https://github.com/jedypod/ociogen) and therefore by the released
# OpenDRT OCIO config, whose LUTs are shaped through T-LogE.
#   (label, shortname, logSideSlope, logSideOffset, linSideSlope,
#    linSideOffset, linSideBreak, linearSlope or None for C1 auto)
# ---------------------------------------------------------------------------
PRESETS = [
    ("Filmlight T-LogE", "tloge", 0.055459048831, 0.500867797955, 1.0, 0.00487980885981, 0.0, None),
    ("Filmlight T-Log", "tlog", 0.0639976040320219, 0.552012656860665, 1.0, 0.00570482440424738, 0.0, None),
    ("ACEScct", "acescct", 0.0570776255707763, 0.554794520547945, 1.0, 0.0, 0.0078125, None),
    ("Arri LogC3", "logc3", 0.0744116046281795, 0.385537, 5.555556, 0.052272, 0.010591, None),
    ("Arri LogC4", "logc4", 0.0647954196341293, -0.295908392682586, 2231.82630906769, 64.0, -0.0180569961199113, None),
    ("Sony S-Log3", "slog3", 0.0769495052454847, 0.410557184750733, 5.26315789473684, 0.0526315789473684, 0.01125, 6.62194371177582),
    ("Red Log3G10", "log3g10", 0.067515609487509, 0.0, 155.975327, 2.55975327, -0.01, None),
    ("DaVinci Intermediate", "davinci_intermediate", 0.07329248, 0.51304736, 1.0, 0.0075, 0.00262409, None),
    ("XLog", "xlog", None, None, None, None, None, None),
]
XLOG = len(PRESETS) - 1


def table_expr(col, xlog_expr):
    """Nested ternary over the preset index for one LogCamera parameter."""
    expr = xlog_expr
    for i in range(XLOG - 1, -1, -1):
        p = PRESETS[i]
        v = p[col]
        if col == 7 and v is None:  # auto linear slope: derivative at the break
            v = p[2] * p[4] / ((p[4] * p[6] + p[5]) * math.log(2.0))
        expr = "preset==%d?%.17g:(%s)" % (i, v, expr)
    return expr


SHAPER_KNOBCHANGED = r"""n = nuke.thisNode()
if nuke.thisKnob().name() in ('preset', 'showPanel'):
    x = n['preset'].getValue() == %d
    for k in ('w0', 'o', 'x0', 'y0', 'k'):
        n[k].setVisible(x)
""" % XLOG


GENERATE = r'''import os, datetime, tempfile
import nuke
import PyOpenColorIO as ocio

gen = nuke.thisNode()
MARK = 'clfbake_shaper'
SHORT = __SHORT__
SKIP_CLASSES = ('Tab_Knob', 'Text_Knob', 'PyScript_Knob', 'Script_Knob',
                'Obsolete_Knob', 'Pulldown_Knob', 'Help_Knob',
                'BeginTabGroup_Knob', 'EndTabGroup_Knob')
SKIP_NAMES = ('name', 'xpos', 'ypos', 'selected', 'tile_color', 'note_font',
              'note_font_size', 'note_font_color', 'hide_input', 'cached',
              'dope_sheet', 'bookmark', 'postage_stamp', 'postage_stamp_frame',
              'gl_color', 'icon', 'indicators', 'knobChanged', 'onCreate',
              'onDestroy', 'updateUI', 'autolabel', 'panel', 'lifetimeStart',
              'lifetimeEnd', 'useLifetime', 'lock_connections', 'help',
              'mapsize', 'window', 'rootNodeUpdated', 'process_mask',
              'disable', 'label', 'group_view_position', 'show_group_view',
              'disable_group_view')


def chain():
    nodes, n = [], gen.input(0)
    while n is not None:
        if MARK in n.knobs():
            return n, nodes
        nodes.append(n)
        n = n.input(0)
    raise RuntimeError('No CLFShaper found upstream of %s' % gen.name())


def knob_values(n):
    out = []
    for name in n.knobs():
        if name in SKIP_NAMES:
            continue
        try:
            k = n[name]
        except Exception:
            continue
        if k.Class() in SKIP_CLASSES:
            continue
        try:
            out.append((name, k.toScript()))
        except Exception:
            pass
    return out


def child(md, name, value=''):
    md.addChildElement(name, value)
    return list(md.getChildElements())[-1]


shaper, nodes = chain()
if not nodes:
    raise RuntimeError('Nothing between %s and %s to bake' % (shaper.name(), gen.name()))
preset = shaper['preset'].value()
short = SHORT[int(shaper['preset'].getValue())]
par = dict((k, shaper[k].value()) for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl'))
size = int(shaper['cube_size'].value())
log_input = bool(gen['log_input'].value())

path = gen['file'].evaluate()
if not path:
    root = nuke.root().name()
    d = os.path.dirname(root) if root and root != 'Root' else tempfile.gettempdir()
    path = os.path.join(d, '%s__%s.clf' % (nodes[-1].name(), short))
    gen['file'].setValue(path.replace(os.sep, '/'))
ext = os.path.splitext(path)[1].lower()
fmt = 'Color Transform Format' if ext == '.ctf' else 'Academy/ASC Common LUT Format'

# 1. bake the shaped test pattern through the chain with Nuke's GenerateLUT
tmp = os.path.join(tempfile.gettempdir(), 'clfbake_%s_%d.cube' % (gen.name(), os.getpid()))
gen['tmp_cube'].setValue(tmp.replace(os.sep, '/'))
shaper['pattern'].setValue(True)
try:
    f = nuke.frame()
    nuke.execute(gen.node('GenerateLUT1'), f, f)
finally:
    shaper['pattern'].setValue(False)

# 2. wrap it in a CLF: parametric shaper + LUT3D + metadata
cfg = ocio.Config.CreateRaw()
proc = cfg.getProcessor(ocio.FileTransform(src=tmp))
lut = None
for t in proc.getOptimizedProcessor(ocio.OPTIMIZATION_NONE).createGroupTransform():
    if isinstance(t, ocio.Lut3DTransform):
        lut = t
if lut is None:
    raise RuntimeError('GenerateLUT did not produce a 3D LUT: %s' % tmp)
lut.setInterpolation(ocio.INTERP_TETRAHEDRAL if gen['interpolation'].value() == 'tetrahedral'
                     else ocio.INTERP_LINEAR)
grp = ocio.GroupTransform()
if not log_input:
    grp.appendTransform(ocio.LogCameraTransform(
        base=2.0, linSideBreak=[par['lb']] * 3,
        logSideSlope=[par['gs']] * 3, logSideOffset=[par['go']] * 3,
        linSideSlope=[par['ls']] * 3, linSideOffset=[par['lo']] * 3,
        linearSlope=[par['sl']] * 3))
grp.appendTransform(lut)

md = grp.getFormatMetadata()
md.setID('%s__%s' % (nodes[-1].name(), short))
md.setName(nodes[-1].name())
child(md, 'InputDescriptor', preset if log_input else 'Linear (%s shaper)' % preset)
child(md, 'OutputDescriptor', nodes[-1].name())
nb = child(child(md, 'Info'), 'NukeBake')
nb['version'] = '1'
sh = child(nb, 'Shaper')
sh['preset'] = preset
sh['shortname'] = short
sh['cube_size'] = str(size)
sh['embedded'] = 'false' if log_input else 'true'
for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl'):
    sh[k] = repr(par[k])
for k in ('w0', 'o', 'x0', 'y0', 'k'):
    sh[k] = repr(shaper[k].value())
ns = child(nb, 'Nodes')
for n in nodes:
    e = child(ns, 'Node')
    e['class'] = n.Class()
    e['name'] = n.name()
    for name, val in knob_values(n):
        child(e, 'Knob', val)['name'] = name
g = child(nb, 'Generator')
g['nuke'] = nuke.NUKE_VERSION_STRING
g['ocio'] = ocio.__version__
g['date'] = datetime.datetime.now().isoformat(timespec='seconds')
g['script'] = nuke.root().name()
grp.write(config=cfg, formatName=fmt, fileName=path)
try:
    os.remove(tmp)
except OSError:
    pass
gen['status'].setValue('wrote %s (%d nodes, %d^3, %s)' % (
    os.path.basename(path), len(nodes), size, 'log input' if log_input else short))
'''.replace('__SHORT__', repr([p[1] for p in PRESETS]))


RESTORE = r'''import xml.etree.ElementTree as ET
import nuke

gen = nuke.thisNode()
MARK = 'clfbake_shaper'
path = gen['file'].evaluate()
root = ET.parse(path).getroot()


def tag(e):
    return e.tag.split('}')[-1]


def find(e, name):
    for c in e.iter():
        if tag(c) == name:
            return c


nb = find(root, 'NukeBake')
if nb is None:
    raise RuntimeError('%s has no NukeBake metadata' % path)

targets = [n for n in nuke.thisParent().nodes() if n.isSelected()]
if not targets:
    targets, n = [], gen.input(0)
    while n is not None:
        targets.append(n)
        if MARK in n.knobs():
            break
        n = n.input(0)
targets = list(targets)
done = []

sh = find(nb, 'Shaper')
for t in targets:
    if sh is not None and MARK in t.knobs():
        t['preset'].setValue(sh.get('preset'))
        for k in ('w0', 'o', 'x0', 'y0', 'k'):
            if sh.get(k) is not None:
                t[k].setValue(float(sh.get(k)))
        t['cube_size'].setValue(int(sh.get('cube_size', 48)))
        gen['log_input'].setValue(sh.get('embedded') == 'false')
        done.append(t.name())
        targets.remove(t)
        break

for e in find(nb, 'Nodes') or []:
    cls = e.get('class')
    match = None
    for t in targets:
        if t.Class() == cls:
            match = t
            break
    if match is None:
        continue
    targets.remove(match)
    for k in e:
        name = k.get('name')
        if name in match.knobs():
            try:
                match[name].fromScript(k.text or '')
            except Exception:
                pass
    done.append('%s <- %s' % (match.name(), e.get('name')))

gen['status'].setValue('restored: %s' % (', '.join(done) or 'nothing matched (select the nodes to restore)'))
'''


def build_shaper():
    g = nuke.nodes.Group(name='CLFShaper')
    g.begin()
    inp = nuke.nodes.Input(name='Input1')
    pat = nuke.nodes.CMSTestPattern(name='CMSTestPattern1')
    pat['cube_size'].setExpression('parent.cube_size')
    inv = nuke.nodes.Expression(name='InverseShaper', inputs=[pat])
    for i, c in enumerate('rgb'):
        inv['expr%d' % i].setValue(
            '%s<=parent.logbreak?(%s-parent.linoff)/parent.sl:(pow(2,(%s-parent.go)/parent.gs)-parent.lo)/parent.ls'
            % (c, c, c))
    sw = nuke.nodes.Switch(name='Switch1', inputs=[inp, inv])
    sw['which'].setExpression('parent.pattern')
    nuke.nodes.Output(name='Output1', inputs=[sw])
    g.end()

    g.addKnob(nuke.Tab_Knob('CLFShaper'))
    about = nuke.Text_Knob('about', ' ',
        '<font color=#ccc><b>CLFShaper</b> | part of <a href=https://github.com/jedypod/open-display-transform>open-display-transform</a><br>'
        'Place at the head of the chain to bake. CLFGenerate goes at the tail.')
    g.addKnob(about)
    k = nuke.Enumeration_Knob('preset', 'shaper', [p[0] for p in PRESETS])
    k.setTooltip('Log encoding used to shape the 3D LUT domain. The exact same curve is written '
                 'into the CLF as a parametric Log node, so the LUT takes linear input. '
                 'T-LogE is what the released OpenDRT OCIO config uses.')
    g.addKnob(k)
    for name, label, val, lo, hi, tip in (
            ('w0', 'w0', 128.0, 10, 256, 'XLog: linear value that maps to 1.0'),
            ('o', 'o', 7.5, 0, 12, 'XLog: log asymptote offset, as 2^-o'),
            ('x0', 'x0', 0.0, 0, 0.5, 'XLog: lower intersection constraint x'),
            ('y0', 'y0', 0.075, 0, 0.5, 'XLog: lower intersection constraint y'),
            ('k', 'k', 0.06, 0, 0.2, 'XLog: slope of the linear extension')):
        d = nuke.Double_Knob(name, label)
        d.setRange(lo, hi)
        d.setValue(val)
        d.setTooltip(tip)
        g.addKnob(d)
    k = nuke.Int_Knob('cube_size', 'cube size')
    k.setValue(48)
    k.setTooltip('3D LUT lattice size. 48 matches the released OpenDRT OCIO config. CLF allows up to 128.')
    g.addKnob(k)
    k = nuke.Boolean_Knob('pattern', 'show pattern')
    k.setFlag(nuke.STARTLINE)
    k.setTooltip('Output the shaped CMSTestPattern instead of the input. CLFGenerate toggles this while baking.')
    g.addKnob(k)

    g.addKnob(nuke.Tab_Knob('Params'))
    # XLog derivation, verbatim from Jed's XLog.nk
    for name, expr, tip in (
            ('o0', '-(2**-o)', 'x offset: the y asymptote of the log curve'),
            ('s0', '(1-y0)/log((w0-o0)/(x0-o0))', 's0 = scale factor to satisfy intersection constraint at (x0, y0)'),
            ('o1', '1-s0*log(w0-o0)', 'y offset to satisfy intersection constraint at (w0, 1)'),
            ('x1', 's0*log(k*s0*exp(o1/s0))', 'x1 is x coordinate of linear extension intercept'),
            ('y1', 'exp((x1-o1)/s0)+o0', 'y1 is y coordinate of linear extension intercept')):
        d = nuke.Double_Knob(name, name)
        d.setExpression(expr)
        d.setTooltip(tip)
        d.setFlag(nuke.DISABLED)
        g.addKnob(d)
    g.addKnob(nuke.Text_Knob('div1', ''))
    # OCIO LogCameraTransform parameters (base 2) selected by preset
    for name, label, expr, tip in (
            ('gs', 'logSideSlope', table_expr(2, 's0*log(2)'), 'OCIO LogCameraTransform logSideSlope (base 2)'),
            ('go', 'logSideOffset', table_expr(3, 'o1'), 'OCIO LogCameraTransform logSideOffset'),
            ('ls', 'linSideSlope', table_expr(4, '1'), 'OCIO LogCameraTransform linSideSlope'),
            ('lo', 'linSideOffset', table_expr(5, '-o0'), 'OCIO LogCameraTransform linSideOffset'),
            ('lb', 'linSideBreak', table_expr(6, 'y1'), 'OCIO LogCameraTransform linSideBreak'),
            ('sl', 'linearSlope', table_expr(7, '1/k'), 'OCIO LogCameraTransform linearSlope'),
            ('logbreak', 'log break', 'gs*log(ls*lb+lo)/log(2)+go', 'log-side value at the break'),
            ('linoff', 'linear offset', 'logbreak-sl*lb', 'offset of the linear segment')):
        d = nuke.Double_Knob(name, label)
        d.setExpression(expr)
        d.setTooltip(tip)
        d.setFlag(nuke.DISABLED)
        g.addKnob(d)
    k = nuke.String_Knob('clfbake_shaper', '', '1')
    k.setFlag(nuke.INVISIBLE)
    g.addKnob(k)
    g['knobChanged'].setValue(SHAPER_KNOBCHANGED)
    g['preset'].setValue(0)
    g['label'].setValue('[value preset]')
    g['tile_color'].setValue(0x4a3a7aff)
    for name in ('w0', 'o', 'x0', 'y0', 'k'):
        g[name].setVisible(False)
    return g


def build_generate():
    g = nuke.nodes.Group(name='CLFGenerate')
    g.begin()
    inp = nuke.nodes.Input(name='Input1')
    lut = nuke.nodes.GenerateLUT(name='GenerateLUT1', inputs=[inp])
    lut['file_type'].setValue('.cube')
    lut['file'].setValue('[value parent.tmp_cube]')
    nuke.nodes.Output(name='Output1', inputs=[inp])
    g.end()

    g.addKnob(nuke.Tab_Knob('CLFGenerate'))
    g.addKnob(nuke.Text_Knob('about', ' ',
        '<font color=#ccc><b>CLFGenerate</b> | part of <a href=https://github.com/jedypod/open-display-transform>open-display-transform</a><br>'
        'Bakes everything between the upstream CLFShaper and this node into a CLF.'))
    k = nuke.File_Knob('file', 'file')
    k.setTooltip('Output path. Use a .ctf extension to write CTF instead of CLF. Empty: next to the script, named after the baked node.')
    g.addKnob(k)
    k = nuke.Enumeration_Knob('interpolation', 'interpolation', ['tetrahedral', 'trilinear'])
    k.setTooltip('Interpolation recorded in the LUT3D node.')
    g.addKnob(k)
    k = nuke.Boolean_Knob('log_input', 'log input')
    k.setTooltip('Omit the shaper from the CLF so it takes the shaper log encoding as input, '
                 'the way the released OpenDRT OCIO config consumes its cubes. '
                 'Default off: the shaper is embedded and the CLF takes linear input.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('generate', 'Generate', GENERATE)
    k.setFlag(nuke.STARTLINE)
    k.setTooltip('Bake the chain to a 3D LUT via GenerateLUT, then write the CLF with the shaper and node parameters embedded.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('restore', 'Restore', RESTORE)
    k.setTooltip('Read the CLF and apply the recorded parameters to the selected nodes (or to the upstream chain if nothing is selected).')
    g.addKnob(k)
    g.addKnob(nuke.Text_Knob('status', ' ', ''))
    k = nuke.String_Knob('tmp_cube', '', '')
    k.setFlag(nuke.INVISIBLE)
    g.addKnob(k)
    g['tile_color'].setValue(0x4a3a7aff)
    return g


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'CLFBake.nk')
    s = build_shaper()
    g = build_generate()
    g.setInput(0, s)
    s.setXYpos(0, 0)
    g.setXYpos(0, 200)
    for n in nuke.allNodes():
        n.setSelected(n in (s, g))
    nuke.nodeCopy(out)
    lines = [l for l in open(out).read().splitlines()
             if not l.startswith('version ') and l.strip() != 'selected true']
    open(out, 'w').write('\n'.join(lines) + '\n')
    print('wrote', out)


if __name__ == '__main__':
    main()
