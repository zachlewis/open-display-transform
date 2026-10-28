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
COMMON = r'''import os, re, math, uuid, datetime, tempfile
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
              'disable_group_view', 'clf_file', 'clf_params')


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
    # .cube (and .csp) are red-fastest, the CLF Array is blue-fastest
    return n, [rows[r + n * g + n * n * b] for r in range(n) for g in range(n) for b in range(n)], rows


def shaper_inverse(sh, code):
    """log code value -> linear, for a shaper dict (gs go ls lo lb sl)."""
    logbreak = sh['gs'] * math.log(sh['ls'] * sh['lb'] + sh['lo'], 2) + sh['go']
    if code <= logbreak:
        return (code - (logbreak - sh['sl'] * sh['lb'])) / sh['sl']
    return (2.0 ** ((code - sh['go']) / sh['gs']) - sh['lo']) / sh['ls']


CSP_PRELUT = 4096


def write_csp(path, n, rows_red_fastest, shaper, log_input, conform, meta_lines):
    """Cinespace .csp: 1D prelut per channel (the shaper, 4096 log-spaced
    samples) followed by the 3D cube. Everything else stays in the metadata."""
    if log_input:
        pre_in, pre_out = ['0', '1'], ['0', '1']
    else:
        codes = [j / float(CSP_PRELUT - 1) for j in range(CSP_PRELUT)]
        pre_in = ['%.9g' % shaper_inverse(shaper, c) for c in codes]
        pre_out = ['%.9g' % c for c in codes]
    out = ['CSPLUTV100', '3D', '', 'BEGIN METADATA']
    out += [l for l in meta_lines if l.strip()]
    out += ['END METADATA', '']
    for _ in range(3):
        out += [str(len(pre_in)), ' '.join(pre_in), ' '.join(pre_out), '']
    out.append('%d %d %d' % (n, n, n))
    if conform is None:
        out += rows_red_fastest
    else:
        out += [' '.join('%.9g' % (float(v) * conform) for v in r.split()) for r in rows_red_fastest]
    open(path, 'w').write('\n'.join(out) + '\n')


def chain_script(nodes):
    """The nodes as a .nk snippet (nuke.nodeCopy), so a CLF can rebuild them."""
    full = nodes[0].fullName()
    parent = nuke.toNode(full.rsplit('.', 1)[0]) if '.' in full else nuke.root()
    sel = [n for n in parent.nodes() if n.isSelected()]
    tmp = os.path.join(tempfile.gettempdir(), 'clfbake_%d.nk' % os.getpid())
    with parent:
        for n in parent.nodes():
            n.setSelected(n in nodes)
        nuke.nodeCopy(tmp)
        for n in parent.nodes():
            n.setSelected(n in sel)
    txt = open(tmp).read()
    os.remove(tmp)
    # the bake path and the derived parameter string are not part of the node's recipe
    return '\n'.join(l for l in txt.splitlines()
                     if not l.strip().startswith(('clf_file ', 'clf_params '))) + '\n'


# --- structured description of recognised models -------------------------------
# OpenDRT v1.x: one element per module; attribute names are the model's own
# parameter identifiers, shared by the Nuke node, the DCTL (DEFINE_UI_PARAMS
# _<id>) and Jed's preset strings, so any of those can be generated from this.
# Enumerations carry the label plus <id>_index for integer-based targets.
OPENDRT_MODEL = (
    ('Input', ('in_gamut',)),
    ('Tonescale', ('tn_Lp', 'tn_Lg', 'tn_gb', 'tn_con', 'tn_sh', 'tn_toe', 'tn_off', 'tn_su')),
    ('HighContrast', ('tn_hcon_enable', 'tn_hcon', 'tn_hcon_pv', 'tn_hcon_st')),
    ('LowContrast', ('tn_lcon_enable', 'tn_lcon', 'tn_lcon_w')),
    ('CreativeWhite', ('cwp', 'cwp_lm')),
    ('RenderSpace', ('rs_sa', 'rs_rw', 'rs_bw')),
    ('PurityCompress', ('pt_enable', 'pt_hdr', 'pt_lml', 'pt_lml_r', 'pt_lml_g', 'pt_lml_b',
                        'pt_lmh', 'pt_lmh_r', 'pt_lmh_b')),
    ('PuritySoftclip', ('ptl_enable', 'ptl_c', 'ptl_m', 'ptl_y')),
    ('MidPurity', ('ptm_enable', 'ptm_low', 'ptm_low_rng', 'ptm_low_st', 'ptm_high', 'ptm_high_rng',
                   'ptm_high_st')),
    ('Brilliance', ('brl_enable', 'brl', 'brl_r', 'brl_g', 'brl_b', 'brl_rng', 'brl_st')),
    ('PostBrilliance', ('brlp_enable', 'brlp', 'brlp_r', 'brlp_g', 'brlp_b')),
    ('HueContrast', ('hc_enable', 'hc_r', 'hc_r_rng')),
    ('HueShiftRGB', ('hs_rgb_enable', 'hs_r', 'hs_r_rng', 'hs_g', 'hs_g_rng', 'hs_b', 'hs_b_rng')),
    ('HueShiftCMY', ('hs_cmy_enable', 'hs_c', 'hs_c_rng', 'hs_m', 'hs_m_rng', 'hs_y', 'hs_y_rng')),
    ('Output', ('eotf', 'display_gamut', 'clamp', 'display_name')),
)
# CIE xy of the OpenDRT display containers: (R, G, B, container white), ST 2086 style.
# X'Y'Z' has no primaries; its container white is E.
XY = {
    'Rec.709': ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06), (0.3127, 0.3290)),
    'P3-D65': ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290)),
    'Rec.2020 (P3 Limited)': ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046), (0.3127, 0.3290)),
    'P3-D60': ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.32168, 0.33767)),
    'P3-DCI': ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.314, 0.351)),
    "X'Y'Z'": (None, None, None, (1 / 3., 1 / 3.)),
}
CWP_XY = {'D93': (0.2831, 0.2971), 'D75': (0.2990, 0.3149), 'D65': (0.3127, 0.3290),
          'D60': (0.32168, 0.33767), 'D55': (0.3324, 0.3474), 'D50': (0.3457, 0.3585)}

# --- Color Interop IDs -----------------------------------------------------------
# Published CIF IDs are used bare when the encoding is exactly the published one.
# Anything OpenDRT-defined, or a published base with modifiers, is namespaced
# "opendrt-v<major.minor>:" per CIF Rec 03 (organisation ID, published base as
# fallback). Modifiers are appended to the base with "|":
#   p3lim                 Rec.2020 container limited to P3
#   d60sim d55sim ...     creative white other than D65 (D65 in a non-D65
#                         container is the implied, unmodified case)
#   dw<N>                 diffuse white (scene 1.0) luminance in cd/m2, HDR only
#   clamp0                the clamp knob is off, output may leave [0,1]
#   EE                    log code values in, full range out (log input mode)
GAMUT_SHORT = {'ACES': 'ap0', 'ACEScg': 'ap1', 'P3D65': 'p3d65', 'Rec.2020': 'rec2020', 'Rec.709': 'rec709',
               'Arri WideGamut 3': 'awg3', 'Arri WideGamut 4': 'awg4', 'Red WideGamutRGB': 'rwg',
               'Sony SGamut3': 'sgamut3', 'Filmlight E-Gamut': 'egamut', 'Filmlight E-Gamut2': 'egamut2',
               'DaVinci Wide Gamut': 'dwg'}
PUBLISHED_IN = ('lin_ap0_scene', 'lin_ap1_scene', 'lin_p3d65_scene', 'lin_rec2020_scene', 'lin_rec709_scene')
EOTF_SHORT = {'Linear': 'lin', '2.2 Power - sRGB Display': 'g22', '2.4 Power - Rec.1886': 'g24',
              '2.6 Power DCI': 'g26', 'PQ': 'pq', 'HLG': 'hlg'}
CONTAINER_SHORT = {'Rec.709': 'rec709', 'P3-D65': 'p3d65', 'Rec.2020 (P3 Limited)': 'rec2020',
                   'P3-D60': 'p3d60', 'P3-DCI': 'p3dci', "X'Y'Z'": 'xyzd65'}
# published display IDs, and the conforming scale (applied as a Matrix after the
# LUT3D) that makes OpenDRT's output match the published definition exactly:
#   DCDM X'Y'Z' is normalised to 52.37 cd/m2 with 48 cd/m2 white, OpenDRT to peak;
#   OCIO's CIE XYZ D65 display interchange puts 1.0 at 100 cd/m2, OpenDRT at peak.
PUBLISHED_OUT = {
    'g24_rec709_display': None, 'g22_rec709_display': None, 'lin_rec709_display': None,
    'g26_p3d65_display': None, 'pq_p3d65_display': None, 'lin_p3d65_display': None,
    'pq_rec2020_display': None, 'hlg_rec2020_display': None, 'lin_rec2020_display': None,
    'pq_xyzd65_display': None,
    'g26_xyzd65_display': (48.0 / 52.37) ** (1 / 2.6),
    'ocio:lin_ciexyzd65_display': 'lp/100',
}
OUT_ALIAS = {'lin_xyzd65_display': 'ocio:lin_ciexyzd65_display'}
# ITU-T H.273 code points for the output encoding (RGB, full range)
CICP_PRIMARIES = {'Rec.709': 1, 'P3-D65': 12, 'Rec.2020 (P3 Limited)': 9, 'P3-DCI': 11, "X'Y'Z'": 10}
CICP_TRANSFER = {'Linear': 8, '2.2 Power - sRGB Display': 4, '2.4 Power - Rec.1886': 1,
                 '2.6 Power DCI': 17, 'PQ': 16, 'HLG': 18}


def fmt(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        return repr(v)
    return str(v)


# --- parameter strings: name{key{value}group{key{'text'}}} -------------------------
# FilmLight-style parseable parameter string. Numbers bare, booleans 0/1,
# strings and enumeration labels single-quoted (\' and \\ escaped), lists
# comma-separated. Groups follow the Model modules; the parser flattens them,
# so only the leaf keys (the model's parameter ids) matter.
def pq(s):
    return "'" + str(s).replace('\\', '\\\\').replace("'", "\\'") + "'"


def pval(k):
    cls = k.Class()
    if cls == 'Boolean_Knob':
        return '1' if k.value() else '0'
    if cls == 'Enumeration_Knob':
        return pq(k.value())
    v = k.value()
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return ','.join(repr(float(x)) for x in v)
    return pq(v)


def opendrt_string(node, version):
    parts = ['look_name{%s}' % pq(node['look_name'].value())] if 'look_name' in node.knobs() else []
    for elem, ids in OPENDRT_MODEL:
        inner = ''.join('%s{%s}' % (pid, pval(node[pid])) for pid in ids if pid in node.knobs())
        parts.append('%s{%s}' % (elem.lower(), inner))
    return 'opendrt%s{%s}' % ('_'.join(version.split('.')[:2]), ''.join(parts))


def parse_params(s):
    """'name{k{v}g{k{'t'}}}...' -> {name: nested dict}; scalar blocks become values or lists."""
    pos = [0]

    def ws():
        while pos[0] < len(s) and s[pos[0]] in ' \t\r\n':
            pos[0] += 1

    def ident():
        ws()
        st = pos[0]
        while pos[0] < len(s) and (s[pos[0]].isalnum() or s[pos[0]] in '_-.+'):
            pos[0] += 1
        return s[st:pos[0]]

    def scalar():
        ws()
        if s[pos[0]] == "'":
            pos[0] += 1
            out = ''
            while s[pos[0]] != "'":
                if s[pos[0]] == '\\':
                    pos[0] += 1
                out += s[pos[0]]
                pos[0] += 1
            pos[0] += 1
            return out
        st = pos[0]
        while pos[0] < len(s) and s[pos[0]] not in ',}':
            pos[0] += 1
        t = s[st:pos[0]].strip()
        try:
            return float(t) if any(c in t for c in '.eE') else int(t)
        except ValueError:
            return t

    def block():
        items = {}
        while True:
            ws()
            if s[pos[0]] == '}':
                pos[0] += 1
                return items
            st = pos[0]
            name = ident()
            ws()
            if name and pos[0] < len(s) and s[pos[0]] == '{':
                pos[0] += 1
                items[name] = block()
                continue
            pos[0] = st
            vals = [scalar()]
            ws()
            while s[pos[0]] == ',':
                pos[0] += 1
                vals.append(scalar())
                ws()
            if s[pos[0]] != '}':
                raise ValueError('expected } at %d in parameter string' % pos[0])
            pos[0] += 1
            return vals[0] if len(vals) == 1 else vals

    out = {}
    while True:
        ws()
        if pos[0] >= len(s):
            return out
        name = ident()
        ws()
        if not name or pos[0] >= len(s) or s[pos[0]] != '{':
            raise ValueError('expected name{ at %d in parameter string' % pos[0])
        pos[0] += 1
        out[name] = block()


def flatten_params(d, out=None):
    out = {} if out is None else out
    for k, v in d.items():
        if isinstance(v, dict):
            flatten_params(v, out)
        else:
            out[k] = v
    return out


def apply_params(node, text):
    """Set the node's knobs from a parameter string. Returns the number applied."""
    top = parse_params(text.strip())
    leaves = {}
    for name, content in top.items():
        if name.startswith('opendrt') and isinstance(content, dict):
            flatten_params(content, leaves)
    n = 0
    for k, v in leaves.items():
        if k not in node.knobs():
            continue
        kn = node[k]
        if kn.Class() == 'Boolean_Knob':
            kn.setValue(bool(v))
        elif kn.Class() == 'Enumeration_Knob':
            kn.setValue(str(v))
        elif isinstance(v, list):
            kn.setValue([float(x) for x in v])
        elif isinstance(kn.value(), float):
            kn.setValue(float(v))
        else:
            kn.setValue(str(v))
        n += 1
    return n


# --- preset recognition and the automatic file name ---------------------------------
def tcl_tokens(s):
    return [next(g for g in m.groups() if g is not None)
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|\{([^{}]*)\}|(\S+)', s)]


def preset_dicts(node, knob):
    """[(label, {knob: value text})] from a 'knobs this {...}' pulldown."""
    out, vals = [], node[knob].values()
    for i in range(0, len(vals) - 1, 2):
        m = re.search(r'knobs this \{(.*)\}\s*$', vals[i + 1], re.S)
        if m:
            toks = tcl_tokens(m.group(1))
            out.append((vals[i].split('/', 1)[-1], dict(zip(toks[0::2], toks[1::2]))))
    return out


def knob_equals(k, text):
    cls = k.Class()
    try:
        if cls == 'Boolean_Knob':
            return bool(k.value()) == bool(int(float(text)))
        if cls == 'Enumeration_Knob':
            return k.value() == text or (text.lstrip('-').isdigit() and int(k.getValue()) == int(text))
        if isinstance(k.value(), float):
            return abs(k.value() - float(text)) <= 1e-6 * max(1.0, abs(float(text)))
    except ValueError:
        return False
    return str(k.value()) == text


def preset_match(node, knob):
    for label, d in preset_dicts(node, knob):
        if d and all(name in node.knobs() and knob_equals(node[name], v) for name, v in d.items()):
            return label
    return None


def safe_name(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '', str(s).replace(' ', ''))


def opendrt_filename(node, version, in_id, out_id, condition, shaper):
    """Explicit, deterministic:
    OpenDRT-v<ver>_<look|TonescaleName-hash|hash>_<condition>__<in id>__to__<out id>__<size>
    Sections split on '__'; the head on '_' (version, look, condition; the look
    may contain hyphens); the IDs on '__to__'. The size field carries the shaper
    name when it is not the default T-LogE (xlog48)."""
    look = preset_match(node, 'look_presets')
    if look:
        preset = safe_name(look)
    else:
        knobs = node['look_preset_knobs'].value().split() if 'look_preset_knobs' in node.knobs() else []
        digest = uuid.uuid5(uuid.NAMESPACE_URL, ' '.join('%s=%s' % (k, pval(node[k])) for k in knobs if k in node.knobs())).hex[:8]
        tn = preset_match(node, 'tn_presets')
        preset = (safe_name(tn) + '-' + digest) if tn else digest
    def part(i):   # namespace off, _scene/_display off, modifiers joined with '-'
        base, mods = i.split(':')[-1].split('|')[0], i.split('|')[1:]
        return '-'.join([re.sub(r'_(scene|display)$', '', base)] + mods)
    size = '%s%d' % ('' if shaper['shortname'] == 'tloge' else shaper['shortname'], int(shaper['cube_size']))
    return 'OpenDRT-v%s_%s_%s__%s__to__%s__%s' % (version, preset, condition, part(in_id), part(out_id), size)


def is_opendrt(node):
    return all(k in node.knobs() for k in ('look_preset_knobs', 'tn_Lp', 'eotf', 'display_gamut', 'in_gamut'))


def to_nits(v, eotf, lp):
    """Decode one display code value to cd/m2 for OpenDRT's output encodings (neutral)."""
    v = max(v, 0.0)
    if eotf == 'Linear':
        return v * lp
    if eotf == 'PQ':
        m1, m2, c1, c2, c3 = 2610 / 16384., 2523 / 32., 107 / 128., 2413 / 128., 2392 / 128.
        e = v ** (1 / m2)
        return 10000.0 * (max(e - c1, 0.0) / (c2 - c3 * e)) ** (1 / m1)
    if eotf == 'HLG':
        a, b, c = 0.17883277, 1 - 4 * 0.17883277, 0.5 - 0.17883277 * math.log(4 * 0.17883277)
        e = v * v / 3.0 if v <= 0.5 else (math.exp((v - c) / a) + b) / 12.0
        return 1000.0 * e ** 1.2
    return lp * v ** {'2.2 Power - sRGB Display': 2.2, '2.4 Power - Rec.1886': 2.4, '2.6 Power DCI': 2.6}[eotf]


def interop_ids(node, ns, log_input, shaper, measured):
    """(input id, input fallback, output id, output fallback, conform scale or None)."""
    gam, eotf, cont = node['in_gamut'].value(), node['eotf'].value(), node['display_gamut'].value()
    cwp, lp = node['cwp'].value(), node['tn_Lp'].value()
    g = GAMUT_SHORT.get(gam, 'unknown')
    if log_input:
        if shaper['shortname'] == 'acescct' and g == 'ap1':
            in_id, in_fb = 'ocio:acescct_ap1_scene', None
        else:
            in_id, in_fb = '%s:%s_%s_scene' % (ns, shaper['shortname'], g), None
    else:
        base = 'lin_%s_scene' % g
        in_id, in_fb = (base, None) if base in PUBLISHED_IN else ('%s:%s' % (ns, base), None)
    base = '%s_%s_display' % (EOTF_SHORT[eotf], CONTAINER_SHORT[cont])
    base = OUT_ALIAS.get(base, base)
    conform = PUBLISHED_OUT.get(base)
    if conform == 'lp/100':
        conform = lp / 100.0
    mods = []
    if cont == 'Rec.2020 (P3 Limited)':
        mods.append('p3lim')
    if cwp != 'D65':
        mods.append(cwp.lower() + 'sim')
    if eotf in ('PQ', 'HLG') and measured:
        mods.append('dw%d' % int(round(to_nits(sum(measured[1]) / 3.0, eotf, lp))))
    if not node['clamp'].value():
        mods.append('clamp0')
    if log_input:
        mods.append('EE')
    published = base in PUBLISHED_OUT
    if published and not mods:
        out_id, out_fb = base, None
    else:
        out_id, out_fb = '%s:%s' % (ns, '|'.join([base] + mods)), (base if published else None)
    return in_id, in_fb, out_id, out_fb, (conform if abs((conform or 1.0) - 1.0) > 1e-9 else None)


def opendrt_version(node):
    about = node['about_label'].value() if 'about_label' in node.knobs() else ''
    ver = re.search(r'v(\d+\.\d+\.\d+)', about)
    return ver.group(1) if ver else 'unknown'


def describe_opendrt(nb, node, log_input, measured, shaper):
    """Append <Model>, <Mastering> and <Interop> for an OpenDRT node. Returns a
    dict: conform (output scale for a Matrix node or None), in_id, out_id,
    condition, version, params (the parameter string)."""
    version = opendrt_version(node)
    ns = 'opendrt-v' + ('.'.join(version.split('.')[:2]) if version != 'unknown' else 'unknown')
    model = ET.SubElement(nb, 'Model', name='OpenDRT', node=node.name())
    if version != 'unknown':
        model.set('version', version)
    if 'look_name' in node.knobs():
        model.set('look', node['look_name'].value())
    for elem, ids in OPENDRT_MODEL:
        e = ET.SubElement(model, elem)
        for pid in ids:
            if pid not in node.knobs():
                continue
            k = node[pid]
            e.set(pid, fmt(k.value()))
            if k.Class() == 'Enumeration_Knob':
                e.set(pid + '_index', str(int(k.getValue())))
    eotf, gamut = node['eotf'].value(), node['display_gamut'].value()
    lp, lg, gb = node['tn_Lp'].value(), node['tn_Lg'].value(), node['tn_gb'].value()
    mast = ET.SubElement(nb, 'Mastering')
    dcv = ET.SubElement(mast, 'DisplayColorVolume', gamut=gamut, maxLuminance=fmt(float(lp)), minLuminance='0.0')
    if gamut in XY:
        r, g, b, w = XY[gamut]
        for name, xy in (('red', r), ('green', g), ('blue', b), ('white', w)):
            if xy is not None:
                dcv.set(name, '%g %g' % xy)
    cwp = node['cwp'].value()
    if cwp in CWP_XY:
        dcv.set('contentWhite', '%g %g' % CWP_XY[cwp])   # creative white; "white" is the container's
    if gamut == 'Rec.2020 (P3 Limited)':
        dcv.set('limitedTo', 'P3-D65')
    # greyNominal: tn_Lg with grey boost, i.e. the tonescale constraint before
    # surround compensation (dim/bright lift it); greyMeasured is the truth.
    lum = ET.SubElement(mast, 'Luminance', peak=fmt(float(lp)),
                        greyNominal='%.6g' % (lg * (1.0 + gb * math.log(lp / 100.0, 2))))
    if measured:
        grey, white = measured
        lum.set('greyMeasured', '%.4g' % to_nits(sum(grey) / 3.0, eotf, lp))
        lum.set('diffuseWhiteMeasured', '%.4g' % to_nits(sum(white) / 3.0, eotf, lp))
        lum.set('measuredFrom', 'chain output for neutral 0.18 and 1.0, decoded with the output EOTF')
    su = node['tn_su'].value()
    # viewing condition token in the FilmLight style: <class><wide?><peak nits>
    cls = {'dark': 'cinema', 'dim': 'video', 'bright': 'office'}.get(su, su)
    wide = 'wide' if cls != 'cinema' and gamut != 'Rec.709' else ''
    condition = '%s%s%d' % (cls, wide, int(round(lp)))
    ET.SubElement(mast, 'ViewingEnvironment', surround=su, condition=condition,
                  ambient={'dark': 'theatre, dark surround', 'dim': 'video, 5 nit surround',
                           'bright': 'office or living room surround'}.get(su, ''))
    ET.SubElement(mast, 'CreativeWhite', whitepoint=cwp, limit=fmt(node['cwp_lm'].value()),
                  note='adapted to D65 for the render, then D65 to the output container; '
                       'a non-D65 container therefore carries D65 content white unless modified')
    if gamut in CICP_PRIMARIES and eotf in CICP_TRANSFER:
        ET.SubElement(mast, 'CICP', primaries=str(CICP_PRIMARIES[gamut]), transfer=str(CICP_TRANSFER[eotf]),
                      matrix='0', fullRange='true', note='ITU-T H.273; transfer 1 signals BT.709/BT.1886 by convention')
    in_id, in_fb, out_id, out_fb, conform = interop_ids(node, ns, log_input, shaper, measured)
    inter = ET.SubElement(nb, 'Interop', input=in_id, output=out_id, namespace=ns)
    if in_fb:
        inter.set('inputFallback', in_fb)
    if out_fb:
        inter.set('outputFallback', out_fb)
    if conform is not None:
        inter.set('outputConform', repr(conform))
        inter.set('outputConformNote', 'Matrix appended after the LUT3D so the output matches the published '
                  'definition: DCDM 48/52.37 headroom for g26_xyzd65_display, 1.0 = 100 cd/m2 for '
                  'ocio:lin_ciexyzd65_display')
    params = opendrt_string(node, version)
    ET.SubElement(nb, 'Params', syntax="name{key{value}group{key{'text'}}}; apply with Apply Params / Restore").text = params
    return dict(conform=conform, in_id=in_id, out_id=out_id, condition=condition, version=version,
                params=params, node=node)


def write_clf(path, cube, shaper, nodes, interpolation, log_input, measured=None, embed_script=True, fmt='clf'):
    """shaper: dict with preset, shortname, cube_size, gs go ls lo lb sl (and xlog w0 o x0 y0 k).
    measured: ((r,g,b) chain output for neutral 0.18, same for 1.0) or None.
    path may be a directory: the file is then named automatically with `fmt`
    (clf, ctf or csp); an explicit extension on a file path wins.
    Returns (lattice size, final path, id string)."""
    n, rows, rows_rf = read_cube(cube)
    tail = nodes[-1]
    root = ET.Element('ProcessList')
    root.set('name', tail.name())
    ET.SubElement(root, 'InputDescriptor').text = (
        shaper['preset'] if log_input else 'Linear (%s shaper)' % shaper['preset'])
    ET.SubElement(root, 'OutputDescriptor').text = tail.name()
    nb = ET.SubElement(ET.SubElement(root, 'Info'), 'CLFBake', version='2')
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
    desc = None
    for node in nodes:
        if is_opendrt(node):
            desc = describe_opendrt(nb, node, log_input, measured, shaper)
    conform = desc['conform'] if desc else None
    # file name and id
    as_dir = os.path.isdir(path) or path.endswith(('/', os.sep))
    if as_dir:
        base = (opendrt_filename(desc['node'], desc['version'], desc['in_id'], desc['out_id'], desc['condition'], shaper)
                if desc else '%s__%s%d' % (tail.name(), shaper['shortname'], int(shaper['cube_size'])))
        path = os.path.join(path, base + '.' + fmt)
    ext = os.path.splitext(path)[1].lower()
    if ext == '.ctf':
        root.set('version', '2')
    else:
        root.set('compCLFversion', '3')
    bake = 'clfbake{shaper{%s}size{%d}interpolation{%s}embedded{%d}}' % (
        pq(shaper['shortname']), int(shaper['cube_size']), pq(interpolation), 0 if log_input else 1)
    pid = (desc['params'] + bake) if desc else '%s__%s%d' % (tail.name(), shaper['shortname'], int(shaper['cube_size']))
    root.set('id', pid)
    if ext == '.csp':
        inter = nb.find('Interop')
        meta = ['id %s' % pid, 'input %s' % root.find('InputDescriptor').text,
                'output %s' % root.find('OutputDescriptor').text,
                'shaper %s %s (%d-point prelut)' % (shaper['preset'], shaper['shortname'], CSP_PRELUT)]
        if inter is not None:
            meta += ['interop_input %s' % inter.get('input', ''), 'interop_output %s' % inter.get('output', '')]
            if inter.get('outputConform'):
                meta.append('output_conform %s (baked into the cube)' % inter.get('outputConform'))
        if desc:
            mast = nb.find('Mastering')
            meta.append('viewing_condition %s' % mast.find('ViewingEnvironment').get('condition'))
            meta.append('peak_luminance %s' % mast.find('DisplayColorVolume').get('maxLuminance'))
        write_csp(path, n, rows_rf, shaper, log_input, conform, meta)
        return n, path.replace(os.sep, '/'), pid
    if embed_script:
        ET.SubElement(nb, 'Script').text = chain_script(nodes)
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
    if conform is not None:
        m = ET.SubElement(root, 'Matrix', inBitDepth='32f', outBitDepth='32f')
        ET.SubElement(m, 'Description').text = 'conform output scale %r (see Info/CLFBake/Interop)' % conform
        ET.SubElement(m, 'Array', dim='3 3').text = '\n            %r 0 0\n            0 %r 0\n            0 0 %r\n        ' % (
            conform, conform, conform)
        root.find('OutputDescriptor').text += ' (conformed, see Interop)'
    if hasattr(ET, 'indent'):
        ET.indent(root, '    ')
    arr.text = '\n' + '\n'.join('            ' + r for r in rows) + '\n        '
    ET.ElementTree(root).write(path, encoding='UTF-8', xml_declaration=True)
    return n, path.replace(os.sep, '/'), pid


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
        if e.tag.split('}')[-1] in ('CLFBake', 'NukeBake'):   # NukeBake: files from before the rename
            return e
    raise RuntimeError('%s has no CLFBake metadata' % path)


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
mode = gen['domain'].value()
if mode != 'preset':
    # Probe the chain: seven ramps (neutral, 3 chromatic, 3 single-channel
    # negative), find where the output stops changing by more than a 10-bit
    # step, then fit XLog to that (fit) or only if the preset falls short (auto).
    sn = shaper_node
    sn['probe'].setValue(True)
    try:
        W, H, NX = gen.width(), gen.height(), 128
        xs = [(i + 0.5) * W / NX for i in range(NX)]

        def row(v):
            y = (v + 0.5) * H / 8
            return [[gen.sample(c, x, y) for c in ('red', 'green', 'blue')] for x in xs]

        def run_start(vals):
            eps = max(1.0, max(abs(c) for p in vals for c in p)) / 1023.0
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
    if 'top' in unsat:
        top = 2.0 ** 15
    if 'floor' in unsat:
        floor = -0.5
    # what the current preset already covers (lin at code 1 and code 0)
    gs, go, ls, lo, lb, sl = [sn[k].value() for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl')]
    logbreak = gs * math.log(ls * lb + lo, 2) + go
    have_top = (2.0 ** ((1.0 - go) / gs) - lo) / ls
    have_floor = (0.0 - (logbreak - sl * lb)) / sl if 0.0 <= logbreak else (2.0 ** ((0.0 - go) / gs) - lo) / ls
    # an unbounded floor (DRTs keep desaturating negatives forever) is not a
    # reason to fit: the shadow guard caps what a fit could give anyway
    need = top > have_top or ('floor' not in unsat and floor < have_floor)
    mode = 'fit' if mode == 'fit' or need else 'keep'
    if mode == 'keep':
        fit_note = ', auto: %s covers it (top %.4g <= %.4g, floor %.3g >= %.3g)' % (
            sn['preset'].value(), top, have_top, floor, have_floor)
if mode == 'fit':
    w0 = min(2.0 ** 16, max(1.0, 2.0 * top))
    floor = 2.0 * floor
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
        top, w0, floor / 2.0, floor_of(k), k, ' [unsaturated: %s]' % ' '.join(sorted(set(unsat))) if unsat else '')
shaper = dict((k, shaper_node[k].value()) for k in ('gs', 'go', 'ls', 'lo', 'lb', 'sl', 'w0', 'o', 'x0', 'y0', 'k'))
shaper['preset'] = shaper_node['preset'].value()
shaper['shortname'] = SHORT[int(shaper_node['preset'].getValue())]
shaper['cube_size'] = int(shaper_node['cube_size'].value())
log_input = bool(gen['log_input'].value())

path = gen['file'].evaluate()
if not path:   # empty: next to the script (or the temp dir), named automatically
    root_name = nuke.root().name()
    path = os.path.dirname(root_name) if root_name and root_name != 'Root' else tempfile.gettempdir()

# chain output for neutral 0.18 and 1.0 (probe row 7: left half grey, right half white)
shaper_node['probe'].setValue(True)
try:
    W, H = gen.width(), gen.height()
    measured = tuple([gen.sample(c, x, 7.5 * H / 8) for c in ('red', 'green', 'blue')] for x in (W * 0.25, W * 0.75))
finally:
    shaper_node['probe'].setValue(False)
shaper_node['pattern'].setValue(True)
try:
    tmp = bake_cube(gen.node('GenerateLUT1'))
finally:
    shaper_node['pattern'].setValue(False)
size, path, pid = write_clf(path, tmp, shaper, nodes, gen['interpolation'].value(), log_input,
                            measured, bool(gen['embed_script'].value()), gen['format'].value())
os.remove(tmp)
gen['file'].setValue(path)
gen['status'].setValue('wrote %s (%d nodes, %d^3, %s)%s' % (
    os.path.basename(path), len(nodes), size, 'log input' if log_input else shaper['shortname'], fit_note))
'''


FROM_CLF = COMMON + r'''
gen = nuke.thisNode()
parent = nuke.thisParent()
nb = read_bake(gen['file'].evaluate())
sc = nb.find('Script')
if sc is None or not (sc.text or '').strip():
    raise RuntimeError('this CLF carries no node script (older bake); use Restore')
shaper, n = None, gen.input(0)
while n is not None:
    if MARK in n.knobs():
        shaper = n
        break
    n = n.input(0)
if shaper is None:
    raise RuntimeError('connect a CLFShaper upstream of %s first; From CLF rebuilds the nodes between them' % gen.name())
tmp = os.path.join(tempfile.gettempdir(), 'clfbake_from_%d.nk' % os.getpid())
open(tmp, 'w').write(sc.text)
with parent:
    for t in parent.nodes():
        t.setSelected(False)
    nuke.nodePaste(tmp)
    new = [t for t in parent.nodes() if t.isSelected()]
    for t in new:
        t.setSelected(False)
os.remove(tmp)
if not new:
    raise RuntimeError('nothing pasted from the CLF script')
heads = [t for t in new if t.input(0) not in new]
tails = [t for t in new if not any(d in new for d in t.dependent())]
heads[0].setInput(0, shaper)
gen.setInput(0, tails[0])
for i, t in enumerate(new):
    t.setXYpos(gen.xpos(), gen.ypos() - 60 * (len(new) - i))
done, sh = restore_apply(nb, [shaper])
gen['domain'].setValue('preset')
if sh is not None:
    gen['log_input'].setValue(sh.get('embedded') == 'false')
gen['status'].setValue('from CLF: %s -> %s -> %s' % (shaper.name(), ' -> '.join(t.name() for t in new), gen.name()))
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
path = n['clf_file'].evaluate()
if not path:
    root_name = nuke.root().name()
    path = os.path.dirname(root_name) if root_name and root_name != 'Root' else tempfile.gettempdir()
src = n.input(0)
with nuke.thisParent():
    pat = nuke.nodes.CMSTestPattern(cube_size=SHAPER['cube_size'])
    inv = nuke.nodes.Expression(inputs=[pat], expr0=EXPR[0], expr1=EXPR[1], expr2=EXPR[2])
    gw = nuke.nodes.Expression(expr0='x<width/2?0.18:1', expr1='x<width/2?0.18:1', expr2='x<width/2?0.18:1')
    lut = nuke.nodes.GenerateLUT(inputs=[n])
try:
    n.setInput(0, gw)
    W, H = n.width(), n.height()
    measured = tuple([n.sample(c, x, H * 0.5) for c in ('red', 'green', 'blue')] for x in (W * 0.25, W * 0.75))
    n.setInput(0, inv)
    tmp = bake_cube(lut)
finally:
    n.setInput(0, src)
    for t in (lut, gw, inv, pat):
        nuke.delete(t)
size, path, pid = write_clf(path, tmp, SHAPER, [n], 'tetrahedral', False, measured, True)
os.remove(tmp)
n['clf_file'].setValue(path)
n['clf_params'].setValue(pid.split('clfbake{')[0])
nuke.message('wrote %s' % path)
'''.replace('__SHAPER__', repr(OPENDRT_SHAPER)).replace(
    '__EXPR__', repr([inverse_expr(c, OPENDRT_SHAPER) for c in 'rgb']))

OPENDRT_RESTORE = COMMON + r'''
n = nuke.thisNode()
done, sh = restore_apply(read_bake(n['clf_file'].evaluate()), [n])
nuke.message('restored %s' % (', '.join(done) or 'nothing'))
'''

OPENDRT_APPLY = COMMON + r'''
n = nuke.thisNode()
count = apply_params(n, n['clf_params'].value())
nuke.message('applied %d parameters' % count)
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


ABOUT = ('<font color=#ccc><b>%s</b> | CLFBake, part of '
         '<a href=https://github.com/jedypod/open-display-transform>open-display-transform</a><br>%s<br>'
         'Docs: utilities/nuke/README.md</font>')


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
    # probe ramps: 8 rows x 1024 columns. Rows 0-3 exposure ramps (-20..+24
    # stops around 0.18; neutral, then chromatic), rows 4-6 single-channel
    # negative ramps, row 7 neutral 0.18 (left) and 1.0 (right) for luminance.
    nuke.addFormat('1024 8 0 0 1024 8 1 clfbake_probe')
    con = nuke.nodes.Constant(name='ProbeFormat')
    con['format'].setValue('clfbake_probe')
    ramp = nuke.nodes.Expression(name='ProbeRamp', inputs=[con])
    ramp['temp_name0'].setValue('t'); ramp['temp_expr0'].setValue('0.18*pow(2,-20+44*x/width)')
    ramp['temp_name1'].setValue('n'); ramp['temp_expr1'].setValue('0.18*pow(2,-20+26*x/width)')
    ramp['temp_name2'].setValue('v'); ramp['temp_expr2'].setValue('floor(y/height*8)')
    gw = 'v==7?(x<width/2?0.18:1):0.18'
    ramp['expr0'].setValue('v==0?t:v==1?t:v==2?0.2*t:v==3?0.2*t:v==4?-n:' + gw)
    ramp['expr1'].setValue('v==0?t:v==1?0.2*t:v==2?t:v==3?0.2*t:v==5?-n:' + gw)
    ramp['expr2'].setValue('v==0?t:v==1?0.2*t:v==2?0.2*t:v==3?t:v==6?-n:' + gw)
    sw = nuke.nodes.Switch(name='Switch1', inputs=[inp, inv, ramp])
    sw['which'].setExpression('parent.pattern?1:(parent.probe?2:0)')
    nuke.nodes.Output(name='Output1', inputs=[sw])
    g.end()

    g.addKnob(nuke.Tab_Knob('CLFShaper'))
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
    g.addKnob(nuke.Tab_Knob('About'))
    g.addKnob(nuke.Text_Knob('about', ' ', ABOUT % ('CLFShaper',
        'Place at the head of the chain to bake; CLFGenerate goes at the tail. '
        'Passes the image through until CLFGenerate switches it to a shaped CMSTestPattern.')))
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
    k = nuke.File_Knob('file', 'file')
    k.setTooltip('Output path. A directory, or empty (the script\'s directory), names the file automatically '
                 'in the chosen format; an explicit .clf / .ctf / .csp extension wins.')
    g.addKnob(k)
    k = nuke.Enumeration_Knob('format', 'format', ['clf', 'ctf', 'csp'])
    k.setTooltip('clf: Academy/ASC Common LUT Format, parametric Log shaper + LUT3D + metadata. '
                 'ctf: the same as OCIO CTF. csp: Cinespace, the shaper as a 4096-point 1D prelut '
                 'per channel + the 3D cube, for hosts without CLF; metadata is a text block.')
    g.addKnob(k)
    k = nuke.Enumeration_Knob('interpolation', 'interpolation', ['tetrahedral', 'trilinear'])
    k.setTooltip('Interpolation recorded in the LUT3D node.')
    g.addKnob(k)
    k = nuke.Boolean_Knob('log_input', 'log input')
    k.setTooltip('Omit the shaper from the CLF so it takes the shaper log encoding as input, '
                 'the way the released OpenDRT OCIO config consumes its cubes. '
                 'Default off: the shaper is embedded and the CLF takes linear input.')
    g.addKnob(k)
    k = nuke.Boolean_Knob('embed_script', 'embed node script')
    k.setValue(True)
    k.setTooltip('Also store the baked nodes as a Nuke script inside the CLF so From CLF can rebuild them '
                 'exactly. The structured parameters, mastering and interop metadata are written regardless.')
    g.addKnob(k)
    k = nuke.Enumeration_Knob('domain', 'domain', ['preset', 'auto', 'fit'])
    k.setTooltip('preset: use the shaper as set on CLFShaper.\n'
                 'auto: probe the chain with exposure ramps; if its output still changes by more than a '
                 '10-bit step beyond the preset\'s top or floor, replace the preset with a fitted XLog.\n'
                 'fit: always fit. XLog gets w0 one stop above where the output stops changing and a '
                 'linear toe reaching the negative floor. A fitted XLog is written to the CLF exactly, '
                 'like any preset, and the status line reports what was found.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('generate', 'Generate', script(
        GENERATE.replace('__SHORT__', repr([p[1] for p in PRESETS])).replace('__XLOG__', str(XLOG))))
    k.setFlag(nuke.STARTLINE)
    k.setTooltip('Bake the chain to a 3D LUT via GenerateLUT, then write the CLF with the shaper and node parameters embedded.')
    g.addKnob(k)
    k = nuke.PyScript_Knob('restore', 'Restore', script(RESTORE))
    k.setTooltip('Read the CLF and apply the recorded parameters to the selected nodes (or to the upstream chain if nothing is selected).')
    g.addKnob(k)
    k = nuke.PyScript_Knob('from_clf', 'From CLF', script(FROM_CLF))
    k.setTooltip('Rebuild the nodes that made this CLF from the script it carries, wire them between the '
                 'upstream CLFShaper and this node, and restore the shaper. Baking again reproduces the CLF.')
    g.addKnob(k)
    g.addKnob(nuke.Text_Knob('status', ' ', ''))
    g.addKnob(nuke.Tab_Knob('About'))
    g.addKnob(nuke.Text_Knob('about', ' ', ABOUT % ('CLFGenerate',
        'Bakes everything between the upstream CLFShaper and this node into a CLF or CTF: a parametric '
        'Log shaper, a LUT3D, and metadata that restores or rebuilds the nodes.')))
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
    k = nuke.String_Knob('clf_params', 'params')
    k.setTooltip("Parameter string, opendrt1_1{tonescale{tn_Lp{100}...}...}: every parameter of this node in a "
                 "parseable form. Bake CLF fills it in (it is also the CLF's id); paste one and press Apply Params.")
    g.addKnob(k)
    k = nuke.PyScript_Knob('apply_params', 'Apply Params', script(OPENDRT_APPLY))
    k.setTooltip('Set this node from the parameter string above.')
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
