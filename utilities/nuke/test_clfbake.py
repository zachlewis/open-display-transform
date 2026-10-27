"""Headless verification of CLFBake.nk against the OpenDRT Nuke node.

    rez-env nuke fxphd -- nuke -t test_clfbake.py
    CLFBAKE_PRESET=8 rez-env nuke fxphd -- nuke -t test_clfbake.py   # other shaper presets

Builds: test image -> CLFShaper -> OpenDRT -> CLFGenerate, presses Generate,
then checks that applying the CLF with PyOpenColorIO matches what Nuke renders:
  1. on the lattice (pattern mode) the match must be exact (shaper + LUT agree),
  2. off the lattice (real image) the match must be within 3D LUT interpolation
     error,
  3. a log-input CLF applied to log-encoded values matches too,
  4. Restore applies the recorded OpenDRT parameters to a fresh node,
  5. the same bake written as CTF loads,
  6. the thin Bake CLF / Restore CLF buttons on OpenDRT itself do the same,
  7. domain auto keeps a sufficient preset, fit replaces it and clamps exactly,
  8. From CLF rebuilds the chain from the script the CLF carries, and baking
     that rebuilt chain reproduces the original CLF's Log parameters and LUT
     array byte for byte, for both the standalone and the OpenDRT-button CLF.
PyOpenColorIO is used here only as the independent reader; the nodes write
the CLF with xml.etree. Exits non-zero on failure.
"""
import math
import os
import sys
import tempfile

import nuke
import PyOpenColorIO as ocio

HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
CLFBAKE = os.path.join(HERE, 'CLFBake.nk')
OPENDRT = os.path.join(ROOT, 'display-transforms', 'opendrt', 'OpenDRT.nk')
OUT = tempfile.mkdtemp(prefix='clfbake_test_')
PRESET = int(os.environ.get('CLFBAKE_PRESET', '0'))  # nuke -t eats numeric argv as a frame range
W = H = 24
LATTICE_TOL = 2e-4
IMAGE_TOL = 3e-2  # 48^3 tetrahedral interpolation of a DRT, display code values

failures = []


def check(name, err, tol):
    ok = err <= tol
    print('%s %-34s max err %.3e (tol %.0e)' % ('PASS' if ok else 'FAIL', name, err, tol))
    if not ok:
        failures.append(name)


def paste(path):
    before = set(nuke.allNodes())
    nuke.nodePaste(path)
    new = [n for n in nuke.allNodes() if n not in before]
    for n in new:
        n.setSelected(False)
    return new


def sample(node, w, h):
    out = []
    for y in range(h):
        for x in range(w):
            out.append([node.sample(c, x + 0.5, y + 0.5) for c in ('red', 'green', 'blue')])
    return out


def max_err(a, b):
    return max(abs(p - q) for pa, pb in zip(a, b) for p, q in zip(pa, pb))


def clf_core(path):
    """(LogParams attributes or None, LUT3D array rows, recorded knob values) for byte comparison."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    lp = root.find('Log/LogParams')
    rows = [l.split() for l in root.find('LUT3D/Array').text.strip().splitlines()]
    knobs = dict((k.get('name'), k.text) for k in root.iter('Knob'))
    return (dict(lp.attrib) if lp is not None else None), rows, knobs


def apply_clf(path, pixels):
    cfg = ocio.Config.CreateRaw()
    cpu = cfg.getProcessor(ocio.FileTransform(src=path)).getDefaultCPUProcessor()
    return [cpu.applyRGB(list(p)) for p in pixels]


def lin_to_log(shaper, v):
    gs, go, ls, lo, lb, sl = [shaper[k].value() for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl')]
    logbreak = gs * math.log(ls * lb + lo, 2) + go
    if v < lb:
        return sl * v + (logbreak - sl * lb)
    return gs * math.log(ls * v + lo, 2) + go


def log_to_lin(shaper, y):
    gs, go, ls, lo, lb, sl = [shaper[k].value() for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl')]
    logbreak = gs * math.log(ls * lb + lo, 2) + go
    if y <= logbreak:
        return (y - (logbreak - sl * lb)) / sl
    return (2.0 ** ((y - go) / gs) - lo) / ls


# --- scene -----------------------------------------------------------------
nuke.addFormat('%d %d 0 0 %d %d 1 clfbake_test' % (W, H, W, H))
nuke.root()['format'].setValue('clfbake_test')

shaper, gen = paste(CLFBAKE)
if 'clfbake_shaper' not in shaper.knobs():
    shaper, gen = gen, shaper
drt = paste(OPENDRT)[0]
shaper['preset'].setValue(PRESET)
shaper['cube_size'].setValue(48)

# test image spanning the shaper's own domain: 24 stops below its top, and
# negatives down to half of what the linear toe admits
def test_image(hi, lo, name):
    img = nuke.nodes.Expression(name=name)
    img['expr0'].setValue('%r*pow(2,24*(x/width-1))' % hi)
    img['expr1'].setValue('%r*pow(2,24*(y/height-1))' % hi)
    img['expr2'].setValue('0.5*(r+g)*(0.5+0.5*sin(x*0.7+y*0.3))+%r' % (0.5 * lo))
    return img


hi, lo = log_to_lin(shaper, 1.0), log_to_lin(shaper, 0.0)
img = test_image(hi, lo, 'TestImage')
shaper.setInput(0, img)
drt.setInput(0, shaper)
gen.setInput(0, drt)
print('nuke:', nuke.NUKE_VERSION_STRING, ' ocio:', ocio.__version__,
      ' preset:', shaper['preset'].value(), ' domain: %.4g .. %.4g' % (lo, hi), ' node:', drt.name())

# a non-default OpenDRT state so Restore has something to prove
drt['tn_con'].setValue(1.6)
drt['hs_r'].setValue(0.21)
drt['brl_enable'].setValue(False)
drt['tn_Lp'].setValue(200)
drt['display_name'].setValue('clfbake test')

# --- 1. lattice exactness --------------------------------------------------
clf = os.path.join(OUT, 'test.clf')
gen['file'].setValue(clf)
gen['generate'].execute()
print('status:', gen['status'].value())
assert os.path.exists(clf), clf
assert not shaper['pattern'].value(), 'pattern mode left on'

shaper['pattern'].setValue(True)
lin = sample(shaper, W, H)       # linear lattice samples (inverse shaper output)
nuke_out = sample(gen, W, H)     # what the chain renders for them
shaper['pattern'].setValue(False)
check('lattice: CLF vs Nuke', max_err(apply_clf(clf, lin), nuke_out), LATTICE_TOL)

# --- 2. real image ----------------------------------------------------------
lin = sample(img, W, H)
nuke_out = sample(gen, W, H)
check('image: CLF vs Nuke', max_err(apply_clf(clf, lin), nuke_out), IMAGE_TOL)

# --- 3. log input -------------------------------------------------------------
clf_log = os.path.join(OUT, 'test_log.clf')
gen['file'].setValue(clf_log)
gen['log_input'].setValue(True)
gen['generate'].execute()
gen['log_input'].setValue(False)
logpix = [[lin_to_log(shaper, c) for c in p] for p in lin]
check('log input: CLF vs Nuke', max_err(apply_clf(clf_log, logpix), nuke_out), IMAGE_TOL)
txt = open(clf_log).read()
assert '<Log ' not in txt, 'log-input CLF must not embed the shaper'
assert '<Log ' in open(clf).read(), 'linear CLF must embed the shaper'

# --- 4. restore ---------------------------------------------------------------
fresh = paste(OPENDRT)[0]
knobs = drt['look_preset_knobs'].value().split() + ['tn_Lp', 'tn_Lg', 'tn_gb', 'pt_hdr', 'eotf',
                                                   'display_gamut', 'clamp', 'display_name', 'in_gamut']
differs = [k for k in knobs if drt[k].toScript() != fresh[k].toScript()]
assert differs, 'fresh node already matches, restore proves nothing'
gen['file'].setValue(clf)
for n in nuke.allNodes():
    n.setSelected(n is fresh)
gen['restore'].execute()
print('status:', gen['status'].value())
recorded = list(clf_core(clf)[2])
bad = [k for k in recorded if drt[k].toScript() != fresh[k].toScript()]
print('%s restore: %d recorded knobs compared, %d differed before, %d differ after %s'
      % ('PASS' if not bad else 'FAIL', len(recorded), len(differs), len(bad), bad))
if bad:
    failures.append('restore')
for n in nuke.allNodes():
    n.setSelected(False)   # pasted/created nodes connect to the selection

# --- 5. CTF -------------------------------------------------------------------
ctf = os.path.join(OUT, 'test.ctf')
gen['file'].setValue(ctf)
gen['generate'].execute()
check('ctf: CLF vs CTF', max_err(apply_clf(ctf, lin), apply_clf(clf, lin)), 1e-6)

# --- 6. thin buttons on OpenDRT itself (fixed T-LogE shaper) -------------------
preset_was = int(shaper['preset'].getValue())
shaper['preset'].setValue(0)
img2 = test_image(log_to_lin(shaper, 1.0), log_to_lin(shaper, 0.0), 'TestImageTLogE')
shaper['preset'].setValue(preset_was)
d2 = paste(OPENDRT)[0]
d2.setInput(0, img2)
for k in ('tn_con', 'hs_r', 'brl_enable', 'tn_Lp', 'display_name'):
    d2[k].fromScript(drt[k].toScript())
clf2 = os.path.join(OUT, 'opendrt_button.clf')
d2['clf_file'].setValue(clf2)
d2['bake_clf'].execute()
assert os.path.exists(clf2), clf2
assert d2.input(0) is img2, 'Bake CLF did not restore the input'
assert '<Log ' in open(clf2).read()
check('opendrt button: CLF vs Nuke', max_err(apply_clf(clf2, sample(img2, W, H)), sample(d2, W, H)), IMAGE_TOL)
d3 = paste(OPENDRT)[0]
d3['clf_file'].setValue(clf2)
d3['restore_clf'].execute()
bad = [k for k in knobs if d2[k].toScript() != d3[k].toScript()]
print('%s opendrt button restore: %d knobs compared, %d differ %s' % ('PASS' if not bad else 'FAIL', len(knobs), len(bad), bad))
if bad:
    failures.append('opendrt button restore')
txt2 = open(clf2).read()
assert clf2 not in txt2 and '<Knob name="clf_file">' not in txt2, 'the bake path must not be recorded'

# --- 7. domain: auto keeps a sufficient preset, fit replaces it ---------------
shaper['preset'].setValue(len(shaper['preset'].values()) - 1)   # XLog
shaper['w0'].setValue(60000.0)
gen['file'].setValue(os.path.join(OUT, 'test_auto.clf'))
gen['domain'].setValue('auto')
gen['generate'].execute()
print('status:', gen['status'].value())
ok = 'auto: XLog covers it' in gen['status'].value() and shaper['w0'].value() == 60000.0
print('%s auto keeps a sufficient preset' % ('PASS' if ok else 'FAIL'))
if not ok:
    failures.append('auto keep')
shaper['preset'].setValue(PRESET)
shaper['w0'].setValue(128.0)
clf_fit = os.path.join(OUT, 'test_fit.clf')
gen['file'].setValue(clf_fit)
gen['domain'].setValue('fit')
gen['generate'].execute()
gen['domain'].setValue('preset')
print('status:', gen['status'].value())
assert shaper['preset'].value() == 'XLog', shaper['preset'].value()
hi_f, lo_f = log_to_lin(shaper, 1.0), log_to_lin(shaper, 0.0)
print('fit domain: %.4g .. %.4g  (w0 %.4g, k %.4g)' % (lo_f, hi_f, shaper['w0'].value(), shaper['k'].value()))
img3 = test_image(hi_f, lo_f, 'TestImageFit')
shaper.setInput(0, img3)
check('fit: CLF vs Nuke', max_err(apply_clf(clf_fit, sample(img3, W, H)), sample(gen, W, H)), IMAGE_TOL)
# beyond the fitted domain the chain must already be flat, so clamping is harmless
c = nuke.nodes.Expression(expr0=repr(hi_f * 8), expr1=repr(hi_f * 8), expr2=repr(hi_f * 8))
c.setInput(0, None)
shaper.setInput(0, c)
live = [[gen.sample(ch, 0.5, 0.5) for ch in ('red', 'green', 'blue')]]
check('fit: 3 stops above top', max_err(apply_clf(clf_fit, [[hi_f * 8] * 3]), live), 5e-3)
# 2 stops below the floor on one channel: left half red, right half blue
# (an image rather than single pixels: constant negative sources sample
# black in Nuke 15.1 -t)
c = nuke.nodes.Expression(expr0='x<width/2?%r:0.18' % (lo_f * 4), expr1='0.18',
                          expr2='x<width/2?0.18:%r' % (lo_f * 4))
c.setInput(0, None)
shaper.setInput(0, c)
check('fit: 2 stops below floor', max_err(apply_clf(clf_fit, sample(c, W, H)), sample(gen, W, H)), IMAGE_TOL)
shaper.setInput(0, img)

# --- 8. From CLF rebuilds the chain; re-baking reproduces the CLF ---------------
def rebake_matches(name, src_clf):
    for n in nuke.allNodes():
        n.setSelected(False)
    s2, g2 = paste(CLFBAKE)
    if 'clfbake_shaper' not in s2.knobs():
        s2, g2 = g2, s2
    s2.setInput(0, img)
    g2.setInput(0, s2)
    g2['file'].setValue(src_clf)
    g2['from_clf'].execute()
    print('status:', g2['status'].value())
    mid = g2.input(0)
    assert mid is not None and mid is not s2 and mid.input(0) is s2, 'From CLF did not wire the chain'
    out = os.path.join(OUT, 'rebake_%s.clf' % name.replace(' ', '_'))
    g2['file'].setValue(out)
    g2['generate'].execute()
    a, b = clf_core(src_clf), clf_core(out)
    same = a[0] == b[0] and a[1] == b[1] and a[2] == b[2]
    print('%s rebake from CLF (%s): LogParams %s, %d array rows %s, %d knobs %s'
          % ('PASS' if same else 'FAIL', name, 'same' if a[0] == b[0] else 'DIFFER',
             len(a[1]), 'identical' if a[1] == b[1] else 'DIFFER', len(a[2]), 'same' if a[2] == b[2] else 'DIFFER'))
    if not same:
        failures.append('rebake ' + name)

rebake_matches('standalone', clf)          # T-LogE 48^3, the first bake of this run
rebake_matches('opendrt button', clf2)     # the OpenDRT button's own bake

print('outputs in', OUT)
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASS')
