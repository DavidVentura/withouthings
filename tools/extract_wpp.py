#!/usr/bin/env python3
"""Extract the WPP protocol description from jadx-decompiled Withings sources.

Input:  a jadx output directory containing com/withings/comm/wpp/generated/.
Output: a single JSON IR consumed by the Rust and Lua emitters.
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict

CONST_RE = re.compile(r'public static final (?:short|int|byte|long) (\w+) = (-?\d+)')
GETTYPE_RE = re.compile(r'public short getType\(\) \{\s*return ([^;]+);')
DATASIZE_RE = re.compile(r'public short getDataSize\(\) \{\s*return \(short\) (\d+);')
INIT_RE = re.compile(
    r'public void initWithBytes\(java\.nio\.ByteBuffer byteBuffer\) \{(.*?)\n    \}', re.S)
TOBYTES_RE = re.compile(r'public byte\[\] toByteArray\(\) \{(.*?)\n    \}', re.S)
READ_RE = re.compile(r'this\.(\w+) = (\w+)\(byteBuffer\);')
WRITE_RE = re.compile(r'(\w+)\(b, this\.(\w+)\);')

# Java reader method -> wire kind. Closed set: verified to cover every
# initWithBytes body in the APK.
READERS = {
    'readByte': 'i8',
    'readUnsignedByte': 'u8',
    'readShort': 'i16',
    'readUnsignedShort': 'u16',
    'readInt': 'i32',
    'readUnsignedInt': 'u32',
    'readLong': 'i64',
    'readUnsignedLong': 'u64',
    'readString': 'string',
    'readByteArray': 'bytes',
    'readUnsignedByteArray': 'array_u8',
    'readShortArray': 'array_i16',
    'readUnsignedShortArray': 'array_u16',
    'readIntArray': 'array_i32',
    'readUnsignedIntArray': 'array_u32',
}

WRITERS = {
    'writeByte': 'i8',
    'writeUnsignedByte': 'u8',
    'writeShort': 'i16',
    'writeUnsignedShort': 'u16',
    'writeInt': 'i32',
    'writeUnsignedInt': 'u32',
    'writeLong': 'i64',
    'writeUnsignedLong': 'u64',
    'writeString': 'string',
    'writeByteArray': 'bytes',
    'writeUnsignedByteArray': 'array_u8',
    'writeShortArray': 'array_i16',
    'writeUnsignedShortArray': 'array_u16',
    'writeIntArray': 'array_i32',
    'writeUnsignedIntArray': 'array_u32',
}

SCALAR_KINDS = {'i8', 'u8', 'i16', 'u16', 'i32', 'u32', 'i64', 'u64'}

# Constants that describe protocol limits rather than field values.
LIMIT_SUFFIXES = ('_MAX_VAL', '_MIN_VAL', '_MAX', '_MIN', '_SIZE', '_LEN', '_NB_MAX')

# When a class's constants carry no field-name prefix, attach them to the
# first field whose name matches one of these, most specific first.
ENUM_FIELD_PREFERENCE = (
    'cmd', 'state', 'status', 'mode', 'reason', 'step', 'rc', 'type',
    'level', 'id', 'value', 'val', 'mask',
)


def snake(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).upper()


def parse_wpp_constants(path):
    src = open(path).read()
    return OrderedDict((m.group(1), int(m.group(2))) for m in CONST_RE.finditer(src))


def resolve_type_id(expr, consts):
    expr = expr.strip()
    literal = re.match(r'\(short\) (-?\d+)$', expr)
    if literal:
        return int(literal.group(1)) & 0xFFFF
    if expr == 'Short.MIN_VALUE':
        return 0x8000
    if expr == 'Short.MAX_VALUE':
        return 0x7FFF
    name = expr.split('.')[-1]
    if name in consts:
        return consts[name] & 0xFFFF
    return None


# Withings declares every constant as a Java `short`, so an error code on an
# unsigned field arrives negative and reaches the wire as its two's
# complement. A value is plausible for a field if it fits the field's width
# under either signedness.
RANGES = {kind: (-(1 << (bits - 1)), (1 << bits) - 1)
          for kind, bits in (('i8', 8), ('u8', 8), ('i16', 16), ('u16', 16),
                             ('i32', 32), ('u32', 32), ('i64', 64), ('u64', 64))}


def assign_enums(fields, class_consts, class_name):
    """Attach value constants to the field they describe.

    Withings' generator emits the constants without an explicit field link, so
    this reconstructs it from the naming convention and reports what it could
    not place rather than guessing silently.
    """
    if not class_consts:
        return {}, []

    # Only scalar fields can carry an enum; a length-prefixed field holding
    # constants means the constants describe something else.
    kinds = {f['name']: f['kind'] for f in fields}
    field_names = [f['name'] for f in fields if f['kind'] in SCALAR_KINDS]
    groups = OrderedDict()
    leftover = OrderedDict()

    for cname, cval in class_consts.items():
        parts = cname.split('_')
        # Longest field-name match wins: ANCS_CONFIGURATION_STATUS_ENABLE
        # belongs to `status`, ANCS_CONFIGURATION_TYPE_EMAIL_CAT to `type`.
        best = None
        for fname in field_names:
            tokens = snake(fname).split('_')
            n = len(tokens)
            for i in range(len(parts) - n):
                if parts[i:i + n] != tokens:
                    continue
                value = '_'.join(parts[i + n:])
                if value and (best is None or n > best[2]):
                    best = (fname, value, n)
                break
        # A value the field cannot hold is not a value of that field, however
        # well the name matches.
        if best:
            low, high = RANGES[kinds[best[0]]]
            if not low <= cval <= high:
                best = None
        if best:
            groups.setdefault(best[0], OrderedDict())[best[1]] = cval
        else:
            leftover[cname] = cval

    unplaced = []
    if leftover:
        target = None
        if len(field_names) == 1:
            target = field_names[0]
        else:
            # Strip a class-name prefix (MEASURE_CATEGORY_ECG on MeasureCategory)
            # before falling back to a preferred field name.
            for pref in ENUM_FIELD_PREFERENCE:
                matches = [f for f in field_names
                           if f.lower() == pref or snake(f).split('_')[-1].lower() == pref]
                if len(matches) == 1:
                    target = matches[0]
                    break
        if target:
            low, high = RANGES[kinds[target]]
            cls_prefix = snake(class_name) + '_'
            for cname, cval in leftover.items():
                if not low <= cval <= high:
                    unplaced.append(cname)
                    continue
                short = cname[len(cls_prefix):] if cname.startswith(cls_prefix) else cname
                groups.setdefault(target, OrderedDict())[short] = cval
        else:
            unplaced = list(leftover.keys())

    return groups, unplaced


def globals_for_type(consts, type_name, object_type_names):
    """Enum values Withings declares on Wpp itself rather than on the class.

    `TYPE_CMDERROR_ERR_CMDUNKN` describes the `err` field of `Cmderror`, so
    strip the type's own name and let the usual field matching place it.
    Constants that name another object's type are ids, not field values, and
    only match here because one type name prefixes another.
    """
    if not type_name:
        return OrderedDict()
    prefix = type_name + '_'
    return OrderedDict(
        (name[len(prefix):], value) for name, value in consts.items()
        if name.startswith(prefix)
        and name not in object_type_names
        and not name.endswith(LIMIT_SUFFIXES))


def parse_object_class(path, consts, type_names):
    src = open(path).read()
    class_name = os.path.basename(path)[:-5]

    m = GETTYPE_RE.search(src)
    if not m:
        return None, 'no getType()'
    type_id = resolve_type_id(m.group(1), consts)
    if type_id is None:
        return None, 'unresolved type expression: %s' % m.group(1).strip()

    init = INIT_RE.search(src)
    if not init:
        return None, 'no initWithBytes()'

    body = init.group(1).strip()
    fields = []
    for line in body.splitlines():
        line = line.strip()
        if line == 'byteBuffer.getShort();':
            # The size field, consumed by the object itself.
            continue
        r = READ_RE.match(line)
        if not r:
            return None, 'unrecognised read: %s' % line
        reader = READERS.get(r.group(2))
        if reader is None:
            return None, 'unknown reader: %s' % r.group(2)
        fields.append({'name': r.group(1), 'kind': reader})

    # The writer must mirror the reader; a mismatch means the layout was
    # misread and the emitted codec would be wrong.
    writer_order = []
    tb = TOBYTES_RE.search(src)
    if tb:
        for w in WRITE_RE.finditer(tb.group(1)):
            kind = WRITERS.get(w.group(1))
            if kind:
                writer_order.append({'name': w.group(2), 'kind': kind})

    ds = DATASIZE_RE.search(src)
    data_size = int(ds.group(1)) if ds else None

    class_consts = OrderedDict(
        (m.group(1), int(m.group(2))) for m in CONST_RE.finditer(src)
        if not m.group(1).endswith(LIMIT_SUFFIXES))
    limits = OrderedDict(
        (m.group(1), int(m.group(2))) for m in CONST_RE.finditer(src)
        if m.group(1).endswith(LIMIT_SUFFIXES))

    return {
        'id': type_id,
        'class': class_name,
        'type_name': type_names.get(type_id),
        'fields': fields,
        'writer_fields': writer_order,
        'data_size': data_size,
        'class_consts': class_consts,
        'limits': limits,
    }, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('jadx_sources', help='jadx output .../sources directory')
    ap.add_argument('-o', '--output', default='wpp.json')
    args = ap.parse_args()

    gen = os.path.join(args.jadx_sources, 'com/withings/comm/wpp/generated')
    wpp_java = os.path.join(gen, 'Wpp.java')
    if not os.path.isfile(wpp_java):
        sys.exit('Wpp.java not found under %s' % gen)

    consts = parse_wpp_constants(wpp_java)
    commands = OrderedDict(
        sorted(((v & 0xFFFF, k) for k, v in consts.items() if k.startswith('CMD_')),
               key=lambda kv: kv[0]))
    type_names = OrderedDict(
        sorted(((v & 0xFFFF, k) for k, v in consts.items() if k.startswith('TYPE_')),
               key=lambda kv: kv[0]))

    objects = []
    problems = []
    obj_dir = os.path.join(gen, 'object')
    for name in sorted(os.listdir(obj_dir)):
        if not name.endswith('.java'):
            continue
        obj, err = parse_object_class(os.path.join(obj_dir, name), consts, type_names)
        if err:
            problems.append({'class': name[:-5], 'error': err})
            continue
        objects.append(obj)

    # Enum assignment needs to know which names are object types, so it runs
    # once every class has been read.
    object_type_names = {obj['type_name'] for obj in objects if obj['type_name']}
    for obj in objects:
        class_consts = obj.pop('class_consts')
        for name, value in globals_for_type(consts, obj['type_name'], object_type_names).items():
            class_consts.setdefault(name, value)
        obj['enums'], obj['unplaced_consts'] = assign_enums(
            obj['fields'], class_consts, obj['class'])

    seen = {}
    for obj in objects:
        if obj['id'] in seen:
            problems.append({'class': obj['class'],
                             'error': 'type id %d also used by %s' % (obj['id'], seen[obj['id']])})
        seen[obj['id']] = obj['class']

    for obj in objects:
        if obj['writer_fields'] and obj['writer_fields'] != obj['fields']:
            problems.append({'class': obj['class'], 'error': 'reader/writer layout mismatch'})

    objects.sort(key=lambda o: o['id'])
    ir = {
        'commands': OrderedDict((str(k), v) for k, v in commands.items()),
        'type_names': OrderedDict((str(k), v) for k, v in type_names.items()),
        'constants': consts,
        'objects': objects,
        'problems': problems,
    }
    with open(args.output, 'w') as fh:
        json.dump(ir, fh, indent=1)

    print('commands: %d' % len(commands))
    print('objects:  %d' % len(objects))
    print('variable-size objects: %d' % sum(1 for o in objects if o['data_size'] is None))
    print('objects with enums: %d' % sum(1 for o in objects if o['enums']))
    print('problems: %d' % len(problems))
    for p in problems:
        print('  %s: %s' % (p['class'], p['error']))


if __name__ == '__main__':
    main()
