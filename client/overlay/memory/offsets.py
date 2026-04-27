"""Hardcoded Mono runtime offsets for the Unity version Arena currently ships.

Sourced from HackF5.UnitySpy's ``MonoLibraryOffsets.cs``
(``Unity2021_3_2022_3_x64_PE_Offsets``). MTG Arena is a Unity 2021/2022 x64 PE
build using ``mono-2.0-bdwgc.dll``. If Wizards bumps Unity to a major-version
the layout shifts and we need an additional table here — until then a single
set is sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MonoOffsets:
    """Field offsets within Mono's internal C structs."""

    is_64bits: bool

    # _MonoAssembly: ptr to MonoImage*
    assembly_image: int
    # _MonoDomain: ptr to head of domain_assemblies linked list
    referenced_assemblies: int
    # _MonoImage: class_cache hash table head (MonoInternalHashTable)
    image_class_cache: int
    # MonoInternalHashTable: 'size' field (uint32)
    hash_table_size: int
    # MonoInternalHashTable: 'table' (ptr to bucket array)
    hash_table_table: int

    # _MonoClassField struct size (3 ptr + int + padding for x64)
    type_definition_field_size: int
    # _MonoClass: bit_fields (size_inited / valuetype / enumtype, etc.)
    type_definition_bit_fields: int
    # _MonoClass: class_kind (1 byte)
    type_definition_class_kind: int
    # _MonoClass: parent (MonoClass*)
    type_definition_parent: int
    # _MonoClass: nested_in (MonoClass*)
    type_definition_nested_in: int
    # _MonoClass: name (const char*)
    type_definition_name: int
    # _MonoClass: name_space (const char*)
    type_definition_namespace: int
    # _MonoClass: vtable_size (int)
    type_definition_vtable_size: int
    # _MonoClass: sizes (instance_size / class_size / element_size / generic_param_token)
    type_definition_size: int
    # _MonoClass: fields (MonoClassField*)
    type_definition_fields: int
    # _MonoClass: _byval_arg (MonoType, embedded value)
    type_definition_byval_arg: int
    # _MonoClass: runtime_info (MonoClassRuntimeInfo*)
    type_definition_runtime_info: int

    # MonoClassDef: field_count (int)
    type_definition_field_count: int
    # MonoClassDef: next_class_cache (MonoClass*)
    type_definition_next_class_cache: int

    # MonoClassGtd: generic_container (MonoGenericContainer*)
    type_definition_generic_container: int
    # MonoClassGenericInst: mono_generic_class (MonoGenericClass*)
    type_definition_mono_generic_class: int

    # MonoClassRuntimeInfo: domain_vtables (variable-length array of MonoVTable*)
    type_definition_runtime_info_domain_vtables: int

    # MonoVTable: 'vtable' (variable-length, holds static field pointer at end)
    vtable: int

    @property
    def size_of_ptr(self) -> int:
        return 8 if self.is_64bits else 4


# Unity 2021.3 / 2022.3 x64 PE — Arena's current family.
#
# NOTE: The C# source HackF5.UnitySpy/Offsets/MonoLibraryOffsets.cs has
# inline comments next to each arithmetic expression that don't always
# match the computed value (e.g. ``// 0xE0`` next to ``0xa4 + 0x34 + 0x18
# + 0x10`` which sums to ``0x100``). The values below are the **computed**
# results of those expressions, not the (sometimes incorrect) comments.
UNITY_2021_3_2022_3_X64 = MonoOffsets(
    is_64bits=True,
    assembly_image=0x60,                         # 0x10 + 0x50
    referenced_assemblies=0xA0,
    image_class_cache=0x4D0,
    hash_table_size=0x18,                        # 0xc + 0xc
    hash_table_table=0x20,                       # 0x14 + 0xc
    type_definition_field_size=0x20,
    type_definition_bit_fields=0x20,             # 0x14 + 0xc
    type_definition_class_kind=0x1B,
    type_definition_parent=0x30,
    type_definition_nested_in=0x38,
    type_definition_name=0x48,
    type_definition_namespace=0x50,
    type_definition_vtable_size=0x5C,
    type_definition_size=0x90,
    type_definition_fields=0x98,
    type_definition_byval_arg=0xB8,
    type_definition_runtime_info=0xD0,           # 0x84 + 0x34 + 0x18
    type_definition_field_count=0x100,           # 0xa4 + 0x34 + 0x18 + 0x10
    type_definition_next_class_cache=0x108,      # 0xa8 + 0x34 + 0x18 + 0x10 + 0x4
    type_definition_generic_container=0x110,
    type_definition_mono_generic_class=0xF0,     # 0x94 + 0x34 + 0x18 + 0x10
    type_definition_runtime_info_domain_vtables=0x8,  # 0x2 + 0x6
    vtable=0x48,                                 # 0x28 + 0x8 + 0x8 + 0x10
)


def select_offsets(file_version: str | None) -> MonoOffsets | None:
    """Pick an offset table for the running ``mono-2.0-bdwgc.dll`` version.

    Args:
        file_version: PE version string (``X.Y.Z.W``) read from the on-disk
            mono module.

    Returns:
        The matching offset table, or ``None`` if the version is unsupported.
    """
    # Unity 2021/2022 ship Mono 2.0.0.x. We accept any 2.0.0.x today.
    if not file_version:
        return UNITY_2021_3_2022_3_X64
    parts = file_version.split(".")
    if len(parts) >= 2 and parts[0] == "2" and parts[1] == "0":
        return UNITY_2021_3_2022_3_X64
    return None
