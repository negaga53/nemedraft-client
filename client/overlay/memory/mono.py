"""Minimal Mono runtime walker.

Direct port of the parts of HackF5.UnitySpy we actually need:

* :class:`AssemblyImage` — enumerates classes from the image's class cache.
* :class:`ClassDefinition` — reads name / namespace / parent / fields /
  static vtable from a ``_MonoClass`` pointer.
* :class:`FieldDefinition` — reads name / offset / type code from a
  ``_MonoClassField`` pointer.
* :class:`ObjectInstance` — convenience wrapper around an instance pointer
  for chained field reads.

Field reads cover only the type codes the overlay walks: ``I4`` (int32),
``STRING``, and ``CLASS`` (object pointer). Anything else raises
:class:`MonoFieldMissing`.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from .exceptions import MonoBootstrapFailed, MonoFieldMissing
from .offsets import MonoOffsets
from .reader import ProcessReader

logger = logging.getLogger(__name__)


# Mono type codes we read. See HackF5.UnitySpy/Detail/TypeCode.cs.
_TYPE_I4 = 0x08
_TYPE_U4 = 0x09
_TYPE_STRING = 0x0E
_TYPE_VALUETYPE = 0x11
_TYPE_CLASS = 0x12
_TYPE_GENERICINST = 0x15
_TYPE_I = 0x18
_TYPE_U = 0x19
_TYPE_OBJECT = 0x1C
_TYPE_SZARRAY = 0x1D
_TYPE_ENUM = 0x55

_INT_TYPE_CODES = {_TYPE_I4, _TYPE_U4, _TYPE_I, _TYPE_U, _TYPE_ENUM}
_OBJECT_TYPE_CODES = {_TYPE_CLASS, _TYPE_GENERICINST, _TYPE_OBJECT}

# Field attribute flags from MonoFieldAttribute (Mono headers).
_FIELD_ATTR_STATIC = 0x10
_FIELD_ATTR_LITERAL = 0x40


class FieldDefinition:
    """A ``_MonoClassField`` entry on a Mono class."""

    __slots__ = ("name", "offset", "type_code", "is_static", "is_literal")

    def __init__(
        self,
        name: str,
        offset: int,
        type_code: int,
        is_static: bool,
        is_literal: bool,
    ) -> None:
        self.name = name
        self.offset = offset
        self.type_code = type_code
        self.is_static = is_static
        self.is_literal = is_literal

    def __repr__(self) -> str:
        return (
            f"FieldDefinition(name={self.name!r}, offset={self.offset:#x}, "
            f"type=0x{self.type_code:02x}, static={self.is_static})"
        )


class ClassDefinition:
    """A ``_MonoClass`` entry: name, fields, static vtable.

    Args:
        image: The owning :class:`AssemblyImage`.
        address: Pointer to the ``_MonoClass`` struct in MTGA's address space.
    """

    __slots__ = (
        "image",
        "address",
        "name",
        "namespace",
        "parent_address",
        "_fields",
        "_static_fields_addr",
        "_full_name_cache",
    )

    def __init__(self, image: "AssemblyImage", address: int) -> None:
        self.image = image
        self.address = address
        reader = image.reader
        offsets = image.offsets
        self.name = reader.read_ascii_string_at_ptr(address + offsets.type_definition_name)
        self.namespace = reader.read_ascii_string_at_ptr(address + offsets.type_definition_namespace)
        try:
            self.parent_address = reader.read_ptr(address + offsets.type_definition_parent)
        except Exception:
            self.parent_address = 0
        self._fields: Optional[list[FieldDefinition]] = None
        self._static_fields_addr: Optional[int] = None
        self._full_name_cache: Optional[str] = None

    @property
    def full_name(self) -> str:
        """Return the fully qualified ``Namespace.Name`` form."""
        if self._full_name_cache is None:
            if self.namespace:
                self._full_name_cache = f"{self.namespace}.{self.name}"
            else:
                self._full_name_cache = self.name
        return self._full_name_cache

    def fields(self) -> list[FieldDefinition]:
        """Return all fields declared on this class plus inherited ones."""
        if self._fields is not None:
            return self._fields
        reader = self.image.reader
        offsets = self.image.offsets
        try:
            field_count = reader.read_int32(self.address + offsets.type_definition_field_count)
            first_field = reader.read_ptr(self.address + offsets.type_definition_fields)
        except Exception:
            self._fields = []
            return self._fields

        own: list[FieldDefinition] = []
        if first_field != 0 and field_count > 0:
            for index in range(field_count):
                field_address = first_field + (index * offsets.type_definition_field_size)
                try:
                    type_ptr = reader.read_ptr(field_address)
                    if type_ptr == 0:
                        break
                    name_ptr = reader.read_ptr(field_address + reader.size_of_ptr)
                    name = reader.read_ascii_string(name_ptr)
                    instance_offset = reader.read_int32(field_address + (reader.size_of_ptr * 3))
                    # Read the embedded MonoType: data (ptr), attrs (uint32).
                    attrs = reader.read_uint32(type_ptr + reader.size_of_ptr)
                    type_code = (attrs >> 16) & 0xFF
                    is_static = bool(attrs & _FIELD_ATTR_STATIC)
                    is_literal = bool(attrs & _FIELD_ATTR_LITERAL)
                except Exception:
                    logger.debug("Failed to parse field at %#x", field_address, exc_info=True)
                    continue
                if not name:
                    continue
                own.append(
                    FieldDefinition(
                        name=name,
                        offset=instance_offset,
                        type_code=type_code,
                        is_static=is_static,
                        is_literal=is_literal,
                    )
                )

        # Inherit parent fields. Field offsets remain valid because Mono
        # lays parent fields first inside an instance.
        if self.parent_address:
            parent = self.image.get_class_by_address(self.parent_address)
            if parent is not None:
                own.extend(parent.fields())

        self._fields = own
        return self._fields

    def get_field(self, name: str) -> FieldDefinition:
        """Return the named field, raising :class:`MonoFieldMissing` if absent."""
        for field in self.fields():
            if field.name == name:
                return field
        raise MonoFieldMissing(
            f"Field {name!r} not found on class {self.full_name!r}"
        )

    def static_fields_address(self) -> int:
        """Return the address of this class's static-fields blob.

        Mirrors HackF5.UnitySpy's ``GetStaticValue<TValue>`` pointer math:
        chase ``runtime_info`` → first ``domain_vtable`` →
        ``vtable + VTable_offset + size_of_ptr * vtable_size``.

        Returns:
            Absolute address; ``0`` if the class has not been initialised
            yet (no domain vtable).
        """
        if self._static_fields_addr is not None:
            return self._static_fields_addr
        reader = self.image.reader
        offsets = self.image.offsets
        try:
            runtime_info = reader.read_ptr(self.address + offsets.type_definition_runtime_info)
            if runtime_info == 0:
                self._static_fields_addr = 0
                return 0
            vtable = reader.read_ptr(
                runtime_info + offsets.type_definition_runtime_info_domain_vtables
            )
            if vtable == 0:
                self._static_fields_addr = 0
                return 0
            vtable_size = reader.read_int32(self.address + offsets.type_definition_vtable_size)
            blob = reader.read_ptr(
                vtable + offsets.vtable + reader.size_of_ptr * vtable_size
            )
            self._static_fields_addr = blob
            return blob
        except Exception:
            logger.debug("Failed to resolve static fields for %s", self.full_name, exc_info=True)
            self._static_fields_addr = 0
            return 0

    def get_static(self, field_name: str) -> "ObjectInstance | str | int | None":
        """Return a static field's value (only ``CLASS`` / ``STRING`` / ``I4``)."""
        field = self.get_field(field_name)
        if not field.is_static:
            raise MonoFieldMissing(
                f"Field {field_name!r} on {self.full_name!r} is not static"
            )
        blob = self.static_fields_address()
        if blob == 0:
            return None
        return _read_field_value(self.image, blob, field)


class ObjectInstance:
    """A live class instance pointer — supports chained field reads."""

    __slots__ = ("image", "address")

    def __init__(self, image: "AssemblyImage", address: int) -> None:
        self.image = image
        self.address = address

    def runtime_class(self) -> Optional[ClassDefinition]:
        """Return the runtime ``ClassDefinition`` of this instance.

        Reads ``instance.vtable.class``. Useful for polymorphic checks like
        whether ``CurrentNavContent`` is an ``EventPageContentController``.
        """
        if self.address == 0:
            return None
        try:
            vtable = self.image.reader.read_ptr(self.address)
            if vtable == 0:
                return None
            class_ptr = self.image.reader.read_ptr(vtable)
        except Exception:
            return None
        return self.image.get_class_by_address(class_ptr)

    def get(self, field_name: str) -> "ObjectInstance | str | int | None":
        """Read a field on this instance (only ``CLASS`` / ``STRING`` / ``I4``)."""
        klass = self.runtime_class()
        if klass is None:
            return None
        field = klass.get_field(field_name)
        if field.is_static:
            return klass.get_static(field_name)
        return _read_field_value(self.image, self.address, field)

    def __bool__(self) -> bool:
        return self.address != 0


def _read_field_value(
    image: "AssemblyImage", base_address: int, field: FieldDefinition
) -> "ObjectInstance | str | int | None":
    """Decode a field at ``base_address + field.offset`` per its type code.

    Args:
        image: Owning assembly image.
        base_address: Either an instance address or a static-fields blob
            base.
        field: Descriptor of the field to read.

    Returns:
        ``ObjectInstance`` for class/object types, ``str`` for managed
        strings, ``int`` for primitive ints. ``None`` for null pointers or
        unsupported type codes.
    """
    reader = image.reader
    address = base_address + field.offset
    code = field.type_code
    try:
        if code in _INT_TYPE_CODES:
            return reader.read_int32(address)
        if code == _TYPE_STRING:
            return reader.read_managed_string(address)
        if code in _OBJECT_TYPE_CODES:
            ptr = reader.read_ptr(address)
            if ptr == 0:
                return None
            return ObjectInstance(image, ptr)
    except Exception:
        logger.debug(
            "Failed to read field %s @ %#x (type %#x)",
            field.name, address, code, exc_info=True,
        )
        return None
    logger.debug("Unsupported field type %#x for %s", code, field.name)
    return None


class AssemblyImage:
    """A loaded set of ``_MonoImage`` instances — provides class lookup.

    Modern MTG Arena splits its code across multiple assemblies (``Core``,
    ``SharedClientCore``, ``Assembly-CSharp``, etc.). This class merges
    their class caches so a single ``get_class("WrapperController")`` call
    searches all of them.

    Args:
        reader: Active :class:`ProcessReader`.
        offsets: Mono offset table for the running Unity version.
        image_addresses: Addresses of the ``_MonoImage`` structs to merge,
            in priority order — the first image's classes win on full-name
            collisions.
    """

    __slots__ = (
        "reader",
        "offsets",
        "image_addresses",
        "_classes_by_name",
        "_classes_by_address",
        "_walked",
    )

    def __init__(
        self,
        reader: ProcessReader,
        offsets: MonoOffsets,
        image_addresses: list[int],
    ) -> None:
        if not image_addresses:
            raise ValueError("image_addresses must not be empty")
        self.reader = reader
        self.offsets = offsets
        self.image_addresses = list(image_addresses)
        self._classes_by_name: dict[str, ClassDefinition] = {}
        self._classes_by_address: dict[int, ClassDefinition] = {}
        self._walked = False

    @property
    def address(self) -> int:
        """Return the primary (first) image address — for diagnostics only."""
        return self.image_addresses[0]

    def _ensure_walked(self) -> None:
        if self._walked:
            return
        for image_address in self.image_addresses:
            self._walk_class_cache(image_address)
        self._walked = True

    def _walk_class_cache(self, image_address: int) -> None:
        reader = self.reader
        offsets = self.offsets
        try:
            cache_size = reader.read_uint32(
                image_address + offsets.image_class_cache + offsets.hash_table_size
            )
            cache_table = reader.read_ptr(
                image_address + offsets.image_class_cache + offsets.hash_table_table
            )
        except Exception as exc:
            raise MonoBootstrapFailed(
                f"Cannot read class cache header @ image {image_address:#x}: {exc}"
            ) from exc

        if cache_table == 0 or cache_size == 0 or cache_size > 1 << 24:
            logger.debug(
                "Image %#x has empty class cache (size=%d table=%#x)",
                image_address, cache_size, cache_table,
            )
            return

        ptr_size = reader.size_of_ptr
        registered = 0
        for bucket_index in range(cache_size):
            bucket_addr = cache_table + (bucket_index * ptr_size)
            try:
                klass_ptr = reader.read_ptr(bucket_addr)
            except Exception:
                continue
            while klass_ptr != 0:
                if klass_ptr in self._classes_by_address:
                    break
                try:
                    klass = ClassDefinition(self, klass_ptr)
                except Exception:
                    logger.debug("Failed reading class %#x", klass_ptr, exc_info=True)
                    break
                self._classes_by_address[klass_ptr] = klass
                if klass.name and klass.full_name not in self._classes_by_name:
                    self._classes_by_name[klass.full_name] = klass
                registered += 1
                try:
                    klass_ptr = reader.read_ptr(
                        klass_ptr + offsets.type_definition_next_class_cache
                    )
                except Exception:
                    break

        logger.info(
            "Walked image %#x class cache: %d classes registered",
            image_address, registered,
        )

    def get_class(self, full_name: str) -> Optional[ClassDefinition]:
        """Return a class by ``Namespace.Name`` (or just ``Name`` if root-level)."""
        self._ensure_walked()
        return self._classes_by_name.get(full_name)

    def get_class_by_address(self, address: int) -> Optional[ClassDefinition]:
        """Return a class given its ``_MonoClass*`` address (no full walk)."""
        if address == 0:
            return None
        cached = self._classes_by_address.get(address)
        if cached is not None:
            return cached
        # Lazy-add classes referenced via parent / instance vtable that
        # were not yet seen by the cache walk.
        try:
            klass = ClassDefinition(self, address)
        except Exception:
            return None
        self._classes_by_address[address] = klass
        if klass.name and klass.full_name not in self._classes_by_name:
            self._classes_by_name[klass.full_name] = klass
        return klass

    def classes(self) -> Iterator[ClassDefinition]:
        self._ensure_walked()
        return iter(self._classes_by_address.values())
