#!/usr/bin/env python3
import sqlite3
import shutil
from pathlib import Path
import unicodedata
import re
import csv
from datetime import datetime
from typing import Iterable, Optional
import subprocess

# ================== CONFIGURACIÓN RÁPIDA ==================
DB_PATH   = "/Users/jms/projects/fixWD/db/index.db"    # <-- cámbialo
SRC_DIR = "/Volumes/backupWD8TB/recuperacionWD/restsdk/data/files"
# SRC_DIR   = "/Users/jms/projects/fixWD/files"               # <-- donde están los archivos por id (p.ej. 12345.jpg)
# DST_DIR   = "/Users/jms/projects/fixWD/output"              # <-- donde quieres copiarlos con el nombre 'name'
DST_DIR = "/Volumes/backupWD8TB/recuperacionWD/output"
TABLE     = "Files"                          # tabla
ID_COL    = "id"                             # columna id
NAME_COL  = "name"
CONTENT_ID_COL = "contentID"                           # ID del contenido a buscar
MIME_TYPE_COL = "mimeType"               # columna mimeType (opcional, no usada aquí)
BATCH     = 10_000                           # tamaño de lote para leer de SQLite
GLOB_EXTS = ["*"]                            # extensiones a buscar: ["jpg","png","mp4"] o ["*"] para cualquiera
DRY_RUN   = False                            # True: no copia, solo simula y registra
OVERWRITE = False                            # False: si existe destino, no pisa; crea sufijos -1, -2, ...
REPORT_CSV = f"copy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
# ===========================================================

# ---- Manejo de textos con codificaciones raras ----
def smart_text_factory(b):
    if b is None:
        return None
    if isinstance(b, bytes):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")
    return b

def open_sqlite_readonly(path: str) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.text_factory = smart_text_factory
    # PRAGMAs seguros para lectura
    try:
        con.execute("PRAGMA journal_mode=OFF;")
        con.execute("PRAGMA synchronous=OFF;")
        con.execute("PRAGMA temp_store=MEMORY;")
    except sqlite3.DatabaseError:
        pass
    return con

def stream_rows(con: sqlite3.Connection, sql: str, fetch_size: int = 10_000) -> Iterable[sqlite3.Row]:
    cur = con.cursor()
    cur.execute(sql)
    while True:
        rows = cur.fetchmany(fetch_size)
        if not rows:
            break
        for r in rows:
            yield r
    cur.close()

# ---- Normalización de nombres destino ----
_ILLEGAL_RE = re.compile(r'[\\/:\*\?"<>\|\x00-\x1F]')  # Win/mac/Linux incompatible
def sanitize_name(name: str, max_len: int = 240) -> str:
    if name is None:
        name = ""
    # Normaliza Unicode y quita control chars / ilegales
    name = unicodedata.normalize("NFKC", str(name)).strip()
    name = _ILLEGAL_RE.sub("_", name)
    # Evita nombres vacíos o puntos especiales
    if name in {"", ".", ".."}:
        name = "_"
    # Limita longitud (deja margen para sufijos y extensión)
    if len(name) > max_len:
        name = name[:max_len]
    return name

def unique_path(base: Path) -> Path:
    """Si base existe, crea base-1, base-2, ... conservando extensión."""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    i = 1
    while True:
        candidate = base.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1

def find_source_file(src_dir: Path, content_id: str | int, glob_exts: list[str]) -> Optional[Path]:
    """
    Busca el archivo según la regla:
      - El archivo está en una subcarpeta cuyo nombre es el primer carácter del content_id.
      - El archivo se llama exactamente content_id + extensión.
    Ejemplo: content_id='a12345' -> SRC_DIR/a/a12345.jpg
    """
    content_id = str(content_id)
    if not content_id:
        return None

    subfolder = src_dir / content_id[0]  # primer carácter
    if not subfolder.is_dir():
        return None

    # # Buscar cualquier extensión posible
    # candidates = []
    # if glob_exts == ["*"]:
    #     candidates = sorted(list(subfolder.glob(f"{content_id}.*")) + list(subfolder.glob(content_id)))
    # else:
    #     for ext in glob_exts:
    #         candidates += list(subfolder.glob(f"{content_id}.{ext.strip('.')}"))
    #     if not candidates:
    #         candidates = sorted(list(subfolder.glob(f"{content_id}.*")))

    # return candidates[0] if candidates else None
    return subfolder / content_id

def copy_with_name(src: Path, dst_dir: Path, name_no_ext: str, mime_type: str, overwrite: bool) -> tuple[bool, str, Path | None]:
    """
    Copia src -> dst_dir / (sanitize(name_no_ext) + src.suffix)
    Maneja colisiones y devuelve (ok, mensaje, ruta_destino_o_None)
    """
    name_no_ext = sanitize_name(name_no_ext)
    suffix = src.suffix  # preserva extensión del origen
    # target = dst_dir / f"{name_no_ext}{suffix}"
    target = Path(dst_dir , name_no_ext)
    print("copy_with_name")
    print(target)
    if target.exists():
        if overwrite:
            action = "overwrite"
        else:
            # target = unique_path(target)
            action = "unique"
    else:
        action = "new"

    if DRY_RUN:
        return True, f"DRY_RUN {action}: {src.name} -> {target.name}", target

    try:
        print(f"src: {repr(src)}")
        print(f"target: {repr(target)}")
        print(f"file_name: {repr(name_no_ext)}")
        print(f"mime_type: {repr(mime_type)}")
        # print(type(target))
        if mime_type == 'application/x.wd.dir':
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / Path(name_no_ext))  # conserva metadatos

        # ---

        # target2 = Path("/Volumes/backupWD8TB/recuperacionWD/output/Backup/Album/Album de fotos/CLARA 2011 comunion-disney/CIMG1475.JPG")
        # target2.parent.mkdir(parents=True, exist_ok=True)
        # shutil.copy2(src, target2) 
        # subprocess.run(["cp", str(src), str(target)], check=True)
        return True, f"{action}: {src.name} -> {target.name}", target
    except Exception as e:
        return False, f"ERROR copying {src} -> {target}: {e}", None

def get_complete_path(con: sqlite3.Connection, file_id: str) -> Optional[str]:
    """
    Obtiene la ruta completa de un archivo dado su id.
    Devuelve None si no se encuentra.
    """
    path_parts = []
    current_id = file_id

    while current_id:
        sql = "SELECT name, parentID FROM Files WHERE id = ? LIMIT 1;"
        cur = con.cursor()
        cur.execute(sql, (current_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            name, parent_id = row
            if parent_id is None or parent_id == "":
                # Hemos llegado a la raíz
                break
            if current_id == file_id:
                current_id = parent_id
                continue
            path_parts.append(name)
            current_id = parent_id
        else:
            break

    if path_parts:
        return '/' + '/'.join(reversed(path_parts))
    return None

def main():
    src_dir = Path(SRC_DIR)
    dst_dir = Path(DST_DIR)
    assert src_dir.is_dir(), f"Directorio origen no existe: {src_dir}"
    dst_dir.mkdir(parents=True, exist_ok=True)

    con = open_sqlite_readonly(DB_PATH)
    sql = f'SELECT {ID_COL} AS _id, {NAME_COL} AS _name, {CONTENT_ID_COL} AS _content_id, {MIME_TYPE_COL} AS _mime_type FROM {TABLE};'

    total = ok = skipped = not_found = errors = 0

    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "id", "name", "src", "dst", "message"])

        for row in stream_rows(con, sql, fetch_size=BATCH):
            total += 1
            file_id = row["_id"]
            file_name = row["_name"]
            content_id = row["_content_id"]
            mime_type = row["_mime_type"]

            # Busca en origen
            if content_id is None:
                continue
            src = find_source_file(src_dir, content_id, GLOB_EXTS)
            # print(src)
            if src is None:
                not_found += 1
                w.writerow(["NOT_FOUND", file_id, file_name, "", "", "No source file for id"])
                continue
            # exit(1)
            dst_file_folder = get_complete_path(con, file_id)
            if dst_file_folder is None:
                continue
            # print(dst_file_folder)
            # Si ya existe un destino exacto (y OVERWRITE=False), lo consideramos "skipped" (reanudable)
            sanitized = sanitize_name(file_name)
            # Asegura que dst_file_folder sea relativa (quita cualquier / inicial)
            rel_dst_folder = dst_file_folder.lstrip("/") if dst_file_folder else ""
            dst_candidate = (dst_dir / rel_dst_folder / f"{sanitized}{src.suffix}")
            print(dst_candidate)
            if dst_candidate.exists() and not OVERWRITE:
                skipped += 1
                w.writerow(["SKIPPED", file_id, file_name, str(src), str(dst_candidate), "Destination exists"])
                continue

            success, msg, dst = copy_with_name(src, (dst_dir / rel_dst_folder ), sanitized, mime_type, OVERWRITE)
            if success:
                ok += 1
                w.writerow(["OK", file_id, file_name, str(src), str(dst) if dst else "", msg])
            else:
                errors += 1
                w.writerow(["ERROR", file_id, file_name, str(src), str(dst) if dst else "", msg])

    con.close()
    print(f"Total: {total} | OK: {ok} | SKIPPED: {skipped} | NOT_FOUND: {not_found} | ERRORS: {errors}")
    print(f"Reporte: {REPORT_CSV}")
    if DRY_RUN:
        print("⚠️ Estuviste en DRY_RUN. Pon DRY_RUN=False para ejecutar copias reales.")

if __name__ == "__main__":
    main()
