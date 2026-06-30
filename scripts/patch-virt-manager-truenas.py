#!/usr/bin/env python3
"""Apply SubVirt TrueNAS pool support to a virt-manager source tree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


LABEL_LINE = '    "truenas": _("TrueNAS managed storage"),\n'
CREATE_LINE = '                "truenas",\n'
TRANSPORT_ROW = """                    <child>
                      <object class="GtkLabel" id="pool-truenas-transport-label">
                        <property name="visible">True</property>
                        <property name="can-focus">False</property>
                        <property name="halign">end</property>
                        <property name="label" translatable="yes">_Transport:</property>
                        <property name="use-underline">True</property>
                        <property name="mnemonic-widget">pool-truenas-transport</property>
                      </object>
                      <packing>
                        <property name="left-attach">0</property>
                        <property name="top-attach">9</property>
                      </packing>
                    </child>
                    <child>
                      <object class="GtkComboBox" id="pool-truenas-transport">
                        <property name="visible">True</property>
                        <property name="can-focus">False</property>
                        <property name="halign">start</property>
                      </object>
                      <packing>
                        <property name="left-attach">1</property>
                        <property name="top-attach">9</property>
                      </packing>
                    </child>
"""


def replace_once(text: str, old: str, new: str, path: Path) -> str:
    if old not in text:
        raise SystemExit(f"could not find expected text in {path}: {old!r}")
    return text.replace(old, new, 1)


def patch_storagepool(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    if '"truenas": _("TrueNAS managed storage")' not in text:
        text = replace_once(text, '    StoragePool.TYPE_ZFS: _("ZFS Pool"),\n',
                            '    StoragePool.TYPE_ZFS: _("ZFS Pool"),\n' + LABEL_LINE, path)

    if re.search(r'^[ \t]*"truenas",\s*$', text, re.M) is None:
        text = replace_once(text, '                StoragePool.TYPE_ZFS,\n',
                            '                StoragePool.TYPE_ZFS,\n' + CREATE_LINE, path)

    path.write_text(text, encoding="utf-8")


def patch_storage(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    if 'TYPE_TRUENAS' not in text:
        text = re.sub(r'^(\s*TYPE_ZFS\s*=\s*"zfs"\n)', r'\1    TYPE_TRUENAS = "truenas"\n', text, count=1, flags=re.M)
        if 'TYPE_TRUENAS' not in text:
            raise SystemExit(f"could not add TYPE_TRUENAS in {path}")

    if '_DEFAULT_TRUENAS_TARGET' not in text:
        text = replace_once(text, '_DEFAULT_MPATH_TARGET = "/dev/mapper"\n',
                            '_DEFAULT_MPATH_TARGET = "/dev/mapper"\n_DEFAULT_TRUENAS_TARGET = "/dev/disk/by-id"\n', path)

    if 'self.type == self.TYPE_TRUENAS' not in text:
        text = replace_once(text, '        if self.type == self.TYPE_MPATH:\n            return _DEFAULT_MPATH_TARGET\n',
                            '        if self.type == self.TYPE_MPATH:\n            return _DEFAULT_MPATH_TARGET\n        if self.type == self.TYPE_TRUENAS:\n            return _DEFAULT_TRUENAS_TARGET\n', path)

    if 'source_protocol' not in text:
        text = text.replace('"source_name", "target_path"', '"source_name", "source_protocol", "target_path"')
        text = text.replace('''"source_name",
        "target_path",''', '''"source_name",
        "source_protocol",
        "target_path",''')
        text = replace_once(text, '    source_name = XMLProperty("./source/name")\n',
                            '    source_name = XMLProperty("./source/name")\n    source_protocol = XMLProperty("./source/protocol/@type")\n', path)

    text = re.sub(
        r'(def supports_target_path\(self\):\n\s*return self\.type in \[)(.*?)(\n\s*\])',
        lambda m: m.group(0) if 'TYPE_TRUENAS' in m.group(2) else m.group(1) + m.group(2).rstrip().rstrip(',') + ',\n            self.TYPE_TRUENAS,' + m.group(3),
        text,
        count=1,
        flags=re.S,
    )
    target_match = re.search(r'def supports_target_path\(self\):.*?def supports_source_name', text, re.S)
    if target_match and 'TYPE_TRUENAS' not in target_match.group(0):
        text = replace_once(text, 'self.TYPE_SCSI, self.TYPE_MPATH]',
                            'self.TYPE_SCSI, self.TYPE_MPATH, self.TYPE_TRUENAS]', path)
    text = re.sub(
        r'(def supports_source_name\(self\):\n\s*return self\.type in \[)(.*?)(\])',
        lambda m: m.group(0) if 'TYPE_TRUENAS' in m.group(2) else m.group(1) + m.group(2).rstrip() + ', self.TYPE_TRUENAS' + m.group(3),
        text,
        count=1,
        flags=re.S,
    )
    text = re.sub(
        r'(self\.type == StoragePool\.TYPE_ZFS)(\))',
        r'\1 or\n            self.type == StoragePool.TYPE_TRUENAS\2',
        text,
        count=1,
    )

    validate_block = '''
        if self.type == self.TYPE_TRUENAS:
            if not self.source_name:
                raise ValueError(_("TrueNAS source pool name is required."))
            if self.source_protocol not in ["iscsi", "nvmeof"]:
                raise ValueError(_("TrueNAS transport must be iSCSI or NVMe-oF."))
'''
    if 'TrueNAS source pool name is required.' not in text:
        text = replace_once(text, '        self.validate_name(self.conn, self.name)\n',
                            '        self.validate_name(self.conn, self.name)\n' + validate_block, path)

    path.write_text(text, encoding="utf-8")


def patch_createpool(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    if 'pool-truenas-transport' not in text:
        needle = '''        for f in ["auto"]:
            format_model.append([f, f])
'''
        replacement = needle + '''
        transport_list = self.widget("pool-truenas-transport")
        transport_model = Gtk.ListStore(str, str)
        transport_list.set_model(transport_model)
        uiutil.init_combo_text_column(transport_list, 1)
        transport_model.append(["iscsi", "iSCSI"])
        transport_model.append(["nvmeof", "NVMe-oF"])
'''
        text = replace_once(text, needle, replacement, path)

    if 'self.widget("pool-truenas-transport").set_active(-1)' not in text:
        text = replace_once(text, '        self.widget("pool-format").set_active(0)\n',
                            '        self.widget("pool-format").set_active(0)\n        self.widget("pool-truenas-transport").set_active(-1)\n', path)

    if 'is_truenas = pool.type == StoragePool.TYPE_TRUENAS' not in text:
        text = replace_once(text, '        is_lvm = pool.type == StoragePool.TYPE_LOGICAL\n',
                            '        is_lvm = pool.type == StoragePool.TYPE_LOGICAL\n        is_truenas = pool.type == StoragePool.TYPE_TRUENAS\n', path)
        text = replace_once(text, '        show_row("pool-source-name", src_name)\n',
                            '        show_row("pool-source-name", src_name)\n        show_row("pool-truenas-transport", is_truenas)\n', path)
        label_marker = '        self.widget("pool-source-name-label").set_label(\n'
        src_label_marker = '        src_label = _("_Source Path:")'
        label_start = text.find(label_marker)
        label_end = text.find(src_label_marker, label_start)
        if label_start < 0 or label_end < 0:
            raise SystemExit(f"could not patch source-name label in {path}")
        label_replacement = (
            '        self.widget("pool-source-name-label").set_label(\n'
            '                is_lvm and _("Volg_roup Name:") or\n'
            '                is_truenas and _("TrueNAS _Pool:") or _("Sou_rce Name:"))\n\n'
        )
        text = text[:label_start] + label_replacement + text[label_end:]
        text = replace_once(text, '        self._populate_pool_sources()\n',
                            '        if is_truenas:\n            self.widget("pool-truenas-transport").set_active(-1)\n\n        self._populate_pool_sources()\n', path)

    if 'def _get_config_truenas_transport(self):' not in text:
        text = replace_once(text, '    def _get_config_iqn(self):\n        return self._get_visible_text("pool-iqn")\n',
                            '    def _get_config_iqn(self):\n        return self._get_visible_text("pool-iqn")\n\n    def _get_config_truenas_transport(self):\n        return uiutil.get_list_selection(self.widget("pool-truenas-transport"))\n', path)

    if 'transport = self._get_config_truenas_transport()' not in text:
        text = replace_once(text, '        source_name = self._get_config_source_name()\n',
                            '        source_name = self._get_config_source_name()\n        transport = self._get_config_truenas_transport()\n', path)
        text = replace_once(text, '        if source_name:\n            pool.source_name = source_name\n',
                            '        if source_name:\n            pool.source_name = source_name\n        if pool.type == StoragePool.TYPE_TRUENAS:\n            pool.source_protocol = transport\n', path)

    if 'pooltype == StoragePool.TYPE_TRUENAS' not in text:
        source_needle = (
            '        elif pooltype == StoragePool.TYPE_LOGICAL:\n'
            '            vglist = self._list_pool_sources(pooltype)\n'
            '            entry_list = [[v, v] for v in vglist]\n'
            '            use_list = name_list\n'
            '\n'
            '        else:\n'
            '            return\n'
        )
        source_replacement = (
            '        elif pooltype == StoragePool.TYPE_LOGICAL:\n'
            '            vglist = self._list_pool_sources(pooltype)\n'
            '            entry_list = [[v, v] for v in vglist]\n'
            '            use_list = name_list\n'
            '\n'
            '        elif pooltype == StoragePool.TYPE_TRUENAS:\n'
            '            pool_list = self._list_pool_sources(pooltype)\n'
            '            entry_list = [[p, p] for p in pool_list]\n'
            '            use_list = name_list\n'
            '\n'
            '        else:\n'
            '            return\n'
        )
        text = replace_once(text, source_needle, source_replacement, path)

    path.write_text(text, encoding="utf-8")


def patch_ui(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if 'id="pool-truenas-transport"' not in text:
        text = text.replace('<!-- n-columns=3 n-rows=9 -->', '<!-- n-columns=3 n-rows=10 -->', 1)
        needle = '                    <child>\n                      <placeholder/>\n                    </child>\n'
        if needle not in text:
            raise SystemExit(f"could not find placeholder insertion point in {path}")
        text = text.replace(needle, TRANSPORT_ROW + needle, 1)
    path.write_text(text, encoding="utf-8")


def patch_tests(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if "def testTrueNASPoolXML" in text:
        return
    test_block = """

def testTrueNASPoolXML():
    conn = utils.URIs.open_testdefault_cached()
    pool_inst = StoragePool(conn)
    pool_inst.name = "pool-truenas"
    pool_inst.type = StoragePool.TYPE_TRUENAS
    pool_inst.source_name = "hot1"
    pool_inst.source_protocol = "nvmeof"
    pool_inst.target_path = pool_inst.default_target_path()
    pool_inst.validate_name = lambda *_args, **_kwargs: None
    pool_inst.validate()
    xml = pool_inst.get_xml()
    assert "<name>hot1</name>" in xml
    assert '<protocol type="nvmeof"/>' in xml
    assert "<path>/dev/disk/by-id</path>" in xml
"""
    marker = "def testMisc():\n"
    if marker not in text:
        raise SystemExit(f"could not find test insertion point in {path}")
    text = text.replace(marker, test_block + "\n" + marker, 1)
    path.write_text(text, encoding="utf-8")


def patch_source(source_root: Path) -> None:
    patch_storagepool(source_root / "virtManager" / "object" / "storagepool.py")
    patch_storage(source_root / "virtinst" / "storage.py")
    patch_createpool(source_root / "virtManager" / "createpool.py")
    patch_ui(source_root / "ui" / "createpool.ui")
    patch_tests(source_root / "tests" / "test_storage.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root")
    args = parser.parse_args()
    patch_source(Path(args.source_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
