"""Build CLFBake.nk (CLFShaper + CLFGenerate) and the thin "CLF Helpers"
buttons in OpenDRT.nk. Run inside Nuke so knob scripts are escaped by Nuke:

    rez-env nuke fxphd -- nuke -t build_clfbake.py

Writes utilities/nuke/CLFBake.nk and splices the CLF Helpers knob group into
display-transforms/opendrt/OpenDRT.nk (replacing it if already present).
The nodes need nothing beyond Nuke's Python: the CLF is written with
xml.etree, not PyOpenColorIO.
"""
import math
import os
import sys

import nuke

HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
OPENDRT = os.path.join(HERE, '..', '..', 'display-transforms', 'opendrt', 'OpenDRT.nk')

# ---------------------------------------------------------------------------
# Shaper presets: OCIO LogCameraTransform parameters, base 2, as used by Jed's
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
XLOG_DEFAULTS = (('w0', 128.0), ('o', 7.5), ('x0', 0.0), ('y0', 0.075), ('k', 0.06))


def auto_slope(p):
    """linearSlope OCIO computes when unset: derivative of the log segment at the break."""
    return p[2] * p[4] / ((p[4] * p[6] + p[5]) * math.log(2.0))


def preset_params(i):
    p = PRESETS[i]
    return dict(gs=p[2], go=p[3], ls=p[4], lo=p[5], lb=p[6], sl=p[7] if p[7] is not None else auto_slope(p))


def table_expr(col, xlog_expr):
    """Nuke expression: nested ternary over the preset index for one parameter."""
    expr = xlog_expr
    for i in range(XLOG - 1, -1, -1):
        v = PRESETS[i][col]
        if col == 7 and v is None:
            v = auto_slope(PRESETS[i])
        expr = "preset==%d?%.17g:(%s)" % (i, v, expr)
    return expr


# inverse shaper (log code value -> linear), the same formula for all presets
INVERSE = '%(c)s<=%(logbreak)s?(%(c)s-%(linoff)s)/%(sl)s:(pow(2,(%(c)s-%(go)s)/%(gs)s)-%(lo)s)/%(ls)s'


def inverse_expr(c, par):
    return INVERSE % dict(par, c=c)


# ---------------------------------------------------------------------------
# Python shared by every button. Pure stdlib + nuke.
# ---------------------------------------------------------------------------
COMMON = r'''import os, math, datetime, tempfile
import xml.etree.ElementTree as ET
import nuke

MARK = 'clfbake_shaper'
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
              'disable_group_view', 'clf_file')


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


def default_path(node, short, ext='.clf'):
    root = nuke.root().name()
    d = os.path.dirname(root) if root and root != 'Root' else tempfile.gettempdir()
    return os.path.join(d, '%s__%s%s' % (node.name(), short, ext)).replace(os.sep, '/')


def read_cube(path):
    n, rows = None, []
    for line in open(path):
        t = line.split()
        if not t or t[0].startswith('#'):
            continue
        if t[0] == 'LUT_3D_SIZE':
            n = int(t[1])
        elif len(t) == 3 and t[0][0] in '-+.0123456789':
            rows.append(' '.join(t))
    if n is None or len(rows) != n ** 3:
        raise RuntimeError('unexpected cube %s: size %s, %d rows' % (path, n, len(rows)))
    # .cube is red-fastest, CLF Array is blue-fastest
    return n, [rows[r + n * g + n * n * b] for r in range(n) for g in range(n) for b in range(n)]


def write_clf(path, cube, shaper, nodes, interpolation, log_input):
    """shaper: dict with preset, shortname, cube_size, gs go ls lo lb sl (and xlog w0 o x0 y0 k)."""
    n, rows = read_cube(cube)
    ctf = os.path.splitext(path)[1].lower() == '.ctf'
    tail = nodes[-1]
    root = ET.Element('ProcessList', {'version': '2'} if ctf else {'compCLFversion': '3'})
    root.set('id', '%s__%s' % (tail.name(), shaper['shortname']))
    root.set('name', tail.name())
    ET.SubElement(root, 'InputDescriptor').text = (
        shaper['preset'] if log_input else 'Linear (%s shaper)' % shaper['preset'])
    ET.SubElement(root, 'OutputDescriptor').text = tail.name()
    nb = ET.SubElement(ET.SubElement(root, 'Info'), 'NukeBake', version='1')
    sh = ET.SubElement(nb, 'Shaper')
    for k in ('preset', 'shortname', 'cube_size', 'gs', 'go', 'ls', 'lo', 'lb', 'sl', 'w0', 'o', 'x0', 'y0', 'k'):
        if k in shaper:
            sh.set(k, str(shaper[k]))
    sh.set('embedded', 'false' if log_input else 'true')
    ns = ET.SubElement(nb, 'Nodes')
    for node in nodes:
        e = ET.SubElement(ns, 'Node', {'class': node.Class(), 'name': node.name()})
        for name, val in knob_values(node):
            ET.SubElement(e, 'Knob', name=name).text = val
    ET.SubElement(nb, 'Generator', nuke=nuke.NUKE_VERSION_STRING,
                  date=datetime.datetime.now().isoformat(timespec='seconds'),
                  script=nuke.root().name())
    if not log_input:
        log = ET.SubElement(root, 'Log', inBitDepth='32f', outBitDepth='32f', style='cameraLinToLog')
        ET.SubElement(log, 'LogParams', base='2',
                      linSideSlope=repr(float(shaper['ls'])), linSideOffset=repr(float(shaper['lo'])),
                      logSideSlope=repr(float(shaper['gs'])), logSideOffset=repr(float(shaper['go'])),
                      linSideBreak=repr(float(shaper['lb'])), linearSlope=repr(float(shaper['sl'])))
    lut = ET.SubElement(root, 'LUT3D', inBitDepth='32f', outBitDepth='32f', interpolation=interpolation)
    arr = ET.SubElement(lut, 'Array', dim='%d %d %d 3' % (n, n, n))
    if hasattr(ET, 'indent'):
        ET.indent(root, '    ')
    arr.text = '\n' + '\n'.join('            ' + r for r in rows) + '\n        '
    ET.ElementTree(root).write(path, encoding='UTF-8', xml_declaration=True)
    return n


def bake_cube(generate_lut):
    tmp = os.path.join(tempfile.gettempdir(), 'clfbake_%d.cube' % os.getpid()).replace(os.sep, '/')
    generate_lut['file'].setValue(tmp)
    generate_lut['file_type'].setValue('.cube')
    f = nuke.frame()
    nuke.execute(generate_lut, f, f)
    return tmp


def read_bake(path):
    root = ET.parse(path).getroot()
    for e in root.iter():
        if e.tag.split('}')[-1] == 'NukeBake':
            return e
    raise RuntimeError('%s has no NukeBake metadata' % path)


def restore_apply(nb, targets):
    """Apply recorded values to targets (nodes). Returns (done, shaper element)."""
    targets, done = list(targets), []
    sh = nb.find('Shaper')
    for t in targets:
        if sh is not None and MARK in t.knobs():
            t['preset'].setValue(sh.get('preset'))
            for k in ('w0', 'o', 'x0', 'y0', 'k'):
                if sh.get(k) is not None:
                    t[k].setValue(float(sh.get(k)))
            t['cube_size'].setValue(int(sh.get('cube_size', 48)))
            done.append(t.name())
            targets.remove(t)
            break
    for e in nb.find('Nodes') if nb.find('Nodes') is not None else []:
        match = None
        for t in targets:
            if t.Class() == e.get('class'):
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
    return done, sh
'''


GENERATE = COMMON + r'''
SHORT = __SHORT__
gen = nuke.thisNode()

nodes, n = [], gen.input(0)
while n is not None and MARK not in n.knobs():
    nodes.append(n)
    n = n.input(0)
if n is None:
    raise RuntimeError('No CLFShaper found upstream of %s' % gen.name())
if not nodes:
    raise RuntimeError('Nothing between %s and %s to bake' % (n.name(), gen.name()))
shaper_node = n
fit_note = ''
if bool(gen['fit'].value()):
    # Probe the chain: seven ramps (neutral, 3 chromatic, 3 single-channel
    # negative), find where the output stops changing, fit XLog to that.
    sn = shaper_node
    sn['probe'].setValue(True)
    try:
        W, H, NX = gen.width(), gen.height(), 128
        xs = [(i + 0.5) * W / NX for i in range(NX)]

        def row(v):
            y = (v + 0.5) * H / 7
            return [[gen.sample(c, x, y) for c in ('red', 'green', 'blue')] for x in xs]

        def run_start(vals):
            eps = 2e-4 * max(1.0, max(abs(c) for p in vals for c in p))
            last, i = vals[-1], len(vals) - 1
            while i > 0 and max(abs(a - b) for a, b in zip(vals[i - 1], last)) <= eps:
                i -= 1
            return i

        top, floor, unsat = 0.0, 0.0, []
        for v in range(4):
            i = run_start(row(v))
            if i >= NX - 1:
                unsat.append('top')
            top = max(top, 0.18 * 2 ** (-20 + 44 * (i + 0.5) / NX))
        for v in range(4, 7):
            i = run_start(row(v))
            if i >= NX - 1:
                unsat.append('floor')
            floor = min(floor, -0.18 * 2 ** (-20 + 26 * (i + 0.5) / NX))
    finally:
        sn['probe'].setValue(False)
    w0 = 2.0 ** 16 if 'top' in unsat else min(2.0 ** 16, max(1.0, 2.0 * top))
    floor = -1.0 if 'floor' in unsat else 2.0 * floor
    # XLog: w0 sets the top; solve k for the floor (lin value at code 0),
    # keeping the linear toe's join at least 5 stops under grey.
    o, x0, y0 = sn['o'].value(), sn['x0'].value(), sn['y0'].value()
    o0 = -(2.0 ** -o)
    s0 = (1.0 - y0) / math.log((w0 - o0) / (x0 - o0))
    o1 = 1.0 - s0 * math.log(w0 - o0)

    def floor_of(k):
        return k * s0 * (1.0 - math.log(k * s0) - o1 / s0) + o0

    k = sn['k'].value()
    k_min = math.exp(-o1 / s0) / s0 * 1.01
    k_max = (0.18 * 2.0 ** -5 - o0) / s0
    if k_max > k_min:
        if floor <= floor_of(k_max):
            k = k_max
        elif floor >= floor_of(k_min):
            k = k_min
        else:
            lo_, hi_ = k_min, k_max
            for _ in range(60):
                mid = 0.5 * (lo_ + hi_)
                lo_, hi_ = (mid, hi_) if floor_of(mid) > floor else (lo_, mid)
            k = 0.5 * (lo_ + hi_)
    sn['preset'].setValue(__XLOG__)
    sn['w0'].setValue(w0)
    sn['k'].setValue(k)
    fit_note = ', fit: top %.4g -> w0 %.4g, floor %.3g -> %.3g (k %.3g)%s' % (
        top, w0, floor, floor_of(k), k, ' [unsaturated: %s]' % ' '.join(sorted(set(unsat))) if unsat else '')
shaper = dict((k, shaper_node[k].value()) for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl', 'w0', 'o', 'x0', 'y0', 'k'))
shaper['preset'] = shaper_node['preset'].value()
shaper['shortname'] = SHORT[int(shaper_node['preset'].getValue())]
shaper['cube_size'] = int(shaper_node['cube_size'].value())
log_input = bool(gen['log_input'].value())

path = gen['file'].evaluate()
if not path:
    path = default_path(nodes[-1], shaper['shortname'])
    gen['file'].setValue(path)

shaper_node['pattern'].setValue(True)
try:
    tmp = bake_cube(gen.node('GenerateLUT1'))
finally:
    shaper_node['pattern'].setValue(False)
size = write_clf(path, tmp, shaper, nodes, gen['interpolation'].value(), log_input)
os.remove(tmp)
gen['status'].setValue('wrote %s (%d nodes, %d^3, %s)%s' % (
    os.path.basename(path), len(nodes), size, 'log input' if log_input else shaper['shortname'], fit_note))
'''


RESTORE = COMMON + r'''
gen = nuke.thisNode()
nb = read_bake(gen['file'].evaluate())
targets = [n for n in nuke.thisParent().nodes() if n.isSelected()]
if not targets:
    n = gen.input(0)
    while n is not None:
        targets.append(n)
        if MARK in n.knobs():
            break
        n = n.input(0)
done, sh = restore_apply(nb, targets)
if sh is not None:
    gen['log_input'].setValue(sh.get('embedded') == 'false')
gen['status'].setValue('restored: %s' % (', '.join(done) or 'nothing matched (select the nodes to restore)'))
'''


# Thin buttons for OpenDRT.nk: fixed T-LogE shaper at 48^3, tetrahedral, embedded.
OPENDRT_SHAPER = dict(preset_params(0), preset=PRESETS[0][0], shortname=PRESETS[0][1], cube_size=48,
                      **dict(XLOG_DEFAULTS))
OPENDRT_SHAPER['logbreak'] = (OPENDRT_SHAPER['gs'] * math.log(OPENDRT_SHAPER['ls'] * OPENDRT_SHAPER['lb']
                              + OPENDRT_SHAPER['lo'], 2) + OPENDRT_SHAPER['go'])
OPENDRT_SHAPER['linoff'] = OPENDRT_SHAPER['logbreak'] - OPENDRT_SHAPER['sl'] * OPENDRT_SHAPER['lb']

OPENDRT_BAKE = COMMON + r'''
SHAPER = __SHAPER__
EXPR = __EXPR__
n = nuke.thisNode()
path = n['clf_file'].evaluate() or default_path(n, SHAPER['shortname'])
n['clf_file'].setValue(path)
src = n.input(0)
with nuke.thisParent():
    pat = nuke.nodes.CMSTestPattern(cube_size=SHAPER['cube_size'])
    inv = nuke.nodes.Expression(inputs=[pat], expr0=EXPR[0], expr1=EXPR[1], expr2=EXPR[2])
    lut = nuke.nodes.GenerateLUT(inputs=[n])
n.setInput(0, inv)
try:
    tmp = bake_cube(lut)
finally:
    n.setInput(0, src)
    for t in (lut, inv, pat):
        nuke.delete(t)
write_clf(path, tmp, SHAPER, [n], 'tetrahedral', False)
os.remove(tmp)
nuke.message('wrote %s' % path)
'''.replace('__SHAPER__', repr(OPENDRT_SHAPER)).replace(
    '__EXPR__', repr([inverse_expr(c, OPENDRT_SHAPER) for c in 'rgb']))

OPENDRT_RESTORE = COMMON + r'''
n = nuke.thisNode()
done, sh = restore_apply(read_bake(n['clf_file'].evaluate()), [n])
nuke.message('restored %s' % (', '.join(done) or 'nothing'))
'''


def script(body):
    """PyScript knobs run in __main__; keep every name inside one function."""
    return 'def __clfbake():\n' + ''.join('    ' + l + '\n' if l.strip() else '\n'
                                           for l in body.splitlines()) + '__clfbake()\n'


SHAPER_KNOBCHANGED = r"""n = nuke.thisNode()
if nuke.thisKnob().name() in ('preset', 'showPanel'):
    x = n['preset'].getValue() == %d
    for k in ('w0', 'o', 'x0', 'y0', 'k'):
        n[k].setVisible(x)
""" % XLOG


def build_shaper():
    g = nuke.nodes.Group(name='CLFShaper')
    g.begin()
    inp = nuke.nodes.Input(name='Input1')
    pat = nuke.nodes.CMSTestPattern(name='CMSTestPattern1')
    pat['cube_size'].setExpression('parent.cube_size')
    inv = nuke.nodes.Expression(name='InverseShaper', inputs=[pat])
    par = dict((k, 'parent.' + k) for k in ('logbreak', 'linoff', 'sl', 'go', 'gs', 'lo', 'ls'))
    for i, c in enumerate('rgb'):
        inv['expr%d' % i].setValue(inverse_expr(c, par))
    # probe ramps for "fit shaper to chain": 7 rows x 1024 columns.
    # t: exposure ramp -20..+24 stops around 0.18; n: negative magnitude ramp.
    nuke.addFormat('1024 7 0 0 1024 7 1 clfbake_probe')
    con = nuke.nodes.Constant(name='ProbeFormat')
    con['format'].setValue('clfbake_probe')
    ramp = nuke.nodes.Expression(name='ProbeRamp', inputs=[con])
    ramp['temp_name0'].setValue('t'); ramp['temp_expr0'].setValue('0.18*pow(2,-20+44*x/width)')
    ramp['temp_name1'].setValue('n'); ramp['temp_expr1'].setValue('0.18*pow(2,-20+26*x/width)')
    ramp['temp_name2'].setValue('v'); ramp['temp_expr2'].setValue('floor(y/height*7)')
    ramp['expr0'].setValue('v==0?t:v==1?t:v==2?0.2*t:v==3?0.2*t:v==4?-n:0.18')
    ramp['expr1'].setValue('v==0?t:v==1?0.2*t:v==2?t:v==3?0.2*t:v==5?-n:0.18')
    ramp['expr2'].setValue('v==0?t:v==1?0.2*t:v==2?0.2*t:v==3?t:v==6?-n:0.18')
    sw = nuke.nodes.Switch(name='Switch1', inputs=[inp, inv, ramp])
    sw['which'].setExpression('parent.pattern?1:(parent.probe?2:0)')
    nuke.nodes.Output(name='Output1', inputs=[sw])
    g.end()

    g.addKnob(nuke.Tab_Knob('CLFShaper'))
    g.addKnob(nuke.Text_Knob('about', ' ',
        '<font color=#ccc><b>CLFShaper</b> | part of <a href=https://github.com/jedypod/open-display-transform>open-display-transform</a><br>'
        'Place at the head of the chain to bake. CLFGenerate goes at the tail.'))
    k = nuke.Enumeration_Knob('preset', 'shaper', [p[0] for p in PRESETS])
    k.setTooltip('Log encoding used to shape the 3D LUT domain. The exact same curve is written '
                 'into the CLF as a parametric Log node, so the LUT takes linear input. '
                 'T-LogE is what the released OpenDRT OCIO config uses.')
    g.addKnob(k)
    for (name, val), (lo, hi, tip) in zip(XLOG_DEFAULTS, (
            (10, 256, 'XLog: linear value that maps to 1.0'),
            (0, 12, 'XLog: log asymptote offset, as 2^-o'),
            (0, 0.5, 'XLog: lower intersection constraint x'),
            (0, 0.5, 'XLog: lower intersection constraint y'),
            (0, 0.2, 'XLog: slope of the linear extension'))):
        d = nuke.Double_Knob(name, name)
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
    k = nuke.Boolean_Knob('probe', 'probe')
    k.setFlag(nuke.INVISIBLE)
    g.addKnob(k)
    g['knobChanged'].setValue(SHAPER_KNOBCHANGED)
    g['preset'].setValue(0)
    g['label'].setValue('[value preset]')
    g['tile_color'].setValue(0x4a3a7aff)
    for name, _ in XLOG_DEFAULTS:
        g[name].setVisible(False)
    return g


def build_generate():
    g = nuke.nodes.Group(name='CLFGenerate')
    g.begin()
    inp = nuke.nodes.Input(name='Input1')
    lut = nuke.nodes.GenerateLUT(name='GenerateLUT1', inputs=[inp])
    lut['file_type'].setValue('.cube')
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
    k = nuke.Boolean_Knob('fit', 'fit shaper to chain')
    k.setTooltip('Before baking, probe the chain with exposure ramps to find where its output stops '
                 'changing, then set the shaper to XLog with w0 at the top (one stop of margin) and the '
                 'linear toe reaching the negative floor. The fitted XLog is written to the CLF exactly, '
                 'like any preset. Off: the preset is used as is.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('generate', 'Generate', script(
        GENERATE.replace('__SHORT__', repr([p[1] for p in PRESETS])).replace('__XLOG__', str(XLOG))))
    k.setFlag(nuke.STARTLINE)
    k.setTooltip('Bake the chain to a 3D LUT via GenerateLUT, then write the CLF with the shaper and node parameters embedded.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('restore', 'Restore', script(RESTORE))
    k.setTooltip('Read the CLF and apply the recorded parameters to the selected nodes (or to the upstream chain if nothing is selected).')
    g.addKnob(k)
    g.addKnob(nuke.Text_Knob('status', ' ', ''))
    g['tile_color'].setValue(0x4a3a7aff)
    return g


def opendrt_knob_lines():
    """Knob definitions for the CLF Helpers group, escaped by Nuke."""
    g = nuke.nodes.Group(name='clfhelpers_tmp')
    g.addKnob(nuke.Tab_Knob('clf_grp', 'CLF Helpers', nuke.TABBEGINCLOSEDGROUP))
    k = nuke.File_Knob('clf_file', 'file')
    k.setTooltip('CLF (or .ctf) path. Empty: next to the script, named after this node.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('bake_clf', 'Bake CLF', script(OPENDRT_BAKE))
    k.setTooltip('Bake this node to a CLF: T-LogE shaper (as in the OpenDRT OCIO config) embedded as a '
                 'parametric Log node, 48^3 tetrahedral LUT3D, and every parameter stored in the metadata. '
                 'For other shapers, sizes, or baking a chain of nodes use CLFShaper + CLFGenerate from utilities/nuke/CLFBake.nk.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('restore_clf', 'Restore CLF', script(OPENDRT_RESTORE))
    k.setTooltip('Apply the parameters recorded in the CLF to this node.')
    g.addKnob(k)
    g.addKnob(nuke.Tab_Knob('endGroup_clf', '', nuke.TABENDGROUP))
    for n in nuke.allNodes():
        n.setSelected(n is g)
    tmp = os.path.join(HERE, '.clfhelpers_tmp.nk')
    nuke.nodeCopy(tmp)
    lines = open(tmp).read().splitlines()
    os.remove(tmp)
    nuke.delete(g)
    start = next(i for i, l in enumerate(lines) if 'addUserKnob {20 clf_grp' in l)
    end = next(i for i, l in enumerate(lines) if 'addUserKnob {20 endGroup_clf' in l)
    return lines[start:end + 1]


def splice_opendrt(path, new):
    lines = open(path).read().splitlines()
    starts = [i for i, l in enumerate(lines) if 'addUserKnob {20 clf_grp' in l]
    if starts:
        end = next(i for i, l in enumerate(lines) if 'addUserKnob {20 endGroup_clf' in l)
        lines[starts[0]:end + 1] = new
    else:
        anchor = next(i for i, l in enumerate(lines) if 'addUserKnob {20 endGroup_1' in l)
        lines[anchor + 1:anchor + 1] = new
    open(path, 'w').write('\n'.join(lines) + '\n')


def main():
    out = os.path.join(HERE, 'CLFBake.nk')
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
    splice_opendrt(os.path.normpath(OPENDRT), opendrt_knob_lines())
    print('spliced CLF Helpers into', os.path.normpath(OPENDRT))


if __name__ == '__main__':
    main()
