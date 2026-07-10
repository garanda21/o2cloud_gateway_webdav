"""Tests del overlay del store contra un fake que simula el contrato real de
Movistar (Funambol sapi), medido empíricamente:

- la respuesta del upload trae el id; el listado de carpeta tarda en reflejarlo
- subir un nombre existente SIN media_id crea un duplicado "nombre (1).ext"
- subir CON media_id sobrescribe in-place (mismo id)
- delete/rename/overwrite sobre media sin validar devuelven MED-1017 (~4-10s)
- tras borrar, el listado remoto puede seguir mostrando el archivo un rato
"""
from __future__ import annotations

import asyncio
import re

from o2gateway.o2.api import O2Item
from o2gateway.o2.store import O2CloudFileStore
from o2gateway.operations.errors import CloudMediaNotValidated
from o2gateway.persistence.db import Database
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.settings import Settings


class FakeMovistarApi:
    """Simula sapi: ids en la respuesta del upload, dedupe por nombre, ventana
    de validación controlada por el test y lag de listado controlado por el test."""

    def __init__(self):
        self.files: dict[str, O2Item] = {}
        self.contents: dict[str, bytes] = {}
        self.visible: set[str] = set()
        self.unvalidated: set[str] = set()
        self.upload_calls: list[dict] = []
        self.trash_calls: list[str] = []
        self._sequence = 0

    def _next_id(self) -> str:
        self._sequence += 1
        return "m%d" % self._sequence

    def seed(self, name: str, content: bytes, *, visible: bool = True, validated: bool = True) -> O2Item:
        item = O2Item(self._next_id(), name, "root", False, size=len(content))
        self.files[item.id] = item
        self.contents[item.id] = content
        if visible:
            self.visible.add(item.id)
        if not validated:
            self.unvalidated.add(item.id)
        return item

    def settle(self) -> None:
        """El remoto se pone al día: todo validado y visible en el listado."""
        self.unvalidated.clear()
        self.visible = set(self.files)

    async def root_folder(self):
        return O2Item("root", "", None, True)

    async def list_folder(self, folder_id):
        return [item for item_id, item in self.files.items() if item_id in self.visible]

    async def upload_file(self, parent_folder_id, name, local_path, *, media_id=None):
        with open(local_path, "rb") as handle:
            content = handle.read()
        self.upload_calls.append({"name": name, "media_id": media_id, "size": len(content)})
        if media_id is not None:
            if media_id in self.unvalidated:
                raise CloudMediaNotValidated("MED-1017")
            assert media_id in self.files, "overwrite por id de un media inexistente"
            updated = O2Item(media_id, name, parent_folder_id, False, size=len(content))
            self.files[media_id] = updated
            self.contents[media_id] = content
            self.unvalidated.add(media_id)
            return updated
        final_name = name
        taken = {item.name for item in self.files.values()}
        counter = 1
        while final_name in taken:
            stem, dot, ext = name.rpartition(".")
            final_name = "%s (%d).%s" % (stem, counter, ext) if dot else "%s (%d)" % (name, counter)
            counter += 1
        item = O2Item(self._next_id(), final_name, parent_folder_id, False, size=len(content))
        self.files[item.id] = item
        self.contents[item.id] = content
        self.unvalidated.add(item.id)
        return item

    async def download(self, item, byte_range=None):
        content = self.contents[item.id]
        if byte_range is not None:
            start, end = byte_range
            content = content[start : (end + 1) if end is not None else None]
        yield content

    async def move_to_trash(self, item):
        if item.id in self.unvalidated:
            raise CloudMediaNotValidated("MED-1017")
        self.trash_calls.append(item.id)
        self.files.pop(item.id, None)
        self.contents.pop(item.id, None)
        # El listado remoto sigue mostrando el archivo hasta settle().

    async def rename_or_move(self, item, new_name, parent_folder_id):
        if item.id in self.unvalidated:
            raise CloudMediaNotValidated("MED-1017")
        current = self.files[item.id]
        self.files[item.id] = O2Item(item.id, new_name, parent_folder_id, False, size=current.size)

    async def find_child_with_retries(self, parent_folder_id, name, is_folder, expected_size=None, expected_id=None, *, attempts=8, delay_seconds=2.5):
        for item in self.files.values():
            if item.name == name and (expected_size is None or item.size == expected_size):
                return item
        return None


async def build(tmp_path, **overrides):
    db = Database(str(tmp_path / "gateway.db"))
    await db.initialize()
    cache = MetadataCache(db, ttl_seconds=20, negative_ttl_seconds=5)
    settings = Settings(
        cache_dir=str(tmp_path / "cache"),
        upload_recent_cache_max_file_mb=1,
        upload_recent_cache_ttl_seconds=900,
        delete_tombstone_ttl_seconds=300,
        upload_overwrite_retry_seconds=8.0,
        **overrides,
    )
    api = FakeMovistarApi()
    store = O2CloudFileStore(api, cache, settings)  # type: ignore[arg-type]
    return api, store


async def drain(store):
    while store._background_tasks:
        await asyncio.gather(*list(store._background_tasks), return_exceptions=True)


async def test_finder_copy_sequence_placeholder_then_content(tmp_path):
    api, store = await build(tmp_path)

    # 1) PUT de 0 bytes (placeholder de Finder): jamás se sube al proveedor.
    placeholder = tmp_path / "placeholder"
    placeholder.write_bytes(b"")
    metadata = await store.upload("/informe.txt", str(placeholder))
    assert metadata.size == 0
    assert api.upload_calls == []
    assert (await store.get_metadata("/informe.txt")).size == 0
    assert [item.name for item in await store.list("/")] == ["informe.txt"]

    # 2) Segundo PUT con el contenido: una única subida, sin media_id (no existe remoto).
    source = tmp_path / "contenido"
    source.write_bytes(b"contenido real del informe")
    metadata = await store.upload("/informe.txt", str(source))
    assert metadata.size == len(b"contenido real del informe")
    assert api.upload_calls == [{"name": "informe.txt", "media_id": None, "size": 26}]

    # 3) Lecturas servidas desde el overlay aunque el listado remoto aún no lo muestre.
    chunks = []
    async for chunk in store.open_read("/informe.txt", (0, 8)):
        chunks.append(chunk)
    assert b"".join(chunks) == b"contenido"
    listed = await store.list("/")
    assert [(item.name, item.size) for item in listed] == [("informe.txt", 26)]

    # 4) El listado remoto se pone al día: el overlay se retira sin duplicados.
    api.settle()
    listed = await store.list("/")
    assert [(item.name, item.size) for item in listed] == [("informe.txt", 26)]
    assert store._overlay == {}


async def test_placeholder_delete_never_touches_remote(tmp_path):
    api, store = await build(tmp_path)
    placeholder = tmp_path / "placeholder"
    placeholder.write_bytes(b"")
    await store.upload("/borrador.txt", str(placeholder))

    await store.delete("/borrador.txt")
    await drain(store)

    assert api.upload_calls == []
    assert api.trash_calls == []
    assert await store.get_metadata("/borrador.txt") is None
    assert await store.list("/") == []


async def test_overwrite_existing_remote_uses_media_id(tmp_path):
    api, store = await build(tmp_path)
    existing = api.seed("notas.txt", b"version antigua")

    source = tmp_path / "nueva"
    source.write_bytes(b"version nueva mas larga")
    metadata = await store.upload("/notas.txt", str(source))

    assert api.upload_calls == [{"name": "notas.txt", "media_id": existing.id, "size": 23}]
    assert metadata.id == existing.id
    # Sin duplicados "notas (1).txt" en el servidor.
    assert sorted(item.name for item in api.files.values()) == ["notas.txt"]


async def test_overwrite_during_validation_window_retries(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0.01))
    api, store = await build(tmp_path)

    first = tmp_path / "v1"
    first.write_bytes(b"version 1")
    await store.upload("/doc.txt", str(first))
    # El archivo recién subido sigue en ventana de validación (MED-1017).

    second = tmp_path / "v2"
    second.write_bytes(b"version 2 distinta")

    async def validate_soon():
        await real_sleep(0.05)
        api.unvalidated.clear()

    task = asyncio.create_task(validate_soon())
    metadata = await store.upload("/doc.txt", str(second))
    await task

    assert len(api.upload_calls) >= 3  # subida inicial + al menos un MED-1017 + éxito
    assert metadata.size == len(b"version 2 distinta")
    assert sorted(item.name for item in api.files.values()) == ["doc.txt"]


async def test_delete_recent_upload_defers_until_validated(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0.01))
    api, store = await build(tmp_path)

    source = tmp_path / "src"
    source.write_bytes(b"contenido")
    await store.upload("/temporal.txt", str(source))
    uploaded_id = api.upload_calls[0] and next(iter(api.files))

    # DELETE en ventana de validación: aceptado localmente, diferido en remoto.
    await store.delete("/temporal.txt")
    assert await store.get_metadata("/temporal.txt") is None
    assert await store.list("/") == []

    await real_sleep(0.05)
    api.unvalidated.clear()
    await drain(store)
    assert api.trash_calls == [uploaded_id]

    # El remoto ya no lo lista: el tombstone se retira y el árbol queda limpio.
    api.settle()
    assert await store.list("/") == []
    assert store._overlay == {}


async def test_deleted_file_stays_hidden_while_remote_lists_it(tmp_path):
    api, store = await build(tmp_path)
    api.seed("antiguo.txt", b"contenido viejo")

    assert [item.name for item in await store.list("/")] == ["antiguo.txt"]
    await store.delete("/antiguo.txt")
    await drain(store)

    # El remoto borró (move_to_trash llamado) pero su listado sigue mostrándolo.
    assert len(api.trash_calls) == 1
    assert await store.list("/") == []
    assert await store.get_metadata("/antiguo.txt") is None

    api.settle()
    assert await store.list("/") == []
    assert store._overlay == {}


async def test_move_recent_upload_renames_remotely_by_id(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0.01))
    api, store = await build(tmp_path)

    source = tmp_path / "src"
    source.write_bytes(b"contenido")
    await store.upload("/origen.txt", str(source))
    api.unvalidated.clear()

    metadata = await store.move("/origen.txt", "/destino.txt")
    await drain(store)

    assert metadata.name == "destino.txt"
    assert await store.get_metadata("/origen.txt") is None
    assert (await store.get_metadata("/destino.txt")).size == len(b"contenido")
    assert sorted(item.name for item in api.files.values()) == ["destino.txt"]


async def test_second_copy_after_failed_first_does_not_duplicate(tmp_path):
    """Regresión del bug de campo: reintento de copia tras un intento fallido no
    debe dejar duplicados "nombre (1).txt" en el servidor."""
    api, store = await build(tmp_path)

    # Primer intento: placeholder que Finder borra al abortar la copia.
    placeholder = tmp_path / "p1"
    placeholder.write_bytes(b"")
    await store.upload("/gio-test.txt", str(placeholder))
    await store.delete("/gio-test.txt")
    await drain(store)

    # Segundo intento completo: placeholder + contenido.
    placeholder2 = tmp_path / "p2"
    placeholder2.write_bytes(b"")
    await store.upload("/gio-test.txt", str(placeholder2))
    source = tmp_path / "c"
    source.write_bytes(b"contenido definitivo")
    await store.upload("/gio-test.txt", str(source))

    assert [item.name for item in api.files.values()] == ["gio-test.txt"]
    assert not any(re.search(r"\(\d+\)", item.name) for item in api.files.values())
