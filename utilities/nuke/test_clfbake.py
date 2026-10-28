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

# --- 9. model / mastering / interop metadata --------------------------------------
import xml.etree.ElementTree as ET


def meta_checks(name, path, node, expect_out, expect_cicp, grey_nits, in_id=None, conform=None, tail=None):
    nb = ET.parse(path).getroot().find('Info/CLFBake')
    model, mast, inter = nb.find('Model'), nb.find('Mastering'), nb.find('Interop')
    problems = []
    if callable(expect_out):
        expect_out = expect_out(nb)
    got_conform = float(inter.get('outputConform')) if inter is not None and inter.get('outputConform') else None
    if (conform is None) != (got_conform is None) or (conform is not None and abs(got_conform - conform) > 1e-9):
        problems.append('outputConform %s vs %s' % (got_conform, conform))
    if conform is not None and tail is not None:
        # the CLF must equal the node's output times the conforming scale
        lin = sample(img, W, H)
        via = [[c / conform for c in p] for p in apply_clf(path, lin)]   # compare in the node's own units
        err = max_err(via, sample(tail, W, H))
        if err > IMAGE_TOL:
            problems.append('conformed output err %.2e' % err)
    if model is None or model.get('name') != 'OpenDRT' or not model.get('version'):
        problems.append('Model element')
    else:
        ts = model.find('Tonescale')
        if ts is None or float(ts.get('tn_Lp')) != node['tn_Lp'].value() or ts.get('tn_su') != node['tn_su'].value() \
                or ts.get('tn_su_index') != str(int(node['tn_su'].getValue())):
            problems.append('Tonescale ids')
        # every model attribute is a real knob id with the same value as the node
        for e in model:
            for pid, val in e.attrib.items():
                if pid.endswith('_index'):
                    continue
                if pid not in node.knobs():
                    problems.append('unknown id %s' % pid)
                elif node[pid].Class() == 'Enumeration_Knob':
                    if val != node[pid].value():
                        problems.append('enum %s' % pid)
                elif node[pid].Class() == 'Boolean_Knob':
                    if val != ('true' if node[pid].value() else 'false'):
                        problems.append('bool %s' % pid)
                elif isinstance(node[pid].value(), float) and float(val) != node[pid].value():
                    problems.append('value %s' % pid)
    dcv = mast.find('DisplayColorVolume') if mast is not None else None
    if dcv is None or float(dcv.get('maxLuminance')) != node['tn_Lp'].value() or not dcv.get('white'):
        problems.append('DisplayColorVolume')
    lum = mast.find('Luminance') if mast is not None else None
    if lum is None or float(lum.get('greyMeasured', 0)) <= 0 or float(lum.get('diffuseWhiteMeasured', 0)) <= 0:
        problems.append('Luminance measured')
    elif grey_nits is not None and abs(float(lum.get('greyMeasured')) - grey_nits) > 0.03 * grey_nits:
        problems.append('greyMeasured %s vs nominal %.3g' % (lum.get('greyMeasured'), grey_nits))
    cicp = mast.find('CICP') if mast is not None else None
    if expect_cicp and (cicp is None or (cicp.get('primaries'), cicp.get('transfer')) != expect_cicp):
        problems.append('CICP')
    if (inter.get('output') if inter is not None else None) != expect_out:
        problems.append('Interop output')
    if (inter.get('input') if inter is not None else None) != in_id:
        problems.append('Interop input')
    print('%s metadata (%s)%s' % ('PASS' if not problems else 'FAIL', name, '' if not problems else ': ' + ', '.join(problems)))
    if problems:
        failures.append('metadata ' + name)


# SDR, default "dim" surround lifts grey above nominal, so only presence is checked;
# the default input gamut (Filmlight E-Gamut) has no published CIID -> namespaced
meta_checks('sdr rec1886', clf, drt, 'g24_rec709_display', ('1', '1'), None, in_id='opendrt-v1.1:lin_egamut_scene')
# PQ Rec.2100 1000 nit on a fresh node, with a mappable input gamut
d4 = paste(OPENDRT)[0]
d4.setInput(0, img)
d4['eotf'].setValue('PQ')
d4['display_gamut'].setValue('Rec.2020 (P3 Limited)')
d4['tn_Lp'].setValue(1000)
d4['tn_su'].setValue('dark')
d4['in_gamut'].setValue('ACEScg')
for n in nuke.allNodes():
    n.setSelected(False)
s3, g3 = paste(CLFBAKE)
if 'clfbake_shaper' not in s3.knobs():
    s3, g3 = g3, s3
s3.setInput(0, img)
d4.setInput(0, s3)
g3.setInput(0, d4)
g3['embed_script'].setValue(False)
clf_pq = os.path.join(OUT, 'test_pq.clf')
g3['file'].setValue(clf_pq)
g3['generate'].execute()
assert '<Script>' not in open(clf_pq).read(), 'embed_script off must omit the script'


def pq_expect(nb):   # P3-limited container and HDR diffuse white are modifiers on the published base
    dw = int(round(float(nb.find('Mastering/Luminance').get('diffuseWhiteMeasured'))))
    return 'opendrt-v1.1:pq_rec2020_display|p3lim|dw%d' % dw


meta_checks('pq rec2020 1000nit', clf_pq, d4, pq_expect, ('9', '16'),
            d4['tn_Lg'].value() * (1 + d4['tn_gb'].value() * math.log(10, 2)), in_id='lin_ap1_scene')
nb_pq = ET.parse(clf_pq).getroot().find('Info/CLFBake')
cond = nb_pq.find('Mastering/ViewingEnvironment').get('condition')
fb = nb_pq.find('Interop').get('outputFallback')
print('%s pq viewing condition / fallback: %s / %s' % ('PASS' if (cond, fb) == ('cinema1000', 'pq_rec2020_display') else 'FAIL', cond, fb))
if (cond, fb) != ('cinema1000', 'pq_rec2020_display'):   # dark surround = cinema, wide gamut implied
    failures.append('pq condition/fallback')

# DCDM X'Y'Z' 2.6: published ID earned by the 48/52.37 conforming scale
d4['eotf'].setValue('2.6 Power DCI')
d4['display_gamut'].setValue("X'Y'Z'")
d4['tn_Lp'].setValue(100)
clf_xyz = os.path.join(OUT, 'test_xyz.clf')
g3['file'].setValue(clf_xyz)
g3['generate'].execute()
meta_checks('dcdm xyz 2.6', clf_xyz, d4, 'g26_xyzd65_display', ('10', '17'), None, in_id='lin_ap1_scene',
            conform=(48.0 / 52.37) ** (1 / 2.6), tail=d4)
# linear CIE XYZ D65 at 1000 nit peak: OCIO display interchange, 1.0 = 100 cd/m2, so scale 10
d4['eotf'].setValue('Linear')
d4['tn_Lp'].setValue(1000)
clf_lx = os.path.join(OUT, 'test_linxyz.clf')
g3['file'].setValue(clf_lx)
g3['generate'].execute()
meta_checks('linear xyz 1000nit', clf_lx, d4, 'ocio:lin_ciexyzd65_display', ('10', '8'), None, in_id='lin_ap1_scene',
            conform=10.0, tail=d4)

# --- 10. automatic file name and the parameter string --------------------------------
import re


def auto_bake(g, d, label):
    g['file'].setValue(OUT + '/')          # directory only
    g['generate'].execute()
    path = g['file'].value()
    base = os.path.basename(path)
    print('   %-28s %s' % (label, base))
    return path, base


# drt is modified (tn_con, hs_r, ...), tn_Lp 200: expect a hash, not a look name
shaper['preset'].setValue(0)   # back to T-LogE after the fit section
auto_path, base = auto_bake(gen, drt, 'modified node')
pat = r'OpenDRT-v1\.1\.0_[0-9a-f]{8}_video200__lin_egamut__to__g24_rec709__48\.clf$'
ok_mod = re.match(pat, base) is not None and os.path.exists(auto_path)
# untouched node: look name
for n in nuke.allNodes():
    n.setSelected(False)
s6, g6 = paste(CLFBAKE)
if 'clfbake_shaper' not in s6.knobs():
    s6, g6 = g6, s6
d6 = paste(OPENDRT)[0]
s6.setInput(0, img); d6.setInput(0, s6); g6.setInput(0, d6)
_, base6 = auto_bake(g6, d6, 'untouched node')
ok_std = re.match(r'OpenDRT-v1\.1\.0_Standard_video100__lin_egamut__to__g24_rec709__48\.clf$', base6) is not None
# tonescale preset applied over Standard: tonescale name + hash (look no longer matches)
vals = d6['tn_presets'].values()
for i in range(0, len(vals) - 1, 2):
    if vals[i].endswith('/Low Contrast'):
        nuke.tcl(vals[i + 1].replace('knobs this', 'knobs %s' % d6.fullName()))
_, base_tn = auto_bake(g6, d6, 'tonescale Low Contrast')
ok_tn = re.match(r'OpenDRT-v1\.1\.0_LowContrast-[0-9a-f]{8}_video100__lin_egamut__to__g24_rec709__48\.clf$', base_tn) is not None
# same look parameters twice -> same hash (deterministic), display change does not alter it
d6['tn_Lp'].setValue(400)
_, base_tn2 = auto_bake(g6, d6, 'same look, Lp 400')
look_of = lambda b: '_'.join(b.split('__')[0].split('_')[1:-1])   # OpenDRT-v.._<look>_<condition>
ok_det = look_of(base_tn) == look_of(base_tn2) and '_video400__' in base_tn2
print('%s auto file names: modified %s, untouched %s, tonescale %s, deterministic %s'
      % ('PASS' if all((ok_mod, ok_std, ok_tn, ok_det)) else 'FAIL', ok_mod, ok_std, ok_tn, ok_det))
if not all((ok_mod, ok_std, ok_tn, ok_det)):
    failures.append('auto file names')

# the CLF id is the parameter string; applying it to another node reproduces every recorded knob
pid = ET.parse(auto_path).getroot().get('id')
params_el = ET.parse(auto_path).getroot().find('Info/CLFBake/Params').text
ok_id = pid.startswith('opendrt1_1{') and 'clfbake{' in pid and pid.startswith(params_el)
d6['clf_params'].setValue(pid)
d6['apply_params'].execute()
recorded = list(clf_core(auto_path)[2])
bad = [k for k in recorded if drt[k].toScript() != d6[k].toScript()]
print('%s parameter string: id ok %s, applied to a fresh node %d/%d recorded knobs equal %s'
      % ('PASS' if ok_id and not bad else 'FAIL', ok_id, len(recorded) - len(bad), len(recorded), bad))
if not ok_id or bad:
    failures.append('parameter string')
print('   id:', pid[:160] + ('...' if len(pid) > 160 else ''))
# the OpenDRT button names its file the same way and fills the params field
d2['clf_file'].setValue(OUT + '/')
d2['bake_clf'].execute()
base2 = os.path.basename(d2['clf_file'].value())
ok_btn = re.match(r'OpenDRT-v1\.1\.0_[0-9a-f]{8}_video200__lin_egamut__to__g24_rec709__48\.clf$', base2) is not None \
    and d2['clf_params'].value().startswith('opendrt1_1{')
print('%s opendrt button auto name: %s' % ('PASS' if ok_btn else 'FAIL', base2))
if not ok_btn:
    failures.append('button auto name')

# --- 11. Cinespace output -----------------------------------------------------------
gen['format'].setValue('csp')
gen['file'].setValue(OUT + '/')
gen['generate'].execute()
csp = gen['file'].value()
gen['format'].setValue('clf')
txt = open(csp).read().splitlines()
ok_csp = csp.endswith('.csp') and txt[0] == 'CSPLUTV100' and txt[1] == '3D' and txt.count('4096') == 3 \
    and any(l.startswith('id opendrt1_1{') for l in txt)
print('%s csp structure: %s' % ('PASS' if ok_csp else 'FAIL', os.path.basename(csp)))
if not ok_csp:
    failures.append('csp structure')
shaper.setInput(0, img)
live = sample(gen, W, H)
# end to end through OCIO, whose csp reader resamples the prelut into a uniform
# 65536-entry 1D LUT over its input range (coarse in the shadows for a 512 top)
check('csp: vs Nuke (OCIO reader)', max_err(apply_clf(csp, lin), live), IMAGE_TOL)
# structural: the prelut points lie exactly on the CLF's Log node, and the cube
# rows are the CLF's rows (csp red-fastest, CLF blue-fastest)
lp = clf_core(auto_path)[0]
log = ocio.LogCameraTransform(base=2.0, linSideBreak=[float(lp['linSideBreak'])] * 3,
                              logSideSlope=[float(lp['logSideSlope'])] * 3, logSideOffset=[float(lp['logSideOffset'])] * 3,
                              linSideSlope=[float(lp['linSideSlope'])] * 3, linSideOffset=[float(lp['linSideOffset'])] * 3,
                              linearSlope=[float(lp['linearSlope'])] * 3)
lcpu = ocio.Config.CreateRaw().getProcessor(log).getDefaultCPUProcessor()
i = txt.index('END METADATA') + 2
n_pre = int(txt[i]); pre_in = [float(v) for v in txt[i + 1].split()]; pre_out = [float(v) for v in txt[i + 2].split()]
pre_err = max(abs(lcpu.applyRGB([x, x, x])[0] - y) for x, y in zip(pre_in, pre_out))
check('csp: prelut points on the Log node', pre_err, 1e-5)
size_line = next(k for k, l in enumerate(txt) if l == '48 48 48')
csp_rows = [tuple(l.split()) for l in txt[size_line + 1:size_line + 1 + 48 ** 3]]
N = 48
csp_bf = [csp_rows[r + N * g + N * N * b] for r in range(N) for g in range(N) for b in range(N)]
same = csp_bf == [tuple(r) for r in clf_core(auto_path)[1]]
print('%s csp: cube rows identical to the CLF (%d rows, n_prelut %d)' % ('PASS' if same and n_pre == 4096 else 'FAIL', len(csp_rows), n_pre))
if not (same and n_pre == 4096):
    failures.append('csp cube rows')

print('outputs in', OUT)
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASS')
